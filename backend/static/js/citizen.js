let pubMap, pickMap, pin, pos = null, roadLayer, clusterLayer, wardLayer, hi;
const tabs = $$(".tabs button");
function show(t) {
  tabs.forEach(b => b.setAttribute("aria-selected", b.dataset.t === t));
  $$("section[role=tabpanel]").forEach(s => s.hidden = s.id !== t);
  if (t === "map") { initPub(); setTimeout(() => pubMap.invalidateSize(), 50); }
  if (t === "report") { initPick(); setTimeout(() => pickMap.invalidateSize(), 50); }
}
tabs.forEach(b => b.onclick = () => { show(b.dataset.t); history.replaceState(null, "", "#" + b.dataset.t); });

/* ---------- public map ---------- */
const roadColor = n => n >= 3 ? "#b3261e" : n >= 1 ? "#c4781a" : "#5b6770";
async function initPub() {
  if (pubMap) return;
  pubMap = baseMap("pub");
  roadLayer = L.layerGroup().addTo(pubMap); clusterLayer = L.layerGroup().addTo(pubMap);
  pubMap.on("moveend", loadRoads);
  await Promise.all([loadRoads(), loadReports(), stats()]);
  $("#wardsOn").onchange = async e => {
    if (!e.target.checked) { if (wardLayer) pubMap.removeLayer(wardLayer); return; }
    const g = await api("/api/public/wards");
    wardLayer = L.geoJSON(g, { style: { color: "#607080", weight: 1, fillOpacity: 0.03 }, onEachFeature: (f, l) => l.bindTooltip(`Ward ${esc(f.properties.number)} ${esc(f.properties.name || "")}: ${f.properties.reports} report(s)`, { sticky: true }) }).addTo(pubMap);
    wardLayer.bringToBack();
  };
  let t; $("#rq").oninput = e => { clearTimeout(t); t = setTimeout(searchRoad, 250); };
}
async function stats() {
  const s = await api("/api/public/stats");
  if (s.sample_reports) { const b = $("#banner"); b.hidden = false; b.innerHTML = `Demonstration mode: ${s.sample_reports} of the ${s.reports} reports are <b>synthetic samples</b>, and government records are sample records. They are labelled and are not real.`; }
  $("#mapnote").textContent = `${s.roads} roads mapped, ${s.roadworks_on_record} government work records loaded, ${s.wards} wards (${s.ward_regime || "none"} regime).`;
}
let roadReq = 0;
async function loadRoads() {
  const my = ++roadReq, b = pubMap.getBounds();
  const g = await api("/api/public/roads?bbox=" + [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()].join(","));
  if (my !== roadReq) return;
  roadLayer.clearLayers();
  L.geoJSON(g, {
    style: f => ({ color: roadColor(f.properties.reports), weight: f.properties.reports ? 6 : 4, opacity: 0.85 }),
    onEachFeature: (f, l) => { const p = f.properties; l.bindTooltip(`${esc(p.name)}${p.ward ? ", ward " + esc(p.ward) : ""}: ${p.reports} report(s) on this segment`, { sticky: true }); l.on("click", () => openRoad(p.road_id)); },
  }).addTo(roadLayer);
}
async function loadReports() {
  const j = await api("/api/public/map");
  clusterLayer.clearLayers();
  j.clusters.forEach(c => L.circle([c.lat, c.lon], { radius: 35 + c.count * 8, color: "#b3261e", weight: 2, fillOpacity: 0.18 }).addTo(clusterLayer)
    .bindPopup(`<b>${esc(CAT[c.category] || c.category)}</b><br>${c.count} similar reports${c.road ? " on " + esc(c.road) : ""}<br>First ${dt(c.first)}, latest ${dt(c.last)}<br>Status: ${esc(STATUS[c.status] || c.status)}<br><span class="small">Reported by citizens, not confirmed.</span>`));
  j.reports.forEach(r => L.circleMarker([r.lat, r.lon], { radius: 5, color: "#0d7a6b", weight: 2, fillOpacity: 0.7, dashArray: "2 3" }).addTo(clusterLayer)
    .bindPopup(`${esc(CAT[r.category] || r.category)}<br>${dt(r.date)}${r.demo ? '<br><span class="badge b-sample">SAMPLE</span>' : ""}`));
}
async function searchRoad() {
  const q = $("#rq").value.trim(), box = $("#rres");
  if (q.length < 2) { box.innerHTML = ""; return; }
  const r = await api("/api/public/roads/search?q=" + encodeURIComponent(q));
  box.innerHTML = r.length ? r.map(x => `<button class="roadpick" data-id="${x.id}">${esc(x.name)}${x.approx ? ' <span class="muted small">(approximate shape)</span>' : ""}</button>`).join("") : `<p class="muted small">No mapped road matches "${esc(q)}".</p>`;
  $$(".roadpick", box).forEach(b => b.onclick = () => { openRoad(+b.dataset.id); box.innerHTML = ""; });
}
async function openRoad(id) {
  const p = await api("/api/public/roads/" + id), box = $("#rprofile");
  if (hi) pubMap.removeLayer(hi);
  if (p.bbox) pubMap.fitBounds([[p.bbox[1], p.bbox[0]], [p.bbox[3], p.bbox[2]]], { maxZoom: 16 });
  const cats = Object.entries(p.reports.by_category).map(([k, v]) => `<span class="chip">${esc(CAT[k] || k)}: ${v}</span>`).join("");
  const works = p.government_records.map(w => `<div style="margin:8px 0;padding:8px;border:2px ${w.provenance === "inferred" ? "dashed var(--inferred)" : "solid var(--official)"};border-radius:6px">
     <div>${provBadge(w.provenance)}${sample(w.sample)}</div><div style="margin-top:4px"><b>${esc(w.description)}</b></div>
     <div class="small muted">${w.ward ? "Ward " + esc(w.ward) + ". " : ""}${w.contractor ? "Contractor: " + esc(w.contractor) + ". " : ""}${w.work_order_date ? "Work order " + dt(w.work_order_date) + ". " : ""}${esc(w.declared_status)}</div>
     <div class="small">${esc(w.provenance_text)}</div></div>`).join("");
  box.innerHTML = `<div class="card" style="margin-top:12px"><h2>${esc(p.name)}</h2>
   <p class="muted small">${p.length_m.toLocaleString()} m${p.wards.length ? " · ward " + p.wards.map(esc).join(", ") : ""}${p.geometry_approximate ? " · approximate shape" : ""}</p>
   <div class="row"><span class="badge b-citizen">${p.reports.total} citizen report(s)</span><span class="muted small">${p.reports.last_30_days} in the last 30 days</span></div>
   <div class="chips" style="margin:8px 0">${cats || '<span class="muted small">No reports yet.</span>'}</div>
   ${p.clusters.length ? `<p class="small">${p.clusters.length} group(s) of similar reports on this road.</p>` : ""}
   <h4>Government records</h4>${works || '<p class="muted small">None linked.</p>'}<p class="small muted">${esc(p.government_records_note)}</p>
   <button class="btn sm" id="rhere">Report a problem on this road</button></div>`;
  $("#rhere").onclick = () => { show("report"); $("#rn").value = p.name; };
}

