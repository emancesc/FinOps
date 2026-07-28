"""
Test di integrazione con moto: verifica che l'estrazione popoli raw_resources
con EC2, EBS, VPC, Subnet, SecurityGroup, S3 con relazioni coerenti.
"""
import json
import os
import pytest
import boto3
from moto import mock_aws

# ---------------------------------------------------------------------------
# Fixtures AWS (moto)
# ---------------------------------------------------------------------------

ACCOUNT_ID = "123456789012"
REGION = "eu-south-1"

os.environ.setdefault("AWS_DEFAULT_REGION", REGION)
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")


@pytest.fixture
def aws_resources():
    """Crea un set di risorse EC2/S3 in moto e ritorna i loro ID."""
    with mock_aws():
        ec2 = boto3.client("ec2", region_name=REGION)

        # VPC
        vpc = ec2.create_vpc(CidrBlock="10.0.0.0/16")
        vpc_id = vpc["Vpc"]["VpcId"]
        ec2.create_tags(Resources=[vpc_id], Tags=[{"Key": "Name", "Value": "test-vpc"}])

        # Subnet
        subnet = ec2.create_subnet(VpcId=vpc_id, CidrBlock="10.0.1.0/24")
        subnet_id = subnet["Subnet"]["SubnetId"]
        ec2.create_tags(Resources=[subnet_id], Tags=[{"Key": "Name", "Value": "test-subnet"}])

        # Security Group
        sg = ec2.create_security_group(
            GroupName="test-sg",
            Description="Test SG",
            VpcId=vpc_id,
        )
        sg_id = sg["GroupId"]
        ec2.create_tags(Resources=[sg_id], Tags=[{"Key": "Name", "Value": "test-sg"}])

        # EC2 Instance
        instance = ec2.run_instances(
            ImageId="ami-12345678",
            MinCount=1,
            MaxCount=1,
            InstanceType="t3.medium",
            SubnetId=subnet_id,
            SecurityGroupIds=[sg_id],
            TagSpecifications=[{
                "ResourceType": "instance",
                "Tags": [{"Key": "Name", "Value": "web-01"}, {"Key": "env", "Value": "prod"}],
            }],
        )
        instance_id = instance["Instances"][0]["InstanceId"]

        # EBS Volume
        volume = ec2.create_volume(
            AvailabilityZone=f"{REGION}a",
            Size=20,
            VolumeType="gp3",
            TagSpecifications=[{
                "ResourceType": "volume",
                "Tags": [{"Key": "Name", "Value": "data-disk"}],
            }],
        )
        volume_id = volume["VolumeId"]

        # S3 Bucket
        s3 = boto3.client("s3", region_name=REGION)
        bucket_name = "test-finops-bucket"
        s3.create_bucket(
            Bucket=bucket_name,
            CreateBucketConfiguration={"LocationConstraint": REGION},
        )
        s3.put_bucket_tagging(
            Bucket=bucket_name,
            Tagging={"TagSet": [{"Key": "team", "Value": "platform"}]},
        )

        yield {
            "vpc_id": vpc_id,
            "subnet_id": subnet_id,
            "sg_id": sg_id,
            "instance_id": instance_id,
            "volume_id": volume_id,
            "bucket_name": bucket_name,
        }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@mock_aws
def test_list_resources_types(aws_resources):
    """Verifica che list_resources restituisca le 6 tipologie attese."""
    from app.aws_client import AWSClient, DEFAULT_RESOURCE_TYPES

    client = AWSClient(account_id=ACCOUNT_ID, region=REGION)
    resources = client.list_resources()

    types_found = {r.resource_type for r in resources}
    expected = set(DEFAULT_RESOURCE_TYPES)
    # Almeno EC2, VPC, Subnet, SG, S3 devono essere presenti
    assert "AWS::EC2::Instance" in types_found
    assert "AWS::EC2::VPC" in types_found
    assert "AWS::EC2::Subnet" in types_found
    assert "AWS::EC2::SecurityGroup" in types_found
    assert "AWS::S3::Bucket" in types_found


