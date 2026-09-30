-- =========================================================
-- 002 — Registro Tagging Strategy (revisione, data di rilascio) con regole
--       estratte via LLM, e run di generazione delle proposte di tagging.
-- Idempotente: può essere rieseguita.
-- =========================================================

-- ---------------------------------------------------------
-- TAGGING_STRATEGIES: documento ufficiale, identificato da nome + revisione
-- ---------------------------------------------------------
CREATE TABLE IF NOT EXISTS tagging_strategies (
    strategy_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT NOT NULL,                -- es. 'CINECA AWS Tagging Strategy'
    revision        TEXT NOT NULL,                -- es. '1.6'
    release_date    DATE,                         -- data di rilascio della revisione
    file_name       TEXT NOT NULL,
    storage_path    TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'uploaded'
                    CHECK (status IN ('uploaded', 'extracting', 'extracted', 'error')),
    is_active       BOOLEAN NOT NULL DEFAULT false,  -- revisione usata per le proposte
    progress_pct    NUMERIC(5,1) NOT NULL DEFAULT 0,
    chunks_total    INT NOT NULL DEFAULT 0,
    chunks_done     INT NOT NULL DEFAULT 0,
    summary         TEXT,
    changelog       JSONB NOT NULL DEFAULT '[]'::jsonb,  -- [{version, date, authors, changes}]
    llm_model       TEXT,
    error           TEXT,
    uploaded_by     TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    extracted_at    TIMESTAMPTZ,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (name, revision)
);

-- Una sola revisione attiva alla volta
CREATE UNIQUE INDEX IF NOT EXISTS idx_tagging_strategies_one_active
    ON tagging_strategies ((true)) WHERE is_active;

-- ---------------------------------------------------------
-- STRATEGY_TAGS: definizione di ciascun tag della strategy (es. cineca:Customer)
-- ---------------------------------------------------------
CREATE TABLE IF NOT EXISTS strategy_tags (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    strategy_id     UUID NOT NULL REFERENCES tagging_strategies(strategy_id) ON DELETE CASCADE,
    tag_key         TEXT NOT NULL,
    category        TEXT NOT NULL DEFAULT 'other'
                    CHECK (category IN ('cost_allocation', 'operational', 'other')),
    description     TEXT,
    mandatory       BOOLEAN NOT NULL DEFAULT false,
    billing         BOOLEAN NOT NULL DEFAULT false,   -- tag di cost allocation: vuoto/N/A non ammessi
    multi_value     BOOLEAN NOT NULL DEFAULT false,
    separator       TEXT,
    allowed_values  JSONB NOT NULL DEFAULT '[]'::jsonb, -- [{value, description, business_unit}]
    notes           JSONB NOT NULL DEFAULT '[]'::jsonb, -- vincoli testuali specifici del tag
    source_ref      TEXT,                              -- es. '§2.2 pag. 8'
    status          TEXT NOT NULL DEFAULT 'proposed'
                    CHECK (status IN ('proposed', 'approved', 'rejected')),
    reviewed_by     TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (strategy_id, tag_key)
);

-- ---------------------------------------------------------
-- STRATEGY_RULES: regole applicative ricavate dal documento
-- ---------------------------------------------------------
CREATE TABLE IF NOT EXISTS strategy_rules (
    rule_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    strategy_id     UUID NOT NULL REFERENCES tagging_strategies(strategy_id) ON DELETE CASCADE,
    rule_type       TEXT NOT NULL DEFAULT 'guideline'
                    CHECK (rule_type IN ('guideline', 'resource_type', 'naming_pattern', 'shared_resource',
                                         'value_constraint', 'example', 'other')),
    tag_keys        JSONB NOT NULL DEFAULT '[]'::jsonb,  -- tag a cui si applica
    title           TEXT NOT NULL,
    description     TEXT NOT NULL,
    condition       JSONB NOT NULL DEFAULT '{}'::jsonb,  -- es. {"resource_types": ["AWS::SQS::Queue"]}
    resolution      JSONB NOT NULL DEFAULT '{}'::jsonb,  -- es. {"cineca:Role": ""} o {"strategy": "..."}
    source_ref      TEXT,
    status          TEXT NOT NULL DEFAULT 'proposed'
                    CHECK (status IN ('proposed', 'approved', 'rejected')),
    reviewed_by     TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_strategy_rules_strategy ON strategy_rules (strategy_id);

-- ---------------------------------------------------------
-- PROPOSAL_RUNS: esecuzioni della generazione proposte (avanzamento persistito)
-- ---------------------------------------------------------
CREATE TABLE IF NOT EXISTS proposal_runs (
    run_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id          UUID NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    strategy_id     UUID NOT NULL REFERENCES tagging_strategies(strategy_id),
    status          TEXT NOT NULL DEFAULT 'queued'
                    CHECK (status IN ('queued', 'running', 'done', 'error', 'cancelled')),
    progress_pct    NUMERIC(5,1) NOT NULL DEFAULT 0,
    resources_total INT NOT NULL DEFAULT 0,
    resources_done  INT NOT NULL DEFAULT 0,
    proposals_saved INT NOT NULL DEFAULT 0,
    llm_calls       INT NOT NULL DEFAULT 0,
    input_tokens    BIGINT NOT NULL DEFAULT 0,
    output_tokens   BIGINT NOT NULL DEFAULT 0,
    options         JSONB NOT NULL DEFAULT '{}'::jsonb,
    message         TEXT,
    error           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at      TIMESTAMPTZ,
    finished_at     TIMESTAMPTZ,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_proposal_runs_job ON proposal_runs (job_id);

-- ---------------------------------------------------------
-- TAG_PROPOSALS: tracciabilità verso strategy/run e motivazione
-- ---------------------------------------------------------
ALTER TABLE tag_proposals ADD COLUMN IF NOT EXISTS run_id UUID REFERENCES proposal_runs(run_id) ON DELETE SET NULL;
ALTER TABLE tag_proposals ADD COLUMN IF NOT EXISTS strategy_id UUID REFERENCES tagging_strategies(strategy_id);
ALTER TABLE tag_proposals ADD COLUMN IF NOT EXISTS reasoning TEXT;
ALTER TABLE tag_proposals ADD COLUMN IF NOT EXISTS current_value TEXT;

ALTER TABLE tag_proposals DROP CONSTRAINT IF EXISTS tag_proposals_source_type_check;
ALTER TABLE tag_proposals ADD CONSTRAINT tag_proposals_source_type_check
    CHECK (source_type IN ('document', 'inheritance', 'arbitration_rule', 'manual_override', 'strategy_rule', 'llm'));
