"""
Test Fase 5 — Arbiter & Policy Registry.
Acceptance criteria:
  una seconda richiesta identica (stesso tenant/resource_type/tag_key)
  viene risolta dalla regola approvata senza chiamata LLM.
"""
from __future__ import annotations
import json
import os
import uuid
import psycopg2
import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

os.environ.setdefault("DATABASE_URL", "postgresql://finops:changeme@localhost:5432/finops")

from llm_gateway.base import LLMClient, LLMMessage, LLMResponse


# ---------------------------------------------------------------------------
# Mock LLM
# ---------------------------------------------------------------------------

class _MockLLM(LLMClient):
    def __init__(self, resolved_value: str = "production"):
        self.call_count = 0
        self._value = resolved_value

    async def complete(self, system, messages, response_format=None, max_tokens=4096):
        self.call_count += 1
        payload = json.dumps({
            "resolved_value": self._value,
            "confidence": 0.85,
            "reasoning": "Dedotto dal contesto",
            "proposed_rule": {
                "tag_key": "environment",
                "condition": {"resource_type": "AWS::EC2::Instance", "description": "Tutte le istanze EC2"},
                "resolution": {"strategy": "fixed_value", "detail": self._value},
            },
        })
        return LLMResponse(content=payload, model="mock", input_tokens=50, output_tokens=30)


# ---------------------------------------------------------------------------
# Helpers DB
# ---------------------------------------------------------------------------

def _connect():
    return psycopg2.connect(os.environ["DATABASE_URL"])


def _insert_job(job_id: str, tenant_id: str = "tenant-test"):
    conn = _connect()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO jobs (job_id, account_id, region, tenant_id, phase) "
                    "VALUES (%s::uuid, %s, %s, %s, %s)",
                    (job_id, "111111111111", "eu-south-1", tenant_id, "arbitration"),
                )
    finally:
        conn.close()


def _insert_resource(job_id: str, resource_id: str):
    conn = _connect()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO raw_resources (resource_id, job_id, account_id, region, resource_type) "
                    "VALUES (%s, %s::uuid, %s, %s, %s) ON CONFLICT DO NOTHING",
                    (resource_id, job_id, "111111111111", "eu-south-1", "AWS::EC2::Instance"),
                )
    finally:
        conn.close()


def _clean_job(job_id: str):
    conn = _connect()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM jobs WHERE job_id = %s::uuid", (job_id,))
    finally:
        conn.close()


def _clean_rules(tenant_id: str):
    conn = _connect()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM tenant_tagging_rules WHERE tenant_id = %s", (tenant_id,))
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

TENANT_ID = f"test-tenant-{uuid.uuid4().hex[:8]}"
RESOURCE_TYPE = "AWS::EC2::Instance"
TAG_KEY = "environment"


@pytest.fixture
def job_setup():
    job_id = str(uuid.uuid4())
    resource_id = f"arn:aws:ec2:eu-south-1:111111111111:instance/i-{uuid.uuid4().hex[:8]}"
    _insert_job(job_id, TENANT_ID)
    _insert_resource(job_id, resource_id)
    yield job_id, resource_id
    _clean_job(job_id)
    _clean_rules(TENANT_ID)


