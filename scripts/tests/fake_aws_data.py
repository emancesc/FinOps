"""Dati AWS finti condivisi dai test di extract_linked_resources (.py e .ps1)."""


def fake_aws(region, *args):
    call = " ".join(args[:2])
    populated = region == "eu-south-1"
    if region == "us-east-1" and call.startswith("ssm "):
        raise RuntimeError("AccessDeniedException")
    if call == "sts get-caller-identity":
        return {"Account": "123456789012"}
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
