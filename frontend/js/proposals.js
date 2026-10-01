// Pagina Proposta Tagging: prerequisiti, documenti Design/Assessment, generazione e avanzamento
const $ = (id) => document.getElementById(id);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
let jobId = null;
let pollTimer = null;

const RUN_STATUS = { queued: "in coda", running: "in corso", done: "completata", error: "interrotta", cancelled: "annullata" };

async function loadJobs() {
  const jobs = await api.listJobs().catch(() => []);
  const wanted = new URLSearchParams(location.search).get("job_id") || localStorage.getItem("last_job_id");
  $("job-select").innerHTML = jobs.length
    ? jobs.map((j) => `<option value="${esc(j.job_id)}">${esc(j.tenant_id)} — ${esc(j.account_id)} — ${esc(j.region)} (${esc(j.phase)})</option>`).join("")
    : '<option value="">Nessun job: crearne uno in Setup Account</option>';
  if (wanted && jobs.some((j) => j.job_id === wanted)) $("job-select").value = wanted;
  jobId = $("job-select").value || null;
  refresh();
}

function check(ok, title, detail) {
  return `<li class="${ok ? "ok" : "ko"}"><span class="mark">${ok ? "✓" : "✗"}</span><div><strong>${title}</strong><div class="muted">${detail}</div></div></li>`;
}

async function loadReadiness() {
  if (!jobId) return null;
  let r;
  try {
    r = await api.proposalReadiness(jobId);
  } catch (err) {
    $("checklist").innerHTML = `<li class="ko">Errore: ${esc(err.message)}</li>`;
    $("btn-generate").disabled = true;
    return null;
  }
  const s = r.strategy;
  $("checklist").innerHTML = [
    check(r.inventory.ok, "Inventario estratto",
      r.inventory.ok ? `${r.inventory.resources} risorse nel job` : `Nessuna risorsa: estrai l'inventario dalla pagina <a href="inventory.html?job_id=${jobId}">Inventario</a>`),
    check(r.linked_json.ok, "JSON a corredo dell'account",
      r.linked_json.ok ? `Regioni: ${r.linked_json.regions.map(esc).join(", ")} (${esc(r.linked_json.path)})`
        : `Esegui <code>python scripts\\extract_linked_resources.py --profile &lt;profilo&gt; --account ${esc(r.job.account_id)}</code>`),
    check(s.ok, "Tagging Strategy attiva",
      s.ok ? `${esc(s.name)} rev. ${esc(s.revision)} (rilascio ${esc(s.release_date || "n/d")}) — ${s.tags} tag, ${s.rules} regole`
        : 'Carica e attiva una strategy nella pagina <a href="rules.html">Regole</a>'),
    check(r.documents.count > 0, "Documenti di Design / Assessment (facoltativi)",
      r.documents.count ? `${r.documents.count} documenti, ${r.documents.chars.toLocaleString("it-IT")} caratteri indicizzati`
        : "Nessun documento: la proposta userà solo inventario, evidenze e strategy"),
  ].join("");
  $("btn-generate").disabled = !r.ready || !!r.running;
  $("estimate").textContent = r.ready
    ? `Stima: fino a ${r.estimate.resources} risorse con una sola chiamata LLM `
      + "(volumi/ENI che ereditano i tag dall'istanza e risorse già conformi non vengono passati al modello)."
      + (r.running ? " Una generazione è già in corso." : "")
    : "Generazione disponibile quando tutti i prerequisiti obbligatori sono soddisfatti.";
  return r;
}

async function loadDocuments() {
  if (!jobId) return;
  const docs = await api.listDocuments(jobId).catch(() => []);
  $("docs-table").querySelector("tbody").innerHTML = docs.length ? docs.map((d) => `
    <tr><td>${esc(d.file_name)}</td><td>${esc(d.doc_type)}</td><td>${d.chunks}</td><td>${Number(d.chars).toLocaleString("it-IT")}</td>
      <td>${esc((d.created_at || "").slice(0, 16).replace("T", " "))}</td>
      <td><button class="btn btn-danger btn-sm" onclick="removeDoc('${d.document_id}')">Elimina</button></td></tr>`).join("")
    : '<tr><td colspan="6" class="muted">Nessun documento caricato per questo job.</td></tr>';
}

