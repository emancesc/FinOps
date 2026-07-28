"""
Client Neo4j per Agent 4 — Knowledge Graph Builder.

Navigazione supportata su due assi:
  1. Attributi risorsa (resource_type, region, account_id, instance_type, …)
  2. Valori dei tag (via nodi dimensione: Environment, CostCenter, BusinessUnit, Application, Tenant)

APOC non disponibile su installazione nativa Windows → relazione architetturale
costruita per interpolazione stringa con whitelist Python.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from neo4j import GraphDatabase, Driver

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Whitelist relazioni architetturali (no APOC → interpolazione sicura)
# ---------------------------------------------------------------------------

_ARCH_REL_WHITELIST: set[str] = {
    "CONTAINS",
    "ATTACHED_TO",
    "DEPENDS_ON",
    "SECURED_BY",
    "ROUTES_TO",
}

# ---------------------------------------------------------------------------
# Mappa tag key → (label nodo dimensione, proprietà nodo, tipo relazione)
# ---------------------------------------------------------------------------

_TAG_DIMENSION_MAP: dict[str, tuple[str, str, str]] = {
    "environment":   ("Environment",  "name", "IN_ENVIRONMENT"),
    "cost-center":   ("CostCenter",   "code", "CHARGED_TO"),
    "business-unit": ("BusinessUnit", "name", "BELONGS_TO_BU"),
    "application":   ("Application",  "name", "PART_OF_APPLICATION"),
    "team":          ("Team",         "name", "OWNED_BY"),
}

# Proprietà delle risorse esposte come attributi navigabili (appiattite da attributes JSON)
_NAVIGABLE_ATTRS: set[str] = {
    "instance_type", "state", "vpc_id", "subnet_id",
    "size_gb", "volume_type", "bucket_name", "cidr_block",
}


class Neo4jClient:
    def __init__(self, uri: str, user: str, password: str) -> None:
        self._driver: Driver = GraphDatabase.driver(uri, auth=(user, password))

    def close(self) -> None:
        self._driver.close()

    # ------------------------------------------------------------------
    # Inizializzazione schema / indici
    # ------------------------------------------------------------------

    def ensure_indexes(self) -> None:
        """Crea indici sulle proprietà di Resource più usate in navigazione."""
        with self._driver.session() as s:
            s.run("CREATE INDEX resource_arn IF NOT EXISTS FOR (r:Resource) ON (r.arn)")
            s.run("CREATE INDEX resource_type_idx IF NOT EXISTS FOR (r:Resource) ON (r.resource_type)")
            s.run("CREATE INDEX resource_region_idx IF NOT EXISTS FOR (r:Resource) ON (r.region)")
            s.run("CREATE INDEX resource_account_idx IF NOT EXISTS FOR (r:Resource) ON (r.account_id)")
            s.run("CREATE INDEX resource_job_idx IF NOT EXISTS FOR (r:Resource) ON (r.job_id)")
            # Indici sulle dimensioni tag
            s.run("CREATE INDEX env_name_idx IF NOT EXISTS FOR (e:Environment) ON (e.name)")
            s.run("CREATE INDEX bu_name_idx IF NOT EXISTS FOR (b:BusinessUnit) ON (b.name)")
            s.run("CREATE INDEX cc_code_idx IF NOT EXISTS FOR (c:CostCenter) ON (c.code)")
            s.run("CREATE INDEX app_name_idx IF NOT EXISTS FOR (a:Application) ON (a.name)")

    # ------------------------------------------------------------------
    # BUILD: upsert risorse + relazioni architetturali + tag
    # ------------------------------------------------------------------

    def upsert_resource(self, tx, resource: dict, tenant_id: str) -> None:
        """Nodo Resource con tutte le proprietà appiattite (attributi + tag)."""
        tags: dict = resource.get("tags") or {}
        attributes: dict = resource.get("attributes") or {}

        # Appiatto gli attributi navigabili come proprietà dirette
        flat_attrs = {k: v for k, v in attributes.items() if k in _NAVIGABLE_ATTRS}

        tx.run(
            """
            MERGE (r:Resource {arn: $arn})
            SET r.resource_type = $resource_type,
                r.region        = $region,
                r.account_id    = $account_id,
                r.job_id        = $job_id,
                r.tenant_id     = $tenant_id,
                r.tags          = $tags_json,
                r += $flat_attrs
            """,
            arn=resource["resource_id"],
            resource_type=resource["resource_type"],
            region=resource.get("region", ""),
            account_id=resource.get("account_id", ""),
            job_id=resource.get("job_id", ""),
            tenant_id=tenant_id,
            tags_json=str(tags),          # stringa JSON-like per fulltext
            flat_attrs=flat_attrs,
        )

    def upsert_arch_rel(self, tx, src_arn: str, rel_type: str, dst_arn: str) -> None:
        """Crea relazione architetturale (whitelist-validated)."""
        if rel_type not in _ARCH_REL_WHITELIST:
            logger.warning("Tipo relazione non ammesso ignorato: %s", rel_type)
            return
        tx.run(
            f"""
            MATCH (src:Resource {{arn: $src_arn}})
            MATCH (dst:Resource {{arn: $dst_arn}})
            MERGE (src)-[:{rel_type}]->(dst)
            """,
            src_arn=src_arn,
            dst_arn=dst_arn,
        )

    def upsert_tag_relationships(self, tx, arn: str, tag_key: str, tag_value: str) -> None:
        """
        Aggiorna il valore del tag sul nodo Resource (in tags_json e come
        proprietà separata) e crea nodo dimensione + relazione tipizzata.
        """
        if not tag_value:
            return

        # Aggiorna la proprietà flat del tag sul nodo Resource
        safe_key = tag_key.replace("-", "_")
        tx.run(
            f"""
            MATCH (r:Resource {{arn: $arn}})
            SET r.`tag_{safe_key}` = $value
            """,
            arn=arn,
            value=tag_value,
        )

        dim = _TAG_DIMENSION_MAP.get(tag_key)
        if not dim:
            return
        label, prop, rel_type = dim

        tx.run(
            f"""
            MATCH (r:Resource {{arn: $arn}})
            MERGE (d:{label} {{{prop}: $value}})
            MERGE (r)-[:{rel_type}]->(d)
            """,
            arn=arn,
            value=tag_value,
        )

    def build_graph(
        self,
        resources: list[dict],
        relationships: list[dict],
        tag_proposals: list[dict],
        tenant_id: str,
    ) -> dict[str, int]:
        """
        Upsert completo:
          1. Nodi Resource con attributi appiattiti
          2. Relazioni architetturali (whitelist)
          3. Nodi dimensione + relazioni tag (da tag_proposals approvate)
        """
        nodes_written = 0
        rels_written = 0
        tag_rels_written = 0

        with self._driver.session() as s:
            # Passo 1: nodi risorsa
            for res in resources:
                s.execute_write(self.upsert_resource, res, tenant_id)
                nodes_written += 1

            # Passo 2: relazioni architetturali
            for rel in relationships:
                src = rel.get("source_id") or rel.get("src_arn")
                dst = rel.get("target_id") or rel.get("dst_arn")
                rtype = rel.get("relationship_type") or rel.get("rel_type")
                if src and dst and rtype:
                    s.execute_write(self.upsert_arch_rel, src, rtype, dst)
                    rels_written += 1

            # Passo 3: relazioni tag da proposals approvate
            for proposal in tag_proposals:
                arn = proposal.get("resource_id")
                tag_key = proposal.get("tag_key")
                tag_value = proposal.get("proposed_value")
                if arn and tag_key and tag_value:
                    s.execute_write(self.upsert_tag_relationships, arn, tag_key, tag_value)
                    tag_rels_written += 1

        return {
            "nodes_written": nodes_written,
            "arch_rels_written": rels_written,
            "tag_rels_written": tag_rels_written,
        }

    # ------------------------------------------------------------------
    # GET /graph/resource/{arn} — sottografo centrato su una risorsa
    # ------------------------------------------------------------------

    def get_resource_subgraph(self, arn: str, depth: int = 2) -> dict[str, Any]:
        """
        Ritorna nodi e archi entro `depth` hop dalla risorsa con ARN dato.
        Include sia relazioni architetturali sia relazioni verso nodi tag-dimensione.

        Due query separate: la prima raccoglie i nodi, la seconda le relazioni
        tra quei nodi. Evita il bug di aggregazione su path multipli con .single().
        depth non può essere parametro Cypher → literal intero validato (ge=1, le=5).
        """
        with self._driver.session() as s:
            # Query 1: tutti i nodi raggiungibili entro depth hop
            node_rec = s.run(
                f"MATCH (start:Resource {{arn: $arn}}) "
                f"OPTIONAL MATCH (start)-[*1..{depth}]-(n) "
                f"WITH start, collect(DISTINCT n) AS others "
                f"RETURN [start] + others AS all_nodes",
                arn=arn,
            ).single()

            if not node_rec:
                return {"nodes": [], "edges": []}

            raw_nodes = node_rec["all_nodes"]
            if not raw_nodes:
                return {"nodes": [], "edges": []}

            # Query 2: tutte le relazioni tra i nodi del sottografo
            # element_id è stringa in Neo4j 5.x; id() in Cypher accetta sia int che element_id
            element_ids = [n.element_id for n in raw_nodes]
            rel_rec = s.run(
                "MATCH (a)-[r]->(b) "
                "WHERE elementId(a) IN $eids AND elementId(b) IN $eids "
                "RETURN collect({"
                "  source: coalesce(a.arn, elementId(a)), "
                "  target: coalesce(b.arn, elementId(b)), "
                "  type:   type(r)"
                "}) AS all_rels",
                eids=element_ids,
            ).single()

        nodes_out = []
        for node in raw_nodes:
            props = dict(node)
            props["_labels"] = list(node.labels)
            nodes_out.append(props)

        edges_out = rel_rec["all_rels"] if rel_rec else []

        return {"nodes": nodes_out, "edges": edges_out}

    # ------------------------------------------------------------------
    # GET /graph/group — navigazione per dimensione (tag o attributo)
    # ------------------------------------------------------------------

    def get_by_dimension(self, dimension: str, value: str) -> list[dict]:
        """
        Navigazione a doppio asse:
          - dimension tag (environment, cost-center, business-unit, application, team)
            → traversal via relazione tipizzata verso nodo dimensione
          - dimension attributo (resource_type, region, account_id, instance_type, …)
            → filtro diretto su proprietà del nodo Resource

        Ritorna la lista di nodi Resource che soddisfano la condizione.
        """
        dim_info = _TAG_DIMENSION_MAP.get(dimension)

        with self._driver.session() as s:
            if dim_info:
                # Navigazione via nodo tag-dimensione
                label, prop, rel_type = dim_info
                result = s.run(
                    f"""
                    MATCH (d:{label} {{{prop}: $value}})<-[:{rel_type}]-(r:Resource)
                    RETURN r
                    """,
                    value=value,
                )
            else:
                # Navigazione diretta su attributo Resource
                safe_dim = dimension.replace("-", "_")
                result = s.run(
                    f"MATCH (r:Resource) WHERE r.`{safe_dim}` = $value RETURN r",
                    value=value,
                )

            rows = []
            for record in result:
                node = record["r"]
                props = dict(node)
                props["_labels"] = list(node.labels)
                rows.append(props)
            return rows

    # ------------------------------------------------------------------
    # GET /graph/search — fulltext su arn, resource_type, tags, attributi
    # ------------------------------------------------------------------

    def search(self, q: str) -> list[dict]:
        """
        Ricerca case-insensitive su:
          arn, resource_type, region, account_id, tags_json
        e sulle proprietà flat dei tag (tag_environment, tag_cost_center, …).
        """
        pattern = f"(?i).*{q}.*"
        with self._driver.session() as s:
            result = s.run(
                """
                MATCH (r:Resource)
                WHERE r.arn          =~ $pat
                   OR r.resource_type =~ $pat
                   OR r.region        =~ $pat
                   OR r.account_id    =~ $pat
                   OR r.tags_json     =~ $pat
                   OR any(key IN keys(r) WHERE key STARTS WITH 'tag_' AND r[key] =~ $pat)
                RETURN r LIMIT 100
                """,
                pat=pattern,
            )
            rows = []
            for record in result:
                node = record["r"]
                props = dict(node)
                props["_labels"] = list(node.labels)
                rows.append(props)
            return rows


# ---------------------------------------------------------------------------
# Singleton client (lazily initialized da main.py)
# ---------------------------------------------------------------------------

_client: Neo4jClient | None = None


def get_neo4j_client() -> Neo4jClient:
    global _client
    if _client is None:
        uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
        user = os.environ.get("NEO4J_USER", "neo4j")
        password = os.environ["NEO4J_PASSWORD"]
        _client = Neo4jClient(uri, user, password)
        _client.ensure_indexes()
    return _client
