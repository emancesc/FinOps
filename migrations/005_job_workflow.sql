-- 005 — Stato del job dal flusso interattivo (inventario → proposta → revisione → grafo).
-- I passi del flusso non passano dalla state machine dell'orchestrator: la fase del job viene
-- derivata dai dati (orchestrator/app/workflow.py) per i job gestiti così (workflow_managed).
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS workflow_managed BOOLEAN NOT NULL DEFAULT false;
-- Ultima costruzione del knowledge graph (agent4 POST /graph/build)
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS graph_built_at TIMESTAMPTZ;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS graph_stats JSONB;
