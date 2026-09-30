"""
Generazione della proposta di tagging applicando la Tagging Strategy attiva.

Per ogni risorsa del job e per ogni tag della strategy mancante o con valore non
ammesso:
  1. ereditarietà deterministica: volumi ed ENI prendono i tag di billing e
     cineca:Service dall'istanza a cui sono collegati (regola v1.6 sugli EBS);
  2. LLM a batch: system prompt (in cache) con tag, valori ammessi e regole della
     strategy; messaggio con le risorse, le evidenze dai JSON a corredo
     dell'inventario e gli estratti dei documenti Design/Assessment;
  3. validazione di ogni valore contro i valori ammessi (multi-valore, separatore,
     combinazioni ammesse): i valori non ammessi restano visibili ma segnalati.
Le proposte vengono salvate dopo ogni batch e l'avanzamento è in proposal_runs:
un run interrotto riprende dalla prima risorsa non elaborata.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
from typing import Optional

from pydantic import BaseModel

from . import proposal_db as pdb
from .linked_evidence import EvidenceIndex

logger = logging.getLogger(__name__)

BATCH_SIZE = int(os.environ.get("PROPOSAL_BATCH_SIZE", "10"))
MAX_OUTPUT_TOKENS = int(os.environ.get("PROPOSAL_MAX_OUTPUT_TOKENS", "16000"))
INHERITED_TAGS = ("cineca:BusinessUnit", "cineca:Customer", "cineca:Product", "cineca:Environment", "cineca:Service")


# ---------------------------------------------------------------------------
# Strategy -> system prompt e validazione
# ---------------------------------------------------------------------------

def strategy_prompt(strategy: dict) -> str:
    """Rappresentazione compatta e stabile (cacheable) della strategy."""
    lines = [
        f"TAGGING STRATEGY: {strategy['name']} rev. {strategy['revision']} (rilascio {strategy.get('release_date') or 'n/d'})",
        "", "TAG DA VALORIZZARE:",
    ]
    for t in strategy["tags"]:
        flags = [t["category"]]
        if t.get("mandatory"):
            flags.append("obbligatorio")
        if t.get("billing"):
            flags.append("billing: vuoto o N/A non ammessi")
        if t.get("multi_value"):
            flags.append(f"multi-valore separatore '{t.get('separator') or '+'}'")
        lines.append(f"\n## {t['tag_key']} ({', '.join(flags)})")
        if t.get("description"):
            lines.append(t["description"])
        for note in t.get("notes") or []:
            lines.append(f"- {note}")
        values = t.get("allowed_values") or []
        if values:
            rendered = [v["value"] + (f" [{v['business_unit']}]" if v.get("business_unit") else "")
                        + (f" = {v['description']}" if v.get("description") and v["description"].lower() != v["value"].lower() else "")
                        for v in values]
            lines.append(f"Valori ammessi ({len(values)}): " + "; ".join(rendered))
    lines.append("\nREGOLE:")
    for r in strategy["rules"]:
        extra = []
        if r.get("condition"):
            extra.append("condizione " + json.dumps(r["condition"], ensure_ascii=False, sort_keys=True))
        if r.get("resolution"):
            extra.append("esito " + json.dumps(r["resolution"], ensure_ascii=False, sort_keys=True))
        lines.append(f"- [{r['rule_type']}] {r['title']}: {r['description']}" + (f" ({'; '.join(extra)})" if extra else ""))
    return "\n".join(lines)


SYSTEM_TEMPLATE = """Sei un esperto FinOps che propone i tag cineca:* per risorse AWS applicando RIGOROSAMENTE la
Tagging Strategy riportata sotto. Usa solo i valori ammessi, scritti esattamente come nella strategy;
per i tag multi-valore usa il separatore indicato. Basati sugli elementi forniti per ciascuna risorsa
(tipo, nome, tag attuali, attributi, evidenze sulle risorse collegate, estratti dei documenti di Design e
Assessment, esempi reali della strategy). Non inventare: se un valore non è deducibile con ragionevole
certezza restituisci value null e spiega perché nel reasoning (la strategy preferisce un tag vuoto e
segnalato a un valore arbitrario). Per ogni proposta indica confidence (0-1), un reasoning breve in
italiano e source_ref (regola/sezione della strategy, documento o evidenza usata).

Rispondi SOLO con JSON: {{"resources": [{{"resource_id": "...", "proposals": [{{"tag_key": "...",
"value": "... o null", "confidence": 0.0, "reasoning": "...", "source_ref": "..."}}]}}]}}

