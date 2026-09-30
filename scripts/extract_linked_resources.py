"""Extract linked-resource JSON files for any AWS account, across regions.

Usage: python scripts/extract_linked_resources.py --profile <profile>
           [--account <id>] [--regions all] [--out-root extracted]
  --account: default = account of the profile (sts get-caller-identity).
  --regions: "all" (default) = every region enabled on the account,
             or a single region / comma-separated list ("eu-south-1,eu-west-1").
Output: <out-root>/<account>/<region>/*.json, written only for regions that
contain at least one resource. Objects are saved in full (no --query trimming).

Files per region (EVIDENCE_FILES): acm_inuseby.json, acm_inuseby_full.json,
cloudformation_stacks.json, config_rules.json, eip_associations.json,
eni_attachments.json, instances_for_volumes.json, snapshots_volumeid.json,
ssm_managedinstances.json, volumes_all.json, volumes_live.json,
volumes_report.json (+ instances_all.json).
A failing AWS call only affects its own file, which then contains
{"error": "..."}; the other files of the region are still written.
"""
import argparse
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
HOME_REGION = "eu-south-1"
REPORT_SCRIPT = os.path.join(HERE, "build_volumes_report.py")
MAX_WORKERS = 6

EVIDENCE_FILES = [
    "acm_inuseby.json",
    "acm_inuseby_full.json",
    "cloudformation_stacks.json",
    "config_rules.json",
    "eip_associations.json",
    "eni_attachments.json",
    "instances_for_volumes.json",
    "snapshots_volumeid.json",
    "ssm_managedinstances.json",
    "volumes_all.json",
    "volumes_live.json",
    "volumes_report.json",
]

_profile = None


def aws_json(region, *args):
    cmd = ["aws", *args, "--profile", _profile, "--region", region, "--output", "json"]
    env = {**os.environ, "AWS_RETRY_MODE": "adaptive", "AWS_MAX_ATTEMPTS": "10"}
    res = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if res.returncode != 0:
        raise RuntimeError(f"AWS call failed: {' '.join(cmd)}\nSTDERR={res.stderr}")
    text = (res.stdout or "").strip()
    return json.loads(text) if text else {}


def resolve_regions(spec):
    if spec.strip().lower() != "all":
        return list(dict.fromkeys(r.strip() for r in spec.split(",") if r.strip()))
    regions = aws_json(HOME_REGION, "ec2", "describe-regions").get("Regions", [])
    return sorted(r["RegionName"] for r in regions)


def _error(exc):
    return {"error": str(exc)}


def _ssm_managed_instances(region):
    """SSM managed instances -> linked EC2 instance / hybrid node."""
    ssm = aws_json(region, "ssm", "describe-instance-information").get("InstanceInformationList", [])
    for inst in ssm:
        if not inst["InstanceId"].startswith("mi-"):
            inst["Tags"] = None
            continue
        try:
            inst["Tags"] = aws_json(region, "ssm", "list-tags-for-resource", "--resource-type", "ManagedInstance",
                                    "--resource-id", inst["InstanceId"]).get("TagList", [])
        except RuntimeError:
            inst["Tags"] = None
    return ssm


def _cloudformation_stacks(region):
    """CloudFormation stacks + resources they own."""
    stacks = aws_json(region, "cloudformation", "describe-stacks").get("Stacks", [])
    for st in stacks:
        try:
            st["StackResources"] = aws_json(
                region, "cloudformation", "list-stack-resources", "--stack-name", st["StackId"]
            ).get("StackResourceSummaries", [])
        except RuntimeError as e:
            st["StackResources"] = _error(e)
    return stacks


def _acm_certificates(region):
    """ACM certificates (full detail + tags), including InUseBy."""
    cert_summary = aws_json(region, "acm", "list-certificates", "--includes",
                            "keyTypes=RSA_1024,RSA_2048,RSA_3072,RSA_4096,EC_prime256v1,EC_secp384r1,EC_secp521r1"
                            ).get("CertificateSummaryList", [])
    full = []
    for cert in cert_summary:
        detail = aws_json(region, "acm", "describe-certificate", "--certificate-arn", cert["CertificateArn"]).get("Certificate")
        if not detail:
            continue
        try:
            detail["Tags"] = aws_json(region, "acm", "list-tags-for-certificate",
                                      "--certificate-arn", cert["CertificateArn"]).get("Tags", [])
        except RuntimeError:
            detail["Tags"] = None
        full.append(detail)
    return full


def _config_rules(region):
    """AWS Config rules + scope / compliance."""
    rules = aws_json(region, "configservice", "describe-config-rules").get("ConfigRules", [])
    try:
        compliance = {
            c["ConfigRuleName"]: c.get("Compliance")
            for c in aws_json(region, "configservice", "describe-compliance-by-config-rule").get("ComplianceByConfigRules", [])
        }
    except RuntimeError:
        compliance = {}
    for r in rules:
        r["Compliance"] = compliance.get(r["ConfigRuleName"])
        try:
            r["Tags"] = aws_json(region, "configservice", "list-tags-for-resource",
                                 "--resource-arn", r["ConfigRuleArn"]).get("Tags", [])
        except RuntimeError:
            r["Tags"] = None
    return rules


