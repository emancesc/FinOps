"""
Test Fase 6 — Knowledge Graph Builder.

Acceptance criteria:
  1. Dopo il build, GET /graph/resource/{arn} di una EC2 di test
     restituisce il sottografo con VPC, Subnet, SecurityGroup collegati.
  2. GET /graph/group?dimension=business-unit&value=BU-Engineering
     restituisce TUTTE le risorse taggate con quella BU,
     indipendentemente dalla relazione architetturale.
  3. GET /graph/group?dimension=resource_type&value=AWS::EC2::Instance
     naviga per attributo (non tag) e restituisce le EC2.
  4. GET /graph/search?q=eu-south-1 trova risorse per regione.

Prerequisiti runtime:
  - Neo4j raggiungibile a NEO4J_URI con credenziali NEO4J_USER/NEO4J_PASSWORD
  - PostgreSQL con DATABASE_URL (usato dal DB layer — mock nel test)
  - Le env var sono lette dall'ambiente; il test usa un client Neo4j reale
    ma bypassa il DB Postgres con dati iniettati direttamente.
"""
from __future__ import annotations

import os
import uuid
import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

# Variabili d'ambiente minime richieste
os.environ.setdefault("NEO4J_URI", "bolt://localhost:7687")
os.environ.setdefault("NEO4J_USER", "neo4j")
os.environ.setdefault("NEO4J_PASSWORD", "changeme")
os.environ.setdefault("DATABASE_URL", "postgresql://finops:changeme@localhost:5432/finops")

from app.neo4j_client import Neo4jClient
import app.main as main_module


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

JOB_ID = str(uuid.uuid4())
ACCOUNT = "111111111111"
REGION = "eu-south-1"
TENANT = "tenant-test"

ARN_EC2    = f"arn:aws:ec2:{REGION}:{ACCOUNT}:instance/i-{uuid.uuid4().hex[:8]}"
ARN_VPC    = f"arn:aws:ec2:{REGION}:{ACCOUNT}:vpc/vpc-{uuid.uuid4().hex[:8]}"
ARN_SUBNET = f"arn:aws:ec2:{REGION}:{ACCOUNT}:subnet/subnet-{uuid.uuid4().hex[:8]}"
ARN_SG     = f"arn:aws:ec2:{REGION}:{ACCOUNT}:security-group/sg-{uuid.uuid4().hex[:8]}"

RESOURCES = [
    {
        "resource_id": ARN_EC2,
        "job_id": JOB_ID,
        "account_id": ACCOUNT,
        "region": REGION,
        "resource_type": "AWS::EC2::Instance",
        "attributes": {"instance_type": "t3.medium", "vpc_id": ARN_VPC},
        "tags": {},
    },
    {
        "resource_id": ARN_VPC,
        "job_id": JOB_ID,
        "account_id": ACCOUNT,
        "region": REGION,
        "resource_type": "AWS::EC2::VPC",
        "attributes": {"cidr_block": "10.0.0.0/16"},
        "tags": {},
    },
    {
        "resource_id": ARN_SUBNET,
        "job_id": JOB_ID,
        "account_id": ACCOUNT,
        "region": REGION,
        "resource_type": "AWS::EC2::Subnet",
        "attributes": {"vpc_id": ARN_VPC, "cidr_block": "10.0.1.0/24"},
        "tags": {},
    },
    {
        "resource_id": ARN_SG,
        "job_id": JOB_ID,
        "account_id": ACCOUNT,
        "region": REGION,
        "resource_type": "AWS::EC2::SecurityGroup",
        "attributes": {"vpc_id": ARN_VPC},
        "tags": {},
    },
]

RELATIONSHIPS = [
    {"source_id": ARN_EC2,    "target_id": ARN_VPC,    "relationship_type": "CONTAINS"},
    {"source_id": ARN_EC2,    "target_id": ARN_SUBNET,  "relationship_type": "CONTAINS"},
    {"source_id": ARN_EC2,    "target_id": ARN_SG,      "relationship_type": "SECURED_BY"},
    {"source_id": ARN_SUBNET, "target_id": ARN_VPC,     "relationship_type": "CONTAINS"},
    {"source_id": ARN_SG,     "target_id": ARN_VPC,     "relationship_type": "CONTAINS"},
]

