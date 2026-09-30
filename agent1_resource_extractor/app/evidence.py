"""
Raccolta evidenze AWS per account.

``region`` accetta una regione, una lista "a,b" oppure "all" (tutte le regioni
abilitate). Le sorgenti regionali (aws-config, ec2-eni, elb, cloudtrail,
ssm-inventory, sqs) vengono eseguite per ogni regione, in parallelo; quelle
globali (cloudfront, route53, oc-routes, terraform-state) una sola volta.
"""
from __future__ import annotations

import json
import logging
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

import boto3

from .aws_client import (
    _CONFIG_SELECT_FIELDS,
    _MAX_REGION_WORKERS,
    _default_home_region,
    parse_region_spec,
)

logger = logging.getLogger(__name__)

REGIONAL_SOURCE_NAMES = ["aws-config", "ec2-eni", "elb", "cloudtrail", "ssm-inventory", "sqs"]
GLOBAL_SOURCE_NAMES = ["cloudfront", "route53", "oc-routes", "terraform-state"]
GLOBAL_REGION_LABEL = "global"

# La creazione di client dalla sessione di default di boto3 non e' thread-safe
# (i client creati invece lo sono): la serializziamo.
_CLIENT_LOCK = threading.Lock()


def _client(service_name: str, region_name: str | None = None):
    with _CLIENT_LOCK:
        if region_name is None:
            return boto3.client(service_name)
        return boto3.client(service_name, region_name=region_name)


def resolve_regions(region: str | None) -> list[str]:
    """Espande "all" nelle regioni abilitate (describe_regions)."""
    requested = parse_region_spec(region)
    if requested:
        return requested
    home = _default_home_region()
    try:
        resp = _client("ec2", home).describe_regions()
        regions = sorted(r["RegionName"] for r in resp.get("Regions", []))
    except Exception as exc:  # noqa: BLE001
        logger.warning("describe_regions fallita (%s): uso solo %s", exc, home)
        return [home]
    return regions or [home]


DEFAULT_SOURCE_NAMES = [
    "aws-config",
    "ec2-eni",
    "elb",
    "cloudfront",
    "route53",
    "oc-routes",
    "cloudtrail",
    "terraform-state",
    "ssm-inventory",
    "sqs",
]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def _make_record(
    account_id: str,
    region: str,
    resource_type: str,
    source: str,
    evidence_type: str,
    value: Any,
    confidence: float,
    reason: str,
) -> dict[str, Any]:
    return {
        "account_id": account_id,
        "region": region,
        "resource_type": resource_type,
        "source": source,
        "evidence_type": evidence_type,
        "value": value,
        "confidence": confidence,
        "observed_at": _utc_now(),
        "reason": reason,
    }


def _parse_json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _iter_resource_types(resource_types: Iterable[str] | None) -> list[str]:
    if resource_types:
        return list(resource_types)
    return [
        "AWS::EC2::Volume",
        "AWS::EC2::NetworkInterface",
        "AWS::EC2::Instance",
        "AWS::ELBv2::LoadBalancer",
    ]


