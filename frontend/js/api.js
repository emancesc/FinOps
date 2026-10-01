// Wrapper fetch verso il backend orchestrator
const ORCHESTRATOR_URL = "http://localhost:8000";
const AGENT1_URL = "http://localhost:8001";
const AGENT2_URL = "http://localhost:8002";
const AGENT3_URL = "http://localhost:8003";
const AGENT4_URL = "http://localhost:8004";

async function apiFetch(baseUrl, path, options = {}) {
  const res = await fetch(`${baseUrl}${path}`, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`${res.status} ${res.statusText}: ${body}`);
  }
  return res.json();
}

// Upload multipart (senza Content-Type esplicito: lo imposta il browser con il boundary)
async function uploadForm(baseUrl, path, formData) {
  const res = await fetch(`${baseUrl}${path}`, { method: "POST", body: formData });
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`${res.status} ${res.statusText}: ${body}`);
  }
  return res.json();
}

// Jobs
const api = {
  createJob: (payload) => apiFetch(ORCHESTRATOR_URL, "/jobs", { method: "POST", body: JSON.stringify(payload) }),
  getJob: (jobId) => apiFetch(ORCHESTRATOR_URL, `/jobs/${jobId}`),
  listJobs: () => apiFetch(ORCHESTRATOR_URL, "/jobs"),

  // Inventario (agent1): tutte le risorse AWS Config multi-regione + analisi tagging
  extractConfigInventory: (payload) =>
    apiFetch(AGENT1_URL, "/extract/config-inventory", { method: "POST", body: JSON.stringify(payload) }),

  // Documenti di progetto (Design / Assessment) del job
  uploadDocument: (jobId, formData) => uploadForm(AGENT2_URL, `/documents/${jobId}`, formData),
  listDocuments: (jobId) => apiFetch(AGENT2_URL, `/documents/${jobId}`),
  deleteDocument: (documentId) => apiFetch(AGENT2_URL, `/documents/item/${documentId}`, { method: "DELETE" }),

  // Proposta di tagging (strategy attiva + inventario + JSON a corredo + documenti)
  proposalReadiness: (jobId) => apiFetch(AGENT2_URL, `/proposals/readiness?job_id=${jobId}`),
  generateProposals: (payload) => apiFetch(AGENT2_URL, "/proposals/generate", { method: "POST", body: JSON.stringify(payload) }),
  listProposalRuns: (jobId) => apiFetch(AGENT2_URL, `/proposals/runs?job_id=${jobId}`),
  getProposalRun: (runId) => apiFetch(AGENT2_URL, `/proposals/runs/${runId}`),
  resumeProposalRun: (runId) => apiFetch(AGENT2_URL, `/proposals/runs/${runId}/resume`, { method: "POST" }),
  importProposals: (formData) => uploadForm(AGENT2_URL, "/proposals/import", formData),

  // Tag proposals
  listProposals: (jobId, filters = {}) =>
    apiFetch(AGENT2_URL, `/proposals?${new URLSearchParams({ job_id: jobId, ...filters })}`),
  reviewProposal: (proposalId, payload) =>
    apiFetch(AGENT2_URL, `/proposals/${proposalId}`, { method: "PATCH", body: JSON.stringify(payload) }),
  bulkReviewProposals: (jobId, ids, reviewStatus) =>
    apiFetch(AGENT2_URL, "/proposals/bulk-review", {
      method: "POST", body: JSON.stringify({ job_id: jobId, ids, review_status: reviewStatus }),
    }),
  proposalsExportUrl: (jobId, runId) =>
    `${AGENT2_URL}/proposals/export.xlsx?${new URLSearchParams(runId ? { job_id: jobId, run_id: runId } : { job_id: jobId })}`,

  // Registro Tagging Strategy (agent3)
  listStrategies: () => apiFetch(AGENT3_URL, "/strategies"),
  getStrategy: (id) => apiFetch(AGENT3_URL, `/strategies/${id}`),
  uploadStrategy: (formData) => uploadForm(AGENT3_URL, "/strategies", formData),
  updateStrategy: (id, payload) => apiFetch(AGENT3_URL, `/strategies/${id}`, { method: "PATCH", body: JSON.stringify(payload) }),
  extractStrategy: (id, fresh = false) => apiFetch(AGENT3_URL, `/strategies/${id}/extract?fresh=${fresh}`, { method: "POST" }),
  activateStrategy: (id) => apiFetch(AGENT3_URL, `/strategies/${id}/activate`, { method: "POST" }),
  approveAllStrategy: (id) => apiFetch(AGENT3_URL, `/strategies/${id}/approve-all`, { method: "POST" }),
  reviewStrategyItem: (id, kind, itemId, status) =>
    apiFetch(AGENT3_URL, `/strategies/${id}/${kind}/${itemId}`, { method: "PATCH", body: JSON.stringify({ status }) }),
  deleteStrategy: (id) => apiFetch(AGENT3_URL, `/strategies/${id}`, { method: "DELETE" }),

  // Regole
  listRules: (tenantId) => apiFetch(AGENT3_URL, `/rules?tenant_id=${tenantId}`),
  approveRule: (ruleId) => apiFetch(AGENT3_URL, `/rules/${ruleId}/approve`, { method: "POST" }),
  rejectRule: (ruleId) => apiFetch(AGENT3_URL, `/rules/${ruleId}/reject`, { method: "POST" }),

  // Grafo
  buildGraph: (jobId) => apiFetch(AGENT4_URL, `/graph/build`, { method: "POST", body: JSON.stringify({ job_id: jobId }) }),
  getGraphResource: (arn) => apiFetch(AGENT4_URL, `/graph/resource/${encodeURIComponent(arn)}`),
  getGraphGroup: (dimension, value) => apiFetch(AGENT4_URL, `/graph/group?dimension=${dimension}&value=${encodeURIComponent(value)}`),
  searchGraph: (q) => apiFetch(AGENT4_URL, `/graph/search?q=${encodeURIComponent(q)}`),
};

window.api = api;
