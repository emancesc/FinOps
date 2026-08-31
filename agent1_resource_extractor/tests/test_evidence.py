import io
import json

from app.evidence import collect_aws_evidence


def test_collect_aws_evidence_handles_new_sources(monkeypatch, tmp_path):
    account_id = "123456789012"
    region = "eu-south-1"

    class FakeCloudFrontClient:
        def list_distributions(self, **kwargs):
            if kwargs.get("Marker") is None:
                return {
                    "DistributionList": {
                        "Items": [{"Id": "dist-1", "ARN": "arn:aws:cloudfront::123456789012:distribution/dist-1"}],
                        "IsTruncated": True,
                        "NextMarker": "next-dist",
                    }
                }
            return {"DistributionList": {"Items": [{"Id": "dist-2", "ARN": "arn:aws:cloudfront::123456789012:distribution/dist-2"}], "IsTruncated": False}}

        def get_distribution(self, **kwargs):
            return {
                "Distribution": {
                    "DomainName": "d123.cloudfront.net",
                    "Status": "Deployed",
                    "DistributionConfig": {
                        "Aliases": {"Items": ["apps.example.com"]},
                        "Origins": {"Items": [{"DomainName": "example-origin.example.com"}]},
                    },
                }
            }

        def list_tags_for_resource(self, **kwargs):
            return {"Tags": {"Items": [{"Key": "env", "Value": "prod"}]}}

    class FakeRoute53Client:
        def list_hosted_zones(self, **kwargs):
            if kwargs.get("Marker") is None:
                return {
                    "HostedZones": [{"Id": "/hostedzone/Z123", "Name": "example.com."}],
                    "IsTruncated": True,
                    "NextMarker": "next-zone",
                }
            return {"HostedZones": [{"Id": "/hostedzone/Z456", "Name": "second.example.com."}], "IsTruncated": False}

        def list_resource_record_sets(self, **kwargs):
            if kwargs.get("HostedZoneId") == "Z123":
                return {
                    "ResourceRecordSets": [{"Name": "example.com.", "Type": "A", "TTL": 300, "ResourceRecords": [{"Value": "1.2.3.4"}]}],
                    "IsTruncated": True,
                    "NextRecordName": "www.example.com.",
                    "NextRecordType": "A",
                }
            return {"ResourceRecordSets": [{"Name": "www.example.com.", "Type": "A", "TTL": 60, "ResourceRecords": [{"Value": "5.6.7.8"}]}], "IsTruncated": False}

    class FakeEC2Client:
        def get_paginator(self, operation_name):
            return type("P", (), {"paginate": lambda self, **kwargs: iter([{"Reservations": [{"Instances": [{"InstanceId": "i-123"}]}]}])})()

    class FakeSSMClient:
        def list_inventory_entries(self, **kwargs):
            return {"Entries": [{"Name": "app"}]}

    class FakeSQSClient:
        def list_queues(self, **kwargs):
            return {"QueueUrls": ["https://queue/1"]}

        def get_queue_attributes(self, **kwargs):
            return {"Attributes": {"QueueArn": "arn:aws:sqs:eu-south-1:123456789012:demo", "OwnerAccountId": "123456789012"}}

    class FakeCloudTrailClient:
        def lookup_events(self, **kwargs):
            return {"Events": [{"EventId": "ct-1", "EventName": "CreateVolume", "UserIdentity": {"Type": "IAMUser"}, "Resources": []}]}

    class FakeS3Client:
        def list_buckets(self):
            return {"Buckets": [{"Name": "cineca-tf-state-demo"}]}

        def list_objects_v2(self, **kwargs):
            return {"Contents": [{"Key": "main.tfstate"}], "IsTruncated": False}

        def get_object(self, **kwargs):
            return {"Body": io.BytesIO(json.dumps({"resources": [{"type": "aws_instance", "name": "demo"}]}).encode("utf-8"))}

    def fake_client(service_name, region_name=None):
        if service_name == "cloudfront":
            return FakeCloudFrontClient()
        if service_name == "route53":
            return FakeRoute53Client()
        if service_name == "ec2":
            return FakeEC2Client()
        if service_name == "ssm":
            return FakeSSMClient()
        if service_name == "sqs":
            return FakeSQSClient()
        if service_name == "cloudtrail":
            return FakeCloudTrailClient()
        if service_name == "s3":
            return FakeS3Client()
        raise AssertionError(f"Unexpected service: {service_name}")

    def fake_run(command, capture_output, text, check):
        return type(
            "R",
            (),
            {"returncode": 0, "stdout": json.dumps({"items": [{"metadata": {"name": "demo", "namespace": "prod"}, "spec": {"host": "demo.example.com", "to": {"name": "svc"}}, "status": {"ingress": [{"host": "demo.example.com"}]}}]})},
        )()

    monkeypatch.setattr("app.evidence.boto3.client", fake_client)
    monkeypatch.setattr("app.evidence.subprocess.run", fake_run)

    result = collect_aws_evidence(
        account_id=account_id,
        region=region,
        output_dir=str(tmp_path),
        source_names=["cloudfront", "route53", "oc-routes", "cloudtrail", "terraform-state", "ssm-inventory", "sqs"],
    )

    assert len([r for r in result["records"] if r["source"] == "cloudfront"]) >= 2
    assert len([r for r in result["records"] if r["source"] == "route53"]) >= 2
    assert len([r for r in result["records"] if r["source"] == "oc-routes"]) >= 1
    assert "cloudfront" in result["sources"]
    assert "route53" in result["sources"]
    assert "oc-routes" in result["sources"]