def _collect_aws_config_evidence(
    account_id: str,
    region: str,
    resource_types: Iterable[str],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        client = _client("config", region)
    except Exception as exc:  # noqa: BLE001
        return [_config_error(account_id, region, "AWS::Config::ResourceCompliance", exc)]
    for resource_type in resource_types:
        expr = (
            f"SELECT {_CONFIG_SELECT_FIELDS}, relationships, configurationItemCaptureTime, "
            f"configurationItemStatus WHERE resourceType = '{resource_type}'"
        )
        kwargs: dict[str, Any] = {"Expression": expr, "Limit": 100}
        try:
            while True:
                resp = client.select_resource_config(**kwargs)
                for item in resp.get("Results", []):
                    payload = json.loads(item)
                    config = payload.get("configuration")
                    if isinstance(config, str):
                        try:
                            payload["configuration"] = json.loads(config)
                        except ValueError:
                            pass
                    records.append(
                        _make_record(
                            account_id=account_id,
                            region=payload.get("awsRegion") or region,
                            resource_type=resource_type,
                            source="aws-config",
                            evidence_type="resource_config",
                            value=payload,
                            confidence=0.85,
                            reason="Live AWS Config snapshot for resource history and attachment context",
                        )
                    )
                next_token = resp.get("NextToken")
                if not next_token:
                    break
                kwargs["NextToken"] = next_token
        except Exception as exc:  # noqa: BLE001
            records.append(_config_error(account_id, region, resource_type, exc))
    return records


def _config_error(account_id: str, region: str, resource_type: str, exc: Exception) -> dict[str, Any]:
    return _make_record(
        account_id=account_id,
        region=region,
        resource_type=resource_type,
        source="aws-config",
        evidence_type="service_error",
        value={"error": str(exc)},
        confidence=0.0,
        reason="AWS Config not available or permissions missing; collection failed gracefully",
    )


def _collect_ec2_eni_evidence(
    account_id: str,
    region: str,
    resource_types: Iterable[str],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        ec2 = _client("ec2", region)
        if "AWS::EC2::NetworkInterface" not in resource_types:
            return records

        for page in ec2.get_paginator("describe_network_interfaces").paginate():
            for eni in page.get("NetworkInterfaces", []):
                attachment = eni.get("Attachment") or {}
                records.append(
                    _make_record(
                        account_id=account_id,
                        region=region,
                        resource_type="AWS::EC2::NetworkInterface",
                        source="ec2-eni",
                        evidence_type="network_interface",
                        value={
                            "network_interface_id": eni.get("NetworkInterfaceId"),
                            "interface_type": eni.get("InterfaceType"),
                            "description": eni.get("Description"),
                            "requester_id": eni.get("RequesterId"),
                            "vpc_id": eni.get("VpcId"),
                            "subnet_id": eni.get("SubnetId"),
                            "attachment": {
                                "instance_id": attachment.get("InstanceId"),
                                "attachment_id": attachment.get("AttachmentId"),
                                "status": attachment.get("Status"),
                            },
                            "tags": eni.get("TagSet", []),
                            "raw": eni,
                        },
                        confidence=0.9,
                        reason="EC2 ENI attributes showing service owner and last attachment state",
                    )
                )
    except Exception as exc:  # noqa: BLE001
        records.append(
            _make_record(
                account_id=account_id,
                region=region,
                resource_type="AWS::EC2::NetworkInterface",
                source="ec2-eni",
                evidence_type="service_error",
                value={"error": str(exc)},
                confidence=0.0,
                reason="EC2 Network Interfaces collection failed gracefully",
            )
        )
    return records


def _collect_elb_evidence(
    account_id: str,
    region: str,
    resource_types: Iterable[str],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        elb = _client("elbv2", region)
        if "AWS::ELBv2::LoadBalancer" not in resource_types:
            return records

        paginator = elb.get_paginator("describe_load_balancers")
        for page in paginator.paginate():
            for lb in page.get("LoadBalancers", []):
                lb_arn = lb.get("LoadBalancerArn")
                listeners = elb.describe_listeners(LoadBalancerArn=lb_arn).get("Listeners", [])
                rules_by_listener: list[dict[str, Any]] = []
                for listener in listeners:
                    rules = elb.describe_rules(ListenerArn=listener["ListenerArn"]).get("Rules", [])
                    rules_by_listener.append(
                        {
                            "listener_arn": listener.get("ListenerArn"),
                            "port": listener.get("Port"),
                            "protocol": listener.get("Protocol"),
                            "rules": rules,
                        }
                    )

                records.append(
                    _make_record(
                        account_id=account_id,
                        region=region,
                        resource_type="AWS::ELBv2::LoadBalancer",
                        source="elb",
                        evidence_type="load_balancer",
                        value={
                            "load_balancer_arn": lb_arn,
                            "load_balancer_name": lb.get("LoadBalancerName"),
                            "scheme": lb.get("Scheme"),
                            "state": lb.get("State", {}).get("Code"),
                            "vpc_id": lb.get("VpcId"),
                            "availability_zones": lb.get("AvailabilityZones", []),
                            "listeners": rules_by_listener,
                            "raw": lb,
                        },
                        confidence=0.95,
                        reason="Application/Network Load Balancer metadata confirming ownership and listener configuration",
                    )
                )
    except Exception as exc:  # noqa: BLE001
        records.append(
            _make_record(
                account_id=account_id,
                region=region,
                resource_type="AWS::ELBv2::LoadBalancer",
                source="elb",
                evidence_type="service_error",
                value={"error": str(exc)},
                confidence=0.0,
                reason="ELB collection failed gracefully",
            )
        )
    return records


def _collect_cloudtrail_evidence(account_id: str, region: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        client = _client("cloudtrail", region)
        event_names = ["CreateVolume", "CreateNetworkInterface", "CreateQueue"]
        for event_name in event_names:
            kwargs: dict[str, Any] = {
                "LookupAttributes": [{"AttributeKey": "EventName", "AttributeValue": event_name}],
                "MaxResults": 50,
            }
            while True:
                response = client.lookup_events(**kwargs)
                for event in response.get("Events", []):
                    identity = event.get("UserIdentity") or {}
                    records.append(
                        _make_record(
                            account_id=account_id,
                            region=region,
                            resource_type="AWS::CloudTrail::Event",
                            source="cloudtrail",
                            evidence_type="creator_identity",
                            value={
                                "event_name": event_name,
                                "event_id": event.get("EventId"),
                                "event_time": event.get("EventTime"),
                                "user_identity": identity,
                                "resources": event.get("Resources", []),
                                "event_source": event.get("EventSource"),
                                "cloudtrail_event": _parse_json(event.get("CloudTrailEvent")),
                            },
                            confidence=0.8,
                            reason="CloudTrail creator/userIdentity for creation events relevant to ownership attribution",
                        )
                    )
                next_token = response.get("NextToken")
                if not next_token:
                    break
                kwargs["NextToken"] = next_token
    except Exception as exc:  # noqa: BLE001
        records.append(
            _make_record(
                account_id=account_id,
                region=region,
                resource_type="AWS::CloudTrail::Event",
                source="cloudtrail",
                evidence_type="service_error",
                value={"error": str(exc)},
                confidence=0.0,
                reason="CloudTrail lookup failed gracefully",
            )
        )
    return records


def _collect_terraform_state_evidence(account_id: str, region: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        # Sorgente globale: in multi-regione region == "global", non valida per il client.
        s3 = _client("s3", _default_home_region() if region == GLOBAL_REGION_LABEL else region)
        buckets = s3.list_buckets().get("Buckets", [])
        matches = [b["Name"] for b in buckets if "cineca-tf-state-" in b["Name"]]
        if not matches:
            records.append(
                _make_record(
                    account_id=account_id,
                    region=region,
                    resource_type="AWS::S3::Bucket",
                    source="terraform-state",
                    evidence_type="no_match",
                    value={"status": "no_matching_tf_state_buckets"},
                    confidence=0.0,
                    reason="No Terraform state buckets matched the configured naming pattern",
                )
            )
            return records

        for bucket_name in matches:
            kwargs: dict[str, Any] = {"Bucket": bucket_name}
            while True:
                response = s3.list_objects_v2(**kwargs)
                objects = response.get("Contents", [])
                for obj in objects:
                    key = obj.get("Key", "")
                    if not key.endswith((".tfstate", ".tfstate.backup")):
                        continue
                    body = s3.get_object(Bucket=bucket_name, Key=key).get("Body")
                    payload = json.loads(body.read().decode("utf-8"))
                    resources = payload.get("resources", [])
                    records.append(
                        _make_record(
                            account_id=account_id,
                            region=region,
                            resource_type="AWS::S3::Bucket",
                            source="terraform-state",
                            evidence_type="state_file",
                            value={
                                "bucket_name": bucket_name,
                                "key": key,
                                "module": payload.get("module"),
                                "resource_count": len(resources),
                                "resources": resources,
                            },
                            confidence=0.85,
                            reason="Terraform state object with module/repository ownership metadata",
                        )
                    )
                if not response.get("IsTruncated"):
                    break
                kwargs["ContinuationToken"] = response.get("NextContinuationToken")
    except Exception as exc:  # noqa: BLE001
        records.append(
            _make_record(
                account_id=account_id,
                region=region,
                resource_type="AWS::S3::Bucket",
                source="terraform-state",
                evidence_type="service_error",
                value={"error": str(exc)},
                confidence=0.0,
                reason="Terraform state inspection failed gracefully",
            )
        )
    return records


def _collect_ssm_inventory_evidence(account_id: str, region: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        ssm = _client("ssm", region)
        ec2 = _client("ec2", region)
        instances = []
        for page in ec2.get_paginator("describe_instances").paginate():
            for reservation in page.get("Reservations", []):
                for inst in reservation.get("Instances", []):
                    instances.append(inst)

        if not instances:
            records.append(
                _make_record(
                    account_id=account_id,
                    region=region,
                    resource_type="AWS::EC2::Instance",
                    source="ssm-inventory",
                    evidence_type="no_instances",
                    value={"status": "no_instances"},
                    confidence=0.0,
                    reason="No EC2 instances found in the target region for SSM inventory collection",
                )
            )
            return records

        for inst in instances:
            instance_id = inst.get("InstanceId")
            try:
                kwargs: dict[str, Any] = {
                    "InstanceId": instance_id,
                    "TypeName": "AWS:Application",
                    "MaxResults": 50,
                }
                while True:
                    response = ssm.list_inventory_entries(**kwargs)
                    entries = response.get("Entries", [])
                    if entries:
                        records.append(
                            _make_record(
                                account_id=account_id,
                                region=region,
                                resource_type="AWS::EC2::Instance",
                                source="ssm-inventory",
                                evidence_type="application_inventory",
                                value={
                                    "instance_id": instance_id,
                                    "entries": entries,
                                },
                                confidence=0.8,
                                reason="SSM inventory entries for application ownership/role attribution",
                            )
                        )
                    next_token = response.get("NextToken")
                    if not next_token:
                        break
                    kwargs["NextToken"] = next_token
            except Exception:
                continue
    except Exception as exc:  # noqa: BLE001
        records.append(
            _make_record(
                account_id=account_id,
                region=region,
                resource_type="AWS::EC2::Instance",
                source="ssm-inventory",
                evidence_type="service_error",
                value={"error": str(exc)},
                confidence=0.0,
                reason="SSM inventory collection failed gracefully",
            )
        )
    return records


def _collect_sqs_evidence(account_id: str, region: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        sqs = _client("sqs", region)
        kwargs: dict[str, Any] = {}
        while True:
            response = sqs.list_queues(**kwargs)
            queue_urls = response.get("QueueUrls", [])
            if not queue_urls:
                if not kwargs:
                    records.append(
                        _make_record(
                            account_id=account_id,
                            region=region,
                            resource_type="AWS::SQS::Queue",
                            source="sqs",
                            evidence_type="no_queues",
                            value={"status": "no_queues"},
                            confidence=0.0,
                            reason="No SQS queues found in the target region",
                        )
                    )
                break

            for queue_url in queue_urls:
                attrs = sqs.get_queue_attributes(QueueUrl=queue_url, AttributeNames=["All"])
                attributes = attrs.get("Attributes", {})
                records.append(
                    _make_record(
                        account_id=account_id,
                        region=region,
                        resource_type="AWS::SQS::Queue",
                        source="sqs",
                        evidence_type="queue_attributes",
                        value={
                            "queue_url": queue_url,
                            "queue_arn": attributes.get("QueueArn"),
                            "redrive_policy": attributes.get("RedrivePolicy"),
                            "policy": attributes.get("Policy"),
                            "owner_account_id": attributes.get("OwnerAccountId"),
                            "attributes": attributes,
                        },
                        confidence=0.8,
                        reason="SQS queue attributes and policy to identify producer/consumer relationships and DLQ usage",
                    )
                )

            next_token = response.get("NextToken")
            if not next_token:
                break
            kwargs["NextToken"] = next_token
    except Exception as exc:  # noqa: BLE001
        records.append(
            _make_record(
                account_id=account_id,
                region=region,
                resource_type="AWS::SQS::Queue",
                source="sqs",
                evidence_type="service_error",
                value={"error": str(exc)},
                confidence=0.0,
                reason="SQS collection failed gracefully",
            )
        )
    return records


def _collect_cloudfront_evidence(account_id: str, region: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        client = _client("cloudfront")
        kwargs: dict[str, Any] = {}
        while True:
            response = client.list_distributions(**kwargs)
            dist_list = response.get("DistributionList", {})
            for dist in dist_list.get("Items", []):
                dist_id = dist.get("Id")
                dist_arn = dist.get("ARN")
                detail = client.get_distribution(Id=dist_id).get("Distribution", {})
                cfg = detail.get("DistributionConfig", {})
                tags = client.list_tags_for_resource(Resource=dist_arn or f"arn:aws:cloudfront::{account_id}:distribution/{dist_id}").get("Tags", {}).get("Items", [])
                records.append(
                    _make_record(
                        account_id=account_id,
                        region=region,
                        resource_type="AWS::CloudFront::Distribution",
                        source="cloudfront",
                        evidence_type="distribution",
                        value={
                            "distribution_id": dist_id,
                            "arn": dist_arn,
                            "domain_name": detail.get("DomainName"),
                            "status": detail.get("Status"),
                            "aliases": cfg.get("Aliases", {}).get("Items", []),
                            "origins": cfg.get("Origins", {}).get("Items", []),
                            "viewer_certificate": cfg.get("ViewerCertificate"),
                            "tags": tags,
                            "raw": detail,
                        },
                        confidence=0.9,
                        reason="CloudFront distribution metadata confirming public endpoint exposure and origin configuration",
                    )
                )
            if not dist_list.get("IsTruncated"):
                break
            kwargs["Marker"] = dist_list.get("NextMarker")
    except Exception as exc:  # noqa: BLE001
        records.append(
            _make_record(
                account_id=account_id,
                region=region,
                resource_type="AWS::CloudFront::Distribution",
                source="cloudfront",
                evidence_type="service_error",
                value={"error": str(exc)},
                confidence=0.0,
                reason="CloudFront collection failed gracefully",
            )
        )
    return records


def _collect_route53_evidence(account_id: str, region: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        client = _client("route53")
        zone_kwargs: dict[str, Any] = {}
        while True:
            zones_response = client.list_hosted_zones(**zone_kwargs)
            for zone in zones_response.get("HostedZones", []):
                zone_id = zone.get("Id", "").replace("/hostedzone/", "")
                rr_kwargs: dict[str, Any] = {"HostedZoneId": zone_id}
                while True:
                    rr_response = client.list_resource_record_sets(**rr_kwargs)
                    for record in rr_response.get("ResourceRecordSets", []):
                        records.append(
                            _make_record(
                                account_id=account_id,
                                region=region,
                                resource_type="AWS::Route53::HostedZone",
                                source="route53",
                                evidence_type="record_set",
                                value={
                                    "hosted_zone_id": zone_id,
                                    "zone_name": zone.get("Name"),
                                    "record_name": record.get("Name"),
                                    "record_type": record.get("Type"),
                                    "ttl": record.get("TTL"),
                                    "resource_records": record.get("ResourceRecords", []),
                                    "alias_target": record.get("AliasTarget"),
                                    "raw": record,
                                },
                                confidence=0.85,
                                reason="Route53 hosted zone records showing public DNS routing and name ownership",
                            )
                        )
                    if not rr_response.get("IsTruncated"):
                        break
                    rr_kwargs["StartRecordName"] = rr_response.get("NextRecordName")
                    rr_kwargs["StartRecordType"] = rr_response.get("NextRecordType")
                    rr_kwargs["StartRecordIdentifier"] = rr_response.get("NextRecordIdentifier")
            if not zones_response.get("IsTruncated"):
                break
            zone_kwargs["Marker"] = zones_response.get("NextMarker")
    except Exception as exc:  # noqa: BLE001
        records.append(
            _make_record(
                account_id=account_id,
                region=region,
                resource_type="AWS::Route53::HostedZone",
                source="route53",
                evidence_type="service_error",
                value={"error": str(exc)},
                confidence=0.0,
                reason="Route53 collection failed gracefully",
            )
        )
    return records


def _collect_oc_routes_evidence(account_id: str, region: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        result = subprocess.run(
            ["oc", "get", "routes", "-A", "-o", "json"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr or result.stdout or "oc get routes failed")
        payload = json.loads(result.stdout or "{}")
        items = payload.get("items", [])
        if not items:
            records.append(
                _make_record(
                    account_id=account_id,
                    region=region,
                    resource_type="OpenShift::Route",
                    source="oc-routes",
                    evidence_type="no_routes",
                    value={"status": "no_routes"},
                    confidence=0.0,
                    reason="OpenShift cluster returned no routes for the current context",
                )
            )
            return records

        for route in items:
            metadata = route.get("metadata", {})
            spec = route.get("spec", {})
            status = route.get("status", {})
            records.append(
                _make_record(
                    account_id=account_id,
                    region=region,
                    resource_type="OpenShift::Route",
                    source="oc-routes",
                    evidence_type="route",
                    value={
                        "name": metadata.get("name"),
                        "namespace": metadata.get("namespace"),
                        "host": spec.get("host"),
                        "to": spec.get("to"),
                        "wildcard_policy": spec.get("wildcardPolicy"),
                        "ingress": status.get("ingress", []),
                        "raw": route,
                    },
                    confidence=0.88,
                    reason="OpenShift route metadata showing external DNS exposure and service target mapping",
                )
            )
    except Exception as exc:  # noqa: BLE001
        records.append(
            _make_record(
                account_id=account_id,
                region=region,
                resource_type="OpenShift::Route",
                source="oc-routes",
                evidence_type="service_error",
                value={"error": str(exc)},
                confidence=0.0,
                reason="OpenShift route inspection failed gracefully",
            )
        )
    return records


def _regional_collectors(resource_type_list: list[str]) -> dict[str, Callable[[str, str], list[dict[str, Any]]]]:
    return {
        "aws-config": lambda acc, reg: _collect_aws_config_evidence(acc, reg, resource_type_list),
        "ec2-eni": lambda acc, reg: _collect_ec2_eni_evidence(acc, reg, resource_type_list),
        "elb": lambda acc, reg: _collect_elb_evidence(acc, reg, resource_type_list),
        "cloudtrail": _collect_cloudtrail_evidence,
        "ssm-inventory": _collect_ssm_inventory_evidence,
        "sqs": _collect_sqs_evidence,
    }


_GLOBAL_COLLECTORS: dict[str, Callable[[str, str], list[dict[str, Any]]]] = {
    "cloudfront": _collect_cloudfront_evidence,
    "route53": _collect_route53_evidence,
    "oc-routes": _collect_oc_routes_evidence,
    "terraform-state": _collect_terraform_state_evidence,
}


def collect_aws_evidence(
    account_id: str,
    region: str = "all",
    output_dir: str | None = None,
    source_names: list[str] | None = None,
    resource_types: list[str] | None = None,
    filename: str = "aws_evidence.json",
) -> dict[str, Any]:
    """Collect real AWS evidence records and persist them as JSON."""
    sources = list(source_names or DEFAULT_SOURCE_NAMES)
    resource_type_list = _iter_resource_types(resource_types)
    regions = resolve_regions(region)
    # Con una sola regione le sorgenti globali mantengono quella regione
    # (compatibilita'); in multi-regione sono etichettate "global".
    global_region = regions[0] if len(regions) == 1 else GLOBAL_REGION_LABEL

    regional = _regional_collectors(resource_type_list)
    records_by_source: dict[str, list[dict[str, Any]]] = {}

    regional_sources = [name for name in sources if name in regional]
    if regional_sources:
        by_region: dict[str, dict[str, list[dict[str, Any]]]] = {}

        def _run_region(reg: str) -> None:
            by_region[reg] = {name: regional[name](account_id, reg) for name in regional_sources}

        with ThreadPoolExecutor(max_workers=min(_MAX_REGION_WORKERS, len(regions))) as pool:
            list(pool.map(_run_region, regions))
        for name in regional_sources:
            records_by_source[name] = [r for reg in regions for r in by_region[reg][name]]

    for name in sources:
        if name in _GLOBAL_COLLECTORS:
            records_by_source[name] = _GLOBAL_COLLECTORS[name](account_id, global_region)

    for source_name in set(sources) - set(regional) - set(_GLOBAL_COLLECTORS):
        records_by_source[source_name] = [
            _make_record(
                account_id=account_id,
                region=global_region,
                resource_type="AWS::EC2::Volume",
                source=source_name,
                evidence_type="not_implemented",
                value={"status": "pending"},
                confidence=0.0,
                reason="Source is configured but not yet implemented in the real collection layer",
            )
        ]

    aggregated: list[dict[str, Any]] = [
        r for name in sources for r in records_by_source.get(name, [])
    ]

    payload: dict[str, Any] = {
        "account_id": account_id,
        "region": region,
        "regions": regions,
        "generated_at": _utc_now(),
        "sources": sources,
        "resource_types": resource_type_list,
        "records": aggregated,
    }

    target_dir = Path(output_dir) if output_dir else Path.cwd() / "exports"
    target_dir.mkdir(parents=True, exist_ok=True)

    file_path = target_dir / filename
    file_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=_json_default),
        encoding="utf-8",
    )

    for source_name, source_records in records_by_source.items():
        source_file = target_dir / f"aws_evidence_{source_name}.json"
        source_file.write_text(
            json.dumps(
                {
                    "account_id": account_id,
                    "region": region,
                    "regions": regions if source_name in regional else [global_region],
                    "source": source_name,
                    "generated_at": _utc_now(),
                    "records": source_records,
                },
                indent=2,
                ensure_ascii=False,
                default=_json_default,
            ),
            encoding="utf-8",
        )

    payload["file_path"] = str(file_path)
    payload["source_files"] = {source_name: str(target_dir / f"aws_evidence_{source_name}.json") for source_name in records_by_source}
    return payload


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Genera un file JSON di evidenze AWS")
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--region", default="all", help='"all" (default), una regione o lista "a,b"')
    parser.add_argument("--output-dir", default="exports")
    parser.add_argument("--source", action="append", dest="sources", default=None)
    parser.add_argument("--resource-type", action="append", dest="resource_types", default=None)
    args = parser.parse_args()

    result = collect_aws_evidence(
        account_id=args.account_id,
        region=args.region,
        output_dir=args.output_dir,
        source_names=args.sources,
        resource_types=args.resource_types,
    )
    print(json.dumps({"file_path": result["file_path"], "count": len(result["records"])}, indent=2))
