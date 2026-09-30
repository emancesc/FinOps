// Pagina Regole: registro Tagging Strategy (agent3) + regole apprese dall'arbitraggio
const $ = (id) => document.getElementById(id);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
let selectedId = new URLSearchParams(location.search).get("strategy_id");
let pollTimer = null;

const STATUS_LABEL = { uploaded: "caricata", extracting: "estrazione in corso", extracted: "regole estratte", error: "errore" };
const CATEGORY_LABEL = { cost_allocation: "Cost allocation", operational: "Operativo", other: "Altro" };
const RULE_LABEL = {
  value_constraint: "Vincolo sui valori", resource_type: "Tipo di risorsa", naming_pattern: "Pattern di nome",
  shared_resource: "Risorsa condivisa", example: "Esempio reale", guideline: "Principio", other: "Altro",
};

function badge(status) {
  const cls = { proposed: "badge-proposed", approved: "badge-approved", rejected: "badge-rejected",
    extracted: "badge-approved", extracting: "badge-pending", uploaded: "badge-pending", error: "badge-rejected" }[status] || "";
  return `<span class="badge ${cls}">${esc(STATUS_LABEL[status] || status)}</span>`;
}

function progress(s) {
  if (s.status !== "extracting" && s.status !== "error") return "";
  return `<div class="progress-bar" style="margin-top:0.3rem"><div class="progress-bar-fill" style="width:${s.progress_pct}%"></div></div>
    <span class="muted">${s.progress_pct}% — blocco ${s.chunks_done}/${s.chunks_total || "?"}</span>`;
}

async function loadStrategies() {
  let list;
  try {
    list = await api.listStrategies();
  } catch (err) {
    $("strategies").innerHTML = `<p>Errore: ${esc(err.message)}</p>`;
    return;
  }
  if (!list.length) {
    $("strategies").innerHTML = '<p class="muted">Nessuna Tagging Strategy caricata.</p>';
    return;
  }
  // Raggruppate per nome; revisioni ordinate per data di rilascio (più recente prima)
  const groups = {};
  list.forEach((s) => (groups[s.name] = groups[s.name] || []).push(s));
  $("strategies").innerHTML = Object.entries(groups).map(([name, revs]) => `
    <h3 class="group-title">${esc(name)}</h3>
    <table class="compact">
      <thead><tr><th>Revisione</th><th>Data di rilascio</th><th>Stato</th><th>Tag</th><th>Valori ammessi</th><th>Regole</th><th>Documento</th><th></th></tr></thead>
      <tbody>${revs.map((s) => `
        <tr class="${s.strategy_id === selectedId ? "row-selected" : ""}">
          <td><strong>${esc(s.revision)}</strong> ${s.is_active ? '<span class="badge badge-approved">attiva</span>' : ""}</td>
          <td>${esc(s.release_date || "—")}</td>
          <td>${badge(s.status)}${progress(s)}${s.error ? `<div class="muted" title="${esc(s.error)}">${esc(s.error.slice(0, 90))}</div>` : ""}</td>
          <td>${s.tags_count}</td><td>${s.allowed_values_count}</td><td>${s.rules_count}</td>
          <td class="muted">${esc(s.file_name)}</td>
          <td style="white-space:nowrap">
            <button class="btn btn-secondary btn-sm" onclick="selectStrategy('${s.strategy_id}')">Dettaglio</button>
            ${s.status === "error" ? `<button class="btn btn-secondary btn-sm" onclick="reextract('${s.strategy_id}', false)">Riprendi</button>` : ""}
            ${s.status === "extracted" ? `<button class="btn btn-secondary btn-sm" onclick="reextract('${s.strategy_id}', true)">Riestrai</button>` : ""}
            <button class="btn btn-danger btn-sm" onclick="removeStrategy('${s.strategy_id}')">Elimina</button>
          </td>
        </tr>`).join("")}
      </tbody>
    </table>`).join("");

  const running = list.some((s) => s.status === "extracting");
  clearTimeout(pollTimer);
  if (running) {
    pollTimer = setTimeout(() => { loadStrategies(); if (selectedId) loadDetail(); }, 3000);
  } else if (wasRunning && selectedId) {
    loadDetail(); // estrazione appena terminata (o fallita): ultimo aggiornamento del dettaglio
  }
  wasRunning = running;
}
let wasRunning = false;

async function selectStrategy(id) {
  selectedId = id;
  history.replaceState(null, "", `?strategy_id=${id}`);
  await loadStrategies();
  await loadDetail();
  $("detail").scrollIntoView({ behavior: "smooth" });
}

