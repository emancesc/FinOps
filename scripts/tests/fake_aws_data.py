"""Dati AWS finti condivisi dai test di extract_linked_resources (.py e .ps1)."""


def fake_aws(region, *args):
    call = " ".join(args[:2])
    populated = region == "eu-south-1"
    if region == "us-east-1" and call.startswith("ssm "):
        raise RuntimeError("AccessDeniedException")
    if call == "sts get-caller-identity":
        return {"Account": "123456789012"}
    if call.startswith("resource-explorer-2 "):
        return fake_resource_explorer(region, *args)
    if call == "ec2 describe-regions":
        return {"Regions": [{"RegionName": r} for r in ("eu-south-1", "eu-west-1", "us-east-1")]}
    if call == "ssm describe-instance-information":
        return {"InstanceInformationList": [{"InstanceId": "i-1"}] if populated else []}
    if call == "cloudformation describe-stacks":
        return {"Stacks": [{"StackId": "stack-1"}] if populated else []}
    if call == "cloudformation list-stack-resources":
        return {"StackResourceSummaries": [{"LogicalResourceId": "Vol"}]}
    if call == "ec2 describe-addresses":
        return {"Addresses": [{"AllocationId": "eipalloc-1"}] if populated else []}
    if call == "acm list-certificates":
        certs = {"eu-south-1": ["arn:cert-1", "arn:cert-2"], "us-east-1": ["arn:cert-cf"]}.get(region, [])
        return {"CertificateSummaryList": [{"CertificateArn": a} for a in certs]}
    if call == "acm describe-certificate":
        arn = args[args.index("--certificate-arn") + 1]
        return {"Certificate": {"CertificateArn": arn, "InUseBy": ["arn:elb"] if arn != "arn:cert-2" else []}}
    if call == "acm list-tags-for-certificate":
        return {"Tags": []}
    if call == "configservice describe-config-rules":
        return {"ConfigRules": [{"ConfigRuleName": "r1", "ConfigRuleArn": "arn:rule"}] if populated else []}
    if call == "configservice describe-compliance-by-config-rule":
        return {"ComplianceByConfigRules": [{"ConfigRuleName": "r1", "Compliance": {"ComplianceType": "COMPLIANT"}}]}
    if call == "configservice list-tags-for-resource":
        return {"Tags": []}
    if call == "ec2 describe-network-interfaces":
        return {"NetworkInterfaces": [{"NetworkInterfaceId": "eni-1"}] if populated else []}
    if call == "ec2 describe-volumes":
        if not populated:
            return {"Volumes": []}
        return {"Volumes": [
            {"VolumeId": "vol-1", "State": "in-use", "VolumeType": "gp3", "Size": 8, "AvailabilityZone": "eu-south-1a",
             "Attachments": [{"InstanceId": "i-1", "Device": "/dev/xvda", "State": "attached"}]},
            {"VolumeId": "vol-2", "State": "available", "VolumeType": "gp3", "Size": 4, "AvailabilityZone": "eu-south-1b",
             "Attachments": []},
        ]}
    if call == "ec2 describe-snapshots":
        return {"Snapshots": [{"SnapshotId": "snap-1", "VolumeId": "vol-1"}, {"SnapshotId": "snap-ami"}] if populated else []}
    if call == "ec2 describe-instances":
        if not populated:
            return {"Reservations": []}
        return {"Reservations": [{"Instances": [
            {"InstanceId": "i-1", "State": {"Name": "running"}, "Tags": [{"Key": "Name", "Value": "web-01"}]},
            {"InstanceId": "i-2", "State": {"Name": "stopped"}},
        ]}]}
    raise AssertionError(f"chiamata inattesa: {region} {args}")


def _re_resource(region, kind, rid, tags=None):
    props = []
    if tags is not None:
        props.append({"Name": "tags", "LastReportedAt": "2026-09-25T12:00:00+00:00",
                      "Data": [{"Key": k, "Value": v} for k, v in tags.items()]})
    service, rtype = kind.split(":")
    return {
        "Arn": f"arn:aws:{service}:{region}:123456789012:{rtype}/{rid}",
        "OwningAccountId": "123456789012",
        "Region": region,
        "ResourceType": kind,
        "Service": service,
        "LastReportedAt": "2026-09-26T03:00:00+00:00",
        "Properties": props,
    }


COMPLIANT = {"cineca:BusinessUnit": "UNIV", "cineca:Customer": "UNIBO", "cineca:Product": "ESSE3",
             "cineca:Environment": "PROD", "cineca:Role": "Web", "Name": "web-01", "Owner": "team-a"}

# Resource Explorer: eu-south-1 su 2 pagine, us-east-1 1 pagina, eu-west-1 negata da SCP
RESOURCE_EXPLORER_PAGES = {
    "eu-south-1": [
        [_re_resource("eu-south-1", "ec2:instance", "i-1", COMPLIANT),
         _re_resource("eu-south-1", "ec2:volume", "vol-1", {"Name": "data", "cineca:Customer": "UNIBO"})],
        [_re_resource("eu-south-1", "ec2:snapshot", "snap-1"),
         _re_resource("eu-south-1", "s3:bucket", "b-1", {"cineca:Custom": "x"})],
    ],
    "us-east-1": [[_re_resource("us-east-1", "acm:certificate", "c-1", {"Name": "cert"})]],
}


def fake_resource_explorer(region, *args):
    call = " ".join(args[:2])
    if call == "resource-explorer-2 list-indexes":
        return {"Indexes": [{"Region": r, "Type": "LOCAL"} for r in ("eu-south-1", "eu-west-1", "us-east-1")]}
    if call == "resource-explorer-2 list-resources":
        if region == "eu-west-1":
            raise RuntimeError("AccessDeniedException ... with an explicit deny in a service control policy")
        pages = RESOURCE_EXPLORER_PAGES.get(region, [[]])
        token = args[args.index("--next-token") + 1] if "--next-token" in args else None
        index = int(token.split("-")[1]) if token else 0
        out = {"Resources": pages[index], "ViewArn": f"arn:view/{region}"}
        if index + 1 < len(pages):
            out["NextToken"] = f"page-{index + 1}"
        return out
    raise AssertionError(f"chiamata resource-explorer inattesa: {region} {args}")
