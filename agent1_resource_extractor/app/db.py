"""
Persistenza sincrona su raw_resources via psycopg2.
"""
from __future__ import annotations
import json
import logging
import os

import psycopg2
import psycopg2.extras

from .aws_client import AccountMismatchError, NormalizedResource

logger = logging.getLogger(__name__)


def _connect() -> psycopg2.extensions.connection:
    return psycopg2.connect(os.environ["DATABASE_URL"])


def check_resource_accounts(job_account_id: str, resources: list[NormalizedResource]) -> None:
    """
    Rifiuta risorse di un account diverso da quello del job: l'upsert sposta la
    risorsa sull'ultimo job che l'ha estratta, quindi senza questo controllo
    risorse di un altro account finirebbero sul job sbagliato.
    """
    foreign = sorted({r.account_id for r in resources if r.account_id != job_account_id})
    if foreign:
        raise AccountMismatchError(job_account_id, ",".join(foreign))


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
                cur.execute("SELECT account_id FROM jobs WHERE job_id = %s::uuid", (job_id,))
                job = cur.fetchone()
                if job is None:
                    raise ValueError(f"Job {job_id} inesistente")
                check_resource_accounts(job[0], resources)
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
                        job_id        = EXCLUDED.job_id,   -- la risorsa appartiene all'ultimo job che l'ha estratta
                        account_id    = EXCLUDED.account_id,
                        region        = EXCLUDED.region,
                        resource_type = EXCLUDED.resource_type,
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