async function loadDetail() {
  if (!selectedId) return;
  let s;
  try {
    s = await api.getStrategy(selectedId);
  } catch (err) {
    $("detail").style.display = "none";
    return;
  }
  $("detail").style.display = "block";
  $("detail-title").innerHTML = `${esc(s.name)} — rev. ${esc(s.revision)} ${s.is_active ? '<span class="badge badge-approved">attiva</span>' : ""}`;
  $("btn-activate").disabled = s.status !== "extracted" || s.is_active;
  $("detail-meta").innerHTML = `
    <form id="form-meta" class="form-row">
      <div class="form-group"><label>Nome</label><input id="m-name" value="${esc(s.name)}" /></div>
      <div class="form-group" style="max-width:130px"><label>Revisione</label><input id="m-revision" value="${esc(s.revision)}" /></div>
      <div class="form-group" style="max-width:170px"><label>Data di rilascio</label><input type="date" id="m-date" value="${esc(s.release_date || "")}" /></div>
      <button type="submit" class="btn btn-secondary">Salva metadati</button>
    </form>
    <p class="muted" style="margin:0.5rem 0">${badge(s.status)} ${progress(s)} Documento: ${esc(s.file_name)} — modello LLM: ${esc(s.llm_model || "—")}
      ${s.extracted_at ? " — estratta il " + esc(s.extracted_at.slice(0, 16).replace("T", " ")) : ""}</p>
    ${s.summary ? `<p>${esc(s.summary)}</p>` : ""}`;
  $("form-meta").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await api.updateStrategy(selectedId, { name: $("m-name").value, revision: $("m-revision").value, release_date: $("m-date").value || null });
      await loadStrategies();
      await loadDetail();
    } catch (err) { alert("Errore: " + err.message); }
  });

  $("tab-tags").innerHTML = s.tags.length ? `
    <table class="compact">
      <thead><tr><th>Tag</th><th>Categoria</th><th>Obblig.</th><th>Billing</th><th>Multi-valore</th><th>Valori ammessi</th><th>Note</th><th>Stato</th><th></th></tr></thead>
      <tbody>${s.tags.map((t) => `
        <tr>
          <td><strong>${esc(t.tag_key)}</strong><div class="muted">${esc(t.source_ref || "")}</div></td>
          <td>${esc(CATEGORY_LABEL[t.category] || t.category)}</td>
          <td>${t.mandatory ? "SI" : "—"}</td><td>${t.billing ? "SI" : "—"}</td>
          <td>${t.multi_value ? `SI (${esc(t.separator || "+")})` : "—"}</td>
          <td><details><summary>${t.allowed_values.length} valori</summary>
            <div class="values">${t.allowed_values.map((v) => `<span class="chip" title="${esc(v.description || "")}">${esc(v.value)}${v.business_unit ? ` <em>${esc(v.business_unit)}</em>` : ""}</span>`).join(" ")}</div>
          </details></td>
          <td class="muted">${(t.notes || []).map(esc).join("<br>")}</td>
          <td>${badge(t.status)}</td>
          <td style="white-space:nowrap">${reviewButtons("tags", t.id, t.status)}</td>
        </tr>`).join("")}</tbody>
    </table>` : '<p class="muted">Nessun tag estratto (ancora).</p>';

  $("tab-rules").innerHTML = s.rules.length ? `
    <table class="compact">
      <thead><tr><th>Tipo</th><th>Tag</th><th>Regola</th><th>Condizione / esito</th><th>Fonte</th><th>Stato</th><th></th></tr></thead>
      <tbody>${s.rules.map((r) => `
        <tr>
          <td>${esc(RULE_LABEL[r.rule_type] || r.rule_type)}</td>
          <td>${(r.tag_keys || []).map(esc).join("<br>")}</td>
          <td><strong>${esc(r.title)}</strong><div>${esc(r.description)}</div></td>
          <td>${Object.keys(r.condition || {}).length ? `<pre class="mini">${esc(JSON.stringify(r.condition, null, 1))}</pre>` : ""}
              ${Object.keys(r.resolution || {}).length ? `<pre class="mini">${esc(JSON.stringify(r.resolution, null, 1))}</pre>` : ""}</td>
          <td class="muted">${esc(r.source_ref || "")}</td>
          <td>${badge(r.status)}</td>
          <td style="white-space:nowrap">${reviewButtons("rules", r.rule_id, r.status)}</td>
        </tr>`).join("")}</tbody>
    </table>` : '<p class="muted">Nessuna regola estratta (ancora).</p>';

  $("tab-changelog").innerHTML = (s.changelog || []).length ? `
    <table class="compact"><thead><tr><th>Versione</th><th>Data</th><th>Autori</th><th>Modifiche</th></tr></thead>
      <tbody>${s.changelog.map((c) => `<tr><td>${esc(c.version)}</td><td>${esc(c.date || "")}</td><td>${esc(c.authors || "")}</td>
        <td>${(c.changes || []).map(esc).join("<br>")}</td></tr>`).join("")}</tbody></table>` : '<p class="muted">Nessun changelog.</p>';
}

