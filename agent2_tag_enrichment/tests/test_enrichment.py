"""
Test integrazione Fase 4:
- ingestion di un documento fixture (testo inline)
- enrichment con LLM mockato
- verifica >= 80% proposte per risorse "facili"
"""
from __future__ import annotations
import asyncio
import json
import os
import tempfile
import uuid
import pytest
import numpy as np

os.environ.setdefault("DATABASE_URL", "postgresql://finops:changeme@localhost:5432/finops")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from llm_gateway.base import LLMClient, LLMMessage, LLMResponse


# ---------------------------------------------------------------------------
# Documento fixture
# ---------------------------------------------------------------------------

FIXTURE_DOC = """
Documento: HLD Migration Project Alpha
Data: 2024-01-15

Sezione 1 - Infrastruttura
Il progetto utilizza istanze EC2 nell'ambiente production.
L'istanza web-01 (AWS::EC2::Instance) appartiene al team platform.
Il cost-center di riferimento è CC-100.
L'applicazione si chiama webapp-frontend.
Business unit: BU-Engineering.

Sezione 2 - Database
L'istanza db-01 (AWS::EC2::Instance) è gestita dal team data-engineering.
Cost-center: CC-200. Ambiente: production. Applicazione: db-core.
Business unit: BU-Engineering.

Sezione 3 - Storage
Il bucket S3 logs-bucket appartiene al team platform, cost-center CC-100.
Ambiente: production. Applicazione: logging.
"""

# -----------------------------------------------------------------------
# Mock LLM che legge il contenuto del documento e propone tag
# -----------------------------------------------------------------------

class _MockLLMClient(LLMClient):
    """
    LLM mock: propone tag based on keyword matching del documento fixture.
    """
    def __init__(self):
        self.call_count = 0

    async def complete(self, system, messages, response_format=None, max_tokens=4096):
        self.call_count += 1
        content = messages[-1].content if messages else ""

        # Estrai resource_id dalla richiesta
        resource_id = ""
        for line in content.split("\n"):
            if "resource_id:" in line:
                resource_id = line.split("resource_id:", 1)[-1].strip()
                break

        proposals = []
        # Logica di mock: se l'estratto contiene le keyword → proponi tag con alta confidence
        excerpts = content.lower()

        # environment
        if "production" in excerpts or "prod" in excerpts:
            proposals.append({"tag_key": "environment", "value": "production", "confidence": 0.92, "source_ref": "HLD#sezione1", "reasoning": "Esplicitamente menzionato"})

        # team
        if "platform" in excerpts and ("web-01" in resource_id or "logs-bucket" in resource_id or "s3" in resource_id.lower()):
            proposals.append({"tag_key": "team", "value": "platform", "confidence": 0.88, "source_ref": "HLD#sezione1", "reasoning": "Menzionato nella sezione"})
        elif "data-engineering" in excerpts:
            proposals.append({"tag_key": "team", "value": "data-engineering", "confidence": 0.88, "source_ref": "HLD#sezione2", "reasoning": "Menzionato nella sezione"})
        elif "platform" in excerpts:
            proposals.append({"tag_key": "team", "value": "platform", "confidence": 0.85, "source_ref": "HLD#sezione1", "reasoning": "Menzionato nel documento"})

        # cost-center
        if "cc-100" in excerpts:
            proposals.append({"tag_key": "cost-center", "value": "CC-100", "confidence": 0.95, "source_ref": "HLD#sezione1", "reasoning": "Valore esplicito"})
        elif "cc-200" in excerpts:
            proposals.append({"tag_key": "cost-center", "value": "CC-200", "confidence": 0.95, "source_ref": "HLD#sezione2", "reasoning": "Valore esplicito"})

        # application
        if "webapp-frontend" in excerpts:
            proposals.append({"tag_key": "application", "value": "webapp-frontend", "confidence": 0.90, "source_ref": "HLD#sezione1", "reasoning": "Menzionato"})
        elif "db-core" in excerpts:
            proposals.append({"tag_key": "application", "value": "db-core", "confidence": 0.90, "source_ref": "HLD#sezione2", "reasoning": "Menzionato"})
        elif "logging" in excerpts:
            proposals.append({"tag_key": "application", "value": "logging", "confidence": 0.87, "source_ref": "HLD#sezione3", "reasoning": "Menzionato"})

        payload = json.dumps({"resource_id": resource_id, "proposals": proposals})
        return LLMResponse(content=payload, model="mock", input_tokens=100, output_tokens=50)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _clean_job(job_id: str):
    """Rimuove tutti i dati del job dal DB (cascade)."""
    import psycopg2
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM jobs WHERE job_id = %s::uuid", (job_id,))
    finally:
        conn.close()


def _insert_job(job_id: str):
    import psycopg2
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO jobs (job_id, account_id, region, tenant_id, phase) "
                    "VALUES (%s::uuid, %s, %s, %s, %s)",
                    (job_id, "123456789012", "eu-south-1", "test-tenant", "enrichment")
                )
    finally:
        conn.close()