def test_collect_aws_evidence_handles_pagination(monkeypatch, tmp_path):
    account_id = "123456789012"
    region = "eu-south-1"

    class FakePaginator:
        def __init__(self, pages):
            self._pages = pages

        def paginate(self, **kwargs):
            return iter(self._pages)

    class FakeEC2Client:
        def get_paginator(self, operation_name):
            return FakePaginator([
                {
                    "Reservations": [{"Instances": [{"InstanceId": "i-123"}]}]
                }
            ])

    class FakeSSMClient:
        def list_inventory_entries(self, **kwargs):
            if kwargs.get("NextToken") is None:
                return {
                    "Entries": [{"Name": "first-app"}],
                    "NextToken": "next-page",
                }
            return {"Entries": [{"Name": "second-app"}]}

    class FakeSQSClient:
        def list_queues(self, **kwargs):
            if kwargs.get("NextToken") is None:
                return {"QueueUrls": ["https://queue/1"], "NextToken": "next-queue"}
            return {"QueueUrls": ["https://queue/2"]}

        def get_queue_attributes(self, **kwargs):
            return {
                "Attributes": {
                    "QueueArn": "arn:aws:sqs:eu-south-1:123456789012:demo",
                    "OwnerAccountId": account_id,
                }
            }

    class FakeCloudTrailClient:
        def lookup_events(self, **kwargs):
            if kwargs.get("NextToken") is None:
                return {
                    "Events": [{"EventId": "ct-1", "EventName": "CreateVolume", "UserIdentity": {"Type": "IAMUser"}, "Resources": []}],
                    "NextToken": "next-ct",
                }
            return {
                "Events": [{"EventId": "ct-2", "EventName": "CreateNetworkInterface", "UserIdentity": {"Type": "IAMUser"}, "Resources": []}]
            }

    class FakeS3Client:
        def list_buckets(self):
            return {"Buckets": [{"Name": "cineca-tf-state-demo"}]}

        def list_objects_v2(self, **kwargs):
            token = kwargs.get("ContinuationToken")
            if token is None:
                return {
                    "Contents": [{"Key": "main.tfstate"}],
                    "IsTruncated": True,
                    "NextContinuationToken": "next-object",
                }
            return {"Contents": [{"Key": "main.tfstate.backup"}], "IsTruncated": False}

        def get_object(self, **kwargs):
            body = json.dumps({"resources": [{"type": "aws_instance", "name": "demo"}]})
            return {"Body": io.BytesIO(body.encode("utf-8"))}

    def fake_client(service_name, region_name=None):
        if service_name == "cloudtrail":
            return FakeCloudTrailClient()
        if service_name == "s3":
            return FakeS3Client()
        if service_name == "ssm":
            return FakeSSMClient()
        if service_name == "sqs":
            return FakeSQSClient()
        if service_name == "ec2":
            return FakeEC2Client()
        raise AssertionError(f"Unexpected service: {service_name}")

    monkeypatch.setattr("app.evidence.boto3.client", fake_client)

    result = collect_aws_evidence(
        account_id=account_id,
        region=region,
        output_dir=str(tmp_path),
        source_names=["cloudtrail", "terraform-state", "ssm-inventory", "sqs"],
    )

    assert len(result["records"]) >= 8
    assert len([r for r in result["records"] if r["source"] == "cloudtrail"]) >= 2
    assert len([r for r in result["records"] if r["source"] == "terraform-state"]) >= 2
    assert len([r for r in result["records"] if r["source"] == "ssm-inventory"]) >= 2
    assert len([r for r in result["records"] if r["source"] == "sqs"]) >= 2


def test_collect_aws_evidence_creates_json_file(tmp_path):
    account_id = "123456789012"
    region = "eu-south-1"

    result = collect_aws_evidence(
        account_id=account_id,
        region=region,
        output_dir=str(tmp_path),
        source_names=["aws-config", "ec2-eni", "elb"],
        resource_types=["AWS::EC2::Instance"],
    )

    assert result["account_id"] == account_id
    assert result["region"] == region
    assert "records" in result
    assert isinstance(result["records"], list)

    json_path = tmp_path / "aws_evidence.json"
    assert json_path.exists()

    source_json = tmp_path / "aws_evidence_aws-config.json"
    assert source_json.exists()

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["account_id"] == account_id
    assert payload["region"] == region
    assert "aws-config" in payload["sources"]
