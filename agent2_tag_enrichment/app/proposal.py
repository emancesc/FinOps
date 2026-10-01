"""
Generazione della proposta di tagging applicando la Tagging Strategy attiva.

Per ogni risorsa del job e per ogni tag della strategy mancante o con valore non
ammesso:
  1. ereditarietà deterministica: volumi ed ENI prendono i tag di billing e
     cineca:Service dall'istanza a cui sono collegati (regola v1.6 sugli EBS);
  2. UNA sola chiamata LLM: system prompt con la strategy; nel messaggio, come file,
     i documenti Design/Assessment completi e l'inventario (inventory.tsv: una riga
     per risorsa con attributi, evidenze dai JSON a corredo e tag da proporre).
     Il modello non risponde risorsa per risorsa (l'output non starebbe nel limite
     di una risposta) ma con regole per gruppi di risorse (tipo, regione, nome,
     testo della riga o righe esplicite) e i valori da assegnare;
  3. le regole sono applicate alle righe in ordine (vale la prima che corrisponde)
     e ogni valore è validato contro i valori ammessi (multi-valore, separatore,
     combinazioni ammesse): i valori non ammessi restano visibili ma segnalati.
Un run fallito si rilancia con Riprendi: l'upsert non tocca le proposte già revisionate.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Optional

from pydantic import BaseModel

from . import proposal_db as pdb
from .linked_evidence import EvidenceIndex

logger = logging.getLogger(__name__)

MAX_OUTPUT_TOKENS = int(os.environ.get("PROPOSAL_MAX_OUTPUT_TOKENS", "64000"))
# ~1M token di contesto: margine per system prompt e risposta. Misurato su inventario e documenti
# reali: circa 2 caratteri per token (ARN, identificativi, testo italiano)
MAX_INPUT_CHARS = int(os.environ.get("PROPOSAL_MAX_INPUT_CHARS", "1800000"))
CELL_CHARS = 120
ATTRIBUTES_PER_ROW = 8
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
Tagging Strategy riportata sotto. Nel messaggio trovi, come file, i documenti di Design e Assessment del
progetto e inventory.tsv: una riga per risorsa con tipo, regione, nome, identificativo (l'ARN senza
il prefisso arn:aws:<servizio>:<regione>:<account>:), tag cineca:* attuali, tag già ereditati
dall'istanza collegata, attributi, evidenze sulle risorse collegate e, in to_propose, i tag da proporre
("*" = tutti i tag della strategy; tra parentesi il valore attuale non conforme).

Non rispondere risorsa per risorsa: restituisci REGOLE che assegnano i valori a gruppi di righe. Ogni regola
ha condizioni in "match", tutte da soddisfare (una condizione omessa non filtra):
- resource_types: tipi come nella colonna type (es. "EC2::Instance");
- regions: regioni esatte;
- name_regex: regex (case-insensitive) sulla colonna name;
- text_regex: regex (case-insensitive) sull'intera riga (nome, resource_id, attributi, evidenze, ...);
- rows: numeri di riga espliciti, per i casi che non hanno un criterio comune.
"tags" assegna i valori: per ogni riga e ogni tag di to_propose vale la PRIMA regola, nell'ordine
dell'elenco, che corrisponde e contiene quel tag. Metti quindi prima le regole specifiche e poi quelle
generali. Usa solo i valori ammessi, scritti esattamente come nella strategy (separatore indicato per i
multi-valore). Non inventare: se per un gruppo un valore non è deducibile con ragionevole certezza assegna
null (la strategy preferisce un tag vuoto e segnalato a un valore arbitrario). Ogni regola ha confidence
(0-1), un reasoning breve in italiano e source_ref (regola/sezione della strategy, documento o evidenza).

Rispondi SOLO con JSON: {{"rules": [{{"rule_id": "R1", "match": {{"resource_types": [], "regions": [],
"name_regex": null, "text_regex": null, "rows": []}}, "tags": {{"cineca:...": "... o null"}},
"confidence": 0.0, "reasoning": "...", "source_ref": "..."}}]}}

{strategy}"""


