from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import boto3


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
        client = boto3.client("config", region_name=region)
        for resource_type in resource_types:
            if resource_type in {"AWS::EC2::Volume", "AWS::EC2::NetworkInterface"}:
                expr = (
                    "SELECT resourceId, arn, resourceType, accountId, region, configuration, tags "
                    f"WHERE resourceType = '{resource_type}'"
                )
                kwargs: dict[str, Any] = {"Expression": expr, "Limit": 100}
                while True:
                    resp = client.select_resource_config(**kwargs)
                    for item in resp.get("Results", []):
                        payload = json.loads(item)
                        records.append(
                            _make_record(
                                account_id=account_id,
                                region=region,
                                resource_type=resource_type,
                                source="aws-config",
                                evidence_type="resource_config",
                                value={
                                    "resourceId": payload.get("resourceId"),
                                    "arn": payload.get("arn"),
                                    "configuration": payload.get("configuration"),
                                    "tags": payload.get("tags", []),
                                },
                                confidence=0.85,
                                reason="Live AWS Config snapshot for resource history and attachment context",
                            )
                        )
                    next_token = resp.get("NextToken")
                    if not next_token:
                        break
                    kwargs["NextToken"] = next_token
    except Exception as exc:  # noqa: BLE001
        records.append(
            _make_record(
                account_id=account_id,
                region=region,
                resource_type="AWS::EC2::Volume",
                source="aws-config",
                evidence_type="service_error",
                value={"error": str(exc)},
                confidence=0.0,
                reason="AWS Config not available or permissions missing; collection failed gracefully",
            )
        )
    return records


def _collect_ec2_eni_evidence(
    account_id: str,
    region: str,
    resource_types: Iterable[str],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        ec2 = boto3.client("ec2", region_name=region)
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
        elb = boto3.client("elbv2", region_name=region)
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
        client = boto3.client("cloudtrail", region_name=region)
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
        s3 = boto3.client("s3", region_name=region)
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
                                "resources": resources[:10],
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
        ssm = boto3.client("ssm", region_name=region)
        ec2 = boto3.client("ec2", region_name=region)
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
        sqs = boto3.client("sqs", region_name=region)
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
        client = boto3.client("cloudfront")
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
        client = boto3.client("route53")
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


def collect_aws_evidence(
    account_id: str,
    region: str,
    output_dir: str | None = None,
    source_names: list[str] | None = None,
    resource_types: list[str] | None = None,
    filename: str = "aws_evidence.json",
) -> dict[str, Any]:
    """Collect real AWS evidence records and persist them as JSON."""
    sources = list(source_names or DEFAULT_SOURCE_NAMES)
    resource_type_list = _iter_resource_types(resource_types)

    records_by_source: dict[str, list[dict[str, Any]]] = {}
    aggregated: list[dict[str, Any]] = []

    if "aws-config" in sources:
        source_records = _collect_aws_config_evidence(account_id, region, resource_type_list)
        records_by_source["aws-config"] = source_records
        aggregated.extend(source_records)
    if "ec2-eni" in sources:
        source_records = _collect_ec2_eni_evidence(account_id, region, resource_type_list)
        records_by_source["ec2-eni"] = source_records
        aggregated.extend(source_records)
    if "elb" in sources:
        source_records = _collect_elb_evidence(account_id, region, resource_type_list)
        records_by_source["elb"] = source_records
        aggregated.extend(source_records)
    if "cloudfront" in sources:
        source_records = _collect_cloudfront_evidence(account_id, region)
        records_by_source["cloudfront"] = source_records
        aggregated.extend(source_records)
    if "route53" in sources:
        source_records = _collect_route53_evidence(account_id, region)
        records_by_source["route53"] = source_records
        aggregated.extend(source_records)
    if "oc-routes" in sources:
        source_records = _collect_oc_routes_evidence(account_id, region)
        records_by_source["oc-routes"] = source_records
        aggregated.extend(source_records)
    if "cloudtrail" in sources:
        source_records = _collect_cloudtrail_evidence(account_id, region)
        records_by_source["cloudtrail"] = source_records
        aggregated.extend(source_records)
    if "terraform-state" in sources:
        source_records = _collect_terraform_state_evidence(account_id, region)
        records_by_source["terraform-state"] = source_records
        aggregated.extend(source_records)
    if "ssm-inventory" in sources:
        source_records = _collect_ssm_inventory_evidence(account_id, region)
        records_by_source["ssm-inventory"] = source_records
        aggregated.extend(source_records)
    if "sqs" in sources:
        source_records = _collect_sqs_evidence(account_id, region)
        records_by_source["sqs"] = source_records
        aggregated.extend(source_records)

    for source_name in set(sources) - {"aws-config", "ec2-eni", "elb", "cloudfront", "route53", "oc-routes", "cloudtrail", "terraform-state", "ssm-inventory", "sqs"}:
        placeholder = [
            _make_record(
                account_id=account_id,
                region=region,
                resource_type="AWS::EC2::Volume",
                source=source_name,
                evidence_type="not_implemented",
                value={"status": "pending"},
                confidence=0.0,
                reason="Source is configured but not yet implemented in the real collection layer",
            )
        ]
        records_by_source[source_name] = placeholder
        aggregated.extend(placeholder)

    payload: dict[str, Any] = {
        "account_id": account_id,
        "region": region,
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
    parser.add_argument("--region", required=True)
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
