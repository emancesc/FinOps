"""
Proposta di tagging: prerequisiti (inventario + JSON a corredo + strategy attiva), documenti
Design/Assessment, ereditarietà volume -> istanza, LLM a batch con validazione dei valori
ammessi, persistenza per batch, ripresa dopo errore e revisione delle proposte.
Richiede il PostgreSQL locale con le migrazioni applicate (scripts/apply_migrations.py).
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid

import psycopg2
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("DATABASE_URL", "postgresql://finops:changeme@localhost:5432/finops")

from llm_gateway.base import LLMClient, LLMResponse  # noqa: E402

ACCOUNT = "123456789012"
REGION = "eu-south-1"
INSTANCE = f"arn:aws:ec2:{REGION}:{ACCOUNT}:instance/i-web01"
VOLUME = f"arn:aws:ec2:{REGION}:{ACCOUNT}:volume/vol-data01"
QUEUE = f"arn:aws:sqs:{REGION}:{ACCOUNT}:esse3-queue"
INSTANCE_TAGS = {"Name": "esse3-web-01", "cineca:BusinessUnit": "UNIV", "cineca:Customer": "UNIBO",
                 "cineca:Product": "ESSE3", "cineca:Environment": "PROD", "cineca:Service": "Tomcat"}


class _FakeLLM(LLMClient):
    _model = "fake"

    def __init__(self, fail_calls: int = 0):
        self.requests = []
        self.fail_calls = fail_calls

    async def complete(self, system, messages, response_format=None, max_tokens=4096):
        payload = json.loads(messages[-1].content.split("\n", 1)[1])
        self.requests.append({"system": system, "payload": payload})
        if self.fail_calls:
            self.fail_calls -= 1
            raise RuntimeError("LLM non disponibile")
        out = []
        for res in payload["resources"]:
            props = []
            for t in res["tags_to_propose"]:
                value = {"cineca:Role": "Compute-Application", "cineca:Customer": "UNIBO+NONESISTE",
                         "cineca:Environment": "PROD", "cineca:BusinessUnit": "UNIV", "cineca:Product": "ESSE3",
                         "cineca:Service": "Tomcat"}.get(t["tag_key"])
                props.append({"tag_key": t["tag_key"], "value": value, "confidence": 0.8,
                              "reasoning": "dedotto dal nome", "source_ref": "strategy §3"})
            out.append({"resource_id": res["resource_id"], "proposals": props})
        return LLMResponse(content=json.dumps({"resources": out}), model="fake", input_tokens=1000, output_tokens=200)


def _conn():
    return psycopg2.connect(os.environ["DATABASE_URL"])


def _tag(key, category, values, mandatory=True, billing=True, multi=False):
    return (key, category, mandatory, billing, multi, "+" if multi else None,
            json.dumps([{"value": v} for v in values]))


@pytest.fixture
def world(tmp_path, monkeypatch):
    """Job con 3 risorse, JSON a corredo in una cartella temporanea, strategy attiva di test."""
    import app.linked_evidence as le
    monkeypatch.setattr(le, "EXTRACTED_ROOT", str(tmp_path / "extracted"))
    job_id = str(uuid.uuid4())
    conn = _conn()
    with conn, conn.cursor() as cur:
        cur.execute("SELECT strategy_id FROM tagging_strategies WHERE is_active")
        previous_active = cur.fetchone()
        cur.execute("UPDATE tagging_strategies SET is_active = false WHERE is_active")
        cur.execute("INSERT INTO jobs (job_id, account_id, region, tenant_id, phase) VALUES (%s, %s, 'all', 'test', 'created')",
                    (job_id, ACCOUNT))
        cur.execute("""INSERT INTO tagging_strategies (name, revision, release_date, file_name, storage_path, status, is_active)
                       VALUES ('Test Strategy', %s, '2026-09-25', 'x.pdf', 'x.pdf', 'extracted', true) RETURNING strategy_id""",
                    (f"t-{job_id[:8]}",))
        strategy_id = str(cur.fetchone()[0])
        for row in (_tag("cineca:BusinessUnit", "cost_allocation", ["UNIV", "MIPA"]),
                    _tag("cineca:Customer", "cost_allocation", ["UNIBO", "POLIMI", "shared"], multi=True),
                    _tag("cineca:Product", "cost_allocation", ["ESSE3", "shared"], multi=True),
                    _tag("cineca:Environment", "cost_allocation", ["PROD", "PREPROD", "PROD+PREPROD"]),
                    _tag("cineca:Service", "operational", ["Tomcat", "Oracle"], mandatory=False, billing=False),
                    _tag("cineca:Role", "operational", ["Compute-Application", "Storage-Volume"], mandatory=False, billing=False)):
            cur.execute("""INSERT INTO strategy_tags (strategy_id, tag_key, category, mandatory, billing, multi_value,
                                                      separator, allowed_values) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                        (strategy_id, *row))
        cur.execute("""INSERT INTO strategy_rules (strategy_id, rule_type, title, description)
                       VALUES (%s, 'guideline', 'EBS mantengono il Service', 'I volumi EBS mantengono cineca:Service del workload')""",
                    (strategy_id,))
    conn.close()
    yield {"job_id": job_id, "strategy_id": strategy_id, "root": tmp_path / "extracted"}
    conn = _conn()
    with conn, conn.cursor() as cur:
        cur.execute("DELETE FROM jobs WHERE job_id = %s", (job_id,))
        cur.execute("DELETE FROM proposal_runs WHERE strategy_id = %s", (strategy_id,))
        cur.execute("DELETE FROM tagging_strategies WHERE strategy_id = %s", (strategy_id,))
        if previous_active:
            cur.execute("UPDATE tagging_strategies SET is_active = true WHERE strategy_id = %s", (previous_active[0],))
    conn.close()