class _Match(BaseModel):
    resource_types: list[str] = []
    regions: list[str] = []
    name_regex: Optional[str] = None
    text_regex: Optional[str] = None
    rows: list[int] = []


class _Rule(BaseModel):
    rule_id: str
    match: _Match = _Match()
    tags: dict[str, Optional[str]] = {}
    confidence: float = 0.0
    reasoning: str = ""
    source_ref: Optional[str] = None


class _RulesResponse(BaseModel):
    rules: list[_Rule] = []


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
# Contesto della chiamata: documenti e inventario come file
# ---------------------------------------------------------------------------

def _name(resource: dict) -> Optional[str]:
    tags = resource.get("current_tags") or {}
    attrs = resource.get("attributes") or {}
    return tags.get("Name") or attrs.get("resource_name")


def _short_type(resource_type: str) -> str:
    return resource_type[5:] if resource_type.startswith("AWS::") else resource_type


def _short_id(arn: str) -> str:
    """ARN senza arn:aws:<servizio>:<regione>:<account>: (tipo e regione sono già nella riga)."""
    parts = arn.split(":", 5)
    return parts[5] if len(parts) == 6 and parts[0] == "arn" else arn


def _kv(tags: dict) -> str:
    return _cell("; ".join(f"{k}={v}" for k, v in tags.items()))


def _cell(value) -> str:
    """Valore su una sola riga, senza tabulazioni, troncato."""
    text = value if isinstance(value, str) else json.dumps(value, default=str, ensure_ascii=False)
    text = " ".join(text.replace("\t", " ").split())
    return text if len(text) <= CELL_CHARS else text[:CELL_CHARS] + "..."


def _attributes_cell(attrs: dict) -> str:
    """Attributi normalizzati dell'inventario (senza configuration completa) in forma k=v."""
    skip = ("configuration", "supplementary_configuration", "tagging_analysis", "resource_name")
    items = [(k, v) for k, v in attrs.items() if k not in skip and v not in (None, "", [], {})][:ATTRIBUTES_PER_ROW]
    return "; ".join(f"{k}={_cell(v)}" for k, v in items)


def _evidence_cell(ev: dict) -> str:
    """Evidenze dai JSON a corredo in forma compatta."""
    parts = []
    if ev.get("instance"):
        inst = ev["instance"]
        parts.append(f"istanza {inst.get('state')} {inst.get('type')} ip {inst.get('private_ip')}")
    if ev.get("ssm"):
        parts.append(f"ssm {ev['ssm'].get('platform')} {ev['ssm'].get('computer_name')}")
    for inst in ev.get("attached_instances") or []:
        parts.append(f"attaccato a {inst['instance_id']} {inst.get('name')} {_kv(inst.get('cineca_tags') or {})}")
    if ev.get("eni"):
        eni = ev["eni"]
        owner = eni.get("instance")
        parts.append(f"eni {eni.get('interface_type')} '{eni.get('description') or ''}'"
                     + (f" istanza {owner['instance_id']} {owner.get('name')} {_kv(owner.get('cineca_tags') or {})}"
                        if isinstance(owner, dict) else ""))
    if ev.get("certificate"):
        cert = ev["certificate"]
        users = [u.rsplit(":", 1)[-1] for u in cert.get("in_use_by") or []]
        parts.append(f"certificato {cert.get('domain')} in uso da {', '.join(users) if users else 'nessuno'}")
    if ev.get("cloudformation"):
        stack = ev["cloudformation"]
        parts.append(f"stack {stack.get('stack')} {_kv(stack.get('stack_tags') or {})}")
    return _cell(" | ".join(parts)) if parts else ""


