"""
Fase 7 — Task RQ reali per l'orchestratore.
Ogni task chiama il rispettivo agente HTTP, aspetta il completamento,
poi avanza la state machine. In caso di errore marca il job 'failed'.
"""
from __future__ import annotations

import logging
import os
import time

import psycopg2
import psycopg2.extras
import requests

logger = logging.getLogger(__name__)

_ORCHESTRATOR_URL = os.environ.get("ORCHESTRATOR_URL", "http://localhost:8000")
_AGENT1_URL       = os.environ.get("AGENT1_URL",       "http://localhost:8001")
_AGENT2_URL       = os.environ.get("AGENT2_URL",       "http://localhost:8002")
_AGENT3_URL       = os.environ.get("AGENT3_URL",       "http://localhost:8003")
_AGENT4_URL       = os.environ.get("AGENT4_URL",       "http://localhost:8004")

_POLL_INTERVAL = int(os.environ.get("TASK_POLL_INTERVAL_S", "5"))
_POLL_TIMEOUT  = int(os.environ.get("TASK_POLL_TIMEOUT_S", "600"))

_CONFIDENCE_THRESHOLD = float(os.environ.get("ENRICHMENT_CONFIDENCE_THRESHOLD", "0.6"))


# ---------------------------------------------------------------------------
# Helper DB
# ---------------------------------------------------------------------------

def _connect():
    return psycopg2.connect(os.environ["DATABASE_URL"])


