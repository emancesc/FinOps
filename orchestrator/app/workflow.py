"""
Stato del job secondo il flusso interattivo della piattaforma:
inventario → JSON a corredo → documenti (facoltativi) → proposta di tagging → revisione → grafo.

Questi passi non passano dalla state machine (POST /jobs/{id}/advance): la fase del job è derivata
dai dati e salvata sulla riga del job, così dashboard e liste mostrano lo stato reale. Si applica
ai job gestiti dal flusso (workflow_managed) e a quelli mai avviati con la pipeline (fase 'created');
un job annullato o in corso nella pipeline non viene toccato.
"""
from __future__ import annotations

import json
import os
from typing import Any, Optional

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EXTRACTED_ROOT = os.environ.get("EXTRACTED_ROOT", os.path.join(REPO, "extracted"))
# Come agent2 (app/linked_evidence.py): una regione conta se contiene almeno uno di questi file
LINKED_FILES = ("instances_all.json", "volumes_all.json", "eni_attachments.json", "acm_inuseby_full.json")


def _n(value: int) -> str:
    """Intero con il separatore delle migliaia italiano."""
    return f"{value:,}".replace(",", ".")


def _iso(value) -> Optional[str]:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _linked_json(account_id: str) -> dict:
    """JSON a corredo dell'account (scripts/extract_linked_resources.py): regioni e fine estrazione."""
    account_dir = os.path.join(EXTRACTED_ROOT, account_id)
    run: dict = {}
    try:
        with open(os.path.join(account_dir, "_run.json"), encoding="utf-8-sig") as f:
            run = json.load(f)
    except (OSError, ValueError):
        pass
    regions = sorted(d for d in os.listdir(account_dir)
                     if any(os.path.exists(os.path.join(account_dir, d, f)) for f in LINKED_FILES)) \
        if os.path.isdir(account_dir) else []
    # pronto come per agent2: regioni presenti ed estrazione conclusa (o file di stato assente)
    return {"regions": regions, "finished_at": run.get("finished_at"),
            "ready": bool(regions) and (bool(run.get("finished_at")) or not run)}


def _step(key: str, label: str, status: str, detail: str, at=None, link: Optional[str] = None) -> dict:
    return {"key": key, "label": label, "status": status, "detail": detail, "at": _iso(at), "link": link}