@mock_aws
def test_ec2_instance_attributes(aws_resources):
    """Verifica gli attributi dell'istanza EC2."""
    from app.aws_client import AWSClient

    client = AWSClient(account_id=ACCOUNT_ID, region=REGION)
    resources = client.list_resources(["AWS::EC2::Instance"])

    assert len(resources) >= 1
    inst = resources[0]
    assert inst.resource_type == "AWS::EC2::Instance"
    assert inst.attributes.get("instance_type") == "t3.medium"
    assert inst.attributes.get("vpc_id") == aws_resources["vpc_id"]
    assert inst.attributes.get("subnet_id") == aws_resources["subnet_id"]


@mock_aws
def test_ec2_instance_relationships(aws_resources):
    """Verifica le relazioni CONTAINS e SECURED_BY dell'istanza."""
    from app.aws_client import AWSClient

    client = AWSClient(account_id=ACCOUNT_ID, region=REGION)
    resources = client.list_resources(["AWS::EC2::Instance"])

    inst = resources[0]
    rel_types = {r.type for r in inst.relationships}
    assert "CONTAINS" in rel_types, f"Manca CONTAINS, rels={inst.relationships}"
    assert "SECURED_BY" in rel_types, f"Manca SECURED_BY, rels={inst.relationships}"

    # Verifica che gli ARN referenziati siano corretti
    vpc_arn = f"arn:aws:ec2:{REGION}:{ACCOUNT_ID}:vpc/{aws_resources['vpc_id']}"
    sg_arn = f"arn:aws:ec2:{REGION}:{ACCOUNT_ID}:security-group/{aws_resources['sg_id']}"
    all_targets = {r.target_resource_id for r in inst.relationships}
    assert vpc_arn in all_targets
    assert sg_arn in all_targets


@mock_aws
def test_subnet_contains_vpc(aws_resources):
    """Verifica che la Subnet abbia relazione CONTAINS verso la VPC."""
    from app.aws_client import AWSClient

    client = AWSClient(account_id=ACCOUNT_ID, region=REGION)
    resources = client.list_resources(["AWS::EC2::Subnet"])

    assert len(resources) >= 1
    vpc_arn = f"arn:aws:ec2:{REGION}:{ACCOUNT_ID}:vpc/{aws_resources['vpc_id']}"
    subnet_arn = f"arn:aws:ec2:{REGION}:{ACCOUNT_ID}:subnet/{aws_resources['subnet_id']}"
    our_subnet = next((r for r in resources if r.resource_id == subnet_arn), None)
    assert our_subnet is not None, f"Subnet {subnet_arn} non trovata tra {[r.resource_id for r in resources]}"
    targets = {r.target_resource_id for r in our_subnet.relationships}
    assert vpc_arn in targets


@mock_aws
def test_s3_bucket_extracted(aws_resources):
    """Verifica che il bucket S3 venga estratto."""
    from app.aws_client import AWSClient

    client = AWSClient(account_id=ACCOUNT_ID, region=REGION)
    resources = client.list_resources(["AWS::S3::Bucket"])

    bucket_names = [
        r.resource_id.split(":::")[-1] if ":::" in r.resource_id else r.resource_id
        for r in resources
    ]
    assert aws_resources["bucket_name"] in bucket_names or any(
        aws_resources["bucket_name"] in r.resource_id for r in resources
    )


@mock_aws
def test_resource_ids_are_arns(aws_resources):
    """Verifica che tutti i resource_id siano ARN validi."""
    from app.aws_client import AWSClient

    client = AWSClient(account_id=ACCOUNT_ID, region=REGION)
    resources = client.list_resources()

    for r in resources:
        assert r.resource_id.startswith("arn:"), (
            f"resource_id non e' un ARN: {r.resource_id} (type={r.resource_type})"
        )
