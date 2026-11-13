"use strict";
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const UI = Object.assign({ win: "24h", prov: "all", metric: "cost" }, JSON.parse(localStorage.getItem("fc-ui") || "{}"));
const openHosts = new Set(JSON.parse(localStorage.getItem("fc-open") || "[]"));
let STATE = null, RAW = null, STATIC = false, showNotes = false;
UI.private = new URLSearchParams(location.search).has("private"); // ?private=1 hides names; it is not saved

const PALETTE = ["#6aa8ff", "#4dd4c6", "#b48cff", "#e5a54b", "#f07fa8", "#a3d65c", "#d9895c", "#818cf8", "#4cc38a", "#f2c94c", "#5ec8f2", "#e57fd8"];
let RANK = {};
let FIXED = { system: "#5c6068" };
const OTHER = "#454950";
// Projects get palette colors in order of their token use this week, so the top projects never share a color.
function color(name) {
  if (FIXED[name]) return FIXED[name];
  if (RANK[name] != null) return PALETTE[RANK[name] % PALETTE.length];
  let h = 0; for (const c of name) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return PALETTE[h % PALETTE.length];
}
const short = (l) => String(l || "").split("@")[0];
const ACOL = ["#6aa8ff", "#d9895c", "#b48cff", "#4dd4c6", "#f07fa8"];
const fmtN = (n) => n >= 1e9 ? (n / 1e9).toFixed(n >= 1e10 ? 0 : 1) + "B" : n >= 1e6 ? (n / 1e6).toFixed(n >= 1e7 ? 0 : 1) + "M" : n >= 1e3 ? (n / 1e3).toFixed(0) + "k" : String(Math.round(n));
const fmt$ = (n) => n >= 1000 ? "$" + (n / 1000).toFixed(n >= 10000 ? 0 : 1) + "k" : n >= 10 ? "$" + Math.round(n) : "$" + n.toFixed(n >= 1 ? 1 : 2);
const fmtM = (v) => UI.metric === "cost" ? fmt$(v) : fmtN(v);
const fmtB = (b) => b >= 1 << 30 ? (b / (1 << 30)).toFixed(b >= 100 * (1 << 30) ? 0 : 1) + " GB" : Math.round(b / (1 << 20)) + " MB";
function rel(sec) {
  const a = Math.abs(sec); let s;
  if (a < 90) s = Math.round(a) + "s"; else if (a < 3600) s = Math.round(a / 60) + "m";
  else if (a < 86400) s = Math.floor(a / 3600) + "h " + Math.round((a % 3600) / 60) + "m";
  else s = Math.floor(a / 86400) + "d " + Math.round((a % 86400) / 3600) + "h";
  return sec < 0 ? s + " ago" : "in " + s;
}
function when(ts) {
  if (!ts) return "?";
  const d = new Date(ts * 1000), now = new Date();
  const day = d.toDateString() === now.toDateString() ? "today" : d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
  return day + " " + d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}