BU_VALUE = "BU-Engineering"
TAG_PROPOSALS = [
    {"resource_id": ARN_EC2,    "tag_key": "business-unit", "proposed_value": BU_VALUE},
    {"resource_id": ARN_VPC,    "tag_key": "business-unit", "proposed_value": BU_VALUE},
    {"resource_id": ARN_SUBNET, "tag_key": "business-unit", "proposed_value": BU_VALUE},
    {"resource_id": ARN_SG,     "tag_key": "business-unit", "proposed_value": BU_VALUE},
    {"resource_id": ARN_EC2,    "tag_key": "environment",   "proposed_value": "production"},
]


@pytest.fixture(scope="module")
def neo4j_client():
    """Client Neo4j reale per il modulo di test."""
    client = Neo4jClient(
        os.environ["NEO4J_URI"],
        os.environ["NEO4J_USER"],
        os.environ["NEO4J_PASSWORD"],
    )
    client.ensure_indexes()
    yield client
    # Pulizia: rimuove i nodi creati dal test
    with client._driver.session() as s:
        s.run(
            "MATCH (r:Resource) WHERE r.job_id = $jid DETACH DELETE r",
            jid=JOB_ID,
        )
        s.run("MATCH (d:BusinessUnit {name: $v}) DETACH DELETE d", v=BU_VALUE)
        s.run("MATCH (e:Environment {name: 'production'}) DETACH DELETE e")
    client.close()


@pytest.fixture(scope="module", autouse=True)
def build_graph(neo4j_client):
    """Esegue il graph build una sola volta per l'intero modulo."""
    stats = neo4j_client.build_graph(RESOURCES, RELATIONSHIPS, TAG_PROPOSALS, TENANT)
    assert stats["nodes_written"] == 4
    assert stats["arch_rels_written"] == 5
    assert stats["tag_rels_written"] >= 4   # almeno le 4 BU + 1 environment


# ---------------------------------------------------------------------------
# Classe mock Neo4j per endpoint HTTP (usa lo stesso client reale)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def app_client(neo4j_client):
    main_module._neo4j_override = neo4j_client
    yield
    main_module._neo4j_override = None


# ---------------------------------------------------------------------------
# Test 1: Sottografo EC2 include VPC, Subnet, SG
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resource_subgraph_includes_vpc_subnet_sg(neo4j_client):
    """
    Acceptance criteria 1: il sottografo della EC2 contiene
    VPC, Subnet e SecurityGroup collegati architetturalmente.
    """
    result = neo4j_client.get_resource_subgraph(ARN_EC2, depth=2)

    node_arns = {n["arn"] for n in result["nodes"] if "arn" in n}
    assert ARN_EC2    in node_arns, "EC2 non trovata nel sottografo"
    assert ARN_VPC    in node_arns, "VPC non trovata nel sottografo"
    assert ARN_SUBNET in node_arns, "Subnet non trovata nel sottografo"
    assert ARN_SG     in node_arns, "SecurityGroup non trovato nel sottografo"

    edge_types = {e["type"] for e in result["edges"]}
    assert "CONTAINS"    in edge_types
    assert "SECURED_BY"  in edge_types


# ---------------------------------------------------------------------------
# Test 2: group per business-unit (navigazione tag)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_group_by_business_unit(neo4j_client):
    """
    Acceptance criteria 2: tutte le 4 risorse taggate BU-Engineering
    sono restituite indipendentemente dalle relazioni architetturali.
    """
    results = neo4j_client.get_by_dimension("business-unit", BU_VALUE)
    returned_arns = {r["arn"] for r in results}

    assert ARN_EC2    in returned_arns, "EC2 non presente nel gruppo BU"
    assert ARN_VPC    in returned_arns, "VPC non presente nel gruppo BU"
    assert ARN_SUBNET in returned_arns, "Subnet non presente nel gruppo BU"
    assert ARN_SG     in returned_arns, "SG non presente nel gruppo BU"


