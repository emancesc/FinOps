"""
Agent 3 — Arbiter & Policy Registry  (porta 8003)
POST /arbitrate
GET  /rules?tenant_id=...
POST /rules/{rule_id}/approve
POST /rules/{rule_id}/reject
GET  /resource-types?tenant_id=...
POST /resource-types
GET  /health
"""
from __future__ import annotations
import asyncio
import json
from dotenv import load_dotenv
load_dotenv()
import logging
import os
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

logger = logging.getLogger(__name__)
app = FastAPI(title="FinOps Agent 3 — Arbiter", version="0.2.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ---------------------------------------------------------------------------
# Prompt templates (da spec 04-prompt-templates.md)
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "Sei l'arbitro delle politiche di tagging FinOps per un tenant specifico. "
    "Ricevi un caso in cui il valore di un tag non e' stato determinato con sufficiente "
    "confidenza. Il tuo compito e': "
    "1. proporre il valore piu' ragionevole per il tag, coerente con la tagging strategy "
    "e con pattern osservati in risorse simili; "
    "2. formulare una REGOLA GENERALE riutilizzabile (non specifica di questa singola "
    "risorsa) che permetta di risolvere automaticamente casi analoghi in futuro. "
    "Se non hai elementi sufficienti, restituisci \"resolved_value\": null.\n\n"
    "Rispondi SOLO con un oggetto JSON conforme esattamente a questo schema:\n"
    '{"resolved_value": "<str o null>", "confidence": <0-1>, "reasoning": "<frase>", '
    '"proposed_rule": {"tag_key": "<str>", '
    '"condition": {"resource_type": "<str o null>", "description": "<str>"}, '
    '"resolution": {"strategy": "<str>", "detail": "<str>"}}}'
)

_USER_TEMPLATE = (
    "Tenant: {tenant_id}\n"
    "Tag da arbitrare: {tag_key}\n"
    "Risorsa: {resource_id} (tipo: {resource_type})\n\n"
    "Contesto:\n"
    "- Estratti documentali: {document_excerpts}\n"
    "- Tag risorse collegate: {related_resources_tags_json}\n"
    "- Regole esistenti per coerenza: {existing_rules_json}\n"
)


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class ArbitrateContext(BaseModel):
    resource_type: str
    document_excerpts: list[str] = []
    related_resources_tags: list[dict] = []


class ArbitrateRequest(BaseModel):
    job_id: str
    resource_id: str
    tag_key: str
    context: ArbitrateContext


class ArbitrateResponse(BaseModel):
    resolved_value: Optional[str]
    rule_id: Optional[str]
    status: str  # "resolved_by_rule" | "resolved_by_llm" | "unresolved"


class _LLMArbitrationResponse(BaseModel):
    resolved_value: Optional[str] = None
    confidence: float = 0.0
    reasoning: str = ""
    proposed_rule: Optional[dict] = None


class MandatoryTypeRequest(BaseModel):
    tenant_id: str
    resource_type: str
    is_mandatory: bool = True
    reason: Optional[str] = None


# ---------------------------------------------------------------------------
# Helper: get LLM client (injectable per test)
# ---------------------------------------------------------------------------

_llm_override = None  # usato dai test per iniettare un mock


def _get_llm():
    if _llm_override is not None:
        return _llm_override
    from llm_gateway.factory import get_llm_client
    return get_llm_client()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok", "service": "agent3_arbiter"}


