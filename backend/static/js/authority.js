const AV = {};
const VIEWS = [["overview", "Overview"], ["clusters", "Issue clusters"], ["reports", "All reports"], ["alerts", "Alerts"], ["unmatched", "No government record"], ["settings", "Alert settings"]];
let CAN_WRITE = false;
const SEV_ORDER = { high: 0, medium: 1, low: 2 };

function start(auth) {
  CAN_WRITE = ["authority", "admin"].includes(auth.role);
  document.body.innerHTML = `<header class="topbar"><a class="brand" href="/citizen"><i></i>InfraSignal</a><span class="area">Authority dashboard</span><span class="spacer"></span>
   <a class="link" href="/gov">Government monitoring</a><span class="small" style="color:#c3ccd2">${esc(auth.email)} (${esc(auth.role)})</span><button class="btn ghost sm" id="out" style="color:#fff;border-color:#46525b">Sign out</button></header>
   <div class="banner" id="banner" hidden></div>
   <div class="shell"><nav class="side" aria-label="Sections">${VIEWS.map(([k, t]) => `<button data-v="${k}" aria-selected="false">${t}</button>`).join("")}</nav><main class="main" id="view" tabindex="-1"></main></div>`;
  $("#out").onclick = () => { Auth.clear(); location.reload(); };
  $$(".side button").forEach(b => b.onclick = () => go(b.dataset.v));
  if (!CAN_WRITE) { const b = $("#banner"); b.hidden = false; b.textContent = "Read-only: your role can view complaints but not change their status."; }
  go((location.hash || "#overview").slice(1));
}
function go(v) {
  if (!AV[v]) v = "overview";
  history.replaceState(null, "", "#" + v);
  $$(".side button").forEach(b => b.setAttribute("aria-selected", b.dataset.v === v));
  closeDrawer(); const el = $("#view"); el.innerHTML = '<p class="loading">Loading…</p>';
  AV[v](el).catch(e => { el.innerHTML = `<div class="notice bad">${esc(e.message)}</div>`; });
}
/* photos need the sign-in header, so they are fetched as blobs */
async function fillImages(root) {
  const a = Auth.get();
  for (const img of $$("img[data-src]", root)) {
    try { const r = await fetch(img.dataset.src, { headers: { Authorization: "Bearer " + a.token } }); if (r.ok) { img.src = URL.createObjectURL(await r.blob()); img.onclick = () => window.open(img.src); } } catch { }
  }
}
const gm = s => ({ matched: '<span class="badge b-official">Record linked</span>', possible: '<span class="badge b-inferred">Suggested only</span>', unmatched: '<span class="badge b-neutral">No record found</span>' }[s] || "");