# ---------------------------------------------------------------------------
# Test 3: group per resource_type (navigazione attributo)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_group_by_resource_type_attribute(neo4j_client):
    """
    Acceptance criteria 3: la navigazione per attributo resource_type
    restituisce le EC2 del test (senza relazione tag).
    """
    results = neo4j_client.get_by_dimension("resource_type", "AWS::EC2::Instance")
    returned_arns = {r["arn"] for r in results}
    assert ARN_EC2 in returned_arns, "EC2 non trovata tramite navigazione per resource_type"


# ---------------------------------------------------------------------------
# Test 4: group per region (navigazione attributo)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_group_by_region_attribute(neo4j_client):
    results = neo4j_client.get_by_dimension("region", REGION)
    returned_arns = {r["arn"] for r in results}
    for arn in [ARN_EC2, ARN_VPC, ARN_SUBNET, ARN_SG]:
        assert arn in returned_arns, f"{arn} non trovato navigando per region"


# ---------------------------------------------------------------------------
# Test 5: search fulltext per regione
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_search_by_region(neo4j_client):
    """
    Acceptance criteria 4: la ricerca per stringa di regione trova le risorse.
    """
    results = neo4j_client.search("eu-south-1")
    returned_arns = {r["arn"] for r in results}
    assert ARN_EC2 in returned_arns, "EC2 non trovata dalla ricerca fulltext per regione"


# ---------------------------------------------------------------------------
# Test 6: search per tag value (environment=production)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_search_by_tag_value(neo4j_client):
    results = neo4j_client.search("production")
    returned_arns = {r["arn"] for r in results}
    assert ARN_EC2 in returned_arns, "EC2 non trovata cercando il tag value 'production'"


# ---------------------------------------------------------------------------
# Test 7: whitelist relazioni — tipo non ammesso non crea relazione
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_arch_rel_whitelist_blocks_invalid(neo4j_client):
    """Un tipo di relazione non nella whitelist viene ignorato senza eccezioni."""
    with neo4j_client._driver.session() as s:
        s.execute_write(
            neo4j_client.upsert_arch_rel,
            ARN_EC2,
            "MALICIOUS_REL_TYPE",
            ARN_VPC,
        )
        result = s.run(
            "MATCH (a:Resource {arn: $a})-[r:MALICIOUS_REL_TYPE]->(b:Resource {arn: $b}) RETURN r",
            a=ARN_EC2, b=ARN_VPC,
        )
        assert result.single() is None, "Relazione non nella whitelist non dovrebbe esistere"


# ---------------------------------------------------------------------------
# Test 8: endpoint HTTP /graph/build (mock db layer)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_http_build_endpoint(neo4j_client, app_client, monkeypatch):
    """
    POST /graph/build con mock del layer Postgres:
    verifica che l'endpoint risponda 200 e ritorni stats coerenti.
    """
    import app.db as db_module

    job_id2 = str(uuid.uuid4())
    arn_ec2b = f"arn:aws:ec2:{REGION}:{ACCOUNT}:instance/i-{uuid.uuid4().hex[:8]}"

    def fake_resources(jid):
        return [{
            "resource_id": arn_ec2b,
            "job_id": jid,
            "account_id": ACCOUNT,
            "region": REGION,
            "resource_type": "AWS::EC2::Instance",
            "attributes": {},
            "tags": {},
        }]

    def fake_rels(jid):
        return []

    def fake_proposals(jid):
        return [{"resource_id": arn_ec2b, "tag_key": "environment", "proposed_value": "staging"}]

    def fake_tenant(jid):
        return TENANT

    monkeypatch.setattr(db_module, "load_resources_for_job", fake_resources)
    monkeypatch.setattr(db_module, "load_relationships_for_job", fake_rels)
    monkeypatch.setattr(db_module, "load_approved_proposals_for_job", fake_proposals)
    monkeypatch.setattr(db_module, "get_tenant_id_for_job", fake_tenant)

    async with AsyncClient(transport=ASGITransport(app=main_module.app), base_url="http://test") as client:
        resp = await client.post("/graph/build", json={"job_id": job_id2, "tenant_id": TENANT})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["nodes_written"] == 1
    assert body["tag_rels_written"] >= 1

    # Cleanup
    with neo4j_client._driver.session() as s:
        s.run("MATCH (r:Resource {arn: $a}) DETACH DELETE r", a=arn_ec2b)
        s.run("MATCH (e:Environment {name: 'staging'}) DETACH DELETE e")
