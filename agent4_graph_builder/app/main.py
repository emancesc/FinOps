"""
Agent 4 — Knowledge Graph Builder  (porta 8004)

POST /graph/build                          — costruisce/aggiorna il grafo per un job
GET  /graph/resource/{arn}                 — sottografo centrato su ARN (depth=2 default)
GET  /graph/group?dimension=&value=        — naviga per attributo o tag-dimension
GET  /graph/search?q=                      — fulltext su arn, type, region, tag values
GET  /health
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

logger = logging.getLogger(__name__)
app = FastAPI(title="FinOps Agent 4 — Graph Builder", version="0.2.0")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class BuildRequest(BaseModel):
    job_id: str
    tenant_id: str | None = None  # se None viene letto dal DB


class BuildResponse(BaseModel):
    job_id: str
    tenant_id: str
    nodes_written: int
    arch_rels_written: int
    tag_rels_written: int


# ---------------------------------------------------------------------------
# Helper: Neo4j client (singleton, injectable per test)
# ---------------------------------------------------------------------------

_neo4j_override = None  # usato dai test


def _get_neo4j():
    if _neo4j_override is not None:
        return _neo4j_override
    from .neo4j_client import get_neo4j_client
    return get_neo4j_client()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok", "service": "agent4_graph_builder"}


@app.post("/graph/build", response_model=BuildResponse)
async def build_graph(req: BuildRequest):
    """
    1. Legge raw_resources + resource_relationships dal DB Postgres per il job.
    2. Legge tag_proposals (review_status='approved') per lo stesso job.
    3. Fa upsert in Neo4j: Resource nodes, relazioni architetturali, nodi tag-dimensione.
    """
    from .db import (
        load_resources_for_job,
        load_relationships_for_job,
        load_approved_proposals_for_job,
        get_tenant_id_for_job,
    )

    # Risolvi tenant_id
    tenant_id = req.tenant_id
    if not tenant_id:
        tenant_id = await asyncio.to_thread(get_tenant_id_for_job, req.job_id)
    if not tenant_id:
        raise HTTPException(status_code=404, detail=f"Job {req.job_id} non trovato")

    resources = await asyncio.to_thread(load_resources_for_job, req.job_id)
    relationships = await asyncio.to_thread(load_relationships_for_job, req.job_id)
    proposals = await asyncio.to_thread(load_approved_proposals_for_job, req.job_id)

    if not resources:
        raise HTTPException(status_code=404, detail=f"Nessuna risorsa trovata per job {req.job_id}")

    neo4j = _get_neo4j()
    stats = await asyncio.to_thread(
        neo4j.build_graph, resources, relationships, proposals, tenant_id
    )

    logger.info(
        "Graph build job=%s tenant=%s %s", req.job_id, tenant_id, stats
    )
    return BuildResponse(job_id=req.job_id, tenant_id=tenant_id, **stats)


@app.get("/graph/resource/{arn:path}")
async def get_resource_subgraph(
    arn: str,
    depth: int = Query(default=2, ge=1, le=5),
) -> dict[str, Any]:
    """
    Sottografo centrato sulla risorsa identificata dall'ARN.
    Ritorna nodi (Resource + nodi tag-dimensione) e archi
    entro `depth` hop. Supporta navigazione sia architettural sia per tag.
    """
    neo4j = _get_neo4j()
    result = await asyncio.to_thread(neo4j.get_resource_subgraph, arn, depth)
    if not result["nodes"]:
        raise HTTPException(status_code=404, detail=f"Risorsa non trovata: {arn}")
    return result


@app.get("/graph/group")
async def get_group(
    dimension: str = Query(..., description=(
        "Nome della dimensione. "
        "Tag-dimensioni: environment, cost-center, business-unit, application, team. "
        "Attributi risorsa: resource_type, region, account_id, instance_type, …"
    )),
    value: str = Query(..., description="Valore della dimensione cercata"),
) -> list[dict[str, Any]]:
    """
    Naviga il grafo per dimensione:
    - Per tag-dimensioni usa la relazione tipizzata verso il nodo dimensione
      (es. BELONGS_TO_BU, IN_ENVIRONMENT, CHARGED_TO).
    - Per attributi usa il filtro diretto sulle proprietà del nodo Resource
      (es. resource_type='AWS::EC2::Instance', region='eu-south-1').
    """
    neo4j = _get_neo4j()
    results = await asyncio.to_thread(neo4j.get_by_dimension, dimension, value)
    return results


@app.get("/graph/search")
async def search_graph(
    q: str = Query(..., min_length=2, description="Stringa da cercare in arn, tipo, regione, tag"),
) -> list[dict[str, Any]]:
    """
    Ricerca fulltext (case-insensitive, regex) su:
      arn, resource_type, region, account_id, tags_json, tag_* properties.
    """
    neo4j = _get_neo4j()
    results = await asyncio.to_thread(neo4j.search, q)
    return results
