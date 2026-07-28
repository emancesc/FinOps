"""Accesso sincrono a PostgreSQL per Agent 4 (psycopg2)."""
from __future__ import annotations

import json
import os
import psycopg2
import psycopg2.extras


def _connect() -> psycopg2.extensions.connection:
    return psycopg2.connect(os.environ["DATABASE_URL"])


def load_resources_for_job(job_id: str) -> list[dict]:
    """
    Carica raw_resources del job.
    Alias: current_tags → tags (per coerenza con neo4j_client.py).
    """
    conn = _connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT resource_id, job_id::text, account_id, region,
                       resource_type, attributes,
                       current_tags AS tags
                FROM raw_resources
                WHERE job_id = %s::uuid
                """,
                (job_id,),
            )
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def load_relationships_for_job(job_id: str) -> list[dict]:
    """
    Estrae le relazioni architetturali dal campo JSONB 'relationships' di raw_resources.
    agent1 serializza: [{"type": "CONTAINS", "target_resource_id": "arn:..."}]
    Non esiste una tabella resource_relationships separata.
    """
    conn = _connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT resource_id, relationships FROM raw_resources WHERE job_id = %s::uuid",
                (job_id,),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    result: list[dict] = []
    for row in rows:
        rels = row["relationships"]
        # psycopg2 restituisce JSONB come list/dict; TEXT come stringa
        if isinstance(rels, str):
            try:
                rels = json.loads(rels)
            except (ValueError, TypeError):
                rels = []
        if not rels:
            continue
        for rel in rels:
            src = row["resource_id"]
            dst = rel.get("target_resource_id")
            rtype = rel.get("type")
            if src and dst and rtype:
                result.append({"source_id": src, "target_id": dst, "relationship_type": rtype})
    return result


def load_approved_proposals_for_job(job_id: str) -> list[dict]:
    """
    Carica tag_proposals con review_status='approved'.
    Alias: tag_value → proposed_value (chiave attesa da neo4j_client.py).
    """
    conn = _connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT resource_id, tag_key,
                       tag_value AS proposed_value,
                       confidence
                FROM tag_proposals
                WHERE job_id = %s::uuid
                  AND review_status = 'approved'
                """,
                (job_id,),
            )
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def get_tenant_id_for_job(job_id: str) -> str | None:
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT tenant_id FROM jobs WHERE job_id = %s::uuid",
                (job_id,),
            )
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()
