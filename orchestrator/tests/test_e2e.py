"""
Test Fase 7 — Integrazione end-to-end del pipeline FinOps.

Acceptance criteria:
  Un job avanzato attraverso tutte le fasi (extraction → enrichment →
  arbitration → graph_build) termina in stato 'completed'.
  In caso di errore di un agente, il job passa a 'failed'.

Strategia:
  - Le chiamate HTTP agli agenti (1-4) sono mockate con unittest.mock.
  - _advance e _fail in tasks.py vengono reindirizzati alle funzioni
    DB dirette (sm_advance / sm_fail) per evitare HTTP verso se stesso.
  - _wait_for_rq_job è sostituito con un no-op (il job RQ non esiste realmente).
  - Il DB Postgres è reale: viene creato un job di test e rimosso al termine.
"""
from __future__ import annotations

import os
import uuid
from unittest.mock import MagicMock, patch

import psycopg2
import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://finops:changeme@localhost:5432/finops")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from app.state_machine import advance as sm_advance, fail as sm_fail
import app.tasks as tasks


# ---------------------------------------------------------------------------
# Helpers DB
# ---------------------------------------------------------------------------

def _db():
    return psycopg2.connect(os.environ["DATABASE_URL"])


def _create_job(account_id="111111111111", region="eu-south-1", tenant_id="test-e2e") -> str:
    job_id = str(uuid.uuid4())
    conn = _db()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO jobs (job_id, account_id, region, tenant_id, phase, progress_pct)
                       VALUES (%s::uuid, %s, %s, %s, 'created', 0)""",
                    (job_id, account_id, region, tenant_id),
                )
    finally:
        conn.close()
    return job_id


def _get_phase(job_id: str) -> str:
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT phase FROM jobs WHERE job_id = %s::uuid", (job_id,))
            row = cur.fetchone()
            return row[0] if row else "not_found"
    finally:
        conn.close()


def _delete_job(job_id: str) -> None:
    conn = _db()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM jobs WHERE job_id = %s::uuid", (job_id,))
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def job_id():
    jid = _create_job()
    yield jid
    _delete_job(jid)


# ---------------------------------------------------------------------------
# Utilities mock
# ---------------------------------------------------------------------------

def _resp(json_data: dict, status: int = 200) -> MagicMock:
    """Crea un oggetto Response mock con .json() e .raise_for_status() configurati."""
    m = MagicMock()
    m.status_code = status
    m.ok = status < 400
    m.raise_for_status = MagicMock()
    m.json = MagicMock(return_value=json_data)
    return m


def _resp_error(status: int = 500) -> MagicMock:
    m = MagicMock()
    m.status_code = status
    m.ok = False
    m.raise_for_status = MagicMock(side_effect=Exception(f"HTTP {status}"))
    m.json = MagicMock(return_value={"detail": "error"})
    return m


def _post_side_effect(mapping: dict):
    """
    Factory: ritorna una side_effect per requests.post che seleziona
    la risposta in base al pattern nell'URL.
    """
    def _fn(url, **kwargs):
        for pattern, response in mapping.items():
            if pattern in url:
                return response
        return _resp({})
    return _fn


# ---------------------------------------------------------------------------
# Test 1: pipeline completa → completed
# ---------------------------------------------------------------------------

def test_full_pipeline_completes(job_id, monkeypatch):
    """
    Acceptance criteria principale:
    Un job percorre tutte le fasi senza errori e termina in 'completed'.
    """
    # Reindirizza _advance/_fail al DB diretto (bypassa HTTP verso se stesso)
    monkeypatch.setattr(tasks, "_advance", sm_advance)
    monkeypatch.setattr(tasks, "_fail", lambda jid, err: sm_fail(jid, err))
    # Sostituisce il polling Redis con no-op (il task RQ non esiste nel test)
    monkeypatch.setattr(tasks, "_wait_for_rq_job", lambda tid: None)

    agent_posts = {
        "extract/full":  _resp({"task_id": "ext-t1", "status": "queued"}, 202),
        "enrich/run":    _resp({"task_id": "enr-t1", "status": "queued"}, 202),
        "arbitrate":     _resp({"resolved_value": "production", "status": "resolved_by_llm", "rule_id": None}),
        "graph/build":   _resp({
            "job_id": job_id, "tenant_id": "test-e2e",
            "nodes_written": 0, "arch_rels_written": 0, "tag_rels_written": 0,
        }),
    }

    with patch("requests.post", side_effect=_post_side_effect(agent_posts)), \
         patch("requests.get", return_value=_resp({"status": "finished"})):

        # Avanza da 'created' → 'extraction' (enqueue task in state machine)
        sm_advance(job_id)
        assert _get_phase(job_id) == "extraction"

        tasks.run_extraction(job_id)
        assert _get_phase(job_id) == "enrichment"

        tasks.run_enrichment(job_id)
        assert _get_phase(job_id) == "arbitration"

        tasks.run_arbitration(job_id)
        assert _get_phase(job_id) == "graph_build"

        tasks.run_graph_build(job_id)
        assert _get_phase(job_id) == "completed", (
            f"Atteso 'completed', ottenuto '{_get_phase(job_id)}'"
        )


# ---------------------------------------------------------------------------
# Test 2: errore agent1 → 'failed'
# ---------------------------------------------------------------------------

def test_extraction_failure_marks_job_failed(job_id, monkeypatch):
    """Se agent1 risponde con HTTP 500, il job passa a 'failed'."""
    monkeypatch.setattr(tasks, "_advance", sm_advance)
    monkeypatch.setattr(tasks, "_fail", lambda jid, err: sm_fail(jid, err))

    sm_advance(job_id)  # created → extraction

    with patch("requests.post", return_value=_resp_error(500)):
        with pytest.raises(Exception):
            tasks.run_extraction(job_id)

    assert _get_phase(job_id) == "failed"


# ---------------------------------------------------------------------------
# Test 3: extraction timeout (polling sempre pending) → 'failed'
# ---------------------------------------------------------------------------

def test_extraction_timeout_marks_job_failed(job_id, monkeypatch):
    """Se il task agent1 non termina entro il timeout, il job viene marcato 'failed'."""
    monkeypatch.setattr(tasks, "_advance", sm_advance)
    monkeypatch.setattr(tasks, "_fail", lambda jid, err: sm_fail(jid, err))
    # Timeout cortissimo per il test
    monkeypatch.setattr(tasks, "_POLL_TIMEOUT", 0)
    monkeypatch.setattr(tasks, "_POLL_INTERVAL", 0)

    sm_advance(job_id)  # created → extraction

    agent_posts = {"extract/full": _resp({"task_id": "ext-timeout", "status": "queued"}, 202)}
    # Status risponde sempre "running" (mai "finished")
    with patch("requests.post", side_effect=_post_side_effect(agent_posts)), \
         patch("requests.get", return_value=_resp({"status": "running"})):
        with pytest.raises(Exception):
            tasks.run_extraction(job_id)

    assert _get_phase(job_id) == "failed"


# ---------------------------------------------------------------------------
# Test 4: errore graph_build → 'failed'
# ---------------------------------------------------------------------------

def test_graph_build_failure_marks_job_failed(job_id, monkeypatch):
    """Se agent4 risponde con errore, il job passa a 'failed' dalla fase graph_build."""
    monkeypatch.setattr(tasks, "_advance", sm_advance)
    monkeypatch.setattr(tasks, "_fail", lambda jid, err: sm_fail(jid, err))
    monkeypatch.setattr(tasks, "_wait_for_rq_job", lambda tid: None)

    sm_advance(job_id)  # → extraction

    ok_posts = {
        "extract/full": _resp({"task_id": "ext-t", "status": "queued"}, 202),
        "enrich/run":   _resp({"task_id": "enr-t", "status": "queued"}, 202),
    }

    def _post(url, **kwargs):
        if "graph/build" in url:
            return _resp_error(500)
        for pat, r in ok_posts.items():
            if pat in url:
                return r
        return _resp({})

    with patch("requests.post", side_effect=_post), \
         patch("requests.get", return_value=_resp({"status": "finished"})):

        tasks.run_extraction(job_id)   # → enrichment
        tasks.run_enrichment(job_id)   # → arbitration
        tasks.run_arbitration(job_id)  # → graph_build

        with pytest.raises(Exception):
            tasks.run_graph_build(job_id)

    assert _get_phase(job_id) == "failed"


# ---------------------------------------------------------------------------
# Test 5: advance su job già completed è idempotente
# ---------------------------------------------------------------------------

def test_advance_on_completed_is_noop(job_id):
    """Un ulteriore sm_advance su un job completed non cambia lo stato."""
    conn = _db()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE jobs SET phase = 'completed', progress_pct = 100 WHERE job_id = %s::uuid",
                    (job_id,),
                )
    finally:
        conn.close()

    result = sm_advance(job_id)
    assert result["current_phase"] == "completed"
    assert result["queued"] is False


# ---------------------------------------------------------------------------
# Test 6: auto-approve proposals durante arbitration
# ---------------------------------------------------------------------------

def test_arbitration_auto_approves_pending_proposals(job_id, monkeypatch):
    """
    La fase arbitration auto-approva le tag_proposals con review_status='pending'.
    Dopo arbitration, le proposals diventano 'approved'.
    """
    monkeypatch.setattr(tasks, "_advance", sm_advance)
    monkeypatch.setattr(tasks, "_fail", lambda jid, err: sm_fail(jid, err))

    # Inserisce una raw_resource e una proposal pending per il job
    resource_id = f"arn:aws:ec2:eu-south-1:111111111111:instance/i-{uuid.uuid4().hex[:8]}"
    conn = _db()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO raw_resources
                           (resource_id, job_id, account_id, region, resource_type)
                       VALUES (%s, %s::uuid, %s, %s, %s) ON CONFLICT DO NOTHING""",
                    (resource_id, job_id, "111111111111", "eu-south-1", "AWS::EC2::Instance"),
                )
                cur.execute(
                    """INSERT INTO tag_proposals
                           (job_id, resource_id, tag_key, tag_value, confidence, source_type)
                       VALUES (%s::uuid, %s, %s, %s, %s, %s)
                       ON CONFLICT DO NOTHING""",
                    (job_id, resource_id, "environment", "production", 0.9, "document"),
                )
    finally:
        conn.close()

    sm_advance(job_id)                # created → extraction
    sm_advance(job_id)                # extraction → enrichment
    sm_advance(job_id)                # enrichment → arbitration

    with patch("requests.post", return_value=_resp({})):
        tasks.run_arbitration(job_id)

    assert _get_phase(job_id) == "graph_build"

    # Verifica che la proposal sia stata approvata
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT review_status FROM tag_proposals WHERE job_id = %s::uuid AND resource_id = %s",
                (job_id, resource_id),
            )
            row = cur.fetchone()
    finally:
        conn.close()

    assert row is not None
    assert row[0] == "approved", f"Atteso 'approved', ottenuto '{row[0]}'"
