"""Persistenza per documenti di progetto, run di proposta e tag_proposals (agent2)."""
from __future__ import annotations

import json
from typing import Optional

import psycopg2.extras

from .db import _connect


def _jsonable(row: dict) -> dict:
    out = {}
    for k, v in row.items():
        if hasattr(v, "isoformat"):
            v = v.isoformat()
        elif v.__class__.__name__ in ("UUID", "Decimal"):
            v = float(v) if v.__class__.__name__ == "Decimal" else str(v)
        out[k] = v
    return out


def _fetch(sql: str, params=()) -> list[dict]:
    conn = _connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return [_jsonable(dict(r)) for r in cur.fetchall()]
    finally:
        conn.close()


def _execute(sql: str, params=()) -> int:
    conn = _connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.rowcount
    finally:
        conn.close()


# --- job / strategy ---------------------------------------------------------

def get_job(job_id: str) -> Optional[dict]:
    rows = _fetch("SELECT job_id, account_id, region, tenant_id, phase FROM jobs WHERE job_id = %s::uuid", (job_id,))
    return rows[0] if rows else None


def get_strategy(strategy_id: Optional[str]) -> Optional[dict]:
    """Strategy indicata (o quella attiva) con tag e regole non rifiutati."""
    where = "strategy_id = %s::uuid" if strategy_id else "is_active"
    rows = _fetch(f"SELECT strategy_id, name, revision, release_date, status, is_active FROM tagging_strategies WHERE {where}",
                  (strategy_id,) if strategy_id else ())
    if not rows:
        return None
    strategy = rows[0]
    strategy["tags"] = _fetch(
        """SELECT tag_key, category, description, mandatory, billing, multi_value, separator, allowed_values, notes
           FROM strategy_tags WHERE strategy_id = %s::uuid AND status <> 'rejected'
           ORDER BY CASE category WHEN 'cost_allocation' THEN 0 WHEN 'operational' THEN 1 ELSE 2 END, created_at""",
        (strategy["strategy_id"],))
    strategy["rules"] = _fetch(
        """SELECT rule_type, tag_keys, title, description, condition, resolution, source_ref
           FROM strategy_rules WHERE strategy_id = %s::uuid AND status <> 'rejected' ORDER BY rule_type, created_at""",
        (strategy["strategy_id"],))
    return strategy


def count_resources(job_id: str) -> int:
    return _fetch("SELECT COUNT(*) AS n FROM raw_resources WHERE job_id = %s::uuid", (job_id,))[0]["n"]


def load_resources(job_id: str, regions: Optional[list] = None, resource_types: Optional[list] = None) -> list[dict]:
    sql = ("SELECT resource_id, region, resource_type, current_tags, attributes, relationships "
           "FROM raw_resources WHERE job_id = %s::uuid")
    params: list = [job_id]
    if regions:
        sql += " AND region = ANY(%s)"
        params.append(regions)
    if resource_types:
        sql += " AND resource_type = ANY(%s)"
        params.append(resource_types)
    return _fetch(sql + " ORDER BY resource_id", tuple(params))


def load_resource_tags(job_id: str) -> dict[str, dict]:
    """resource_id -> current_tags per tutte le risorse del job (serve all'ereditarietà)."""
    return {r["resource_id"]: r["current_tags"] or {}
            for r in _fetch("SELECT resource_id, current_tags FROM raw_resources WHERE job_id = %s::uuid", (job_id,))}


# --- documenti di progetto ------------------------------------------------

def insert_document(job_id: str, doc_type: str, file_name: str, storage_path: str) -> dict:
    conn = _connect()
    try:
        with conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """INSERT INTO documents (job_id, doc_type, file_name, storage_path)
                   VALUES (%s::uuid, %s, %s, %s)
                   RETURNING document_id, job_id, doc_type, file_name, storage_path, parsed_at, created_at""",
                (job_id, doc_type, file_name, storage_path))
            return _jsonable(dict(cur.fetchone()))
    finally:
        conn.close()


