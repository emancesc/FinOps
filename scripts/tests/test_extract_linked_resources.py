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


def _region_dirs(account_dir):
    return sorted(d for d in os.listdir(account_dir) if os.path.isdir(os.path.join(account_dir, d)))


def _assert_evidence(account_dir, expected_files):
    # eu-west-1 non ha risorse: non viene scritta
    assert _region_dirs(account_dir) == ["eu-south-1", "us-east-1"]

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
    ssm = _load(os.path.join(us, "ssm_managedinstances.json"))
    assert "AccessDenied" in ssm["error"] and ssm["denied"] is True
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
    assert _region_dirs(tmp_path / ACCOUNT) == ["eu-south-1"]


class _Crash(BaseException):
    """Simula un'interruzione brusca (kill, token scaduto non gestito, ...)."""


def test_python_progress_output(mod, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(mod, "aws_json", fake_aws)
    mod.main(["--profile", "p", "--account", ACCOUNT, "--regions", "eu-south-1", "--out-root", str(tmp_path)])
    out = capsys.readouterr().out
    assert f"(1/{len(mod.SECTIONS)})" in out and "100.0%" in out
    assert "acm" in out and "2/2" in out  # contatore elementi nel ciclo ACM
    run = _load(tmp_path / ACCOUNT / "_run.json")
    assert run["progress_pct"] == 100.0 and run["regions"] == {"eu-south-1": "done"}


def test_python_crash_persists_and_resumes(mod, tmp_path, monkeypatch):
    calls = []

    def crashing(region, *args):
        if args[:2] == ("ec2", "describe-instances"):
            raise _Crash()
        return fake_aws(region, *args)

    monkeypatch.setattr(mod, "aws_json", crashing)
    argv = ["--profile", "p", "--account", ACCOUNT, "--regions", "eu-south-1", "--out-root", str(tmp_path)]
    with pytest.raises(_Crash):
        mod.main(argv)

    # Le sezioni completate prima del crash sono gia' su disco
    eu = tmp_path / ACCOUNT / "eu-south-1"
    assert len(_load(eu / "volumes_all.json")) == 2
    assert len(_load(eu / "acm_inuseby_full.json")) == 2
    state = _load(eu / "_state.json")
    assert state["sections"]["volumes"] == "done" and "instances" not in state["sections"]
    assert not list(eu.glob("*.tmp"))

    def recording(region, *args):
        calls.append(args[:2])
        return fake_aws(region, *args)

    monkeypatch.setattr(mod, "aws_json", recording)
    mod.main(argv)
    # Ripresa: nessuna chiamata per le sezioni gia' completate, solo le mancanti
    assert ("acm", "list-certificates") not in calls
    assert ("ec2", "describe-volumes") not in calls
    assert ("ec2", "describe-instances") in calls
    assert [i["InstanceId"] for i in _load(eu / "instances_for_volumes.json")] == ["i-1"]
    assert _load(eu / "volumes_report.json")[0]["VolumeId"] in {"vol-1", "vol-2"}
    assert _load(tmp_path / ACCOUNT / "_run.json")["regions"]["eu-south-1"] == "done"

    # Terzo run con --resume: regione gia' conclusa, nessuna chiamata AWS
    calls.clear()
    mod.main(argv + ["--resume"])
    assert calls == []

    # Quarto run senza opzioni: il run precedente e' completato -> nuova estrazione completa da AWS
    mod.main(argv)
    assert ("acm", "list-certificates") in calls and ("ec2", "describe-volumes") in calls


def test_python_crash_inside_item_loop_resumes_items(mod, tmp_path, monkeypatch):
    """Crash a meta' del ciclo ACM: i certificati gia' scaricati non vengono richiesti di nuovo."""
    monkeypatch.setattr(mod, "ITEM_WORKERS", 1)  # ordine deterministico

    def crashing(region, *args):
        if args[:2] == ("acm", "describe-certificate") and "arn:cert-2" in args:
            raise _Crash()
        return fake_aws(region, *args)

    monkeypatch.setattr(mod, "aws_json", crashing)
    argv = ["--profile", "p", "--account", ACCOUNT, "--regions", "eu-south-1", "--out-root", str(tmp_path)]
    with pytest.raises(_Crash):
        mod.main(argv)
    eu = tmp_path / ACCOUNT / "eu-south-1"
    partial = (eu / "_acm.partial.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["key"] for line in partial] == ["arn:cert-1"]

    calls = []

    def recording(region, *args):
        calls.append(args)
        return fake_aws(region, *args)

    monkeypatch.setattr(mod, "aws_json", recording)
    mod.main(argv)
    described = [a[a.index("--certificate-arn") + 1] for a in calls if a[:2] == ("acm", "describe-certificate")]
    assert described == ["arn:cert-2"]
    assert len(_load(eu / "acm_inuseby_full.json")) == 2
    assert not (eu / "_acm.partial.jsonl").exists()


def _recording(calls, base=fake_aws):
    def fake(region, *args):
        calls.append((region, *args[:2]))
        return base(region, *args)
    return fake


def test_python_denied_is_not_retried(mod, tmp_path, monkeypatch):
    """SCP/IAM: la sezione diventa "denied" e non viene ritentata, salvo --retry-denied."""
    monkeypatch.setattr(mod, "aws_json", fake_aws)
    argv = ["--profile", "p", "--account", ACCOUNT, "--regions", "us-east-1", "--out-root", str(tmp_path)]
    mod.main(argv)
    us = tmp_path / ACCOUNT / "us-east-1"
    assert _load(us / "_state.json")["sections"]["ssm"] == "denied"
    assert _load(tmp_path / ACCOUNT / "_run.json")["regions"]["us-east-1"] == "done_with_denied"

    calls = []
    monkeypatch.setattr(mod, "aws_json", _recording(calls))
    mod.main(argv + ["--resume"])
    assert calls == []  # ripresa: regione conclusa (con dinieghi), nessuna chiamata

    mod.main(argv + ["--resume", "--retry-denied"])
    assert ("us-east-1", "ssm", "describe-instance-information") in calls
    assert ("us-east-1", "acm", "list-certificates") not in calls  # le sezioni "done" restano da disco


def test_python_region_fully_denied(mod, tmp_path, monkeypatch):
    """Regione bloccata da SCP (ACM risponde vuoto, il resto e' negato): stato "denied", niente cartella."""
    def scp(region, *args):
        if args[:2] in {("acm", "list-certificates"), ("configservice", "describe-config-rules")}:
            return fake_aws("eu-west-1", *args)
        raise RuntimeError("AccessDeniedException ... with an explicit deny in a service control policy")

    monkeypatch.setattr(mod, "aws_json", scp)
    argv = ["--profile", "p", "--account", ACCOUNT, "--regions", "ap-south-1", "--out-root", str(tmp_path)]
    results = mod.main(argv)
    assert results == [("ap-south-1", "denied", None)]
    assert not (tmp_path / ACCOUNT / "ap-south-1").exists()
    assert _load(tmp_path / ACCOUNT / "_run.json")["regions"]["ap-south-1"] == "denied"

    calls = []
    monkeypatch.setattr(mod, "aws_json", _recording(calls, scp))
    mod.main(argv + ["--resume"])
    assert calls == []


def test_python_progress_weighted_on_items(mod, tmp_path, monkeypatch, capsys):
    """Con molti certificati la percentuale deve riflettere il ciclo ACM, non solo le sezioni."""
    many = [f"arn:cert-{i}" for i in range(200)]

    def lots_of_certs(region, *args):
        if args[:2] == ("acm", "list-certificates"):
            return {"CertificateSummaryList": [{"CertificateArn": a} for a in many]}
        return fake_aws(region, *args)

    monkeypatch.setattr(mod, "aws_json", lots_of_certs)
    mod.main(["--profile", "p", "--account", ACCOUNT, "--regions", "eu-south-1", "--out-root", str(tmp_path)])
    lines = capsys.readouterr().out.splitlines()
    acm_done = next(line for line in lines if " acm " in line and " done " in line)
    pct = float(acm_done.split("%")[0].strip("[ "))
    # 3 sezioni + 200 certificati su 10 sezioni + 200 elementi (+ SSM/CFN/regole): ben oltre il 30% "a sezioni"
    assert pct > 90, acm_done
    run = _load(tmp_path / ACCOUNT / "_run.json")
    assert run["items_total"] >= 200 and run["items_done"] == run["items_total"]
    assert run["progress_pct"] == 100.0


def test_python_failed_section_is_retried(mod, tmp_path, monkeypatch):
    def denied_snapshots(region, *args):
        if args[:2] == ("ec2", "describe-snapshots"):
            raise RuntimeError("Throttling")
        return fake_aws(region, *args)

    monkeypatch.setattr(mod, "aws_json", denied_snapshots)
    argv = ["--profile", "p", "--account", ACCOUNT, "--regions", "eu-south-1", "--out-root", str(tmp_path)]
    mod.main(argv)
    eu = tmp_path / ACCOUNT / "eu-south-1"
    assert "Throttling" in _load(eu / "snapshots_volumeid.json")["error"]
    assert _load(tmp_path / ACCOUNT / "_run.json")["regions"]["eu-south-1"] == "partial"

    calls = []

    def recording(region, *args):
        calls.append(args[:2])
        return fake_aws(region, *args)

    monkeypatch.setattr(mod, "aws_json", recording)
    mod.main(argv + ["--resume"])
    assert ("ec2", "describe-snapshots") in calls
    assert ("acm", "list-certificates") not in calls
    assert [s["SnapshotId"] for s in _load(eu / "snapshots_volumeid.json")] == ["snap-1"]
    assert _load(tmp_path / ACCOUNT / "_run.json")["regions"]["eu-south-1"] == "done"


# PowerShell 7 (pwsh) e Windows PowerShell 5.1: serializzano JSON/encoding in modo diverso
_SHELLS = [shutil.which(name) for name in ("pwsh", "powershell")]


def _ps_env(tmp_path, **extra):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    # Finto `aws` e `python` in testa al PATH
    (bin_dir / "aws.cmd").write_text(
        f'@"{sys.executable}" "{os.path.join(HERE, "fake_aws_cli.py")}" %*\n', encoding="ascii")
    (bin_dir / "python.cmd").write_text(f'@"{sys.executable}" %*\n', encoding="ascii")
    return {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", **extra}


def _run_ps(shell, env, out_root, regions="all", *extra):
    res = subprocess.run(
        [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", SCRIPT_PS1,
         "-AwsProfile", "p", "-Regions", regions, "-OutRoot", str(out_root), *extra],
        capture_output=True, text=True, env=env, timeout=300,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    return res.stdout


def _logged_calls(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f]


@pytest.mark.skipif(os.name != "nt", reason="richiede PowerShell su Windows")
@pytest.mark.parametrize("shell", _SHELLS, ids=["pwsh", "powershell"])
def test_powershell_all_regions(mod, tmp_path, shell):
    if not shell:
        pytest.skip("shell non installata")
    out_root = tmp_path / "out"
    out = _run_ps(shell, _ps_env(tmp_path), out_root)
    _assert_evidence(out_root / ACCOUNT, mod.EVIDENCE_FILES)
    assert "100.0%" in out and f"/{3 * len(mod.SECTIONS)})" in out
    assert _load(out_root / ACCOUNT / "_run.json")["regions"] == {
        "eu-south-1": "done", "eu-west-1": "empty", "us-east-1": "done_with_denied"}


@pytest.mark.skipif(os.name != "nt", reason="richiede PowerShell su Windows")
@pytest.mark.parametrize("shell", _SHELLS, ids=["pwsh", "powershell"])
def test_powershell_resume(mod, tmp_path, shell):
    if not shell:
        pytest.skip("shell non installata")
    out_root = tmp_path / "out"
    eu = out_root / ACCOUNT / "eu-south-1"
    eu.mkdir(parents=True)
    # Residuo di un crash a meta' del ciclo ACM: cert-1 gia' scaricato
    (eu / "_acm.partial.jsonl").write_text(
        json.dumps({"key": "arn:cert-1", "value": {"CertificateArn": "arn:cert-1", "InUseBy": ["arn:elb"], "Tags": []}}) + "\n",
        encoding="utf-8")
    log = tmp_path / "calls.jsonl"

    _run_ps(shell, _ps_env(tmp_path, FAKE_AWS_LOG=str(log), FAKE_AWS_FAIL="ec2 describe-snapshots"), out_root, "eu-south-1")
    described = [c[-1] for c in _logged_calls(log) if c[1:3] == ["acm", "describe-certificate"]]
    assert described == ["arn:cert-2"]
    assert len(_load(eu / "acm_inuseby_full.json")) == 2
    assert not (eu / "_acm.partial.jsonl").exists()
    assert "error" in _load(eu / "snapshots_volumeid.json")
    assert _load(eu / "_state.json")["sections"]["snapshots"] == "error"

    # Secondo run in ripresa: solo la sezione in errore viene ritentata
    log.unlink()
    out = _run_ps(shell, _ps_env(tmp_path, FAKE_AWS_LOG=str(log)), out_root, "eu-south-1", "-Resume")
    ops = {tuple(c[1:3]) for c in _logged_calls(log)}
    assert ("ec2", "describe-snapshots") in ops
    assert ("acm", "list-certificates") not in ops and ("ec2", "describe-volumes") not in ops
    assert "resumed from disk" in out
    assert [s["SnapshotId"] for s in _load(eu / "snapshots_volumeid.json")] == ["snap-1"]
    report = {v["VolumeId"]: v for v in _load(eu / "volumes_report.json")}
    assert report["vol-1"]["Snapshots"][0]["SnapshotId"] == "snap-1"
    assert _load(out_root / ACCOUNT / "_run.json")["regions"]["eu-south-1"] == "done"

    # Terzo run senza opzioni: run precedente completato -> nuova estrazione completa da AWS
    log.unlink()
    out = _run_ps(shell, _ps_env(tmp_path, FAKE_AWS_LOG=str(log)), out_root, "eu-south-1")
    ops = {tuple(c[1:3]) for c in _logged_calls(log)}
    assert "full extraction from AWS" in out
    assert ("acm", "list-certificates") in ops and ("ec2", "describe-volumes") in ops
