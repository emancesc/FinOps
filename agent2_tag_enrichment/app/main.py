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
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from redis import Redis
from rq import Queue
from rq.job import Job, NoSuchJobError

logger = logging.getLogger(__name__)
app = FastAPI(title="FinOps Agent 2 — Tag Enrichment", version="0.2.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


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


# ---------------------------------------------------------------------------
# Documenti di progetto (Design / Assessment) e proposta di tagging
# ---------------------------------------------------------------------------

from fastapi import File, Form, Query, UploadFile  # noqa: E402

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_DOC_UPLOAD_DIR = os.environ.get("DOCUMENT_UPLOAD_DIR", os.path.join(_REPO, "uploads", "documents"))
_DOC_TYPES = {"HLD", "LLD", "PRE_MIGRATION_ASSESSMENT", "MIGRATION_RESOURCE_LIST", "OTHER"}
_proposal_tasks: dict[str, asyncio.Task] = {}
_llm_override = None  # usato dai test per iniettare un LLM finto


@app.on_event("startup")
async def _mark_interrupted_runs():
    """Run di proposta rimasti 'running'/'queued' dopo un riavvio: segnati come interrotti (riprendibili)."""
    from . import proposal_db as pdb
    try:
        await asyncio.to_thread(pdb.mark_interrupted_runs)
    except Exception as exc:  # DB non raggiungibile all'avvio: non blocca il servizio
        logger.warning("Impossibile verificare i run interrotti: %s", exc)


class GenerateRequest(BaseModel):
    job_id: str
    strategy_id: Optional[str] = None       # default: strategy attiva
    regions: Optional[list[str]] = None
    resource_types: Optional[list[str]] = None
    max_resources: Optional[int] = None


class ProposalReview(BaseModel):
    review_status: Optional[str] = None     # approved | rejected | pending
    tag_value: Optional[str] = None         # valorizzato -> review_status 'edited'
    reviewed_by: Optional[str] = "operator"


@app.post("/documents/{job_id}", status_code=201)
async def upload_document(job_id: str, file: UploadFile = File(...), doc_type: str = Form(default="OTHER")):
    """Carica un documento di Design/Assessment del job e ne indicizza il testo."""
    import uuid
    from . import proposal_db as pdb
    from .ingestion import _chunk, _parse_docx, _parse_pdf, _parse_text, _parse_xlsx

    if doc_type not in _DOC_TYPES:
        raise HTTPException(status_code=422, detail=f"doc_type non valido: {sorted(_DOC_TYPES)}")
    if not await asyncio.to_thread(pdb.get_job, job_id):
        raise HTTPException(status_code=404, detail=f"Job {job_id} non trovato")
    target = os.path.join(_DOC_UPLOAD_DIR, job_id)
    os.makedirs(target, exist_ok=True)
    safe_name = os.path.basename(file.filename or "documento")
    path = os.path.join(target, f"{uuid.uuid4().hex[:8]}_{safe_name}")
    with open(path, "wb") as out:
        out.write(await file.read())

    def _index():
        ext = os.path.splitext(path)[1].lower()
        parser = {".pdf": _parse_pdf, ".docx": _parse_docx, ".xlsx": _parse_xlsx}.get(ext, _parse_text)
        text = parser(path)
        doc = pdb.insert_document(job_id, doc_type, safe_name, path)
        chunks = _chunk(text, size=1500, overlap=200)
        pdb.save_text_chunks(doc["document_id"], chunks, {"doc_type": doc_type, "source": safe_name})
        doc.update(chunks=len(chunks), chars=len(text))
        return doc

    try:
        return await asyncio.to_thread(_index)
    except Exception as exc:
        logger.exception("Indicizzazione documento %s fallita", safe_name)
        raise HTTPException(status_code=422, detail=f"Documento non leggibile: {exc}")


@app.get("/documents/{job_id}")
async def get_documents(job_id: str):
    from . import proposal_db as pdb
    return await asyncio.to_thread(pdb.list_documents, job_id)


@app.delete("/documents/item/{document_id}")
async def remove_document(document_id: str):
    from . import proposal_db as pdb
    path = await asyncio.to_thread(pdb.delete_document, document_id)
    if path is None:
        raise HTTPException(status_code=404, detail="Documento non trovato")
    if path and os.path.exists(path):
        os.remove(path)
    return {"document_id": document_id, "deleted": True}


def _readiness(job_id: str) -> dict:
    from . import proposal_db as pdb
    from .linked_evidence import linked_status

    job = pdb.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} non trovato")
    resources = pdb.count_resources(job_id)
    linked = linked_status(job["account_id"])
    strategy = pdb.get_strategy(None)
    documents = pdb.list_documents(job_id)
    missing = []
    if not resources:
        missing.append("Inventario non estratto: eseguire l'estrazione (pagina Inventario) per il job")
    if not linked["ready"]:
        missing.append(f"JSON a corredo assenti o incompleti per l'account {job['account_id']}: "
                       "eseguire scripts/extract_linked_resources.py")
    if not strategy:
        missing.append("Nessuna Tagging Strategy attiva (pagina Regole)")
    elif strategy["status"] != "extracted":
        missing.append("La Tagging Strategy attiva non ha completato l'estrazione delle regole")
    return {
        "job": job,
        "inventory": {"resources": resources, "ok": resources > 0},
        "linked_json": {**linked, "ok": linked["ready"]},
        "strategy": ({"strategy_id": strategy["strategy_id"], "name": strategy["name"], "revision": strategy["revision"],
                      "release_date": strategy["release_date"], "tags": len(strategy["tags"]),
                      "rules": len(strategy["rules"]), "ok": strategy["status"] == "extracted"} if strategy else {"ok": False}),
        "documents": {"count": len(documents), "chars": sum(int(d.get("chars") or 0) for d in documents)},
        "estimate": {"resources": resources, "llm_calls": 1 if resources else 0},
        "running": pdb.running_run(job_id),
        "ready": not missing,
        "missing": missing,
    }