def documents_files(chunks: list[dict]) -> list[str]:
    """Un file per documento Design/Assessment, con il testo completo."""
    by_doc: dict[str, dict] = {}
    for c in chunks:
        doc = by_doc.setdefault(c["file_name"], {"doc_type": c.get("doc_type"), "parts": []})
        doc["parts"].append(c["content"])
    return [f'<file name="{name}" type="{doc["doc_type"]}">\n' + "\n".join(doc["parts"]) + "\n</file>"
            for name, doc in by_doc.items()]


INVENTORY_COLUMNS = ("row", "type", "region", "name", "id", "current_cineca_tags", "inherited",
                     "attributes", "evidence", "to_propose")


def inventory_row(row: int, res: dict, ev: dict, inherited: dict, remaining: list, all_keys: list[str]) -> list[str]:
    current = {k: v for k, v in (res.get("current_tags") or {}).items() if k.startswith("cineca:")}
    if [t["tag_key"] for t, cur, _ in remaining] == all_keys and all(cur is None for _, cur, _ in remaining):
        to_propose = "*"
    else:
        to_propose = ",".join(f"{t['tag_key']}{'' if cur is None else '(' + _cell(cur) + ')'}" for t, cur, _ in remaining)
    return [str(row), _short_type(res["resource_type"]), res["region"], _cell(_name(res) or ""),
            _short_id(res["resource_id"]), _kv(current), _kv(inherited),
            _attributes_cell(res.get("attributes") or {}), _evidence_cell(ev), to_propose]


def inventory_file(rows: list[list[str]]) -> str:
    lines = ["\t".join(INVENTORY_COLUMNS)] + ["\t".join(r) for r in rows]
    return '<file name="inventory.tsv">\n' + "\n".join(lines) + "\n</file>"


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


# ---------------------------------------------------------------------------
# Applicazione delle regole restituite dal modello
# ---------------------------------------------------------------------------

def _compile(pattern: Optional[str]):
    if not pattern:
        return None
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error:
        return False


def rule_matches(rule: _Rule, row: int, fields: dict) -> bool:
    """Tutte le condizioni indicate devono valere (una condizione assente non filtra)."""
    m = rule.match
    if m.rows and row not in m.rows:
        return False
    if m.resource_types and fields["type"] not in {_short_type(t) for t in m.resource_types}:
        return False
    if m.regions and fields["region"] not in m.regions:
        return False
    for pattern, text in ((m.name_regex, fields["name"]), (m.text_regex, fields["text"])):
        compiled = _compile(pattern)
        if compiled is False or (compiled and not compiled.search(text)):
            return False
    return True


def apply_rules(rules: list[_Rule], items: list[dict], tags_by_key: dict) -> tuple[list[dict], set[str]]:
    """
    Per ogni risorsa e tag da proporre vale la prima regola (in ordine) che corrisponde e
    valorizza quel tag; un valore null nella regola lascia il tag vuoto di proposito.
    Ritorna (proposte, resource_id coperti da almeno una regola).
    """
    proposals, covered = [], set()
    for item in items:
        for key, (current, _why) in item["wanted"].items():
            for rule in rules:
                if key not in rule.tags or not rule_matches(rule, item["row"], item["fields"]):
                    continue
                covered.add(item["resource_id"])
                value = rule.tags[key]
                if value:
                    ok, problem = validate_value(tags_by_key[key], value)
                    reasoning = f"[{rule.rule_id}] {rule.reasoning}"
                    proposals.append({"resource_id": item["resource_id"], "tag_key": key, "tag_value": value,
                                      "confidence": rule.confidence if ok else min(rule.confidence, 0.2),
                                      "source_type": "llm", "source_ref": rule.source_ref, "current_value": current,
                                      "reasoning": reasoning if ok else f"[VALORE NON AMMESSO: {problem}] {reasoning}"})
                break
    return proposals, covered