/* ---------- report form ---------- */
async function initPick() {
  if (pickMap) return;
  const cats = await api("/api/public/categories");
  $("#cat").innerHTML = cats.map(c => `<option value="${esc(c.key)}">${esc(c.label)}</option>`).join("");
  pickMap = baseMap("pick");
  pickMap.on("click", e => setPin(e.latlng.lat, e.latlng.lng, null));
  api("/api/public/roads").then(g => L.geoJSON(g, { style: { color: "#5b6770", weight: 3, opacity: .55 }, interactive: false }).addTo(pickMap));
}
async function setPin(lat, lon, acc) {
  pos = { lat, lon, acc };
  pin ? pin.setLatLng([lat, lon]) : pin = L.marker([lat, lon], { draggable: true }).addTo(pickMap).on("dragend", e => { const p = e.target.getLatLng(); setPin(p.lat, p.lng, null); });
  pickMap.setView([lat, lon], Math.max(pickMap.getZoom(), 16));
  const box = $("#locbox"); box.textContent = "Finding the road…";
  try {
    const j = await api(`/api/public/locate?lat=${lat}&lon=${lon}`);
    box.className = "where";
    box.innerHTML = `<b>${j.road ? esc(j.road.name) : "No mapped road within 60 m"}</b><br><span class="small">${j.ward ? esc(j.ward) : "Ward not found"}${acc ? " · GPS accuracy about " + Math.round(acc) + " m" : ""}</span>` +
      (j.road ? `<br><span class="small muted">${j.road.gov_status === "unmatched" ? "No government work record is linked to this road yet." : "A government work record may be linked to this road."}</span>` : `<br><span class="small muted">You can still send the report. It will be kept and reviewed.</span>`);
  } catch (e) { box.className = "where"; box.textContent = e.message; }
}
$("#gpsBtn").onclick = () => {
  if (!navigator.geolocation) return toast("Location is not available in this browser. Tap the map instead.", true);
  navigator.geolocation.getCurrentPosition(p => setPin(p.coords.latitude, p.coords.longitude, p.coords.accuracy), () => toast("Could not get your location. Tap the map instead.", true), { enableHighAccuracy: true, timeout: 10000 });
};
$("#ph").onchange = e => {
  const files = [...e.target.files], prev = $("#prev"); prev.innerHTML = "";
  if (files.length > 3) { toast("Choose at most 3 photos", true); e.target.value = ""; return; }
  files.forEach(f => { const i = new Image(); i.src = URL.createObjectURL(f); i.alt = "Selected photo"; prev.append(i); });
};
$("#f").onsubmit = async e => {
  e.preventDefault();
  const err = $("#ferr"); err.hidden = true;
  if (!pos) { err.hidden = false; err.textContent = "Choose the location first: use your location or tap the map."; return; }
  const fd = new FormData();
  fd.append("category", $("#cat").value); fd.append("description", $("#desc").value); fd.append("road_name", $("#rn").value); fd.append("contact", $("#ct").value);
  fd.append("lat", pos.lat); fd.append("lon", pos.lon); if (pos.acc) fd.append("accuracy_m", pos.acc);
  [...$("#ph").files].forEach(f => fd.append("photos", f));
  const btn = $("#submitBtn"); btn.disabled = true; btn.textContent = "Sending…";
  try {
    const r = await fetch("/api/reports", { method: "POST", body: fd }), j = await r.json();
    if (!r.ok) throw new Error(typeof j.detail === "string" ? j.detail : "Check the form and try again");
    try { localStorage.setItem("is_last", JSON.stringify({ ref: j.ref, token: j.token })); } catch { }
    e.target.reset(); $("#prev").innerHTML = "";
    $("#done").innerHTML = `<div class="receipt"><h2>Report received</h2><p>Reference <span class="code">${esc(j.ref)}</span><br>Tracking code <span class="code">${esc(j.token)}</span></p>
      <p class="small"><b>Write both down now.</b> The tracking code is shown only once.</p>
      <p class="small">Location: ${j.location.road ? esc(j.location.road) : "no mapped road within 60 m (kept for review)"}${j.location.ward ? ", " + esc(j.location.ward) : ""}.</p>
      ${j.grouped_with_similar ? '<p class="small">It was grouped with similar reports nearby.</p>' : ""}${j.duplicate ? '<p class="small">This looks like a repeat of a report you just sent.</p>' : ""}</div>`;
    loadReports();
  } catch (x) { err.hidden = false; err.textContent = x.message; }
  btn.disabled = false; btn.textContent = "Send report";
};

