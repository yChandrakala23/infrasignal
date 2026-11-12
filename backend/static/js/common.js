/* Shared helpers: API calls, escaping, formatting, auth, small UI parts. */
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const inr = v => v == null || v === "" ? "–" : "₹" + Number(v).toLocaleString("en-IN", { maximumFractionDigits: 0 });
const crore = v => v == null ? "–" : v >= 1e7 ? "₹" + (v / 1e7).toFixed(2) + " cr" : v >= 1e5 ? "₹" + (v / 1e5).toFixed(1) + " lakh" : inr(v);
const dt = v => v ? new Date(v.endsWith && !/[zZ]|[+-]\d\d:?\d\d$/.test(v) ? v + "Z" : v).toLocaleDateString("en-IN", { day: "2-digit", month: "short", year: "numeric" }) : "–";
const dtt = v => v ? new Date(v.endsWith && !/[zZ]|[+-]\d\d:?\d\d$/.test(v) ? v + "Z" : v).toLocaleString("en-IN", { day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" }) : "–";
const pct = v => v == null ? "–" : Math.round(v * 100) + "%";
const label = s => String(s || "").replace(/_/g, " ").replace(/^./, c => c.toUpperCase());
const CAT = { pothole: "Pothole", damaged_road: "Damaged road surface", cracks: "Cracks", waterlogging: "Waterlogging / drainage", incomplete_work: "Incomplete or abandoned work", recurring: "Repeatedly repaired, failing again", footpath: "Damaged footpath", service_lane: "Service lane issue", other: "Other" };
const STATUS = { received: "Received", under_review: "Under review", forwarded: "Forwarded to department", work_started: "Work started", repair_reported: "Repair reported", closed: "Closed", rejected: "Rejected" };

const Auth = {
  key: "is_auth_" + location.pathname.replace(/\W/g, ""),
  get() { try { return JSON.parse(sessionStorage.getItem(this.key)); } catch { return null; } },
  set(v) { try { sessionStorage.setItem(this.key, JSON.stringify(v)); } catch { } },
  clear() { try { sessionStorage.removeItem(this.key); } catch { } },
};
async function api(path, opts = {}) {
  const a = Auth.get(); const headers = { ...(opts.headers || {}) };
  if (a) headers.Authorization = "Bearer " + a.token;
  let body = opts.body;
  if (opts.form) { body = new FormData(); for (const [k, v] of Object.entries(opts.form)) if (v !== undefined && v !== null) body.append(k, v); }
  const r = await fetch(path, { method: opts.method || (body ? "POST" : "GET"), headers, body });
  if (r.status === 401 && a) { Auth.clear(); location.reload(); throw new Error("Session expired"); }
  const ct = r.headers.get("content-type") || "";
  const data = ct.includes("json") ? await r.json() : await r.text();
  if (!r.ok) throw new Error((data && data.detail && (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail))) || "Request failed (" + r.status + ")");
  return data;
}
const qs = o => { const p = new URLSearchParams(); for (const [k, v] of Object.entries(o)) if (v !== "" && v != null && v !== false) p.append(k, v); const s = p.toString(); return s ? "?" + s : ""; };
function toast(msg, err) { const t = document.createElement("div"); t.className = "toast" + (err ? " err" : ""); t.setAttribute("role", "status"); t.textContent = msg; document.body.append(t); setTimeout(() => t.remove(), err ? 6000 : 3000); }
function confirmBox(title, text, okLabel = "Confirm") {
  return new Promise(res => {
    const b = document.createElement("div"); b.className = "modal-back";
    b.innerHTML = `<div class="modal" role="dialog" aria-modal="true"><h3>${esc(title)}</h3><p>${esc(text)}</p><div class="row" style="justify-content:flex-end;margin-top:14px"><button class="btn ghost" data-v="0">Cancel</button><button class="btn" data-v="1">${esc(okLabel)}</button></div></div>`;
    b.onclick = e => { const v = e.target.dataset.v; if (v !== undefined || e.target === b) { b.remove(); res(v === "1"); } };
    document.body.append(b); $("[data-v='1']", b).focus();
  });
}
const sample = s => s ? ' <span class="badge b-sample" title="Synthetic sample record, not a real government record or citizen report">SAMPLE</span>' : "";
const PROV = {
  verified_official: ["b-official", "Verified government record"], official_record: ["b-official", "Government record, matched by system"],
  inferred: ["b-inferred", "Inferred, not confirmed"], citizen: ["b-citizen", "Citizen-submitted"],
};
const provBadge = k => { const [c, t] = PROV[k] || ["b-neutral", k]; return `<span class="badge ${c}">${esc(t)}</span>`; };
const linkBadge = st => ({ verified: `<span class="badge b-official">Verified by officer</span>`, auto: `<span class="badge b-official">Auto: exact identifier</span>`, suggested: `<span class="badge b-inferred">Suggested, needs review</span>`, rejected: `<span class="badge b-bad">Rejected</span>`, unverified: `<span class="badge b-neutral">Reopened</span>` }[st] || `<span class="badge b-neutral">${esc(st)}</span>`);
const sevBadge = s => `<span class="badge b-${esc(s || "low")}">${esc(label(s))}</span>`;
function table(cols, rows, opts = {}) {
  if (!rows.length) return `<div class="empty">${opts.empty || "Nothing to show."}</div>`;
  return `<div class="tablewrap"><table><thead><tr>${cols.map(c => `<th class="${c.cls || ""}">${esc(c.h)}</th>`).join("")}</tr></thead><tbody>${rows.map((r, i) => `<tr class="${opts.click ? "click" : ""}" ${opts.click ? `data-i="${i}" tabindex="0"` : ""}>${cols.map(c => `<td class="${c.cls || ""}">${c.f(r)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
}
function onRows(root, rows, fn) { $$("tr.click", root).forEach(tr => { const go = () => fn(rows[+tr.dataset.i]); tr.onclick = go; tr.onkeydown = e => { if (e.key === "Enter") go(); }; }); }
function drawer(html) { closeDrawer(); const d = document.createElement("aside"); d.className = "drawer"; d.id = "drawer"; d.setAttribute("aria-label", "Details"); d.innerHTML = `<button class="btn ghost sm close" aria-label="Close details">Close</button>` + html; document.body.append(d); $(".close", d).onclick = closeDrawer; return d; }
function closeDrawer() { const d = $("#drawer"); if (d) d.remove(); }
document.addEventListener("keydown", e => { if (e.key === "Escape") closeDrawer(); });

/* login gate: shows a form until a token with an allowed role is stored */
function loginGate(roles, title, onReady) {
  const a = Auth.get();
  if (a && roles.includes(a.role)) { onReady(a); return; }
  document.body.innerHTML = `<header class="topbar"><a class="brand" href="/citizen"><i></i>InfraSignal</a><span class="area">${esc(title)}</span></header>
  <main class="card login"><h1>${esc(title)}</h1><p class="muted">Sign in with the account issued to you. Citizens do not need an account: use the <a href="/citizen">public portal</a>.</p>
  <form id="lf"><label for="em">Email</label><input id="em" type="email" autocomplete="username" required><label for="pw">Password</label><input id="pw" type="password" autocomplete="current-password" required>
  <p id="lerr" class="notice bad" hidden></p><button class="btn block" style="margin-top:14px">Sign in</button></form></main>`;
  $("#lf").onsubmit = async e => {
    e.preventDefault();
    try {
      const j = await api("/api/auth/login", { form: { email: $("#em").value, password: $("#pw").value } });
      if (!roles.includes(j.role)) throw new Error("This account cannot open this area (role: " + j.role + ").");
      Auth.set(j); location.reload();
    } catch (err) { const p = $("#lerr"); p.hidden = false; p.textContent = err.message; }
  };
}
/* maps */
function baseMap(el, opts = {}) {
  const map = L.map(el, { zoomControl: true, ...opts }).setView([12.9716, 77.5946], 12);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", { attribution: "&copy; OpenStreetMap contributors", maxZoom: 19 }).addTo(map);
  return map;
}
const CAT_COLOR = { pothole: "#b3261e", damaged_road: "#c4581a", cracks: "#8a6d00", waterlogging: "#1b6fa8", incomplete_work: "#6b3fa0", recurring: "#a1306b", footpath: "#2d7a3e", service_lane: "#4a545c", other: "#4a545c" };
const SEV_COLOR = { high: "#b3261e", medium: "#c4781a", low: "#5b6770" };
function chartDefaults() { if (window.Chart) { Chart.defaults.font.family = getComputedStyle(document.body).fontFamily; Chart.defaults.color = getComputedStyle(document.body).color; Chart.defaults.maintainAspectRatio = false; } }
const charts = {};
function chart(id, cfg) { const el = document.getElementById(id); if (!el || !window.Chart) return; if (charts[id]) charts[id].destroy(); chartDefaults(); if (cfg && cfg.options && cfg.options.indexAxis === 'y') {  // horizontal bars: show every label
    cfg.options.scales = cfg.options.scales || {}; const y = cfg.options.scales.y = cfg.options.scales.y || {};
    y.ticks = Object.assign({ autoSkip: false }, y.ticks || {});
  }
  charts[id] = new Chart(el, cfg); }