def _get_job_info(job_id: str) -> dict:
    """Legge account_id, region, tenant_id dal DB."""
    conn = _connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT account_id, region, tenant_id FROM jobs WHERE job_id = %s::uuid",
                (job_id,),
            )
            row = cur.fetchone()
            if not row:
                raise ValueError(f"Job {job_id} non trovato")
            return dict(row)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Helper state machine (chiamate verso l'orchestratore stesso)
# ---------------------------------------------------------------------------

def _advance(job_id: str) -> None:
    """Chiama POST /jobs/{job_id}/advance sull'orchestratore."""
    url = f"{_ORCHESTRATOR_URL}/jobs/{job_id}/advance"
    resp = requests.post(url, timeout=10)
    resp.raise_for_status()
    logger.info("Job %s: advance → %s", job_id, resp.json())


def _fail(job_id: str, error: str) -> None:
    """Chiama POST /jobs/{job_id}/fail sull'orchestratore."""
    url = f"{_ORCHESTRATOR_URL}/jobs/{job_id}/fail"
    try:
        requests.post(url, json={"error": error}, timeout=10)
    except Exception:
        pass
    logger.error("Job %s: fallito — %s", job_id, error[:300])


# ---------------------------------------------------------------------------
# Helper polling
# ---------------------------------------------------------------------------

def _poll_http_status(status_url: str) -> str:
    """
    GET su status_url finché status è 'finished' o 'failed'.
    Ritorna 'finished', 'failed', o 'timeout'.
    """
    deadline = time.monotonic() + _POLL_TIMEOUT
    while time.monotonic() < deadline:
        try:
            r = requests.get(status_url, timeout=10)
            if r.status_code == 200:
                status = r.json().get("status", "unknown")
                if status in ("finished", "failed"):
                    return status
        except Exception as exc:
            logger.warning("Polling %s: %s", status_url, exc)
        time.sleep(_POLL_INTERVAL)
    return "timeout"


def _wait_for_rq_job(task_id: str) -> None:
    """
    Polling diretto su Redis per aspettare un RQ job (task_id).
    Funzione separata per essere sostituibile nei test.
    """
    from redis import Redis
    from rq.job import Job as RQJob, NoSuchJobError

    redis_conn = Redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
    deadline = time.monotonic() + _POLL_TIMEOUT
    while time.monotonic() < deadline:
        try:
            rq_job = RQJob.fetch(task_id, connection=redis_conn)
            status_val = rq_job.get_status()
            status = status_val.value if hasattr(status_val, "value") else str(status_val)
            if status == "finished":
                return
            if status == "failed":
                raise RuntimeError(f"RQ job {task_id} fallito: {rq_job.exc_info}")
        except NoSuchJobError:
            pass  # job non ancora visibile in Redis, riprova
        time.sleep(_POLL_INTERVAL)
    raise RuntimeError(f"Timeout attesa RQ job {task_id} (>{_POLL_TIMEOUT}s)")


def _auto_approve_proposals(job_id: str) -> int:
    """Auto-approva tutte le tag_proposals pending per il job. Ritorna righe aggiornate."""
    conn = _connect()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE tag_proposals
                       SET review_status = 'approved', updated_at = now()
                       WHERE job_id = %s::uuid AND review_status = 'pending'""",
                    (job_id,),
                )
                return cur.rowcount
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Task RQ reali
# ---------------------------------------------------------------------------

def run_extraction(job_id: str) -> None:
    """
    Chiama agent1 POST /extract/full → polling GET /extract/status/{task_id}
    → avanza la state machine.
    """
    logger.info("[EXTRACTION] Job %s — avviato", job_id)
    try:
        info = _get_job_info(job_id)
        resp = requests.post(
            f"{_AGENT1_URL}/extract/full",
            json={"job_id": job_id, "account_id": info["account_id"], "region": info["region"]},
            timeout=30,
        )
        resp.raise_for_status()
        task_id = resp.json()["task_id"]
        logger.info("[EXTRACTION] Job %s: agent1 task_id=%s", job_id, task_id)

        status = _poll_http_status(f"{_AGENT1_URL}/extract/status/{task_id}")
        if status != "finished":
            raise RuntimeError(f"Extraction {status} per job {job_id}")

        logger.info("[EXTRACTION] Job %s — completato", job_id)
        _advance(job_id)
    except Exception as exc:
        _fail(job_id, f"extraction: {exc}")
        raise


def run_enrichment(job_id: str) -> None:
    """
    Chiama agent2 POST /enrich/run → attesa completamento RQ job via Redis
    → avanza la state machine.
    """
    logger.info("[ENRICHMENT] Job %s — avviato", job_id)
    try:
        resp = requests.post(
            f"{_AGENT2_URL}/enrich/run",
            json={"job_id": job_id},
            timeout=30,
        )
        resp.raise_for_status()
        task_id = resp.json()["task_id"]
        logger.info("[ENRICHMENT] Job %s: agent2 task_id=%s", job_id, task_id)

        # agent2 non ha endpoint /enrich/status/{task_id}: polling diretto su Redis
        _wait_for_rq_job(task_id)

        logger.info("[ENRICHMENT] Job %s — completato", job_id)
        _advance(job_id)
    except Exception as exc:
        _fail(job_id, f"enrichment: {exc}")
        raise


def run_arbitration(job_id: str) -> None:
    """
    Seconda passata di arbitration:
    1. Chiama agent3 per le proposals low-confidence ancora pending.
    2. Auto-approva tutte le proposals pending (già passate per LLM o agent3).
    3. Avanza la state machine.
    """
    logger.info("[ARBITRATION] Job %s — avviato", job_id)
    try:
        _arbitration_low_confidence_pass(job_id)
        approved = _auto_approve_proposals(job_id)
        logger.info("[ARBITRATION] Job %s — %d proposals auto-approvate", job_id, approved)
        _advance(job_id)
    except Exception as exc:
        _fail(job_id, f"arbitration: {exc}")
        raise


def _arbitration_low_confidence_pass(job_id: str) -> None:
    """Per ogni proposal pending con confidence < threshold, chiama agent3 /arbitrate."""
    conn = _connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT tp.resource_id, tp.tag_key, tp.confidence,
                       rr.resource_type
                FROM tag_proposals tp
                JOIN raw_resources rr
                  ON rr.resource_id = tp.resource_id
                 AND rr.job_id      = tp.job_id
                WHERE tp.job_id = %s::uuid
                  AND tp.review_status = 'pending'
                  AND tp.confidence < %s
                """,
                (job_id, _CONFIDENCE_THRESHOLD),
            )
            low_conf = cur.fetchall()
    finally:
        conn.close()

    for row in low_conf:
        payload = {
            "job_id": job_id,
            "resource_id": row["resource_id"],
            "tag_key": row["tag_key"],
            "context": {
                "resource_type": row["resource_type"],
                "document_excerpts": [],
                "related_resources_tags": [],
            },
        }
        try:
            r = requests.post(f"{_AGENT3_URL}/arbitrate", json=payload, timeout=30)
            if r.ok:
                resolved = r.json().get("resolved_value")
                if resolved:
                    _update_proposal_value(job_id, row["resource_id"], row["tag_key"], resolved)
        except Exception as exc:
            logger.warning("[ARBITRATION] %s/%s: %s", row["resource_id"], row["tag_key"], exc)


def _update_proposal_value(job_id: str, resource_id: str, tag_key: str, value: str) -> None:
    conn = _connect()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE tag_proposals
                       SET tag_value = %s, updated_at = now()
                       WHERE job_id = %s::uuid AND resource_id = %s AND tag_key = %s""",
                    (value, job_id, resource_id, tag_key),
                )
    finally:
        conn.close()


def run_graph_build(job_id: str) -> None:
    """
    Chiama agent4 POST /graph/build (risposta sincrona con stats) → avanza.
    """
    logger.info("[GRAPH_BUILD] Job %s — avviato", job_id)
    try:
        info = _get_job_info(job_id)
        resp = requests.post(
            f"{_AGENT4_URL}/graph/build",
            json={"job_id": job_id, "tenant_id": info["tenant_id"]},
            timeout=120,
        )
        resp.raise_for_status()
        stats = resp.json()
        logger.info("[GRAPH_BUILD] Job %s — %s", job_id, stats)
        _advance(job_id)
    except Exception as exc:
        _fail(job_id, f"graph_build: {exc}")
        raise