def _insert_resources(job_id: str, resources: list[dict]):
    import psycopg2, psycopg2.extras
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        with conn:
            with conn.cursor() as cur:
                rows = [
                    (r["resource_id"], job_id, r["account_id"], r["region"],
                     r["resource_type"], json.dumps(r.get("current_tags", {})),
                     json.dumps(r.get("attributes", {})), json.dumps(r.get("relationships", [])))
                    for r in resources
                ]
                psycopg2.extras.execute_values(
                    cur,
                    "INSERT INTO raw_resources (resource_id, job_id, account_id, region, resource_type, current_tags, attributes, relationships) VALUES %s",
                    rows
                )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Risorse di test (5 "facili" — info esplicita nel documento)
# ---------------------------------------------------------------------------

ACCOUNT = "123456789012"
REGION = "eu-south-1"

TEST_RESOURCES = [
    {
        "resource_id": f"arn:aws:ec2:{REGION}:{ACCOUNT}:instance/i-web01",
        "account_id": ACCOUNT, "region": REGION,
        "resource_type": "AWS::EC2::Instance",
        "current_tags": {}, "attributes": {"instance_type": "t3.medium"}, "relationships": [],
    },
    {
        "resource_id": f"arn:aws:ec2:{REGION}:{ACCOUNT}:instance/i-db01",
        "account_id": ACCOUNT, "region": REGION,
        "resource_type": "AWS::EC2::Instance",
        "current_tags": {}, "attributes": {"instance_type": "r5.large"}, "relationships": [],
    },
    {
        "resource_id": f"arn:aws:s3:::logs-bucket",
        "account_id": ACCOUNT, "region": REGION,
        "resource_type": "AWS::S3::Bucket",
        "current_tags": {}, "attributes": {}, "relationships": [],
    },
    {
        "resource_id": f"arn:aws:ec2:{REGION}:{ACCOUNT}:vpc/vpc-test01",
        "account_id": ACCOUNT, "region": REGION,
        "resource_type": "AWS::EC2::VPC",
        "current_tags": {}, "attributes": {"cidr_block": "10.0.0.0/16"}, "relationships": [],
    },
    {
        "resource_id": f"arn:aws:ec2:{REGION}:{ACCOUNT}:subnet/subnet-test01",
        "account_id": ACCOUNT, "region": REGION,
        "resource_type": "AWS::EC2::Subnet",
        "current_tags": {}, "attributes": {"cidr_block": "10.0.1.0/24"}, "relationships": [],
    },
]


@pytest.fixture
def job_with_data():
    """Crea job, risorse e documento di test; pulisce alla fine."""
    job_id = str(uuid.uuid4())
    doc_id = str(uuid.uuid4())

    _insert_job(job_id)
    _insert_resources(job_id, TEST_RESOURCES)

    # Scrivi fixture doc su file temporaneo e fai ingestion
    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False, mode="w", encoding="utf-8") as f:
        f.write(FIXTURE_DOC)
        tmp_path = f.name

    from app.ingestion import ingest

    # Usa embed_fn deterministica (quella di default)
    ingest(job_id, doc_id, tmp_path, "HLD")

    os.unlink(tmp_path)

    yield job_id, doc_id

    _clean_job(job_id)


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_enrichment_80_percent(job_with_data):
    """L'enrichment produce proposte per >= 80% delle risorse facili."""
    job_id, _ = job_with_data
    llm = _MockLLMClient()

    from app.enrichment import enrich_job
    summary = await enrich_job(job_id, llm_client=llm)

    assert summary["resources_processed"] == len(TEST_RESOURCES)
    rate = summary["resources_with_proposals"] / summary["resources_processed"]
    assert rate >= 0.8, f"Tasso proposte {rate:.0%} < 80%"


@pytest.mark.asyncio
async def test_proposals_have_confidence_and_source(job_with_data):
    """Ogni proposta ha confidence > 0 e source_ref valorizzato."""
    job_id, _ = job_with_data
    import psycopg2, psycopg2.extras
    conn = psycopg2.connect(os.environ["DATABASE_URL"])

    from app.enrichment import enrich_job
    await enrich_job(job_id, llm_client=_MockLLMClient())

    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM tag_proposals WHERE job_id = %s::uuid",
                (job_id,)
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    assert len(rows) > 0, "Nessuna proposta salvata"
    for row in rows:
        assert float(row["confidence"]) > 0, f"confidence=0 per {row['resource_id']}/{row['tag_key']}"
        assert row["source_ref"] is not None, f"source_ref mancante per {row['resource_id']}/{row['tag_key']}"


@pytest.mark.asyncio
async def test_enrichment_idempotent(job_with_data):
    """Eseguire enrichment due volte non duplica le proposte (ON CONFLICT)."""
    job_id, _ = job_with_data

    from app.enrichment import enrich_job
    s1 = await enrich_job(job_id, llm_client=_MockLLMClient())
    s2 = await enrich_job(job_id, llm_client=_MockLLMClient())

    import psycopg2
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM tag_proposals WHERE job_id = %s::uuid", (job_id,))
            count = cur.fetchone()[0]
    finally:
        conn.close()

    assert count == s1["proposals_saved"], "ON CONFLICT non ha deduplicato correttamente"
