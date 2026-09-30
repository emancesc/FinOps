"""
Test offline di scripts/extract_linked_resources.py e .ps1 (AWS finto):
- tutte le evidenze attese vengono prodotte per ogni regione con risorse
- le regioni senza risorse non vengono scritte
- un errore su una sezione (es. AccessDenied su SSM) non fa perdere le altre
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
SCRIPT_PY = os.path.join(SCRIPTS, "extract_linked_resources.py")
SCRIPT_PS1 = os.path.join(SCRIPTS, "extract_linked_resources.ps1")
ACCOUNT = "123456789012"

sys.path.insert(0, HERE)
from fake_aws_data import fake_aws  # noqa: E402


@pytest.fixture
def mod():
    spec = importlib.util.spec_from_file_location("extract_linked_resources", SCRIPT_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load(path):
    # utf-8-sig: Set-Content -Encoding UTF8 di Windows PowerShell scrive il BOM
    with open(path, encoding="utf-8-sig") as f:
        return json.load(f)


def _assert_evidence(account_dir, expected_files):
    # eu-west-1 non ha risorse: non viene scritta
    assert sorted(os.listdir(account_dir)) == ["eu-south-1", "us-east-1"]

    for region in ("eu-south-1", "us-east-1"):
        missing = set(expected_files) - set(os.listdir(os.path.join(account_dir, region)))
        assert not missing, f"{region}: mancano {missing}"

    eu = os.path.join(account_dir, "eu-south-1")
    assert len(_load(os.path.join(eu, "acm_inuseby_full.json"))) == 2
    assert [c["CertificateArn"] for c in _load(os.path.join(eu, "acm_inuseby.json"))] == ["arn:cert-1"]
    assert _load(os.path.join(eu, "cloudformation_stacks.json"))[0]["StackResources"]
    assert _load(os.path.join(eu, "config_rules.json"))[0]["Compliance"] == {"ComplianceType": "COMPLIANT"}
    assert len(_load(os.path.join(eu, "eip_associations.json"))) == 1
    assert len(_load(os.path.join(eu, "eni_attachments.json"))) == 1
    assert len(_load(os.path.join(eu, "ssm_managedinstances.json"))) == 1
    assert len(_load(os.path.join(eu, "volumes_all.json"))) == 2
    assert [v["VolumeId"] for v in _load(os.path.join(eu, "volumes_live.json"))] == ["vol-1"]
    assert [s["SnapshotId"] for s in _load(os.path.join(eu, "snapshots_volumeid.json"))] == ["snap-1"]
    assert [i["InstanceId"] for i in _load(os.path.join(eu, "instances_for_volumes.json"))] == ["i-1"]
    report = {v["VolumeId"]: v for v in _load(os.path.join(eu, "volumes_report.json"))}
    assert report["vol-1"]["Attachments"][0]["InstanceName"] == "web-01"
    assert report["vol-1"]["Attachments"][0]["InstanceState"] == "running"
    assert report["vol-1"]["Snapshots"][0]["SnapshotId"] == "snap-1"
    assert report["vol-2"]["Attachments"] == []

    # us-east-1: SSM negato -> solo quel file contiene l'errore, il resto c'e'
    us = os.path.join(account_dir, "us-east-1")
    assert "AccessDenied" in _load(os.path.join(us, "ssm_managedinstances.json"))["error"]
    assert len(_load(os.path.join(us, "acm_inuseby.json"))) == 1
    assert _load(os.path.join(us, "volumes_all.json")) == []
    assert _load(os.path.join(us, "volumes_report.json")) == []


def test_python_all_regions(mod, tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "aws_json", fake_aws)
    mod.main(["--profile", "p", "--account", ACCOUNT, "--regions", "all", "--out-root", str(tmp_path)])
    _assert_evidence(tmp_path / ACCOUNT, mod.EVIDENCE_FILES)


def test_python_region_list_and_default_account(mod, tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "aws_json", fake_aws)
    mod.main(["--profile", "p", "--regions", "eu-south-1", "--out-root", str(tmp_path)])
    assert os.listdir(tmp_path / ACCOUNT) == ["eu-south-1"]


# PowerShell 7 (pwsh) e Windows PowerShell 5.1: serializzano JSON/encoding in modo diverso
_SHELLS = [shutil.which(name) for name in ("pwsh", "powershell")]


@pytest.mark.skipif(os.name != "nt", reason="richiede PowerShell su Windows")
@pytest.mark.parametrize("shell", _SHELLS, ids=["pwsh", "powershell"])
def test_powershell_all_regions(mod, tmp_path, shell):
    if not shell:
        pytest.skip("shell non installata")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # Finto `aws` e `python` in testa al PATH
    (bin_dir / "aws.cmd").write_text(
        f'@"{sys.executable}" "{os.path.join(HERE, "fake_aws_cli.py")}" %*\n', encoding="ascii")
    (bin_dir / "python.cmd").write_text(f'@"{sys.executable}" %*\n', encoding="ascii")
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    out_root = tmp_path / "out"

    res = subprocess.run(
        [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", SCRIPT_PS1,
         "-AwsProfile", "p", "-Regions", "all", "-OutRoot", str(out_root)],
        capture_output=True, text=True, env=env, timeout=300,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    _assert_evidence(out_root / ACCOUNT, mod.EVIDENCE_FILES)
