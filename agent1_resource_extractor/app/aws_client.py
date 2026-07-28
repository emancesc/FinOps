"""
AWS Client: estrae risorse via AWS Config (select_resource_config) con fallback
a describe_* / list_buckets quando Config non è disponibile (es. moto in CI).
AssumeRole opzionale.
"""
from __future__ import annotations
import json
import logging
import os
from typing import Optional

import boto3
from pydantic import BaseModel

logger = logging.getLogger(__name__)

DEFAULT_RESOURCE_TYPES: list[str] = [
    "AWS::EC2::Instance",
    "AWS::EC2::Volume",
    "AWS::EC2::VPC",
    "AWS::EC2::Subnet",
    "AWS::EC2::SecurityGroup",
    "AWS::S3::Bucket",
]

_TAGGING_FILTER: dict[str, str] = {
    "AWS::EC2::Instance":      "ec2:instance",
    "AWS::EC2::Volume":        "ec2:volume",
    "AWS::EC2::VPC":           "ec2:vpc",
    "AWS::EC2::Subnet":        "ec2:subnet",
    "AWS::EC2::SecurityGroup": "ec2:security-group",
    "AWS::S3::Bucket":         "s3",
}


class Relationship(BaseModel):
    type: str
    target_resource_id: str


class NormalizedResource(BaseModel):
    resource_id: str
    account_id: str
    region: str
    resource_type: str
    current_tags: dict[str, str] = {}
    attributes: dict = {}
    relationships: list[Relationship] = []


