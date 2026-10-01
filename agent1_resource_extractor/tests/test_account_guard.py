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


def _db_or_skip():
    import os
    import psycopg2
    url = os.environ.get("DATABASE_URL", "postgresql://finops:changeme@localhost:5432/finops")
    try:
        return url, psycopg2.connect(url, connect_timeout=3)
    except psycopg2.OperationalError:
        pytest.skip("PostgreSQL non raggiungibile")


def test_same_arn_in_two_accounts_keeps_both(monkeypatch):
    """Un ARN senza account (es. regole Route 53 Resolver autodefined) non sposta la risorsa dell'altro account."""
    import uuid
    from app.db import upsert_resources

    url, conn = _db_or_skip()
    monkeypatch.setenv("DATABASE_URL", url)
    arn = f"arn:aws:route53resolver:{REGION}::autodefined-rule/rslvr-test-{uuid.uuid4().hex[:8]}"
    jobs = {"111111111111": str(uuid.uuid4()), "222222222222": str(uuid.uuid4())}
    try:
        with conn, conn.cursor() as cur:
            for account, job_id in jobs.items():
                cur.execute("INSERT INTO jobs (job_id, account_id, region, tenant_id, phase) "
                            "VALUES (%s, %s, 'all', 'test', 'created')", (job_id, account))
        for account, job_id in jobs.items():
            res = _resource(account).model_copy(update={"resource_id": arn})
            upsert_resources(job_id, [res])
        with conn, conn.cursor() as cur:
            cur.execute("SELECT account_id, job_id::text FROM raw_resources WHERE resource_id = %s ORDER BY 1", (arn,))
            assert cur.fetchall() == sorted(jobs.items())
    finally:
        with conn, conn.cursor() as cur:
            cur.execute("DELETE FROM jobs WHERE job_id = ANY(%s::uuid[])", (list(jobs.values()),))
        conn.close()