@app.post("/arbitrate", response_model=ArbitrateResponse)
async def arbitrate(req: ArbitrateRequest):
    from .db import get_tenant_id, list_rules, insert_proposed_rule, log_arbitration
    from .rules_engine import match_rule, apply_rule

    # 1. Ricava tenant_id dal job
    tenant_id = await asyncio.to_thread(get_tenant_id, req.job_id)
    if not tenant_id:
        raise HTTPException(status_code=404, detail=f"Job {req.job_id} non trovato")

    resource_type = req.context.resource_type

    # 2. Cerca regola approvata
    rule = await asyncio.to_thread(match_rule, tenant_id, req.tag_key, resource_type)
    if rule:
        resolved_value = apply_rule(rule)
        await asyncio.to_thread(
            log_arbitration,
            req.job_id, req.resource_id, req.tag_key,
            req.context.model_dump(), resolved_value,
            "resolved_by_rule", rule["rule_id"],
        )
        logger.info(
            "Arbitration %s/%s → regola %s → %s",
            req.resource_id, req.tag_key, rule["rule_id"], resolved_value,
        )
        return ArbitrateResponse(
            resolved_value=resolved_value,
            rule_id=rule["rule_id"],
            status="resolved_by_rule",
        )

    # 3. Nessuna regola → chiama LLM
    from llm_gateway.base import LLMMessage

    existing_rules = await asyncio.to_thread(list_rules, tenant_id)
    user_msg = _USER_TEMPLATE.format(
        tenant_id=tenant_id,
        tag_key=req.tag_key,
        resource_id=req.resource_id,
        resource_type=resource_type,
        document_excerpts="\n---\n".join(req.context.document_excerpts[:3]) or "(nessuno)",
        related_resources_tags_json=json.dumps(req.context.related_resources_tags[:5]),
        existing_rules_json=json.dumps(existing_rules[:10], default=str)[:1000],
    )

    try:
        llm = _get_llm()
        resp = await llm.complete(
            system=_SYSTEM_PROMPT,
            messages=[LLMMessage(role="user", content=user_msg)],
            response_format=_LLMArbitrationResponse,
        )
        llm_result = _LLMArbitrationResponse.model_validate_json(resp.content)
    except Exception as exc:
        logger.error("LLM arbitration failed per %s/%s: %s", req.resource_id, req.tag_key, exc)
        await asyncio.to_thread(
            log_arbitration,
            req.job_id, req.resource_id, req.tag_key,
            req.context.model_dump(), None, "pending",
        )
        return ArbitrateResponse(resolved_value=None, rule_id=None, status="unresolved")

    # 4. Salva regola proposta se presente
    new_rule_id: str | None = None
    if llm_result.proposed_rule:
        pr = llm_result.proposed_rule
        condition = pr.get("condition", {})
        resolution = pr.get("resolution", {})
        new_rule_id = await asyncio.to_thread(
            insert_proposed_rule, tenant_id, req.tag_key, condition, resolution
        )
        logger.info("Nuova regola proposta %s per tenant %s", new_rule_id, tenant_id)

    await asyncio.to_thread(
        log_arbitration,
        req.job_id, req.resource_id, req.tag_key,
        req.context.model_dump(), llm_result.resolved_value,
        "resolved_by_llm", new_rule_id,
    )

    return ArbitrateResponse(
        resolved_value=llm_result.resolved_value,
        rule_id=new_rule_id,
        status="resolved_by_llm",
    )


@app.get("/rules")
async def get_rules(tenant_id: str = Query(...)):
    from .db import list_rules
    rules = await asyncio.to_thread(list_rules, tenant_id)
    return rules


@app.post("/rules/{rule_id}/approve")
async def approve_rule(rule_id: str, approved_by: str = Query(default="operator")):
    from .db import update_rule_status
    ok = await asyncio.to_thread(update_rule_status, rule_id, "approved", approved_by)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Regola {rule_id} non trovata")
    return {"rule_id": rule_id, "status": "approved"}


@app.post("/rules/{rule_id}/reject")
async def reject_rule(rule_id: str):
    from .db import update_rule_status
    ok = await asyncio.to_thread(update_rule_status, rule_id, "rejected")
    if not ok:
        raise HTTPException(status_code=404, detail=f"Regola {rule_id} non trovata")
    return {"rule_id": rule_id, "status": "rejected"}


@app.get("/resource-types")
async def get_resource_types(tenant_id: str = Query(...)):
    from .db import get_mandatory_types
    return await asyncio.to_thread(get_mandatory_types, tenant_id)


@app.post("/resource-types", status_code=201)
async def post_resource_type(req: MandatoryTypeRequest):
    from .db import upsert_mandatory_type
    await asyncio.to_thread(
        upsert_mandatory_type, req.tenant_id, req.resource_type, req.is_mandatory, req.reason
    )
    return {"tenant_id": req.tenant_id, "resource_type": req.resource_type, "is_mandatory": req.is_mandatory}