def _instances(region):
    """All EC2 instances, full objects + flat keys used by build_volumes_report.py."""
    instances = []
    for res in aws_json(region, "ec2", "describe-instances").get("Reservations", []):
        for i in res.get("Instances", []):
            tags = {t["Key"]: t["Value"] for t in i.get("Tags") or []}
            instances.append({**i, "Name": tags.get("Name"), "State": i["State"]["Name"], "StateDetail": i["State"]})
    return instances


def collect_region(region):
    """Return {file_name: data} for one region; each section fails independently."""
    def safe(fn, *args):
        try:
            return fn(*args)
        except RuntimeError as e:
            return _error(e)

    files = {}
    files["ssm_managedinstances.json"] = safe(_ssm_managed_instances, region)
    files["cloudformation_stacks.json"] = safe(_cloudformation_stacks, region)
    # EIP associations (all EIPs, incl. unassociated, with link fields)
    files["eip_associations.json"] = safe(lambda: aws_json(region, "ec2", "describe-addresses").get("Addresses", []))

    acm = safe(_acm_certificates, region)
    files["acm_inuseby_full.json"] = acm
    files["acm_inuseby.json"] = [c for c in acm if c.get("InUseBy")] if isinstance(acm, list) else acm

    files["config_rules.json"] = safe(_config_rules, region)
    # Network interfaces (all, with attachment / requester info)
    files["eni_attachments.json"] = safe(
        lambda: aws_json(region, "ec2", "describe-network-interfaces").get("NetworkInterfaces", []))

    # EBS volumes (all + in-use), snapshots by source volume, instances they attach to
    volumes = safe(lambda: aws_json(region, "ec2", "describe-volumes").get("Volumes", []))
    files["volumes_all.json"] = volumes
    files["volumes_live.json"] = [v for v in volumes if v.get("State") == "in-use"] if isinstance(volumes, list) else volumes
    files["snapshots_volumeid.json"] = safe(lambda: [
        s for s in aws_json(region, "ec2", "describe-snapshots", "--owner-ids", "self").get("Snapshots", [])
        if s.get("VolumeId")
    ])
    instances = safe(_instances, region)
    files["instances_all.json"] = instances
    if isinstance(instances, list):
        attached_ids = {a["InstanceId"] for v in (volumes if isinstance(volumes, list) else [])
                        for a in v.get("Attachments") or []}
        files["instances_for_volumes.json"] = [i for i in instances if i["InstanceId"] in attached_ids]
    else:
        files["instances_for_volumes.json"] = instances
    return files


def has_data(files):
    return any(isinstance(data, list) and data for data in files.values())


def all_failed(files):
    return all(isinstance(data, dict) and "error" in data for data in files.values())


def save(out_dir, name, data):
    path = os.path.join(out_dir, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)
    return len(data) if isinstance(data, list) else "error" if isinstance(data, dict) and "error" in data else "-"


def build_report(out_dir):
    """Consolidated volume -> instance / snapshot report (volumes_report.json)."""
    inputs = ["volumes_all.json", "instances_for_volumes.json", "snapshots_volumeid.json"]
    for name in inputs:
        with open(os.path.join(out_dir, name), encoding="utf-8") as f:
            if not isinstance(json.load(f), list):
                data = {"error": f"volumes_report non generato: {name} contiene un errore"}
                return save(out_dir, "volumes_report.json", data)
    res = subprocess.run([sys.executable, REPORT_SCRIPT, out_dir], capture_output=True, text=True)
    if res.returncode != 0:
        return save(out_dir, "volumes_report.json", {"error": res.stderr.strip() or "build_volumes_report failed"})
    with open(os.path.join(out_dir, "volumes_report.json"), encoding="utf-8") as f:
        return len(json.load(f))


def run_region(region, account, out_root):
    files = collect_region(region)
    if all_failed(files):
        first = next(iter(files.values()))["error"]
        return region, None, first
    if not has_data(files):
        return region, None, None
    out_dir = os.path.join(out_root, account, region)
    os.makedirs(out_dir, exist_ok=True)
    counts = {name: save(out_dir, name, data) for name, data in files.items()}
    counts["volumes_report.json"] = build_report(out_dir)
    return region, counts, None


def main(argv=None):
    global _profile
    parser = argparse.ArgumentParser(description="Extract linked-resource JSON files for an AWS account")
    parser.add_argument("--profile", required=True, help="AWS CLI profile")
    parser.add_argument("--account", default=None, help="account id (default: from sts get-caller-identity)")
    parser.add_argument("--regions", default="all", help='"all" (default), one region or "a,b"')
    parser.add_argument("--out-root", default=os.path.join(REPO, "extracted"), help="output root folder")
    args = parser.parse_args(argv)
    _profile = args.profile

    account = args.account or aws_json(HOME_REGION, "sts", "get-caller-identity")["Account"]
    regions = resolve_regions(args.regions)
    print(f"Account {account}: {len(regions)} regions -> {', '.join(regions)}")
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(regions))) as pool:
        results = list(pool.map(lambda r: run_region(r, account, args.out_root), regions))

    for region, counts, error in results:
        if error:
            print(f"[{region}] ERROR: {error.splitlines()[0]}")
        elif counts is None:
            print(f"[{region}] no resources, skipped")
        else:
            print(f"[{region}] written to {os.path.join(account, region)}")
            for name, count in counts.items():
                print(f"    {name:40s} items={count}")
    return results


if __name__ == "__main__":
    main()