{strategy}"""


class _Proposal(BaseModel):
    tag_key: str
    value: Optional[str] = None
    confidence: float = 0.0
    reasoning: str = ""
    source_ref: Optional[str] = None


class _ResourceProposals(BaseModel):
    resource_id: str
    proposals: list[_Proposal] = []


class _BatchResponse(BaseModel):
    resources: list[_ResourceProposals] = []


def validate_value(tag: dict, value: Optional[str]) -> tuple[bool, str]:
    """Il valore rispetta i valori ammessi della strategy? Ritorna (ok, motivo)."""
    if value is None or str(value).strip() == "":
        return (not tag.get("billing") and not tag.get("mandatory"), "valore vuoto")
    allowed = {v["value"].strip().upper() for v in tag.get("allowed_values") or []}
    if not allowed:
        return True, ""
    if value.strip().upper() in allowed:  # include combinazioni ammesse esplicite (es. PROD+PREPROD)
        return True, ""
    if tag.get("multi_value"):
        sep = tag.get("separator") or "+"
        parts = [p.strip() for p in value.split(sep)]
        bad = [p for p in parts if p.upper() not in allowed]
        return (not bad, f"valori non ammessi: {', '.join(bad)}" if bad else "")
    return False, f"'{value}' non è tra i valori ammessi"


def tags_to_propose(strategy: dict, current_tags: dict) -> list[tuple[dict, Optional[str], str]]:
    """[(tag, valore attuale, motivo)] per i tag mancanti o non conformi."""
    out = []
    for tag in strategy["tags"]:
        current = current_tags.get(tag["tag_key"])
        if current is None:
            if tag.get("mandatory") or tag.get("billing") or tag["category"] in ("cost_allocation", "operational"):
                out.append((tag, None, "mancante"))
            continue
        ok, reason = validate_value(tag, current)
        if not ok:
            out.append((tag, current, reason))
    return out


# ---------------------------------------------------------------------------
# Documenti di progetto: recupero per parole chiave
# ---------------------------------------------------------------------------

_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9]{2,}")
_STOP = {"aws", "arn", "eu", "south", "west", "north", "east", "central", "the", "and", "for", "with",
         "instance", "volume", "private", "cineca", "prod", "test"}


class DocumentIndex:
    def __init__(self, chunks: list[dict]):
        self.chunks = [dict(c, lower=c["content"].lower()) for c in chunks]

    def search(self, resource: dict, name: Optional[str], top_k: int = 2, size: int = 500) -> list[str]:
        if not self.chunks:
            return []
        terms = {t.lower() for t in _TOKEN.findall(" ".join(filter(None, [name, resource["resource_id"].rsplit("/", 1)[-1]])))}
        terms -= _STOP
        if not terms:
            return []
        scored = []
        for c in self.chunks:
            score = sum(c["lower"].count(t) for t in terms)
            if score:
                scored.append((score, c))
        scored.sort(key=lambda x: -x[0])
        return [f"[{c['file_name']} #{c['chunk_index']}] {c['content'][:size]}" for _, c in scored[:top_k]]


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

def _name(resource: dict) -> Optional[str]:
    tags = resource.get("current_tags") or {}
    attrs = resource.get("attributes") or {}
    return tags.get("Name") or attrs.get("resource_name")


def _brief_attributes(attrs: dict) -> dict:
    """Chiavi normalizzate dell'inventario (senza la configuration completa), valori lunghi troncati."""
    keep = {k: v for k, v in attrs.items() if k not in ("configuration", "supplementary_configuration", "tagging_analysis")}
    brief = {}
    for key, value in list(keep.items())[:15]:
        text = value if isinstance(value, (int, float, bool, str)) or value is None else json.dumps(value, default=str, ensure_ascii=False)
        brief[key] = text if not isinstance(text, str) or len(text) <= 150 else text[:150] + "..."
    return brief


def inherit(resource: dict, pending: list, evidence: EvidenceIndex, resource_tags: dict, account: str) -> list[dict]:
    """Proposte ereditate dall'istanza collegata (volumi, ENI)."""
    parent = evidence.parent_instance(resource["resource_id"], resource["resource_type"])
    if not parent:
        return []
    parent_arn = f"arn:aws:ec2:{resource['region']}:{account}:instance/{parent}"
    # Tag dell'istanza: inventario (più recente) con i tag mancanti completati dai JSON a corredo
    parent_tags = {**((evidence.instances.get(parent) or {}).get("tags") or {}), **(resource_tags.get(parent_arn) or {})}
    out = []
    for tag, current, reason in pending:
        key = tag["tag_key"]
        value = parent_tags.get(key)
        if key in INHERITED_TAGS and value and validate_value(tag, value)[0]:
            relation = "volume attaccato" if resource["resource_type"] == "AWS::EC2::Volume" else "ENI collegata"
            out.append({"resource_id": resource["resource_id"], "tag_key": key, "tag_value": value,
                        "confidence": 0.85, "source_type": "inheritance", "current_value": current,
                        "source_ref": f"istanza {parent} ({(evidence.instances.get(parent) or {}).get('name') or 'senza nome'})",
                        "reasoning": f"Ereditato dall'istanza a cui è {relation} ({reason})."})
    return out


