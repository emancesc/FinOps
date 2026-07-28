"""
Agent 2 — Document Intelligence & Tag Enrichment (porta 8002)
POST /documents/ingest
POST /enrich/run
GET  /enrich/status/{job_id}
GET  /health
"""
from __future__ import annotations
import asyncio
import logging
from dotenv import load_dotenv
load_dotenv()
import os
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from redis import Redis
from rq import Queue
from rq.job import Job, NoSuchJobError

logger = logging.getLogger(__name__)
app = FastAPI(title="FinOps Agent 2 — Tag Enrichment", version="0.2.0")


class IngestRequest(BaseModel):
    job_id: str
    document_id: str
    storage_path: str
    doc_type: str


class EnrichRequest(BaseModel):
    job_id: str


def _redis() -> Redis:
    return Redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))


def _queue(name: str = "enrichment") -> Queue:
    return Queue(name, connection=_redis())


@app.get("/health")
async def health():
    return {"status": "ok", "service": "agent2_tag_enrichment"}


@app.post("/documents/ingest")
async def documents_ingest(req: IngestRequest):
    """Parsa, chunka, embeds e persiste il documento."""
    from .ingestion import ingest

    def _run():
        return ingest(req.job_id, req.document_id, req.storage_path, req.doc_type)

    try:
        chunk_count = await asyncio.to_thread(_run)
    except Exception as exc:
        logger.exception("Ingestion fallita per %s", req.document_id)
        raise HTTPException(status_code=500, detail=str(exc))

    return {"document_id": req.document_id, "chunks": chunk_count}


@app.post("/enrich/run", status_code=202)
async def enrich_run(req: EnrichRequest):
    """Accoda il task di enrichment, ritorna task_id."""
    q = _queue()
    rq_job = q.enqueue("app.worker.run_enrichment", req.job_id, job_timeout=-1)
    return {"task_id": rq_job.id, "status": "queued"}


@app.get("/enrich/status/{job_id}")
async def enrich_status(job_id: str):
    """Ritorna il conteggio di tag_proposals per il job."""
    def _count():
        from .db import _connect
        import psycopg2
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM tag_proposals WHERE job_id = %s::uuid",
                    (job_id,)
                )
                return cur.fetchone()[0]
        finally:
            conn.close()

    count = await asyncio.to_thread(_count)
    return {"job_id": job_id, "proposals_count": count}
