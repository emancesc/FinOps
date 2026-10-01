"""
Agent 1 — Resource Extractor  (porta 8001)
Endpoints:
  POST /extract/full
  POST /extract/resource-type/{resource_type}
  POST /extract/config-inventory
  GET  /extract/status/{task_id}
  GET  /health
"""
from __future__ import annotations
import logging
import os
from dotenv import load_dotenv
load_dotenv()
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from redis import Redis
from rq import Queue
from rq.job import Job, NoSuchJobError

logger = logging.getLogger(__name__)

app = FastAPI(title="FinOps Agent 1 — Resource Extractor", version="0.2.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class ExtractFullRequest(BaseModel):
    job_id: str
    account_id: str
    # "all" (default) = tutte le regioni abilitate; oppure "eu-south-1" o "a,b"
    region: str = "all"


class ExtractResourceTypeRequest(BaseModel):
    job_id: str
    account_id: str
    # "all" (default) = tutte le regioni abilitate; oppure "eu-south-1" o "a,b"
    region: str = "all"
    resource_type: Optional[str] = None


class ExtractConfigInventoryRequest(BaseModel):
    job_id: str
    account_id: str
    # "all" (default) = tutte le regioni abilitate; oppure "eu-south-1" o "a,b"
    region: str = "all"


class ExtractEvidenceRequest(BaseModel):
    job_id: str
    account_id: str
    # "all" (default) = tutte le regioni abilitate; oppure "eu-south-1" o "a,b"
    region: str = "all"
    output_dir: Optional[str] = None
    source_names: Optional[list[str]] = None
    resource_types: Optional[list[str]] = None


class TaskStatus(BaseModel):
    task_id: str
    status: str
    result: Optional[dict] = None
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Redis / RQ helpers
# ---------------------------------------------------------------------------

def _redis() -> Redis:
    return Redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))


def _queue() -> Queue:
    return Queue("extraction", connection=_redis())


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok", "service": "agent1_resource_extractor"}


@app.post("/extract/evidence", status_code=202)
async def extract_evidence(req: ExtractEvidenceRequest):
    """Genera un file JSON con le evidenze AWS per account/region (anche "all")."""
    from .evidence import collect_aws_evidence

    payload = collect_aws_evidence(
        account_id=req.account_id,
        region=req.region,
        output_dir=req.output_dir,
        source_names=req.source_names,
        resource_types=req.resource_types,
        filename=f"aws_evidence_{req.job_id}.json",
    )

    return {
        "job_id": req.job_id,
        "status": "generated",
        "file_path": payload["file_path"],
        "records": len(payload["records"]),
        "regions": payload.get("regions", [payload.get("region")]),
    }


@app.post("/extract/full", status_code=202)
async def extract_full(req: ExtractFullRequest):
    """Accoda l'estrazione completa; ritorna immediatamente il task_id."""
    q = _queue()
    rq_job = q.enqueue(
        "app.worker.run_extraction",
        req.job_id,
        req.account_id,
        req.region,
        job_timeout=-1,
    )
    logger.info("Extraction enqueued: task_id=%s job_id=%s", rq_job.id, req.job_id)
    return {"task_id": rq_job.id, "status": "queued"}


@app.post("/extract/resource-type/{resource_type}")
async def extract_resource_type(resource_type: str, req: ExtractResourceTypeRequest):
    """
    Estrazione sincrona per un singolo resource type.
    Ritorna la lista di risorse normalizzate e le persiste in raw_resources.
    """
    import asyncio
    from .aws_client import AWSClient, AccountMismatchError
    from .db import upsert_resources

    assume_role_arn = os.environ.get("AWS_ASSUME_ROLE_ARN") or None

    def _run():
        client = AWSClient(
            account_id=req.account_id,
            region=req.region,
            assume_role_arn=assume_role_arn,
        )
        client.verify_account()
        resources = client.list_resources([resource_type])
        upsert_resources(req.job_id, resources)
        return [r.model_dump() for r in resources]

    try:
        result = await asyncio.to_thread(_run)
    except AccountMismatchError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:
        logger.exception("Errore estrazione %s", resource_type)
        raise HTTPException(status_code=500, detail=str(exc))

    return {
        "resource_type": resource_type,
        "count": len(result),
        "regions": sorted({r["region"] for r in result}),
        "resources": result,
    }


@app.post("/extract/config-inventory")
async def extract_config_inventory(req: ExtractConfigInventoryRequest):
    """
    Estrazione sincrona di TUTTE le risorse instanziate nell'account, partendo
    dal servizio AWS Config (resourceType scoperti dinamicamente, non solo i
    6 di DEFAULT_RESOURCE_TYPES). Ogni risorsa include le relazioni native di
    Config e, in attributes["tagging_analysis"], il confronto con la CINECA
    Tagging Strategy (tag mandatory/operativi mancanti + suggerimenti).
    Persiste in raw_resources come le altre estrazioni.
    """
    import asyncio
    from .aws_client import AWSClient, AccountMismatchError
    from .db import upsert_resources

    assume_role_arn = os.environ.get("AWS_ASSUME_ROLE_ARN") or None

    def _run():
        client = AWSClient(
            account_id=req.account_id,
            region=req.region,
            assume_role_arn=assume_role_arn,
        )
        client.verify_account()
        resources = client.list_all_resources_from_config()
        upsert_resources(req.job_id, resources)
        return [r.model_dump() for r in resources]

    try:
        result = await asyncio.to_thread(_run)
    except AccountMismatchError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:
        logger.exception("Errore estrazione config-inventory")
        raise HTTPException(status_code=500, detail=str(exc))

    by_type: dict[str, int] = {}
    non_compliant = 0
    for r in result:
        by_type[r["resource_type"]] = by_type.get(r["resource_type"], 0) + 1
        if not r["attributes"].get("tagging_analysis", {}).get("compliant", True):
            non_compliant += 1

    return {
        "count": len(result),
        "resource_types": by_type,
        "non_compliant_mandatory_tags": non_compliant,
        "regions": sorted({r["region"] for r in result}),
        "resources": result,
    }


@app.get("/extract/status/{task_id}", response_model=TaskStatus)
async def extract_status(task_id: str):
    """Ritorna lo stato del task RQ corrispondente."""
    r = _redis()
    try:
        job = Job.fetch(task_id, connection=r)
    except NoSuchJobError:
        raise HTTPException(status_code=404, detail=f"Task {task_id} non trovato")

    status_map = {
        "queued":   "queued",
        "started":  "running",
        "finished": "finished",
        "failed":   "failed",
        "deferred": "deferred",
        "stopped":  "stopped",
    }
    status = status_map.get(job.get_status().value if hasattr(job.get_status(), "value") else str(job.get_status()), "unknown")

    result = None
    error = None
    if status == "finished":
        result = job.result if isinstance(job.result, dict) else {"raw": str(job.result)}
    elif status == "failed":
        error = str(job.exc_info)[-500:] if job.exc_info else "unknown error"

    return TaskStatus(task_id=task_id, status=status, result=result, error=error)
