from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import boto3


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

VOLUME_IDS = [
    "vol-0c921d8bba7f54a07",
    "vol-00a2e175ad57436fe",
    "vol-053366b87797732d0",
    "vol-0da063cc42381b720",
    "vol-0ce3a5e4a7ca8a0ec",
    "vol-0b43f6c8adc8363e6",
    "vol-05d1db6082900832b",
    "vol-012064e1e09bf74b6",
    "vol-098a3ea7765e0e943",
    "vol-0bfca3cd9893aa7d9",
    "vol-0eab9a2e87ea2dbea",
    "vol-03dac0ae52a02acbd",
    "vol-07ccbf414b37fbf83",
    "vol-055fbe9e3a7287f11",
    "vol-057a1cd228f6c8dd6",
    "vol-07922fd3a632b7a17",
    "vol-00801635064f2b9a2",
    "vol-0debdff7f3d854a8a",
    "vol-0ad0dae0f8fb3d649",
    "vol-0f7e57343aaae052a",
    "vol-0c17b6e995ca6dcd9",
]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def export_idm_prod_volume_describe(
    profile_name: str = "idm-prod",
    region: str = "eu-south-1",
    output_dir: str = "exports",
    filename: str = "idm_prod_volume_describe.json",
) -> dict[str, Any]:
    session = boto3.Session(profile_name=profile_name, region_name=region)
    sts = session.client("sts", region_name=region)
    identity = sts.get_caller_identity()

    ec2 = session.client("ec2", region_name=region)
    response = ec2.describe_volumes(VolumeIds=VOLUME_IDS)
    volumes = response.get("Volumes", [])

    payload = {
        "account_id": identity.get("Account"),
        "account_arn": identity.get("Arn"),
        "profile": profile_name,
        "region": region,
        "generated_at": _utc_now(),
        "volume_count": len(volumes),
        "volume_ids_requested": VOLUME_IDS,
        "volumes": volumes,
    }

    target_dir = Path(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    file_path = target_dir / filename
    file_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=_json_default),
        encoding="utf-8",
    )

    payload["file_path"] = str(file_path)
    return payload


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Export fixed EC2 volume describe from IDM-PROD")
    parser.add_argument("--profile", default="idm-prod")
    parser.add_argument("--region", default="eu-south-1")
    parser.add_argument("--output-dir", default="exports")
    parser.add_argument("--filename", default="idm_prod_volume_describe.json")
    args = parser.parse_args()

    result = export_idm_prod_volume_describe(
        profile_name=args.profile,
        region=args.region,
        output_dir=args.output_dir,
        filename=args.filename,
    )
    print(json.dumps({"file_path": result["file_path"], "count": result["volume_count"]}, indent=2))