def save_text_chunks(document_id: str, chunks: list[str], meta: dict) -> None:
    """Chunk testuali senza embedding (il recupero per la proposta è per parole chiave)."""
    conn = _connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute("DELETE FROM document_chunks WHERE document_id = %s::uuid", (document_id,))
            psycopg2.extras.execute_values(
                cur,
                "INSERT INTO document_chunks (document_id, chunk_index, content, metadata) VALUES %s",
                [(document_id, i, c, json.dumps({**meta, "chunk_index": i})) for i, c in enumerate(chunks)],
                template="(%s::uuid, %s, %s, %s)")
            cur.execute("UPDATE documents SET parsed_at = now() WHERE document_id = %s::uuid", (document_id,))
    finally:
        conn.close()


def list_documents(job_id: str) -> list[dict]:
    return _fetch(
        """SELECT d.document_id, d.doc_type, d.file_name, d.parsed_at, d.created_at,
                  (SELECT COUNT(*) FROM document_chunks c WHERE c.document_id = d.document_id) AS chunks,
                  (SELECT COALESCE(SUM(length(c.content)), 0) FROM document_chunks c WHERE c.document_id = d.document_id) AS chars
           FROM documents d WHERE d.job_id = %s::uuid ORDER BY d.created_at""", (job_id,))


def delete_document(document_id: str) -> Optional[str]:
    conn = _connect()
    try:
        with conn, conn.cursor() as cur:  # "with conn" = commit (i chunk vanno via in cascata)
            cur.execute("DELETE FROM documents WHERE document_id = %s::uuid RETURNING storage_path", (document_id,))
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


def load_document_chunks(job_id: str) -> list[dict]:
    return _fetch(
        """SELECT d.file_name, d.doc_type, c.chunk_index, c.content
           FROM document_chunks c JOIN documents d ON d.document_id = c.document_id
           WHERE d.job_id = %s::uuid AND d.doc_type <> 'TAGGING_STRATEGY'
           ORDER BY d.created_at, c.chunk_index""", (job_id,))


# --- run di proposta --------------------------------------------------------

def create_run(job_id: str, strategy_id: str, options: dict, resources_total: int) -> dict:
    conn = _connect()
    try:
        with conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """INSERT INTO proposal_runs (job_id, strategy_id, options, resources_total)
                   VALUES (%s::uuid, %s::uuid, %s, %s) RETURNING *""",
                (job_id, strategy_id, json.dumps(options), resources_total))
            return _jsonable(dict(cur.fetchone()))
    finally:
        conn.close()


def get_run(run_id: str) -> Optional[dict]:
    rows = _fetch("SELECT * FROM proposal_runs WHERE run_id = %s::uuid", (run_id,))
    return rows[0] if rows else None


def list_runs(job_id: str) -> list[dict]:
    return _fetch("SELECT * FROM proposal_runs WHERE job_id = %s::uuid ORDER BY created_at DESC", (job_id,))


def running_run(job_id: str) -> Optional[dict]:
    rows = _fetch("SELECT * FROM proposal_runs WHERE job_id = %s::uuid AND status IN ('queued', 'running') "
                  "ORDER BY created_at DESC LIMIT 1", (job_id,))
    return rows[0] if rows else None


def update_run(run_id: str, **fields) -> None:
    """Aggiorna i campi indicati; *_add incrementa (llm_calls_add, input_tokens_add, ...)."""
    sets, params = ["updated_at = now()"], []
    for key, value in fields.items():
        if key.endswith("_add"):
            col = key[:-4]
            sets.append(f"{col} = {col} + %s")
        elif key in ("started", "finished"):
            if value:
                sets.append(f"{key}_at = now()")
            continue
        else:
            col = key
            sets.append(f"{col} = %s")
            value = json.dumps(value) if isinstance(value, (dict, list)) else value
        params.append(value)
    _execute(f"UPDATE proposal_runs SET {', '.join(sets)} WHERE run_id = %s::uuid", (*params, run_id))


