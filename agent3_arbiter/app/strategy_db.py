"""
Persistenza del registro Tagging Strategy (tagging_strategies, strategy_tags, strategy_rules).
"""
from __future__ import annotations

import json
import os
from datetime import date
from typing import Optional

import psycopg2.extras

from .db import _connect

_STRATEGY_COLS = (
    "strategy_id, name, revision, release_date, file_name, storage_path, status, is_active, "
    "progress_pct, chunks_total, chunks_done, summary, changelog, llm_model, error, uploaded_by, "
    "extraction_state, created_at, extracted_at, updated_at"
)


def _row(cur, row) -> dict:
    d = dict(zip([c.name for c in cur.description], row))
    for k, v in list(d.items()):
        if hasattr(v, "isoformat"):
            d[k] = v.isoformat()
        elif k == "progress_pct" and v is not None:
            d[k] = float(v)
        elif k in ("strategy_id", "id", "rule_id") and v is not None:
            d[k] = str(v)
    return d


def _parse_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def create_strategy(name: str, revision: str, release_date: Optional[str], file_name: str,
                    storage_path: str, uploaded_by: Optional[str]) -> dict:
    conn = _connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                f"""INSERT INTO tagging_strategies (name, revision, release_date, file_name, storage_path, uploaded_by)
                    VALUES (%s, %s, %s, %s, %s, %s) RETURNING {_STRATEGY_COLS}""",
                (name, revision, _parse_date(release_date), file_name, storage_path, uploaded_by),
            )
            return _row(cur, cur.fetchone())
    finally:
        conn.close()


def get_strategy(strategy_id: str) -> Optional[dict]:
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT {_STRATEGY_COLS} FROM tagging_strategies WHERE strategy_id = %s::uuid", (strategy_id,))
            row = cur.fetchone()
            if not row:
                return None
            strategy = _row(cur, row)
            strategy["_tag_in_progress"] = (strategy.get("extraction_state") or {}).get("tag_in_progress")
            return strategy
    finally:
        conn.close()


def list_strategies() -> list[dict]:
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT {', '.join('s.' + c.strip() for c in _STRATEGY_COLS.split(','))},
                           (SELECT COUNT(*) FROM strategy_tags t WHERE t.strategy_id = s.strategy_id) AS tags_count,
                           (SELECT COUNT(*) FROM strategy_rules r WHERE r.strategy_id = s.strategy_id) AS rules_count,
                           (SELECT COALESCE(SUM(jsonb_array_length(t.allowed_values)), 0)
                              FROM strategy_tags t WHERE t.strategy_id = s.strategy_id) AS allowed_values_count
                    FROM tagging_strategies s
                    ORDER BY s.name, s.release_date DESC NULLS LAST, s.created_at DESC"""
            )
            return [_row(cur, r) for r in cur.fetchall()]
    finally:
        conn.close()


def get_active_strategy() -> Optional[dict]:
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT strategy_id FROM tagging_strategies WHERE is_active")
            row = cur.fetchone()
    finally:
        conn.close()
    return get_strategy(str(row[0])) if row else None


def update_metadata(strategy_id: str, name: Optional[str], revision: Optional[str],
                    release_date: Optional[str]) -> None:
    conn = _connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE tagging_strategies SET
                       name = COALESCE(%s, name), revision = COALESCE(%s, revision),
                       release_date = COALESCE(%s, release_date), updated_at = now()
                   WHERE strategy_id = %s::uuid""",
                (name, revision, _parse_date(release_date), strategy_id),
            )
    finally:
        conn.close()