/* ---------- tracking ---------- */
$("#tf").onsubmit = async e => {
  e.preventDefault();
  try {
    const j = await api(`/api/reports/track?ref=${encodeURIComponent($("#ref").value.trim())}&token=${encodeURIComponent($("#tok").value.trim())}`);
    $("#tr").innerHTML = `<div class="card"><h2>${esc(j.ref)}: ${esc(j.category_label)}</h2><p>Status: <b>${esc(STATUS[j.status] || j.status)}</b></p>
     <p class="muted small">${j.road ? esc(j.road) + ", " : ""}${esc(j.ward || "")}</p>
     <p>${j.cluster ? `Grouped with ${j.cluster.report_count - 1} other nearby report(s).` : "Not grouped with other reports so far."}</p>
     <p>${j.related_roadworks.length ? "Related government work (confirmed by an officer): " + j.related_roadworks.map(x => esc(x.description)).join("; ") : "No confirmed related government work."}</p>
     <ul class="steps">${j.history.map(h => `<li><b>${esc(STATUS[h.status] || h.status)}</b> · ${dtt(h.at)}${h.note ? "<br>" + esc(h.note) : ""}</li>`).join("")}</ul></div>`;
  } catch (x) { $("#tr").innerHTML = `<div class="notice bad">${esc(x.message)}</div>`; }
};
try { const s = JSON.parse(localStorage.getItem("is_last")); if (s) { $("#ref").value = s.ref; $("#tok").value = s.token; } } catch { }
show(["map", "report", "track", "about"].includes(location.hash.slice(1)) ? location.hash.slice(1) : "map");