function reviewButtons(kind, id, status) {
  return `${status !== "approved" ? `<button class="btn btn-success btn-sm" onclick="reviewItem('${kind}','${id}','approved')">✓</button>` : ""}
          ${status !== "rejected" ? `<button class="btn btn-danger btn-sm" onclick="reviewItem('${kind}','${id}','rejected')">✗</button>` : ""}`;
}

async function reviewItem(kind, id, status) {
  try { await api.reviewStrategyItem(selectedId, kind, id, status); await loadDetail(); } catch (err) { alert("Errore: " + err.message); }
}

async function reextract(id, fresh) {
  if (fresh && !confirm("Riestrarre da capo le regole? Tag e regole attuali (e le revisioni fatte) verranno sostituiti.")) return;
  try { await api.extractStrategy(id, fresh); selectedId = id; await loadStrategies(); await loadDetail(); } catch (err) { alert("Errore: " + err.message); }
}

async function removeStrategy(id) {
  if (!confirm("Eliminare questa revisione della Tagging Strategy con tutte le regole estratte?")) return;
  try {
    await api.deleteStrategy(id);
    if (selectedId === id) { selectedId = null; $("detail").style.display = "none"; }
    await loadStrategies();
  } catch (err) { alert("Errore: " + err.message); }
}

$("form-upload").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = new FormData();
  form.append("file", $("st-file").files[0]);
  if ($("st-name").value.trim()) form.append("name", $("st-name").value.trim());
  if ($("st-revision").value.trim()) form.append("revision", $("st-revision").value.trim());
  if ($("st-date").value) form.append("release_date", $("st-date").value);
  $("btn-upload").disabled = true;
  $("upload-status").textContent = "Caricamento…";
  try {
    const s = await api.uploadStrategy(form);
    $("upload-status").textContent = `Caricata: estrazione delle regole avviata (${s.file_name}).`;
    $("form-upload").reset();
    await selectStrategy(s.strategy_id);
  } catch (err) {
    $("upload-status").textContent = "Errore: " + err.message;
  } finally {
    $("btn-upload").disabled = false;
  }
});

$("btn-activate").addEventListener("click", async () => {
  try { await api.activateStrategy(selectedId); await loadStrategies(); await loadDetail(); } catch (err) { alert("Errore: " + err.message); }
});
$("btn-approve-all").addEventListener("click", async () => {
  try { await api.approveAllStrategy(selectedId); await loadDetail(); } catch (err) { alert("Errore: " + err.message); }
});
document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => {
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t === tab));
  ["tags", "rules", "changelog"].forEach((name) => ($(`tab-${name}`).hidden = name !== tab.dataset.tab));
}));
document.querySelectorAll(".info-toggle").forEach((btn) => btn.addEventListener("click", () => {
  const box = $(btn.dataset.target);
  box.hidden = !box.hidden;
  btn.setAttribute("aria-expanded", String(!box.hidden));
}));

// --- Regole apprese dall'arbitraggio (invariato) -------------------------------
const tenantId = localStorage.getItem("last_tenant_id") || "";
$("tenant-label").textContent = tenantId || "(nessun tenant selezionato)";

function arbiterBadge(status) {
  const cls = { proposed: "badge-proposed", approved: "badge-approved", rejected: "badge-rejected" }[status] || "";
  return `<span class="badge ${cls}">${esc(status)}</span>`;
}

async function loadRules() {
  const tbody = $("rules-body");
  if (!tenantId) { tbody.innerHTML = "<tr><td colspan='6' style='text-align:center'>Nessun tenant: crea un job in Setup Account.</td></tr>"; return; }
  try {
    const rules = await api.listRules(tenantId);
    if (!rules.length) { tbody.innerHTML = "<tr><td colspan='6' style='text-align:center'>Nessuna regola.</td></tr>"; return; }
    tbody.innerHTML = rules.map((r) => `
      <tr>
        <td>${esc(r.tag_key)}</td>
        <td><pre class="mini">${esc(JSON.stringify(r.condition, null, 2))}</pre></td>
        <td><pre class="mini">${esc(JSON.stringify(r.resolution, null, 2))}</pre></td>
        <td>${arbiterBadge(r.status)}</td>
        <td>${esc(r.approved_by || "—")}</td>
        <td>${r.status === "proposed" ? `
          <button class="btn btn-success btn-sm" onclick="approveRule('${r.rule_id}')">Approva</button>
          <button class="btn btn-danger btn-sm" onclick="rejectRule('${r.rule_id}')">Rifiuta</button>` : "—"}</td>
      </tr>`).join("");
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan='6'>Errore: ${esc(err.message)}</td></tr>`;
  }
}
async function approveRule(id) { try { await api.approveRule(id); loadRules(); } catch (err) { alert("Errore: " + err.message); } }
async function rejectRule(id) { try { await api.rejectRule(id); loadRules(); } catch (err) { alert("Errore: " + err.message); } }

loadStrategies().then(loadDetail);
loadRules();
