#!/usr/bin/env bash
# Verifica criteri di accettazione Fase 0
# Eseguire dalla root del repo dopo: docker compose up -d postgres redis neo4j
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

source .env 2>/dev/null || true

echo "=== Verifica Fase 0 ==="

# --- Postgres ---
echo ""
echo "[1/3] Postgres — schema applicato?"
TABLES=$(docker compose exec -T postgres psql -U finops -d finops -c "\dt" 2>&1)
echo "$TABLES"
REQUIRED=("jobs" "documents" "document_chunks" "raw_resources" "mandatory_resource_types" "tenant_tagging_rules" "arbitration_requests" "tag_proposals" "audit_log")
MISSING=()
for t in "${REQUIRED[@]}"; do
  echo "$TABLES" | grep -q "$t" || MISSING+=("$t")
done
if [ ${#MISSING[@]} -eq 0 ]; then
  echo "✓ Tutte le tabelle presenti."
else
  echo "✗ Tabelle mancanti: ${MISSING[*]}"
  exit 1
fi

# --- Redis ---
echo ""
echo "[2/3] Redis — risponde al PING?"
PONG=$(docker compose exec -T redis redis-cli ping 2>&1)
if [ "$PONG" = "PONG" ]; then
  echo "✓ Redis OK."
else
  echo "✗ Redis non risponde: $PONG"
  exit 1
fi

# --- Neo4j ---
echo ""
echo "[3/3] Neo4j — vincoli creati?"
CONSTRAINTS=$(docker compose exec -T neo4j cypher-shell \
  -u neo4j -p "${NEO4J_PASSWORD}" \
  "SHOW CONSTRAINTS;" 2>&1)
echo "$CONSTRAINTS"
REQUIRED_CONSTRAINTS=("resource_arn_unique" "businessunit_name_unique" "customer_name_unique" "costcenter_code_unique" "tenant_id_unique")
for c in "${REQUIRED_CONSTRAINTS[@]}"; do
  if echo "$CONSTRAINTS" | grep -q "$c"; then
    echo "✓ Constraint $c presente."
  else
    echo "✗ Constraint $c MANCANTE."
    exit 1
  fi
done

echo ""
echo "=== Fase 0 completata con successo ==="
