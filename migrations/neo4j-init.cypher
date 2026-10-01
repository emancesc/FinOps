// Vincoli di unicità e indici per il Knowledge Graph FinOps
// Eseguito una sola volta al bootstrap dal container neo4j-init

// Risorse della piattaforma: label dedicata (la label "Resource" è usata da tagsviewer)
// e chiave (account_id, arn), perché alcuni ARN gestiti da AWS sono uguali in ogni account
CREATE CONSTRAINT finops_resource_key IF NOT EXISTS
FOR (r:FinopsResource) REQUIRE (r.account_id, r.arn) IS UNIQUE;

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

CREATE INDEX finops_resource_arn IF NOT EXISTS
FOR (r:FinopsResource) ON (r.arn);

CREATE INDEX finops_resource_type IF NOT EXISTS
FOR (r:FinopsResource) ON (r.resource_type);

CREATE INDEX finops_resource_region IF NOT EXISTS
FOR (r:FinopsResource) ON (r.region);

CREATE INDEX finops_resource_job IF NOT EXISTS
FOR (r:FinopsResource) ON (r.job_id);
