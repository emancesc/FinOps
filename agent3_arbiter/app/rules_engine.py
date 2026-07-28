"""
Rules engine: matching e applicazione di una regola approvata.
Logica separata da main.py per testabilità.
"""
from __future__ import annotations
from .db import find_approved_rule


def match_rule(tenant_id: str, tag_key: str, resource_type: str) -> dict | None:
    """
    Cerca una regola approved applicabile.
    Ritorna il dict della regola o None se non trovata.
    """
    return find_approved_rule(tenant_id, tag_key, resource_type)


def apply_rule(rule: dict) -> str | None:
    """
    Applica la resolution strategy della regola e ritorna il valore del tag.
    Supporta 'fixed_value', 'inherit_from_parent_vpc', 'derive_from_related_resource'.
    """
    resolution = rule.get("resolution") or {}
    strategy = resolution.get("strategy", "fixed_value")

    if strategy == "fixed_value":
        return resolution.get("detail")

    # Per le strategie di ereditarietà la logica completa richiede
    # attributi della risorsa (gestiti dall'orchestratore); qui restituiamo
    # il 'detail' come valore di default.
    return resolution.get("detail")
