"""
Export del describe completo di volumi EBS per un account qualsiasi.

Uso:
  python -m app.volume_describe --profile <profilo> [--region all] \
      [--volume-id vol-1 --volume-id vol-2 | --volume-ids-file ids.txt]

Senza --volume-id / --volume-ids-file esporta tutti i volumi delle regioni
indicate. --volume-ids-file accetta un .txt (un ID per riga) oppure un JSON
prodotto da questo script (usa "volume_ids_requested").
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import boto3

from .aws_client import DEFAULT_HOME_REGION, parse_region_spec


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "isoformat") and callable(value.isoformat):
        try:
            return value.isoformat()
        except TypeError:
            pass
    if isinstance(value, (set, tuple)):
        return list(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_volume_ids(path: str) -> list[str]:
    text = Path(path).read_text(encoding="utf-8")
    if path.lower().endswith(".json"):
        data = json.loads(text)
        ids = data.get("volume_ids_requested") if isinstance(data, dict) else data
        return [str(v) for v in ids or []]
    return [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")]


def _resolve_regions(session: boto3.Session, region: str, home_region: str) -> list[str]:
    spec = parse_region_spec(region)
    if spec:
        return spec
    resp = session.client("ec2", region_name=home_region).describe_regions()
    return sorted(r["RegionName"] for r in resp.get("Regions", []))


def export_volume_describe(
    profile_name: str | None = None,
    region: str = "all",
    output_dir: str = "exports",
    filename: str | None = None,
    volume_ids: list[str] | None = None,
) -> dict[str, Any]:
    """
    Describe completo dei volumi (tutti, o solo quelli richiesti) in tutte le
    regioni indicate ("all" = tutte le regioni abilitate). Con una lista di ID
    si usa il filtro volume-id invece di VolumeIds: VolumeIds fallisce
    (InvalidVolume.NotFound) appena un ID non esiste nella regione interrogata.
    """
    requested = list(dict.fromkeys(volume_ids or []))
    home_region = (parse_region_spec(region) or [DEFAULT_HOME_REGION])[0]
    session = boto3.Session(profile_name=profile_name, region_name=home_region)
    identity = session.client("sts", region_name=home_region).get_caller_identity()
    account_id = identity.get("Account")

    regions = _resolve_regions(session, region, home_region)
    # Il filtro accetta al massimo 200 valori: interroghiamo a blocchi.
    filter_chunks = [requested[i:i + 200] for i in range(0, len(requested), 200)] or [None]
    volumes: list[dict[str, Any]] = []
    errors: dict[str, str] = {}
    for reg in regions:
        paginator = session.client("ec2", region_name=reg).get_paginator("describe_volumes")
        try:
            for chunk in filter_chunks:
                kwargs = {"Filters": [{"Name": "volume-id", "Values": chunk}]} if chunk else {}
                for page in paginator.paginate(**kwargs):
                    for vol in page.get("Volumes", []):
                        vol["Region"] = reg
                        volumes.append(vol)
        except Exception as exc:  # noqa: BLE001
            errors[reg] = str(exc)

    found = {v["VolumeId"] for v in volumes}
    payload = {
        "account_id": account_id,
        "account_arn": identity.get("Arn"),
        "profile": profile_name,
        "region": region,
        "regions": regions,
        "generated_at": _utc_now(),
        "volume_count": len(volumes),
        "volume_ids_requested": requested,
        "volume_ids_not_found": [v for v in requested if v not in found],
        "region_errors": errors,
        "volumes": volumes,
    }

    target_dir = Path(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    file_path = target_dir / (filename or f"volume_describe_{account_id}.json")
    file_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=_json_default),
        encoding="utf-8",
    )

    payload["file_path"] = str(file_path)
    return payload


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Export del describe completo di volumi EBS (multi-regione)")
    parser.add_argument("--profile", default=None, help="profilo AWS CLI (default: catena credenziali standard)")
    parser.add_argument("--region", default="all", help='"all" (default), una regione o lista "a,b"')
    parser.add_argument("--output-dir", default="exports")
    parser.add_argument("--filename", default=None, help="default: volume_describe_<account>.json")
    parser.add_argument("--volume-id", action="append", dest="volume_ids", default=None)
    parser.add_argument("--volume-ids-file", default=None, help=".txt (un ID per riga) o JSON di un export precedente")
    args = parser.parse_args()

    ids = list(args.volume_ids or [])
    if args.volume_ids_file:
        ids.extend(load_volume_ids(args.volume_ids_file))

    result = export_volume_describe(
        profile_name=args.profile,
        region=args.region,
        output_dir=args.output_dir,
        filename=args.filename,
        volume_ids=ids or None,
    )
    print(json.dumps({"file_path": result["file_path"], "count": result["volume_count"]}, indent=2))
