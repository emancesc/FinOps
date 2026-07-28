"""
Persistenza sincrona su raw_resources via psycopg2.
"""
from __future__ import annotations
import json
import logging
import os

import psycopg2
import psycopg2.extras

from .aws_client import NormalizedResource

logger = logging.getLogger(__name__)


def _connect() -> psycopg2.extensions.connection:
    return psycopg2.connect(os.environ["DATABASE_URL"])


def upsert_resources(job_id: str, resources: list[NormalizedResource]) -> int:
    """
    Inserisce o aggiorna le risorse in raw_resources.
    Ritorna il numero di righe inserite/aggiornate.
    """
    if not resources:
        return 0

    conn = _connect()
    try:
        with conn:
            with conn.cursor() as cur:
                rows = [
                    (
                        r.resource_id,
                        job_id,
                        r.account_id,
                        r.region,
                        r.resource_type,
                        json.dumps(r.current_tags),
                        json.dumps(r.attributes),
                        json.dumps([rel.model_dump() for rel in r.relationships]),
                    )
                    for r in resources
                ]
                psycopg2.extras.execute_values(
                    cur,
                    """
                    INSERT INTO raw_resources
                        (resource_id, job_id, account_id, region, resource_type,
                         current_tags, attributes, relationships)
                    VALUES %s
                    ON CONFLICT (resource_id) DO UPDATE SET
                        current_tags  = EXCLUDED.current_tags,
                        attributes    = EXCLUDED.attributes,
                        relationships = EXCLUDED.relationships,
                        extracted_at  = now()
                    """,
                    rows,
                )
                count = cur.rowcount
        logger.info("Upsert %d risorse per job %s", count, job_id)
        return count
    finally:
        conn.close()
