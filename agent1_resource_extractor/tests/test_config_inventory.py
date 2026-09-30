"""
Test dell'estrazione "config-inventory": scoperta dinamica dei resource type
da AWS Config, risoluzione delle relationships in ARN, e analisi di
conformita' rispetto alla CINECA Tagging Strategy.

AWS Config non e' simulabile con moto (select_resource_config non e'
implementato), quindi qui si monkeypatch-a la logica di query di AWSClient
direttamente, per testare la normalizzazione e l'arricchimento che aggiungiamo
in list_all_resources_from_config().
"""
import os

os.environ.setdefault("AWS_DEFAULT_REGION", "eu-south-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")

from app.aws_client import AWSClient, _normalize_relationship_type
from app.tagging_strategy import analyze_tagging

ACCOUNT_ID = "123456789012"
REGION = "eu-south-1"


def test_normalize_relationship_type():
    assert _normalize_relationship_type("Is attached to Instance") == "ATTACHED_TO"
    assert _normalize_relationship_type("Is associated with Security Group") == "SECURED_BY"
    assert _normalize_relationship_type("Is contained in VPC") == "CONTAINS"
    assert _normalize_relationship_type("Is used by AutoScaling Group") == "DEPENDS_ON"


def test_analyze_tagging_fully_compliant():
    tags = {
        "cineca:BusinessUnit": "UNIV",
        "cineca:Customer": "UNIBO",
        "cineca:Product": "ESSE3",
        "cineca:Environment": "PROD",
        "cineca:Service": "Oracle",
        "cineca:Role": "Database-Primary",
        "cineca:ManagedBy": "Terraform",
    }
    result = analyze_tagging("AWS::EC2::Instance", tags)
    assert result["compliant"] is True
    assert result["missing_mandatory"] == []
    assert result["missing_recommended"] == []


def test_analyze_tagging_missing_mandatory():
    result = analyze_tagging("AWS::EC2::Volume", {"Name": "vol-orphan"})
    assert result["compliant"] is False
    assert "cineca:BusinessUnit" in result["missing_mandatory"]
    # Suggerimento di Role dedotto dal resource type
    assert result["suggestions"]["cineca:Role"] == "Storage-Volume"


def test_analyze_tagging_role_exempt_type_not_flagged():
    result = analyze_tagging("AWS::EC2::EIP", {})
    assert result["is_role_exempt"] is True
    assert "cineca:Role" not in result["missing_recommended"]


def test_analyze_tagging_infrastructure_suggestions():
    result = analyze_tagging("AWS::EC2::VPC", {})
    assert result["is_infrastructure"] is True
    assert result["suggestions"]["cineca:Customer"] == "shared"
    assert result["suggestions"]["cineca:Product"] == "shared"


def test_analyze_tagging_name_pattern_hint():
    result = analyze_tagging("AWS::EC2::Instance", {}, name="idp5be-aws-103")
    assert result["suggestions"]["cineca:Service"] == "shibboleth-idp"
    assert result["suggestions"]["cineca:Role"] == "Identity-Backend"


def test_list_all_resources_from_config(monkeypatch):
    """
    Verifica end-to-end (con query Config mockate) che:
    - i tipi scoperti da discover_config_resource_types vengano tutti interrogati
    - le relationships vengano risolte in ARN quando la risorsa target e' tra
      quelle estratte, e in un id sintetico altrimenti
    - ogni risorsa porti attributes["tagging_analysis"]
    """
    client = AWSClient(account_id=ACCOUNT_ID, region=REGION)

    instance_arn = f"arn:aws:ec2:{REGION}:{ACCOUNT_ID}:instance/i-0abc"
    volume_arn = f"arn:aws:ec2:{REGION}:{ACCOUNT_ID}:volume/vol-0abc"

    fake_items = {
        "AWS::EC2::Instance": [
            {
                "resourceId": "i-0abc",
                "arn": instance_arn,
                "resourceType": "AWS::EC2::Instance",
                "accountId": ACCOUNT_ID,
                "region": REGION,
                "configuration": "{}",
                "tags": [{"key": "Name", "value": "idp5be-aws-103"}],
                "relationships": [
                    {
                        "relationshipName": "Is attached to Volume",
                        "resourceId": "vol-0abc",
                        "resourceType": "AWS::EC2::Volume",
                    },
                    {
                        "relationshipName": "Is a member of Auto Scaling Group",
                        "resourceId": "asg-not-extracted",
                        "resourceType": "AWS::AutoScaling::AutoScalingGroup",
                    },
                ],
            }
        ],
        "AWS::EC2::Volume": [
            {
                "resourceId": "vol-0abc",
                "arn": volume_arn,
                "resourceType": "AWS::EC2::Volume",
                "accountId": ACCOUNT_ID,
                "region": REGION,
                "configuration": "{}",
                "tags": [],
                "relationships": [
                    {
                        "relationshipName": "Is attached to Instance",
                        "resourceId": "i-0abc",
                        "resourceType": "AWS::EC2::Instance",
                    }
                ],
            }
        ],
    }

    monkeypatch.setattr(
        client, "discover_config_resource_types", lambda: sorted(fake_items.keys())
    )
    monkeypatch.setattr(client, "_get_tags_by_arn", lambda types: {})
    monkeypatch.setattr(
        client,
        "_config_query",
        lambda resource_type, select_fields=None: fake_items.get(resource_type),
    )

    resources = client.list_all_resources_from_config()
    assert len(resources) == 2

    by_id = {r.resource_id: r for r in resources}
    instance = by_id[instance_arn]
    volume = by_id[volume_arn]

    # Relationship risolta in ARN quando la risorsa target e' stata estratta
    rel_targets = {r.target_resource_id: r.type for r in instance.relationships}
    assert rel_targets[volume_arn] == "ATTACHED_TO"
    # Relationship verso una risorsa non estratta: id sintetico, tipo di default
    assert "AWS::AutoScaling::AutoScalingGroup:asg-not-extracted" in rel_targets
    assert rel_targets["AWS::AutoScaling::AutoScalingGroup:asg-not-extracted"] == "DEPENDS_ON"

    # Tagging analysis presente su entrambe, con suggerimenti coerenti
    assert instance.attributes["tagging_analysis"]["suggestions"]["cineca:Service"] == "shibboleth-idp"
    assert volume.attributes["tagging_analysis"]["suggestions"]["cineca:Role"] == "Storage-Volume"
