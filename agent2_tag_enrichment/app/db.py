"""Accesso sincrono al DB per agent2 (psycopg2)."""
from __future__ import annotations
import json
import os
import numpy as np
import psycopg2
import psycopg2.extras


def _connect():
    return psycopg2.connect(os.environ["DATABASE_URL"])


def load_resources(job_id: str) -> list[dict]:
    """Carica tutte le raw_resources per il job."""
    conn = _connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT resource_id, resource_type, current_tags, attributes, relationships "
                "FROM raw_resources WHERE job_id = %s::uuid",
                (job_id,)
            )
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def load_document_ids(job_id: str) -> list[str]:
    """Ritorna gli UUID dei documenti associati al job."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT document_id::text FROM documents WHERE job_id = %s::uuid",
                (job_id,)
            )
            return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def save_chunks(document_id: str, chunks: list[tuple[int, str, np.ndarray, dict]]) -> None:
    """
    Salva chunk in document_chunks.
    chunks: list of (chunk_index, content, embedding_array, metadata)
    """
    conn = _connect()
    try:
        with conn:
            with conn.cursor() as cur:
                rows = [
                    (document_id, idx, content, embedding.tobytes(), json.dumps(meta))
                    for idx, content, embedding, meta in chunks
                ]
                psycopg2.extras.execute_values(
                    cur,
                    """INSERT INTO document_chunks (document_id, chunk_index, content, embedding, metadata)
                       VALUES %s ON CONFLICT DO NOTHING""",
                    rows,
                    template="(%s::uuid, %s, %s, %s, %s)",
                )
    finally:
        conn.close()


def load_chunks(document_ids: list[str]) -> list[tuple[str, str, bytes, dict]]:
    """
    Carica tutti i chunk dei documenti specificati.
    Ritorna: list of (chunk_id, content, embedding_bytes, metadata)
    """
    if not document_ids:
        return []
    conn = _connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT chunk_id::text, content, embedding, metadata "
                "FROM document_chunks WHERE document_id = ANY(%s::uuid[])",
                (document_ids,)
            )
            return [
                (r["chunk_id"], r["content"], bytes(r["embedding"]) if r["embedding"] else b"", r["metadata"] or {})
                for r in cur.fetchall()
            ]
    finally:
        conn.close()


def save_proposals(job_id: str, proposals: list[dict]) -> int:
    """
    Upsert tag_proposals. proposals: list of dicts con chiavi:
    resource_id, tag_key, tag_value, confidence, source_type, source_ref
    Ritorna numero di righe.
    """
    if not proposals:
        return 0
    conn = _connect()
    try:
        with conn:
            with conn.cursor() as cur:
                rows = [
                    (
                        job_id,
                        p["resource_id"],
                        p["tag_key"],
                        p.get("tag_value"),
                        p.get("confidence", 0.0),
                        p.get("source_type", "document"),
                        p.get("source_ref"),
                    )
                    for p in proposals
                ]
                psycopg2.extras.execute_values(
                    cur,
                    """INSERT INTO tag_proposals
                           (job_id, resource_id, tag_key, tag_value, confidence, source_type, source_ref)
                       VALUES %s
                       ON CONFLICT (job_id, resource_id, tag_key) DO UPDATE SET
                           tag_value  = EXCLUDED.tag_value,
                           confidence = EXCLUDED.confidence,
                           source_ref = EXCLUDED.source_ref,
                           updated_at = now()""",
                    rows,
                    template="(%s::uuid, %s, %s, %s, %s, %s, %s)",
                )
                return cur.rowcount
    finally:
        conn.close()


def upsert_document(job_id: str, document_id: str, doc_type: str, file_name: str, storage_path: str) -> None:
    """Inserisce o aggiorna una riga in documents."""
    conn = _connect()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO documents (document_id, job_id, doc_type, file_name, storage_path)
                       VALUES (%s::uuid, %s::uuid, %s, %s, %s)
                       ON CONFLICT (document_id) DO NOTHING""",
                    (document_id, job_id, doc_type, file_name, storage_path)
                )
    finally:
        conn.close()


def mark_document_parsed(document_id: str) -> None:
    conn = _connect()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE documents SET parsed_at = now() WHERE document_id = %s::uuid",
                    (document_id,)
                )
    finally:
        conn.close()
