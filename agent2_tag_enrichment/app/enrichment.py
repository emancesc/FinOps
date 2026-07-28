"""
Enrichment loop: per ogni risorsa/tag mancante:
1. retrieval dai document_chunks
2. prompt LLM (AggrProposalResponse)
3. se confidence < soglia → POST a Agente 3 /arbitrate
4. salva in tag_proposals
"""
from __future__ import annotations
import asyncio
import json
import logging
import os
from typing import Callable

import httpx
from pydantic import BaseModel

from .db import load_resources, load_document_ids, save_proposals
from .retrieval import retrieve

logger = logging.getLogger(__name__)

CONFIDENCE_THRESHOLD = float(os.environ.get("ENRICHMENT_CONFIDENCE_THRESHOLD", "0.6"))
MANDATORY_TAG_KEYS = ["environment", "cost-center", "team", "application", "business-unit"]

_SYSTEM_PROMPT = (
    "Sei un assistente esperto di FinOps e cloud tagging su AWS. Il tuo compito è determinare "
    "il valore di uno o più tag per una risorsa AWS, basandoti ESCLUSIVAMENTE sulle "
    "informazioni fornite nel contesto (estratti di documentazione di progetto e attributi "
    "della risorsa). Non inventare valori. Se l'informazione non è presente o non è deducibile "
    "con ragionevole certezza, restituisci \"value\": null e \"confidence\": 0.\n\n"
    "Rispondi SOLO con un oggetto JSON conforme esattamente a questo schema, senza testo "
    "aggiuntivo, markdown o commento:\n\n"
    '{"resource_id": "<stringa>", "proposals": [{"tag_key": "<stringa>", "value": "<stringa o null>", '
    '"confidence": <numero 0-1>, "source_ref": "<riferimento o null>", "reasoning": "<frase>"}]}'
)

_USER_TEMPLATE = (
    "Risorsa da taggare:\n"
    "- resource_id: {resource_id}\n"
    "- resource_type: {resource_type}\n"
    "- attributi noti: {attributes_json}\n"
    "- tag già presenti: {current_tags_json}\n\n"
    "Tag da valorizzare: {tag_keys_list}\n\n"
    "Estratti documentali rilevanti:\n{document_excerpts}\n\n"
    "Risorse collegate e relativi tag:\n{related_resources_tags_json}\n\n"
    "Determina il valore per ciascun tag seguendo esattamente lo schema JSON."
)


class _TagItem(BaseModel):
    tag_key: str
    value: str | None = None
    confidence: float = 0.0
    source_ref: str | None = None
    reasoning: str = ""


class _TagProposalResponse(BaseModel):
    resource_id: str
    proposals: list[_TagItem]


async def enrich_job(
    job_id: str,
    llm_client=None,
    embed_fn: Callable | None = None,
) -> dict:
    """
    Esegue il loop di enrichment per tutte le risorse del job.
    Ritorna un summary dict.
    """
    from llm_gateway.base import LLMMessage

    if llm_client is None:
        from llm_gateway.factory import get_llm_client
        llm_client = get_llm_client()

    resources = load_resources(job_id)
    document_ids = load_document_ids(job_id)

    logger.info("Job %s: %d risorse, %d documenti", job_id, len(resources), len(document_ids))

    all_proposals: list[dict] = []
    resources_with_proposals = 0

    for resource in resources:
        resource_id = resource["resource_id"]
        resource_type = resource["resource_type"]
        current_tags: dict = resource["current_tags"] or {}
        attributes: dict = resource["attributes"] or {}

        missing_tags = [k for k in MANDATORY_TAG_KEYS if k not in current_tags]
        if not missing_tags:
            continue

        # Retrieval
        query = f"{resource_type} {resource_id} {' '.join(missing_tags)}"
        excerpts = retrieve(document_ids, query, top_k=5, embed_fn=embed_fn)
        excerpts_text = "\n---\n".join(excerpts) if excerpts else "(nessun documento disponibile)"

        # Related tags per inheritance
        relationships = resource.get("relationships") or []
        related = json.dumps(relationships[:5])

        user_msg = _USER_TEMPLATE.format(
            resource_id=resource_id,
            resource_type=resource_type,
            attributes_json=json.dumps(attributes, ensure_ascii=False)[:500],
            current_tags_json=json.dumps(current_tags),
            tag_keys_list=", ".join(missing_tags),
            document_excerpts=excerpts_text[:2000],
            related_resources_tags_json=related,
        )

        try:
            resp = await llm_client.complete(
                system=_SYSTEM_PROMPT,
                messages=[LLMMessage(role="user", content=user_msg)],
                response_format=_TagProposalResponse,
            )
            parsed = _TagProposalResponse.model_validate_json(resp.content)
        except Exception as exc:
            logger.warning("LLM error per risorsa %s: %s", resource_id, exc)
            continue

        resource_proposals: list[dict] = []
        for item in parsed.proposals:
            if item.value is None or item.confidence == 0:
                # Confidence bassa → arbiter (fire-and-forget se Agent 3 non disponibile)
                await _maybe_arbitrate(job_id, resource_id, resource_type, item.tag_key, excerpts)
                continue

            if item.confidence < CONFIDENCE_THRESHOLD:
                await _maybe_arbitrate(job_id, resource_id, resource_type, item.tag_key, excerpts)

            resource_proposals.append({
                "resource_id": resource_id,
                "tag_key": item.tag_key,
                "tag_value": item.value,
                "confidence": item.confidence,
                "source_type": "document",
                "source_ref": item.source_ref,
            })

        if resource_proposals:
            all_proposals.extend(resource_proposals)
            resources_with_proposals += 1

    count = save_proposals(job_id, all_proposals)
    summary = {
        "job_id": job_id,
        "resources_processed": len(resources),
        "resources_with_proposals": resources_with_proposals,
        "proposals_saved": count,
    }
    logger.info("Enrichment %s completato: %s", job_id, summary)
    return summary


async def _maybe_arbitrate(
    job_id: str,
    resource_id: str,
    resource_type: str,
    tag_key: str,
    excerpts: list[str],
) -> None:
    """Chiama Agente 3 /arbitrate. Ignora errori se non disponibile."""
    agent3_url = os.environ.get("AGENT3_URL", "http://localhost:8003")
    payload = {
        "job_id": job_id,
        "resource_id": resource_id,
        "tag_key": tag_key,
        "context": {
            "resource_type": resource_type,
            "document_excerpts": excerpts[:3],
            "related_resources_tags": [],
        },
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(f"{agent3_url}/arbitrate", json=payload)
    except Exception as exc:
        logger.debug("Agente 3 non disponibile per %s/%s: %s", resource_id, tag_key, exc)
