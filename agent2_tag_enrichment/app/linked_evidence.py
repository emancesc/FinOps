"""
Indice delle evidenze dai file JSON "a corredo" dell'inventario
(extracted/<account>/<regione>/*.json prodotti da scripts/extract_linked_resources.py).

Per ogni risorsa fornisce elementi utili alla proposta di tagging: nome e tag
dell'istanza a cui è collegato un volume o una ENI, descrizione della ENI,
dove è usato un certificato ACM, stack CloudFormation di appartenenza, dati SSM.
"""
from __future__ import annotations

import json
import os
from typing import Optional

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EXTRACTED_ROOT = os.environ.get("EXTRACTED_ROOT", os.path.join(REPO, "extracted"))
REQUIRED_FILES = ("instances_all.json", "volumes_all.json", "eni_attachments.json", "acm_inuseby_full.json")


def _load(path: str):
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []  # file con {"error": ...} = sezione negata/fallita


def _tags(tag_list) -> dict:
    return {t.get("Key"): t.get("Value") for t in tag_list or [] if isinstance(t, dict)}


def linked_status(account_id: str, root: Optional[str] = None) -> dict:
    """Stato dei JSON a corredo per l'account: regioni estratte e completezza."""
    root = root or EXTRACTED_ROOT
    account_dir = os.path.join(root, account_id)
    run = {}
    try:
        with open(os.path.join(account_dir, "_run.json"), encoding="utf-8-sig") as f:
            run = json.load(f)
    except (OSError, ValueError):
        pass
    regions = []
    if os.path.isdir(account_dir):
        for region in sorted(os.listdir(account_dir)):
            region_dir = os.path.join(account_dir, region)
            if os.path.isdir(region_dir) and any(os.path.exists(os.path.join(region_dir, f)) for f in REQUIRED_FILES):
                regions.append(region)
    return {
        "path": account_dir,
        "regions": regions,
        "run_finished": bool(run.get("finished_at")),
        "run_progress_pct": run.get("progress_pct"),
        "ready": bool(regions) and (bool(run.get("finished_at")) or not run),
    }


class EvidenceIndex:
    def __init__(self, account_id: str, root: Optional[str] = None):
        root = root or EXTRACTED_ROOT
        self.instances: dict[str, dict] = {}
        self.volumes: dict[str, dict] = {}
        self.enis: dict[str, dict] = {}
        self.certs: dict[str, dict] = {}
        self.stack_of: dict[str, dict] = {}
        self.ssm: dict[str, dict] = {}
        for region in linked_status(account_id, root)["regions"]:
            d = os.path.join(root, account_id, region)
            for i in _load(os.path.join(d, "instances_all.json")):
                self.instances[i.get("InstanceId")] = {
                    "name": i.get("Name"), "state": i.get("State"), "type": i.get("InstanceType"),
                    "private_ip": i.get("PrivateIpAddress"), "tags": _tags(i.get("Tags")), "region": region}
            for v in _load(os.path.join(d, "volumes_all.json")):
                self.volumes[v.get("VolumeId")] = {
                    "attached_instances": [a.get("InstanceId") for a in v.get("Attachments") or []],
                    "tags": _tags(v.get("Tags")), "size": v.get("Size"), "type": v.get("VolumeType")}
            for e in _load(os.path.join(d, "eni_attachments.json")):
                self.enis[e.get("NetworkInterfaceId")] = {
                    "description": e.get("Description"), "interface_type": e.get("InterfaceType"),
                    "requester_id": e.get("RequesterId"),
                    "instance": (e.get("Attachment") or {}).get("InstanceId"), "tags": _tags(e.get("TagSet"))}
            for c in _load(os.path.join(d, "acm_inuseby_full.json")):
                self.certs[c.get("CertificateArn")] = {
                    "domain": c.get("DomainName"), "in_use_by": c.get("InUseBy") or [], "tags": _tags(c.get("Tags"))}
            for st in _load(os.path.join(d, "cloudformation_stacks.json")):
                info = {"stack": st.get("StackName"), "stack_tags": _tags(st.get("Tags"))}
                for res in st.get("StackResources") or [] if isinstance(st.get("StackResources"), list) else []:
                    if res.get("PhysicalResourceId"):
                        self.stack_of[res["PhysicalResourceId"]] = info
            for s in _load(os.path.join(d, "ssm_managedinstances.json")):
                self.ssm[s.get("InstanceId")] = {"platform": s.get("PlatformName"), "computer_name": s.get("ComputerName")}

    @property
    def size(self) -> int:
        return len(self.instances) + len(self.volumes) + len(self.enis) + len(self.certs)

    def for_resource(self, arn: str, resource_type: str) -> dict:
        """Evidenze rilevanti per la risorsa (vuoto se non ce ne sono)."""
        rid = arn.rsplit("/", 1)[-1]
        ev: dict = {}
        if resource_type == "AWS::EC2::Instance" and rid in self.instances:
            ev["instance"] = self.instances[rid]
            if rid in self.ssm:
                ev["ssm"] = self.ssm[rid]
        elif resource_type == "AWS::EC2::Volume" and rid in self.volumes:
            vol = self.volumes[rid]
            ev["attached_instances"] = [self._instance_brief(i) for i in vol["attached_instances"]]
        elif resource_type == "AWS::EC2::NetworkInterface" and rid in self.enis:
            eni = dict(self.enis[rid])
            if eni.get("instance"):
                eni["instance"] = self._instance_brief(eni["instance"])
            ev["eni"] = eni
        elif resource_type == "AWS::ACM::Certificate" and arn in self.certs:
            ev["certificate"] = self.certs[arn]
        stack = self.stack_of.get(rid) or self.stack_of.get(arn)
        if stack:
            ev["cloudformation"] = stack
        return ev

    def parent_instance(self, arn: str, resource_type: str) -> Optional[str]:
        """Istanza da cui ereditare i tag (volume attaccato, ENI collegata)."""
        rid = arn.rsplit("/", 1)[-1]
        if resource_type == "AWS::EC2::Volume" and rid in self.volumes:
            attached = self.volumes[rid]["attached_instances"]
            return attached[0] if len(attached) == 1 else None
        if resource_type == "AWS::EC2::NetworkInterface" and rid in self.enis:
            return self.enis[rid].get("instance")
        return None

    def _instance_brief(self, instance_id: str) -> dict:
        inst = self.instances.get(instance_id) or {}
        return {"instance_id": instance_id, "name": inst.get("name"), "state": inst.get("state"),
                "cineca_tags": {k: v for k, v in (inst.get("tags") or {}).items() if k.startswith("cineca:")}}
