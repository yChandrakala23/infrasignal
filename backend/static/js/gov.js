const ME = { views: {} };

const VIEWS = [["overview", "Overview"], ["works", "Works"], ["documents", "Documents"], ["matching", "Matching review"], ["webcrawl", "Live web collection"], ["collection", "Collected data"], ["imports", "Import data"], ["roads", "Roads"], ["contractors", "Contractors"], ["evaluation", "Evaluation"]];
let OPTS = null, F = {};

function start(auth) {
  document.body.innerHTML = `<header class="topbar"><a class="brand" href="/citizen"><i></i>InfraSignal</a><span class="area">Government monitoring</span><span class="spacer"></span>
   ${auth.role === "admin" ? '<a class="link" href="/authority">Authority dashboard</a>' : ""}<span class="small" style="color:#c3ccd2">${esc(auth.email)}</span><button class="btn ghost sm" id="out" style="color:#fff;border-color:#46525b">Sign out</button></header>
   <div class="banner" id="banner" hidden></div>
   <div class="shell"><nav class="side" aria-label="Sections">${VIEWS.concat(Auth.get().role === "admin" ? [["audit", "Audit log"]] : []).map(([k, t]) => `<button data-v="${k}" aria-selected="false">${t}<span class="count" id="c-${k}" hidden></span></button>`).join("")}</nav><main class="main" id="view" tabindex="-1"></main></div>`;
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
function filterBar(onChange, keys = ["q", "ward", "contractor", "department", "category", "tender_status", "work_status", "fiscal_year", "date_from", "date_to", "verification", "origin"]) {
  const sel = (k, lab, opts) => `<div><label for="f-${k}">${lab}</label><select id="f-${k}" data-k="${k}"><option value="">All</option>${opts.map(o => `<option value="${esc(o.v)}"${F[k] === o.v ? " selected" : ""}>${esc(o.t)}</option>`).join("")}</select></div>`;
  const txt = (k, lab, type = "text") => `<div><label for="f-${k}">${lab}</label><input id="f-${k}" data-k="${k}" type="${type}" value="${esc(F[k] || "")}"></div>`;
  const parts = {
    q: txt("q", "Search description"), ward: sel("ward", "Ward", OPTS.wards.map(w => ({ v: w.value, t: `${w.number} ${w.name || ""}${w.regime ? " (" + w.regime + " map)" : ""}` }))),
    contractor: sel("contractor", "Contractor", OPTS.contractors.map(c => ({ v: c, t: c }))), department: sel("department", "Department", OPTS.departments.map(c => ({ v: c, t: c }))),
    category: sel("category", "Category", OPTS.categories.map(c => ({ v: c, t: label(c) }))), tender_status: sel("tender_status", "Tender status", OPTS.tender_statuses.map(c => ({ v: c, t: label(c) }))),
    work_status: sel("work_status", "Work status (as stated)", OPTS.work_statuses.map(c => ({ v: c.key, t: c.label }))), fiscal_year: sel("fiscal_year", "Fiscal year", OPTS.fiscal_years.map(c => ({ v: c, t: c }))),
    date_from: txt("date_from", "Work order from", "date"), date_to: txt("date_to", "Work order to", "date"),
    origin: `<div><label for="f-origin">Data origin</label><select id="f-origin" data-k="origin">${[["", "All"], ["collected", "Collected from BBMP open data"], ["sample", "Sample (synthetic) only"]].map(([v, t]) => `<option value="${v}"${(F.origin || "") === v ? " selected" : ""}>${t}</option>`).join("")}</select></div>`,
    verification: `<div><label for="f-verification">Records shown</label><select id="f-verification" data-k="verification">${[["", "Official and confirmed"], ["provisional", "Provisional, started from a PDF (to verify)"], ["all", "All, including provisional"]].map(([v, t]) => `<option value="${v}"${(F.verification || "") === v ? " selected" : ""}>${esc(t)}</option>`).join("")}</select></div>`,
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
      <div class="stat"><b>${t.works_with_award_amount === 0 ? "–" : crore(t.awarded_amount)}</b><span>Awarded amount (as published)${t.works_with_award_amount === 0 ? "<br>not published in these sources (BBMP open data lists bills and payments only)" : ""}</span></div>
      <div class="stat"><b>${crore(t.paid_amount_recorded)}</b><span>Payments recorded (${t.payments}). Not a measure of completion${t.repeat_payment_records_excluded ? `<br>${t.repeat_payment_records_excluded} repeat record(s), ${crore(t.repeat_payment_amount_excluded)}, counted once` : ""}</span></div>
      <div class="stat"><b>${t.tenders}</b><span>Tender records linked to these works</span></div>
      <div class="stat"><b>${t.documents}</b><span>Documents linked to these works</span></div>
      ${t.provisional_works_not_counted ? `<div class="stat warn"><b>${t.provisional_works_not_counted}</b><span>Provisional works (started from a PDF), not in the totals</span></div>` : ""}
      <div class="stat warn"><b>${d.deadlines.past_deadline_no_completion_record}</b><span>Past stated deadline, no completion certificate in records</span></div>
      <div class="stat"><b>${d.deadlines.due_in_30_days}</b><span>Deadlines in the next 30 days</span></div>
    </div>
    <div class="grid g2" style="margin-top:14px">
      <div class="card"><h3>Works by ward</h3><p class="small muted">Each ward number belongs to its ward map (198, 225 or 243 wards, or "common" for city-level divisions). Numbers from different maps are different places.</p><div class="chart-box"><canvas id="ch-ward"></canvas></div></div>
      <div class="card"><h3>Stated work status</h3><div class="chart-box"><canvas id="ch-status"></canvas></div><p class="small muted">Only what a document states. "Not stated" means no such document is loaded, not that the work is undone.</p></div>
      <div class="card"><h3>Works by category</h3><div class="chart-box"><canvas id="ch-cat"></canvas></div></div>
      <div class="card"><h3>Work orders by month</h3><div class="chart-box"><canvas id="ch-month"></canvas></div></div>
    </div>
    <div class="grid g-main" style="margin-top:14px">
      <div class="card"><h3>Largest contractors in view</h3>${table([{ h: "Contractor", f: r => esc(r.name) }, { h: "Works", cls: "right num", f: r => r.works }, { h: "Awarded", cls: "right num", f: r => crore(r.awarded) }, { h: "Paid", cls: "right num", f: r => r.paid ? crore(r.paid) : "–" }], d.top_contractors, { empty: "No contractor data." })}</div>
      <div class="card"><h3>Gaps and items to verify</h3>
        <dl class="kv"><dt>No document</dt><dd>${g.works_without_documents}</dd><dt>No tender ID</dt><dd>${g.works_without_tender_id}</dd><dt>No payment record</dt><dd>${g.works_without_payment_record}</dd>
        <dt>Documents to review</dt><dd>${g.documents_needing_review}</dd><dt>Document links awaiting review</dt><dd>${g.links_awaiting_review}</dd></dl>
        <h4 style="margin-top:10px">Not attached to a work (filters do not apply)</h4>
        <dl class="kv"><dt>Documents with no work</dt><dd>${d.unscoped.documents_without_a_work} (${d.unscoped.unmatched_documents} unmatched)</dd><dt>Tenders with no work</dt><dd>${d.unscoped.tenders_without_work}</dd>
        <dt>All link suggestions</dt><dd>${Object.entries(d.unscoped.links_awaiting_review).map(([k, v]) => `${esc(k)}: ${v}`).join(", ") || "0"}</dd></dl>
        ${g.paid_above_award_to_verify.length ? `<h4 style="margin-top:10px">Payments above the awarded amount (to verify)</h4><p class="small muted">May be extra items, variations or data errors. Not a finding.</p>${g.paid_above_award_to_verify.map(x => `<div class="small"><a href="#" data-w="${x.id}">${esc((x.description || "").slice(0, 60))}</a> paid ${crore(x.paid)} vs ${crore(x.awarded)}</div>`).join("")}` : ""}
        <p class="small muted" style="margin-top:10px">${d.deadlines.note}</p></div>
    </div>
    <div class="card" style="margin-top:14px"><h3>Deadlines passed with no completion certificate in the records</h3>${table([{ h: "Job number", f: r => esc(r.job_number || "–") }, { h: "Work", cls: "desc", f: r => `<a href="#" data-w="${r.id}">${esc(r.description)}</a>` }, { h: "Stated deadline", f: r => dt(r.deadline) }], d.deadlines.items, { empty: "None in view." })}</div>
    <div class="card" style="margin-top:14px"><h3>Recent imports</h3>${table([{ h: "File", f: r => esc(r.file) + sample(r.sample) }, { h: "Kind", f: r => esc(r.kind) }, { h: "Rows loaded", cls: "right", f: r => r.ok }, { h: "Duplicates skipped", cls: "right", f: r => r.duplicate || 0 }, { h: "When", f: r => dtt(r.at) }], d.imports)}</div>`;
    $$("[data-w]", el).forEach(a => a.onclick = e => { e.preventDefault(); openWork(+a.dataset.w); });
    const pal = ["#1b4f8f", "#3d7fc4", "#7aa9d8", "#a8610f", "#5b6770", "#0d7a6b", "#b3261e"];
    chart("ch-ward", { type: "bar", data: { labels: d.works_by_ward.map(x => `${x.number}${x.regime ? " (" + x.regime + (/^\d+$/.test(x.regime) ? "-ward map" : "") + ")" : ""} ${x.name || ""}`.trim().slice(0, 30)), datasets: [{ data: d.works_by_ward.map(x => x.count), backgroundColor: "#1b4f8f" }] }, options: { indexAxis: "y", plugins: { legend: { display: false } } } });
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
    const cols = [{ h: "Job number", f: x => `<span class="nowrap">${esc(x.job_number || "–")}</span>` + sample(x.sample) }, { h: "Work", cls: "desc", f: x => esc(x.description) + (x.verification_status === "provisional" ? ' <span class="badge b-inferred" title="Started from a PDF; not yet checked by an officer">Provisional</span>' : "") },
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
   <h2 style="padding-right:70px">${esc(w.description || "(no description in the document)")}</h2><p>${sample(w.sample)} <span class="badge b-neutral">${esc(w.work_status_label)}</span> ${w.verification_status === "provisional" ? '<span class="badge b-inferred">Provisional, needs verification</span>' : w.verification_status === "confirmed" ? '<span class="badge b-official">Confirmed by officer</span>' : w.verification_status === "merged" ? '<span class="badge b-neutral">Merged into work #' + w.merged_into_id + '</span>' : ""}</p>
   ${p.provisional && p.provisional.status === "provisional" ? `<div class="notice" style="margin:8px 0"><b>${esc(p.provisional.note)}</b><ul class="small" style="margin:6px 0 0 18px">${p.provisional.reasons.map(r => `<li>${esc(r)}</li>`).join("")}</ul>
     <div class="row" style="margin-top:8px"><button class="btn sm" id="pconf">Confirm this work</button><a href="#" class="small" data-d="${p.provisional.created_from_document_id}">Open source document</a>
     <label for="ptgt" class="small" style="margin:0">or merge into work #</label><input id="ptgt" type="number" min="1" style="width:100px"><button class="btn ghost sm" id="pmerge">Merge</button></div></div>` : ""}
   ${p.provisional && p.provisional.fields.length ? `<details style="margin:6px 0"><summary>Where each value came from (document and page)</summary>${table([{ h: "Field", f: x => esc(label(x.field)) }, { h: "Value", cls: "desc", f: x => esc(String(x.value)) + (x.snippet ? `<br><span class="small muted">“${esc(x.snippet.slice(0, 110))}”</span>` : "") }, { h: "Document", f: x => `<a href="#" data-d="${x.document_id}">#${x.document_id}</a>` }, { h: "Page", cls: "right", f: x => x.page }, { h: "Conf.", cls: "right", f: x => pct(x.confidence) }], p.provisional.fields)}</details>` : ""}
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
   ${(p.boq || []).length ? `<h3 style="margin-top:16px">Bill of quantities in the linked documents</h3>${table([{ h: "Document", f: x => `<a href="#" data-d="${x.document_id}">${esc(x.filename)}</a>` }, { h: "Items", cls: "right", f: x => x.items },
     { h: "Item amounts add up to", cls: "right", f: x => inr(x.sum_of_item_amounts) }, { h: "Printed total", cls: "right", f: x => inr(x.printed_total) },
     { h: "Checks", f: x => x.mismatches ? `<span class="badge b-bad">${x.mismatches} do not add up</span>` : x.incomplete ? `<span class="badge b-inferred">${x.incomplete} cannot be checked</span>` : '<span class="badge b-ok">all pass</span>' }], p.boq)}` : ""}
   ${p.documents_to_review.length ? `<h4 style="margin-top:10px">Documents suggested for this work</h4>${linkTable(p.documents_to_review)}` : ""}
   <h3 style="margin-top:16px">Tender</h3>${table([{ h: "Tender ID", f: x => esc(x.tender_id) + sample(x.sample) }, { h: "Title", f: x => esc(x.title || "") }, { h: "Status", f: x => esc(x.status || "–") }], p.tenders, { empty: "No tender linked." })}
   <h3 style="margin-top:16px">Bills and payments</h3><p class="small muted">${esc(p.payment_summary.note)}</p>
   ${table([{ h: "Payment", f: x => esc(x.reference || "–") }, { h: "Date", f: x => dt(x.paid_on) }, { h: "Gross", cls: "right num", f: x => x.gross_amount == null ? "–" : inr(x.gross_amount) }, { h: "Deduction", cls: "right num", f: x => x.deduction == null ? "–" : inr(x.deduction) },
      { h: "Net paid", cls: "right num", f: x => inr(x.amount) },
      { h: "Counted", f: x => x.counted ? '<span class="badge b-official">counted</span>' : `<span class="badge b-inferred" title="Same work, amount and date as payment #${x.duplicate_of_id}">repeat of #${x.duplicate_of_id}</span>` },
      { h: "", f: x => x.counted ? "" : `<button class="btn ghost sm" data-pay="${x.id}" title="Count this payment separately">Not a repeat</button>` }], p.payments, { empty: "No payment record." })}
   ${p.payment_summary.repeat_records_excluded ? `<p class="small muted">${p.payment_summary.repeat_records_excluded} payment row(s) worth ${inr(p.payment_summary.repeat_amount_excluded)} repeat another source and are left out of the total: ${inr(p.payment_summary.paid_total)}.</p>` : ""}
   ${p.values ? `<h3 style="margin-top:16px">Officer-verified values</h3><p class="small muted">${esc(p.values.note || "")}</p>
     ${p.values.verified_fields.length ? `<p>${p.values.verified_fields.map(k => `<span class="badge b-official">${esc(label(k))}</span> <button class="btn ghost sm" data-unlock="${esc(k)}">Unlock</button>`).join(" ")}</p>` : '<p class="small muted">No value has been confirmed by an officer yet.</p>'}
     <div class="row" style="gap:8px;flex-wrap:wrap;align-items:flex-end"><div><label for="vf-k">Field</label><select id="vf-k">${["description", "ward_number", "department", "road_name_extracted", "location_text", "estimated_cost", "awarded_amount", "fiscal_year"].map(k => `<option value="${k}">${esc(label(k))}</option>`).join("")}</select></div>
     <div style="flex:1;min-width:180px"><label for="vf-v">Value (leave empty to confirm the current one)</label><input id="vf-v"></div><button class="btn sm" id="vf-go">Save as verified</button></div>` : ""}
   <h3 style="margin-top:16px">Source records behind this work</h3><p class="small muted">Every original record that mentions this work is kept as it was imported. The link shows how it was matched and whether an officer checked it.</p>
   ${table([{ h: "Record", cls: "desc", f: x => `${esc((x.import && x.import.file) || "record")}, row ${x.row}${x.import && x.import.sample ? sample(true) : ""}<br><span class="small muted">${esc(label(x.source_type || ""))} · ${esc(x.import && x.import.source_name || "")}</span>${x.fiscal_year ? `<br><span class="badge b-neutral">FY ${esc(x.fiscal_year)}</span>` : ""}${x.file ? `<br><span class="small">${esc(x.file.filename || "")}${x.file.download_url && /^https?:/.test(x.file.download_url) ? ` · <a href="${esc(x.file.download_url)}" target="_blank" rel="noopener">source file</a>` : ""}${x.file.dataset_url ? ` · <a href="${esc(x.file.dataset_url)}" target="_blank" rel="noopener">dataset page</a>` : ""}<br>downloaded ${dt(x.file.downloaded_at)}, checksum ${esc((x.file.sha256 || "").slice(0, 10))}…</span>` : ""}` },
      { h: "Role", f: x => esc(label(x.role || "")) }, { h: "Matched by", f: x => `<span class="small">${esc(x.method || "")}<br>confidence ${pct(x.confidence)}</span>` }, { h: "Status", f: x => linkBadge(x.status) },
      { h: "", f: x => `<span class="nowrap"><button class="btn sm" data-sl="${x.link_id}" data-s="verified">Verify</button> <button class="btn ghost sm" data-sl="${x.link_id}" data-s="rejected">Reject</button></span>` },
      { h: "Original and normalised", f: x => `<details><summary class="small">as published</summary><pre class="json">${esc(JSON.stringify(x.raw, null, 1))}</pre></details>${x.normalised ? `<details><summary class="small">as understood</summary><pre class="json">${esc(JSON.stringify(x.normalised, null, 1))}</pre></details>` : ""}` }], p.sources, { empty: "No source record is linked." })}
   <h3 style="margin-top:16px">Roads this work is linked to</h3>${p.roads.length ? linkTable(p.roads) : '<p class="muted small">No road linked.</p>'}
   ${p.complaints_nearby.length ? `<h3 style="margin-top:16px">Citizen complaint clusters suggested near this work</h3>${p.complaints_nearby.map(c => `<div class="small">Cluster #${c.cluster_id}: ${c.count} reports ${linkBadge(c.status)}</div>`).join("")}` : ""}
   ${p.source_row ? `<details><summary>Original source row (${esc(p.source_row.import_job || "")}, row ${p.source_row.row})</summary><pre class="json">${esc(JSON.stringify(p.source_row.raw, null, 1))}</pre></details>` : ""}`);
  $$("[data-d]", d).forEach(a => a.onclick = e => { e.preventDefault(); openDoc(+a.dataset.d); });
  const pc = $("#pconf", d); if (pc) pc.onclick = async () => {
    if (!(await confirmBox("Confirm work", "Mark this work as checked against its source document?", "Confirm"))) return;
    try { await api(`/api/gov/roadworks/${id}/confirm`, { method: "POST" }); toast("Work confirmed"); openWork(id); } catch (e) { toast(e.message, true); }
  };
  const pm = $("#pmerge", d); if (pm) pm.onclick = async () => {
    const t = $("#ptgt", d).value; if (!t) return;
    if (!(await confirmBox("Merge work", "Move this profile's documents, payments and links into work #" + t + "? The profile stays in the history as merged.", "Merge"))) return;
    try { await api(`/api/gov/roadworks/${id}/merge`, { method: "POST", form: { target_id: t } }); toast("Merged into work #" + t); openWork(+t); } catch (e) { toast(e.message, true); }
  };
  $$("[data-sl]", d).forEach(b => b.onclick = async () => {
    try { await api("/api/gov/source-links/" + b.dataset.sl, { method: "PATCH", form: { status: b.dataset.s } }); toast("Source link " + b.dataset.s); openWork(id); } catch (e) { toast(e.message, true); }
  });
  $$("[data-pay]", d).forEach(b => b.onclick = async () => {
    try { await api("/api/gov/payments/" + b.dataset.pay, { method: "PATCH", form: { duplicate_of: "" } }); toast("Payment is now counted"); openWork(id); } catch (e) { toast(e.message, true); }
  });
  const vg = $("#vf-go", d); if (vg) vg.onclick = async () => {
    const k = $("#vf-k", d).value, v = $("#vf-v", d).value.trim();
    try { await api(`/api/gov/roadworks/${id}/fields`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ [k]: v === "" ? null : v }) }); toast("Saved. Later imports will not change this value."); openWork(id); } catch (e) { toast(e.message, true); }
  };
  $$("[data-unlock]", d).forEach(b => b.onclick = async () => { try { await api(`/api/gov/roadworks/${id}/fields/${b.dataset.unlock}`, { method: "DELETE" }); toast("Unlocked"); openWork(id); } catch (e) { toast(e.message, true); } });
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
   <details class="panel" id="srcbox" style="margin:8px 0"><summary>Where did these files come from? (optional, kept with each file)</summary>
    <div class="filters" style="margin-top:8px"><div><label for="us">Source</label><input id="us" placeholder="e.g. BBMP, KPPP, GBA" maxlength="100"></div>
    <div><label for="up1">Web page it was listed on</label><input id="up1" type="url" placeholder="https://…"></div>
    <div><label for="ud">Direct download link</label><input id="ud" type="url" placeholder="https://…/file.pdf"></div>
    <div><label for="udt">Downloaded on</label><input id="udt" type="date"></div></div>
    <p class="small muted">Applies to every file in the next upload. Nothing is filled in for you: leave a box empty if you do not know it.</p></details>
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
    fd.append("source_name", $("#us").value); fd.append("source_page_url", $("#up1").value); fd.append("download_url", $("#ud").value); fd.append("downloaded_at", $("#udt").value);
    try {
      const r = await api("/api/gov/documents", { method: "POST", body: fd });
      $("#up").innerHTML = table([{ h: "File", f: x => esc(x.filename) }, { h: "Result", f: x => x.error ? `<span class="badge b-bad">${esc(x.error)}</span>` : (x.duplicate_of_existing ? '<span class="badge b-neutral">Already uploaded</span>' : `${esc(x.type_label)} <span class="muted small">(${pct(x.type_confidence)})</span>`) },
        { h: "Match", f: x => x.error ? "" : esc(label(x.match_status)) + (x.created_work_id ? ` <span class="badge b-inferred">Provisional work #${x.created_work_id} started</span>` : "") }], r.results);
      draw();
    } catch (e) { $("#up").innerHTML = `<div class="notice bad">${esc(e.message)}</div>`; }
  }
  let seq = 0;  // a slow earlier request must not overwrite the list for the filter chosen since
  const draw = async () => {
    const mine = ++seq;
    const r = await api("/api/gov/documents" + qs({ doc_type: type, match_status: ms, q, page }));
    if (mine !== seq) return;
    $("#dl").innerHTML = `<p class="muted small">${r.total} document(s)</p>` + table([{ h: "File", cls: "desc", f: x => esc(x.filename) + sample(x.sample) }, { h: "Type", f: x => `${esc(x.type_label)}<br><span class="muted small">confidence ${pct(x.type_confidence)}</span>` },
      { h: "Pages", cls: "right", f: x => x.pages + (x.ocr_pages ? ` <span class="badge b-neutral" title="OCR used">OCR ${x.ocr_pages}</span>` : "") + (x.ocr_flagged_pages ? ` <span class="badge b-bad" title="Pages that need a person to check the text">${x.ocr_flagged_pages} to check</span>` : "") }, { h: "Match", f: x => `<span class="badge ${x.match_status === "auto" || x.match_status === "verified" || x.match_status === "manual" ? "b-official" : x.match_status === "needs_review" || x.match_status === "provisional" ? "b-inferred" : "b-neutral"}">${esc(label(x.match_status))}</span>` },
      { h: "Best candidate", cls: "desc", f: x => x.best_candidate ? `<span class="small">${esc(x.best_candidate.label)}<br>${pct(x.best_candidate.confidence)} · ${esc(x.best_candidate.method)}</span>` : '<span class="muted small">none</span>' }], r.items, { click: true, empty: "No documents yet. Upload PDFs above." });
    onRows($("#dl"), r.items, x => openDoc(x.id));
  };
  $("#dt").onchange = e => { type = e.target.value; draw(); }; $("#dm").onchange = e => { ms = e.target.value; draw(); }; $("#dq").onchange = e => { q = e.target.value; draw(); };
  await draw();
};
const FIELD_LABEL = { tender_id: "Tender ID", job_number: "BBMP job number", work_id: "Work / contract ID", work_order_id: "Work order no.", description: "Work description", road_name: "Road", location: "Location", ward: "Ward", department: "Department", contractor: "Contractor", estimated_cost: "Estimated cost", awarded_amount: "Awarded amount", work_order_date: "Work order date", commencement_date: "Commencement date", completion_deadline: "Completion deadline", payment_amount: "Payment amount", payment_date: "Payment date", document_date: "Document date", document_type: "Document type", amount: "Amount (generic)", date: "Date (generic)" };
const num2 = v => v == null ? "–" : Number(v).toLocaleString("en-IN", { maximumFractionDigits: 2 });
const BOQ_BADGE = { ok: ["b-ok", "adds up"], mismatch: ["b-bad", "does not add up"], incomplete: ["b-inferred", "cannot check"] };
function boqSection(bq) {
  if (!bq) return "";
  const s = bq.summary;
  const head = bq.items.length ? `<p class="small">${s.items} items. Printed amounts add up to <b>${inr(s.sum_of_item_amounts)}</b>${s.printed_total != null ? `; the document prints a total of <b>${inr(s.printed_total)}</b>` : ""}.
     ${s.mismatches ? `<span class="badge b-bad">${s.mismatches} row${s.mismatches > 1 ? "s" : ""} do not add up</span>` : '<span class="badge b-ok">all arithmetic checks pass</span>'}${s.incomplete ? ` <span class="badge b-inferred">${s.incomplete} cannot be checked</span>` : ""}${s.reviewed ? ` <span class="small muted">${s.reviewed} reviewed by an officer</span>` : ""}</p>` : `<p class="muted small">${esc(bq.note || "No BOQ rows.")}</p>`;
  const tbl = bq.items.length ? `<div style="overflow-x:auto">` + table([
    { h: "Page", cls: "right", f: b => b.page }, { h: "No.", f: b => esc(b.item_no || "") },
    { h: "Description", cls: "desc", f: b => b.kind === "item" ? esc(b.description || "") : `<b>${esc(b.description || b.kind)}</b>` },
    { h: "Unit", f: b => esc(b.unit || "") }, { h: "Qty", cls: "right", f: b => num2(b.quantity) }, { h: "Rate", cls: "right", f: b => num2(b.unit_rate) },
    { h: "Amount printed", cls: "right", f: b => num2(b.total_amount) },
    { h: "Qty × rate", cls: "right", f: b => b.kind === "item" ? num2(b.calc_total) : `<span class="muted">sum ${num2(b.calc_total)}</span>` },
    { h: "Check", cls: "desc", f: b => { const [c, t] = BOQ_BADGE[b.check_status] || ["b-neutral", "–"]; return `<span class="badge ${c}">${t}</span>${b.check_note ? `<br><span class="small muted">${esc(b.check_note)}</span>` : ""}${b.status !== "unverified" ? ` <span class="badge b-official">${esc(b.status)}</span>` : ""}`; } },
    { h: "Review", f: b => b.kind === "item" ? `<span class="nowrap"><button class="btn ghost sm" data-b="${b.id}" data-s="verified" title="Mark correct">✓</button> <button class="btn ghost sm" data-b="${b.id}" data-s="corrected" title="Correct the numbers">Edit</button> <button class="btn ghost sm" data-b="${b.id}" data-s="rejected" title="Not a BOQ row">✗</button></span>` : "" }], bq.items) + `</div>` : "";
  return `<h3 style="margin-top:14px">Bill of quantities</h3>${head}${tbl}<div class="row" style="margin-top:6px"><button class="btn ghost sm" id="boqx">Read the tables again</button></div>`;
}
function ocrSection(o) {
  if (!o || !o.pages || !o.pages.length) return "";
  const sm = o.summary || {}, scanned = o.pages.some(p => p.method.startsWith("ocr") || p.needs_review);
  if (!scanned) return "";
  const flagged = o.pages.filter(p => p.needs_review);
  return `<h3 style="margin-top:14px">Reading quality (OCR)</h3>
   <p class="small">${sm.pages_ocr || 0} of ${sm.pages_total || o.pages.length} pages were read by OCR${sm.mean_confidence != null ? `, mean confidence <b>${Math.round(sm.mean_confidence)}%</b>` : ""}.
    Languages used: ${esc((sm.languages_used || []).join(", ") || "none")}${sm.kannada_chars ? `. ${sm.kannada_chars} Kannada characters found` : ""}.
    ${flagged.length ? `<span class="badge b-bad">${flagged.length} page${flagged.length > 1 ? "s" : ""} need manual correction</span>` : '<span class="badge b-ok">no page flagged</span>'}</p>
   ${sm.installed_languages && !sm.installed_languages.includes("kan") ? '<p class="small muted">Kannada language data is not installed on this server, so Kannada text cannot be read.</p>' : ""}
   <div style="overflow-x:auto">${table([{ h: "Page", cls: "right", f: p => p.page }, { h: "Read by", f: p => p.method === "text_layer" ? "text layer" : p.method === "ocr" ? "OCR" : esc(p.method) },
     { h: "Language", f: p => esc(p.language || "–") }, { h: "Confidence", cls: "right", f: p => p.confidence != null ? Math.round(p.confidence) + "%" : "–" },
     { h: "Scan", cls: "desc", f: p => p.method === "ocr" ? `<span class="small">${p.dpi || "?"} dpi${p.deskew_deg ? `, straightened ${p.deskew_deg > 0 ? "+" : ""}${p.deskew_deg}°` : ""}${p.preprocess ? `<br>${esc(p.preprocess)}` : ""}</span>` : "" },
     { h: "Check", cls: "desc", f: p => p.needs_review ? `<span class="badge b-bad">needs correction</span><br><span class="small muted">${esc(p.review_reason || "")}</span><br><button class="btn ghost sm" data-pg="${p.page}">Mark page as checked</button>` : (p.review_reason ? `<span class="small muted">${esc(p.review_reason)}</span>` : "") }], o.pages)}</div>`;
}
function wireOcr(dr, id, reopen) {
  $$("[data-pg]", dr).forEach(b => b.onclick = async () => {
    if (!(await confirmBox("Mark page as checked", "Confirm you compared page " + b.dataset.pg + " with the original and corrected the values below that you found wrong?", "Mark as checked"))) return;
    try { await api(`/api/gov/documents/${id}/pages/${b.dataset.pg}`, { method: "PATCH", form: {} }); toast("Page marked as checked"); reopen(); } catch (e) { toast(e.message, true); }
  });
}
function wireBoq(dr, id, reopen) {
  const x = $("#boqx", dr);
  if (x) x.onclick = async () => {
    try { await api(`/api/gov/documents/${id}/boq/extract`, { method: "POST" }); toast("BOQ tables read again"); reopen(); }
    catch (e) {
      if (!/reviewed/.test(e.message)) return toast(e.message, true);
      if (await confirmBox("Replace reviewed rows?", e.message, "Replace")) { try { await api(`/api/gov/documents/${id}/boq/extract`, { method: "POST", form: { replace: "true" } }); toast("BOQ tables read again"); reopen(); } catch (e2) { toast(e2.message, true); } }
    }
  };
  $$("[data-b]", dr).forEach(b => b.onclick = async () => {
    const form = { status: b.dataset.s };
    if (b.dataset.s === "corrected") {
      const q = prompt("Quantity (leave empty to keep)"); const r = prompt("Unit rate (leave empty to keep)"); const t = prompt("Amount (leave empty to keep)");
      if (q) form.quantity = q; if (r) form.unit_rate = r; if (t) form.total_amount = t;
      if (!q && !r && !t) return;
    }
    try { await api("/api/gov/boq-items/" + b.dataset.b, { method: "PATCH", form }); toast("Saved"); reopen(); } catch (e) { toast(e.message, true); }
  });
}

async function openDoc(id) {
  const d = await api("/api/gov/documents/" + id);
  const prov = d.provenance || {};
  const bq = d.boq || d.doc_type === "boq" ? await api(`/api/gov/documents/${id}/boq`) : null;
  let cur = 1;
  const dr = drawer(`<h2 style="padding-right:70px">${esc(d.filename)}${sample(d.sample)}</h2>
   <p><span class="badge b-neutral">${esc(d.type_label)} · ${pct(d.type_confidence)}</span> ${d.classification_cues && d.classification_cues.length ? `<span class="small muted">because: ${d.classification_cues.slice(0, 4).map(c => esc(c.cue)).join(", ")}</span>` : ""}</p>
   <p>${d.roadwork_id ? `Linked to work #${d.roadwork_id} (${esc(d.match_status)}) <a href="#" id="gow">open work</a>` : `<span class="badge b-inferred">${esc(label(d.match_status))}</span>`}</p>
   <iframe class="pdfbox" id="pdf" title="Document preview" src="/static/blank.html"></iframe><p class="small muted">The browser downloads the file with your sign-in. If the preview stays blank, use "Open PDF".</p>
   <div class="row"><button class="btn sm" id="opdf">Open PDF</button>${prov.source_page_url ? `<a class="btn ghost sm" href="${esc(prov.source_page_url)}" target="_blank" rel="noopener noreferrer">Open source page</a>` : ""}${prov.download_url ? `<a class="btn ghost sm" href="${esc(prov.download_url)}" target="_blank" rel="noopener noreferrer">Open original link</a>` : ""}<button class="btn ghost sm" id="rem">Re-run matching</button>${!d.roadwork_id && d.doc_type !== "other" ? '<button class="btn ghost sm" id="mkwork" title="Start a provisional work profile from the values in this document">Start a work profile from this PDF</button>' : ""}</div>
   <h3 style="margin-top:14px">Where this file came from</h3>
   <dl class="kv"><dt>Source</dt><dd>${esc(prov.source_name || "not recorded")}</dd><dt>Source page</dt><dd>${prov.source_page_url ? `<a href="${esc(prov.source_page_url)}" target="_blank" rel="noopener noreferrer">${esc(prov.source_page_url)}</a>` : "not recorded"}</dd>
   <dt>Direct download link</dt><dd>${prov.download_url ? `<a href="${esc(prov.download_url)}" target="_blank" rel="noopener noreferrer">${esc(prov.download_url)}</a>` : "not recorded"}</dd>
   <dt>Downloaded on</dt><dd>${prov.downloaded_at ? dt(prov.downloaded_at) : "not recorded"}</dd><dt>Original file name</dt><dd>${esc(prov.original_filename || "–")}</dd>
   <dt>Added to InfraSignal</dt><dd>${dtt(prov.uploaded_at)}</dd><dt>SHA-256 checksum</dt><dd class="small" style="word-break:break-all">${esc(prov.sha256 || "–")}</dd></dl>
   <details style="margin:6px 0"><summary>Add or correct the source details</summary>
    <div class="filters" style="margin-top:6px"><div><label for="ps">Source</label><input id="ps" value="${esc(prov.source_name || "")}" maxlength="100"></div>
    <div><label for="pp">Source page URL</label><input id="pp" type="url" value="${esc(prov.source_page_url || "")}"></div>
    <div><label for="pd">Download URL</label><input id="pd" type="url" value="${esc(prov.download_url || "")}"></div>
    <div><label for="pt">Downloaded on</label><input id="pt" type="date" value="${prov.downloaded_at ? String(prov.downloaded_at).slice(0, 10) : ""}"></div></div>
    <button class="btn sm" id="psave">Save source details</button></details>
   <h3 style="margin-top:14px">Extracted fields</h3><p class="small muted">Every value shows its page and method. Verify, correct or reject a value; matching re-runs afterwards. Values are never invented: a field not found is simply absent.</p>
   ${table([{ h: "Field", f: e => esc(FIELD_LABEL[e.field] || e.field) }, { h: "Value", cls: "desc", f: e => `<b>${esc(e.norm || e.raw)}</b>${e.norm && e.raw && e.norm !== e.raw ? `<br><span class="muted small">as written: ${esc(e.raw)}</span>` : ""}${e.snippet ? `<br><span class="muted small">“${esc(e.snippet.slice(0, 110))}”</span>` : ""}` },
      { h: "Page", cls: "right", f: e => e.page }, { h: "Conf.", cls: "right", f: e => pct(e.confidence) }, { h: "Method", f: e => `<span class="small">${esc(e.method)}</span>` },
      { h: "Review", f: e => `<span class="nowrap">${e.status !== "unverified" ? `<span class="badge b-official">${esc(e.status)}</span> ` : ""}<button class="btn ghost sm" data-e="${e.id}" data-s="verified" title="Mark correct">✓</button> <button class="btn ghost sm" data-e="${e.id}" data-s="corrected" title="Correct value">Edit</button> <button class="btn ghost sm" data-e="${e.id}" data-s="rejected" title="Reject value">✗</button></span>` }], d.extractions)}
   ${ocrSection(d.ocr)}
   ${boqSection(bq)}
   <h3 style="margin-top:14px">Match candidates</h3>${d.candidates.length ? linkTable(d.candidates) : '<p class="muted small">No candidate. This document is listed as unmatched.</p>'}
   <div class="row" style="margin-top:8px"><label for="asg" class="small" style="margin:0">Assign by work ID</label><input id="asg" type="number" min="1" style="width:110px" placeholder="work #"><button class="btn sm" id="asgb">Assign</button></div>
   <details style="margin-top:12px"><summary>Decision history</summary>${d.history.map(h => `<div class="small">${dtt(h.at)} · ${esc(h.action)} <span class="muted">${esc(JSON.stringify(h.detail).slice(0, 160))}</span></div>`).join("") || '<span class="muted small">None</span>'}</details>`);
  try {
    const a = Auth.get(); const r = await fetch(`/api/gov/documents/${id}/file`, { headers: { Authorization: "Bearer " + a.token } });
    if (r.ok) { const url = URL.createObjectURL(await r.blob()); $("#pdf", dr).src = url; $("#opdf", dr).onclick = () => window.open(url); }
  } catch { }
  const mk = $("#mkwork", dr); if (mk) mk.onclick = async () => {
    if (!(await confirmBox("Start a work profile", "Create a provisional work from the values in this PDF? It stays marked as provisional until an officer confirms it.", "Create"))) return;
    try { const r = await api(`/api/gov/documents/${id}/create-work`, { method: "POST" }); toast(r.duplicate_prevented ? "A matching work already exists; it was suggested instead" : "Provisional work started"); openDoc(id); } catch (e) { toast(e.message, true); }
  };
  $("#psave", dr).onclick = async () => {
    const v = { source_name: $("#ps", dr).value.trim(), source_page_url: $("#pp", dr).value.trim(), download_url: $("#pd", dr).value.trim(), downloaded_at: $("#pt", dr).value };
    const clear = Object.keys(v).filter(k => !v[k] && prov[k]);  // an emptied box removes the stored value
    try { await api(`/api/gov/documents/${id}/source`, { method: "PATCH", form: { ...v, clear: clear.join(",") } }); toast("Source details saved"); openDoc(id); } catch (e) { toast(e.message, true); }
  };
  const gow = $("#gow", dr); if (gow) gow.onclick = e => { e.preventDefault(); openWork(d.roadwork_id); };
  $("#rem", dr).onclick = async () => { try { const r = await api(`/api/gov/documents/${id}/rematch`, { method: "POST" }); toast("Matching re-run: " + r.auto + " auto, " + r.needs_review + " to review, " + r.unmatched + " unmatched"); openDoc(id); } catch (e) { toast(e.message, true); } };
  $("#asgb", dr).onclick = async () => { const v = $("#asg", dr).value; if (!v) return; if (!(await confirmBox("Assign document", "Link this document to work #" + v + " as an officer-confirmed match?", "Assign"))) return; try { await api(`/api/gov/documents/${id}/assign`, { method: "POST", form: { roadwork_id: v } }); toast("Assigned"); openDoc(id); } catch (e) { toast(e.message, true); } };
  $$("[data-e]", dr).forEach(b => b.onclick = async () => {
    let nv; if (b.dataset.s === "corrected") { nv = prompt("Correct value"); if (!nv) return; }
    try { await api("/api/gov/extractions/" + b.dataset.e, { method: "PATCH", form: { status: b.dataset.s, norm_value: nv } }); toast("Saved"); openDoc(id); } catch (e) { toast(e.message, true); }
  });
  wireBoq(dr, id, () => openDoc(id));
  wireOcr(dr, id, () => openDoc(id));
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
  $("#run").onclick = async () => {
    const b = $("#run");
    if (b.disabled) return;
    b.disabled = true; b.textContent = "Matching…";
    try { await api("/api/gov/matching/run", { method: "POST", form: { documents: true, tenders: true, roads: true } }); toast("Matching finished"); go("matching"); }
    catch (e) { toast(e.message, true); }
    finally { if (b.isConnected) { b.disabled = false; b.textContent = "Run matching now"; } }
  };
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


/* ---------- collected data (BBMP open data, automatic collection) ---------- */
const CF = { fiscal_year: "", ward: "", contractor: "" };
const RUN_BADGE = { done: ["b-ok", "Done"], done_with_errors: ["b-inferred", "Done, with problems"], failed: ["b-bad", "Failed"], running: ["b-neutral", "Running"] };
const FILE_BADGE = { imported: ["b-ok", "Imported"], import_failed: ["b-bad", "Import failed"], download_failed: ["b-bad", "Download failed"], downloaded: ["b-neutral", "Downloaded"], found: ["b-neutral", "Found"], skipped: ["b-neutral", "Skipped"] };
const badgeOf = (map, k) => { const [c, t] = map[k] || ["b-neutral", label(k)]; return `<span class="badge ${c}">${esc(t)}</span>`; };
const CMISSING = { no_contractor: "No contractor named", no_ward: "No ward", no_description: "No description", no_road_name: "No road name found in the description", no_department: "No department (not published)",
  no_estimated_amount: "No estimated amount (not published)", no_approved_amount: "No approved amount (not published)", no_work_order_date: "No work-order date (not published)",
  no_payment_recorded: "No payment recorded", non_numeric_ward: "Ward is not a single ward number (for example several wards)" };

async function saveStored(id, name) {
  const a = Auth.get(), r = await fetch(`/api/gov/collection/files/${id}/download`, { headers: { Authorization: "Bearer " + a.token } });
  if (!r.ok) return toast("No stored copy of this file", true);
  const url = URL.createObjectURL(await r.blob()), l = document.createElement("a"); l.href = url; l.download = name || "bbmp-file.csv"; l.click(); setTimeout(() => URL.revokeObjectURL(url), 2000);
}

ME.views.collection = async el => {
  let timer = null, summaryFor = null;
  const stop = () => { if (timer) { clearInterval(timer); timer = null; } };
  el.innerHTML = `<h1>Collected data</h1>
   <p class="muted">Work orders and payments collected from BBMP open data (data.opencity.in). Every figure below is counted from the database. Sample records are never included here. The published files give a payment for each work order; they do not give estimated or approved amounts or work-order dates, so those stay empty rather than being guessed.</p>
   <div id="cstate"></div><div id="cfilters" style="margin-top:14px"></div><div id="csum"></div><div id="chist"></div>`;
  const wardOpts = (OPTS.wards || []).map(w => ({ v: w.value, t: `${w.number}${w.regime ? " (" + w.regime + " map)" : ""}` }));

  async function drawState() {
    const [st, sam] = await Promise.all([api("/api/gov/collection/status"), api("/api/gov/dashboard?origin=sample")]);
    const run = st.running, last = st.last_run, ok = st.last_successful_collection, sch = st.schedule, failedFiles = (st.files_by_status.download_failed || 0) + (st.files_by_status.import_failed || 0);
    const admin = Auth.get().role === "admin";
    $("#cstate").innerHTML = `
     ${sam.totals.roadworks ? `<div class="notice info" role="note"><b>${sam.totals.roadworks} sample road work(s) exist in this database.</b> They are labelled SAMPLE (synthetic) and are left out of everything on this page. On the Overview, set "Data origin" to "Collected from BBMP open data" to see the same numbers.</div>` : ""}
     <div class="card" style="margin-top:12px"><div class="grid g3">
       <div><b>Last successful collection</b><p>${ok ? dtt(ok) : "None yet"}</p><p class="small muted">A collection counts as successful when every file was downloaded and every row was accepted.</p></div>
       <div><b>Last run</b><p>${last ? `${badgeOf(RUN_BADGE, last.status)} ${dtt(last.started_at)} (${esc(label(last.trigger))})` : "No run yet"}</p>
         ${last ? `<p class="small muted">${last.files_downloaded} downloaded, ${last.files_unchanged} unchanged, ${last.files_failed} failed; rows +${last.records_inserted}, ${last.records_duplicate} duplicate, ${last.records_failed} failed</p>` : ""}</div>
       <div><b>Schedule</b><p>${sch.enabled ? `On: ${esc(sch.cron)} (${esc(sch.timezone)})${sch.next_run ? "<br>Next run " + dtt(sch.next_run) : ""}` : "Off. Runs only when started here."}</p>
         ${admin ? '<button class="btn ghost sm" id="csched">Change schedule</button>' : ""}</div></div>
       <div class="row" style="margin-top:12px;gap:8px;flex-wrap:wrap;align-items:flex-end">
         <div><label for="cds">Dataset</label><select id="cds"><option value="">All configured datasets</option>${st.configured_datasets.map(d => `<option value="${esc(d.slug)}">${esc(d.fiscal_year || d.slug)}${d.enabled ? "" : " (off)"}</option>`).join("")}</select></div>
         <button class="btn primary" id="crun" ${run ? "disabled" : ""}>${run ? "Collecting…" : "Collect now"}</button>
         <button class="btn" id="cretry" ${failedFiles && !run ? "" : "disabled"}>Retry failed files (${failedFiles})</button>
         <label class="row" style="font-weight:400"><input type="checkbox" id="cforce" style="width:auto;min-height:0"> Download again even if unchanged</label></div>
       <p class="small muted" style="margin-bottom:0">${st.notes.map(esc).join(" ")}</p></div>`;
    $("#crun").onclick = async () => {
      try { const r = await api("/api/gov/collection/run", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ datasets: $("#cds").value ? [$("#cds").value] : null, force: $("#cforce").checked }) }); toast("Collection started"); watch(); }
      catch (e) { toast(e.message, true); }
    };
    $("#cretry").onclick = async () => { try { await api("/api/gov/collection/retry", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }); toast("Retrying failed files"); watch(); } catch (e) { toast(e.message, true); } };
    if ($("#csched")) $("#csched").onclick = () => scheduleForm(sch);
    return !!run;
  }
  function watch() {
    stop(); const tick = async () => { try { const busy = await drawState(); if (!busy) { stop(); drawAll(); } } catch { stop(); } };
    tick(); timer = setInterval(tick, 3000);
    const obs = new MutationObserver(() => { if (!document.body.contains(el) || $("#view") !== el) { stop(); obs.disconnect(); } }); obs.observe(document.body, { childList: true, subtree: true });
  }
  function scheduleForm(sch) {
    const d = drawer(`<h2>Schedule</h2><p class="small muted">Standard 5-field cron: minute hour day-of-month month day-of-week (0 = Sunday). Example: <code>0 3 * * 0</code> is every Sunday at 03:00.</p>
      <label class="row" style="font-weight:400"><input type="checkbox" id="sen" style="width:auto;min-height:0" ${sch.enabled ? "checked" : ""}> Collect on a schedule</label>
      <label for="scr">Cron</label><input id="scr" value="${esc(sch.cron)}"><label for="stz">Time zone</label><input id="stz" value="${esc(sch.timezone)}"><button class="btn primary" id="ssv" style="margin-top:10px">Save schedule</button>`);
    $("#ssv", d).onclick = async () => {
      try { await api("/api/gov/collection/schedule", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled: $("#sen", d).checked, cron: $("#scr", d).value.trim(), timezone: $("#stz", d).value.trim() }) }); closeDrawer(); toast("Schedule saved"); drawState(); }
      catch (e) { toast(e.message, true); }
    };
  }

  async function drawSummary() {
    const s = await api("/api/gov/collection/summary" + qs(CF)), t = s.totals;
    const fy = OPTS.fiscal_years || [];
    $("#cfilters").innerHTML = `<div class="filters" role="search" aria-label="Filters for collected data">
      <div><label for="cf-fy">Financial year</label><select id="cf-fy"><option value="">All</option>${fy.map(y => `<option${CF.fiscal_year === y ? " selected" : ""}>${esc(y)}</option>`).join("")}</select></div>
      <div><label for="cf-w">Ward</label><select id="cf-w"><option value="">All</option>${wardOpts.map(o => `<option value="${esc(o.v)}"${CF.ward === o.v ? " selected" : ""}>${esc(o.t)}</option>`).join("")}</select></div>
      <div><label for="cf-c">Contractor contains</label><input id="cf-c" value="${esc(CF.contractor)}"></div><div><button class="btn ghost sm" id="cf-x">Clear filters</button></div></div>`;
    const re = () => drawSummary().catch(e => toast(e.message, true));
    $("#cf-fy").onchange = e => { CF.fiscal_year = e.target.value; re(); }; $("#cf-w").onchange = e => { CF.ward = e.target.value; re(); };
    $("#cf-c").onchange = e => { CF.contractor = e.target.value.trim(); re(); }; $("#cf-x").onclick = () => { CF.fiscal_year = CF.ward = CF.contractor = ""; re(); };
    if (!t.works && !s.sources.length && !s.origin.filters_active) {
      $("#csum").innerHTML = `<div class="empty" style="margin-top:14px"><b>Nothing has been collected yet.</b><br>Press "Collect now", or import a file you downloaded yourself (below).</div>`; return;
    }
    const notPub = (n, what) => n ? crore(n) : `<span title="Not in the published files">Not published</span>`;
    $("#csum").innerHTML = `
     <div class="grid g4" style="margin-top:14px">
       <div class="stat"><b>${t.works}</b><span>Work orders${s.origin.filters_active ? " in view" : ""}</span></div>
       <div class="stat"><b>${t.contractors}</b><span>Contractors named</span></div>
       <div class="stat"><b>${t.wards}</b><span>Wards with work orders</span></div>
       <div class="stat"><b>${t.payments}</b><span>Payments, each counted once${t.repeat_payments_excluded ? `<br>${t.repeat_payments_excluded} repeat record(s) of the same payment left out (${crore(t.repeat_payment_amount_excluded)})` : ""}</span></div>
       <div class="stat"><b>${crore(t.billed_gross)}</b><span>Billed (before deductions)</span></div>
       <div class="stat"><b>${crore(t.deductions)}</b><span>Deductions</span></div>
       <div class="stat"><b>${crore(t.paid_net)}</b><span>Paid (net, with an RTGS record). Not a measure of completion</span></div>
       <div class="stat ${t.unmatched_source_records ? "warn" : ""}"><b>${t.unmatched_source_records}</b><span>Rows not linked to any work order</span></div>
       <div class="stat warn"><b>${notPub(t.estimated_amount)}</b><span>Estimated amount${t.works_with_estimated_amount ? ` (${t.works_with_estimated_amount} work orders)` : " (the published files do not contain it)"}</span></div>
       <div class="stat warn"><b>${notPub(t.approved_amount)}</b><span>Approved amount${t.works_with_approved_amount ? ` (${t.works_with_approved_amount} work orders)` : " (the published files do not contain it)"}</span></div>
       <div class="stat ${t.failed_rows_reported ? "bad" : ""}"><b>${t.failed_rows_reported}</b><span>Rows rejected as invalid (listed under "Rows with problems")</span></div></div>
     <div class="grid g2" style="margin-top:14px">
       <div class="card"><h3>Work orders and payments by financial year</h3>${table([{ h: "Year", f: r => esc(r.fiscal_year || "Unknown") }, { h: "Work orders", cls: "right num", f: r => r.works }, { h: "Billed", cls: "right num", f: r => crore(r.billed) },
          { h: "Paid", cls: "right num", f: r => crore(r.paid) }, { h: "Payments", cls: "right num", f: r => r.payments }], s.by_fiscal_year)}
         <p class="small muted">A payment is shown in the year of the file that published it.</p></div>
       <div class="card"><h3>Work orders by ward</h3><div class="chart-box"><canvas id="ch-cward"></canvas></div></div></div>
     <div class="grid g2" style="margin-top:14px">
       <div class="card"><h3>Contractors by amount paid</h3>${table([{ h: "Contractor", f: r => esc(r.name) + (r.vendor_code ? ` <span class="small muted">code ${esc(r.vendor_code)}</span>` : "") }, { h: "Work orders", cls: "right num", f: r => r.works }, { h: "Paid", cls: "right num", f: r => crore(r.paid) }], s.contractors.slice(0, 15), { empty: "No contractor named in these records." })}
         <p class="small muted">Names are as printed in the files (often shortened). No phone numbers are kept.</p></div>
       <div class="card"><h3>Missing information</h3>${table([{ h: "What is missing", f: r => esc(CMISSING[r[0]] || label(r[0])) }, { h: "Work orders", cls: "right num", f: r => r[1] }, { h: "Share", cls: "right num", f: r => t.works ? pct(r[1] / t.works) : "–" }], Object.entries(s.missing))}</div></div>
     <div class="card" style="margin-top:14px"><h3>Rows not linked to any work order</h3>${table([{ h: "Year", f: r => esc(r.fiscal_year || "") }, { h: "Row", cls: "right num", f: r => r.row }, { h: "As published", cls: "desc", f: r => `<span class="small">${esc(Object.entries(r.raw).map(([k, v]) => k + ": " + v).join("; "))}</span>` }], s.unmatched, { empty: "Every collected row is linked to a work order." })}</div>
     <div class="card" style="margin-top:14px"><h3>Where these records come from</h3>${table([{ h: "File", cls: "desc", f: r => esc(r.filename || "") + (r.manual ? ' <span class="badge b-neutral">uploaded by an officer</span>' : "") }, { h: "Year", f: r => esc(r.fiscal_year || "") }, { h: "Wards", f: r => esc(r.ward_regime || "") },
          { h: "Downloaded", f: r => dt(r.downloaded_at) }, { h: "Rows", cls: "right num", f: r => `${r.rows} (+${r.inserted}, ${r.duplicates} dup, ${r.failed} failed)` },
          { h: "Source", f: r => (r.download_url && /^https?:/.test(r.download_url) ? `<a href="${esc(r.download_url)}" target="_blank" rel="noopener">file</a>` : "") + (r.dataset_url ? ` <a href="${esc(r.dataset_url)}" target="_blank" rel="noopener">dataset page</a>` : "") }], s.sources)}
       <p class="small muted">${s.notes.map(esc).join(" ")}</p></div>`;
    chart("ch-cward", { type: "bar", data: { labels: s.by_ward.slice(0, 20).map(w => w.ward + (w.regime ? " (" + w.regime + ")" : "")), datasets: [{ label: "Work orders", data: s.by_ward.slice(0, 20).map(w => w.works), backgroundColor: "#1f5f8b" }] }, options: { indexAxis: "y", plugins: { legend: { display: false } }, maintainAspectRatio: false, scales: { x: { ticks: { precision: 0 } } } } });
  }

  let pagePay = 1;
  async function drawHistory() {
    const [runs, files, issues, pays] = await Promise.all([api("/api/gov/collection/runs?page_size=10"), api("/api/gov/collection/files?page_size=30"),
      api("/api/gov/collection/issues?page_size=30&kind=failed"), api("/api/gov/collection/payments" + qs({ page: pagePay, page_size: 15, fiscal_year: CF.fiscal_year, ward: CF.ward, contractor: CF.contractor }))]);
    $("#chist").innerHTML = `
     <h2 style="margin-top:22px">Payment history</h2><div class="card">${table([{ h: "Paid on", f: p => dt(p.paid_on) }, { h: "Work order", f: p => `<a href="#" data-work="${p.work_id}">${esc(p.work_order_id || "open")}</a>` }, { h: "Description", cls: "desc", f: p => esc((p.description || "").slice(0, 90)) },
        { h: "Ward", f: p => esc(p.ward || "") }, { h: "Reference", f: p => esc(p.reference || "") }, { h: "Gross", cls: "right num", f: p => inr(p.gross_amount) }, { h: "Deduction", cls: "right num", f: p => inr(p.deduction) },
        { h: "Net paid", cls: "right num", f: p => inr(p.amount) + (p.counted ? "" : ' <span class="badge b-neutral" title="The same payment appears in another file; not added twice">repeat</span>') }, { h: "Year", f: p => esc(p.fiscal_year || "") }], pays.items, { empty: "No payments in view." })}
       <div class="row" style="gap:8px;margin-top:8px"><button class="btn ghost sm" id="pp" ${pagePay <= 1 ? "disabled" : ""}>Previous</button><span class="small muted">Page ${pagePay} of ${Math.max(1, Math.ceil(pays.total / 15))} (${pays.total} payments)</span><button class="btn ghost sm" id="pn" ${pagePay * 15 >= pays.total ? "disabled" : ""}>Next</button></div></div>
     <h2 style="margin-top:22px">Collection history</h2><div class="card">${table([{ h: "Run", f: r => `#${r.id}` }, { h: "Started", f: r => dtt(r.started_at) }, { h: "How", f: r => esc(label(r.trigger)) }, { h: "Result", f: r => badgeOf(RUN_BADGE, r.status) },
        { h: "Files", cls: "right num", f: r => `${r.files_downloaded} new, ${r.files_unchanged} same, ${r.files_failed} failed` }, { h: "Rows", cls: "right num", f: r => `${r.records_processed}: +${r.records_inserted}, ~${r.records_updated}, ${r.records_duplicate} dup, ${r.records_failed} failed` }], runs.items, { click: true, empty: "No runs yet." })}</div>
     <h2 style="margin-top:22px">Files</h2><div class="card">${table([{ h: "File", cls: "desc", f: f => esc(f.original_filename || f.resource_name || "") }, { h: "Year", f: f => esc(f.fiscal_year || "") }, { h: "Wards", f: f => esc(f.ward_regime || "") }, { h: "Status", f: f => badgeOf(FILE_BADGE, f.status) },
        { h: "Checked", f: f => dtt(f.last_checked_at) }, { h: "Checksum", f: f => f.sha256 ? `<code title="${esc(f.sha256)}">${esc(f.sha256.slice(0, 10))}…</code>` : "" }, { h: "Rows", cls: "right num", f: f => `${f.rows_processed || 0} (${f.rows_inserted || 0} new, ${f.rows_duplicate || 0} dup, ${f.rows_failed || 0} failed)` },
        { h: "Problem", cls: "desc", f: f => esc(f.error || "") }, { h: "", f: f => `${f.stored ? `<button class="btn ghost sm" data-dl="${f.id}">Save copy</button>` : ""}${["download_failed", "import_failed"].includes(f.status) ? ` <button class="btn sm" data-rt="${f.id}">Retry</button>` : ""}` }], files.items, { empty: "No files yet." })}</div>
     <h2 style="margin-top:22px">Rows with problems</h2><div class="card">${table([{ h: "File", f: i => "#" + i.file_id }, { h: "Row", cls: "right num", f: i => i.row }, { h: "Problem", f: i => `<b>${esc(label(i.code))}</b><br><span class="small">${esc(i.message || "")}</span>` },
        { h: "As published", cls: "desc", f: i => `<span class="small">${esc(i.raw ? Object.entries(i.raw).map(([k, v]) => k + ": " + v).join("; ") : "").slice(0, 200)}</span>` }], issues.items, { empty: "No rejected rows." })}
       ${issues.total > issues.items.length ? `<p class="small muted">Showing ${issues.items.length} of ${issues.total}.</p>` : ""}<p class="small muted">Rows that failed are reported, not stored. Fix the source file or the column settings and import again; rows already stored are never duplicated.</p></div>
     <h2 style="margin-top:22px">Import a file yourself</h2><div class="card"><p class="small muted">Use this when a file was downloaded by hand, or the website could not be read automatically. It goes through the same checks and the same history.</p>
       <div class="grid g3"><div><label for="mf">CSV or Excel file</label><input id="mf" type="file" accept=".csv,.xlsx,.xls"></div><div><label for="mfy">Financial year</label><input id="mfy" placeholder="2024-25"></div>
       <div><label for="mwr">Ward map</label><select id="mwr"><option value="">Choose…</option><option>198</option><option>225</option><option>243</option><option>common</option></select></div></div>
       <label for="mu">Or a web address of one file (public sites only)</label><input id="mu" placeholder="https://…/file.csv"><button class="btn primary" id="mgo" style="margin-top:10px">Import</button><div id="mres"></div></div>`;
    $("#pp").onclick = () => { pagePay--; drawHistory(); }; $("#pn").onclick = () => { pagePay++; drawHistory(); };
    $$("[data-work]", $("#chist")).forEach(a => a.onclick = e => { e.preventDefault(); openWork(+a.dataset.work); });
    $$("[data-dl]", $("#chist")).forEach(b => b.onclick = () => saveStored(+b.dataset.dl, (files.items.find(f => f.id === +b.dataset.dl) || {}).original_filename));
    $$("[data-rt]", $("#chist")).forEach(b => b.onclick = async () => { try { await api("/api/gov/collection/retry", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ file_ids: [+b.dataset.rt] }) }); toast("Retrying"); watch(); } catch (e) { toast(e.message, true); } });
    onRows($("#chist"), runs.items, r => runDetail(r.id));
    $("#mgo").onclick = async () => {
      const f = $("#mf").files[0], u = $("#mu").value.trim(); if (!f && !u) return toast("Choose a file or give a web address", true);
      $("#mgo").disabled = true;
      try {
        const r = f ? await api("/api/gov/collection/manual-import", { method: "POST", form: { file: f, fiscal_year: $("#mfy").value.trim(), ward_regime: $("#mwr").value } })
          : await api("/api/gov/collection/manual-url", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ url: u, fiscal_year: $("#mfy").value.trim(), ward_regime: $("#mwr").value }) });
        const x = r.file, msg = `<div class="notice ${x.status === "imported" ? "ok" : "bad"}" style="margin-top:10px"><b>${esc(label(x.status))}.</b> ${x.rows_processed || 0} rows read: ${x.rows_inserted || 0} new, ${x.rows_duplicate || 0} already stored, ${x.rows_failed || 0} rejected.${x.error ? " " + esc(x.error) : ""}</div>`;
        await drawAll();  // rebuilds this section, so write the result after it
        if ($("#mres")) $("#mres").innerHTML = msg;
      } catch (e) { toast(e.message, true); }
      $("#mgo").disabled = false;
    };
  }
  async function runDetail(id) {
    const r = await api("/api/gov/collection/runs/" + id);
    drawer(`<h2>Run #${r.id} ${badgeOf(RUN_BADGE, r.status)}</h2><p class="small muted">${dtt(r.started_at)} to ${r.finished_at ? dtt(r.finished_at) : "still running"}</p>
      ${table([{ h: "File", cls: "desc", f: f => esc(f.original_filename || f.resource_name || "") }, { h: "Status", f: f => badgeOf(FILE_BADGE, f.status) }, { h: "Problem", cls: "desc", f: f => esc(f.error || "") }], r.files, { empty: "No files in this run." })}
      <h3>What happened</h3><ul class="small">${(r.log || []).map(l => `<li class="${l.level === "error" ? "bad" : ""}">${esc(l.message)}</li>`).join("")}</ul>`);
  }
  const drawAll = () => Promise.all([drawSummary(), drawHistory()]).catch(e => toast(e.message, true));
  const busy = await drawState();
  await drawAll();
  if (busy) watch();
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
  el.innerHTML = `<h1>Contractors</h1><p class="muted">Names are normalised (M/s, Pvt Ltd and spelling variants) so one contractor is counted once. Totals are amounts as published, not proof of completion.</p>
    <p class="small muted">"Awarded" shows "–" when no source publishes an award or sanction amount for the contractor's works: the BBMP open-data files are bill-payment lists and carry only bill and payment amounts. "Paid" is the net amount of the recorded bill payments (a payment recorded twice is counted once); "Billed" is before deductions.</p>` +
    table([{ h: "Contractor", f: c => esc(c.name) + sample(c.sample) }, { h: "Works", cls: "right", f: c => c.works },
      { h: "Awarded", cls: "right num", f: c => c.awarded == null ? "–" : crore(c.awarded) + (c.works_with_award < c.works ? ` <span class="small muted">(${c.works_with_award} of ${c.works} works)</span>` : "") },
      { h: "Payments", cls: "right num", f: c => c.payments || "–" }, { h: "Billed", cls: "right num", f: c => c.payments ? crore(c.gross_billed) : "–" },
      { h: "Paid", cls: "right num", f: c => c.payments ? crore(c.paid) : "–" }], r);
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


/* ---------- live web collection (crawler of the BBMP / GBA website and other portals) ---------- */
const ITEM_BADGE = { processed: ["b-ok", "Processed"], downloaded: ["b-neutral", "Downloaded"], found: ["b-neutral", "Found, not yet downloaded"], download_failed: ["b-bad", "Failed"], skipped: ["b-neutral", "Stored, not parsed"] };
const WF = { category: "", status: "", q: "", page: 1 };
async function saveWebItem(id, name) {
  const a = Auth.get(), r = await fetch(`/api/gov/collection/web/items/${id}/download`, { headers: { Authorization: "Bearer " + a.token } });
  if (!r.ok) return toast("No stored copy of this document", true);
  const url = URL.createObjectURL(await r.blob()), l = document.createElement("a"); l.href = url; l.download = name || "document"; l.click(); setTimeout(() => URL.revokeObjectURL(url), 2000);
}
ME.views.webcrawl = async el => {
  let timer = null;
  el.innerHTML = `<h1>Live web collection</h1>
   <p class="muted">The crawler visits the official GBA / BBMP website and the other configured portals, finds notices, tenders, work orders and payment documents, downloads them and stores what it can read. Every item links back to the page it was listed on and the file as downloaded. Nothing here is typed in by hand.</p>
   <div id="wstate"></div><div id="wfil" style="margin-top:14px"></div><div id="witems"></div><div id="wrecs"></div><div id="wpages"></div>`;
  async function drawState() {
    const s = await api("/api/gov/collection/web/summary"), last = s.last_run, ok = s.last_successful_run, busy = last && last.status === "running";
    const cats = Object.entries(s.documents_by_category).sort((a, b) => b[1] - a[1]);
    $("#wstate").innerHTML = `<div class="card"><div class="grid g3">
      <div><b>Last crawl</b><p>${last ? `${badgeOf(RUN_BADGE, last.status)} ${dtt(last.started_at)}` : "No crawl yet"}</p>
        ${last ? `<p class="small muted">${last.datasets_found} page(s) read, ${last.files_found} document link(s), ${last.files_downloaded} downloaded, ${last.files_unchanged} unchanged, ${last.files_failed} failed; ${last.records_inserted} new record(s)</p>` : ""}</div>
      <div><b>Last successful crawl</b><p>${ok ? dtt(ok.finished_at) : "None yet"}</p><p class="small muted">${s.documents_processed} document(s) processed, ${s.records} table record(s) stored</p></div>
      <div><b>Documents by kind</b><p class="small">${cats.length ? cats.map(([c, n]) => `${esc(label(c))}: ${n}`).join("<br>") : "None yet"}</p></div></div>
      <div class="row" style="margin-top:12px;gap:8px;flex-wrap:wrap;align-items:flex-end">
        <button class="btn primary" id="wrun" ${busy ? "disabled" : ""}>${busy ? "Crawling…" : "Crawl now"}</button>
        <label class="row" style="font-weight:400"><input type="checkbox" id="wforce" style="width:auto;min-height:0"> Download again even if unchanged</label></div>
      <p class="small muted">Sources: ${s.sources.map(x => `<b>${esc(x.name)}</b> (${x.seeds.map(esc).join(", ")})${x.enabled ? "" : " off"}`).join("; ")}</p>
      ${last && last.log ? `<details><summary>Log of the last crawl (${last.log.length} lines)</summary><pre class="small" style="white-space:pre-wrap;max-height:300px;overflow:auto">${last.log.map(x => esc(`${x.at} [${x.level}] ${x.message}`)).join("\n")}</pre></details>` : ""}</div>`;
    $("#wrun").onclick = async () => {
      try { await api("/api/gov/collection/web/run", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ force: $("#wforce").checked }) }); toast("Crawl started"); watch(); }
      catch (e) { toast(e.message, true); }
    };
    return busy;
  }
  function watch() {
    if (timer) clearInterval(timer);
    const tick = async () => { try { const b = await drawState(); if (!b) { clearInterval(timer); timer = null; drawItems(); drawPages(); } } catch { clearInterval(timer); } };
    tick(); timer = setInterval(tick, 3000);
  }
  async function drawItems() {
    $("#wfil").innerHTML = `<div class="filters" role="search" aria-label="Filter collected documents">
      <div><label for="wf-c">Kind</label><select id="wf-c"><option value="">All</option>${["tender", "work_order", "payment", "budget", "works", "circular", "notice", "other"].map(c => `<option value="${c}"${WF.category === c ? " selected" : ""}>${esc(label(c))}</option>`).join("")}</select></div>
      <div><label for="wf-s">Status</label><select id="wf-s"><option value="">All</option>${Object.entries(ITEM_BADGE).map(([k, v]) => `<option value="${k}"${WF.status === k ? " selected" : ""}>${esc(v[1])}</option>`).join("")}</select></div>
      <div><label for="wf-q">Search</label><input id="wf-q" value="${esc(WF.q)}" placeholder="title, URL or text"></div></div>`;
    ["#wf-c", "#wf-s"].forEach(id => $(id).onchange = () => { WF.category = $("#wf-c").value; WF.status = $("#wf-s").value; drawItems(); });
    $("#wf-q").onchange = () => { WF.q = $("#wf-q").value.trim(); drawItems(); };
    const r = await api("/api/gov/collection/web/items" + qs({ category: WF.category, status: WF.status, q: WF.q, page_size: 100 }));
    $("#witems").innerHTML = `<div class="card" style="margin-top:14px"><h3>Documents found (${r.total})</h3>${table([
      { h: "Kind", f: i => esc(label(i.category)) },
      { h: "Title as published", cls: "desc", f: i => `${esc(i.label || "(no link text)")}${i.still_listed ? "" : ' <span class="badge b-neutral">no longer listed</span>'}<br><a class="small" href="${esc(i.url)}" target="_blank" rel="noopener noreferrer">${esc(i.url)}</a>${i.text_excerpt ? `<br><span class="small muted">${esc(i.text_excerpt.slice(0, 200))}</span>` : ""}` },
      { h: "Listed on", f: i => `<a class="small" href="${esc(i.found_on)}" target="_blank" rel="noopener noreferrer">${esc(new URL(i.found_on).pathname)}</a><br><span class="small muted">via ${esc(i.discovered_via)}</span>` },
      { h: "First seen", f: i => dtt(i.first_seen_at) },
      { h: "Status", f: i => `${badgeOf(ITEM_BADGE, i.status)}${i.error ? `<br><span class="small muted">${esc(i.error.slice(0, 160))}</span>` : ""}${i.versions > 1 ? `<br><span class="small">${i.versions} versions</span>` : ""}` },
      { h: "Stored", f: i => i.stored ? `<button class="btn ghost sm" data-dl="${i.id}">File</button>${i.records_extracted ? `<br><span class="small">${i.records_extracted} rows</span>` : ""}` : "" }],
      r.items, { empty: "Nothing collected yet. Press “Crawl now”." })}</div>`;
    $("#witems").querySelectorAll("[data-dl]").forEach(b => b.onclick = () => { const it = r.items.find(x => x.id == b.dataset.dl); saveWebItem(it.id, it.url.split("/").pop()); });
    const rec = await api("/api/gov/collection/web/records?page_size=50");
    $("#wrecs").innerHTML = `<div class="card" style="margin-top:14px"><h3>Records read from web tables and documents (${rec.total})</h3>${table([
      { h: "From", f: x => esc(label(x.origin)) },
      { h: "As published", cls: "desc", f: x => `<span class="small">${esc(Object.entries(x.raw).filter(([k]) => k !== "_links").map(([k, v]) => k + ": " + v).join("; ").slice(0, 400))}</span>` },
      { h: "Read as", f: x => `<span class="small">${esc([x.normalised.category, x.normalised.tender_id, x.normalised.amount ? "Rs " + Math.round(x.normalised.amount).toLocaleString("en-IN") : "", x.normalised.ward ? "ward " + x.normalised.ward : ""].filter(Boolean).join(" · "))}</span>` },
      { h: "Page", f: x => `<a class="small" href="${esc(x.page_url)}" target="_blank" rel="noopener noreferrer">${esc(new URL(x.page_url).pathname)}</a>` }],
      rec.items, { empty: "No tables found yet." })}</div>`;
  }
  async function drawPages() {
    const p = await api("/api/gov/collection/web/pages");
    $("#wpages").innerHTML = `<details class="card" style="margin-top:14px"><summary><b>Pages visited (${p.items.length})</b></summary>${table([
      { h: "Page", cls: "desc", f: x => `<a href="${esc(x.url)}" target="_blank" rel="noopener noreferrer">${esc(x.url)}</a><br><span class="small muted">${esc(x.title || "")}</span>` },
      { h: "Status", f: x => `${esc(x.status)}${x.error ? `<br><span class="small muted">${esc(x.error.slice(0, 160))}</span>` : ""}` },
      { h: "Read via", f: x => esc(x.via || "") }, { h: "Links", cls: "right num", f: x => x.links_found }, { h: "Tables", cls: "right num", f: x => x.tables_found },
      { h: "Last changed", f: x => x.last_changed_at ? dtt(x.last_changed_at) + (x.times_changed ? ` (${x.times_changed}×)` : "") : "" }],
      p.items, { empty: "No pages visited yet." })}</details>`;
  }
  await drawState(); drawItems(); drawPages();
};


/* ---------- audit log (admin only: who changed what, and when) ---------- */
ME.views.audit = async el => {
  el.innerHTML = `<h1>Audit log</h1><p class="muted">The last 300 actions taken by staff: imports, collection runs, link reviews, alert and complaint status changes, field corrections. Visible to administrators only.</p><div id="aud"></div>`;
  try {
    const rows = await api("/api/admin/audit");
    $("#aud").innerHTML = `<div class="card">${table([
      { h: "When", f: a => dtt(a.at) }, { h: "User", cls: "num", f: a => esc(String(a.user ?? "system")) }, { h: "Action", f: a => esc(label(a.action)) },
      { h: "Record", f: a => esc(`${a.entity || ""} ${a.entity_id ?? ""}`) },
      { h: "Detail", cls: "desc", f: a => `<span class="small">${esc(JSON.stringify(a.detail || {}).slice(0, 300))}</span>` }], rows, { empty: "No actions recorded yet." })}</div>`;
  } catch (e) { $("#aud").innerHTML = `<div class="notice">${esc(e.message)}</div>`; }
};