/* ---------- overview ---------- */
AV.overview = async el => {
  const o = await api("/api/authority/overview"), t = o.totals, cl = await api("/api/authority/clusters");
  if (t.sample_reports) { const b = $("#banner"); b.hidden = false; b.innerHTML = `${t.sample_reports} reports are <b>synthetic samples</b> (labelled SAMPLE). They are not real complaints.`; }
  el.innerHTML = `<h1>Overview</h1>
   <div class="grid g4"><div class="stat"><b>${t.reports}</b><span>Reports (${t.last_period} in the last 60 days)</span></div><div class="stat"><b>${t.clusters}</b><span>Issue clusters (${t.high_severity_clusters} high)</span></div>
   <div class="stat bad"><b>${t.open_alerts}</b><span>Open alerts</span></div><div class="stat warn"><b>${t.clusters_without_government_record}</b><span>Clusters with no government record</span></div></div>
   <div class="grid g-main" style="margin-top:14px"><div><div id="ovmap" class="map" style="height:430px"></div><p class="small muted">Circle size follows the number of reports. Colour follows severity.</p></div>
   <div class="grid" style="align-content:start"><div class="card"><h3>Reports per day</h3><div class="chart-box" style="height:180px"><canvas id="c-day"></canvas></div></div>
   <div class="card"><h3>By status</h3><div class="chart-box" style="height:180px"><canvas id="c-st"></canvas></div></div></div></div>
   <div class="grid g2" style="margin-top:14px"><div class="card"><h3>Most-reported roads</h3>${table([{ h: "Road", f: r => esc(r.name) }, { h: "Reports", cls: "right", f: r => r.reports }], o.top_roads)}</div>
   <div class="card"><h3>Most-reported wards</h3>${table([{ h: "Ward", f: r => esc(r.ward) }, { h: "Reports", cls: "right", f: r => r.reports }], o.top_wards)}</div></div>
   <p class="small muted">${t.reports_without_road} report(s) are not near any mapped road. They are kept and shown under "All reports".</p>`;
  const map = baseMap("ovmap"); const pts = [];
  cl.forEach(c => { const col = SEV_COLOR[c.severity] || "#5b6770"; L.circle([c.lat, c.lon], { radius: 40 + c.count * 12, color: col, weight: 2, fillOpacity: .25 }).addTo(map).bindTooltip(`#${c.id} ${esc(c.category_label)}: ${c.count} reports`).on("click", () => openCluster(c.id)); pts.push([c.lat, c.lon]); });
  if (pts.length) map.fitBounds(pts, { padding: [30, 30], maxZoom: 14 });
  chart("c-day", { type: "bar", data: { labels: o.per_day.map(x => x.day.slice(5)), datasets: [{ data: o.per_day.map(x => x.count), backgroundColor: "#b3261e" }] }, options: { plugins: { legend: { display: false } }, scales: { x: { ticks: { maxTicksLimit: 8 } } } } });
  chart("c-st", { type: "bar", data: { labels: Object.keys(o.by_status).map(k => STATUS[k] || k), datasets: [{ data: Object.values(o.by_status), backgroundColor: "#4a545c" }] }, options: { indexAxis: "y", plugins: { legend: { display: false } } } });
};

