"""
Analisi di conformita' dei tag correnti di una risorsa rispetto alla
CINECA AWS Tagging Strategy (guideline v1.5).

Non applica/scrive tag: si limita a confrontare i tag correnti con lo schema
cineca:* e a produrre suggerimenti best-effort (da validare manualmente),
usati da agent1 per arricchire l'estrazione da AWS Config.
"""
from __future__ import annotations
import re

MANDATORY_TAGS: list[str] = [
    "cineca:BusinessUnit",
    "cineca:Customer",
    "cineca:Product",
    "cineca:Environment",
]

RECOMMENDED_OPERATIONAL_TAGS: list[str] = [
    "cineca:Service",
    "cineca:Role",
    "cineca:ManagedBy",
]

OPTIONAL_OPERATIONAL_TAGS: list[str] = [
    "cineca:LifecycleStatus",
    "cineca:ComplianceScope",
]

ALL_TAGS: list[str] = MANDATORY_TAGS + RECOMMENDED_OPERATIONAL_TAGS + OPTIONAL_OPERATIONAL_TAGS

# Resource type per cui la strategy prevede esplicitamente cineca:Role vuoto
# (sezione 2.3, nota su SQS/Prefix List/SSM Document/Launch Template/EIP/VPC Endpoint)
ROLE_EXEMPT_TYPES: set[str] = {
    "AWS::SQS::Queue",
    "AWS::EC2::PrefixList",
    "AWS::SSM::Document",
    "AWS::EC2::LaunchTemplate",
    "AWS::EC2::EIP",
    "AWS::EC2::VPCEndpoint",
}

# Risorse puramente infrastrutturali (sezione "Open Points" #2): BusinessUnit=CINECA,
# Customer=shared, Product=shared; il resto puo' restare vuoto in attesa di Tag Compliance.
INFRASTRUCTURE_TYPES: set[str] = {
    "AWS::EC2::VPC",
    "AWS::EC2::Subnet",
    "AWS::EC2::SubnetRouteTableAssociation",
    "AWS::EC2::InternetGateway",
    "AWS::EC2::RouteTable",
    "AWS::EC2::NetworkAcl",
    "AWS::EC2::DHCPOptions",
    "AWS::EC2::TransitGatewayAttachment",
    "AWS::EC2::VPNGateway",
    "AWS::EC2::VPNConnection",
    "AWS::EC2::CustomerGateway",
    "AWS::EC2::FlowLog",
}

# cineca:Role suggerito in base al resource type (sezione 2.3 "Application Security",
# "Storage", "Network")
ROLE_HINT_BY_TYPE: dict[str, str] = {
    "AWS::EC2::Volume": "Storage-Volume",
    "AWS::FSx::FileSystem": "Storage-Volume",
    "AWS::ElasticLoadBalancingV2::LoadBalancer": "Network-LoadBalancer",
    "AWS::EC2::NetworkInterface": "Network-Interface",
    "AWS::ACM::Certificate": "acm:certificate",
    "AWS::KMS::Key": "kms:key",
}

# cineca:Service suggerito in base al resource type (sezione 2.3)
SERVICE_HINT_BY_TYPE: dict[str, str] = {
    "AWS::ElasticLoadBalancingV2::LoadBalancer": "LoadBalancer",
}

# Euristiche sul tag Name, dedotte dagli scenari reali documentati nella strategy
# (v1.5, changelog + "Real-World Tagging Scenarios"). Sono suggerimenti da validare,
# non regole vincolanti: la strategy stessa qualifica queste mappature come proposte.
_NAME_PATTERNS: list[tuple[re.Pattern, dict[str, str]]] = [
    (re.compile(r"^idp5be", re.I), {"cineca:Service": "shibboleth-idp", "cineca:Role": "Identity-Backend"}),
    (re.compile(r"^idp5fe", re.I), {"cineca:Service": "apache-httpd", "cineca:Role": "Identity-Frontend"}),
    (re.compile(r"^ldap-aws", re.I), {"cineca:Service": "openldap"}),
    (re.compile(r"mongodb", re.I), {"cineca:Service": "MongoDB", "cineca:Role": "Database-Primary"}),
    (re.compile(r"worker", re.I), {"cineca:Service": "OpenShift", "cineca:Role": "Compute-OpenShift-Worker"}),
    (re.compile(r"(master|controlplane)", re.I), {"cineca:Service": "OpenShift", "cineca:Role": "Compute-OpenShift-ControlPlane"}),
]


def _suggest_from_name(name: str | None) -> dict[str, str]:
    if not name:
        return {}
    for pattern, hints in _NAME_PATTERNS:
        if pattern.search(name):
            return dict(hints)
    return {}


def suggest_tags(resource_type: str, name: str | None = None) -> dict[str, str]:
    """Suggerimenti best-effort (da validare) per i tag operativi mancanti."""
    suggestions: dict[str, str] = {}
    if resource_type in ROLE_HINT_BY_TYPE:
        suggestions["cineca:Role"] = ROLE_HINT_BY_TYPE[resource_type]
    if resource_type in SERVICE_HINT_BY_TYPE:
        suggestions["cineca:Service"] = SERVICE_HINT_BY_TYPE[resource_type]
    suggestions.update(_suggest_from_name(name))
    if resource_type in INFRASTRUCTURE_TYPES:
        suggestions.setdefault("cineca:BusinessUnit", "CINECA")
        suggestions.setdefault("cineca:Customer", "shared")
        suggestions.setdefault("cineca:Product", "shared")
    return suggestions


def analyze_tagging(resource_type: str, tags: dict[str, str], name: str | None = None) -> dict:
    """
    Confronta i tag correnti di una risorsa con la CINECA Tagging Strategy v1.5.

    Ritorna un dizionario con: tag cineca:* presenti, tag mandatory/operativi
    mancanti, suggerimenti (per i soli tag ancora mancanti) e due flag di
    contesto (is_role_exempt, is_infrastructure) usati per non segnalare come
    "mancante" un tag che la strategy prevede esplicitamente vuoto.
    """
    name = name or tags.get("Name")
    is_role_exempt = resource_type in ROLE_EXEMPT_TYPES
    is_infrastructure = resource_type in INFRASTRUCTURE_TYPES

    present = {k: tags[k] for k in ALL_TAGS if tags.get(k)}
    missing_mandatory = [k for k in MANDATORY_TAGS if not tags.get(k)]
    missing_recommended = [
        k
        for k in RECOMMENDED_OPERATIONAL_TAGS
        if not tags.get(k) and not (k == "cineca:Role" and is_role_exempt)
    ]
    suggestions = {
        k: v for k, v in suggest_tags(resource_type, name).items() if not tags.get(k)
    }

    return {
        "present": present,
        "missing_mandatory": missing_mandatory,
        "missing_recommended": missing_recommended,
        "suggestions": suggestions,
        "is_role_exempt": is_role_exempt,
        "is_infrastructure": is_infrastructure,
        "compliant": not missing_mandatory,
    }