def _add_inventory(job_id):
    conn = _conn()
    with conn, conn.cursor() as cur:
        for arn, rtype, tags in ((INSTANCE, "AWS::EC2::Instance", {"Name": "esse3-web-01"}),
                                 (VOLUME, "AWS::EC2::Volume", {"Name": "esse3-data"}),
                                 (QUEUE, "AWS::SQS::Queue", {})):
            cur.execute("""INSERT INTO raw_resources (resource_id, job_id, account_id, region, resource_type, current_tags, attributes)
                           VALUES (%s, %s, %s, %s, %s, %s, '{}') ON CONFLICT (resource_id) DO UPDATE SET job_id = EXCLUDED.job_id,
                           current_tags = EXCLUDED.current_tags""", (arn, job_id, ACCOUNT, REGION, rtype, json.dumps(tags)))
    conn.close()


def _add_linked_json(root):
    region_dir = root / ACCOUNT / REGION
    region_dir.mkdir(parents=True)
    tags = [{"Key": k, "Value": v} for k, v in INSTANCE_TAGS.items()]
    (region_dir / "instances_all.json").write_text(json.dumps([
        {"InstanceId": "i-web01", "Name": "esse3-web-01", "State": "running", "Tags": tags}]), encoding="utf-8")
    (region_dir / "volumes_all.json").write_text(json.dumps([
        {"VolumeId": "vol-data01", "Attachments": [{"InstanceId": "i-web01"}], "Tags": []}]), encoding="utf-8")
    (region_dir / "eni_attachments.json").write_text("[]", encoding="utf-8")
    (region_dir / "acm_inuseby_full.json").write_text("[]", encoding="utf-8")
    (root / ACCOUNT / "_run.json").write_text(json.dumps({"finished_at": "2026-09-30T00:00:00+00:00"}), encoding="utf-8")


@pytest_asyncio.fixture
async def api():
    import app.main as main
    async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test") as c:
        yield c, main
    main._llm_override = None


async def _wait(main, run_id):
    task = main._proposal_tasks.get(run_id)
    if task:
        await asyncio.wait_for(task, timeout=30)


@pytest.mark.asyncio
async def test_readiness_gates_generation(api, world):
    c, main = api
    r = (await c.get("/proposals/readiness", params={"job_id": world["job_id"]})).json()
    assert r["ready"] is False and not r["inventory"]["ok"] and not r["linked_json"]["ok"]
    assert r["strategy"]["ok"] and r["strategy"]["revision"].startswith("t-")
    assert (await c.post("/proposals/generate", json={"job_id": world["job_id"]})).status_code == 409

    _add_inventory(world["job_id"])
    r = (await c.get("/proposals/readiness", params={"job_id": world["job_id"]})).json()
    assert r["inventory"]["resources"] == 3 and not r["ready"]  # mancano ancora i JSON a corredo
    assert any("JSON a corredo" in m for m in r["missing"])

    _add_linked_json(world["root"])
    r = (await c.get("/proposals/readiness", params={"job_id": world["job_id"]})).json()
    assert r["ready"] is True and r["linked_json"]["regions"] == [REGION]


