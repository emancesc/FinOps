"""Accesso sincrono al DB per agent3 (psycopg2)."""
from __future__ import annotations
import json
import os
import psycopg2
import psycopg2.extras


def _connect() -> psycopg2.extensions.connection:
    return psycopg2.connect(os.environ["DATABASE_URL"])


def get_tenant_id(job_id: str) -> str | None:
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT tenant_id FROM jobs WHERE job_id = %s::uuid", (job_id,))
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


def find_approved_rule(tenant_id: str, tag_key: str, resource_type: str) -> dict | None:
    """
    Cerca una regola approved per (tenant_id, tag_key, resource_type).
    resource_type=None nella condizione della regola = match universale.
    """
    conn = _connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """SELECT rule_id::text, tenant_id, tag_key, condition, resolution, status
                   FROM tenant_tagging_rules
                   WHERE tenant_id = %s
                     AND tag_key   = %s
                     AND status    = 'approved'
                     AND (
                         condition->>'resource_type' IS NULL
                         OR condition->>'resource_type' = %s
                     )
                   ORDER BY updated_at DESC
                   LIMIT 1""",
                (tenant_id, tag_key, resource_type),
            )
            row = cur.fetchone()
            return dict(row) if row else None
    finally:
        conn.close()


def insert_proposed_rule(
    tenant_id: str, tag_key: str, condition: dict, resolution: dict
) -> str:
    """Inserisce una regola proposta dall'LLM. Ritorna il rule_id."""
    conn = _connect()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO tenant_tagging_rules
                           (tenant_id, tag_key, condition, resolution, status)
                       VALUES (%s, %s, %s, %s, 'proposed')
                       RETURNING rule_id::text""",
                    (tenant_id, tag_key, json.dumps(condition), json.dumps(resolution)),
                )
                return cur.fetchone()[0]
    finally:
        conn.close()


def update_rule_status(rule_id: str, status: str, approved_by: str | None = None) -> bool:
    conn = _connect()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE tenant_tagging_rules
                       SET status = %s, approved_by = %s, updated_at = now()
                       WHERE rule_id = %s::uuid""",
                    (status, approved_by, rule_id),
                )
                return cur.rowcount > 0
    finally:
        conn.close()


def list_rules(tenant_id: str) -> list[dict]:
    conn = _connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """SELECT rule_id::text, tenant_id, tag_key, condition, resolution,
                          status, approved_by, created_at::text, updated_at::text
                   FROM tenant_tagging_rules
                   WHERE tenant_id = %s
                   ORDER BY updated_at DESC""",
                (tenant_id,),
            )
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def log_arbitration(
    job_id: str,
    resource_id: str,
    tag_key: str,
    context: dict,
    resolved_value: str | None,
    status: str,
    rule_id: str | None = None,
) -> str:
    """Inserisce una riga in arbitration_requests. Ritorna request_id."""
    conn = _connect()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO arbitration_requests
                           (job_id, resource_id, tag_key, context,
                            resolution_rule_id, resolved_value, status, resolved_at)
                       VALUES (%s::uuid, %s, %s, %s, %s::uuid, %s, %s, now())
                       RETURNING request_id::text""",
                    (
                        job_id,
                        resource_id,
                        tag_key,
                        json.dumps(context),
                        rule_id,
                        resolved_value,
                        status,
                    ),
                )
                return cur.fetchone()[0]
    finally:
        conn.close()


def get_mandatory_types(tenant_id: str) -> list[dict]:
    conn = _connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT id::text, tenant_id, resource_type, is_mandatory, reason "
                "FROM mandatory_resource_types WHERE tenant_id = %s ORDER BY resource_type",
                (tenant_id,),
            )
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def upsert_mandatory_type(tenant_id: str, resource_type: str, is_mandatory: bool, reason: str | None) -> None:
    conn = _connect()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO mandatory_resource_types (tenant_id, resource_type, is_mandatory, reason)
                       VALUES (%s, %s, %s, %s)
                       ON CONFLICT (tenant_id, resource_type) DO UPDATE SET
                           is_mandatory = EXCLUDED.is_mandatory,
                           reason = EXCLUDED.reason""",
                    (tenant_id, resource_type, is_mandatory, reason),
                )
    finally:
        conn.close()