/* ---------- clusters ---------- */
AV.clusters = async el => {
  const f = { status: "", category: "", severity: "", gov: "", sort: "count", q: "" };
  el.innerHTML = `<h1>Issue clusters</h1><p class="muted">Reports grouped by place, time, category, wording and (when photos exist) picture similarity. A cluster shows repeated complaints, not who is responsible.</p>
  <div class="filters"><div><label for="fc">Category</label><select id="fc"><option value="">All</option>${Object.entries(CAT).map(([k, v]) => `<option value="${k}">${v}</option>`).join("")}</select></div>
  <div><label for="fs">Severity</label><select id="fs"><option value="">All</option><option>high</option><option>medium</option><option>low</option></select></div>
  <div><label for="fg">Government record</label><select id="fg"><option value="">All</option><option value="matched">Linked</option><option value="possible">Suggested only</option><option value="unmatched">None found</option></select></div>
  <div><label for="fst">Cluster state</label><select id="fst"><option value="">Active</option><option value="verified">Verified</option><option value="rejected">Rejected</option><option value="auto">Automatic only</option></select></div>
  <div><label for="fo">Sort</label><select id="fo"><option value="count">Most reports</option><option value="recent">Latest report</option><option value="oldest">Oldest first</option></select></div>
  <div><a class="btn ghost sm" href="#" id="csv">Export CSV</a></div></div><div class="grid g-main"><div id="cl"></div><div id="clmap" class="map" style="height:520px"></div></div>`;
  const map = baseMap("clmap"); const layer = L.layerGroup().addTo(map);
  const draw = async () => {
    const r = await api("/api/authority/clusters" + qs(f));
    $("#cl").innerHTML = `<p class="muted small">${r.length} cluster(s)</p>` + table([{ h: "#", f: x => x.id }, { h: "Issue", cls: "desc", f: x => `<b>${esc(x.category_label)}</b><br><span class="small">${esc(x.road || "unmapped road")}${x.ward ? ", " + esc(x.ward) : ""}</span>` },
      { h: "Reports", cls: "right num", f: x => x.count }, { h: "Severity", f: x => sevBadge(x.severity) }, { h: "Last", f: x => dt(x.last) }, { h: "Gov. record", f: x => gm(x.gov_match_status) }, { h: "State", f: x => x.status === "verified" ? '<span class="badge b-official">verified</span>' : x.status === "rejected" ? '<span class="badge b-bad">rejected</span>' : '<span class="badge b-neutral">auto</span>' }], r, { click: true, empty: "No clusters match." });
    onRows($("#cl"), r, x => openCluster(x.id));
    layer.clearLayers(); const pts = [];
    r.forEach(c => { L.circle([c.lat, c.lon], { radius: 40 + c.count * 12, color: SEV_COLOR[c.severity] || "#5b6770", weight: 2, fillOpacity: .25 }).addTo(layer).on("click", () => openCluster(c.id)).bindTooltip(`#${c.id}: ${c.count}`); pts.push([c.lat, c.lon]); });
    if (pts.length) map.fitBounds(pts, { padding: [24, 24], maxZoom: 14 });
  };
  const bind = (id, k) => $(id).onchange = e => { f[k] = e.target.value; draw(); };
  bind("#fc", "category"); bind("#fs", "severity"); bind("#fg", "gov"); bind("#fst", "status"); bind("#fo", "sort");
  $("#csv").onclick = async e => { e.preventDefault(); const a = Auth.get(); const r = await fetch("/api/authority/export/clusters.csv", { headers: { Authorization: "Bearer " + a.token } }); const u = URL.createObjectURL(await r.blob()); const l = document.createElement("a"); l.href = u; l.download = "clusters.csv"; l.click(); };
  await draw();
};
async function openCluster(id) {
  const c = await api("/api/authority/clusters/" + id);
  const d = drawer(`<h2 style="padding-right:70px">#${c.id} ${esc(c.category_label)}</h2><p>${sevBadge(c.severity)} ${gm(c.gov_match_status)} <span class="badge b-neutral">${esc(c.status)}</span></p><p>${esc(c.summary || "")}</p>
   <dl class="kv"><dt>Road</dt><dd>${esc(c.road || "no mapped road")}</dd><dt>Ward</dt><dd>${esc(c.ward || "–")}</dd><dt>Reports</dt><dd>${c.count}, from ${dt(c.first)} to ${dt(c.last)}</dd><dt>Public status</dt><dd>${esc(STATUS[c.public_status] || c.public_status)}</dd></dl>
   ${CAN_WRITE ? `<h3 style="margin-top:14px">Manage</h3><div class="row"><select id="ns" style="width:auto">${Object.entries(STATUS).map(([k, v]) => `<option value="${k}">${v}</option>`).join("")}</select><input id="nn" placeholder="Note shown to citizens (optional)" style="flex:1;min-width:160px"><button class="btn sm" id="setst">Update all ${c.count} reports</button></div>
   <div class="row" style="margin-top:8px"><button class="btn ghost sm" id="ver">Mark as verified</button><button class="btn ghost sm" id="rej">Not a real cluster</button>${c.status !== "auto" ? '<button class="btn ghost sm" id="reo">Return to automatic</button>' : ""}</div>` : ""}
   <h3 style="margin-top:14px">Why these reports were grouped</h3><p class="small muted">Distance, wording similarity, picture similarity and time gap between report pairs.</p>
   <details><summary>Pair evidence (${(c.evidence.pairs || []).length})</summary>${table([{ h: "A", f: p => esc(p.a) }, { h: "B", f: p => esc(p.b) }, { h: "Distance", cls: "right", f: p => p.meters + " m" }, { h: "Days apart", cls: "right", f: p => p.days }, { h: "Text sim.", cls: "right", f: p => p.text_sim }, { h: "Photo sim.", cls: "right", f: p => p.photo_sim ?? "–" }], c.evidence.pairs || [])}</details>
   <h3 style="margin-top:14px">Reports</h3>${c.reports.map(r => `<div class="card" style="margin:8px 0;padding:10px"><div class="row between"><b>${esc(r.ref)}</b><span class="small muted">${dtt(r.created_at)}</span></div>
     <div class="small">${esc(CAT[r.category] || r.category)} · ${esc(STATUS[r.status] || r.status)}${sample(r.sample)}</div><p>${esc(r.description || "(no description)")}</p>
     ${r.road_name_typed ? `<div class="small muted">Citizen wrote: ${esc(r.road_name_typed)}</div>` : ""}
     <div class="g-photos">${r.photos.map(p => `<img alt="Photo for ${esc(r.ref)}" data-src="/api/authority/photos/${p}${p === 0 ? "?report_id=" + r.id : ""}">`).join("")}</div></div>`).join("")}
   <h3 style="margin-top:14px">Related government works</h3><p class="small muted">${esc(c.related_works_note)}</p>
   ${c.related_works.length ? table([{ h: "Work", cls: "desc", f: l => esc(l.b_label) }, { h: "Basis", f: l => `<span class="small">${esc(l.method)}<br>${pct(l.confidence)}</span>` }, { h: "Status", f: l => linkBadge(l.status) },
      { h: "", f: l => CAN_WRITE ? `<span class="nowrap"><button class="btn sm" data-lk="${l.id}" data-s="verified">Verify</button> <button class="btn ghost sm" data-lk="${l.id}" data-s="rejected">Reject</button></span>` : "" }], c.related_works) : '<div class="empty">No government record found for this location.</div>'}
   ${CAN_WRITE ? `<div class="row" style="margin-top:8px"><input id="rwq" placeholder="Search a work by description or job number" style="flex:1"><button class="btn ghost sm" id="rws">Search</button></div><div id="rwr"></div>` : ""}
   <h3 style="margin-top:14px">Alerts</h3>${c.alerts.length ? table([{ h: "Alert", cls: "desc", f: a => esc(a.title) }, { h: "Severity", f: a => sevBadge(a.severity) }, { h: "Status", f: a => esc(a.status) }, { h: "Raised", f: a => dt(a.created_at) }], c.alerts) : '<p class="muted small">None.</p>'}`);
  fillImages(d);
  if (!CAN_WRITE) return;
  const post = async (path, form, msg) => { try { await api(path, { method: "POST", form }); toast(msg); openCluster(id); } catch (e) { toast(e.message, true); } };
  $("#setst", d).onclick = () => post(`/api/authority/clusters/${id}/status`, { status: $("#ns", d).value, note_public: $("#nn", d).value }, "Status updated");
  $("#ver", d).onclick = () => post(`/api/authority/clusters/${id}/review`, { decision: "verified" }, "Cluster verified");
  $("#rej", d).onclick = async () => { if (await confirmBox("Reject cluster", "These reports will be shown as individual reports. Automatic re-clustering will not group them again.", "Reject")) post(`/api/authority/clusters/${id}/review`, { decision: "rejected" }, "Cluster rejected"); };
  const reo = $("#reo", d); if (reo) reo.onclick = () => post(`/api/authority/clusters/${id}/review`, { decision: "auto" }, "Returned to automatic");
  $$("[data-lk]", d).forEach(b => b.onclick = async () => { try { await api("/api/authority/links/" + b.dataset.lk, { method: "PATCH", form: { status: b.dataset.s } }); toast("Saved"); openCluster(id); } catch (e) { toast(e.message, true); } });
  $("#rws", d).onclick = async () => {
    const r = await api("/api/authority/roadworks" + qs({ q: $("#rwq", d).value }));
    $("#rwr", d).innerHTML = r.length ? r.map(w => `<div class="row between" style="padding:4px 0;border-bottom:1px solid var(--line)"><span class="small">${esc(w.job_number || "#" + w.id)}: ${esc((w.description || "").slice(0, 90))}${sample(w.sample)}</span><button class="btn sm" data-w="${w.id}">Link</button></div>`).join("") : '<p class="muted small">No work found.</p>';
    $$("[data-w]", d).forEach(b => b.onclick = () => post(`/api/authority/clusters/${id}/link`, { roadwork_id: b.dataset.w }, "Linked and marked verified"));
  };
}