# ---------------------------------------------------------------------------
# Registro Tagging Strategy (documento ufficiale -> regole estratte via LLM)
# ---------------------------------------------------------------------------

from fastapi import File, Form, UploadFile  # noqa: E402

_UPLOAD_DIR = os.environ.get(
    "STRATEGY_UPLOAD_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "uploads", "strategies"),
)
_extraction_tasks: dict[str, asyncio.Task] = {}


@app.on_event("startup")
async def _mark_interrupted_extractions():
    """Estrazioni rimaste 'extracting' dopo un riavvio del servizio: segnate come interrotte (riprendibili)."""
    from . import strategy_db as sdb
    try:
        await asyncio.to_thread(sdb.mark_interrupted)
    except Exception as exc:  # DB non raggiungibile all'avvio: non blocca il servizio
        logger.warning("Impossibile verificare le estrazioni interrotte: %s", exc)


class StrategyMetaUpdate(BaseModel):
    name: Optional[str] = None
    revision: Optional[str] = None
    release_date: Optional[str] = None


class ReviewRequest(BaseModel):
    status: str  # approved | rejected | proposed
    reviewed_by: Optional[str] = "operator"


def _start_extraction(strategy_id: str, fresh: bool = False) -> None:
    from .strategy import extract_strategy

    running = _extraction_tasks.get(strategy_id)
    if running and not running.done():
        return
    llm = _llm_override  # nei test; altrimenti extract_strategy crea il client dal factory
    _extraction_tasks[strategy_id] = asyncio.create_task(extract_strategy(strategy_id, llm_client=llm, fresh=fresh))


@app.post("/strategies", status_code=201)
async def upload_strategy(
    file: UploadFile = File(...),
    name: Optional[str] = Form(default=None),
    revision: Optional[str] = Form(default=None),
    release_date: Optional[str] = Form(default=None),
    uploaded_by: Optional[str] = Form(default=None),
    extract: bool = Form(default=True),
):
    """
    Carica un documento di Tagging Strategy e avvia in background l'estrazione delle
    regole via LLM. Nome, revisione e data di rilascio, se non indicati, vengono
    ricavati dal documento (changelog/copertina).
    """
    import uuid
    from . import strategy_db as sdb

    os.makedirs(_UPLOAD_DIR, exist_ok=True)
    safe_name = os.path.basename(file.filename or "strategy.pdf")
    path = os.path.join(_UPLOAD_DIR, f"{uuid.uuid4().hex[:8]}_{safe_name}")
    with open(path, "wb") as out:
        out.write(await file.read())

    provisional = f"bozza-{uuid.uuid4().hex[:6]}"
    try:
        strategy = await asyncio.to_thread(
            sdb.create_strategy,
            name or os.path.splitext(safe_name)[0], revision or provisional, release_date,
            safe_name, path, uploaded_by,
        )
    except Exception as exc:  # es. nome + revisione già presenti
        os.remove(path)
        raise HTTPException(status_code=409, detail=f"Strategy non registrata: {exc}")
    if extract:
        _start_extraction(strategy["strategy_id"])
    return strategy


@app.get("/strategies")
async def get_strategies():
    """Elenco delle strategy, ordinate per nome e data di rilascio (revisioni più recenti prima)."""
    from . import strategy_db as sdb
    return await asyncio.to_thread(sdb.list_strategies)


@app.get("/strategies/active")
async def get_active_strategy(include_rules: bool = Query(default=True)):
    """Strategy attiva con tag e regole non rifiutati: è la base della proposta di tagging."""
    from . import strategy_db as sdb

    strategy = await asyncio.to_thread(sdb.get_active_strategy)
    if not strategy:
        raise HTTPException(status_code=404, detail="Nessuna Tagging Strategy attiva")
    tags = await asyncio.to_thread(sdb.list_tags, strategy["strategy_id"])
    strategy["tags"] = [t for t in tags if t["status"] != "rejected"]
    if include_rules:
        rules = await asyncio.to_thread(sdb.list_rules, strategy["strategy_id"])
        strategy["rules"] = [r for r in rules if r["status"] != "rejected"]
    return strategy


