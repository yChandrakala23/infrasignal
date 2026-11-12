const ME = { views: {} };

const VIEWS = [["overview", "Overview"], ["works", "Works"], ["documents", "Documents"], ["matching", "Matching review"], ["imports", "Import data"], ["roads", "Roads"], ["contractors", "Contractors"], ["evaluation", "Evaluation"]];
let OPTS = null, F = {};

function start(auth) {
  document.body.innerHTML = `<header class="topbar"><a class="brand" href="/citizen"><i></i>InfraSignal</a><span class="area">Government monitoring</span><span class="spacer"></span>
   ${auth.role === "admin" ? '<a class="link" href="/authority">Authority dashboard</a>' : ""}<span class="small" style="color:#c3ccd2">${esc(auth.email)}</span><button class="btn ghost sm" id="out" style="color:#fff;border-color:#46525b">Sign out</button></header>
   <div class="banner" id="banner" hidden></div>
   <div class="shell"><nav class="side" aria-label="Sections">${VIEWS.map(([k, t]) => `<button data-v="${k}" aria-selected="false">${t}<span class="count" id="c-${k}" hidden></span></button>`).join("")}</nav><main class="main" id="view" tabindex="-1"></main></div>`;
  $("#out").onclick = () => { Auth.clear(); location.reload(); };
  $$(".side button").forEach(b => b.onclick = () => go(b.dataset.v));
  api("/api/gov/filters").then(o => { OPTS = o; go((location.hash || "#overview").slice(1)); });
}
function go(v) {
  if (!ME.views[v]) v = "overview";
  history.replaceState(null, "", "#" + v);
  $$(".side button").forEach(b => b.setAttribute("aria-selected", b.dataset.v === v));
  closeDrawer(); const el = $("#view"); el.innerHTML = '<p class="loading">Loading…</p>';
  ME.views[v](el).catch(e => { el.innerHTML = `<div class="notice bad">${esc(e.message)}</div>`; });
}

/* ---------- shared filter bar ---------- */
function filterBar(onChange, keys = ["q", "ward", "contractor", "department", "category", "tender_status", "work_status", "fiscal_year", "date_from", "date_to"]) {
  const sel = (k, lab, opts) => `<div><label for="f-${k}">${lab}</label><select id="f-${k}" data-k="${k}"><option value="">All</option>${opts.map(o => `<option value="${esc(o.v)}"${F[k] === o.v ? " selected" : ""}>${esc(o.t)}</option>`).join("")}</select></div>`;
  const txt = (k, lab, type = "text") => `<div><label for="f-${k}">${lab}</label><input id="f-${k}" data-k="${k}" type="${type}" value="${esc(F[k] || "")}"></div>`;
  const parts = {
    q: txt("q", "Search description"), ward: sel("ward", "Ward", OPTS.wards.map(w => ({ v: w.number, t: `${w.number} ${w.name || ""} (${w.regime})` }))),
    contractor: sel("contractor", "Contractor", OPTS.contractors.map(c => ({ v: c, t: c }))), department: sel("department", "Department", OPTS.departments.map(c => ({ v: c, t: c }))),
    category: sel("category", "Category", OPTS.categories.map(c => ({ v: c, t: label(c) }))), tender_status: sel("tender_status", "Tender status", OPTS.tender_statuses.map(c => ({ v: c, t: label(c) }))),
    work_status: sel("work_status", "Work status (as stated)", OPTS.work_statuses.map(c => ({ v: c.key, t: c.label }))), fiscal_year: sel("fiscal_year", "Fiscal year", OPTS.fiscal_years.map(c => ({ v: c, t: c }))),
    date_from: txt("date_from", "Work order from", "date"), date_to: txt("date_to", "Work order to", "date"),
  };
  const box = document.createElement("div");
  box.innerHTML = `<div class="filters" role="search" aria-label="Filters">${keys.map(k => parts[k]).join("")}<div><button class="btn ghost sm" id="f-clear">Clear filters</button></div></div>`;
  $$("[data-k]", box).forEach(i => i.onchange = () => { F[i.dataset.k] = i.value; onChange(); });
  $("#f-clear", box).onclick = () => { F = {}; onChange(true); };
  return box;
}