const dateOnly = (ts) => new Date(ts * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" });
const whenShort = (ts) => { const d = new Date(ts * 1000); return d.toLocaleDateString(undefined, { weekday: "short" }) + " " + d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" }); };
const barColor = (p, warn, crit) => p >= crit ? "var(--crit)" : p >= warn ? "var(--warn)" : "var(--barok)";
const tip = () => $("#tip");
const IC = (n, cls) => '<i class="ic ic-' + n + (cls ? " " + cls : "") + '"></i>';
// Provider look (name, color, icon) comes from the server, so new providers need no UI change.
const PROV = () => (STATE && STATE.meta.providers) || {};
const pname = (p) => (PROV()[p] || {}).name || p;
const PIC = (p) => { const m = PROV()[p]; if (!m) return ""; return m.icon ? '<i class="ic ic-' + m.icon + '" style="color:' + m.color + '"></i>' : '<span class="pdot" style="background:' + m.color + '"></span>'; };
const OSIC = (h) => IC(h.os === "windows" ? "windows" : h.os === "macos" ? "apple" : "linux", "os");
function placeTip(e) {
  const t = tip(), w = t.offsetWidth, h = t.offsetHeight;
  let x = e.clientX + 14, y = e.clientY + 16;
  if (x + w > innerWidth - 8) x = e.clientX - w - 14;
  if (y + h > innerHeight - 8) y = e.clientY - h - 12;
  t.style.transform = "translate(" + Math.max(8, x) + "px," + Math.max(8, y) + "px)";
}
function showTip(e, html) { const t = tip(); t.innerHTML = html; t.style.display = "block"; placeTip(e); }
// One custom tooltip for every [data-tip] element: no delay, follows the cursor, keeps line breaks.
let tipEl = null;
document.addEventListener("mouseover", (e) => {
  const el = e.target.closest("[data-tip]");
  if (el && el.dataset.tip) { tipEl = el; showTip(e, '<div class="tt">' + esc(el.dataset.tip) + "</div>"); }
});
document.addEventListener("mousemove", (e) => { if (tipEl && tipEl.contains(e.target)) placeTip(e); });
document.addEventListener("mouseout", (e) => { if (tipEl && !tipEl.contains(e.relatedTarget)) { tipEl = null; hideTip(); } });
addEventListener("scroll", () => { tipEl = null; hideTip(); }, { passive: true });
function hideTip() { tip().style.display = "none"; }

// ---------------------------------------------------------------- header: status, problems in a drop-down
function renderTop(s) {
  const main = s.issues.filter((i) => i.level !== "info"), info = s.issues.filter((i) => i.level === "info");
  const nc = main.filter((i) => i.level === "crit").length, nw = main.length - nc;
  $("#lvl").className = "dot " + s.level;
  $("#status").textContent = main.length ? [nc && nc + (nc > 1 ? " problems" : " problem"), nw && nw + (nw > 1 ? " warnings" : " warning")].filter(Boolean).join(", ") : "All healthy";
  const row = (i) => '<div class="iss"><span class="dot ' + i.level + '"></span><div>' + esc(i.text) +
    (i.who ? '<span class="mut"> · ' + esc(i.who) + "</span>" : "") + (i.fix ? ' <code class="fix" data-tip="Click to copy">' + esc(i.fix) + "</code>" : "") + "</div></div>";
  $("#issues").innerHTML = (main.map(row).join("") || '<div class="mut">No problems.</div>') + (info.length ? '<div class="mut small" style="margin:8px 0 2px">Notes</div>' + info.map(row).join("") : "");
}
$("#status").onclick = (e) => { e.stopPropagation(); $("#issues").classList.toggle("open"); };
document.addEventListener("click", (e) => { if (!e.target.closest("#issues")) $("#issues").classList.remove("open"); });

document.addEventListener("click", (e) => {
  const c = e.target.closest("code.fix"); if (!c) return;
  navigator.clipboard && navigator.clipboard.writeText(c.textContent);
  const old = c.textContent; c.textContent = "copied"; setTimeout(() => (c.textContent = old), 900);
});

// ---------------------------------------------------------------- accounts: one pooled chart per provider
function renderPools(s) {
  const provs = Object.keys(s.meta.providers).filter((p) => s.accounts.some((a) => a.provider === p));
  const box = $("#pools");
  if (!provs.length) { box.innerHTML = '<div class="card mut">No accounts yet. Add an account source in config.toml (see docs/plugins.md).</div>'; return; }
  if (box.dataset.provs !== provs.join()) {
    box.dataset.provs = provs.join();
    box.innerHTML = provs.map((p) => '<div class="card pv" style="--pc:' + s.meta.providers[p].color + '"><h3>' + PIC(p) + esc(pname(p)) + ' <span id="ah-' + p + '"></span></h3><div class="achart" id="ac-' + p + '"></div><div id="al-' + p + '"></div></div>').join("");
  }
  provs.forEach((p) => renderPool(s, p));
}
function renderPool(s, prov) {
  const P = (s.pools || {})[prov], el = $("#ac-" + prov), now = s.now;
  const accts = s.accounts.filter((a) => a.provider === prov).sort((x, y) => (y.weekly_reset || 0) - (x.weekly_reset || 0));
  // Colors follow the account name, so they do not move when the reset order changes.
  accts.slice().sort((x, y) => (x.label || x.id).localeCompare(y.label || y.id)).forEach((a, i) => (a._c = ACOL[i % ACOL.length]));
  const head = $("#ah-" + prov);
  if (!P) { el.innerHTML = '<div class="mut">No quota data</div>'; head.textContent = ""; }
  else {
    const hr = (ts) => { const d = new Date(ts * 1000); return d.toLocaleDateString(undefined, { weekday: "short" }) + " " + d.toLocaleTimeString(undefined, { hour: "numeric" }).replace(":00", ""); };
    const dryAll = P.dry.map((d) => hr(d[0]) + (d[1] ? " to " + hr(d[1]) : " on")).join(", ");
    head.innerHTML = "<b>" + Math.round(P.left_now) + "%</b> left" + (P.dry.length ? ' · <span class="c-warn" data-tip="Empty: ' + esc(dryAll) + '">empty ' + hr(P.dry[0][0]) + "</span>" : "");
    const T = P.times, n = T.length, t0 = now - 7 * 86400, t1 = now + 7 * 86400, ni = P.now_idx;
    const W = 1400, H = 300, L = 40, R = 8, TP = 10, B = 26;
    const X = (t) => L + ((Math.max(t0, Math.min(t1, t)) - t0) / (t1 - t0)) * (W - L - R);
    const Y = (v) => TP + (1 - v / 100) * (H - TP - B);
    const ids = accts.map((a) => a.id).filter((e) => P.bands[e]);
    let g = '<rect x="' + X(now) + '" y="' + TP + '" width="' + (X(t1) - X(now)) + '" height="' + (H - TP - B) + '" fill="rgba(255,255,255,.025)"/>';
    for (const v of [0, 50, 100]) g += '<line x1="' + L + '" x2="' + (W - R) + '" y1="' + Y(v) + '" y2="' + Y(v) + '" stroke="#26282d"/><text x="' + (L - 6) + '" y="' + (Y(v) + 4) + '" fill="#5c6068" font-size="12" text-anchor="end">' + v + "%</text>";
    for (let d = new Date(new Date(t0 * 1000).setHours(24, 0, 0, 0)); d.getTime() / 1000 < t1; d = new Date(d.getFullYear(), d.getMonth(), d.getDate() + 1)) {
      const x = X(d.getTime() / 1000);
      g += '<line x1="' + x + '" x2="' + x + '" y1="' + (H - B) + '" y2="' + (H - B + 3) + '" stroke="#3a3d44"/><text x="' + (x + 2) + '" y="' + (H - 6) + '" fill="#5c6068" font-size="12">' + d.toLocaleDateString(undefined, { weekday: "narrow" }) + d.getDate() + "</text>";
    }
    for (const d of P.dry) g += '<rect x="' + X(d[0]) + '" y="' + (H - B - 3) + '" width="' + Math.max(1, X(d[1] || t1) - X(d[0])) + '" height="3" fill="var(--warn)" opacity=".8"/>';
    // stacked bands; break where any account has no data
    const ok = T.map((_, k) => ids.every((e) => P.bands[e][k] != null));
    const ranges = [];
    for (let k = 0; k < n; k++) if (ok[k]) { if (ranges.length && ranges[ranges.length - 1][1] === k - 1) ranges[ranges.length - 1][1] = k; else ranges.push([k, k]); }
    const parts = [];
    for (const [a, b] of ranges) { if (a < ni && b > ni) { parts.push([a, ni, true]); parts.push([ni, b, false]); } else parts.push([a, b, b <= ni]); }
    for (const [a, b, past] of parts) {
      if (b <= a) continue;
      const base = new Array(b - a + 1).fill(0);
      for (const e of ids) {
        const c = accts.find((x) => x.id === e)._c;
        let up = "", lo = "";
        for (let k = a; k <= b; k++) { const v = P.bands[e][k]; up += (k === a ? "M" : "L") + X(T[k]).toFixed(1) + " " + Y(base[k - a] + v).toFixed(1); }
        for (let k = b; k >= a; k--) lo += "L" + X(T[k]).toFixed(1) + " " + Y(base[k - a]).toFixed(1);
        g += '<path d="' + up + lo + 'Z" fill="' + c + '" opacity="' + (past ? 0.55 : 0.22) + '"/>';
        for (let k = a; k <= b; k++) base[k - a] += P.bands[e][k];
      }
      const top = base.map((v, i) => (i ? "L" : "M") + X(T[a + i]).toFixed(1) + " " + Y(v).toFixed(1)).join("");
      g += '<path d="' + top + '" fill="none" stroke="#e4e5e7" stroke-width="1.4"' + (past ? "" : ' stroke-dasharray="4 4"') + "/>";
    }
    for (const r of P.resets) { const a = accts.find((x) => x.id === r[1]); if (a) g += '<circle cx="' + X(r[0]) + '" cy="' + (TP + 3) + '" r="4" fill="' + a._c + '" data-tip="' + esc(a.label) + " resets " + whenShort(r[0]) + ", adds " + r[2] + '% of the pool"></circle>'; }
    for (const a of accts) for (const r of a.resets || []) if (r.weekly && r.expires && r.expires < t1) {
      const x = X(r.expires), y = H - B - 9;
      g += '<path d="M' + x + " " + (y - 4) + " L" + (x + 4) + " " + y + " L" + x + " " + (y + 4) + " L" + (x - 4) + " " + y + ' Z" fill="none" stroke="' + a._c + '" stroke-width="1.4" data-tip="Free reset of ' + esc(a.label) + " expires " + whenShort(r.expires) + '"></path>';
    }
    g += '<line x1="' + X(now) + '" x2="' + X(now) + '" y1="' + TP + '" y2="' + (H - B) + '" stroke="#6b7079"/><text x="' + (X(now) + 4) + '" y="' + (TP + 18) + '" fill="#8b8f97" font-size="12">now</text>';
    g += '<line class="hv" y1="' + TP + '" y2="' + (H - B) + '" stroke="#8b8f97" stroke-width="0.8" visibility="hidden"/>';
    el.innerHTML = '<svg viewBox="0 0 ' + W + " " + H + '">' + g + "</svg>";
    const svg = el.querySelector("svg"), hv = el.querySelector(".hv");
    svg.onmousemove = (e) => {
      if (e.target.closest("[data-tip]")) return;
      const r = svg.getBoundingClientRect(), x = ((e.clientX - r.left) / r.width) * W;
      if (x < L) return hideTip();
      const t = t0 + ((x - L) / (W - L - R)) * (t1 - t0);
      let k = 0; for (let j = 0; j < n; j++) if (Math.abs(T[j] - t) < Math.abs(T[k] - t)) k = j;
      hv.setAttribute("x1", x); hv.setAttribute("x2", x); hv.setAttribute("visibility", "visible");
      if (!ok[k]) return showTip(e, '<div class="mut">' + when(t) + "</div>No data yet");
      const tot = ids.reduce((a, em) => a + P.bands[em][k], 0);
      const rows = ids.slice().reverse().map((em) => { const a = accts.find((x) => x.id === em), own = (100 * P.bands[em][k]) / ((100 * P.weights[em]) / P.units);
        return '<div class="r"><span><span class="sw" style="background:' + a._c + '"></span>' + esc(short(a.label)) + '</span><span class="mut">' + Math.round(own) + "% of its limit</span></div>"; }).join("");
      showTip(e, '<div class="mut">' + when(T[k]) + (T[k] > now ? " · projected" : "") + '</div><div class="r"><span>Pool left</span><b>' + Math.round(tot) + "%</b></div>" + rows);
    };
    svg.onmouseleave = () => { hv.setAttribute("visibility", "hidden"); hideTip(); };
  }
  // one line per account
  const leg = accts.slice().reverse().map((a) => {
    const left = a.weekly_used == null ? null : Math.max(0, 100 - a.weekly_used);
    const warn = a.status === "signed_out" ? ["c-crit", "signed out"] : a.runs_out_before_reset && a.eta ? [a.eta - s.now < 86400 ? "c-crit" : "c-warn", "runs out " + whenShort(a.eta)] : null;
    return '<span class="li" data-tip="' + esc(a.label + " · " + (a.plan || "") + (a.weekly_reset ? "\nResets " + when(a.weekly_reset) : "") + (a.short_used != null ? "\nShort window " + Math.round(a.short_used) + "% used" : "")) + '"><span class="sw" style="background:' + a._c + '"></span>' + esc(short(a.label)) + (a.active ? ' <span class="mut small" data-tip="The router sends new requests to this account">active</span>' : "") + " <b>" + (left == null ? "?" : Math.round(left) + "%") + "</b>" + (warn ? ' <span class="' + warn[0] + '">' + esc(warn[1]) + "</span>" : "") + "</span>";
  }).join("");
  let h = '<table class="alist"><tr><th>Account</th><th class=n>Left</th><th>Weekly reset</th><th class=n data-tip="Short window, for example 5 hours">Short</th><th data-tip="Count, then the first expiry date">Resets</th><th></th></tr>';
  for (const a of accts.slice().reverse()) {
    const left = a.weekly_used == null ? null : Math.max(0, 100 - a.weekly_used);
    let note = "", cls = "";
    if (a.status === "signed_out") { note = "signed out, log in again"; cls = "c-crit"; }
    else if (a.weekly_used == null) { note = "no quota data"; cls = "c-warn"; }
    else if (a.weekly_used >= 100) { note = "empty until reset"; cls = "mut"; }
    else if (a.runs_out_before_reset && a.eta) { note = "runs out " + whenShort(a.eta); cls = a.eta - s.now < 86400 ? "c-crit" : "c-warn"; }
    const fr = (a.resets || []).filter((r) => r.weekly), exps = fr.map((r) => r.expires).filter(Boolean).sort((x, y) => x - y);
    const frs = a.resets == null ? '<span class="dim">?</span>' : fr.length ? '<span data-tip="Expire: ' + esc(exps.map(dateOnly).join(", ")) + '">' + fr.length + (exps.length ? ' <span class="mut">· ' + dateOnly(exps[0]) + "</span>" : "") + "</span>" : '<span class="dim">0</span>';
    const share = P && P.weights[a.id] ? Math.round((100 * P.weights[a.id]) / P.units) + "% of pool" : "";
    h += '<tr><td><span class="sw" style="background:' + a._c + '"></span>' + esc(a.label) + '<span class="tag" data-tip="' + esc(share) + '">' + esc((a.plan || "").replace("_", " ")) + "</span>" + (a.active ? '<span class="tag">active</span>' : "") +
      "</td><td class=n>" + (left == null ? "" : "<b>" + Math.round(left) + "%</b>") + "</td><td>" + (a.weekly_reset ? whenShort(a.weekly_reset) : "") +
      "</td><td class=n>" + (a.short_used != null ? Math.round(a.short_used) + "%" : "") + "</td><td>" + frs + '</td><td class="' + cls + '">' + esc(note) + "</td></tr>";
  }
  $("#al-" + prov).innerHTML = '<div class="legend2">' + leg + '</div><details' + (openHosts.has("__acct") ? " open" : "") + ' class="acctd"><summary>Account details</summary>' + h + "</table></details>";
}


// ---------------------------------------------------------------- machines
// Jobs started with "job run" (jobs/job.py): memory-capped, queued for memory, with an owner thread.
const JOB_BAD = { killed: "c-warn", lost: "c-warn", failed: "c-warn" };
function jobPill(h) {
  const js = h.jobs || [], run = js.filter((j) => j.state === "run").length, q = js.filter((j) => j.state === "queued").length;
  const bad = js.filter((j) => JOB_BAD[j.state]).length;
  if (!run && !q && !bad) return "";
  const t = [run && run + " running", q && q + " waiting for memory", bad && bad + " killed or failed (6 h)"].filter(Boolean).join(", ");
  return '<span class="mut' + (bad ? " c-warn" : "") + '" data-tip="Jobs started with job run: ' + t + '">' + (run + q) + (run + q === 1 ? " job" : " jobs") + (q ? " · " + q + " queued" : "") + "</span>";
}
function jobTable(h) {
  const js = (h.jobs || []).slice().reverse().slice(0, 8);
  if (!js.length) return "";
  return "<table><tr><th>Job</th><th>State</th><th class=n>Mem / cap</th><th>Owner</th></tr>" + js.map((j) => {
    const mem = j.state === "run" ? j.mem_now : j.peak_bytes;
    const tip = j.cmd + (j.why ? "\n" + j.why : "") + "\njob logs " + j.id;
    return '<tr><td class="cmd" data-tip="' + esc(tip) + '">' + esc(j.project + " · " + j.name) + '</td><td class="' + (JOB_BAD[j.state] || "") + '">' + esc(j.state) +
      '</td><td class=n>' + fmtB(mem || 0) + " / " + j.mem_gb + 'G</td><td class="mut" data-tip="' + esc(j.owner || "no owner given") + '">' + esc((j.owner || "").slice(0, 8)) + "</td></tr>";
  }).join("") + "</table>";
}

function meter(l, pct, val, col, title) {
  return '<div class="meter"' + (title ? ' data-tip="' + esc(title) + '"' : "") + '><span class="l">' + l + '</span><div class="bar"><i style="width:' + Math.min(100, Math.max(0, pct)) + "%;background:" + col + '"></i></div><span class="v">' + val + "</span></div>";
}
// rich tooltips for machine meters
const HOSTMAP = {};
function mrow(k, v, cls) { return '<div class="r"><span class="mut">' + k + '</span><span' + (cls ? ' class="' + cls + '"' : "") + ">" + v + "</span></div>"; }
function mbar(pct, col) { return '<span class="tb"><i style="width:' + Math.min(100, Math.max(0, pct)) + "%;background:" + col + '"></i></span>'; }
function meterTip(h, kind) {
  let x = '<div class="tth">' + esc(h.name) + " · " + kind + "</div>";
  if (kind === "CPU") {
    x += mrow("Busy (1 min avg)", Math.round(h.cpu || 0) + "%") + mrow("Load", h.load[0].toFixed(1) + " / " + h.cores + " cores");
    if (h.iowait != null) x += mrow("Waits for disk", Math.round(h.iowait) + "%", h.iowait >= 30 ? "c-warn" : "");
    const ps = (h.projects || []).filter((p) => p.cpu >= 10).slice(0, 4);
    if (ps.length) x += '<div class="tts">Top projects</div>' + ps.map((p) => mrow('<span class="sq" style="background:' + color(p.project) + '"></span>' + esc(p.project), (p.cpu / 100).toFixed(1) + " cores")).join("");
  } else if (kind === "Memory") {
    x += mrow("Used", fmtB(h.mem_total - h.mem_avail) + " / " + fmtB(h.mem_total)) + mrow("Free", fmtB(h.mem_avail));
    const grp = {};
    for (const p of h.mem_top || []) { const k = p.project === "system" ? p.comm : p.project + " · " + p.comm; const g = (grp[k] = grp[k] || { n: 0, b: 0 }); g.n++; g.b += p.rss; }
    const ps = Object.entries(grp).sort((a, b) => b[1].b - a[1].b).slice(0, 4);
    if (ps.length) x += '<div class="tts">Biggest processes</div>' + ps.map(([k, g]) => mrow(esc(k) + (g.n > 1 ? ' <span class="dim">×' + g.n + "</span>" : ""), fmtB(g.b))).join("");
  } else if (kind === "Swap") {
    x += mrow("Used", fmtB(h.swap_used) + " / " + fmtB(h.swap_total)) + mrow("Reads from swap", (h.swap_in_mbps || 0).toFixed(1) + " MB/s");
    if (h.psi && h.psi.some != null) x += mrow("Tasks wait for memory", Math.round(h.psi.some) + "% of the time");
    x += mrow("Thrashing", h.thrash === "ok" ? "no" : h.thrash === "crit" ? "yes, bad" : "yes", h.thrash === "ok" ? "c-ok" : "c-" + h.thrash);
    const ps = (h.mem_top || []).filter((p) => p.swap > 50 * 1048576).sort((a, b) => b.swap - a.swap).slice(0, 3);
    if (ps.length) x += '<div class="tts">Most in swap</div>' + ps.map((p) => mrow(esc(p.project + " · " + p.comm), fmtB(p.swap))).join("");
    if (h.thrash === "ok" && h.swap_pct >= 90) x += '<div class="tts">Full swap with no reads is fine.</div>';
  } else if (kind === "Disk") {
    x += '<div class="dg">' + h.disks.slice().sort((a, b) => b.pct - a.pct).map((d) => '<span class="mut">' + esc(d.mount) + "</span>" + mbar(d.pct, barColor(d.pct, 90, 95)) + "<span>" + Math.round(d.pct) + '%</span><span class="mut">' + fmtB(d.total - d.used) + " free</span>").join("") + "</div>";
  }
  return x;
}
document.addEventListener("mouseover", (e) => {
  const el = e.target.closest("[data-mtip]"); if (!el) return;
  const [name, kind] = el.dataset.mtip.split("|"), h = HOSTMAP[name]; if (!h) return;
  tipEl = el; showTip(e, meterTip(h, kind));
});

function renderHosts(s) {
  const live = s.hosts.filter((h) => h.cores), dead = s.hosts.filter((h) => !h.cores);
  const card = (h) => {
    const stale = !h.reachable, disk = h.disks.slice().sort((a, b) => b.pct - a.pct)[0];
    HOSTMAP[h.name] = h;
    const big = (l, pct, col, tip) => '<div class="m" data-mtip="' + esc(h.name + "|" + l) + '"><div class="mh"><span>' + l + "</span><b" + (col !== "var(--barok)" ? ' style="color:' + col + '"' : "") + ">" + Math.round(pct) + '%</b></div><div class="bar"><i style="width:' + Math.min(100, Math.max(0, pct)) + "%;background:" + col + '"></i></div></div>';
    const none = (l, why) => '<div class="m"><div class="mh"><span>' + l + '</span><span class="mut">' + why + '</span></div><div class="bar"></div></div>';
    const thc = h.thrash === "crit" ? "var(--crit)" : h.thrash === "warn" ? "var(--warn)" : "var(--barok)";
    let x = '<div class="host ' + h.status + (stale ? " stale" : "") + '"><div class="hh">' + OSIC(h) + '<b>' + esc(h.name) + '</b><span class="dot ' + h.status + '"></span>'  + (h.label !== h.name ? '<span class="dim small">' + esc(h.label) + "</span>" : "") + (stale ? '<span class="mut c-warn" data-tip="' + esc(h.error || "No answer from this machine") + '">no data for ' + rel(-h.age).replace(" ago", "") + "</span>" : "") + (h.active_sessions ? '<span class="mut" data-tip="Agent sessions active in the last 15 min">' + h.active_sessions + (h.active_sessions > 1 ? " agents" : " agent") + "</span>" : "") + jobPill(h) + '</div><div class="ms">';
    x += big("CPU", h.cpu, barColor(h.cpu, 80, 95), "Load " + h.load[0].toFixed(1) + " on " + h.cores + " cores" + (h.hog && h.hog.cores >= 0.3 ? "\nTop: " + h.hog.project + (h.hog.sub ? " / " + h.hog.sub : "") + ", " + h.hog.cores + " cores" : ""));
    x += big("Memory", h.mem_pct, barColor(h.mem_pct, 88, 95), fmtB(h.mem_total - h.mem_avail) + " of " + fmtB(h.mem_total) + " used");
    x += h.swap_total ? big("Swap", h.swap_pct, thc, fmtB(h.swap_used) + " of " + fmtB(h.swap_total) + " used" + (h.thrash !== "ok" ? "\nThrashing: " + h.thrash_why : "\nNo thrashing (full swap alone is fine)")) : none("Swap", "no swap");
    x += disk ? big("Disk", disk.pct, barColor(disk.pct, 90, 95), h.disks.map((d) => d.mount + ": " + Math.round(d.pct) + "%, " + fmtB(d.total - d.used) + " free").join("\n")) : none("Disk", "no data");
    x += "</div>";
    // details: everything else
    let d = '<div class="kv">' + h.cores + " cores · " + fmtB(h.mem_total) + " RAM" + (h.gpus || []).map((g) => " · " + esc(g.name) + " " + Math.round(g.util) + "%").join("") + "</div>";
    if (h.hog && h.hog.cores >= 0.3) d += '<div class="kv">Top: ' + esc(h.hog.project) + (h.hog.sub ? " / " + esc(h.hog.sub) : "") + " · " + h.hog.cores + " cores</div>";
    const reasons = (h.reasons || []).filter((r) => !r.startsWith("swap thrashing"));
    if (h.status !== "ok" && reasons.length) d += '<div class="kv c-' + h.status + '">' + esc(reasons.join("; ")) + "</div>";
    if (h.thrash && h.thrash !== "ok" && h.mem_top[0]) { const p = h.mem_top[0]; d += '<div class="kv c-' + h.thrash + '">Swap thrashing: ' + esc(h.thrash_why) + '</div><div class="kv">Biggest: ' + esc(p.project) + " pid " + p.pid + " · " + fmtB(p.rss + (p.swap || 0)) + ' <code class="fix" data-tip="Click to copy">' + esc(h.kill_prefix + "kill " + p.pid) + "</code></div>"; }
    d += jobTable(h);
    d += "<table><tr><th class=n>CPU</th><th class=n>Mem</th><th>Project</th><th>Command</th></tr>" + h.procs.slice(0, 8).map((p) => "<tr><td class=n>" + Math.round(p.cpu) + "%</td><td class=n>" + fmtB(p.rss) + "</td><td>" + esc(p.project) + '</td><td class="cmd" data-tip="' + esc(p.cmd + "\n" + p.cwd) + '">' + esc(p.cmd) + "</td></tr>").join("") + "</table>";
    if (h.mem_top && h.mem_top.length) d += "<table><tr><th class=n>RAM</th><th class=n>Swap</th><th>Project</th><th>Process</th></tr>" + h.mem_top.slice(0, 5).map((p) => "<tr><td class=n>" + fmtB(p.rss) + "</td><td class=n>" + (p.swap ? fmtB(p.swap) : "") + "</td><td>" + esc(p.project) + '</td><td class="cmd" data-tip="' + esc(p.cmd) + '">' + p.pid + " " + esc(p.comm) + "</td></tr>").join("") + "</table>";
    x += '<details data-host="' + esc(h.name) + '"' + (openHosts.has(h.name) ? " open" : "") + "><summary>Details</summary>" + d + "</details>";
    return x + "</div>";
  };
  const main = live.filter((h) => !h.hidden), minor = live.filter((h) => h.hidden);
  const bad = minor.filter((h) => h.status !== "ok").length + dead.length;
  $("#hosts").innerHTML = main.map(card).join("");
  $("#more-hosts").innerHTML = '<details id="mh"' + (openHosts.has("__more") ? " open" : "") + '><summary>' + (minor.length + dead.length) + " more machines" + (bad ? ' <span class="dot warn"></span>' : "") + '</summary><div class="hosts">' + minor.map(card).join("") + "</div>" +
    (dead.length ? '<div class="mut small">' + dead.map((h) => esc(h.name) + " " + (h.status === "online" ? "online" : h.status === "down" ? "no data" : "offline")).join(" · ") + "</div>" : "") + "</details>";
  document.querySelectorAll("details[data-host], #mh").forEach((el) => el.addEventListener("toggle", () => {
    const k = el.id === "mh" ? "__more" : el.dataset.host;
    el.open ? openHosts.add(k) : openHosts.delete(k);
    localStorage.setItem("fc-open", JSON.stringify([...openHosts]));
  }));
}

function renderMatrix(s) {
  const hosts = s.hosts.filter((h) => h.cores), rows = {};
  for (const h of hosts) for (const p of h.projects) if (p.project !== "system") (rows[p.project] = rows[p.project] || {})[h.name] = p.cpu / 100;
  const list = Object.entries(rows).map(([p, v]) => [p, v, Object.values(v).reduce((a, b) => a + b, 0)]).filter((r) => r[2] >= 0.2).sort((a, b) => b[2] - a[2]).slice(0, 12);
  if (!list.length) { $("#matrix").innerHTML = '<div class="mut">No project uses CPU now.</div>'; return; }
  const used = hosts.filter((x) => list.some((r) => (r[1][x.name] || 0) >= 0.05));
  let h = '<div class="card"><table class="mx"><tr><th>Project</th>' + used.map((x) => "<th>" + esc(x.name) + "</th>").join("") + "<th>Total</th></tr>";
  for (const [p, v, t] of list) {
    h += '<tr><td><span class="sq" style="background:' + color(p) + '"></span>' + esc(p) + "</td>";
    for (const x of used) {
      const c = v[x.name] || 0, share = c / x.cores;
      h += "<td>" + (c >= 0.05 ? '<span class="heat" style="background:rgba(' + (share > 0.5 ? "229,72,77" : share > 0.25 ? "229,165,75" : "106,168,255") + "," + Math.min(0.45, 0.06 + share * 0.8).toFixed(2) + ')">' + c.toFixed(1) + "</span>" : '<span class="dim">·</span>') + "</td>";
    }
    h += "<td><b>" + t.toFixed(1) + "</b></td></tr>";
  }
  $("#matrix").innerHTML = h + "</table></div>";
}

// ---------------------------------------------------------------- token use
const WIN = { "1h": 1, "24h": 24, "7d": 168 };
const allProvs = () => [...Object.keys(PROV()), "other"];
const provs = () => (UI.prov === "all" || !PROV()[UI.prov] ? allProvs() : [UI.prov]);
function projTotals(u, hb) {
  const n = u.hours.length, from = n - hb, k = UI.metric, res = [];
  for (const [p, pv] of Object.entries(u.series)) {
    const r = { p, total: 0, all: 0, by: {} };
    for (const pr of Object.keys(pv)) { const v = pv[pr][k].slice(from).reduce((a, b) => a + b, 0); r.by[pr] = v; r.all += v; if (provs().includes(pr)) r.total += v; }
    if (r.total > 0) res.push(r);
  }
  return res.sort((a, b) => b.total - a.total);
}
function renderBurn(s) {
  const u = s.usage, n = u.hours.length;
  const tot = (pr, a, b) => Object.values(u.series).reduce((t, p) => t + (p[pr] ? p[pr].cost.slice(a, b).reduce((x, y) => x + y, 0) : 0), 0);
  $("#burn").innerHTML = Object.keys(PROV()).map((pr) => {
    const d = tot(pr, n - 24, n), avg = tot(pr, 0, n - 24) / 6, r = avg > 0 ? d / avg : null;
    if (!d && !avg) return "";
    return PIC(pr) + esc(pname(pr)) + " " + '<span style="color:var(--text)">' + fmt$(d) + "</span> in 24h" + (r ? ' <span class="' + (r > 1.5 ? "c-warn" : "") + '">(' + r.toFixed(1) + "× avg)</span>" : "");
  }).filter(Boolean).join(" &nbsp;·&nbsp; ");
}
function renderChart(s) {
  const u = s.usage, n = u.hours.length, hb = Math.max(24, WIN[UI.win]), from = n - hb;
  const bucket = hb > 48 ? 3 : 1, nb = Math.ceil(hb / bucket), k = UI.metric;
  const top = projTotals(u, hb).slice(0, 8).map((r) => r.p);
  const stacks = Array.from({ length: nb }, () => ({}));
  for (const [p, pv] of Object.entries(u.series)) {
    const key = top.includes(p) ? p : "other";
    for (const pr of provs()) if (pv[pr]) for (let i = from; i < n; i++) { const b = Math.floor((i - from) / bucket); stacks[b][key] = (stacks[b][key] || 0) + pv[pr][k][i]; }
  }
  const keys = [...top, "other"].filter((x) => stacks.some((st) => st[x]));
  const col = (key) => (key === "other" ? OTHER : color(key));
  const max = Math.max(1e-9, ...stacks.map((st) => Object.values(st).reduce((a, b) => a + b, 0)));
  const W = 1200, H = 170, pad = 40, bw = (W - pad) / nb;
  let svg = '<svg viewBox="0 0 ' + W + " " + (H + 20) + '">';
  for (let g = 0; g <= 2; g++) { const y = H - (g / 2) * (H - 8); svg += '<line x1="' + pad + '" x2="' + W + '" y1="' + y + '" y2="' + y + '" stroke="#26282d"/><text x="' + (pad - 6) + '" y="' + (y + 4) + '" fill="#5c6068" font-size="10" text-anchor="end">' + fmtM((max * g) / 2) + "</text>"; }
  stacks.forEach((st, b) => {
    let y = H; const x = pad + b * bw, d = new Date(u.hours[from + b * bucket] * 1000);
    for (const key of keys) { const v = st[key] || 0; if (!v) continue; const hh = (v / max) * (H - 8); y -= hh; svg += '<rect x="' + (x + 0.5) + '" y="' + y + '" width="' + Math.max(1, bw - 1.5) + '" height="' + hh + '" fill="' + col(key) + '"/>'; }
    svg += '<rect class="hov" data-b="' + b + '" x="' + x + '" y="0" width="' + bw + '" height="' + H + '" fill="transparent"/>';
    if ((bucket === 1 && d.getHours() % 6 === 0) || (bucket === 3 && d.getHours() < 3)) svg += '<text x="' + (x + bw / 2) + '" y="' + (H + 14) + '" fill="#5c6068" font-size="10" text-anchor="middle">' + (bucket === 3 || d.getHours() === 0 ? d.toLocaleDateString(undefined, { weekday: "short" }) : d.toLocaleTimeString(undefined, { hour: "numeric" })) + "</text>";
  });
  if (WIN[UI.win] < hb) { const x = pad + ((hb - WIN[UI.win]) / bucket) * bw; svg += '<rect x="' + pad + '" y="0" width="' + (x - pad) + '" height="' + H + '" fill="rgba(14,15,17,.6)"/>'; }
  $("#chart").innerHTML = svg + "</svg>";
  $("#legend").innerHTML = keys.map((k2) => '<span><span class="sq" style="background:' + col(k2) + '"></span>' + esc(k2) + "</span>").join("");
  document.querySelectorAll("#chart .hov").forEach((r) => {
    r.onmousemove = (e) => {
      const b = +r.dataset.b, st = stacks[b];
      const rows = keys.filter((x) => st[x]).sort((a, c) => st[c] - st[a]).map((x) => '<div class="r"><span><span class="sq" style="background:' + col(x) + '"></span>' + esc(x) + "</span><b>" + fmtM(st[x]) + "</b></div>").join("");
      showTip(e, '<div class="mut">' + when(u.hours[from + b * bucket]) + "</div>" + (rows || '<span class="mut">no use</span>'));
    };
    r.onmouseleave = hideTip;
  });
}
const openRows = new Set();
function modelTable(by, k) {
  const arr = Object.entries(by || {}).sort((a, b) => b[1][1] - a[1][1]);
  const all = arr.reduce((t, x) => t + (k === "cost" ? x[1][1] : x[1][2]), 0) || 1;
  return '<table class="mt"><tr><th>Model</th><th class=n>$ weight</th><th class=n>Tokens</th><th class=n>Output</th><th class=n data-tip="Share of input tokens read from cache">Cache hit</th><th class=n>Requests</th><th class=n>Share</th></tr>' +
    arr.map(([m, v]) => { const inp = v[3] + v[4] + v[5];
      return '<tr><td>' + (PIC(v[0]) || '<span class="sq" style="background:var(--other)"></span>') + " " + esc(m) + '</td><td class=n>' + fmt$(v[1]) + "</td><td class=n>" + fmtN(v[2]) + "</td><td class=n>" + fmtN(v[6]) + "</td><td class=n>" + (inp ? Math.round((100 * v[4]) / inp) + "%" : "") + "</td><td class=n>" + fmtN(v[7]) + '</td><td class="n mut">' + Math.round((100 * (k === "cost" ? v[1] : v[2])) / all) + "%</td></tr>"; }).join("") + "</table>";
}
function provFilter(by) {
  if (provs().length > 1) return by;
  const o = {}; for (const [m, v] of Object.entries(by || {})) if (v[0] === UI.prov) o[m] = v; return o;
}
function renderProjects(s) {
  const u = s.usage, rows = projTotals(u, WIN[UI.win]), all = rows.reduce((a, b) => a + b.total, 0) || 1, subs = u.subs[UI.win] || {};
  const pm = (u.proj_models || {})[UI.win] || {}, threads = u.threads[UI.win] || [];
  $("#projects").innerHTML = rows.length ? "<table>" + rows.slice(0, 12).map((r) => {
    const sb = Object.entries(subs[r.p] || {}).filter((e) => e[0] !== "-").sort((a, b) => b[1] - a[1]).slice(0, 3).map((e) => e[0]).join(", ");
    const t = r.all || 1, id = "p:" + r.p, open = openRows.has(id);
    let x = '<tr class="click' + (open ? " open" : "") + '" data-id="' + esc(id) + '"><td><span class="sq" style="background:' + color(r.p) + '"></span>' + esc(r.p) + (sb ? ' <span class="dim small">' + esc(sb) + "</span>" : "") +
      "</td>" + (UI.prov === "all" ? '<td class="n dim small pshare">' + Object.entries(r.by).filter((e) => e[1] > 0 && PROV()[e[0]]).sort((a, b) => b[1] - a[1]).map((e) => PIC(e[0]) + Math.round((100 * e[1]) / t) + "%").join(" ") + "</td>" : "") + "<td class=n>" + fmtM(r.total) + '</td><td class="n mut">' + Math.round((100 * r.total) / all) + "%</td></tr>";
    if (open) {
      const th = threads.filter((z) => z.project === r.p).slice(0, 6);
      x += '<tr class="exp"><td colspan="4">' + modelTable(provFilter(pm[r.p]), UI.metric) +
        (th.length ? '<div class="mut small" style="margin:10px 0 4px">Top threads</div><table class="mt">' + th.map((z) => '<tr><td><div class="title">' + esc(z.title) + '</div></td><td class=n>' + fmtM(UI.metric === "cost" ? z.cost : z.tok) + "</td></tr>").join("") + "</table>" : "") + "</td></tr>";
    }
    return x;
  }).join("") + "</table>" : '<div class="mut">No use in this window.</div>';
}
function renderThreads(s) {
  const k = UI.metric;
  let arr = (s.usage.threads[UI.win] || []).slice();
  const one = provs().length === 1 ? UI.prov : null;
  if (one) arr = arr.filter((t) => t.by_prov[one] > 0).sort((a, b) => b.by_prov[one] - a.by_prov[one]);
  const val = (t) => (k === "cost" ? (one ? t.by_prov[one] : t.cost) : t.tok);
  const all = arr.reduce((a, t) => a + val(t), 0) || 1;
  $("#threads").innerHTML = arr.length ? "<table>" + arr.slice(0, 12).map((t) => {
    const id = "t:" + t.host + ":" + t.title, open = openRows.has(id);
    let x = '<tr class="click' + (open ? " open" : "") + '" data-id="' + esc(id) + '"><td><div class="title" data-tip="' + esc(t.title) + '">' + Object.keys(t.by_prov).filter((p) => t.by_prov[p] > 0).map(PIC).join("") + " " + esc(t.title) + '</div><div class="dim small">' + esc(t.project) + (t.sub ? " / " + esc(t.sub) : "") + " · " + esc(t.client) + " · " + esc(t.host) + " · " + rel(t.last - s.now) +
      "</div></td><td class=n>" + fmtM(val(t)) + '</td><td class="n mut">' + Math.round((100 * val(t)) / all) + "%</td></tr>";
    if (open) x += '<tr class="exp"><td colspan="3">' + modelTable(provFilter(t.by_model), k) + '<div class="dim small" style="margin-top:6px">' + t.sessions + " sessions, subagents included</div></td></tr>";
    return x;
  }).join("") + "</table>" : '<div class="mut">No threads in this window.</div>';
}
document.addEventListener("click", (e) => {
  const tr = e.target.closest("tr.click"); if (!tr) return;
  const id = tr.dataset.id; openRows.has(id) ? openRows.delete(id) : openRows.add(id);
  renderUsage(STATE);
});

function renderUsage(s) {
  document.querySelectorAll(".seg").forEach((seg) => seg.querySelectorAll("button").forEach((b) => b.classList.toggle("on", UI[seg.dataset.k] === b.dataset.v)));
  renderBurn(s); renderChart(s); renderProjects(s); renderThreads(s);
}

// ---------------------------------------------------------------- main
function render() {
  const s = STATE; if (!s) return;
  document.title = s.meta.title; $("#title").textContent = s.meta.title; $("#demo").hidden = !s.meta.demo;
  FIXED = Object.assign({ system: "#5c6068" }, s.meta.project_colors);
  const tot7 = Object.entries(s.usage.series).map(([p, pv]) => [p, Object.values(pv).reduce((a, x) => a + x.cost.reduce((b, c) => b + c, 0), 0)]);
  RANK = {}; tot7.filter((e) => !FIXED[e[0]]).sort((a, b) => b[1] - a[1]).forEach((e, i) => (RANK[e[0]] = i));
  const used = new Set(Object.values(FIXED)); let k = 0;
  for (const p of Object.keys(RANK)) { while (used.has(PALETTE[(RANK[p] + k) % PALETTE.length]) && k < PALETTE.length) k++; RANK[p] += k; }
  const ps = Object.keys(PROV());
  if ($("#provseg").dataset.k2 !== ps.join()) { $("#provseg").dataset.k2 = ps.join(); $("#provseg").innerHTML = (ps.length > 1 ? '<button data-v="all">All</button>' : "") + ps.map((p) => '<button data-v="' + p + '">' + PIC(p) + esc(pname(p)) + "</button>").join(""); }
  if (!ps.includes(UI.prov)) UI.prov = ps.length > 1 ? "all" : ps[0] || "all";
  renderTop(s); renderPools(s); renderHosts(s); renderMatrix(s);
  document.querySelectorAll(".acctd").forEach((el) => (el.ontoggle = () => { el.open ? openHosts.add("__acct") : openHosts.delete("__acct"); localStorage.setItem("fc-open", JSON.stringify([...openHosts])); document.querySelectorAll(".acctd").forEach((o) => (o.open = el.open)); })); renderUsage(s);
}
function tickAge() { if (STATE) $("#age").textContent = "updated " + rel(STATE.now - Date.now() / 1000); }
// The server serves api/state. A static copy (the hosted demo) serves state.json; its times are moved to now.
async function load() {
  try {
    let s = null;
    if (!STATIC) try { const r = await fetch("api/state", { cache: "no-store" }); if (r.ok) s = await r.json(); } catch (e) {}
    if (!s || !s.hosts) {
      const r = await fetch("state.json", { cache: "no-store" }); if (!r.ok) throw new Error();
      const j = await r.json(); s = shiftTimes(j, Date.now() / 1000 - j.now); STATIC = true;
      $("#refresh").hidden = true;
    }
    RAW = s; STATE = UI.private ? privatize(RAW) : RAW; render();
  } catch (e) { console.error(e); if (!RAW) $("#age").textContent = "server not answering"; }
}
function shiftTimes(o, d) {
  if (Array.isArray(o)) return o.map((x) => shiftTimes(x, d));
  if (o && typeof o === "object") { const r = {}; for (const [k, v] of Object.entries(o)) r[/^1[6-9]\d{8}$/.test(k) ? String(Math.round(+k + d)) : k] = shiftTimes(v, d); return r; }
  return typeof o === "number" && o > 1.6e9 && o < 2.2e9 ? o + d : o;
}

// ---------------------------------------------------------------- hide names (for screenshots)
// Replaces machine, account, project, sub-project, user and thread names with made-up ones, everywhere in the
// state (issue texts, paths and tooltips too). Command lines are cut to the program name.
const FAKE = {
  host: ["atlas", "borealis", "cobalt", "dynamo", "ember", "falcon", "granite", "helix", "ion", "juno", "kepler", "lumen", "meridian", "nimbus"],
  acct: ["work", "personal", "team", "lab", "side", "alt", "ops", "research"],
  proj: ["api-server", "web-app", "ml-pipeline", "mobile", "infra", "docs-site", "search", "billing", "analytics", "cli-tools", "data-lake", "design-system", "notifications", "payments", "auth-service", "edge-cache"],
  sub: ["core", "worker", "ui", "eval", "ingest", "jobs", "gateway", "sync", "admin", "export"],
  title: ["Fix flaky checkout test", "Migrate auth to OAuth 2.1", "Speed up search indexing", "Train ranking model v3", "Eval harness for ranker",
    "Dark mode for dashboard", "Move infra to new region", "Write API reference", "Debug memory leak in worker", "Billing: proration edge cases",
    "Push notifications", "Refactor data loaders", "Upgrade to React 20", "Rate limiter for public API", "Nightly backup job", "Investigate p99 latency"],
};
function privatize(raw) {
  const words = new Map(), titles = new Map(), keep = new Set(["system", "other", "", "?", "-"]);
  const put = (real, fake) => { if (real && !keep.has(real) && !words.has(real)) words.set(real, fake); };
  const pick = (list, i) => list[i % list.length] + (i >= list.length ? "-" + (1 + Math.floor(i / list.length)) : "");
  raw.hosts.forEach((h, i) => {
    const f = pick(FAKE.host, i);
    put(h.name, f); put(h.label, f); put(h.hostname, f);
    const ssh = (h.kill_prefix || "").match(/^ssh (\S+)/); if (ssh) put(ssh[1], f);
  });
  [...new Set(raw.accounts.map((a) => a.label))].forEach((l, i) => { put(l, pick(FAKE.acct, i) + "@example.com"); if (l.includes("@")) put(l.split("@")[0], pick(FAKE.acct, i)); });
  const u = raw.usage, cost = {};
  for (const [p, pv] of Object.entries(u.series)) cost[p] = Object.values(pv).reduce((a, x) => a + x.cost.reduce((b, c) => b + c, 0), 0);
  for (const h of raw.hosts) for (const p of h.projects || []) cost[p.project] = cost[p.project] || 0;
  Object.keys(cost).sort((a, b) => cost[b] - cost[a]).filter((p) => !keep.has(p)).forEach((p, i) => put(p, pick(FAKE.proj, i)));
  const subs = new Set();
  for (const w of Object.values(u.subs || {})) for (const d of Object.values(w)) Object.keys(d).forEach((s) => subs.add(s));
  for (const w of Object.values(u.threads)) w.forEach((t) => subs.add(t.sub));
  for (const h of raw.hosts) for (const p of [...(h.procs || []), ...(h.mem_top || [])]) subs.add(p.sub);
  [...subs].forEach((s, i) => put(s, pick(FAKE.sub, i)));
  const users = new Set(); JSON.stringify(raw).replace(/\/(?:Users|home)\/([^\/\s"\\]+)/g, (m, n) => users.add(n));
  users.forEach((n) => put(n, "dev"));
  let ti = 0; for (const w of Object.values(u.threads)) for (const t of w) if (!titles.has(t.title)) titles.set(t.title, pick(FAKE.title, ti++));
  const esc2 = (x) => x.replace(/[.*+?^\${}()|[\]\\]/g, "\\$&");
  const rx = words.size ? new RegExp("(?<![\\w-])(" + [...words.keys()].sort((a, b) => b.length - a.length).map(esc2).join("|") + ")(?![\\w-])", "g") : null;
  const str = (v) => (titles.has(v) ? titles.get(v) : rx ? v.replace(rx, (m) => words.get(m)) : v);
  const walk = (o) => {
    if (Array.isArray(o)) return o.map(walk);
    if (!o || typeof o !== "object") return typeof o === "string" ? str(o) : o;
    // Rename keys only in maps keyed by names (every key is a name). Records keep their field names,
    // even when a project has the same name as a field (for example "projects").
    const ks = Object.keys(o), byName = ks.every((k) => words.has(k) || titles.has(k) || keep.has(k));
    const r = {};
    for (const [k, v] of Object.entries(o)) r[byName ? (words.get(k) || titles.get(k) || k) : k] = walk(v);
    if ("cmd" in o && "comm" in o) r.cmd = r.comm;
    if (o.user) r.user = "dev";
    if ("cwd" in o) r.cwd = "project" in r && !keep.has(r.project) ? "~/src/" + r.project + (r.sub ? "/" + r.sub : "") : "";
    return r;
  };
  return walk(raw);
}
document.querySelectorAll(".seg").forEach((seg) => seg.addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  UI[seg.dataset.k] = b.dataset.v; localStorage.setItem("fc-ui", JSON.stringify(UI)); renderUsage(STATE);
}));
$("#refresh").onclick = async () => { $("#refresh").textContent = "Refreshing"; await fetch("api/refresh"); setTimeout(() => { $("#refresh").textContent = "Refresh"; load(); }, 8000); };
load(); setInterval(() => !STATIC && load(), 15000); setInterval(tickAge, 1000);
