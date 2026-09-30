// Pagina Inventario: POST agent1 /extract/config-inventory + tabella filtrabile
let resources = [];
let jobsById = {};

const $ = (id) => document.getElementById(id);

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function shortId(arn) {
  const tail = arn.split(":").slice(5).join(":") || arn;
  return tail.includes("/") ? tail.slice(tail.indexOf("/") + 1) : tail;
}

function analysis(r) {
  return (r.attributes && r.attributes.tagging_analysis) || {};
}

async function loadJobs() {
  const select = $("job-select");
  try {
    const jobs = await api.listJobs();
    jobsById = Object.fromEntries(jobs.map((j) => [j.job_id, j]));
    select.innerHTML = jobs
      .map((j) => `<option value="${esc(j.job_id)}">${esc(j.tenant_id)} — ${esc(j.account_id)} — ${esc(j.region)} (${esc(j.phase)})</option>`)
      .join("");
    const last = new URLSearchParams(location.search).get("job_id") || localStorage.getItem("last_job_id");
    if (last && jobsById[last]) select.value = last;
    if (!jobs.length) select.innerHTML = '<option value="">Nessun job: crearne uno in Setup Account</option>';
  } catch (err) {
    select.innerHTML = '<option value="">Orchestrator non raggiungibile</option>';
    $("inv-status").textContent = "Errore caricamento job: " + err.message;
  }
}

async function extract(e) {
  e.preventDefault();
  const job = jobsById[$("job-select").value];
  if (!job) return;
  const region = $("inv-region").value.trim() || job.region || "all";
  const btn = $("btn-extract");
  btn.disabled = true;
  const started = Date.now();
  const timer = setInterval(() => {
    $("inv-status").textContent = `Estrazione in corso su ${job.account_id} (${region})… ${Math.round((Date.now() - started) / 1000)} s`;
  }, 1000);
  try {
    const res = await api.extractConfigInventory({ job_id: job.job_id, account_id: job.account_id, region });
    resources = res.resources || [];
    $("inv-status").textContent =
      `Completato in ${Math.round((Date.now() - started) / 1000)} s: ${res.count} risorse in ${(res.regions || []).length} regioni.`;
    render(res);
  } catch (err) {
    $("inv-status").textContent = "Errore: " + err.message;
  } finally {
    clearInterval(timer);
    btn.disabled = false;
  }
}

function render(res) {
  const nonCompliant = resources.filter((r) => analysis(r).compliant === false).length;
  const byRegion = {};
  resources.forEach((r) => { byRegion[r.region] = (byRegion[r.region] || 0) + 1; });
  const types = Object.keys(res.resource_types || {}).sort();

  $("stats").innerHTML = [
    ["Risorse", resources.length],
    ["Regioni", Object.keys(byRegion).length],
    ["Tipi di risorsa", types.length],
    ["Non conformi (tag obbligatori)", nonCompliant],
    ["Conformità", resources.length ? Math.round(100 * (resources.length - nonCompliant) / resources.length) + "%" : "—"],
  ].map(([label, value]) => `<div class="stat"><div class="stat-value">${esc(value)}</div><div class="stat-label">${esc(label)}</div></div>`).join("");
  $("region-counts").innerHTML = Object.entries(byRegion).sort()
    .map(([region, n]) => `<span class="chip">${esc(region)}: ${n}</span>`).join("");

  $("f-region").innerHTML = '<option value="">Tutte</option>' +
    Object.keys(byRegion).sort().map((r) => `<option>${esc(r)}</option>`).join("");
  $("f-type").innerHTML = '<option value="">Tutti</option>' +
    types.map((t) => `<option>${esc(t)}</option>`).join("");

  $("inv-summary").style.display = "block";
  $("inv-results").style.display = "block";
  applyFilters();
}

function filtered() {
  const region = $("f-region").value;
  const type = $("f-type").value;
  const text = $("f-text").value.trim().toLowerCase();
  const onlyBad = $("f-noncompliant").checked;
  return resources.filter((r) => {
    if (region && r.region !== region) return false;
    if (type && r.resource_type !== type) return false;
    if (onlyBad && analysis(r).compliant !== false) return false;
    if (text) {
      const hay = (r.resource_id + " " + JSON.stringify(r.current_tags || {})).toLowerCase();
      if (!hay.includes(text)) return false;
    }
    return true;
  });
}

function applyFilters() {
  const rows = filtered();
  const max = 1000;
  $("shown-count").textContent = `${rows.length} risorse` + (rows.length > max ? ` (mostrate le prime ${max}, usa i filtri o il CSV)` : "");
  $("inv-table").querySelector("tbody").innerHTML = rows.slice(0, max).map((r, i) => {
    const a = analysis(r);
    const suggestions = Object.entries(a.suggestions || {}).map(([k, v]) => `${k}=${v}`).join(", ");
    return `<tr data-idx="${resources.indexOf(r)}">
      <td>${esc(r.region)}</td>
      <td title="${esc(r.resource_type)}">${esc(r.resource_type)}</td>
      <td title="${esc(r.resource_id)}">${esc(shortId(r.resource_id))}</td>
      <td title="${esc((r.current_tags || {}).Name)}">${esc((r.current_tags || {}).Name)}</td>
      <td>${a.compliant === false ? '<span class="badge badge-bad">NO</span>' : '<span class="badge badge-ok">SI</span>'}</td>
      <td title="${esc((a.missing_mandatory || []).join(", "))}">${esc((a.missing_mandatory || []).join(", "))}</td>
      <td class="muted" title="${esc(suggestions)}">${esc(suggestions)}</td>
    </tr>`;
  }).join("");
}

function showDetail(e) {
  const tr = e.target.closest("tr[data-idx]");
  if (!tr) return;
  const r = resources[Number(tr.dataset.idx)];
  $("res-detail").style.display = "block";
  $("res-detail-content").textContent = JSON.stringify(r, null, 2);
  $("res-detail").scrollIntoView({ behavior: "smooth" });
}

function downloadCsv() {
  const cols = ["region", "resource_type", "resource_id", "name", "compliant", "missing_mandatory", "missing_recommended", "tags"];
  const quote = (v) => `"${String(v ?? "").replace(/"/g, '""')}"`;
  const lines = [cols.join(";")].concat(filtered().map((r) => {
    const a = analysis(r);
    return [r.region, r.resource_type, r.resource_id, (r.current_tags || {}).Name, a.compliant === false ? "NO" : "SI",
      (a.missing_mandatory || []).join(", "), (a.missing_recommended || []).join(", "), JSON.stringify(r.current_tags || {})]
      .map(quote).join(";");
  }));
  const blob = new Blob(["﻿" + lines.join("\r\n")], { type: "text/csv;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `inventario_${new Date().toISOString().slice(0, 10)}.csv`;
  a.click();
  URL.revokeObjectURL(a.href);
}

$("form-inventory").addEventListener("submit", extract);
["f-region", "f-type", "f-noncompliant"].forEach((id) => $(id).addEventListener("change", applyFilters));
$("f-text").addEventListener("input", applyFilters);
$("inv-table").addEventListener("click", showDetail);
$("btn-csv").addEventListener("click", downloadCsv);
loadJobs();
