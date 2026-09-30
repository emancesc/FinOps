"""Extract linked-resource JSON files for any AWS account, across regions.

Usage: python scripts/extract_linked_resources.py --profile <profile>
           [--account <id>] [--regions all] [--out-root extracted] [--fresh | --resume]
  --account: default = account of the profile (sts get-caller-identity).
  --regions: "all" (default) = every region enabled on the account,
             or a single region / comma-separated list ("eu-south-1,eu-west-1").
  --fresh:   always extract everything again from AWS.
  --resume:  reuse the saved sections even if the previous run completed.
Output: <out-root>/<account>/<region>/*.json, kept only for regions that
contain at least one resource. Objects are saved in full (no --query trimming).

Files per region (EVIDENCE_FILES): acm_inuseby.json, acm_inuseby_full.json,
cloudformation_stacks.json, config_rules.json, eip_associations.json,
eni_attachments.json, instances_for_volumes.json, snapshots_volumeid.json,
ssm_managedinstances.json, volumes_all.json, volumes_live.json,
volumes_report.json (+ instances_all.json).

Robustness / resume:
- every run calls AWS: if the previous run completed, a new run extracts
  everything again. Saved sections are reused ONLY to resume a run that was
  interrupted (or explicitly with --resume).
- every section is written to disk as soon as it completes (atomic write),
  with its state in <region>/_state.json; the account-level progress is in
  <account>/_run.json. If the run is interrupted, re-running the same command
  reloads the completed sections from disk and only calls AWS for the rest.
- a failing AWS call only affects its own section, whose files contain
  {"error": "..."}; failed sections are retried on the next run. Permanent
  denials (SCP "explicit deny", UnauthorizedOperation, AccessDenied, ...) are
  saved as {"error": "...", "denied": true} and NOT retried (region status
  "denied" / "done_with_denied"); use --retry-denied after a permission change.
- progress percentage is weighted on real work: every section and every item
  of the long loops counts as one unit.
- inside the long per-item loops (ACM certificates, CloudFormation stacks,
  Config rules, SSM tags) every item is appended to <region>/_<section>.partial.jsonl
  as soon as it is fetched, so a resume skips the items already done; these
  loops run ITEM_WORKERS calls in parallel.
Progress: one line per completed section with the overall percentage, plus
item counters inside the long loops.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
HOME_REGION = "eu-south-1"
REPORT_SCRIPT = os.path.join(HERE, "build_volumes_report.py")
MAX_WORKERS = 6       # regioni in parallelo
ITEM_WORKERS = 4      # chiamate per-elemento in parallelo dentro una regione (ACM, stack, regole)
REGION_STATE = "_state.json"
RUN_STATE = "_run.json"

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


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

def write_json(path, data):
    """Atomic write: a crash never leaves a truncated JSON file behind."""
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)
    os.replace(tmp, path)


def read_json(path, default=None):
    try:
        # utf-8-sig: accetta anche file con BOM (Windows PowerShell 5.1)
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def is_error(data):
    return isinstance(data, dict) and "error" in data


# Dinieghi permanenti (SCP dell'organizzazione, IAM): ritentarli non serve.
# Throttling, timeout e simili restano "error" e vengono ritentati.
DENIED_PATTERNS = (
    "explicit deny",
    "UnauthorizedOperation",
    "AccessDenied",
    "not authorized to perform",
    "AuthFailure",
)


def is_denied_message(message):
    return any(p in message for p in DENIED_PATTERNS)


def is_denied(data):
    return is_error(data) and bool(data.get("denied"))


# ---------------------------------------------------------------------------
# Progress (thread-safe, unbuffered, mirrored to <account>/_run.json)
# ---------------------------------------------------------------------------

class Progress:
    """
    La percentuale e' pesata sul lavoro reale: ogni sezione vale 1 unita' e
    ogni elemento di un ciclo lungo (certificato ACM, stack, regola, istanza
    SSM) vale 1 unita'. Quando un ciclo scopre N elementi il totale cresce di N,
    quindi un ciclo da 920 certificati sposta davvero la percentuale.
    """

    def __init__(self, run_path, regions, sections_per_region, resumed_regions):
        self._lock = threading.Lock()
        self._run_path = run_path
        self.sections_total = len(regions) * sections_per_region
        self.sections_done = len(resumed_regions) * sections_per_region
        self.items_total = 0
        self.items_done = 0
        self.state = read_json(run_path, {}) or {}
        self.state.setdefault("started_at", _now())
        self.state.pop("finished_at", None)
        self.state["regions"] = {
            r: (self.state.get("regions", {}).get(r) if r in resumed_regions else "pending") for r in regions
        }
        self._save()

    @property
    def pct(self):
        total = self.sections_total + self.items_total
        return 100.0 * (self.sections_done + self.items_done) / total if total else 100.0

    def _save(self):
        self.state.update({
            "progress_pct": round(self.pct, 1),
            "sections_done": self.sections_done, "sections_total": self.sections_total,
            "items_done": self.items_done, "items_total": self.items_total,
            "updated_at": _now(),
        })
        write_json(self._run_path, self.state)

    def log(self, message):
        with self._lock:
            print(message, flush=True)

    def section(self, region, name, status, detail=""):
        with self._lock:
            self.sections_done += 1
            print(f"[{self.pct:5.1f}%] ({self.sections_done}/{self.sections_total}) {region:15s} {name:16s} {status}"
                  f"{'  ' + detail if detail else ''}", flush=True)
            self._save()

    def region_status(self, region, status):
        with self._lock:
            self.state["regions"][region] = status
            self._save()

    def add_items(self, total, already_done=0):
        with self._lock:
            self.items_total += total
            self.items_done += already_done
            self._save()

    def item(self, region, name, i, n, every=10):
        with self._lock:
            self.items_done += 1
            if n and (i == 1 or i == n or i % every == 0):
                print(f"[{self.pct:5.1f}%] {'':>9} {region:15s} {name:16s} {i}/{n}", flush=True)
                self._save()


# ---------------------------------------------------------------------------
# Sections: each returns {file_name: data}; ctx gives region, previously
# computed data and an item-progress callback.
# ---------------------------------------------------------------------------

class Ctx:
    def __init__(self, region, out_dir, progress, data):
        self.region = region
        self.out_dir = out_dir
        self.progress = progress
        self.data = data

    def item(self, section, i, n):
        self.progress.item(self.region, section, i, n)

    def partial_path(self, section):
        return os.path.join(self.out_dir, f"_{section}.partial.jsonl")


def map_items(ctx, section, items, key, fn):
    """
    Applica fn a ogni elemento con ITEM_WORKERS chiamate in parallelo.
    Ogni risultato viene aggiunto subito a _<section>.partial.jsonl: dopo
    un'interruzione, la ripresa salta gli elementi gia' elaborati.
    Ritorna i risultati nell'ordine di items (None esclusi).
    """
    path = ctx.partial_path(section)
    results = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue  # ultima riga troncata da un crash
                results[rec["key"]] = rec["value"]
        if results:
            ctx.progress.log(f"         {'':>9} {ctx.region:15s} {section:16s} resume: {len(results)}/{len(items)} from disk")
    lock = threading.Lock()
    todo = [i for i in items if key(i) not in results]
    ctx.progress.add_items(len(items), already_done=len(items) - len(todo))

    def work(item):
        value = fn(item)
        line = json.dumps({"key": key(item), "value": value}, ensure_ascii=False, default=str)
        with lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            results[key(item)] = value
            ctx.item(section, len(results), len(items))

    if todo:
        with ThreadPoolExecutor(max_workers=ITEM_WORKERS) as pool:
            list(pool.map(work, todo))
    return [results[key(i)] for i in items if results.get(key(i)) is not None]


def _ssm(ctx):
    """SSM managed instances -> linked EC2 instance / hybrid node."""
    region = ctx.region
    ssm = aws_json(region, "ssm", "describe-instance-information").get("InstanceInformationList", [])

    def with_tags(inst):
        tags = None
        if inst["InstanceId"].startswith("mi-"):
            try:
                tags = aws_json(region, "ssm", "list-tags-for-resource", "--resource-type", "ManagedInstance",
                                "--resource-id", inst["InstanceId"]).get("TagList", [])
            except RuntimeError:
                pass
        return {**inst, "Tags": tags}

    return {"ssm_managedinstances.json": map_items(ctx, "ssm", ssm, lambda i: i["InstanceId"], with_tags)}


def _cloudformation(ctx):
    """CloudFormation stacks + resources they own."""
    region = ctx.region
    stacks = aws_json(region, "cloudformation", "describe-stacks").get("Stacks", [])

    def with_resources(st):
        try:
            resources = aws_json(
                region, "cloudformation", "list-stack-resources", "--stack-name", st["StackId"]
            ).get("StackResourceSummaries", [])
        except RuntimeError as e:
            resources = {"error": str(e)}
        return {**st, "StackResources": resources}

    return {"cloudformation_stacks.json": map_items(ctx, "cloudformation", stacks, lambda st: st["StackId"], with_resources)}


def _eip(ctx):
    """EIP associations (all EIPs, incl. unassociated, with link fields)."""
    return {"eip_associations.json": aws_json(ctx.region, "ec2", "describe-addresses").get("Addresses", [])}


def _acm(ctx):
    """ACM certificates (full detail + tags), including InUseBy."""
    region = ctx.region
    cert_summary = aws_json(region, "acm", "list-certificates", "--includes",
                            "keyTypes=RSA_1024,RSA_2048,RSA_3072,RSA_4096,EC_prime256v1,EC_secp384r1,EC_secp521r1"
                            ).get("CertificateSummaryList", [])

    def detail(cert):
        arn = cert["CertificateArn"]
        cert_detail = aws_json(region, "acm", "describe-certificate", "--certificate-arn", arn).get("Certificate")
        if not cert_detail:
            return None
        try:
            cert_detail["Tags"] = aws_json(region, "acm", "list-tags-for-certificate",
                                           "--certificate-arn", arn).get("Tags", [])
        except RuntimeError:
            cert_detail["Tags"] = None
        return cert_detail

    full = map_items(ctx, "acm", cert_summary, lambda c: c["CertificateArn"], detail)
    return {"acm_inuseby_full.json": full, "acm_inuseby.json": [c for c in full if c.get("InUseBy")]}


def _config_rules(ctx):
    """AWS Config rules + scope / compliance."""
    region = ctx.region
    rules = aws_json(region, "configservice", "describe-config-rules").get("ConfigRules", [])
    try:
        compliance = {
            c["ConfigRuleName"]: c.get("Compliance")
            for c in aws_json(region, "configservice", "describe-compliance-by-config-rule").get("ComplianceByConfigRules", [])
        }
    except RuntimeError:
        compliance = {}

    def with_tags(rule):
        try:
            tags = aws_json(region, "configservice", "list-tags-for-resource",
                            "--resource-arn", rule["ConfigRuleArn"]).get("Tags", [])
        except RuntimeError:
            tags = None
        return {**rule, "Compliance": compliance.get(rule["ConfigRuleName"]), "Tags": tags}

    return {"config_rules.json": map_items(ctx, "config_rules", rules, lambda r: r["ConfigRuleName"], with_tags)}


def _eni(ctx):
    """Network interfaces (all, with attachment / requester info)."""
    return {"eni_attachments.json": aws_json(ctx.region, "ec2", "describe-network-interfaces").get("NetworkInterfaces", [])}


def _volumes(ctx):
    """EBS volumes (all + in-use)."""
    volumes = aws_json(ctx.region, "ec2", "describe-volumes").get("Volumes", [])
    return {"volumes_all.json": volumes, "volumes_live.json": [v for v in volumes if v.get("State") == "in-use"]}


def _snapshots(ctx):
    """Snapshots by source volume."""
    snaps = aws_json(ctx.region, "ec2", "describe-snapshots", "--owner-ids", "self").get("Snapshots", [])
    return {"snapshots_volumeid.json": [s for s in snaps if s.get("VolumeId")]}


def _instances(ctx):
    """All EC2 instances (full objects + flat keys for build_volumes_report.py) and those with volumes."""
    instances = []
    for res in aws_json(ctx.region, "ec2", "describe-instances").get("Reservations", []):
        for i in res.get("Instances", []):
            tags = {t["Key"]: t["Value"] for t in i.get("Tags") or []}
            instances.append({**i, "Name": tags.get("Name"), "State": i["State"]["Name"], "StateDetail": i["State"]})
    volumes = ctx.data.get("volumes_all.json")
    attached_ids = {a["InstanceId"] for v in (volumes if isinstance(volumes, list) else [])
                    for a in v.get("Attachments") or []}
    return {"instances_all.json": instances,
            "instances_for_volumes.json": [i for i in instances if i["InstanceId"] in attached_ids]}


def _volumes_report(ctx):
    """Consolidated volume -> instance / snapshot report (volumes_report.json)."""
    for name in ("volumes_all.json", "instances_for_volumes.json", "snapshots_volumeid.json"):
        if not isinstance(ctx.data.get(name), list):
            raise RuntimeError(f"volumes_report non generato: {name} contiene un errore")
    res = subprocess.run([sys.executable, REPORT_SCRIPT, ctx.out_dir], capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or "build_volumes_report failed")
    return {"volumes_report.json": read_json(os.path.join(ctx.out_dir, "volumes_report.json"), [])}


SECTIONS = [
    ("ssm", _ssm, ["ssm_managedinstances.json"]),
    ("cloudformation", _cloudformation, ["cloudformation_stacks.json"]),
    ("eip", _eip, ["eip_associations.json"]),
    ("acm", _acm, ["acm_inuseby_full.json", "acm_inuseby.json"]),
    ("config_rules", _config_rules, ["config_rules.json"]),
    ("eni", _eni, ["eni_attachments.json"]),
    ("volumes", _volumes, ["volumes_all.json", "volumes_live.json"]),
    ("snapshots", _snapshots, ["snapshots_volumeid.json"]),
    ("instances", _instances, ["instances_all.json", "instances_for_volumes.json"]),
    ("volumes_report", _volumes_report, ["volumes_report.json"]),
]


# ---------------------------------------------------------------------------
# Region / account orchestration
# ---------------------------------------------------------------------------

def _count(data):
    return f"{len(data)} items" if isinstance(data, list) else "denied" if is_denied(data) else "error"


REPORT_INPUTS = ("volumes_all.json", "instances_for_volumes.json", "snapshots_volumeid.json")


def run_region(region, account, out_root, progress, fresh=False, retry_denied=False):
    """
    Extract one region section by section, persisting each one immediately.
    Section status: "done", "denied" (permanent: SCP/IAM, not retried unless
    retry_denied) or "error" (retried on the next run).
    """
    out_dir = os.path.join(out_root, account, region)
    os.makedirs(out_dir, exist_ok=True)
    state_path = os.path.join(out_dir, REGION_STATE)
    state = {} if fresh else (read_json(state_path, {}) or {})
    sections_state = state.setdefault("sections", {})
    state["status"] = "running"
    state.setdefault("started_at", _now())
    write_json(state_path, state)
    progress.region_status(region, "running")

    data = {}
    ctx = Ctx(region, out_dir, progress, data)
    for name, fn, files in SECTIONS:
        paths = [os.path.join(out_dir, f) for f in files]
        keep = ("done", "denied") if not retry_denied else ("done",)
        if sections_state.get(name) in keep and all(os.path.exists(p) for p in paths):
            for f, p in zip(files, paths):
                data[f] = read_json(p)
            label = "resumed from disk" if sections_state[name] == "done" else "denied (skipped)"
            progress.section(region, name, label, _count(data[files[0]]))
            continue
        try:
            result = fn(ctx)
            status = "done"
        except RuntimeError as e:
            message = str(e)
            # Il report deriva da altre sezioni: e' "denied" se lo e' un suo input
            derived_denied = name == "volumes_report" and any(is_denied(data.get(f)) for f in REPORT_INPUTS)
            denied = is_denied_message(message) or derived_denied
            status = "denied" if denied else "error"
            result = {f: ({"error": message, "denied": True} if denied else {"error": message}) for f in files}
        for f, p in zip(files, paths):
            data[f] = result[f]
            write_json(p, result[f])
        sections_state[name] = status
        write_json(state_path, state)
        if status == "done" and os.path.exists(ctx.partial_path(name)):
            os.remove(ctx.partial_path(name))
        progress.section(region, name, status, _count(result[files[0]]))

    raw = [v for k, v in data.items() if k != "volumes_report.json"]
    if not any(isinstance(v, list) and v for v in data.values()):
        shutil.rmtree(out_dir, ignore_errors=True)
        if all(is_error(v) for v in raw) and not all(is_denied(v) for v in raw):
            progress.region_status(region, "failed")
            return region, None, next(v["error"] for v in raw if is_error(v) and not is_denied(v))
        # Nessun dato: regione vuota, oppure negata (SCP/IAM) su tutto cio' che contiene risorse
        status = "denied" if any(is_denied(v) for v in raw) else "empty"
        progress.region_status(region, status)
        return region, status, None

    statuses = set(sections_state.values())
    if statuses <= {"done"}:
        state["status"] = "done"
    elif statuses <= {"done", "denied"}:
        state["status"] = "done_with_denied"
    else:
        state["status"] = "partial"
    state["finished_at"] = _now()
    write_json(state_path, state)
    progress.region_status(region, state["status"])
    return region, {f: (len(v) if isinstance(v, list) else "denied" if is_denied(v) else "error")
                    for f, v in data.items()}, None


def main(argv=None):
    global _profile
    parser = argparse.ArgumentParser(description="Extract linked-resource JSON files for an AWS account")
    parser.add_argument("--profile", required=True, help="AWS CLI profile")
    parser.add_argument("--account", default=None, help="account id (default: from sts get-caller-identity)")
    parser.add_argument("--regions", default="all", help='"all" (default), one region or "a,b"')
    parser.add_argument("--out-root", default=os.path.join(REPO, "extracted"), help="output root folder")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--fresh", action="store_true", help="always extract everything again from AWS")
    mode.add_argument("--resume", action="store_true", help="reuse saved sections even if the previous run completed")
    parser.add_argument("--retry-denied", action="store_true",
                        help="retry sections/regions denied by SCP/IAM (e.g. after a permission change)")
    args = parser.parse_args(argv)
    _profile = args.profile

    account = args.account or aws_json(HOME_REGION, "sts", "get-caller-identity")["Account"]
    regions = resolve_regions(args.regions)
    account_dir = os.path.join(args.out_root, account)
    os.makedirs(account_dir, exist_ok=True)
    run_path = os.path.join(account_dir, RUN_STATE)
    # Run precedente completato -> nuova estrazione completa da AWS;
    # run interrotto -> ripresa dalle sezioni salvate (o --resume esplicito)
    fresh = args.fresh or (bool((read_json(run_path, {}) or {}).get("finished_at")) and not args.resume)
    if fresh and os.path.exists(run_path):
        os.remove(run_path)

    previous = (read_json(run_path, {}) or {}).get("regions", {})
    # Regioni gia' concluse in un run precedente: non vengono rieseguite
    final = ("done", "empty") if args.retry_denied else ("done", "empty", "denied", "done_with_denied")
    finished = [r for r in regions if previous.get(r) in final]
    todo = [r for r in regions if r not in finished]
    progress = Progress(run_path, regions, len(SECTIONS), finished)
    progress.log(f"Account {account}: {len(regions)} regions, {len(SECTIONS)} sections each"
                 f" -> {', '.join(regions)}")
    if finished:
        progress.log(f"Resume: {len(finished)} regions already completed ({', '.join(finished)})")
    progress.log(f"Progress file: {run_path}")
    progress.log("Mode: " + ("full extraction from AWS" if fresh else "resume of an interrupted run (saved sections are reused)"))

    results = [(r, "resumed", None) for r in finished]
    if todo:
        with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(todo))) as pool:
            results += list(pool.map(lambda r: run_region(r, account, args.out_root, progress, fresh, args.retry_denied), todo))

    progress.log("")
    for region, counts, error in sorted(results):
        if error:
            progress.log(f"[{region}] ERROR: {error.splitlines()[0]}")
        elif counts == "resumed":
            progress.log(f"[{region}] already completed in a previous run ({previous.get(region)})")
        elif counts == "denied":
            progress.log(f"[{region}] denied by SCP/IAM, no data, skipped")
        elif counts in ("empty", None):
            progress.log(f"[{region}] no resources, skipped")
        else:
            progress.log(f"[{region}] written to {os.path.join(account, region)}")
            for name, count in counts.items():
                progress.log(f"    {name:40s} items={count}")
    progress.state["finished_at"] = _now()
    progress._save()
    return results


if __name__ == "__main__":
    main()