def _arb_payload(job_id: str, resource_id: str) -> dict:
    return {
        "job_id": job_id,
        "resource_id": resource_id,
        "tag_key": TAG_KEY,
        "context": {
            "resource_type": RESOURCE_TYPE,
            "document_excerpts": ["L'ambiente e' production."],
            "related_resources_tags": [],
        },
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_first_arbitration_calls_llm(job_setup):
    """Prima richiesta → LLM chiamato → regola salvata come 'proposed'."""
    job_id, resource_id = job_setup
    mock_llm = _MockLLM("production")

    import app.main as main_module
    main_module._llm_override = mock_llm

    async with AsyncClient(transport=ASGITransport(app=main_module.app), base_url="http://test") as client:
        resp = await client.post("/arbitrate", json=_arb_payload(job_id, resource_id))

    main_module._llm_override = None

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "resolved_by_llm"
    assert body["resolved_value"] == "production"
    assert body["rule_id"] is not None
    assert mock_llm.call_count == 1


@pytest.mark.asyncio
async def test_second_arbitration_uses_approved_rule(job_setup):
    """
    Criteri di accettazione Fase 5:
    Dopo approvazione della regola, la seconda richiesta identica
    viene risolta senza chiamata LLM.
    """
    job_id, resource_id = job_setup
    mock_llm = _MockLLM("production")

    import app.main as main_module
    main_module._llm_override = mock_llm

    async with AsyncClient(transport=ASGITransport(app=main_module.app), base_url="http://test") as client:
        # 1. Prima richiesta → LLM
        r1 = await client.post("/arbitrate", json=_arb_payload(job_id, resource_id))
        assert r1.json()["status"] == "resolved_by_llm"
        rule_id = r1.json()["rule_id"]
        assert mock_llm.call_count == 1

        # 2. Approva la regola
        r_approve = await client.post(f"/rules/{rule_id}/approve?approved_by=operator")
        assert r_approve.status_code == 200

        # 3. Seconda richiesta identica → regola approvata, ZERO chiamate LLM aggiuntive
        r2 = await client.post("/arbitrate", json=_arb_payload(job_id, resource_id))
        assert r2.status_code == 200
        body2 = r2.json()
        assert body2["status"] == "resolved_by_rule", f"Atteso 'resolved_by_rule', ottenuto: {body2}"
        assert body2["resolved_value"] == "production"
        assert body2["rule_id"] == rule_id

    main_module._llm_override = None

    # LLM chiamato UNA sola volta (dalla prima richiesta)
    assert mock_llm.call_count == 1, (
        f"LLM chiamato {mock_llm.call_count} volte (atteso 1 — la seconda deve usare la regola)"
    )


@pytest.mark.asyncio
async def test_list_rules(job_setup):
    """GET /rules ritorna le regole del tenant."""
    job_id, resource_id = job_setup
    mock_llm = _MockLLM("staging")

    import app.main as main_module
    main_module._llm_override = mock_llm

    async with AsyncClient(transport=ASGITransport(app=main_module.app), base_url="http://test") as client:
        await client.post("/arbitrate", json=_arb_payload(job_id, resource_id))
        resp = await client.get(f"/rules?tenant_id={TENANT_ID}")

    main_module._llm_override = None

    assert resp.status_code == 200
    rules = resp.json()
    assert len(rules) >= 1
    assert any(r["tag_key"] == TAG_KEY for r in rules)


@pytest.mark.asyncio
async def test_reject_rule(job_setup):
    """Una regola rifiutata non viene usata nella seconda richiesta (LLM ri-chiamato)."""
    job_id, resource_id = job_setup
    mock_llm = _MockLLM("production")

    import app.main as main_module
    main_module._llm_override = mock_llm

    async with AsyncClient(transport=ASGITransport(app=main_module.app), base_url="http://test") as client:
        r1 = await client.post("/arbitrate", json=_arb_payload(job_id, resource_id))
        rule_id = r1.json()["rule_id"]

        # Rifiuta la regola
        await client.post(f"/rules/{rule_id}/reject")

        # Seconda richiesta → LLM ri-chiamato (regola rejected)
        r2 = await client.post("/arbitrate", json=_arb_payload(job_id, resource_id))
        assert r2.json()["status"] == "resolved_by_llm"

    main_module._llm_override = None
    assert mock_llm.call_count == 2


@pytest.mark.asyncio
async def test_resource_types_crud(job_setup):
    """POST e GET /resource-types funzionano correttamente."""
    import app.main as main_module

    async with AsyncClient(transport=ASGITransport(app=main_module.app), base_url="http://test") as client:
        resp_post = await client.post("/resource-types", json={
            "tenant_id": TENANT_ID, "resource_type": "AWS::EC2::Instance",
            "is_mandatory": True, "reason": "Obbligatorio per FinOps",
        })
        assert resp_post.status_code == 201

        resp_get = await client.get(f"/resource-types?tenant_id={TENANT_ID}")
        assert resp_get.status_code == 200
        types = resp_get.json()
        assert any(t["resource_type"] == "AWS::EC2::Instance" for t in types)