/* ---------- overview ---------- */
ME.views.overview = async el => {
  const draw = async () => {
    const d = await api("/api/gov/dashboard" + qs(F)), t = d.totals, g = d.gaps;
    $("#dash").innerHTML = `
    <div class="grid g4">
      <div class="stat"><b>${t.roadworks}</b><span>Road works in view${t.sample_roadworks ? " (" + t.sample_roadworks + " sample)" : ""}</span></div>
      <div class="stat"><b>${t.contractors}</b><span>Contractors</span></div>
      <div class="stat"><b>${crore(t.awarded_amount)}</b><span>Awarded amount (as published)</span></div>
      <div class="stat"><b>${crore(t.paid_amount_recorded)}</b><span>Payments recorded (${t.payments}). Not a measure of completion</span></div>
      <div class="stat"><b>${t.tenders}</b><span>Tender records</span></div>
      <div class="stat"><b>${t.documents}</b><span>Documents ingested</span></div>
      <div class="stat warn"><b>${d.deadlines.past_deadline_no_completion_record}</b><span>Past stated deadline, no completion certificate in records</span></div>
      <div class="stat"><b>${d.deadlines.due_in_30_days}</b><span>Deadlines in the next 30 days</span></div>
    </div>
    <div class="grid g2" style="margin-top:14px">
      <div class="card"><h3>Works by ward</h3><div class="chart-box"><canvas id="ch-ward"></canvas></div></div>
      <div class="card"><h3>Stated work status</h3><div class="chart-box"><canvas id="ch-status"></canvas></div><p class="small muted">Only what a document states. "Not stated" means no such document is loaded, not that the work is undone.</p></div>
      <div class="card"><h3>Works by category</h3><div class="chart-box"><canvas id="ch-cat"></canvas></div></div>
      <div class="card"><h3>Work orders by month</h3><div class="chart-box"><canvas id="ch-month"></canvas></div></div>
    </div>
    <div class="grid g-main" style="margin-top:14px">
      <div class="card"><h3>Largest contractors in view</h3>${table([{ h: "Contractor", f: r => esc(r.name) }, { h: "Works", cls: "right num", f: r => r.works }, { h: "Awarded", cls: "right num", f: r => crore(r.awarded) }], d.top_contractors, { empty: "No contractor data." })}</div>
      <div class="card"><h3>Gaps and items to verify</h3>
        <dl class="kv"><dt>No document</dt><dd>${g.works_without_documents}</dd><dt>No tender ID</dt><dd>${g.works_without_tender_id}</dd><dt>No payment record</dt><dd>${g.works_without_payment_record}</dd>
        <dt>Unmatched documents</dt><dd>${g.unmatched_documents}</dd><dt>Documents to review</dt><dd>${g.documents_needing_review}</dd><dt>Tenders without a work</dt><dd>${g.tenders_without_work}</dd>
        <dt>Links awaiting review</dt><dd>${Object.entries(g.links_awaiting_review).map(([k, v]) => `${esc(k)}: ${v}`).join(", ") || "0"}</dd></dl>
        ${g.paid_above_award_to_verify.length ? `<h4 style="margin-top:10px">Payments above the awarded amount (to verify)</h4><p class="small muted">May be extra items, variations or data errors. Not a finding.</p>${g.paid_above_award_to_verify.map(x => `<div class="small"><a href="#" data-w="${x.id}">${esc((x.description || "").slice(0, 60))}</a> paid ${crore(x.paid)} vs ${crore(x.awarded)}</div>`).join("")}` : ""}
        <p class="small muted" style="margin-top:10px">${d.deadlines.note}</p></div>
    </div>
    <div class="card" style="margin-top:14px"><h3>Deadlines passed with no completion certificate in the records</h3>${table([{ h: "Job number", f: r => esc(r.job_number || "–") }, { h: "Work", cls: "desc", f: r => `<a href="#" data-w="${r.id}">${esc(r.description)}</a>` }, { h: "Stated deadline", f: r => dt(r.deadline) }], d.deadlines.items, { empty: "None in view." })}</div>
    <div class="card" style="margin-top:14px"><h3>Recent imports</h3>${table([{ h: "File", f: r => esc(r.file) + sample(r.sample) }, { h: "Kind", f: r => esc(r.kind) }, { h: "Rows loaded", cls: "right", f: r => r.ok }, { h: "Duplicates skipped", cls: "right", f: r => r.duplicate || 0 }, { h: "When", f: r => dtt(r.at) }], d.imports)}</div>`;
    $$("[data-w]", el).forEach(a => a.onclick = e => { e.preventDefault(); openWork(+a.dataset.w); });
    const pal = ["#1b4f8f", "#3d7fc4", "#7aa9d8", "#a8610f", "#5b6770", "#0d7a6b", "#b3261e"];
    chart("ch-ward", { type: "bar", data: { labels: d.works_by_ward.map(x => `${x.number} ${x.name || ""}`.trim().slice(0, 18)), datasets: [{ data: d.works_by_ward.map(x => x.count), backgroundColor: "#1b4f8f" }] }, options: { indexAxis: "y", plugins: { legend: { display: false } } } });
    chart("ch-status", { type: "doughnut", data: { labels: d.works_by_status.map(x => x.label), datasets: [{ data: d.works_by_status.map(x => x.count), backgroundColor: pal, borderWidth: 2 }] }, options: { plugins: { legend: { position: "bottom" } } } });
    chart("ch-cat", { type: "bar", data: { labels: d.works_by_category.map(x => label(x.key)), datasets: [{ data: d.works_by_category.map(x => x.count), backgroundColor: "#3d7fc4" }] }, options: { plugins: { legend: { display: false } } } });
    chart("ch-month", { type: "line", data: { labels: d.works_by_month.map(x => x.month), datasets: [{ data: d.works_by_month.map(x => x.count), borderColor: "#1b4f8f", backgroundColor: "#1b4f8f33", fill: true, tension: .2 }] }, options: { plugins: { legend: { display: false } } } });
    if (t.sample_roadworks) { const b = $("#banner"); b.hidden = false; b.innerHTML = "Sample data is loaded. Records marked <b>SAMPLE</b> are synthetic, clearly labelled, and are not real government records."; }
  };
  el.innerHTML = `<h1>Overview</h1><p class="muted">Counts follow the filters. Provenance and link status are kept for every record.</p><div id="fb"></div><div id="dash" class="loading">Loading…</div>`;
  const rebuild = reset => { if (reset) { $("#fb").replaceChildren(filterBar(rebuild)); } draw(); };
  $("#fb").replaceChildren(filterBar(rebuild)); await draw();
};