def save_run_proposals(job_id: str, run_id: str, strategy_id: str, proposals: list[dict]) -> int:
    """Upsert su (job, risorsa, tag): una revisione manuale già fatta non viene sovrascritta."""
    if not proposals:
        return 0
    conn = _connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SELECT account_id FROM jobs WHERE job_id = %s::uuid", (job_id,))
            account_id = cur.fetchone()[0]
            psycopg2.extras.execute_values(
                cur,
                """INSERT INTO tag_proposals (job_id, account_id, resource_id, tag_key, tag_value, confidence, source_type,
                                              source_ref, run_id, strategy_id, reasoning, current_value)
                   VALUES %s
                   ON CONFLICT (job_id, resource_id, tag_key) DO UPDATE SET
                       tag_value = EXCLUDED.tag_value, confidence = EXCLUDED.confidence,
                       source_type = EXCLUDED.source_type, source_ref = EXCLUDED.source_ref,
                       run_id = EXCLUDED.run_id, strategy_id = EXCLUDED.strategy_id,
                       reasoning = EXCLUDED.reasoning, current_value = EXCLUDED.current_value,
                       review_status = 'pending', reviewed_by = NULL, updated_at = now()
                   WHERE tag_proposals.review_status = 'pending'""",
                [(job_id, account_id, p["resource_id"], p["tag_key"], p.get("tag_value"), round(float(p.get("confidence") or 0), 2),
                  p.get("source_type", "llm"), p.get("source_ref"), run_id, strategy_id, p.get("reasoning"),
                  p.get("current_value")) for p in proposals],
                template="(%s::uuid, %s, %s, %s, %s, %s, %s, %s, %s::uuid, %s::uuid, %s, %s)")
            return cur.rowcount
    finally:
        conn.close()


# --- revisione proposte -----------------------------------------------------

def list_proposals(job_id: str, run_id: Optional[str] = None, review_status: Optional[str] = None,
                   tag_key: Optional[str] = None, limit: int = 5000, offset: int = 0) -> list[dict]:
    sql = ("SELECT p.id, p.resource_id, r.resource_type, r.region, p.tag_key, p.tag_value, p.current_value, "
           "p.confidence, p.source_type, p.source_ref, p.reasoning, p.review_status, p.reviewed_by, p.run_id, "
           "p.updated_at FROM tag_proposals p JOIN raw_resources r ON r.account_id = p.account_id AND r.resource_id = p.resource_id "
           "WHERE p.job_id = %s::uuid")
    params: list = [job_id]
    for col, val in (("p.run_id", run_id), ("p.review_status", review_status), ("p.tag_key", tag_key)):
        if val:
            sql += f" AND {col} = %s" + ("::uuid" if col == "p.run_id" else "")
            params.append(val)
    sql += " ORDER BY p.resource_id, p.tag_key LIMIT %s OFFSET %s"
    params += [limit, offset]
    return _fetch(sql, tuple(params))


def review_proposal(proposal_id: str, review_status: str, tag_value: Optional[str], reviewed_by: Optional[str]) -> int:
    if tag_value is not None:
        return _execute("""UPDATE tag_proposals SET tag_value = %s, review_status = 'edited', reviewed_by = %s,
                               updated_at = now() WHERE id = %s::uuid""", (tag_value, reviewed_by, proposal_id))
    return _execute("""UPDATE tag_proposals SET review_status = %s, reviewed_by = %s, updated_at = now()
                       WHERE id = %s::uuid""", (review_status, reviewed_by, proposal_id))


def mark_interrupted_runs() -> int:
    """Chiamata all'avvio: nessun run può essere davvero in corso."""
    return _execute("""UPDATE proposal_runs SET status = 'error', updated_at = now(),
                          error = 'Generazione interrotta dal riavvio del servizio: usare Riprendi'
                       WHERE status IN ('running', 'queued')""")
