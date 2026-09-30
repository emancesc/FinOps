"""Build volumes_report.json (volume -> instance / snapshot) from an extraction folder.

Usage: python build_volumes_report.py <extracted/<account>/<region>>
Input files (from extract_linked_resources.py): volumes_all.json,
instances_for_volumes.json, snapshots_volumeid.json.
"""
import json
import os
import sys

BASE = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()


def load(name):
    # utf-8-sig: accetta anche i file con BOM scritti da Windows PowerShell 5.1
    with open(os.path.join(BASE, name), encoding="utf-8-sig") as f:
        return json.load(f)


volumes = load("volumes_all.json")
instances = load("instances_for_volumes.json")
snapshots = load("snapshots_volumeid.json")

instances_by_id = {i["InstanceId"]: i for i in instances}

snaps_by_vol = {}
for s in snapshots:
    snaps_by_vol.setdefault(s["VolumeId"], []).append(s)

report = []
for v in volumes:
    tags = {t["Key"]: t["Value"] for t in (v.get("Tags") or [])}
    attachments = []
    for a in v.get("Attachments") or []:
        inst = instances_by_id.get(a["InstanceId"])
        attachments.append(
            {
                "InstanceId": a["InstanceId"],
                "InstanceName": (inst or {}).get("Name"),
                "InstanceState": (inst or {}).get("State"),
                "Device": a.get("Device"),
                "AttachState": a.get("State"),
                "AttachTime": a.get("AttachTime"),
                "DeleteOnTermination": a.get("DeleteOnTermination"),
            }
        )
    vol_snaps = [
        {
            "SnapshotId": s["SnapshotId"],
            "State": s.get("State"),
            "StartTime": s.get("StartTime"),
            "Size": s.get("Size"),
            "Description": s.get("Description"),
        }
        for s in snaps_by_vol.get(v["VolumeId"], [])
    ]
    report.append(
        {
            "VolumeId": v["VolumeId"],
            "State": v["State"],
            "VolumeType": v["VolumeType"],
            "Size": v["Size"],
            "AvailabilityZone": v["AvailabilityZone"],
            "Encrypted": v.get("Encrypted"),
            "KmsKeyId": v.get("KmsKeyId"),
            "Iops": v.get("Iops"),
            "Throughput": v.get("Throughput"),
            "CreateTime": v.get("CreateTime"),
            "Name": tags.get("Name"),
            "Environment": tags.get("Environment"),
            "Customer": tags.get("Customer"),
            "Provider": tags.get("Provider"),
            "MapMigrated": tags.get("map-migrated"),
            "AllTags": tags,
            "Attachments": attachments,
            "Snapshots": vol_snaps,
        }
    )

out_path = os.path.join(BASE, "volumes_report.json")
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(report, f, ensure_ascii=False, indent=2)

print(f"Volumes: {len(report)}")
attached = [r for r in report if r["Attachments"]]
unattached = [r for r in report if not r["Attachments"]]
print(f"Attached: {len(attached)}  Unattached: {len(unattached)}")
total_size = sum(r["Size"] for r in report)
print(f"Total size (GiB): {total_size}")
with_snaps = [r for r in report if r["Snapshots"]]
print(f"Volumes with snapshots: {len(with_snaps)}")
print(f"Report written to: {out_path}")