async def run_proposals(run_id: str, llm_client=None) -> dict:
    from llm_gateway.base import LLMMessage

    run = pdb.get_run(run_id)
    if not run:
        raise ValueError(f"Run {run_id} non trovato")
    job = pdb.get_job(run["job_id"])
    strategy = pdb.get_strategy(run["strategy_id"])
    options = run.get("options") or {}
    resources = pdb.load_resources(run["job_id"], options.get("regions"), options.get("resource_types"))
    if options.get("max_resources"):
        resources = resources[: int(options["max_resources"])]
    start = int(run.get("resources_done") or 0)
    pdb.update_run(run_id, status="running", started=start == 0, resources_total=len(resources),
                   message="Preparazione evidenze e documenti", error=None)

    try:
        if llm_client is None:
            from llm_gateway.factory import get_llm_client
            llm_client = get_llm_client()
        evidence = EvidenceIndex(job["account_id"])
        documents = DocumentIndex(pdb.load_document_chunks(run["job_id"]))
        resource_tags = pdb.load_resource_tags(run["job_id"])
        system = SYSTEM_TEMPLATE.format(strategy=strategy_prompt(strategy))
        tags_by_key = {t["tag_key"]: t for t in strategy["tags"]}
    except Exception as exc:  # noqa: BLE001
        pdb.update_run(run_id, status="error", error=str(exc), finished=True)
        raise

    batches = math.ceil(max(len(resources) - start, 0) / BATCH_SIZE)
    logger.info("Run %s: %d risorse, ripresa da %d, %d batch", run_id, len(resources), start, batches)
    for index in range(start, len(resources), BATCH_SIZE):
        batch = resources[index:index + BATCH_SIZE]
        proposals, llm_items, pending_by_id = [], [], {}
        for res in batch:
            pending = tags_to_propose(strategy, res.get("current_tags") or {})
            if not pending:
                continue
            inherited = inherit(res, pending, evidence, resource_tags, job["account_id"])
            proposals += inherited
            done_keys = {p["tag_key"] for p in inherited}
            remaining = [(t, cur, why) for t, cur, why in pending if t["tag_key"] not in done_keys]
            if not remaining:
                continue
            pending_by_id[res["resource_id"]] = {t["tag_key"]: (cur, why) for t, cur, why in remaining}
            name = _name(res)
            llm_items.append({
                "resource_id": res["resource_id"], "resource_type": res["resource_type"], "region": res["region"],
                "name": name, "current_tags": res.get("current_tags") or {},
                "attributes": _brief_attributes(res.get("attributes") or {}),
                "relationships": (res.get("relationships") or [])[:8],
                "evidence": evidence.for_resource(res["resource_id"], res["resource_type"]),
                "inherited": {p["tag_key"]: p["tag_value"] for p in inherited},
                "document_excerpts": documents.search(res, name),
                "tags_to_propose": [{"tag_key": t["tag_key"], "current_value": cur, "reason": why} for t, cur, why in remaining],
            })

        if llm_items:
            user = ("Proponi i tag indicati in tags_to_propose per ciascuna risorsa.\n"
                    + json.dumps({"resources": llm_items}, ensure_ascii=False, default=str))
            try:
                resp = await llm_client.complete(system=system, messages=[LLMMessage(role="user", content=user)],
                                                 response_format=_BatchResponse, max_tokens=MAX_OUTPUT_TOKENS)
                from llm_gateway.claude_client import _strip_fence
                parsed = _BatchResponse.model_validate(json.loads(_strip_fence(resp.content)))
                pdb.update_run(run_id, llm_calls_add=1, input_tokens_add=resp.input_tokens,
                               output_tokens_add=resp.output_tokens)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Run %s: batch da %d fallito", run_id, index)
                pdb.update_run(run_id, status="error", error=f"batch risorse {index + 1}-{index + len(batch)}: {exc}",
                               message="Interrotto: rilanciare per riprendere dal batch fallito", finished=True)
                return pdb.get_run(run_id)
            for item in parsed.resources:
                wanted = pending_by_id.get(item.resource_id, {})
                for p in item.proposals:
                    if p.tag_key not in wanted or not p.value:
                        continue
                    current, why = wanted[p.tag_key]
                    ok, problem = validate_value(tags_by_key[p.tag_key], p.value)
                    reasoning = p.reasoning if ok else f"[VALORE NON AMMESSO: {problem}] {p.reasoning}"
                    proposals.append({"resource_id": item.resource_id, "tag_key": p.tag_key, "tag_value": p.value,
                                      "confidence": p.confidence if ok else min(p.confidence, 0.2),
                                      "source_type": "llm", "source_ref": p.source_ref, "reasoning": reasoning,
                                      "current_value": current})

        saved = pdb.save_run_proposals(run["job_id"], run_id, run["strategy_id"], proposals)
        done = index + len(batch)
        pdb.update_run(run_id, resources_done=done, proposals_saved_add=saved,
                       progress_pct=round(100.0 * done / len(resources), 1),
                       message=f"Risorse elaborate {done}/{len(resources)}")
    pdb.update_run(run_id, status="done", progress_pct=100.0, finished=True, message="Completato")
    return pdb.get_run(run_id)