@app.get("/strategies/{strategy_id}")
async def get_strategy_detail(strategy_id: str):
    from . import strategy_db as sdb

    strategy = await asyncio.to_thread(sdb.get_strategy, strategy_id)
    if not strategy:
        raise HTTPException(status_code=404, detail=f"Strategy {strategy_id} non trovata")
    strategy["tags"] = await asyncio.to_thread(sdb.list_tags, strategy_id)
    strategy["rules"] = await asyncio.to_thread(sdb.list_rules, strategy_id)
    task = _extraction_tasks.get(strategy_id)
    strategy["extraction_running"] = bool(task and not task.done())
    return strategy


@app.patch("/strategies/{strategy_id}")
async def patch_strategy(strategy_id: str, req: StrategyMetaUpdate):
    from . import strategy_db as sdb
    try:
        await asyncio.to_thread(sdb.update_metadata, strategy_id, req.name, req.revision, req.release_date)
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return await asyncio.to_thread(sdb.get_strategy, strategy_id)


@app.post("/strategies/{strategy_id}/extract", status_code=202)
async def extract_strategy_endpoint(strategy_id: str, fresh: bool = Query(default=False)):
    """(Ri)avvia l'estrazione: riprende dall'ultimo blocco salvato, o da capo con fresh=true."""
    from . import strategy_db as sdb
    if not await asyncio.to_thread(sdb.get_strategy, strategy_id):
        raise HTTPException(status_code=404, detail=f"Strategy {strategy_id} non trovata")
    _start_extraction(strategy_id, fresh=fresh)
    return {"strategy_id": strategy_id, "status": "extracting"}


@app.post("/strategies/{strategy_id}/activate")
async def activate_strategy(strategy_id: str):
    from . import strategy_db as sdb
    strategy = await asyncio.to_thread(sdb.get_strategy, strategy_id)
    if not strategy:
        raise HTTPException(status_code=404, detail=f"Strategy {strategy_id} non trovata")
    if strategy["status"] != "extracted":
        raise HTTPException(status_code=409, detail="Estrazione non completata: impossibile attivare")
    await asyncio.to_thread(sdb.activate, strategy_id)
    return {"strategy_id": strategy_id, "is_active": True}


@app.patch("/strategies/{strategy_id}/tags/{tag_id}")
async def review_strategy_tag(strategy_id: str, tag_id: str, req: ReviewRequest):
    from . import strategy_db as sdb
    n = await asyncio.to_thread(sdb.set_item_status, "strategy_tags", strategy_id, tag_id, req.status, req.reviewed_by)
    if not n:
        raise HTTPException(status_code=404, detail="Tag non trovato")
    return {"id": tag_id, "status": req.status}


@app.patch("/strategies/{strategy_id}/rules/{rule_id}")
async def review_strategy_rule(strategy_id: str, rule_id: str, req: ReviewRequest):
    from . import strategy_db as sdb
    n = await asyncio.to_thread(sdb.set_item_status, "strategy_rules", strategy_id, rule_id, req.status, req.reviewed_by)
    if not n:
        raise HTTPException(status_code=404, detail="Regola non trovata")
    return {"rule_id": rule_id, "status": req.status}


@app.post("/strategies/{strategy_id}/approve-all")
async def approve_all(strategy_id: str, reviewed_by: str = Query(default="operator")):
    """Approva tutti i tag e le regole ancora in stato 'proposed'."""
    from . import strategy_db as sdb
    tags = await asyncio.to_thread(sdb.set_item_status, "strategy_tags", strategy_id, None, "approved", reviewed_by)
    rules = await asyncio.to_thread(sdb.set_item_status, "strategy_rules", strategy_id, None, "approved", reviewed_by)
    return {"strategy_id": strategy_id, "tags_approved": tags, "rules_approved": rules}


@app.delete("/strategies/{strategy_id}")
async def delete_strategy(strategy_id: str):
    from . import strategy_db as sdb
    path = await asyncio.to_thread(sdb.delete_strategy, strategy_id)
    if path is None:
        raise HTTPException(status_code=404, detail=f"Strategy {strategy_id} non trovata")
    if path and os.path.exists(path):
        os.remove(path)
    return {"strategy_id": strategy_id, "deleted": True}
