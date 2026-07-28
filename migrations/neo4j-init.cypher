// Vincoli di unicità e indici per il Knowledge Graph FinOps
// Eseguito una sola volta al bootstrap dal container neo4j-init

CREATE CONSTRAINT resource_arn_unique IF NOT EXISTS
FOR (r:Resource) REQUIRE r.arn IS UNIQUE;

CREATE CONSTRAINT businessunit_name_unique IF NOT EXISTS
FOR (b:BusinessUnit) REQUIRE b.name IS UNIQUE;

CREATE CONSTRAINT customer_name_unique IF NOT EXISTS
FOR (c:Customer) REQUIRE c.name IS UNIQUE;

CREATE CONSTRAINT costcenter_code_unique IF NOT EXISTS
FOR (cc:CostCenter) REQUIRE cc.code IS UNIQUE;

CREATE CONSTRAINT tenant_id_unique IF NOT EXISTS
FOR (t:Tenant) REQUIRE t.tenant_id IS UNIQUE;

CREATE CONSTRAINT application_name_unique IF NOT EXISTS
FOR (a:Application) REQUIRE a.name IS UNIQUE;

CREATE CONSTRAINT environment_name_unique IF NOT EXISTS
FOR (e:Environment) REQUIRE e.name IS UNIQUE;

CREATE INDEX resource_type_idx IF NOT EXISTS
FOR (r:Resource) ON (r.resource_type);

CREATE INDEX resource_account_idx IF NOT EXISTS
FOR (r:Resource) ON (r.account_id);

CREATE INDEX resource_region_idx IF NOT EXISTS
FOR (r:Resource) ON (r.region);
