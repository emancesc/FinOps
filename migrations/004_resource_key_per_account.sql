-- 004 — Chiave delle risorse per account: (account_id, resource_id) invece del solo ARN.
-- Alcune risorse gestite da AWS hanno lo stesso ARN in ogni account (es. le regole
-- Route 53 Resolver "autodefined", senza account nell'ARN): con la chiave sul solo ARN
-- l'estrazione di un account si prendeva la riga dell'altro.

-- Tabelle figlie: account_id ricavato dalla risorsa referenziata (ARN ancora univoco qui)
ALTER TABLE tag_proposals        ADD COLUMN IF NOT EXISTS account_id TEXT;
ALTER TABLE arbitration_requests ADD COLUMN IF NOT EXISTS account_id TEXT;
UPDATE tag_proposals p        SET account_id = r.account_id FROM raw_resources r WHERE r.resource_id = p.resource_id AND p.account_id IS NULL;
UPDATE arbitration_requests a SET account_id = r.account_id FROM raw_resources r WHERE r.resource_id = a.resource_id AND a.account_id IS NULL;
ALTER TABLE tag_proposals        ALTER COLUMN account_id SET NOT NULL;
ALTER TABLE arbitration_requests ALTER COLUMN account_id SET NOT NULL;

ALTER TABLE tag_proposals        DROP CONSTRAINT IF EXISTS tag_proposals_resource_id_fkey;
ALTER TABLE arbitration_requests DROP CONSTRAINT IF EXISTS arbitration_requests_resource_id_fkey;

ALTER TABLE raw_resources DROP CONSTRAINT raw_resources_pkey;
ALTER TABLE raw_resources ADD CONSTRAINT raw_resources_pkey PRIMARY KEY (account_id, resource_id);

ALTER TABLE tag_proposals ADD CONSTRAINT tag_proposals_resource_fkey
    FOREIGN KEY (account_id, resource_id) REFERENCES raw_resources (account_id, resource_id) ON DELETE CASCADE;
ALTER TABLE arbitration_requests ADD CONSTRAINT arbitration_requests_resource_fkey
    FOREIGN KEY (account_id, resource_id) REFERENCES raw_resources (account_id, resource_id) ON DELETE CASCADE;

DROP INDEX IF EXISTS idx_tag_proposals_resource;
CREATE INDEX IF NOT EXISTS idx_tag_proposals_resource ON tag_proposals (account_id, resource_id);