/* ---------- works ---------- */
ME.views.works = async el => {
  let page = 1, sort = "newest";
  el.innerHTML = `<div class="row between"><h1>Road works</h1><div class="row"><label for="sort" style="margin:0">Sort</label><select id="sort" style="width:auto"><option value="newest">Newest first</option><option value="amount">Largest amount</option><option value="deadline">Earliest deadline</option><option value="date">Latest work order</option></select></div></div>
   <div id="fb"></div><div class="row" style="margin-bottom:8px"><div style="flex:1;min-width:200px"><label for="ids" style="margin:0" class="small">Job number, tender ID or work order</label><input id="ids" type="search" value="${esc(F.id_search || "")}"></div>
   <div><label for="flag" class="small" style="margin:0">Gap filter</label><select id="flag"><option value="">None</option><option value="no_documents">No linked document</option><option value="no_tender">No tender ID</option><option value="no_payment">No payment record</option><option value="past_deadline_no_completion">Past deadline, no completion certificate</option></select></div></div><div id="list"></div>`;
  const draw = async () => {
    F.id_search = $("#ids").value; F.flag = $("#flag").value;
    const r = await api("/api/gov/roadworks" + qs({ ...F, sort, page }));
    const cols = [{ h: "Job number", f: x => `<span class="nowrap">${esc(x.job_number || "–")}</span>` + sample(x.sample) }, { h: "Work", cls: "desc", f: x => esc(x.description) },
      { h: "Ward", f: x => x.multi_ward ? '<span class="muted">multi-ward</span>' : esc(x.ward || "–") }, { h: "Contractor", f: x => esc(x.contractor || "–") },
      { h: "Awarded", cls: "right num", f: x => crore(x.awarded_amount) }, { h: "Paid (recorded)", cls: "right num", f: x => x.paid ? crore(x.paid) : "–" },
      { h: "Stated status", f: x => `<span class="badge b-neutral">${esc(x.work_status_label)}</span>` }, { h: "Docs", cls: "right", f: x => x.documents }];
    $("#list").innerHTML = `<p class="muted small">${r.total} work(s). Row opens the full profile.</p>` + table(cols, r.items, { click: true, empty: "No works match these filters." }) +
      `<div class="pager"><button class="btn ghost sm" id="pv" ${page <= 1 ? "disabled" : ""}>Previous</button><span class="small">Page ${page} of ${Math.max(1, Math.ceil(r.total / r.page_size))}</span><button class="btn ghost sm" id="nx" ${page * r.page_size >= r.total ? "disabled" : ""}>Next</button></div>`;
    onRows($("#list"), r.items, x => openWork(x.id));
    $("#pv").onclick = () => { page--; draw(); }; $("#nx").onclick = () => { page++; draw(); };
  };
  const rebuild = reset => { if (reset) { $("#fb").replaceChildren(filterBar(rebuild)); $("#ids").value = ""; $("#flag").value = ""; } page = 1; draw(); };
  $("#fb").replaceChildren(filterBar(rebuild));
  $("#sort").onchange = e => { sort = e.target.value; draw(); }; $("#ids").onchange = () => { page = 1; draw(); }; $("#flag").onchange = () => { page = 1; draw(); };
  await draw();
};

async function openWork(id) {
  const p = await api("/api/gov/roadworks/" + id), w = p.roadwork;
  const d = drawer(`
   <h2 style="padding-right:70px">${esc(w.description)}</h2><p>${sample(w.sample)} <span class="badge b-neutral">${esc(w.work_status_label)}</span></p>
   <dl class="kv"><dt>Job number</dt><dd>${esc(w.job_number || "not stated")}</dd><dt>Tender ID</dt><dd>${esc(w.tender_id || "not stated")}</dd><dt>Work order</dt><dd>${esc(w.work_order_id || "not stated")} ${w.work_order_date ? "(" + dt(w.work_order_date) + ")" : ""}</dd>
   <dt>Work / contract ID</dt><dd>${esc(w.work_id || "not stated")}</dd><dt>Contractor</dt><dd>${esc(p.contractor ? p.contractor.name : "not stated")}</dd><dt>Department</dt><dd>${esc(w.department || "not stated")}</dd>
   <dt>Ward</dt><dd>${w.multi_ward ? "Multi-ward / non-geographic" : esc((w.ward || "not stated") + " " + (w.ward_name || "")) + " (" + esc(w.regime || "?") + " regime)"}</dd>
   <dt>Road / location</dt><dd>${esc(w.road_name_extracted || w.location_text || "not stated")}</dd>
   <dt>Estimated cost</dt><dd>${inr(w.estimated_cost)}</dd><dt>Awarded amount</dt><dd>${inr(w.awarded_amount)}</dd>
   <dt>Commencement</dt><dd>${dt(w.commencement_date)}</dd><dt>Completion deadline</dt><dd>${dt(w.completion_deadline)}</dd>
   <dt>Status source</dt><dd>${esc(w.work_status_source || "no document states a status")}</dd><dt>Data source</dt><dd>${esc(w.source_name || "–")}</dd></dl>
   ${p.missing_fields.length ? `<div class="notice" style="margin-top:10px">Not stated in the loaded records: ${p.missing_fields.map(x => esc(label(x))).join(", ")}. Missing is not the same as wrong or hidden.</div>` : ""}
   <h3 style="margin-top:16px">Lifecycle evidence</h3><ul class="stages">${p.lifecycle.map(s => `<li class="${s.available ? "on" : ""}"><b>${esc(s.stage)}</b><small>${s.evidence ? esc(s.evidence) : "No evidence in the loaded records"}</small></li>`).join("")}</ul>
   <h3 style="margin-top:16px">Documents</h3>${table([{ h: "Document", f: x => `<a href="#" data-d="${x.id}">${esc(x.filename)}</a>${sample(x.sample)}` }, { h: "Type", f: x => esc(x.type_label) }, { h: "Date", f: x => dt(x.date) }], p.documents, { empty: "No document linked." })}
   ${p.documents_to_review.length ? `<h4 style="margin-top:10px">Documents suggested for this work</h4>${linkTable(p.documents_to_review)}` : ""}
   <h3 style="margin-top:16px">Tender</h3>${table([{ h: "Tender ID", f: x => esc(x.tender_id) + sample(x.sample) }, { h: "Title", f: x => esc(x.title || "") }, { h: "Status", f: x => esc(x.status || "–") }], p.tenders, { empty: "No tender linked." })}
   <h3 style="margin-top:16px">Bills and payments</h3><p class="small muted">${esc(p.payment_summary.note)}</p>
   ${table([{ h: "Payment", f: x => esc(x.reference || "–") }, { h: "Date", f: x => dt(x.paid_on) }, { h: "Amount", cls: "right num", f: x => inr(x.amount) }], p.payments, { empty: "No payment record." })}
   <h3 style="margin-top:16px">Roads this work is linked to</h3>${p.roads.length ? linkTable(p.roads) : '<p class="muted small">No road linked.</p>'}
   ${p.complaints_nearby.length ? `<h3 style="margin-top:16px">Citizen complaint clusters suggested near this work</h3>${p.complaints_nearby.map(c => `<div class="small">Cluster #${c.cluster_id}: ${c.count} reports ${linkBadge(c.status)}</div>`).join("")}` : ""}
   ${p.source_row ? `<details><summary>Original source row (${esc(p.source_row.import_job || "")}, row ${p.source_row.row})</summary><pre class="json">${esc(JSON.stringify(p.source_row.raw, null, 1))}</pre></details>` : ""}`);
  $$("[data-d]", d).forEach(a => a.onclick = e => { e.preventDefault(); openDoc(+a.dataset.d); });
  wireLinks(d, () => openWork(id));
}
function linkTable(links) {
  return table([{ h: "From", cls: "desc", f: l => esc(l.a_label) }, { h: "To", cls: "desc", f: l => esc(l.b_label) }, { h: "Evidence", f: l => `<span class="small">${esc(l.method)}<br>confidence ${pct(l.confidence)}</span>` }, { h: "Status", f: l => linkBadge(l.status) },
    { h: "", f: l => `<span class="nowrap"><button class="btn sm" data-lk="${l.id}" data-s="verified">Verify</button> <button class="btn ghost sm" data-lk="${l.id}" data-s="rejected">Reject</button></span>` }], links);
}
function wireLinks(root, after) {
  $$("[data-lk]", root).forEach(b => b.onclick = async () => {
    try { await api("/api/gov/links/" + b.dataset.lk, { method: "PATCH", form: { status: b.dataset.s } }); toast(b.dataset.s === "verified" ? "Link verified" : "Link rejected"); after(); } catch (e) { toast(e.message, true); }
  });
}

