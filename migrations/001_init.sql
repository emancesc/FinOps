-- =========================================================
-- Schema Postgres — Piattaforma FinOps Tagging Multi-Agente
-- =========================================================
-- Richiede estensioni: pgvector (per embedding RAG), pgcrypto (per gen_random_uuid)

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;

-- ---------------------------------------------------------
-- JOBS: stato di avanzamento dell'intera pipeline per un account
-- ---------------------------------------------------------
CREATE TABLE jobs (
    job_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id      TEXT NOT NULL,
    region          TEXT NOT NULL,
    tenant_id       TEXT NOT NULL,               -- identifica il cliente/tenant per le regole di Agente 3
    phase           TEXT NOT NULL DEFAULT 'created'
                    CHECK (phase IN (
                        'created', 'extraction', 'enrichment',
                        'arbitration', 'graph_build', 'completed', 'failed'
                    )),
    status_detail   TEXT,                        -- messaggio libero (es. "estrazione EC2 in corso")
    progress_pct    SMALLINT DEFAULT 0 CHECK (progress_pct BETWEEN 0 AND 100),
    error_message   TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_jobs_tenant ON jobs (tenant_id);
CREATE INDEX idx_jobs_phase ON jobs (phase);

-- ---------------------------------------------------------
-- DOCUMENTS: documenti di progetto caricati (HLD, LLD, assessment, tagging strategy, ecc.)
-- ---------------------------------------------------------
CREATE TABLE documents (
    document_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id          UUID NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    doc_type        TEXT NOT NULL
                    CHECK (doc_type IN (
                        'HLD', 'LLD', 'PRE_MIGRATION_ASSESSMENT',
                        'MIGRATION_RESOURCE_LIST', 'TAGGING_STRATEGY', 'OTHER'
                    )),
    file_name       TEXT NOT NULL,
    storage_path    TEXT NOT NULL,               -- path/URI del file originale
    parsed_at       TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Chunk di documento con embedding per RAG (Agente 2)
CREATE TABLE document_chunks (
    chunk_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id     UUID NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    chunk_index     INT NOT NULL,
    content         TEXT NOT NULL,
    embedding       VECTOR(1536),                 -- dimensione da adattare al modello embedding scelto
    metadata        JSONB DEFAULT '{}'::jsonb,    -- es. {"page": 12, "section": "Networking"}
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_document_chunks_embedding
    ON document_chunks USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- ---------------------------------------------------------
-- RAW_RESOURCES: inventario risorse AWS estratto da Agente 1
-- ---------------------------------------------------------
CREATE TABLE raw_resources (
    resource_id     TEXT PRIMARY KEY,             -- ARN come chiave naturale
    job_id          UUID NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    account_id      TEXT NOT NULL,
    region          TEXT NOT NULL,
    resource_type   TEXT NOT NULL,                -- es. 'AWS::EC2::Instance'
    current_tags    JSONB DEFAULT '{}'::jsonb,
    attributes      JSONB DEFAULT '{}'::jsonb,    -- attributi grezzi da describe_*/Config
    relationships   JSONB DEFAULT '[]'::jsonb,    -- [{type, target_resource_id}] da AWS Config
    extracted_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_raw_resources_job ON raw_resources (job_id);
CREATE INDEX idx_raw_resources_type ON raw_resources (resource_type);

-- ---------------------------------------------------------
-- MANDATORY_RESOURCE_TYPES: registro Agente 3 — quali resource type taggare
-- ---------------------------------------------------------
CREATE TABLE mandatory_resource_types (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       TEXT NOT NULL,
    resource_type   TEXT NOT NULL,
    is_mandatory    BOOLEAN NOT NULL,
    reason          TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, resource_type)
);

-- ---------------------------------------------------------
-- TENANT_TAGGING_RULES: regole aggiuntive/integrative alla tagging strategy ufficiale
-- ---------------------------------------------------------
CREATE TABLE tenant_tagging_rules (
    rule_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       TEXT NOT NULL,
    tag_key         TEXT NOT NULL,
    condition       JSONB NOT NULL,               -- es. {"resource_type": "AWS::EFS::FileSystem", "if_missing": true}
    resolution      JSONB NOT NULL,               -- es. {"strategy": "inherit_from_parent_vpc"} oppure {"fixed_value": "shared"}
    approved_by     TEXT,                         -- utente che ha validato la regola (null se solo proposta LLM)
    status          TEXT NOT NULL DEFAULT 'proposed'
                    CHECK (status IN ('proposed', 'approved', 'rejected')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_tenant_rules_tenant ON tenant_tagging_rules (tenant_id);

-- ---------------------------------------------------------
-- ARBITRATION_REQUESTS: casi incerti inviati da Agente 2 ad Agente 3
-- ---------------------------------------------------------
CREATE TABLE arbitration_requests (
    request_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id          UUID NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    resource_id     TEXT NOT NULL REFERENCES raw_resources(resource_id) ON DELETE CASCADE,
    tag_key         TEXT NOT NULL,
    context         JSONB NOT NULL,               -- estratti documentali/attributi usati come contesto
    resolution_rule_id UUID REFERENCES tenant_tagging_rules(rule_id),
    resolved_value  TEXT,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'resolved_by_rule', 'resolved_by_llm', 'resolved_manually')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at     TIMESTAMPTZ
);

-- ---------------------------------------------------------
-- TAG_PROPOSALS: output di Agente 2 (e delle risoluzioni di Agente 3)
-- ---------------------------------------------------------
CREATE TABLE tag_proposals (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id          UUID NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    resource_id     TEXT NOT NULL REFERENCES raw_resources(resource_id) ON DELETE CASCADE,
    tag_key         TEXT NOT NULL,
    tag_value       TEXT,
    confidence      NUMERIC(3,2) CHECK (confidence BETWEEN 0 AND 1),
    source_type     TEXT NOT NULL
                    CHECK (source_type IN ('document', 'inheritance', 'arbitration_rule', 'manual_override')),
    source_ref      TEXT,                         -- es. "LLD.pdf#sezione 4.2" o "rule_id:<uuid>"
    review_status   TEXT NOT NULL DEFAULT 'pending'
                    CHECK (review_status IN ('pending', 'approved', 'rejected', 'edited')),
    reviewed_by     TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (job_id, resource_id, tag_key)
);

CREATE INDEX idx_tag_proposals_job ON tag_proposals (job_id);
CREATE INDEX idx_tag_proposals_resource ON tag_proposals (resource_id);

-- ---------------------------------------------------------
-- AUDIT_LOG: tracciabilità di ogni azione automatica/manuale rilevante
-- ---------------------------------------------------------
CREATE TABLE audit_log (
    id              BIGSERIAL PRIMARY KEY,
    job_id          UUID REFERENCES jobs(job_id) ON DELETE CASCADE,
    actor           TEXT NOT NULL,                -- 'agent_1' | 'agent_2' | 'agent_3' | 'agent_4' | 'user:<email>'
    action          TEXT NOT NULL,                -- es. 'tag_value_set', 'rule_created', 'resource_extracted'
    entity_type     TEXT,
    entity_id       TEXT,
    payload         JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_audit_log_job ON audit_log (job_id);
