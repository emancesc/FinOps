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

// Jobs
const api = {
  createJob: (payload) => apiFetch(ORCHESTRATOR_URL, "/jobs", { method: "POST", body: JSON.stringify(payload) }),
  getJob: (jobId) => apiFetch(ORCHESTRATOR_URL, `/jobs/${jobId}`),
  listJobs: () => apiFetch(ORCHESTRATOR_URL, "/jobs"),

  // Documenti
  uploadDocument: (jobId, formData) =>
    fetch(`${AGENT2_URL}/documents/${jobId}`, { method: "POST", body: formData }).then((r) => r.json()),

  // Tag proposals
  listProposals: (jobId) => apiFetch(AGENT2_URL, `/proposals?job_id=${jobId}`),
  reviewProposal: (proposalId, payload) =>
    apiFetch(AGENT2_URL, `/proposals/${proposalId}`, { method: "PATCH", body: JSON.stringify(payload) }),

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