/* ---------- documents ---------- */
ME.views.documents = async el => {
  let type = "", ms = "", q = "", page = 1;
  el.innerHTML = `<h1>Documents</h1><p class="muted">Upload PDFs (notices, BOQs, sanctions, orders, bills, payment advices, completion certificates). Each file is classified, its fields extracted with page references, and matched to a work only on exact identifiers.</p>
   <div class="dropzone" id="dz" tabindex="0" role="button" aria-label="Choose PDF files to upload"><b>Drop PDF files here or click to choose</b><div class="small muted">Scanned PDFs are read with OCR. Files are never merged into a work unless an identifier matches exactly.</div><input type="file" id="fi" accept="application/pdf" multiple hidden></div>
   <div id="up" style="margin:10px 0"></div>
   <div class="filters"><div><label for="dt">Type</label><select id="dt"><option value="">All</option>${OPTS.doc_types.map(t => `<option value="${t.key}">${esc(t.label)}</option>`).join("")}</select></div>
   <div><label for="dm">Match status</label><select id="dm"><option value="">All</option><option value="auto">Auto-linked</option><option value="verified">Verified</option><option value="manual">Assigned by officer</option><option value="needs_review">Needs review</option><option value="unmatched">Unmatched</option></select></div>
   <div><label for="dq">File name</label><input id="dq" type="search"></div></div><div id="dl"></div>`;
  const dz = $("#dz"), fi = $("#fi");
  dz.onclick = () => fi.click(); dz.onkeydown = e => { if (e.key === "Enter" || e.key === " ") fi.click(); };
  dz.ondragover = e => { e.preventDefault(); dz.classList.add("over"); }; dz.ondragleave = () => dz.classList.remove("over");
  dz.ondrop = e => { e.preventDefault(); dz.classList.remove("over"); upload(e.dataTransfer.files); }; fi.onchange = () => upload(fi.files);
  async function upload(files) {
    if (!files.length) return;
    $("#up").innerHTML = '<p class="loading">Reading, classifying and matching… scanned pages can take a while.</p>';
    const fd = new FormData(); [...files].forEach(f => fd.append("files", f));
    try {
      const r = await api("/api/gov/documents", { method: "POST", body: fd });
      $("#up").innerHTML = table([{ h: "File", f: x => esc(x.filename) }, { h: "Result", f: x => x.error ? `<span class="badge b-bad">${esc(x.error)}</span>` : (x.duplicate_of_existing ? '<span class="badge b-neutral">Already uploaded</span>' : `${esc(x.type_label)} <span class="muted small">(${pct(x.type_confidence)})</span>`) },
        { h: "Match", f: x => x.error ? "" : esc(label(x.match_status)) }], r.results);
      draw();
    } catch (e) { $("#up").innerHTML = `<div class="notice bad">${esc(e.message)}</div>`; }
  }
  const draw = async () => {
    const r = await api("/api/gov/documents" + qs({ doc_type: type, match_status: ms, q, page }));
    $("#dl").innerHTML = `<p class="muted small">${r.total} document(s)</p>` + table([{ h: "File", cls: "desc", f: x => esc(x.filename) + sample(x.sample) }, { h: "Type", f: x => `${esc(x.type_label)}<br><span class="muted small">confidence ${pct(x.type_confidence)}</span>` },
      { h: "Pages", cls: "right", f: x => x.pages + (x.ocr_pages ? ` <span class="badge b-neutral" title="OCR used">OCR ${x.ocr_pages}</span>` : "") }, { h: "Match", f: x => `<span class="badge ${x.match_status === "auto" || x.match_status === "verified" || x.match_status === "manual" ? "b-official" : x.match_status === "needs_review" ? "b-inferred" : "b-neutral"}">${esc(label(x.match_status))}</span>` },
      { h: "Best candidate", cls: "desc", f: x => x.best_candidate ? `<span class="small">${esc(x.best_candidate.label)}<br>${pct(x.best_candidate.confidence)} · ${esc(x.best_candidate.method)}</span>` : '<span class="muted small">none</span>' }], r.items, { click: true, empty: "No documents yet. Upload PDFs above." });
    onRows($("#dl"), r.items, x => openDoc(x.id));
  };
  $("#dt").onchange = e => { type = e.target.value; draw(); }; $("#dm").onchange = e => { ms = e.target.value; draw(); }; $("#dq").onchange = e => { q = e.target.value; draw(); };
  await draw();
};
const FIELD_LABEL = { tender_id: "Tender ID", job_number: "BBMP job number", work_id: "Work / contract ID", work_order_id: "Work order no.", description: "Work description", road_name: "Road", location: "Location", ward: "Ward", department: "Department", contractor: "Contractor", estimated_cost: "Estimated cost", awarded_amount: "Awarded amount", work_order_date: "Work order date", commencement_date: "Commencement date", completion_deadline: "Completion deadline", payment_amount: "Payment amount", payment_date: "Payment date", document_date: "Document date", document_type: "Document type", amount: "Amount (generic)", date: "Date (generic)" };
async function openDoc(id) {
  const d = await api("/api/gov/documents/" + id);
  let cur = 1;
  const dr = drawer(`<h2 style="padding-right:70px">${esc(d.filename)}${sample(d.sample)}</h2>
   <p><span class="badge b-neutral">${esc(d.type_label)} · ${pct(d.type_confidence)}</span> ${d.classification_cues && d.classification_cues.length ? `<span class="small muted">because: ${d.classification_cues.slice(0, 4).map(c => esc(c.cue)).join(", ")}</span>` : ""}</p>
   <p>${d.roadwork_id ? `Linked to work #${d.roadwork_id} (${esc(d.match_status)}) <a href="#" id="gow">open work</a>` : `<span class="badge b-inferred">${esc(label(d.match_status))}</span>`}</p>
   <iframe class="pdfbox" id="pdf" title="Document preview" src="/static/blank.html"></iframe><p class="small muted">The browser downloads the file with your sign-in. If the preview stays blank, use "Open PDF".</p>
   <div class="row"><button class="btn sm" id="opdf">Open PDF</button><button class="btn ghost sm" id="rem">Re-run matching</button></div>
   <h3 style="margin-top:14px">Extracted fields</h3><p class="small muted">Every value shows its page and method. Verify, correct or reject a value; matching re-runs afterwards. Values are never invented: a field not found is simply absent.</p>
   ${table([{ h: "Field", f: e => esc(FIELD_LABEL[e.field] || e.field) }, { h: "Value", cls: "desc", f: e => `<b>${esc(e.norm || e.raw)}</b>${e.norm && e.raw && e.norm !== e.raw ? `<br><span class="muted small">as written: ${esc(e.raw)}</span>` : ""}${e.snippet ? `<br><span class="muted small">“${esc(e.snippet.slice(0, 110))}”</span>` : ""}` },
      { h: "Page", cls: "right", f: e => e.page }, { h: "Conf.", cls: "right", f: e => pct(e.confidence) }, { h: "Method", f: e => `<span class="small">${esc(e.method)}</span>` },
      { h: "Review", f: e => `<span class="nowrap">${e.status !== "unverified" ? `<span class="badge b-official">${esc(e.status)}</span> ` : ""}<button class="btn ghost sm" data-e="${e.id}" data-s="verified" title="Mark correct">✓</button> <button class="btn ghost sm" data-e="${e.id}" data-s="corrected" title="Correct value">Edit</button> <button class="btn ghost sm" data-e="${e.id}" data-s="rejected" title="Reject value">✗</button></span>` }], d.extractions)}
   <h3 style="margin-top:14px">Match candidates</h3>${d.candidates.length ? linkTable(d.candidates) : '<p class="muted small">No candidate. This document is listed as unmatched.</p>'}
   <div class="row" style="margin-top:8px"><label for="asg" class="small" style="margin:0">Assign by work ID</label><input id="asg" type="number" min="1" style="width:110px" placeholder="work #"><button class="btn sm" id="asgb">Assign</button></div>
   <details style="margin-top:12px"><summary>Decision history</summary>${d.history.map(h => `<div class="small">${dtt(h.at)} · ${esc(h.action)} <span class="muted">${esc(JSON.stringify(h.detail).slice(0, 160))}</span></div>`).join("") || '<span class="muted small">None</span>'}</details>`);
  try {
    const a = Auth.get(); const r = await fetch(`/api/gov/documents/${id}/file`, { headers: { Authorization: "Bearer " + a.token } });
    if (r.ok) { const url = URL.createObjectURL(await r.blob()); $("#pdf", dr).src = url; $("#opdf", dr).onclick = () => window.open(url); }
  } catch { }
  const gow = $("#gow", dr); if (gow) gow.onclick = e => { e.preventDefault(); openWork(d.roadwork_id); };
  $("#rem", dr).onclick = async () => { try { const r = await api(`/api/gov/documents/${id}/rematch`, { method: "POST" }); toast("Matching re-run: " + r.auto + " auto, " + r.needs_review + " to review, " + r.unmatched + " unmatched"); openDoc(id); } catch (e) { toast(e.message, true); } };
  $("#asgb", dr).onclick = async () => { const v = $("#asg", dr).value; if (!v) return; if (!(await confirmBox("Assign document", "Link this document to work #" + v + " as an officer-confirmed match?", "Assign"))) return; try { await api(`/api/gov/documents/${id}/assign`, { method: "POST", form: { roadwork_id: v } }); toast("Assigned"); openDoc(id); } catch (e) { toast(e.message, true); } };
  $$("[data-e]", dr).forEach(b => b.onclick = async () => {
    let nv; if (b.dataset.s === "corrected") { nv = prompt("Correct value"); if (!nv) return; }
    try { await api("/api/gov/extractions/" + b.dataset.e, { method: "PATCH", form: { status: b.dataset.s, norm_value: nv } }); toast("Saved"); openDoc(id); } catch (e) { toast(e.message, true); }
  });
  wireLinks(dr, () => openDoc(id));
}

/* ---------- matching review ---------- */
ME.views.matching = async el => {
  const o = await api("/api/gov/matching/overview"), d = o.documents;
  const sug = await api("/api/gov/links?status=suggested");
  el.innerHTML = `<div class="row between"><h1>Matching review</h1><button class="btn" id="run">Run matching now</button></div><p class="muted">${esc(o.policy)}</p>
  <div class="grid g4"><div class="stat"><b>${d.auto + d.verified + d.manual}</b><span>Documents linked</span></div><div class="stat warn"><b>${d.needs_review}</b><span>Need manual verification</span></div><div class="stat bad"><b>${d.unmatched}</b><span>Unmatched documents</span></div><div class="stat"><b>${o.unmatched_tenders.length}</b><span>Tenders with no work</span></div></div>
  <h2 style="margin-top:20px">Suggested links awaiting a decision (${sug.total})</h2><p class="small muted">Suggestions come from road name, contractor or description similarity. They are never applied until you verify them. Exact-identifier matches conflicting with each other are also listed here.</p>
  <div id="sg">${linkTable(sug.items)}</div>
  <div class="grid g2" style="margin-top:18px"><div><h2>Unmatched documents</h2>${table([{ h: "File", f: x => `<a href="#" data-d="${x.id}">${esc(x.filename)}</a>${sample(x.sample)}` }, { h: "Type", f: x => esc(x.type || "") }], o.unmatched_documents, { empty: "None." })}</div>
  <div><h2>Tenders with no linked work</h2>${table([{ h: "Tender ID", f: x => esc(x.tender_id) + sample(x.sample) }, { h: "Title", cls: "desc", f: x => esc(x.title || "") }], o.unmatched_tenders, { empty: "None." })}</div></div>
  <h2 style="margin-top:18px">Recent matching runs</h2>${table([{ h: "Run", f: r => "#" + r.id }, { h: "Kind", f: r => esc(r.kind) }, { h: "Result", f: r => esc(JSON.stringify(r.stats)) }, { h: "When", f: r => dtt(r.at) }], o.recent_runs)}
  <h2 style="margin-top:18px">Decision history</h2><div id="hist"></div>`;
  $("#run").onclick = async () => { try { const r = await api("/api/gov/matching/run", { method: "POST", form: { documents: true, tenders: true, roads: true } }); toast("Matching finished"); go("matching"); } catch (e) { toast(e.message, true); } };
  $$("[data-d]", el).forEach(a => a.onclick = e => { e.preventDefault(); openDoc(+a.dataset.d); });
  wireLinks(el, () => go("matching"));
  const h = await api("/api/gov/history?limit=25");
  $("#hist").innerHTML = table([{ h: "When", f: x => dtt(x.at) }, { h: "Subject", cls: "desc", f: x => esc(x.label || x.subject) }, { h: "Action", f: x => `<span class="badge b-neutral">${esc(x.action)}</span>` }, { h: "Detail", cls: "desc", f: x => `<span class="small muted">${esc(JSON.stringify(x.detail).slice(0, 140))}</span>` }], h);
};

/* ---------- import ---------- */
ME.views.imports = async el => {
  const jobs = await api("/api/gov/imports");
  el.innerHTML = `<h1>Import data</h1><p class="muted">Upload a CSV or Excel export (BBMP work orders, KPPP tender lists, OpenCity files). Check the column mapping before importing. Identical rows are skipped, so re-importing a file is safe.</p>
   <div class="card"><div class="grid g3"><div><label for="ik">Kind of file</label><select id="ik"><option value="works">Work orders / works list</option><option value="tenders">Tenders (KPPP style)</option></select></div>
   <div><label for="isr">Source name</label><input id="isr" placeholder="e.g. BBMP work orders FY 2024-25"></div><div><label for="iw">Ward regime</label><select id="iw"><option>243</option><option>198</option><option>gba369</option></select></div></div>
   <label for="iu">Source link (optional)</label><input id="iu" placeholder="https://…"><label for="if">File</label><input id="if" type="file" accept=".csv,.xlsx,.xls">
   <label class="row" style="font-weight:400"><input type="checkbox" id="isa" style="width:auto;min-height:0"> This file is synthetic sample data (marks every record SAMPLE)</label>
   <label class="row" style="font-weight:400"><input type="checkbox" id="iro" style="width:auto;min-height:0"> Keep only road-related rows</label>
   <button class="btn" id="ipv" style="margin-top:10px">Preview columns</button><div id="ipr"></div></div>
   <h2 style="margin-top:20px">Past imports</h2>${table([{ h: "File", f: j => esc(j.file) + sample(j.sample) }, { h: "Kind", f: j => esc(j.kind) }, { h: "Source", cls: "desc", f: j => esc(j.source || "") }, { h: "Rows", cls: "right", f: j => j.total }, { h: "Loaded", cls: "right", f: j => j.ok }, { h: "Duplicates", cls: "right", f: j => j.duplicate || 0 }, { h: "Failed", cls: "right", f: j => j.failed }, { h: "When", f: j => dtt(j.at) }], jobs, { empty: "Nothing imported yet." })}`;
  let mapping = null;
  $("#ipv").onclick = async () => {
    const f = $("#if").files[0]; if (!f) return toast("Choose a file first", true);
    try {
      const r = await api("/api/gov/imports/preview", { method: "POST", form: { file: f, kind: $("#ik").value } }); mapping = r.mapping;
      const fields = Object.keys(r.mapping);
      $("#ipr").innerHTML = `<h3 style="margin-top:14px">${r.rows} rows found</h3>${r.required_missing.length ? `<div class="notice bad">Missing required column: ${r.required_missing.map(esc).join(", ")}. Choose it below.</div>` : ""}
      <div class="filters">${fields.concat(r.required_missing).map(k => `<div><label>${esc(label(k))}</label><select data-map="${k}"><option value="">(none)</option>${r.columns.map(c => `<option${r.mapping[k] === c ? " selected" : ""}>${esc(c)}</option>`).join("")}</select></div>`).join("")}</div>
      ${r.unmapped_columns.length ? `<p class="small muted">Not used: ${r.unmapped_columns.map(esc).join(", ")}</p>` : ""}
      <details><summary>First rows</summary><pre class="json">${esc(JSON.stringify(r.sample.slice(0, 3), null, 1))}</pre></details><button class="btn primary" id="ido" style="margin-top:8px">Import ${r.rows} rows</button>`;
      $$("[data-map]").forEach(s => s.onchange = () => { if (s.value) mapping[s.dataset.map] = s.value; else delete mapping[s.dataset.map]; });
      $("#ido").onclick = async () => {
        if (!$("#isr").value.trim()) return toast("Enter the source name so provenance is kept", true);
        $("#ido").disabled = true;
        try {
          const j = await api("/api/gov/imports", { method: "POST", form: { file: f, kind: $("#ik").value, source_name: $("#isr").value, source_url: $("#iu").value, ward_regime: $("#iw").value, mapping_json: JSON.stringify(mapping), roads_only: $("#iro").checked, is_sample: $("#isa").checked } });
          $("#ipr").innerHTML = `<div class="notice ok"><b>Imported ${j.ok} of ${j.total} rows.</b> Duplicates skipped: ${j.duplicate_rows}. Not road-related, skipped: ${j.skipped_non_road}. Failed: ${j.failed}.<br>Matching afterwards: ${esc(JSON.stringify(j.matching_after_import))}</div>${j.errors.length ? `<pre class="json">${esc(JSON.stringify(j.errors, null, 1))}</pre>` : ""}`;
        } catch (e) { toast(e.message, true); $("#ido").disabled = false; }
      };
    } catch (e) { toast(e.message, true); }
  };
};

/* ---------- roads & contractors ---------- */
ME.views.roads = async el => {
  let only = "", q = "";
  el.innerHTML = `<h1>Roads</h1><p class="muted">All mapped roads, with or without a government record. Roads with citizen reports but no linked record are the gaps to look into.</p>
  <div class="filters"><div><label for="rq">Road name</label><input id="rq" type="search"></div><div><label for="ro">Show</label><select id="ro"><option value="">All roads</option><option value="with_reports">With citizen reports</option><option value="without_records">Without any government record</option><option value="with_records">With a linked record</option></select></div></div><div id="rl"></div>`;
  const draw = async () => {
    const r = await api("/api/gov/roads" + qs({ q, only }));
    $("#rl").innerHTML = `<p class="muted small">${r.total} road(s)</p>` + table([{ h: "Road", f: x => esc(x.name) + sample(x.sample) + (x.approx ? ' <span class="badge b-neutral">approx. shape</span>' : "") }, { h: "Length", cls: "right num", f: x => x.length_m.toLocaleString() + " m" },
      { h: "Citizen reports", cls: "right", f: x => x.reports ? `<span class="badge b-citizen">${x.reports}</span>` : "0" }, { h: "Linked govt records", cls: "right", f: x => x.government_records || "0" },
      { h: "Record status", f: x => x.gov_status === "matched" ? '<span class="badge b-official">Linked</span>' : x.gov_status === "possible" ? '<span class="badge b-inferred">Suggested only</span>' : '<span class="badge b-neutral">None found</span>' }], r.items);
  };
  $("#rq").onchange = e => { q = e.target.value; draw(); }; $("#ro").onchange = e => { only = e.target.value; draw(); }; await draw();
};
ME.views.contractors = async el => {
  const r = await api("/api/gov/contractors");
  el.innerHTML = `<h1>Contractors</h1><p class="muted">Names are normalised (M/s, Pvt Ltd and spelling variants) so one contractor is counted once. Totals are amounts as published, not proof of completion.</p>` +
    table([{ h: "Contractor", f: c => esc(c.name) + sample(c.sample) }, { h: "Works", cls: "right", f: c => c.works }, { h: "Awarded", cls: "right num", f: c => crore(c.awarded) }], r);
};

/* ---------- evaluation ---------- */
ME.views.evaluation = async el => {
  el.innerHTML = '<h1>Evaluation</h1><p class="loading">Running the real pipeline on the labelled examples… this takes several seconds.</p>';
  const e = await api("/api/gov/evaluation");
  const ex = e.extraction, cl = e.clustering, mt = e.matching;
  const mrow = (name, m, extra = "") => `<tr><td>${esc(name)}${extra}</td><td class="right num">${pct(m.precision)}</td><td class="right num">${pct(m.recall)}</td><td class="right num">${pct(m.f1)}</td><td class="right num small">${m.tp} / ${m.fp} / ${m.fn}</td></tr>`;
  const clt = rows => table([{ h: "Variant", f: r => esc(r.name || (r.eps_m !== undefined ? r.eps_m + " m" : r.gps_noise_m + " m GPS noise")) }, { h: "Adjusted Rand", cls: "right num", f: r => r.ari.toFixed(2) }, { h: "Pair precision", cls: "right num", f: r => pct(r.precision) }, { h: "Pair recall", cls: "right num", f: r => pct(r.recall) }, { h: "Pair F1", cls: "right num", f: r => pct(r.f1) }, { h: "Clusters found", cls: "right", f: r => r.clusters }], rows);
  el.innerHTML = `<div class="row between"><h1>Evaluation</h1><button class="btn ghost sm" id="rf">Run again</button></div><div class="notice">${esc(e.caveat)}</div>
   <h2 style="margin-top:18px">Field extraction</h2><div class="grid g3">${ex.sets.map(s => `<div class="stat ${s.independent ? "" : "warn"}"><b>${pct(s.accuracy)}</b><span>${esc(s.title)}<br>${s.correct}/${s.total} values, ${s.documents} documents</span></div>`).join("")}</div>
   <p class="small muted" style="margin-top:6px">Only the last set is independent. The orange sets were used to build or fix the extractor, so treat them as regression tests. Document type accuracy over all ${ex.documents} documents: ${pct(ex.type_accuracy)}.</p>
   <div class="grid g2"><div><h3>Weakest fields</h3>${table([{ h: "Field", f: r => esc(FIELD_LABEL[r.field] || r.field) }, { h: "Correct", cls: "right", f: r => r.correct + "/" + r.total }, { h: "Accuracy", cls: "right", f: r => pct(r.accuracy) }], ex.by_field.slice(0, 8))}</div>
   <div><h3>Misses</h3>${table([{ h: "Document", f: r => esc(r.doc) }, { h: "Field", f: r => esc(r.field) }, { h: "Expected", f: r => esc(r.expected) }, { h: "Got", f: r => esc((Array.isArray(r.got) ? r.got.join(", ") : r.got) || "nothing") }], ex.misses.slice(0, 12))}</div></div>
   <h2 style="margin-top:18px">Cross-document matching (automatic links only)</h2>
   <div class="tablewrap"><table><thead><tr><th>Task</th><th class="right">Precision</th><th class="right">Recall</th><th class="right">F1</th><th class="right">TP / FP / FN</th></tr></thead><tbody>
   ${mrow("Document → work", mt.document_to_work)}${mrow("Tender → work", mt.tender_to_work)}${mrow("Work → road", mt.work_to_road)}</tbody></table></div>
   <p class="small muted">Missed links (FN) are the cases without an exact identifier. They are not merged automatically; ${esc(mt.document_to_work.suggested_hits)} of the missed documents had the correct work as the top suggestion for an officer to confirm. Automatic links made on fuzzy evidence alone: ${mt.document_to_work.fuzzy_only_auto_links} (must be 0).</p>
   <details><summary>Per-document result</summary>${table([{ h: "Document", f: r => esc(r.doc) }, { h: "Expected work", f: r => esc(r.expected || "none") }, { h: "Auto-linked to", f: r => esc(r.auto_linked_to || "none") }, { h: "Status", f: r => esc(r.status) }], mt.document_to_work.rows)}</details>
   <h2 style="margin-top:18px">Complaint clustering (${cl.n} labelled complaints in ${cl.groups} groups)</h2>
   <div class="grid g4"><div class="stat"><b>${cl.ari.toFixed(2)}</b><span>Adjusted Rand index (clean data)</span></div><div class="stat"><b>${pct(cl.pairwise.precision)}</b><span>Pairwise precision</span></div><div class="stat"><b>${pct(cl.pairwise.recall)}</b><span>Pairwise recall</span></div><div class="stat"><b>${pct(cl.pairwise.f1)}</b><span>Pairwise F1</span></div></div>
   <h3 style="margin-top:12px">What each signal adds</h3>${clt(cl.ablation)}<h3 style="margin-top:12px">Distance threshold</h3>${clt(cl.sweep)}
   <h3 style="margin-top:12px">Stress test: noisier GPS</h3>${clt(cl.gps_noise)}<p class="small muted">The default radius is ${40} m. With 20 m or more of GPS error, groups start to split. Phones in dense areas often err by that much, which is why the radius is configurable.</p>
   <h2 style="margin-top:18px">Alert false-positive analysis</h2><p class="small muted">${esc(e.alerts.note)}</p>
   ${table([{ h: "Rule", f: r => esc(r.rule) }, { h: "Alerts", cls: "right", f: r => r.alerts }, { h: "On a real group", cls: "right", f: r => r.on_true_group }, { h: "False alarms", cls: "right", f: r => r.false_positive }, { h: "Precision", cls: "right", f: r => pct(r.precision) }], e.alerts.by_rule)}
   <p class="small">Real groups with no alert: ${e.alerts.missed_groups.length ? e.alerts.missed_groups.map(esc).join(", ") : "none"} of ${e.alerts.groups_expected}. Group that must stay quiet (${e.alerts.false_alarm_groups.map(esc).join(", ")}): no alert raised unless listed above as a false alarm.</p>
   <h2 style="margin-top:18px">Limitations</h2><ul>${e.limitations.map(l => `<li>${esc(l)}</li>`).join("")}</ul><p class="small muted">Generated ${dtt(e.generated_at)} in ${e.seconds} s.</p>`;
  $("#rf").onclick = async () => { await api("/api/gov/evaluation?refresh=true"); go("evaluation"); };
};

loginGate(["officer", "admin"], "Government monitoring", start);