/* ---------- reports ---------- */
AV.reports = async el => {
  const f = { status: "", category: "", cluster: "", q: "", no_road: "" }; let page = 1;
  el.innerHTML = `<h1>All reports</h1><div class="filters"><div><label for="rs">Status</label><select id="rs"><option value="">All</option>${Object.entries(STATUS).map(([k, v]) => `<option value="${k}">${v}</option>`).join("")}</select></div>
   <div><label for="rc">Category</label><select id="rc"><option value="">All</option>${Object.entries(CAT).map(([k, v]) => `<option value="${k}">${v}</option>`).join("")}</select></div>
   <div><label for="rg">Grouping</label><select id="rg"><option value="">All</option><option value="any">In a cluster</option><option value="none">Not grouped</option></select></div>
   <div><label for="rr">Road</label><select id="rr"><option value="">All</option><option value="1">Not near any mapped road</option></select></div>
   <div><label for="rq">Search</label><input id="rq" type="search" placeholder="text or reference"></div></div><div id="rl"></div>`;
  const draw = async () => {
    const r = await api("/api/authority/reports" + qs({ ...f, page }));
    $("#rl").innerHTML = `<p class="muted small">${r.total} report(s)</p>` + table([{ h: "Ref", f: x => `<span class="nowrap">${esc(x.ref)}</span>${sample(x.sample)}` }, { h: "Problem", cls: "desc", f: x => `<b>${esc(CAT[x.category] || x.category)}</b><br>${esc(x.description || "")}` },
      { h: "Road", f: x => esc(x.road || "–") + (x.road_name_typed ? `<br><span class="small muted">typed: ${esc(x.road_name_typed)}</span>` : "") }, { h: "Cluster", f: x => x.cluster_id ? "#" + x.cluster_id : "–" }, { h: "Photos", cls: "right", f: x => x.photos }, { h: "Status", f: x => esc(STATUS[x.status] || x.status) }, { h: "Received", f: x => dt(x.created_at) }], r.items, { click: true, empty: "No reports." }) +
      `<div class="pager"><button class="btn ghost sm" id="pv" ${page <= 1 ? "disabled" : ""}>Previous</button><span class="small">Page ${page}</span><button class="btn ghost sm" id="nx" ${page * 50 >= r.total ? "disabled" : ""}>Next</button></div>`;
    onRows($("#rl"), r.items, x => openReport(x));
    $("#pv").onclick = () => { page--; draw(); }; $("#nx").onclick = () => { page++; draw(); };
  };
  const bind = (id, k) => $(id).onchange = e => { f[k] = e.target.value; page = 1; draw(); };
  bind("#rs", "status"); bind("#rc", "category"); bind("#rg", "cluster"); bind("#rr", "no_road"); bind("#rq", "q");
  await draw();
};
function openReport(x) {
  const d = drawer(`<h2 style="padding-right:70px">${esc(x.ref)}${sample(x.sample)}</h2><p><b>${esc(CAT[x.category] || x.category)}</b> · ${esc(STATUS[x.status] || x.status)}</p><p>${esc(x.description || "(no description)")}</p>
   <dl class="kv"><dt>Location</dt><dd>${x.lat.toFixed(5)}, ${x.lon.toFixed(5)}</dd><dt>Road</dt><dd>${esc(x.road || "not near a mapped road")}</dd><dt>Ward</dt><dd>${esc(x.ward || "–")}</dd><dt>Cluster</dt><dd>${x.cluster_id ? "#" + x.cluster_id : "not grouped"}</dd><dt>Contact</dt><dd>${esc(x.contact || "none given")}</dd></dl>
   <div class="g-photos" style="margin:10px 0">${x.photo_ids.map(p => `<img alt="Photo for ${esc(x.ref)}" data-src="/api/authority/photos/${p}${p === 0 ? "?report_id=" + x.id : ""}">`).join("")}</div>
   ${CAN_WRITE ? `<h3>Update status</h3><div class="row"><select id="ns" style="width:auto">${Object.entries(STATUS).map(([k, v]) => `<option value="${k}"${k === x.status ? " selected" : ""}>${v}</option>`).join("")}</select><input id="nn" placeholder="Public note (optional)" style="flex:1"><button class="btn sm" id="sv">Save</button></div>` : ""}
   ${x.cluster_id ? `<p style="margin-top:12px"><a href="#" id="oc">Open cluster #${x.cluster_id}</a></p>` : ""}`);
  fillImages(d);
  if (CAN_WRITE) $("#sv", d).onclick = async () => { try { await api(`/api/authority/reports/${x.id}/status`, { method: "PATCH", form: { status: $("#ns", d).value, note_public: $("#nn", d).value } }); toast("Status updated"); closeDrawer(); go("reports"); } catch (e) { toast(e.message, true); } };
  const oc = $("#oc", d); if (oc) oc.onclick = e => { e.preventDefault(); openCluster(x.cluster_id); };
}

/* ---------- alerts ---------- */
AV.alerts = async el => {
  let status = "open", rule = "";
  el.innerHTML = `<h1>Alerts</h1><p class="muted">Alerts flag patterns in reports for a person to look at. They do not say anyone was negligent or that work is delayed. When the condition clears, an alert closes itself and stays in the history.</p>
  <div class="filters"><div><label for="as">Show</label><select id="as"><option value="open">Open</option><option value="acknowledged">Acknowledged</option><option value="resolved">Resolved</option><option value="dismissed">Dismissed</option><option value="auto_closed">Closed automatically</option><option value="all">Full history</option></select></div>
  <div><label for="ar">Rule</label><select id="ar"><option value="">All rules</option></select></div></div><div id="al"></div>`;
  const rules = await api("/api/authority/alerts/settings"); $("#ar").innerHTML += rules.map(r => `<option value="${r.rule}">${esc(r.title)}</option>`).join("");
  const draw = async () => {
    const r = await api("/api/authority/alerts" + qs({ status, rule }));
    $("#al").innerHTML = `<p class="muted small">${r.length} alert(s)</p>` + (r.length ? r.map(a => `<div class="card" style="margin:8px 0;border-left:6px solid ${SEV_COLOR[a.severity] || "#5b6770"}"><div class="row between"><div><b>${esc(a.title)}</b><div class="small muted">${esc(a.rule_title)} · ${esc(a.road || "")} ${esc(a.ward || "")} · raised ${dtt(a.created_at)}</div></div><div>${sevBadge(a.severity)} <span class="badge b-neutral">${esc(label(a.status))}</span></div></div>
      <p class="small">${esc(a.explanation)}</p>${a.resolution_note ? `<p class="small muted">Note: ${esc(a.resolution_note)}</p>` : ""}
      <div class="row">${a.cluster_id ? `<button class="btn ghost sm" data-c="${a.cluster_id}">Open cluster #${a.cluster_id}</button>` : ""}${(a.evidence && a.evidence.reports) ? `<span class="small muted">${a.evidence.reports.length} report(s): ${a.evidence.reports.slice(0, 4).map(esc).join(", ")}${a.evidence.reports.length > 4 ? "…" : ""}</span>` : ""}
      ${CAN_WRITE && ["open", "acknowledged"].includes(a.status) ? `<span style="flex:1"></span><button class="btn ghost sm" data-a="${a.id}" data-s="acknowledged">Acknowledge</button><button class="btn sm" data-a="${a.id}" data-s="resolved">Resolve</button><button class="btn ghost sm" data-a="${a.id}" data-s="dismissed">Dismiss</button>` : ""}</div></div>`).join("") : '<div class="empty">No alerts in this view.</div>');
    $$("[data-c]", el).forEach(b => b.onclick = () => openCluster(+b.dataset.c));
    $$("[data-a]", el).forEach(b => b.onclick = async () => {
      let note = ""; if (b.dataset.s !== "acknowledged") { note = prompt("Add a short note (optional)") || ""; }
      try { await api("/api/authority/alerts/" + b.dataset.a, { method: "PATCH", form: { status: b.dataset.s, note } }); toast("Alert updated"); draw(); } catch (e) { toast(e.message, true); }
    });
  };
  $("#as").onchange = e => { status = e.target.value; draw(); }; $("#ar").onchange = e => { rule = e.target.value; draw(); };
  await draw();
};

/* ---------- unmatched ---------- */
AV.unmatched = async el => {
  const u = await api("/api/authority/unmatched");
  el.innerHTML = `<h1>Issues with no government record</h1><div class="notice info">${esc(u.note)}</div>
   <h2 style="margin-top:16px">Clusters (${u.clusters.length})</h2>${table([{ h: "#", f: x => x.id }, { h: "Issue", cls: "desc", f: x => `<b>${esc(x.category_label)}</b><br><span class="small">${esc(x.road || "unmapped road")}${x.ward ? ", " + esc(x.ward) : ""}</span>` }, { h: "Reports", cls: "right", f: x => x.count }, { h: "Severity", f: x => sevBadge(x.severity) }, { h: "Last", f: x => dt(x.last) }], u.clusters, { click: true, empty: "Every cluster has at least a suggested record." })}
   <h2 style="margin-top:16px">Single reports (${u.reports.length})</h2>${table([{ h: "Ref", f: x => esc(x.ref) + sample(x.sample) }, { h: "Problem", f: x => esc(CAT[x.category] || x.category) }, { h: "Road", f: x => esc(x.road || "not near a mapped road") }, { h: "Ward", f: x => esc(x.ward || "–") }, { h: "Received", f: x => dt(x.created_at) }], u.reports)}`;
  onRows(el, u.clusters, x => openCluster(x.id));
};

/* ---------- settings ---------- */
AV.settings = async el => {
  const s = await api("/api/authority/alerts/settings");
  const PL = { min_reports: "Minimum reports", min_distinct_days: "Minimum different days", min_span_days: "Minimum span (days)", ratio: "Times the usual weekly rate", radius_m: "Radius (metres)", window_days: "Look-back window (days)", baseline_days: "Baseline period (days)", min_clusters: "Minimum clusters", min_missing: "Minimum missing fields" };
  el.innerHTML = `<h1>Alert settings</h1><p class="muted">Thresholds decide when an alert fires. Changes re-evaluate all alerts immediately. Lower thresholds find problems sooner and raise more false alarms; see the Evaluation page in Government monitoring for measured numbers.</p>` +
    s.map(r => `<form class="card" style="margin:10px 0" data-rule="${r.rule}"><div class="row between"><h3>${esc(r.title)}</h3><label class="row" style="margin:0;font-weight:400"><input type="checkbox" name="enabled" style="width:auto;min-height:0" ${r.enabled ? "checked" : ""} ${CAN_WRITE ? "" : "disabled"}> Enabled</label></div>
    <div class="filters">${Object.entries(r.params).map(([k, v]) => `<div><label for="${r.rule}-${k}">${esc(PL[k] || k)}</label><input id="${r.rule}-${k}" name="${k}" type="number" step="${k === "ratio" ? "0.5" : "1"}" value="${v}" ${CAN_WRITE ? "" : "disabled"}><span class="small muted">default ${r.default_params[k]}</span></div>`).join("")}</div>
    ${CAN_WRITE ? '<button class="btn sm">Save</button>' : ""}</form>`).join("");
  $$("form[data-rule]", el).forEach(f => f.onsubmit = async e => {
    e.preventDefault(); const params = {}; $$("input[type=number]", f).forEach(i => params[i.name] = i.value);
    try { await api("/api/authority/alerts/settings/" + f.dataset.rule, { method: "PUT", form: { enabled: $("[name=enabled]", f).checked, params_json: JSON.stringify(params) } }); toast("Saved. Alerts re-evaluated."); } catch (x) { toast(x.message, true); }
  });
};

loginGate(["authority", "admin", "officer"], "Authority dashboard", start);