@app.get("/proposals/readiness")
async def proposals_readiness(job_id: str = Query(...)):
    """Prerequisiti della proposta: inventario, JSON a corredo, strategy attiva (+ documenti facoltativi)."""
    return await asyncio.to_thread(_readiness, job_id)


def _start_run(run_id: str) -> None:
    from .proposal import run_proposals

    task = _proposal_tasks.get(run_id)
    if task and not task.done():
        return
    _proposal_tasks[run_id] = asyncio.create_task(run_proposals(run_id, llm_client=_llm_override))


@app.post("/proposals/generate", status_code=202)
async def generate_proposals(req: GenerateRequest):
    """Avvia la generazione della proposta (disponibile solo con tutti i prerequisiti)."""
    from . import proposal_db as pdb

    readiness = await asyncio.to_thread(_readiness, req.job_id)
    if not readiness["ready"]:
        raise HTTPException(status_code=409, detail={"message": "Prerequisiti mancanti", "missing": readiness["missing"]})
    if readiness["running"]:
        raise HTTPException(status_code=409, detail="Una generazione è già in corso per questo job")
    strategy_id = req.strategy_id or readiness["strategy"]["strategy_id"]
    options = {k: v for k, v in req.model_dump().items() if k in ("regions", "resource_types", "max_resources") and v}
    run = await asyncio.to_thread(pdb.create_run, req.job_id, strategy_id, options, readiness["inventory"]["resources"])
    _start_run(run["run_id"])
    return run


@app.get("/proposals/runs")
async def proposal_runs(job_id: str = Query(...)):
    from . import proposal_db as pdb
    return await asyncio.to_thread(pdb.list_runs, job_id)


@app.get("/proposals/runs/{run_id}")
async def proposal_run(run_id: str):
    from . import proposal_db as pdb
    run = await asyncio.to_thread(pdb.get_run, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run non trovato")
    task = _proposal_tasks.get(run_id)
    run["task_running"] = bool(task and not task.done())
    return run


@app.post("/proposals/runs/{run_id}/resume", status_code=202)
async def resume_run(run_id: str):
    """Riprende un run interrotto dalla prima risorsa non elaborata."""
    from . import proposal_db as pdb
    run = await asyncio.to_thread(pdb.get_run, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run non trovato")
    if run["status"] == "done":
        raise HTTPException(status_code=409, detail="Run già completato")
    await asyncio.to_thread(pdb.update_run, run_id, status="queued", error=None)
    _start_run(run_id)
    return {"run_id": run_id, "status": "queued"}


@app.post("/proposals/runs/{run_id}/cancel")
async def cancel_run(run_id: str):
    """Ferma un run in corso; le proposte già salvate restano e il run si può riprendere."""
    from . import proposal_db as pdb
    run = await asyncio.to_thread(pdb.get_run, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run non trovato")
    task = _proposal_tasks.get(run_id)
    if task and not task.done():
        task.cancel()
    if run["status"] in ("queued", "running"):
        await asyncio.to_thread(pdb.update_run, run_id, status="cancelled", finished=True,
                                message="Interrotto dall'operatore: usare Riprendi per continuare")
    return await asyncio.to_thread(pdb.get_run, run_id)


@app.get("/proposals")
async def get_proposals(job_id: str = Query(...), run_id: Optional[str] = None, review_status: Optional[str] = None,
                        tag_key: Optional[str] = None, limit: int = 5000, offset: int = 0):
    from . import proposal_db as pdb
    return await asyncio.to_thread(pdb.list_proposals, job_id, run_id, review_status, tag_key, limit, offset)


@app.patch("/proposals/{proposal_id}")
async def patch_proposal(proposal_id: str, req: ProposalReview):
    from . import proposal_db as pdb
    if req.tag_value is None and req.review_status not in ("approved", "rejected", "pending"):
        raise HTTPException(status_code=422, detail="review_status deve essere approved, rejected o pending")
    n = await asyncio.to_thread(pdb.review_proposal, proposal_id, req.review_status, req.tag_value, req.reviewed_by)
    if not n:
        raise HTTPException(status_code=404, detail="Proposta non trovata")
    return {"id": proposal_id, "review_status": "edited" if req.tag_value is not None else req.review_status}