async function loadRuns() {
  if (!jobId) return;
  const runs = await api.listProposalRuns(jobId).catch(() => []);
  $("runs-table").querySelector("tbody").innerHTML = runs.length ? runs.map((r) => `
    <tr>
      <td>${esc((r.created_at || "").slice(0, 16).replace("T", " "))}</td>
      <td><span class="badge ${r.status === "done" ? "badge-approved" : r.status === "error" ? "badge-rejected" : "badge-pending"}">${esc(RUN_STATUS[r.status] || r.status)}</span>
        ${r.error ? `<div class="muted" title="${esc(r.error)}">${esc(r.error.slice(0, 100))}</div>` : ""}</td>
      <td style="min-width:180px"><div class="progress-bar"><div class="progress-bar-fill" style="width:${r.progress_pct}%"></div></div>
        <span class="muted">${r.progress_pct}% — ${r.resources_done}/${r.resources_total} risorse${r.message ? " — " + esc(r.message) : ""}</span></td>
      <td>${r.proposals_saved}</td><td>${r.llm_calls}</td>
      <td>${Number(r.input_tokens).toLocaleString("it-IT")} / ${Number(r.output_tokens).toLocaleString("it-IT")}</td>
      <td class="muted">${esc(Object.entries(r.options || {}).map(([k, v]) => `${k}: ${[].concat(v).join(",")}`).join("; ") || "—")}</td>
      <td style="white-space:nowrap">
        ${r.status === "error" ? `<button class="btn btn-secondary btn-sm" onclick="resumeRun('${r.run_id}')">Riprendi</button>` : ""}
        <a class="btn btn-secondary btn-sm" href="review.html?job_id=${jobId}&run_id=${r.run_id}">Rivedi</a>
      </td>
    </tr>`).join("") : '<tr><td colspan="8" class="muted">Nessuna esecuzione.</td></tr>';
  clearTimeout(pollTimer);
  if (runs.some((r) => r.status === "running" || r.status === "queued")) {
    pollTimer = setTimeout(() => { loadRuns(); loadReadiness(); }, 3000);
  }
}

async function refresh() {
  if (!jobId) return;
  localStorage.setItem("last_job_id", jobId);
  await Promise.all([loadReadiness(), loadDocuments(), loadRuns()]);
}

async function removeDoc(id) {
  if (!confirm("Eliminare il documento dal job?")) return;
  try { await api.deleteDocument(id); refresh(); } catch (err) { alert("Errore: " + err.message); }
}

async function resumeRun(id) {
  try { await api.resumeProposalRun(id); loadRuns(); } catch (err) { alert("Errore: " + err.message); }
}

$("job-select").addEventListener("change", () => { jobId = $("job-select").value; refresh(); });

$("form-doc").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = new FormData();
  form.append("file", $("doc-file").files[0]);
  form.append("doc_type", $("doc-type").value);
  $("btn-doc").disabled = true;
  $("doc-status").textContent = "Caricamento e indicizzazione…";
  try {
    const d = await api.uploadDocument(jobId, form);
    $("doc-status").textContent = `Indicizzato: ${d.file_name} (${d.chunks} blocchi).`;
    $("form-doc").reset();
    refresh();
  } catch (err) {
    $("doc-status").textContent = "Errore: " + err.message;
  } finally {
    $("btn-doc").disabled = false;
  }
});

$("form-generate").addEventListener("submit", async (e) => {
  e.preventDefault();
  const split = (v) => v.split(",").map((x) => x.trim()).filter(Boolean);
  const payload = { job_id: jobId };
  if ($("g-regions").value.trim()) payload.regions = split($("g-regions").value);
  if ($("g-types").value.trim()) payload.resource_types = split($("g-types").value);
  if ($("g-max").value) payload.max_resources = Number($("g-max").value);
  $("btn-generate").disabled = true;
  try {
    await api.generateProposals(payload);
    await refresh();
  } catch (err) {
    alert("Errore: " + err.message);
    loadReadiness();
  }
});

document.querySelectorAll(".info-toggle").forEach((btn) => btn.addEventListener("click", () => {
  const box = $(btn.dataset.target);
  box.hidden = !box.hidden;
  btn.setAttribute("aria-expanded", String(!box.hidden));
}));

loadJobs();