@pytest.mark.asyncio
async def test_generate_inherit_validate_review(api, world, tmp_path):
    c, main = api
    llm = _FakeLLM()
    main._llm_override = llm
    _add_inventory(world["job_id"])
    _add_linked_json(world["root"])

    doc = tmp_path / "LLD_esse3.txt"
    doc.write_text("Architettura ESSE3: il frontend esse3-web-01 serve UNIBO in produzione.", encoding="utf-8")
    with open(doc, "rb") as f:
        r = await c.post(f"/documents/{world['job_id']}", files={"file": ("LLD_esse3.txt", f, "text/plain")},
                         data={"doc_type": "LLD"})
    assert r.status_code == 201 and r.json()["chunks"] == 1
    assert (await c.get(f"/documents/{world['job_id']}")).json()[0]["doc_type"] == "LLD"

    run = (await c.post("/proposals/generate", json={"job_id": world["job_id"]})).json()
    await _wait(main, run["run_id"])
    run = (await c.get(f"/proposals/runs/{run['run_id']}")).json()
    assert run["status"] == "done" and run["progress_pct"] == 100.0 and run["resources_done"] == 3
    assert run["llm_calls"] == 1 and run["input_tokens"] == 1000

    # system prompt con la strategy; estratto del documento LLD passato per l'istanza
    sent = llm.requests[0]
    assert "cineca:Customer" in sent["system"] and "UNIBO; POLIMI; shared" in sent["system"]
    by_id = {r["resource_id"]: r for r in sent["payload"]["resources"]}
    assert "LLD_esse3.txt" in by_id[INSTANCE]["document_excerpts"][0]
    # il volume eredita tutti i tag di billing e il Service dall'istanza: all'LLM chiede solo il Role
    assert [t["tag_key"] for t in by_id[VOLUME]["tags_to_propose"]] == ["cineca:Role"]
    assert by_id[VOLUME]["evidence"]["attached_instances"][0]["name"] == "esse3-web-01"

    proposals = (await c.get("/proposals", params={"job_id": world["job_id"]})).json()
    vol = {p["tag_key"]: p for p in proposals if p["resource_id"] == VOLUME}
    assert vol["cineca:Customer"]["tag_value"] == "UNIBO" and vol["cineca:Customer"]["source_type"] == "inheritance"
    assert vol["cineca:Service"]["tag_value"] == "Tomcat"
    inst = {p["tag_key"]: p for p in proposals if p["resource_id"] == INSTANCE}
    bad = inst["cineca:Customer"]
    assert bad["reasoning"].startswith("[VALORE NON AMMESSO") and float(bad["confidence"]) <= 0.2
    assert inst["cineca:Role"]["tag_value"] == "Compute-Application" and inst["cineca:Role"]["source_type"] == "llm"

    r = await c.patch(f"/proposals/{bad['id']}", json={"tag_value": "UNIBO"})
    assert r.json()["review_status"] == "edited"
    r = await c.patch(f"/proposals/{inst['cineca:Role']['id']}", json={"review_status": "approved"})
    assert r.status_code == 200
    approved = (await c.get("/proposals", params={"job_id": world["job_id"], "review_status": "approved"})).json()
    assert [p["tag_key"] for p in approved] == ["cineca:Role"]


@pytest.mark.asyncio
async def test_failed_batch_then_resume(api, world, monkeypatch):
    c, main = api
    import app.proposal as proposal
    monkeypatch.setattr(proposal, "BATCH_SIZE", 1)
    llm = _FakeLLM()
    main._llm_override = llm
    _add_inventory(world["job_id"])
    _add_linked_json(world["root"])

    calls = {"n": 0}
    original = llm.complete

    async def flaky(*args, **kwargs):  # fallisce la seconda chiamata LLM
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("timeout")
        return await original(*args, **kwargs)

    llm.complete = flaky
    run = (await c.post("/proposals/generate", json={"job_id": world["job_id"]})).json()
    await _wait(main, run["run_id"])
    run = (await c.get(f"/proposals/runs/{run['run_id']}")).json()
    assert run["status"] == "error" and "batch risorse" in run["error"]
    done_before = run["resources_done"]
    assert 0 < done_before < 3

    r = await c.post(f"/proposals/runs/{run['run_id']}/resume")
    assert r.status_code == 202
    await _wait(main, run["run_id"])
    run = (await c.get(f"/proposals/runs/{run['run_id']}")).json()
    assert run["status"] == "done" and run["resources_done"] == 3
    # le risorse già elaborate prima dell'errore non vengono ripassate all'LLM
    seen = [r["resource_id"] for req in llm.requests for r in req["payload"]["resources"]]
    assert len(seen) == len(set(seen))
