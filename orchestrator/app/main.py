from __future__ import annotations
import asyncio
import logging
import os
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Header
from fastapi.middleware.cors import CORSMiddleware

from .db import get_pool, close_pool, _row_to_dict
from .models import JobCreateRequest, JobResponse, AdvanceResponse
from .state_machine import advance as sm_advance, fail as sm_fail

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
logger = logging.getLogger(__name__)

_INTERNAL_TOKEN = os.environ.get("INTERNAL_SERVICE_TOKEN", "changeme")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await get_pool()
    logger.info("Pool DB pronto.")
    yield
    await close_pool()


app = FastAPI(title="FinOps Orchestrator", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok", "service": "orchestrator"}


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

@app.post("/jobs", status_code=201)
async def create_job(req: JobCreateRequest) -> dict[str, Any]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        job_id = await conn.fetchval(
            """INSERT INTO jobs (account_id, region, tenant_id, phase, progress_pct, status_detail)
               VALUES ($1, $2, $3, 'created', 0, 'Job creato, in attesa di avvio')
               RETURNING job_id""",
            req.account_id, req.region, req.tenant_id,
        )
        # Inserisci metadati documenti se forniti
        for doc in req.documents:
            await conn.execute(
                """INSERT INTO documents (job_id, doc_type, file_name, storage_path)
                   VALUES ($1, $2, $3, $4)""",
                job_id, doc.doc_type, doc.file_name, f"pending/{doc.file_name}",
            )

    logger.info("Job %s creato (account=%s, tenant=%s)", job_id, req.account_id, req.tenant_id)
    return {"job_id": str(job_id)}


@app.get("/jobs")
async def list_jobs() -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT 100"
        )
    return [_row_to_dict(r) for r in rows]


@app.get("/jobs/{job_id}")
async def get_job(job_id: str) -> dict:
    pool = await get_pool()
    try:
        uid = uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(400, "job_id non valido")
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM jobs WHERE job_id = $1", uid)
    if not row:
        raise HTTPException(404, f"Job {job_id} non trovato")
    return _row_to_dict(row)


@app.post("/jobs/{job_id}/advance")
async def advance_job(job_id: str) -> AdvanceResponse:
    """
    Chiamato dai worker RQ al termine di ogni fase per avanzare la state machine.
    Può essere chiamato anche manualmente per test.
    """
    try:
        result = await asyncio.to_thread(sm_advance, job_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    except Exception as exc:
        logger.error("Errore in advance per job %s: %s", job_id, exc)
        raise HTTPException(500, str(exc))
    return AdvanceResponse(**result)


@app.post("/jobs/{job_id}/fail")
async def fail_job(job_id: str, body: dict) -> dict:
    """Marca il job come fallito — chiamato dai worker in caso di errore."""
    error = body.get("error", "Errore sconosciuto")
    await asyncio.to_thread(sm_fail, job_id, error)
    return {"job_id": job_id, "phase": "failed"}


# ---------------------------------------------------------------------------
# WebSocket — push stato job ogni 2s
# ---------------------------------------------------------------------------

@app.websocket("/jobs/{job_id}/ws")
async def job_websocket(websocket: WebSocket, job_id: str) -> None:
    await websocket.accept()
    pool = await get_pool()
    try:
        uid = uuid.UUID(job_id)
    except ValueError:
        await websocket.close(code=1008)
        return

    try:
        while True:
            async with pool.acquire() as conn:
                row = await conn.fetchrow("SELECT * FROM jobs WHERE job_id = $1", uid)
            if row:
                await websocket.send_json(_row_to_dict(row))
            else:
                await websocket.send_json({"error": "job non trovato"})
            await asyncio.sleep(2)
    except WebSocketDisconnect:
        logger.info("WebSocket disconnesso per job %s", job_id)
    except Exception as exc:
        logger.error("Errore WebSocket job %s: %s", job_id, exc)