async def compute(conn, job: dict) -> dict[str, Any]:
    """Passi del flusso con stato (done | in_progress | todo | error | optional) e fase derivata."""
    job_id = job["job_id"]
    inv = await conn.fetchrow("SELECT count(*) AS n, max(extracted_at) AS at FROM raw_resources WHERE job_id = $1", job_id)
    docs = await conn.fetchval("SELECT count(*) FROM documents WHERE job_id = $1 AND doc_type <> 'TAGGING_STRATEGY'", job_id)
    run = await conn.fetchrow("SELECT * FROM proposal_runs WHERE job_id = $1 ORDER BY created_at DESC LIMIT 1", job_id)
    review = {r["review_status"]: r["n"] for r in await conn.fetch(
        "SELECT review_status, count(*) AS n FROM tag_proposals WHERE job_id = $1 GROUP BY 1", job_id)}
    last_review = await conn.fetchval(
        "SELECT max(updated_at) FROM tag_proposals WHERE job_id = $1 AND review_status <> 'pending'", job_id)
    linked = _linked_json(job["account_id"])
    total = sum(review.values())
    pending = review.get("pending", 0)
    reviewed = total - pending
    graph_at = job.get("graph_built_at")
    graph_stats = job.get("graph_stats")
    if isinstance(graph_stats, str):
        graph_stats = json.loads(graph_stats)
    graph_stale = bool(graph_at and last_review and last_review > graph_at)
    q = f"?job_id={job_id}"

    steps = [
        _step("inventory", "Inventario", "done" if inv["n"] else "todo",
              f"{_n(inv['n'])} risorse" if inv["n"] else "Da estrarre", inv["at"], f"inventory.html{q}"),
        _step("linked_json", "JSON a corredo", "done" if linked["ready"] else ("in_progress" if linked["regions"] else "todo"),
              (f"Regioni: {', '.join(linked['regions'])}" if linked["regions"]
               else "Da estrarre (scripts/extract_linked_resources.py)"), linked["finished_at"]),
        _step("documents", "Documenti Design/Assessment", "done" if docs else "optional",
              f"{docs} documenti" if docs else "Nessuno (facoltativi)", None, f"proposals.html{q}"),
    ]
    if not run:
        steps.append(_step("proposal", "Proposta di tagging", "todo", "Da generare", None, f"proposals.html{q}"))
    else:
        status = {"done": "done", "error": "error", "cancelled": "error"}.get(run["status"], "in_progress")
        detail = run["message"] or run["status"]
        if run["status"] in ("error", "cancelled") and run["error"]:
            detail = f"{run['error']} — usare Riprendi"
        steps.append(_step("proposal", "Proposta di tagging", status, detail,
                           run["finished_at"] or run["created_at"], f"proposals.html{q}"))
    review_status = "todo" if not total else ("done" if not pending else "in_progress")
    steps.append(_step("review", "Revisione proposte", review_status,
                       (f"{_n(pending)} da rivedere, {_n(review.get('approved', 0))} approvate, "
                        f"{_n(review.get('edited', 0))} modificate, {_n(review.get('rejected', 0))} rifiutate")
                       if total else "Nessuna proposta",
                       last_review, f"review.html{q}"))
    if graph_at:
        detail = (f"{_n(graph_stats.get('nodes_written', 0))} risorse, {_n(graph_stats.get('tag_rels_written', 0))} tag"
                  if graph_stats else "Costruito")
        steps.append(_step("graph", "Knowledge graph", "in_progress" if graph_stale else "done",
                           detail + (" — da ricostruire: revisioni successive all'ultima costruzione" if graph_stale else ""),
                           graph_at, "graph.html"))
    else:
        steps.append(_step("graph", "Knowledge graph", "todo", "Da costruire (pagina Grafo)", None, "graph.html"))

    # Fase derivata (stessi nomi della state machine, per compatibilità con le altre pagine)
    by_key = {s["key"]: s for s in steps}
    if not inv["n"]:
        phase, pct, detail = "created", 0, "Job creato: estrarre l'inventario (pagina Inventario)"
    elif not run:
        phase, pct, detail = "extraction", 25, f"Inventario estratto ({by_key['inventory']['detail']}): generare la proposta di tagging"
    elif by_key["proposal"]["status"] == "in_progress":
        phase, pct, detail = "enrichment", 40, f"Proposta di tagging in corso: {by_key['proposal']['detail']}"
    elif by_key["proposal"]["status"] == "error":
        phase, pct, detail = "enrichment", 40, f"Proposta di tagging interrotta: {by_key['proposal']['detail']}"
    elif pending:
        phase, pct = "arbitration", 60 + int(25 * reviewed / total)
        detail = f"Revisione in corso: {by_key['review']['detail']}"
    elif by_key["graph"]["status"] != "done":
        phase, pct = "graph_build", 85
        detail = ("Revisione completata: ricostruire il grafo" if graph_stale
                  else "Revisione completata: costruire il grafo (pagina Grafo)")
    else:
        phase, pct, detail = "completed", 100, f"Completato: grafo con {by_key['graph']['detail']}"
    next_step = next((s for s in steps if s["status"] in ("todo", "in_progress", "error")), None)
    return {"job_id": str(job_id), "phase": phase, "progress_pct": pct, "status_detail": detail,
            "steps": steps, "next_step": next_step["key"] if next_step else None}


def is_workflow_job(job: dict) -> bool:
    """Job da sincronizzare: già gestito dal flusso o mai avviato con la pipeline."""
    return bool(job.get("workflow_managed")) or job.get("phase") == "created"


async def sync(conn, job: dict) -> Optional[dict]:
    """Calcola il flusso e, per i job del flusso, aggiorna fase/dettaglio/avanzamento sulla riga."""
    if not is_workflow_job(job):
        return None
    wf = await compute(conn, job)
    if wf["phase"] == "created" and not job.get("workflow_managed"):
        return wf  # nessun passo fatto: il job resta disponibile anche per la pipeline
    if (job.get("phase"), job.get("status_detail"), float(job.get("progress_pct") or 0), bool(job.get("workflow_managed"))) \
            != (wf["phase"], wf["status_detail"], float(wf["progress_pct"]), True):
        await conn.execute(
            """UPDATE jobs SET phase = $2, progress_pct = $3, status_detail = $4, workflow_managed = true,
                               updated_at = now() WHERE job_id = $1""",
            job["job_id"], wf["phase"], wf["progress_pct"], wf["status_detail"])
    return wf
