"""
Controllo account: le credenziali e le risorse devono essere dell'account del job.
"""
from __future__ import annotations

import pytest
from moto import mock_aws

from app.aws_client import AWSClient, AccountMismatchError, NormalizedResource
from app.db import check_resource_accounts

ACCOUNT_ID = "123456789012"  # account restituito da moto per get_caller_identity
REGION = "eu-west-1"


@pytest.fixture(autouse=True)
def aws_credentials(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)


def _resource(account_id: str) -> NormalizedResource:
    return NormalizedResource(
        resource_id=f"arn:aws:ec2:{REGION}:{account_id}:instance/i-0abc",
        account_id=account_id,
        region=REGION,
        resource_type="AWS::EC2::Instance",
    )


@mock_aws
def test_verify_account_accepts_matching_credentials():
    AWSClient(account_id=ACCOUNT_ID, region=REGION).verify_account()


@mock_aws
def test_verify_account_rejects_other_account():
    client = AWSClient(account_id="943560362505", region=REGION)
    with pytest.raises(AccountMismatchError) as exc:
        client.verify_account()
    assert exc.value.expected == "943560362505"
    assert exc.value.actual == ACCOUNT_ID


def test_check_resource_accounts_accepts_same_account():
    check_resource_accounts(ACCOUNT_ID, [_resource(ACCOUNT_ID), _resource(ACCOUNT_ID)])


def test_check_resource_accounts_rejects_foreign_resources():
    with pytest.raises(AccountMismatchError) as exc:
        check_resource_accounts("943560362505", [_resource(ACCOUNT_ID)])
    assert exc.value.actual == ACCOUNT_ID