# ---------------------------------------------------------------------------
# Run: ereditarietà + una sola chiamata LLM
# ---------------------------------------------------------------------------

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
    pdb.update_run(run_id, status="running", started=True, resources_total=len(resources), resources_done=0,
                   progress_pct=0.0, message="Preparazione inventario, evidenze e documenti", error=None)

    try:
        if llm_client is None:
            from llm_gateway.factory import get_llm_client
            llm_client = get_llm_client()
        evidence = EvidenceIndex(job["account_id"])
        resource_tags = pdb.load_resource_tags(run["job_id"])
        tags_by_key = {t["tag_key"]: t for t in strategy["tags"]}
        all_keys = [t["tag_key"] for t in strategy["tags"]]

        proposals, items, rows = [], [], []
        for res in resources:
            pending = tags_to_propose(strategy, res.get("current_tags") or {})
            if not pending:
                continue
            inherited = inherit(res, pending, evidence, resource_tags, job["account_id"])
            proposals += inherited
            done_keys = {p["tag_key"] for p in inherited}
            remaining = [(t, cur, why) for t, cur, why in pending if t["tag_key"] not in done_keys]
            if not remaining:
                continue
            row = len(rows) + 1
            cells = inventory_row(row, res, evidence.for_resource(res["resource_id"], res["resource_type"]),
                                  {p["tag_key"]: p["tag_value"] for p in inherited}, remaining, all_keys)
            rows.append(cells)
            items.append({"row": row, "resource_id": res["resource_id"],
                          "fields": {"type": cells[1], "region": cells[2], "name": cells[3], "text": "\t".join(cells[1:])},
                          "wanted": {t["tag_key"]: (cur, why) for t, cur, why in remaining}})

        saved = pdb.save_run_proposals(run["job_id"], run_id, run["strategy_id"], proposals)
        pdb.update_run(run_id, proposals_saved_add=saved, progress_pct=10.0,
                       message=f"Ereditati {len(proposals)} tag; {len(items)} risorse da proporre con una chiamata LLM")
        if items:
            files = documents_files(pdb.load_document_chunks(run["job_id"])) + [inventory_file(rows)]
            user = (f"Proponi le regole di tagging per le {len(items)} risorse di inventory.tsv.\n\n" + "\n\n".join(files))
            if len(user) > MAX_INPUT_CHARS:
                raise ValueError(f"Contesto troppo grande per una sola chiamata ({len(user):,} caratteri, limite "
                                 f"{MAX_INPUT_CHARS:,}): restringere la generazione per regione o tipo di risorsa")
            pdb.update_run(run_id, progress_pct=20.0,
                           message=f"Chiamata LLM unica in corso ({len(items)} risorse, {len(user):,} caratteri di contesto)")
            resp = await llm_client.complete(system=SYSTEM_TEMPLATE.format(strategy=strategy_prompt(strategy)),
                                             messages=[LLMMessage(role="user", content=user)],
                                             response_format=_RulesResponse, max_tokens=MAX_OUTPUT_TOKENS)
            from llm_gateway.claude_client import _strip_fence
            rules = _RulesResponse.model_validate(json.loads(_strip_fence(resp.content))).rules
            pdb.update_run(run_id, llm_calls_add=1, input_tokens_add=resp.input_tokens, output_tokens_add=resp.output_tokens)
            llm_proposals, covered = apply_rules(rules, items, tags_by_key)
            saved = pdb.save_run_proposals(run["job_id"], run_id, run["strategy_id"], llm_proposals)
            message = (f"Completato: {len(rules)} regole, {len(covered)}/{len(items)} risorse coperte, "
                       f"{len(proposals) + len(llm_proposals)} proposte")
            pdb.update_run(run_id, proposals_saved_add=saved, message=message)
        else:
            pdb.update_run(run_id, message=f"Completato senza chiamate LLM: {len(proposals)} proposte ereditate")
    except Exception as exc:  # noqa: BLE001
        logger.exception("Run %s fallito", run_id)
        pdb.update_run(run_id, status="error", error=str(exc), finished=True,
                       message="Interrotto: usare Riprendi per rilanciare la chiamata")
        return pdb.get_run(run_id)

    pdb.update_run(run_id, status="done", resources_done=len(resources), progress_pct=100.0, finished=True)
    return pdb.get_run(run_id)
