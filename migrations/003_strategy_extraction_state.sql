-- 003 — Stato di ripresa dell'estrazione strategy (es. tabella di valori ammessi in corso tra due blocchi)
ALTER TABLE tagging_strategies ADD COLUMN IF NOT EXISTS extraction_state JSONB NOT NULL DEFAULT '{}'::jsonb;