def merge_meta(strategy_id: str, meta: dict) -> None:
    """Completa nome/revisione/data solo se ancora provvisori (valori inseriti dall'utente prevalgono)."""
    conn = _connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SELECT name, revision, release_date, changelog, file_name FROM tagging_strategies "
                        "WHERE strategy_id = %s::uuid", (strategy_id,))
            name, revision, release_date, changelog, file_name = cur.fetchone()
            # Provvisori = non indicati dall'utente: revisione "bozza-..." e nome uguale al nome del file
            provisional_revision = revision.startswith("bozza-")
            provisional_name = name == os.path.splitext(file_name)[0]
            new_name = meta.get("name") if (provisional_name and meta.get("name")) else name
            new_revision = meta.get("revision") if (provisional_revision and meta.get("revision")) else revision
            if (new_name, new_revision) != (name, revision):
                cur.execute("SELECT 1 FROM tagging_strategies WHERE name = %s AND revision = %s AND strategy_id <> %s::uuid",
                            (new_name, new_revision, strategy_id))
                if cur.fetchone():  # revisione già registrata: non si sovrascrive
                    new_name, new_revision = name, revision
            entries = {e.get("version"): e for e in (changelog or [])}
            for e in meta.get("changelog") or []:
                entries.setdefault(e.get("version"), e)
            cur.execute(
                """UPDATE tagging_strategies SET name = %s, revision = %s,
                       release_date = COALESCE(release_date, %s),
                       summary = COALESCE(summary, %s), changelog = %s, updated_at = now()
                   WHERE strategy_id = %s::uuid""",
                (new_name, new_revision, _parse_date(meta.get("release_date")) if not release_date else None,
                 meta.get("summary"), json.dumps(list(entries.values()), ensure_ascii=False), strategy_id),
            )
    finally:
        conn.close()


def update_progress(strategy_id: str, status: Optional[str] = None, chunks_total: Optional[int] = None,
                    chunks_done: Optional[int] = None, progress_pct: Optional[float] = None,
                    llm_model: Optional[str] = None, error: Optional[str] = "__keep__",
                    tag_in_progress: Optional[str] = "__keep__", finished: bool = False) -> None:
    sets, params = ["updated_at = now()"], []
    for col, val in (("status", status), ("chunks_total", chunks_total), ("chunks_done", chunks_done),
                     ("progress_pct", progress_pct), ("llm_model", llm_model)):
        if val is not None:
            sets.append(f"{col} = %s")
            params.append(val)
    if error != "__keep__":
        sets.append("error = %s")
        params.append(error)
    if tag_in_progress != "__keep__":
        sets.append("extraction_state = jsonb_set(extraction_state, '{tag_in_progress}', %s::jsonb)")
        params.append(json.dumps(tag_in_progress))
    if finished:
        sets.append("extracted_at = now()")
    conn = _connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(f"UPDATE tagging_strategies SET {', '.join(sets)} WHERE strategy_id = %s::uuid",
                        (*params, strategy_id))
    finally:
        conn.close()


def reset_extraction(strategy_id: str) -> None:
    conn = _connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute("DELETE FROM strategy_tags WHERE strategy_id = %s::uuid", (strategy_id,))
            cur.execute("DELETE FROM strategy_rules WHERE strategy_id = %s::uuid", (strategy_id,))
            cur.execute("""UPDATE tagging_strategies SET chunks_done = 0, progress_pct = 0, status = 'uploaded',
                               extraction_state = '{}'::jsonb, error = NULL, updated_at = now()
                           WHERE strategy_id = %s::uuid""", (strategy_id,))
    finally:
        conn.close()


def activate(strategy_id: str) -> bool:
    conn = _connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute("UPDATE tagging_strategies SET is_active = false, updated_at = now() WHERE is_active")
            cur.execute("UPDATE tagging_strategies SET is_active = true, updated_at = now() WHERE strategy_id = %s::uuid",
                        (strategy_id,))
            return cur.rowcount == 1
    finally:
        conn.close()


def delete_strategy(strategy_id: str) -> Optional[str]:
    conn = _connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute("DELETE FROM tagging_strategies WHERE strategy_id = %s::uuid RETURNING storage_path", (strategy_id,))
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


# --- tag -------------------------------------------------------------------

_TAG_COLS = ("id, strategy_id, tag_key, category, description, mandatory, billing, multi_value, separator, "
             "allowed_values, notes, source_ref, status, reviewed_by, updated_at")


def get_tag(strategy_id: str, tag_key: str) -> Optional[dict]:
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT {_TAG_COLS} FROM strategy_tags WHERE strategy_id = %s::uuid AND tag_key = %s",
                        (strategy_id, tag_key))
            row = cur.fetchone()
            return _row(cur, row) if row else None
    finally:
        conn.close()


def list_tags(strategy_id: str) -> list[dict]:
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f"""SELECT {_TAG_COLS} FROM strategy_tags WHERE strategy_id = %s::uuid
                            ORDER BY CASE category WHEN 'cost_allocation' THEN 0 WHEN 'operational' THEN 1 ELSE 2 END,
                                     created_at""", (strategy_id,))
            return [_row(cur, r) for r in cur.fetchall()]
    finally:
        conn.close()