class AWSClient:
    def __init__(
        self,
        account_id: str,
        region: str,
        assume_role_arn: Optional[str] = None,
    ) -> None:
        self.account_id = account_id
        self.region = region
        self._session = self._create_session(assume_role_arn)

    def _create_session(self, assume_role_arn: Optional[str]) -> boto3.Session:
        if assume_role_arn:
            sts = boto3.client("sts")
            creds = sts.assume_role(
                RoleArn=assume_role_arn,
                RoleSessionName="finops-extractor",
                DurationSeconds=3600,
            )["Credentials"]
            return boto3.Session(
                aws_access_key_id=creds["AccessKeyId"],
                aws_secret_access_key=creds["SecretAccessKey"],
                aws_session_token=creds["SessionToken"],
                region_name=self.region,
            )
        return boto3.Session(region_name=self.region)

    def list_resources(
        self, resource_types: list[str] | None = None
    ) -> list[NormalizedResource]:
        types = resource_types or DEFAULT_RESOURCE_TYPES

        # Augment with live tags from Resource Groups Tagging API
        tags_by_arn = self._get_tags_by_arn(types)

        results: list[NormalizedResource] = []
        for rt in types:
            logger.info("Extracting %s", rt)
            # Try Config first; fall back to describe_* if unavailable
            items = self._config_query(rt)
            if items is None:
                logger.info("  Config non disponibile per %s, uso describe_*", rt)
                resources = self._describe_fallback(rt, tags_by_arn)
            else:
                resources = [
                    r for item in items
                    for r in [self._normalize(item, tags_by_arn)]
                    if r is not None
                ]
            logger.info("  %d risorse trovate per %s", len(resources), rt)
            results.extend(resources)

        return results

    def _config_query(self, resource_type: str) -> list[dict] | None:
        """
        Interroga AWS Config. Ritorna None se Config non è disponibile
        (NotImplementedError da moto, o errore di servizio).
        """
        cfg = self._session.client("config", region_name=self.region)
        expr = (
            "SELECT resourceId, arn, resourceType, accountId, region, configuration, tags "
            f"WHERE resourceType = '{resource_type}'"
        )
        items: list[dict] = []
        kwargs: dict = {"Expression": expr, "Limit": 100}
        try:
            while True:
                resp = cfg.select_resource_config(**kwargs)
                for s in resp.get("Results", []):
                    items.append(json.loads(s))
                next_token = resp.get("NextToken")
                if not next_token:
                    break
                kwargs["NextToken"] = next_token
            return items
        except Exception as exc:
            msg = str(exc)
            if "not been implemented" in msg or "NotImplemented" in msg:
                return None
            logger.warning("Config query fallita per %s: %s", resource_type, exc)
            return None

    def _get_tags_by_arn(self, resource_types: list[str]) -> dict[str, dict[str, str]]:
        tagging = self._session.client("resourcegroupstaggingapi", region_name=self.region)
        filters = list({_TAGGING_FILTER[rt] for rt in resource_types if rt in _TAGGING_FILTER})
        result: dict[str, dict[str, str]] = {}
        try:
            paginator = tagging.get_paginator("get_resources")
            page_kwargs: dict = {}
            if filters:
                page_kwargs["ResourceTypeFilters"] = filters
            for page in paginator.paginate(**page_kwargs):
                for r in page.get("ResourceTagMappingList", []):
                    result[r["ResourceARN"]] = {
                        t["Key"]: t["Value"] for t in r.get("Tags", [])
                    }
        except Exception as exc:
            logger.warning("Tagging API non disponibile: %s", exc)
        return result

    # ------------------------------------------------------------------
    # Fallback: describe_* / list_buckets
    # ------------------------------------------------------------------

    def _describe_fallback(
        self, resource_type: str, tags_by_arn: dict[str, dict[str, str]]
    ) -> list[NormalizedResource]:
        handlers = {
            "AWS::EC2::Instance":      self._describe_instances,
            "AWS::EC2::Volume":        self._describe_volumes,
            "AWS::EC2::VPC":           self._describe_vpcs,
            "AWS::EC2::Subnet":        self._describe_subnets,
            "AWS::EC2::SecurityGroup": self._describe_security_groups,
            "AWS::S3::Bucket":         self._list_s3_buckets,
        }
        handler = handlers.get(resource_type)
        if not handler:
            logger.warning("Nessun fallback per %s", resource_type)
            return []
        return handler(tags_by_arn)

    def _ec2(self):
        return self._session.client("ec2", region_name=self.region)

    def _tags_from_list(self, tag_list: list[dict], arn: str, tags_by_arn: dict) -> dict[str, str]:
        live = tags_by_arn.get(arn, {})
        if live:
            return live
        return {t["Key"]: t["Value"] for t in (tag_list or [])}

    def _describe_instances(self, tags_by_arn: dict) -> list[NormalizedResource]:
        ec2 = self._ec2()
        pag = ec2.get_paginator("describe_instances")
        resources: list[NormalizedResource] = []
        for page in pag.paginate():
            for reservation in page["Reservations"]:
                for inst in reservation["Instances"]:
                    iid = inst["InstanceId"]
                    arn = f"arn:aws:ec2:{self.region}:{self.account_id}:instance/{iid}"
                    tags = self._tags_from_list(inst.get("Tags"), arn, tags_by_arn)
                    rels: list[Relationship] = []
                    if inst.get("VpcId"):
                        rels.append(Relationship(
                            type="CONTAINS",
                            target_resource_id=f"arn:aws:ec2:{self.region}:{self.account_id}:vpc/{inst['VpcId']}",
                        ))
                    if inst.get("SubnetId"):
                        rels.append(Relationship(
                            type="CONTAINS",
                            target_resource_id=f"arn:aws:ec2:{self.region}:{self.account_id}:subnet/{inst['SubnetId']}",
                        ))
                    for sg in inst.get("SecurityGroups", []):
                        rels.append(Relationship(
                            type="SECURED_BY",
                            target_resource_id=f"arn:aws:ec2:{self.region}:{self.account_id}:security-group/{sg['GroupId']}",
                        ))
                    resources.append(NormalizedResource(
                        resource_id=arn,
                        account_id=self.account_id,
                        region=self.region,
                        resource_type="AWS::EC2::Instance",
                        current_tags=tags,
                        attributes={
                            "instance_type": inst.get("InstanceType"),
                            "vpc_id": inst.get("VpcId"),
                            "subnet_id": inst.get("SubnetId"),
                            "state": inst.get("State", {}).get("Name"),
                            "image_id": inst.get("ImageId"),
                            "platform": inst.get("Platform", "linux"),
                        },
                        relationships=rels,
                    ))
        return resources

    def _describe_volumes(self, tags_by_arn: dict) -> list[NormalizedResource]:
        ec2 = self._ec2()
        pag = ec2.get_paginator("describe_volumes")
        resources: list[NormalizedResource] = []
        for page in pag.paginate():
            for vol in page["Volumes"]:
                vid = vol["VolumeId"]
                arn = f"arn:aws:ec2:{self.region}:{self.account_id}:volume/{vid}"
                tags = self._tags_from_list(vol.get("Tags"), arn, tags_by_arn)
                rels: list[Relationship] = []
                for att in vol.get("Attachments", []):
                    if att.get("InstanceId"):
                        rels.append(Relationship(
                            type="ATTACHED_TO",
                            target_resource_id=f"arn:aws:ec2:{self.region}:{self.account_id}:instance/{att['InstanceId']}",
                        ))
                resources.append(NormalizedResource(
                    resource_id=arn,
                    account_id=self.account_id,
                    region=self.region,
                    resource_type="AWS::EC2::Volume",
                    current_tags=tags,
                    attributes={
                        "size_gb": vol.get("Size"),
                        "volume_type": vol.get("VolumeType"),
                        "state": vol.get("State"),
                        "iops": vol.get("Iops"),
                        "encrypted": vol.get("Encrypted"),
                    },
                    relationships=rels,
                ))
        return resources

    def _describe_vpcs(self, tags_by_arn: dict) -> list[NormalizedResource]:
        ec2 = self._ec2()
        pag = ec2.get_paginator("describe_vpcs")
        resources: list[NormalizedResource] = []
        for page in pag.paginate():
            for vpc in page["Vpcs"]:
                vid = vpc["VpcId"]
                arn = f"arn:aws:ec2:{self.region}:{self.account_id}:vpc/{vid}"
                tags = self._tags_from_list(vpc.get("Tags"), arn, tags_by_arn)
                resources.append(NormalizedResource(
                    resource_id=arn,
                    account_id=self.account_id,
                    region=self.region,
                    resource_type="AWS::EC2::VPC",
                    current_tags=tags,
                    attributes={
                        "cidr_block": vpc.get("CidrBlock"),
                        "state": vpc.get("State"),
                        "is_default": vpc.get("IsDefault"),
                    },
                    relationships=[],
                ))
        return resources

    def _describe_subnets(self, tags_by_arn: dict) -> list[NormalizedResource]:
        ec2 = self._ec2()
        pag = ec2.get_paginator("describe_subnets")
        resources: list[NormalizedResource] = []
        for page in pag.paginate():
            for sn in page["Subnets"]:
                sid = sn["SubnetId"]
                arn = f"arn:aws:ec2:{self.region}:{self.account_id}:subnet/{sid}"
                tags = self._tags_from_list(sn.get("Tags"), arn, tags_by_arn)
                rels: list[Relationship] = []
                if sn.get("VpcId"):
                    rels.append(Relationship(
                        type="CONTAINS",
                        target_resource_id=f"arn:aws:ec2:{self.region}:{self.account_id}:vpc/{sn['VpcId']}",
                    ))
                resources.append(NormalizedResource(
                    resource_id=arn,
                    account_id=self.account_id,
                    region=self.region,
                    resource_type="AWS::EC2::Subnet",
                    current_tags=tags,
                    attributes={
                        "cidr_block": sn.get("CidrBlock"),
                        "vpc_id": sn.get("VpcId"),
                        "availability_zone": sn.get("AvailabilityZone"),
                    },
                    relationships=rels,
                ))
        return resources

    def _describe_security_groups(self, tags_by_arn: dict) -> list[NormalizedResource]:
        ec2 = self._ec2()
        pag = ec2.get_paginator("describe_security_groups")
        resources: list[NormalizedResource] = []
        for page in pag.paginate():
            for sg in page["SecurityGroups"]:
                gid = sg["GroupId"]
                arn = f"arn:aws:ec2:{self.region}:{self.account_id}:security-group/{gid}"
                tags = self._tags_from_list(sg.get("Tags"), arn, tags_by_arn)
                rels: list[Relationship] = []
                if sg.get("VpcId"):
                    rels.append(Relationship(
                        type="CONTAINS",
                        target_resource_id=f"arn:aws:ec2:{self.region}:{self.account_id}:vpc/{sg['VpcId']}",
                    ))
                resources.append(NormalizedResource(
                    resource_id=arn,
                    account_id=self.account_id,
                    region=self.region,
                    resource_type="AWS::EC2::SecurityGroup",
                    current_tags=tags,
                    attributes={
                        "description": sg.get("Description"),
                        "vpc_id": sg.get("VpcId"),
                        "group_name": sg.get("GroupName"),
                    },
                    relationships=rels,
                ))
        return resources

    def _list_s3_buckets(self, tags_by_arn: dict) -> list[NormalizedResource]:
        s3 = self._session.client("s3", region_name=self.region)
        resp = s3.list_buckets()
        resources: list[NormalizedResource] = []
        for bucket in resp.get("Buckets", []):
            name = bucket["Name"]
            arn = f"arn:aws:s3:::{name}"
            tags = tags_by_arn.get(arn, {})
            if not tags:
                try:
                    tag_resp = s3.get_bucket_tagging(Bucket=name)
                    tags = {t["Key"]: t["Value"] for t in tag_resp.get("TagSet", [])}
                except Exception:
                    pass
            resources.append(NormalizedResource(
                resource_id=arn,
                account_id=self.account_id,
                region=self.region,
                resource_type="AWS::S3::Bucket",
                current_tags=tags,
                attributes={"creation_date": str(bucket.get("CreationDate", ""))},
                relationships=[],
            ))
        return resources

    # ------------------------------------------------------------------
    # Normalize path (from Config items)
    # ------------------------------------------------------------------

    def _normalize(
        self, item: dict, tags_by_arn: dict[str, dict[str, str]]
    ) -> NormalizedResource | None:
        arn = item.get("arn") or _build_arn(
            item.get("resourceType", ""),
            item.get("resourceId", ""),
            item.get("region", self.region),
            item.get("accountId", self.account_id),
        )
        if not arn:
            return None

        config_raw = item.get("configuration") or "{}"
        config_data: dict = (
            json.loads(config_raw) if isinstance(config_raw, str) else (config_raw or {})
        )

        live_tags = tags_by_arn.get(arn, {})
        config_tags_raw = item.get("tags") or []
        config_tags: dict[str, str] = (
            {t["key"]: t["value"] for t in config_tags_raw}
            if isinstance(config_tags_raw, list)
            else config_tags_raw
        )
        tags = live_tags if live_tags else config_tags

        resource_type = item.get("resourceType", "")
        region = item.get("region", self.region)
        account_id = item.get("accountId", self.account_id)

        return NormalizedResource(
            resource_id=arn,
            account_id=account_id,
            region=region,
            resource_type=resource_type,
            current_tags=tags,
            attributes=_extract_attributes(resource_type, config_data),
            relationships=[
                Relationship(**r)
                for r in _extract_relationships(resource_type, config_data, region, account_id)
            ],
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_arn(resource_type: str, resource_id: str, region: str, account_id: str) -> str:
    patterns: dict[str, str] = {
        "AWS::EC2::Instance":      f"arn:aws:ec2:{region}:{account_id}:instance/{resource_id}",
        "AWS::EC2::Volume":        f"arn:aws:ec2:{region}:{account_id}:volume/{resource_id}",
        "AWS::EC2::VPC":           f"arn:aws:ec2:{region}:{account_id}:vpc/{resource_id}",
        "AWS::EC2::Subnet":        f"arn:aws:ec2:{region}:{account_id}:subnet/{resource_id}",
        "AWS::EC2::SecurityGroup": f"arn:aws:ec2:{region}:{account_id}:security-group/{resource_id}",
        "AWS::S3::Bucket":         f"arn:aws:s3:::{resource_id}",
    }
    return patterns.get(resource_type, "")


def _extract_attributes(resource_type: str, config: dict) -> dict:
    if resource_type == "AWS::EC2::Instance":
        state = config.get("state") or {}
        return {
            "instance_type": config.get("instanceType"),
            "vpc_id": config.get("vpcId"),
            "subnet_id": config.get("subnetId"),
            "state": state.get("name") if isinstance(state, dict) else state,
            "image_id": config.get("imageId"),
            "platform": config.get("platform", "linux"),
        }
    if resource_type == "AWS::EC2::Volume":
        return {
            "size_gb": config.get("size"),
            "volume_type": config.get("volumeType"),
            "state": config.get("state"),
            "iops": config.get("iops"),
            "encrypted": config.get("encrypted"),
        }
    if resource_type == "AWS::EC2::VPC":
        return {
            "cidr_block": config.get("cidrBlock"),
            "state": config.get("state"),
            "is_default": config.get("isDefault"),
        }
    if resource_type == "AWS::EC2::Subnet":
        return {
            "cidr_block": config.get("cidrBlock"),
            "vpc_id": config.get("vpcId"),
            "availability_zone": config.get("availabilityZone"),
        }
    if resource_type == "AWS::EC2::SecurityGroup":
        return {
            "description": config.get("description"),
            "vpc_id": config.get("vpcId"),
            "group_name": config.get("groupName"),
        }
    if resource_type == "AWS::S3::Bucket":
        return {"creation_date": str(config.get("creationDate", ""))}
    return {}


def _extract_relationships(
    resource_type: str, config: dict, region: str, account_id: str
) -> list[dict]:
    rels: list[dict] = []
    if resource_type == "AWS::EC2::Instance":
        if config.get("vpcId"):
            rels.append({"type": "CONTAINS", "target_resource_id": f"arn:aws:ec2:{region}:{account_id}:vpc/{config['vpcId']}"})
        if config.get("subnetId"):
            rels.append({"type": "CONTAINS", "target_resource_id": f"arn:aws:ec2:{region}:{account_id}:subnet/{config['subnetId']}"})
        for sg in config.get("securityGroups", []):
            gid = sg.get("groupId") if isinstance(sg, dict) else sg
            if gid:
                rels.append({"type": "SECURED_BY", "target_resource_id": f"arn:aws:ec2:{region}:{account_id}:security-group/{gid}"})
    elif resource_type in ("AWS::EC2::Subnet", "AWS::EC2::SecurityGroup"):
        if config.get("vpcId"):
            rels.append({"type": "CONTAINS", "target_resource_id": f"arn:aws:ec2:{region}:{account_id}:vpc/{config['vpcId']}"})
    elif resource_type == "AWS::EC2::Volume":
        for att in config.get("attachments", []):
            inst_id = att.get("instanceId") if isinstance(att, dict) else None
            if inst_id:
                rels.append({"type": "ATTACHED_TO", "target_resource_id": f"arn:aws:ec2:{region}:{account_id}:instance/{inst_id}"})
    return rels
