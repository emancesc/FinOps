"""
Fase del job derivata dal flusso interattivo (app/workflow.py): inventario → proposta → revisione
→ grafo. Richiede il PostgreSQL locale con le migrazioni applicate (scripts/apply_migrations.py);
crea job, strategy e risorse di test e li rimuove alla fine.
"""
from __future__ import annotations

import os
import uuid

import asyncpg
import pytest
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("DATABASE_URL", "postgresql://finops:changeme@localhost:5432/finops")

ACCOUNT = "123456789012"


@pytest.mark.asyncio
async def test_phase_follows_inventory_proposal_review_graph(tmp_path, monkeypatch):
    import app.main as main
    import app.workflow as workflow
    from app.db import close_pool

    monkeypatch.setattr(workflow, "EXTRACTED_ROOT", str(tmp_path))  # nessun JSON a corredo
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    job_id, cancelled_id = uuid.uuid4(), uuid.uuid4()
    strategy_id = await conn.fetchval(
        """INSERT INTO tagging_strategies (name, revision, file_name, storage_path, status, is_active)
           VALUES ('Workflow test', $1, 'x.pdf', 'x.pdf', 'extracted', false) RETURNING strategy_id""",
        f"wf-{job_id.hex[:8]}")
    arn = f"arn:aws:sqs:eu-south-1:{ACCOUNT}:wf-{job_id.hex[:8]}"
    try:
        for jid, phase in ((job_id, "created"), (cancelled_id, "failed")):
            await conn.execute("""INSERT INTO jobs (job_id, account_id, region, tenant_id, phase, status_detail)
                                  VALUES ($1, $2, 'all', 'test', $3, 'x')""", jid, ACCOUNT, phase)
        async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test") as c:
            async def state(jid=job_id):
                job = (await c.get(f"/jobs/{jid}")).json()
                return job["phase"], job["progress_pct"], job["workflow_managed"]

            # nessun passo fatto: il job resta della pipeline
            assert await state() == ("created", 0, False)

            await conn.execute("""INSERT INTO raw_resources (resource_id, job_id, account_id, region, resource_type)
                                  VALUES ($1, $2, $3, 'eu-south-1', 'AWS::SQS::Queue')""", arn, job_id, ACCOUNT)
            assert await state() == ("extraction", 25, True)
            # la pipeline non può più avanzare un job gestito dal flusso
            assert (await c.post(f"/jobs/{job_id}/advance")).status_code == 409

            run_id = await conn.fetchval("""INSERT INTO proposal_runs (job_id, strategy_id, status, message)
                                            VALUES ($1, $2, 'running', 'in corso') RETURNING run_id""", job_id, strategy_id)
            assert (await state())[0] == "enrichment"

            await conn.execute("UPDATE proposal_runs SET status = 'done', message = 'Completato' WHERE run_id = $1", run_id)
            for key in ("cineca:Customer", "cineca:Product"):
                await conn.execute("""INSERT INTO tag_proposals (job_id, account_id, resource_id, tag_key, tag_value,
                                                                 confidence, source_type, run_id)
                                      VALUES ($1, $2, $3, $4, 'X', 0.8, 'llm', $5)""", job_id, ACCOUNT, arn, key, run_id)
            assert await state() == ("arbitration", 60, True)

            await conn.execute("UPDATE tag_proposals SET review_status = 'approved', updated_at = now() WHERE job_id = $1",
                               job_id)
            assert await state() == ("graph_build", 85, True)

            await conn.execute("""UPDATE jobs SET graph_built_at = now(),
                                  graph_stats = '{"nodes_written": 1, "tag_rels_written": 2}' WHERE job_id = $1""", job_id)
            wf = (await c.get(f"/jobs/{job_id}/workflow")).json()
            assert (wf["phase"], wf["progress_pct"], wf["next_step"]) == ("completed", 100, "linked_json")
            assert {s["key"]: s["status"] for s in wf["steps"]} == {
                "inventory": "done", "linked_json": "todo", "documents": "optional", "proposal": "done",
                "review": "done", "graph": "done"}

            # una revisione successiva alla costruzione del grafo: grafo da ricostruire
            await conn.execute("""UPDATE tag_proposals SET review_status = 'edited', updated_at = now() + interval '1 second'
                                  WHERE job_id = $1 AND tag_key = 'cineca:Product'""", job_id)
            assert (await state())[0] == "graph_build"

            # un job annullato non viene toccato
            assert await state(cancelled_id) == ("failed", 0, False)
            listed = {j["job_id"]: j for j in (await c.get("/jobs")).json()}
            assert listed[str(job_id)]["phase"] == "graph_build" and listed[str(cancelled_id)]["phase"] == "failed"
    finally:
        await conn.execute("DELETE FROM jobs WHERE job_id = ANY($1::uuid[])", [job_id, cancelled_id])
        await conn.execute("DELETE FROM tagging_strategies WHERE strategy_id = $1", strategy_id)
        await conn.close()
        await close_pool()