def upsert_tag(strategy_id: str, tag: dict) -> None:
    category = tag.get("category") if tag.get("category") in ("cost_allocation", "operational") else "other"
    conn = _connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO strategy_tags (strategy_id, tag_key, category, description, mandatory, billing,
                                              multi_value, separator, allowed_values, notes, source_ref)
                   VALUES (%s::uuid, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (strategy_id, tag_key) DO UPDATE SET
                       category = EXCLUDED.category, description = EXCLUDED.description,
                       mandatory = EXCLUDED.mandatory, billing = EXCLUDED.billing,
                       multi_value = EXCLUDED.multi_value, separator = EXCLUDED.separator,
                       allowed_values = EXCLUDED.allowed_values, notes = EXCLUDED.notes,
                       source_ref = EXCLUDED.source_ref, updated_at = now()""",
                (strategy_id, tag["tag_key"], category, tag.get("description"), bool(tag.get("mandatory")),
                 bool(tag.get("billing")), bool(tag.get("multi_value")), tag.get("separator"),
                 json.dumps(tag.get("allowed_values") or [], ensure_ascii=False),
                 json.dumps(tag.get("notes") or [], ensure_ascii=False), tag.get("source_ref")),
            )
    finally:
        conn.close()


# --- regole ----------------------------------------------------------------

_RULE_TYPES = {"guideline", "resource_type", "naming_pattern", "shared_resource", "value_constraint", "example", "other"}
_RULE_COLS = ("rule_id, strategy_id, rule_type, tag_keys, title, description, condition, resolution, "
              "source_ref, status, reviewed_by, updated_at")


def insert_rule(strategy_id: str, rule: dict) -> None:
    rule_type = rule.get("rule_type") if rule.get("rule_type") in _RULE_TYPES else "other"
    conn = _connect()
    try:
        with conn, conn.cursor() as cur:
            # Dedup: stessa regola (tipo + titolo) già estratta da un blocco precedente
            cur.execute("SELECT 1 FROM strategy_rules WHERE strategy_id = %s::uuid AND rule_type = %s AND lower(title) = lower(%s)",
                        (strategy_id, rule_type, rule.get("title", "")))
            if cur.fetchone():
                return
            cur.execute(
                """INSERT INTO strategy_rules (strategy_id, rule_type, tag_keys, title, description, condition,
                                               resolution, source_ref)
                   VALUES (%s::uuid, %s, %s, %s, %s, %s, %s, %s)""",
                (strategy_id, rule_type, json.dumps(rule.get("tag_keys") or []), rule.get("title", "")[:300],
                 rule.get("description", ""), json.dumps(rule.get("condition") or {}, ensure_ascii=False),
                 json.dumps(rule.get("resolution") or {}, ensure_ascii=False), rule.get("source_ref")),
            )
    finally:
        conn.close()


def list_rules(strategy_id: str) -> list[dict]:
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f"""SELECT {_RULE_COLS} FROM strategy_rules WHERE strategy_id = %s::uuid
                            ORDER BY rule_type, created_at""", (strategy_id,))
            return [_row(cur, r) for r in cur.fetchall()]
    finally:
        conn.close()


def set_item_status(table: str, strategy_id: str, item_id: Optional[str], status: str,
                    reviewed_by: Optional[str]) -> int:
    """Approva/rifiuta un tag o una regola (item_id None = tutti quelli ancora 'proposed')."""
    assert table in ("strategy_tags", "strategy_rules")
    key = "id" if table == "strategy_tags" else "rule_id"
    conn = _connect()
    try:
        with conn, conn.cursor() as cur:
            if item_id:
                cur.execute(f"""UPDATE {table} SET status = %s, reviewed_by = %s, updated_at = now()
                                WHERE strategy_id = %s::uuid AND {key} = %s::uuid""",
                            (status, reviewed_by, strategy_id, item_id))
            else:
                cur.execute(f"""UPDATE {table} SET status = %s, reviewed_by = %s, updated_at = now()
                                WHERE strategy_id = %s::uuid AND status = 'proposed'""",
                            (status, reviewed_by, strategy_id))
            return cur.rowcount
    finally:
        conn.close()


def mark_interrupted() -> int:
    """Chiamata all'avvio: nessuna estrazione può essere davvero in corso."""
    conn = _connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute("""UPDATE tagging_strategies SET status = 'error', updated_at = now(),
                               error = 'Estrazione interrotta dal riavvio del servizio: usare Riprendi'
                           WHERE status = 'extracting'""")
            return cur.rowcount
    finally:
        conn.close()
