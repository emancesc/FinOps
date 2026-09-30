"""
Test multi-regione (moto): region="a,b" / "all", dedupe delle risorse globali,
S3 assegnato alla regione del bucket, attributi completi.
"""
import os

import boto3
import pytest
from moto import mock_aws

os.environ.setdefault("AWS_DEFAULT_REGION", "eu-south-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")

from app.aws_client import AWSClient, parse_region_spec

ACCOUNT_ID = "123456789012"
REGIONS = ["eu-south-1", "eu-west-1"]


def test_parse_region_spec():
    assert parse_region_spec("eu-south-1") == ["eu-south-1"]
    assert parse_region_spec(" eu-south-1 , eu-west-1,eu-south-1 ") == ["eu-south-1", "eu-west-1"]
    assert parse_region_spec("all") is None
    assert parse_region_spec("ALL") is None
    assert parse_region_spec("") is None
    assert parse_region_spec(None) is None


@pytest.fixture
def two_region_resources():
    with mock_aws():
        created = {}
        for region in REGIONS:
            ec2 = boto3.client("ec2", region_name=region)
            vpc_id = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
            subnet_id = ec2.create_subnet(VpcId=vpc_id, CidrBlock="10.0.1.0/24")["Subnet"]["SubnetId"]
            instance_id = ec2.run_instances(
                ImageId="ami-12345678", MinCount=1, MaxCount=1,
                InstanceType="t3.micro", SubnetId=subnet_id,
            )["Instances"][0]["InstanceId"]
            s3 = boto3.client("s3", region_name=region)
            bucket = f"finops-{region}"
            s3.create_bucket(Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": region})
            created[region] = {"vpc_id": vpc_id, "instance_id": instance_id, "bucket": bucket}
        yield created


def test_all_regions_discovered():
    with mock_aws():
        client = AWSClient(account_id=ACCOUNT_ID, region="all")
        assert client.is_multi_region
        assert set(REGIONS) <= set(client.regions)


def test_single_region_is_not_multi():
    with mock_aws():
        client = AWSClient(account_id=ACCOUNT_ID, region="eu-west-1")
        assert client.regions == ["eu-west-1"]
        assert client.region == "eu-west-1"
        assert not client.is_multi_region


def test_multi_region_extraction(two_region_resources):
    client = AWSClient(account_id=ACCOUNT_ID, region=",".join(REGIONS))
    resources = client.list_resources(["AWS::EC2::Instance", "AWS::EC2::VPC", "AWS::S3::Bucket"])

    ids = [r.resource_id for r in resources]
    assert len(ids) == len(set(ids)), "risorse duplicate tra regioni"

    for region, created in two_region_resources.items():
        inst_arn = f"arn:aws:ec2:{region}:{ACCOUNT_ID}:instance/{created['instance_id']}"
        inst = next(r for r in resources if r.resource_id == inst_arn)
        assert inst.region == region
        # Chiavi normalizzate prima, poi l'oggetto describe completo
        assert list(inst.attributes)[0] == "instance_type"
        assert inst.attributes["configuration"]["InstanceId"] == created["instance_id"]

        bucket_arn = f"arn:aws:s3:::{created['bucket']}"
        buckets = [r for r in resources if r.resource_id == bucket_arn]
        assert len(buckets) == 1
        assert buckets[0].region == region


def test_region_failure_does_not_block_others(two_region_resources, monkeypatch):
    original = AWSClient.list_resources

    def flaky(self, resource_types=None):
        if not self.is_multi_region and self.region == "eu-west-1":
            raise RuntimeError("AccessDenied")
        return original(self, resource_types)

    monkeypatch.setattr(AWSClient, "list_resources", flaky)
    client = AWSClient(account_id=ACCOUNT_ID, region=",".join(REGIONS))
    resources = client.list_resources(["AWS::EC2::VPC"])
    assert resources
    assert {r.region for r in resources} == {"eu-south-1"}


def test_config_item_attributes_are_complete():
    """_normalize: awsRegion, metadati del CI e configuration integrale."""
    with mock_aws():
        client = AWSClient(account_id=ACCOUNT_ID, region="eu-south-1")
    item = {
        "resourceId": "vol-1",
        "resourceName": "data",
        "resourceType": "AWS::EC2::Volume",
        "accountId": ACCOUNT_ID,
        "awsRegion": "eu-west-1",
        "availabilityZone": "eu-west-1a",
        "resourceCreationTime": "2026-01-01T00:00:00.000Z",
        "configuration": '{"size": 20, "volumeType": "gp3", "kmsKeyId": "k", "throughput": 125}',
        "supplementaryConfiguration": {"Foo": '{"bar": 1}'},
        "tags": [],
    }
    r = client._normalize(item, {})
    assert r.region == "eu-west-1"
    assert r.resource_id == f"arn:aws:ec2:eu-west-1:{ACCOUNT_ID}:volume/vol-1"
    assert list(r.attributes)[:2] == ["size_gb", "volume_type"]
    assert r.attributes["configuration"]["throughput"] == 125
    assert r.attributes["availability_zone"] == "eu-west-1a"
    assert r.attributes["resource_name"] == "data"
    assert r.attributes["supplementary_configuration"] == {"Foo": {"bar": 1}}
