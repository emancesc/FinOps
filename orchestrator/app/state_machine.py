"""
State machine sincrona: usata sia dall'endpoint FastAPI (via asyncio.to_thread)
sia dai worker RQ (contesto sync). Usa psycopg2 per semplicità.
"""
from __future__ import annotations
import logging
import os
import psycopg2
import psycopg2.extras
from redis import Redis
from rq import Queue

logger = logging.getLogger(__name__)

PHASE_ORDER = ["created", "extraction", "enrichment", "arbitration", "graph_build", "completed", "failed"]

PHASE_PROGRESS: dict[str, int] = {
    "created":    0,
    "extraction": 10,
    "enrichment": 40,
    "arbitration": 65,
    "graph_build": 85,
    "completed":  100,
    "failed":     0,
}

PHASE_DETAIL: dict[str, str] = {
    "extraction":  "Estrazione risorse AWS in corso",
    "enrichment":  "Valorizzazione tag in corso",
    "arbitration": "Arbitraggio casi incerti in corso",
    "graph_build": "Costruzione knowledge graph in corso",
    "completed":   "Pipeline completata con successo",
}

# Mappa fase → nome coda RQ e task da accodare
PHASE_TASK: dict[str, tuple[str, str]] = {
    "extraction":  ("extraction",  "app.tasks.run_extraction"),
    "enrichment":  ("enrichment",  "app.tasks.run_enrichment"),
    "arbitration": ("arbitration", "app.tasks.run_arbitration"),
    "graph_build": ("graph_build", "app.tasks.run_graph_build"),
}


def _connect() -> psycopg2.extensions.connection:
    return psycopg2.connect(os.environ["DATABASE_URL"])


def advance(job_id: str) -> dict:
    """
    Legge la fase corrente del job, avanza alla successiva, aggiorna DB,
    accoda il task RQ corrispondente.
    Ritorna un dict con previous_phase, current_phase, progress_pct, queued.
    """
    conn = _connect()
    try:
        with conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT job_id, phase, progress_pct FROM jobs WHERE job_id = %s FOR UPDATE",
                    (job_id,),
                )
                row = cur.fetchone()
                if not row:
                    raise ValueError(f"Job {job_id} non trovato")

                previous_phase = row["phase"]
                if previous_phase in ("completed", "failed"):
                    return {
                        "job_id": job_id,
                        "previous_phase": previous_phase,
                        "current_phase": previous_phase,
                        "progress_pct": row["progress_pct"],
                        "queued": False,
                    }

                idx = PHASE_ORDER.index(previous_phase)
                next_phase = PHASE_ORDER[idx + 1]
                progress = PHASE_PROGRESS.get(next_phase, 0)
                detail = PHASE_DETAIL.get(next_phase, "")

                cur.execute(
                    """UPDATE jobs
                       SET phase = %s, progress_pct = %s, status_detail = %s, updated_at = now()
                       WHERE job_id = %s""",
                    (next_phase, progress, detail, job_id),
                )
                logger.info("Job %s: %s → %s", job_id, previous_phase, next_phase)
    finally:
        conn.close()

    # Accoda task RQ se la nuova fase prevede un worker
    queued = False
    if next_phase in PHASE_TASK:
        queue_name, task_path = PHASE_TASK[next_phase]
        redis = Redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
        q = Queue(queue_name, connection=redis)
        # job_id è riservato da RQ → passiamo il finops_job_id come argomento posizionale.
        # job_timeout=-1 disabilita il death penalty (usa SIGALRM, non disponibile su Windows).
        q.enqueue(task_path, job_id, job_timeout=-1)
        logger.info("Job %s: task '%s' accodato su coda '%s'", job_id, task_path, queue_name)
        queued = True

    return {
        "job_id": job_id,
        "previous_phase": previous_phase,
        "current_phase": next_phase,
        "progress_pct": progress,
        "queued": queued,
    }


def fail(job_id: str, error: str) -> None:
    """Imposta il job in stato 'failed' con messaggio di errore."""
    conn = _connect()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE jobs
                       SET phase = 'failed', error_message = %s, updated_at = now()
                       WHERE job_id = %s""",
                    (error[:2000], job_id),
                )
    finally:
        conn.close()
    logger.error("Job %s: fallito — %s", job_id, error)
