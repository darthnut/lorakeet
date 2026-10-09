"use strict";

const ACTIVE_S = 2 * 3600, RECENT_S = 24 * 3600;

const S = {
  nodes: new Map(), status: {}, selected: null, markers: new Map(), circles: new Map(),
  links: [], fitted: false, tab: "nodes", feedCount: 0,
  msgs: [], conv: "^all", unread: new Map(), traces: new Map(),
};
const BROADCAST = "^all";
const MAX_BYTES = 200;
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const now = () => Date.now() / 1000;

function ago(ts) {
  if (!ts) return "never";
  const s = Math.max(0, now() - ts);
  if (s < 60) return `${Math.round(s)}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${(s / 3600).toFixed(s < 36000 ? 1 : 0)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}
const clock = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
const status = (n) => {
  if (n.isLocal) return "active";
  const age = n.lastHeard ? now() - n.lastHeard : Infinity;
  return age < ACTIVE_S ? "active" : age < RECENT_S ? "recent" : "stale";
};
const fmt = (v, d = 1, u = "") => (v == null ? "—" : `${Number(v).toFixed(d)}${u}`);
const prettyHw = (h) => (h || "").replace(/_/g, " ");
const prettyRole = (r) => (r || "CLIENT").replace(/_/g, " ").toLowerCase();

// Meshtastic truncates positions to N bits; ~23.3 km box at 10 bits, halving per bit.
const precisionMeters = (bits) => (bits && bits < 32 ? 23300 * 2 ** (10 - bits) : 0);

/* ---------------------------------------------------------------- map */

const map = L.map("map", { zoomControl: true, attributionControl: true }).setView(...fallbackView());
// until a node shares a position, show the configured area (config arrives a moment after load)
window.LK_CFG_READY.then(() => { if (!S.fitted) map.setView(...fallbackView()); });
const dark = matchMedia("(prefers-color-scheme: dark)");
// tile layers come from prov.js (OpenStreetMap-based by default; Esri when configured), built per provider
let tiles = {}, tilesFor = null;
let basemap = "map", currentTiles = null;
function applyTiles() {
  const p = lkTilesProvider();
  if (p !== tilesFor) {  // first run, or the config arrived and changed the provider
    tilesFor = p;
    tiles = { light: lkTiles("light"), dark: lkTiles("dark"), topo: lkTiles("topo"), sat: lkTiles("sat") };
    const satBtn = document.querySelector('#basemap [data-layer="sat"]');
    if (satBtn) satBtn.hidden = !tiles.sat;
    if (!tiles[basemap] && basemap !== "map") basemap = "map";
    if (currentTiles) { map.removeLayer(currentTiles); currentTiles = null; }
  }
  const t = basemap === "map" ? (dark.matches ? tiles.dark : tiles.light) : tiles[basemap];
  if (t === currentTiles) return;
  if (currentTiles) map.removeLayer(currentTiles);
  currentTiles = t.addTo(map);
}
applyTiles();
window.LK_CFG_READY.then(applyTiles);
dark.addEventListener("change", applyTiles);
$("basemap").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  basemap = b.dataset.layer;
  for (const x of $("basemap").children) x.classList.toggle("on", x === b);
  applyTiles();
});

const linkLayer = L.layerGroup().addTo(map);
const circleLayer = L.layerGroup().addTo(map);
const routeLayer = L.layerGroup().addTo(map);
$("showLinks").addEventListener("change", (e) => (e.target.checked ? linkLayer.addTo(map) : map.removeLayer(linkLayer)));
$("showStale").addEventListener("change", renderMarkers);
$("showEst").addEventListener("change", renderMarkers);
S.est = new Map();
async function loadEstimates() {
  try {
    const r = await fetch("/api/insights/estimates").then((x) => x.json());
    S.est = new Map((r.estimates || []).map((e) => [e.id, e]));
    renderMarkers();
  } catch { /* server restarting */ }
}
setTimeout(loadEstimates, 1500);
setInterval(loadEstimates, 10 * 60 * 1000);

// Our USB radio sits in the house; if it has no GPS, draw it at the base station.
function posOf(n) {
  if (!n) return null;
  if (n.lat != null && n.lon != null) return [n.lat, n.lon];
  if (n.isLocal) {
    const base = [...S.nodes.values()].find((x) => x.isBase && x.lat != null);
    if (base) return [base.lat, base.lon];
  }
  return null;
}

function markerHtml(n, est) {
  const cls = ["mk", n.isBase ? "base" : status(n), n.isStation || (n.isLocal && n.positionFromStation) ? "station" : "",
    S.selected === n.id ? "sel" : "", est ? "est" : ""].join(" ");
  return `<div class="${cls}"><div class="d"></div><div class="l">${esc(n.shortName)}</div></div>`;
}

function renderMarkers() {
  const showStale = $("showStale").checked;
  const seen = new Set();
  const showEst = $("showEst").checked;
  for (const n of S.nodes.values()) {
    let p = posOf(n);
    const e = !p && showEst && !n.isLocal ? S.est.get(n.id) : null;  // inferred, never reported
    if (e) p = [e.lat, e.lon];
    if (!p || (!showStale && status(n) === "stale" && !n.isBase && !n.isLocal)) continue;
    if (n.isLocal && n.lat == null) continue; // drawn under the base marker; avoid a duplicate dot
    seen.add(n.id);
    const icon = L.divIcon({ className: "", html: markerHtml(n, !!e), iconSize: [0, 0] });
    let m = S.markers.get(n.id);
    if (!m) {
      m = L.marker(p, { icon, riseOnHover: true, zIndexOffset: n.isBase ? 1000 : 0 })
        .on("click", () => selectNode(n.id))
        .bindTooltip("", { direction: "top", offset: [0, -10] })
        .addTo(map);
      S.markers.set(n.id, m);
    } else {
      m.setLatLng(p); m.setIcon(icon);
    }
    m.setTooltipContent(`<b>${esc(n.longName)}</b><br>${ago(n.lastHeard)}${n.battery != null ? ` · ${fmt(n.battery, 0, "%")}` : ""}` +
      (e ? `<br>Estimated position ${prov("inferred")} ±${e.radiusKm.toFixed(1)} km<br><span style="opacity:.75">${esc(e.method)}</span>` : ""));

    const r = e ? e.radiusKm * 1000 : precisionMeters(n.precisionBits) / 2;
    let c = S.circles.get(n.id);
    if (r > 0) {
      if (!c) { c = L.circle(p, { radius: r, weight: 1, opacity: .5, fillOpacity: .05, interactive: false }).addTo(circleLayer); S.circles.set(n.id, c); }
      c.setLatLng(p); c.setRadius(r);
      c.setStyle({ color: getComputedStyle(document.documentElement).getPropertyValue(e ? "--text-secondary" : n.isBase ? "--base" : "--accent").trim(),
        dashArray: e ? "4 5" : null });
    } else if (c) { circleLayer.removeLayer(c); S.circles.delete(n.id); }
  }
  for (const [id, m] of S.markers) if (!seen.has(id)) { map.removeLayer(m); S.markers.delete(id); }
  for (const [id, c] of S.circles) if (!seen.has(id)) { circleLayer.removeLayer(c); S.circles.delete(id); }

  const missing = [...S.nodes.values()].filter((n) => !posOf(n)).length;
  const estN = showEst ? [...S.nodes.values()].filter((n) => !posOf(n) && S.est.has(n.id)).length : 0;
  $("nopos").hidden = missing === 0;
  $("nopos").textContent = `${missing} of ${S.nodes.size} nodes haven't shared a position yet` + (estN ? ` · ${estN} shown at estimated positions (dashed)` : "");

  if (!S.fitted) {
    const pts = [...S.nodes.values()].map(posOf).filter(Boolean);
    if (pts.length > 1) { map.fitBounds(pts, { padding: [60, 60], maxZoom: 13 }); S.fitted = true; }
    else if (pts.length === 1) { map.setView(pts[0], 12); S.fitted = true; }
  }
  renderLinks();
}

function renderLinks() {
  linkLayer.clearLayers();
  const color = getComputedStyle(document.documentElement).getPropertyValue("--link").trim();
  const done = new Set();
  for (const l of S.links) {
    const key = [l.a, l.b].sort().join("|");
    if (done.has(key)) continue;
    const a = posOf(S.nodes.get(l.a)), b = posOf(S.nodes.get(l.b));
    if (!a || !b || (a[0] === b[0] && a[1] === b[1])) continue;
    done.add(key);
    L.polyline([a, b], { color, weight: 2, dashArray: l.source === "traceroute" ? "4 6" : null, interactive: false }).addTo(linkLayer);
  }
}

function ping(id) {
  const m = S.markers.get(id);
  const el = m && m.getElement();
  if (!el) return;
  const mk = el.querySelector(".mk"); if (!mk) return;
  const p = document.createElement("div"); p.className = "ping";
  mk.appendChild(p); setTimeout(() => p.remove(), 1500);
}

/* ---------------------------------------------------------------- charts */

const tip = $("tip");
function lineChart(el, rows, key, { title, unit = "", digits = 1, height = 90, yMin, yMax, hours = 72, empty } = {}) {
  const pts = rows.filter((r) => r[key] != null).map((r) => [r.ts, r[key]]);
  const W = el.clientWidth || 320, H = height, pl = 34, pr = 8, pt = 6, pb = 18;
  const head = title ? `<h4>${esc(title)}</h4>` : "";
  if (pts.length === 0) {
    el.innerHTML = `${head}<svg viewBox="0 0 ${W} ${H}" height="${H}"><text class="empty" x="${W / 2}" y="${H / 2}" text-anchor="middle">${esc(empty || "No readings yet — they'll appear as telemetry arrives")}</text></svg>`;
    return;
  }
  const t1 = now(), t0 = t1 - hours * 3600;
  let lo = yMin ?? Math.min(...pts.map((p) => p[1])), hi = yMax ?? Math.max(...pts.map((p) => p[1]));
  if (hi - lo < 1e-6) { lo -= 1; hi += 1; }
  const pad = (hi - lo) * 0.1; if (yMin == null) lo -= pad; if (yMax == null) hi += pad;
  const x = (t) => pl + ((t - t0) / (t1 - t0)) * (W - pl - pr);
  const y = (v) => pt + (1 - (v - lo) / (hi - lo)) * (H - pt - pb);
  const d = pts.map((p, i) => `${i ? "L" : "M"}${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join("");
  const area = `${d}L${x(pts.at(-1)[0]).toFixed(1)},${H - pb}L${x(pts[0][0]).toFixed(1)},${H - pb}Z`;
  const yt = [lo + (hi - lo) * 0.15, lo + (hi - lo) * 0.85];
  const xt = [];
  for (let h = Math.ceil(t0 / 21600) * 21600; h <= t1; h += 21600) xt.push(h);
  const xLabel = (t) => { const dt = new Date(t * 1000); return dt.getHours() === 0 ? dt.toLocaleDateString([], { weekday: "short" }) : `${dt.getHours()}:00`; };
  el.innerHTML = `${head}<svg viewBox="0 0 ${W} ${H}" height="${H}">
    ${yt.map((v) => `<line class="grid" x1="${pl}" x2="${W - pr}" y1="${y(v)}" y2="${y(v)}"/><text class="axis" x="${pl - 4}" y="${y(v) + 3}" text-anchor="end">${v.toFixed(digits)}</text>`).join("")}
    ${xt.filter((_, i) => i % 2 === 0).map((t) => `<text class="axis" x="${x(t)}" y="${H - 4}" text-anchor="middle">${xLabel(t)}</text>`).join("")}
    <path class="area" d="${area}"/><path class="line" d="${d}"/>
    ${pts.length < 3 ? pts.map((p) => `<circle class="pt" cx="${x(p[0])}" cy="${y(p[1])}" r="4"/>`).join("") : `<circle class="pt" cx="${x(pts.at(-1)[0])}" cy="${y(pts.at(-1)[1])}" r="4"/>`}
    <line class="xhair" y1="${pt}" y2="${H - pb}" visibility="hidden"/>
    <circle class="pt hov" r="4" visibility="hidden"/>
    <rect x="${pl}" y="0" width="${W - pl - pr}" height="${H}" fill="transparent"/>
  </svg>`;
  const svg = el.querySelector("svg"), xh = svg.querySelector(".xhair"), hv = svg.querySelector(".hov");
  svg.addEventListener("pointermove", (e) => {
    const r = svg.getBoundingClientRect();
    const mx = ((e.clientX - r.left) / r.width) * W;
    let best = pts[0];
    for (const p of pts) if (Math.abs(x(p[0]) - mx) < Math.abs(x(best[0]) - mx)) best = p;
    const cx = x(best[0]), cy = y(best[1]);
    xh.setAttribute("x1", cx); xh.setAttribute("x2", cx); xh.setAttribute("visibility", "visible");
    hv.setAttribute("cx", cx); hv.setAttribute("cy", cy); hv.setAttribute("visibility", "visible");
    tip.hidden = false;
    tip.innerHTML = `<b>${best[1].toFixed(digits)}${unit}</b> <span class="t">${new Date(best[0] * 1000).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" })}</span>`;
    tip.style.left = `${Math.min(e.clientX + 12, innerWidth - tip.offsetWidth - 8)}px`;
    tip.style.top = `${e.clientY - 34}px`;
  });
  svg.addEventListener("pointerleave", () => { tip.hidden = true; xh.setAttribute("visibility", "hidden"); hv.setAttribute("visibility", "hidden"); });
}

/* ---------------------------------------------------------------- sidebar */

function tile(k, v, unit = "") { return `<div class="tile"><div class="k">${k}</div><div class="v">${v}${unit && v !== "—" ? `<small>${unit}</small>` : ""}</div></div>`; }

function renderStats() {
  const all = [...S.nodes.values()].filter((n) => !n.isLocal);
  const act = all.filter((n) => status(n) === "active").length;
  const day = all.filter((n) => status(n) !== "stale").length;
  const pos = all.filter((n) => n.lat != null).length;
  $("stats").innerHTML = `<span><b>${act}</b> heard in 2 h</span><span><b>${day}</b> in 24 h</span><span><b>${all.length}</b> known</span><span><b>${pos}</b> on map</span>`;
}

function renderConn() {
  const c = $("conn"), st = S.status;
  c.className = `conn ${st.connected ? "ok" : "bad"}`;
  const local = S.nodes.get(st.localId);
  c.querySelector(".label").textContent = st.connected ? `${local ? local.longName : st.localId} · ${st.port}` : st.serverDown ? "Dashboard server unreachable — retrying" : "Radio unplugged — showing last known state, retrying";
}

function renderList() {
  const q = $("search").value.trim().toLowerCase();
  const rows = [...S.nodes.values()]
    .filter((n) => !q || `${n.longName} ${n.shortName} ${n.id} ${n.hwModel}`.toLowerCase().includes(q))
    .sort((a, b) => (b.isBase - a.isBase) || (b.isLocal - a.isLocal) || ((b.lastHeard || 0) - (a.lastHeard || 0)));
  if (!rows.length) { $("nodelist").innerHTML = `<li class="emptystate" style="display:block">${q ? "No matches" : "No nodes heard yet"}</li>`; return; }
  $("nodelist").innerHTML = rows.map((n) => {
    const tags = [n.isBase ? '<span class="tag base">base</span>' : "", n.isLocal ? '<span class="tag">this radio</span>' : "",
      n.isStation ? '<span class="tag">station</span>' : "", n.remote && !n.heardHere ? `<span class="tag" title="Heard only by the listening station ${esc(n.remote.stationName)}">via ${esc(n.remote.stationName)}</span>` : "",
      n.battery != null && n.battery <= 20 ? '<span class="tag low">⚠ low batt</span>' : "", keyBadge(n.keyFlag), v28Tag(n.v28), n.lat == null && !n.isLocal ? '<span class="tag">no position</span>' : ""].join("");
    const sig = n.isLocal ? "" : n.hopsAway === 0 ? `SNR ${fmt(n.snr, 1)} dB` : n.hopsAway != null ? `${n.hopsAway} hop${n.hopsAway === 1 ? "" : "s"}` : "";
    return `<li data-id="${esc(n.id)}"><span class="sdot ${status(n)}"></span>
      <div style="min-width:0"><div class="nm">${esc(n.longName)}${tags}</div><div class="sub">${esc([prettyHw(n.hwModel), n.relayOnly ? "seen relaying" : prettyRole(n.role)].filter(Boolean).join(" · "))}</div></div>
      <div class="right">${n.isLocal ? "USB" : ago(n.lastHeard)}<br>${sig}</div></li>`;
  }).join("");
}
$("nodelist").addEventListener("click", (e) => { const li = e.target.closest("li[data-id]"); if (li) selectNode(li.dataset.id, true); });
$("search").addEventListener("input", renderList);

// Packets carry only the last byte of the relaying node's number; resolve it when unambiguous.
function relayName(b) {
  const hex = b.toString(16).padStart(2, "0");
  if (S.status.baseId && S.status.baseId.endsWith(hex)) return `${S.status.baseName} (base)`;
  const m = [...S.nodes.values()].filter((n) => n.id.endsWith(hex) && !n.isLocal);
  return m.length === 1 ? m[0].shortName : `…${hex}`;
}
// Which other listening stations heard a packet (sync hub). Ours isn't badged; a packet only another
// station heard says so.
function stationBadges(p) {
  const local = S.status?.localId, names = S.status?.stations || {};
  const ids = (p.stations ? String(p.stations).split(",") : [p.station]).filter(Boolean);
  const others = ids.filter((s) => s !== local);
  if (!others.length) return "";
  const only = local && !ids.includes(local);
  return others.map((s) => `<span class="arr st" data-st="${esc(s)}" title="${only ? "Heard only by" : "Also heard by"} the listening station ${esc(names[s] || s)}">${only ? "heard by" : "+"} ${esc(names[s] || s)}</span>`).join("");
}

function feedItem(p, flash) {
  const sig = [p.hops != null ? `${p.hops} hop${p.hops === 1 ? "" : "s"}` : "", p.snr != null ? `SNR ${fmt(p.snr, 1)}` : "", p.rssi != null ? `RSSI ${p.rssi}` : "", p.hops > 0 && p.relay ? `via ${relayName(p.relay)}` : "", p.via_mqtt ? "MQTT" : "", p.pki ? "🔒 PKI" : ""].filter(Boolean).join(" · ");
  const name = p.name || S.nodes.get(p.from_id)?.longName || p.from_id;
  const local = p.arrival === "local"
    ? '<span class="arr" title="The radio reported this to the dashboard itself; it never went over the air (no transport, no signal reading).">local · not over the air</span>' : "";
  return `<li class="${flash ? "flash" : ""} ${local ? "local" : ""}" data-row="${p.rowid ?? ""}" data-key="${p.pkt_id != null ? esc(`${p.from_id}:${p.pkt_id}`) : ""}" title="Click for the full packet"><div class="row1"><span class="who" data-id="${esc(p.from_id)}">${esc(name)}${keyBadge(S.nodes.get(p.from_id)?.keyFlag, { short: true })}${local}${stationBadges(p)}</span><span class="when">${clock(p.ts)}</span></div>
    <div class="row2"><span class="port">${esc((p.portnum || "").replace(/_APP$/, ""))}</span><span>${esc(sig)}</span></div>
    ${p.summary ? `<div class="row2">${esc(p.summary)}</div>` : ""}</li>`;
}
// Clicking a packet (anywhere but the sender's name) toggles its full logged contents.
async function toggleRaw(li) {
  const open = li.querySelector("pre.raw");
  if (open) { open.remove(); return; }
  if (!li.dataset.row) return;
  const raw = await fetch(`/api/packet/${li.dataset.row}`).then((r) => r.json());
  if (li.querySelector("pre.raw")) return;
  li.insertAdjacentHTML("beforeend", `<pre class="raw"><a class="anat-link" href="/packets.html#packet=${encodeURIComponent(li.dataset.row)}" target="_blank" rel="noopener">Packet anatomy: bytes, encryption, fields ↗</a>
${esc(JSON.stringify(raw, null, 2))}</pre>`);
}
for (const id of ["feed", "dFeed"]) {
  document.addEventListener("click", (e) => {
    const list = e.target.closest(`#${id}`); if (!list) return;
    const w = e.target.closest(".who"); if (w) { selectNode(w.dataset.id, true); return; }
    if (e.target.closest("pre.raw")) return; // let people select text in the JSON
    const li = e.target.closest("li[data-row]"); if (li) toggleRaw(li);
  });
}

/* ---------------------------------------------------------------- messages */

const nodeName = (id) => S.nodes.get(id)?.longName || id;
const isBroadcast = (to) => !to || to === BROADCAST || to === "!ffffffff";
const convKey = (m) => (isBroadcast(m.to_id) ? BROADCAST : m.outgoing ? m.to_id : m.from_id);
const convName = (k) => (k === BROADCAST ? "Channels" : nodeName(k));

/* ---------------------------------------------------------------- channels
   The broadcast conversation is split by channel: the picker chooses which one to read and send on.
   Messages carry our radio's channel index (received and sent alike). */
S.channels = [];
S.chan = (() => { try { return Number(localStorage.getItem("meshdash.chan") || 0); } catch { return 0; } })();
S.chanUnread = new Map();
const msgChan = (m) => m.channel ?? 0;
const chanName = (i) => S.channels.find((c) => c.index === i)?.name || (i ? `Channel ${i}` : "LongFast");
function chanPrivacy(c) {
  if (!c) return ["", ""];
  if (!c.encrypted) return ["Not encrypted: anyone who adds a channel with this name can read it", ""];
  if (c.publicKey) return [`Uses the publicly known default key: readable by any Meshtastic user in range`, ""];
  return ["🔒 Private: only radios with this channel's key can read it", "private"];
}
async function loadChannels() {
  try { S.channels = (await fetch("/api/channels").then((r) => r.json())).channels || []; } catch { return; }
  if (!S.channels.some((c) => c.index === S.chan)) S.chan = 0;
  renderChanBar();
}
function renderChanBar() {
  const bar = $("chanBar");
  bar.hidden = S.conv !== BROADCAST || S.channels.length < 2;
  if (bar.hidden) return;
  const opts = S.channels.map((c) => {
    const n = S.chanUnread.get(c.index) || 0;
    return `<option value="${c.index}"${c.index === S.chan ? " selected" : ""}>${esc(c.name)}${!c.encrypted ? " (open)" : c.publicKey ? "" : " 🔒"}${n ? ` · ${n} new` : ""}</option>`;
  }).join("");
  if ($("chanPick").innerHTML !== opts) $("chanPick").innerHTML = opts;
  const [note, cls] = chanPrivacy(S.channels.find((c) => c.index === S.chan));
  $("chanNote").textContent = note;
  $("chanNote").className = `channote ${cls}`;
}
$("chanPick").addEventListener("change", (e) => {
  S.chan = Number(e.target.value);
  try { localStorage.setItem("meshdash.chan", String(S.chan)); } catch { /* storage blocked */ }
  S.chanUnread.delete(S.chan);
  renderMessages(); $("msgText").focus();
});
const bytes = (s) => new TextEncoder().encode(s).length;

const STATUS = {
  sending: ["Sending…", ""],
  relayed: ["✓ Relayed by a neighbour", "good"],
  delivered: ["✓✓ Delivered", "good"],
  "no ack": ["No confirmation heard", "bad"],
};
function statusLabel(st) {
  if (!st) return ["", ""];
  if (STATUS[st]) return STATUS[st];
  if (st.startsWith("failed")) return [`✕ ${st[0].toUpperCase()}${st.slice(1)}`, "bad"];
  return [st, ""];
}

function renderConvs() {
  const keys = [BROADCAST];
  for (const m of [...S.msgs].reverse()) { const k = convKey(m); if (!keys.includes(k)) keys.push(k); }
  if (!keys.includes(S.conv)) keys.push(S.conv);
  $("convs").innerHTML = keys.map((k) => {
    const u = S.unread.get(k);
    return `<button data-k="${esc(k)}" class="${k === S.conv ? "on" : ""}">${esc(convName(k))}${u ? `<span class="n">${u}</span>` : ""}</button>`;
  }).join("");
  const total = [...S.unread.values()].reduce((a, b) => a + b, 0);
  $("unread").hidden = !total;
  $("unread").textContent = total;
}

function renderChat() {
  const rows = S.msgs.filter((m) => convKey(m) === S.conv && (S.conv !== BROADCAST || msgChan(m) === S.chan));
  const chat = $("chat");
  const atBottom = chat.scrollHeight - chat.scrollTop - chat.clientHeight < 40;
  chat.innerHTML = rows.length ? rows.map((m) => {
    const when = new Date(m.ts * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
    // sent by this radio = "You"; sent by another of our stations = shown as from that station
    const mine = m.outgoing && (!m.station || !S.status.localId || m.station === S.status.localId);
    const [st, cls] = mine ? statusLabel(m.status) : ["", ""];
    return `<li class="${mine ? "out" : ""}">
      <div class="meta">${mine ? "You" : `<b data-id="${esc(m.from_id)}">${esc(m.name || nodeName(m.from_id))}</b>`} · ${when}</div>
      ${m.encrypted
        ? `<div class="bubble locked">🔒 Couldn't decrypt this direct message. This radio doesn't have the sender's public key yet. Keys are exchanged automatically after a failed DM, so a resend usually works.</div>`
        : `<div class="bubble">${esc(m.text)}</div>`}${st ? `<div class="st ${cls}">${esc(st)}</div>` : ""}</li>`;
  }).join("") : `<li class="emptystate">${S.conv === BROADCAST ? `No messages on ${esc(chanName(S.chan))} yet.` : `No messages with ${esc(convName(S.conv))} yet.`}</li>`;
  if (atBottom || rows.length < 3) chat.scrollTop = chat.scrollHeight;
  $("composeTo").innerHTML = S.conv === BROADCAST
    ? `To <b>everyone on ${esc(chanName(S.chan))}</b>`
    : `Direct message to <b>${esc(convName(S.conv))}</b>`;
}

function renderMessages() { renderConvs(); renderChanBar(); if (S.tab === "messages") renderChat(); }

function openConversation(k) {
  S.conv = k; S.unread.delete(k);
  setTab("messages");
  S.selected = null; renderMarkers();
}

$("convs").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-k]"); if (!b) return;
  S.conv = b.dataset.k; S.unread.delete(S.conv); renderMessages(); $("msgText").focus();
});
$("chat").addEventListener("click", (e) => { const b = e.target.closest("b[data-id]"); if (b) selectNode(b.dataset.id, true); });

function updateCount() {
  const n = bytes($("msgText").value);
  $("msgCount").textContent = `${n} / ${MAX_BYTES}`;
  $("msgCount").classList.toggle("over", n > MAX_BYTES);
  $("sendBtn").disabled = !$("msgText").value.trim() || n > MAX_BYTES || !S.status.connected;
}
$("msgText").addEventListener("input", updateCount);
$("msgText").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("compose").requestSubmit(); }
});
$("compose").addEventListener("submit", async (e) => {
  e.preventDefault();
  if ($("sendBtn").disabled) return;
  $("sendBtn").disabled = true; $("sendErr").textContent = "";
  try {
    const r = await postJson("/api/send", { text: $("msgText").value, to: S.conv === BROADCAST ? null : S.conv,
                                            channel: S.conv === BROADCAST ? S.chan : 0 });
    if (r.error) throw new Error(r.error);
    $("msgText").value = "";
  } catch (err) {
    $("sendErr").textContent = err.message;
  }
  updateCount();
});

async function postJson(url, body) {
  const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  return r.json();
}

/* ---------------------------------------------------------------- debug log */

// Firmware log lines we've investigated and know to be harmless. Shown dimmed with an explanation,
// never hidden, and the stored log is untouched. Keep patterns narrow so new problems still stand out.
const KNOWN_BENIGN = [
  {
    source: "SerialConsole",
    re: /^Unknown module config type (14|15|16)$/,
    label: "known quirk",
    note: "Harmless firmware 2.7.26 quirk. On each client connect the radio walks every module-config type in "
      + "the protocol (up to 16), but this firmware only implements 13; types 14 (status message), 15 (traffic "
      + "management) and 16 (TAK) have no code, so it logs an error and moves on. Nothing is lost. "
      + "Source: src/mesh/PhoneAPI.cpp, STATE_SEND_MODULECONFIG.",
  },
];
const benignOf = (r) => KNOWN_BENIGN.find((k) => (!k.source || k.source === r.source) && k.re.test(r.message));

const LV_RANK = { TRACE: 0, UNSET: 1, TEXT: 1, DEBUG: 1, INFO: 2, WARN: 3, ERROR: 4, CRIT: 5 };
const DBG_MAX_LINES = 3000;
const D = { loaded: false, paused: false, pending: [], oldest: null, gen: 0, rows: 0 };

function dbgFilter() {
  const min = $("dbgLevel").value;
  return {
    q: $("dbgSearch").value.trim(),
    source: $("dbgSource").value,
    levels: min ? Object.keys(LV_RANK).filter((l) => LV_RANK[l] >= LV_RANK[min]) : [],
  };
}
function dbgMatches(r, f) {
  if (f.levels.length && !f.levels.includes(r.level)) return false;
  if (f.source && (r.source || "(none)") !== f.source) return false;
  return !f.q || r.message.toLowerCase().includes(f.q.toLowerCase());
}

// Firmware prints node numbers as 0x1234abcd; label the ones we know.
function dbgNodeIndex() {
  const m = new Map();
  for (const n of S.nodes.values()) m.set(n.id.slice(1), n);
  return m;
}
function dbgLine(r, idx, q, fresh) {
  let msg = esc(r.message);
  if (q) msg = msg.replace(new RegExp(esc(q).replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "gi"), (m) => `<mark>${m}</mark>`);
  msg = msg.replace(/0x([0-9a-f]{8})\b/gi, (m, hex) => {
    const n = idx.get(hex.toLowerCase());
    return n ? `<span title="${esc(n.longName)}">${m}</span><span class="nn">${esc(n.shortName)}</span>` : m;
  });
  const known = benignOf(r);
  const t = new Date(r.ts * 1000);
  const time = `${t.toLocaleTimeString([], { hour12: false })}<span class="ms">.${String(t.getMilliseconds()).padStart(3, "0")}</span>`;
  if (known) {
    return `<div class="dl benign${fresh ? " fresh" : ""}" data-ts="${r.ts}" title="${esc(known.note)}"><span class="t">${time}</span><span class="lv">${esc(r.level)}</span><span class="src" data-src="${esc(r.source || "(none)")}">${esc(r.source || "")}</span><span class="m">${msg}<span class="kb">ⓘ ${esc(known.label)}</span></span></div>`;
  }
  return `<div class="dl l-${esc(r.level)}${fresh ? " fresh" : ""}" data-ts="${r.ts}"><span class="t">${time}</span><span class="lv">${esc(r.level)}</span><span class="src" data-src="${esc(r.source || "(none)")}">${esc(r.source || "")}</span><span class="m">${msg}</span></div>`;
}

function dbgStatus(extra = "") {
  const f = dbgFilter();
  const what = [f.q && `matching “${f.q}”`, f.source && `from ${f.source}`, f.levels.length && `${$("dbgLevel").selectedOptions[0].text.toLowerCase()}`].filter(Boolean).join(", ");
  const pending = D.paused && D.pending.length ? `<span class="new" id="dbgResume">${D.pending.length} new — resume</span>` : "";
  $("dbgStatus").innerHTML = `<span>${D.rows.toLocaleString()} lines${what ? ` ${what}` : ""}${extra}</span>${pending || (D.paused ? "<span>Paused</span>" : "<span>Live</span>")}`;
}

async function dbgLoad(older = false) {
  const gen = ++D.gen;
  const f = dbgFilter();
  const qs = new URLSearchParams({ limit: older ? 500 : 1000 });
  if (f.q) qs.set("q", f.q);
  if (f.source) qs.set("source", f.source);
  if (f.levels.length) qs.set("levels", f.levels.join(","));
  if (older && D.oldest) qs.set("before", D.oldest);
  const rows = (await fetch(`/api/debuglog?${qs}`).then((r) => r.json())).reverse();
  if (gen !== D.gen) return; // a newer filter change superseded this load
  const box = $("dbgLines"), idx = dbgNodeIndex();
  const html = rows.map((r) => dbgLine(r, idx, f.q)).join("");
  const more = rows.length >= (older ? 500 : 1000) ? '<button class="btn older" id="dbgOlder">Load older</button>' : '<div class="emptystate" style="padding:8px">Start of the 7-day log</div>';
  if (older) {
    const h = box.scrollHeight;
    box.querySelector(".older, .emptystate")?.remove();
    box.insertAdjacentHTML("afterbegin", more + html);
    box.scrollTop += box.scrollHeight - h; // keep the reader's place
    D.rows += rows.length;
  } else {
    box.innerHTML = rows.length ? more + html : '<div class="emptystate">No log lines match.</div>';
    D.rows = rows.length;
    box.scrollTop = box.scrollHeight;
  }
  if (rows.length) D.oldest = rows[0].ts;
  dbgStatus();
}

async function openDebug() {
  if (!D.loaded) {
    D.loaded = true;
    const sources = await fetch("/api/debuglog/sources").then((r) => r.json());
    $("dbgSource").insertAdjacentHTML("beforeend", sources.map((s) => `<option>${esc(s)}</option>`).join(""));
    await dbgLoad();
  } else {
    const box = $("dbgLines"); box.scrollTop = box.scrollHeight;
  }
}

function dbgAppend(rows, fresh) {
  const f = dbgFilter(), idx = dbgNodeIndex(), box = $("dbgLines");
  const keep = rows.filter((r) => dbgMatches(r, f));
  if (!keep.length) return;
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 60;
  box.querySelector(".emptystate:last-child")?.remove();
  box.insertAdjacentHTML("beforeend", keep.map((r) => dbgLine(r, idx, f.q, fresh)).join(""));
  D.rows += keep.length;
  const lines = box.querySelectorAll(".dl");
  if (lines.length > DBG_MAX_LINES) { // cap the DOM; older lines are a "Load older" away
    for (let i = 0; i < lines.length - DBG_MAX_LINES; i++) lines[i].remove();
    D.rows = DBG_MAX_LINES;
    const first = box.querySelector(".dl");
    D.oldest = first ? Number(first.dataset.ts) : D.oldest;
    if (first && !box.querySelector(".older")) box.insertAdjacentHTML("afterbegin", '<button class="btn older" id="dbgOlder">Load older</button>');
  }
  if (atBottom) box.scrollTop = box.scrollHeight;
  dbgStatus();
}

function onDebugBatch(rows) {
  if (!D.loaded) return; // the tab loads history on first open
  if (D.paused) { D.pending.push(...rows); dbgStatus(); return; }
  dbgAppend(rows, true);
}

function dbgResume() {
  D.paused = false;
  $("dbgPause").textContent = "Pause"; $("dbgPause").classList.remove("on");
  const rows = D.pending; D.pending = [];
  dbgAppend(rows, false);
  const box = $("dbgLines"); box.scrollTop = box.scrollHeight;
  dbgStatus();
}

let dbgSearchTimer = null;
$("dbgSearch").addEventListener("input", () => { clearTimeout(dbgSearchTimer); dbgSearchTimer = setTimeout(() => { D.pending = []; dbgLoad(); }, 300); });
$("dbgLevel").addEventListener("change", () => { D.pending = []; dbgLoad(); });
$("dbgSource").addEventListener("change", () => { D.pending = []; dbgLoad(); });
$("dbgPause").addEventListener("click", () => {
  if (D.paused) return dbgResume();
  D.paused = true;
  $("dbgPause").textContent = "Resume"; $("dbgPause").classList.add("on");
  dbgStatus();
});
$("panel-debug").addEventListener("click", (e) => {
  if (e.target.id === "dbgOlder") return dbgLoad(true);
  if (e.target.id === "dbgResume") return dbgResume();
  const src = e.target.closest(".src[data-src]");
  if (src && src.dataset.src) { $("dbgSource").value = src.dataset.src; D.pending = []; dbgLoad(); }
});

/* ---------------------------------------------------------------- storage + backups */

const ST = { s: null };
function fmtBytes(b) {
  if (b == null) return "—";
  if (b < 1024 * 1024) return `${Math.max(1, Math.round(b / 1024))} KB`;
  if (b < 1024 ** 3) return `${(b / 1024 ** 2).toFixed(b < 10 * 1024 ** 2 ? 1 : 0)} MB`;
  return `${(b / 1024 ** 3).toFixed(2)} GB`;
}
const destName = (p) => (/My Drive/i.test(p) ? "Google Drive" : /OneDrive/i.test(p) ? "OneDrive" : p);

function backupProblem(s) {
  const lb = s.lastBackup;
  if (s.backupRunning) return null;
  if (lb && !lb.ok) return lb.partial ? "partial" : "failed";
  if (s.lastOkTs && s.backupStale) return "stale";
  return null;
}

function renderStorage() {
  const s = ST.s; if (!s) return;
  const prob = backupProblem(s);
  const chip = $("store");
  chip.hidden = false;
  chip.className = `store ${s.overLimit || prob === "failed" || prob === "stale" ? "bad" : prob ? "warn" : ""}`;
  const bk = s.backupRunning ? "backing up…" : s.lastOkTs ? `backed up ${ago(s.lastOkTs)}` : "no backup yet";
  chip.textContent = `⛁ ${fmtBytes(s.dbBytes)} · ${bk}`;
  chip.title = "Log database size and backup status";

  // banner: the size limit first (it needs the user's decision), then backup trouble
  const b = $("banner");
  let html = null, cls = "", key = null;
  if (s.overLimit) {
    key = "size";
    html = `<span class="ico">⚠️</span><div class="txt"><b>The mesh log database has reached ${fmtBytes(s.dbBytes)}</b>, past the ${fmtBytes(s.warnBytes)} limit you set.
      Nothing has been deleted and logging continues normally. Please decide how to handle it: archive or export older data, move the database to a larger drive,
      or raise the limit (<code>MESHDASH_DB_WARN_GB</code>). Claude can help with any of these. The file is at <code>${esc(s.dbPath)}</code>.</div>
      <button class="btn" data-dismiss="size">Remind me in a week</button>`;
  } else if (prob === "failed" || prob === "partial" || prob === "stale") {
    key = `backup-${s.lastBackup?.ts}`; cls = "bad";
    const fails = (s.lastBackup?.dests || []).filter((d) => !d.ok).map((d) => `${destName(d.path)}: ${esc(d.error || "failed")}`);
    const why = s.lastBackup?.error ? esc(s.lastBackup.error) : fails.join("; ");
    html = `<span class="ico">⛔</span><div class="txt"><b>${prob === "partial" ? "The last backup only reached one destination" : prob === "stale" ? "No successful backup in over 48 hours" : "The last backup failed"}.</b>
      ${why ? `${why}.` : ""} Last good backup: ${s.lastOkTs ? ago(s.lastOkTs) : "never"}. It retries every hour.</div>
      <button class="btn" data-backup>Retry now</button>`;
  }
  let dismissed = null;
  try { dismissed = JSON.parse(localStorage.getItem("meshdash.dismiss") || "null"); } catch { /* storage blocked */ }
  const hide = !html || (dismissed && dismissed.key === key && Date.now() < dismissed.until);
  b.hidden = hide; b.className = `banner ${cls}`;
  if (!hide) b.innerHTML = html;
  if (!$("storepop").hidden) renderStorePop();
}

function renderStorePop() {
  const s = ST.s, lb = s.lastBackup;
  const pct = Math.min(100, (s.dbBytes / s.warnBytes) * 100);
  const dests = s.dests.map((p) => {
    const d = (lb?.dests || []).find((x) => x.path === p);
    const st = !d ? '<span class="muted">not yet</span>' : d.ok ? '<span class="ok">✓ ok</span>' : `<span class="err">✕ ${esc(d.error || "failed")}</span>`;
    return `<div class="dest"><b>${destName(p)}</b> ${st}<br><span class="muted">${esc(p)}</span></div>`;
  }).join("");
  $("storepop").innerHTML = `<h4>Log database</h4>
    <div>${fmtBytes(s.dbBytes)} of the ${fmtBytes(s.warnBytes)} warning limit</div>
    <div class="meter"><i class="${s.overLimit ? "over" : ""}" style="width:${pct.toFixed(1)}%"></i></div>
    <div class="muted">${esc(s.dbPath)} · kept forever, nothing is auto-deleted. Debug log (7-day): ${fmtBytes(s.debugBytes)}</div>
    <h4 style="margin-top:14px">Backups</h4>
    <dl class="kv">
      <dt>Last good</dt><dd>${s.lastOkTs ? `${new Date(s.lastOkTs * 1000).toLocaleString()} (${ago(s.lastOkTs)})` : "none yet"}</dd>
      ${lb ? `<dt>Last run</dt><dd>${lb.ok ? "ok" : lb.partial ? "partial" : "failed"}${lb.gzBytes ? ` · ${fmtBytes(lb.gzBytes)} compressed` : ""}${lb.seconds != null ? ` · ${lb.seconds} s` : ""}</dd>` : ""}
      <dt>Next</dt><dd>${new Date(s.nextBackupTs * 1000).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" })}</dd>
    </dl>
    ${dests}
    <div class="row"><span class="muted">Daily, compressed and integrity-checked. Keeps 7 daily, 5 weekly, 12 monthly.</span>
    <button class="btn" data-backup ${s.backupRunning ? "disabled" : ""}>${s.backupRunning ? "Backing up…" : "Back up now"}</button></div>`;
}

$("store").addEventListener("click", (e) => {
  e.stopPropagation();
  const pop = $("storepop"), open = pop.hidden;
  pop.hidden = !open; $("store").setAttribute("aria-expanded", String(open));
  if (open) renderStorePop();
});
document.addEventListener("click", (e) => {
  if (!$("storepop").hidden && !e.target.closest("#storepop")) { $("storepop").hidden = true; $("store").setAttribute("aria-expanded", "false"); }
});
document.addEventListener("click", async (e) => {
  const d = e.target.closest("[data-dismiss]");
  if (d) {
    try { localStorage.setItem("meshdash.dismiss", JSON.stringify({ key: d.dataset.dismiss, until: Date.now() + 7 * 86400e3 })); } catch { /* storage blocked */ }
    $("banner").hidden = true;
  }
  const bk = e.target.closest("[data-backup]");
  if (bk) {
    bk.disabled = true; bk.textContent = "Backing up…";
    const r = await postJson("/api/backup", {});
    if (r.error) { bk.textContent = r.error; }
  }
});

async function loadStorage() {
  try { ST.s = await fetch("/api/storage").then((r) => r.json()); renderStorage(); } catch { /* server restarting */ }
}

/* ---------------------------------------------------------------- traceroute */

async function runTraceroute(id) {
  $("dErr").textContent = "";
  $("dTrace").disabled = true;
  const r = await postJson("/api/traceroute", { to: id });
  if (r.error) { $("dErr").textContent = r.error; $("dTrace").disabled = false; return; }
  const tr = { ...r, forward: null, back: null };
  S.traces.set(id, tr);
  renderRoute(tr);
}

function hopList(path) {
  return `<ol>${path.map((h, i) => `<li class="${h.id ? "" : "unk"}"><span class="hn" data-id="${esc(h.id || "")}">${esc(h.id ? h.name : "Unknown relay")}</span>${i ? `<span class="snr">${h.snr != null ? `${h.snr.toFixed(2)} dB` : "? dB"}</span>` : ""}</li>`).join("")}</ol>`;
}

function renderRoute(tr) {
  const el = $("dRoute"); if (!el) return;
  routeLayer.clearLayers();
  const btn = $("dTrace");
  if (tr.status === "pending") {
    el.innerHTML = `<div class="pending">Tracing… waiting for a reply (up to ${30 * (tr.hopLimit || 3)} s)</div>`;
    if (btn) btn.disabled = true;
    return;
  }
  if (btn) btn.disabled = false;
  const when = `<div class="when">${ago(tr.done_ts || tr.ts)}</div>`;
  if (tr.status !== "ok") {
    el.innerHTML = `<div class="err">${tr.status === "timed out" ? "No reply — the node may be asleep, out of range, or too many hops away." : esc(tr.status)}</div>${when}`;
    return;
  }
  const legs = (path) => { const r = path.length - 2; return r === 0 ? "direct" : `via ${r} relay${r === 1 ? "" : "s"}`; };
  const out = legs(tr.forward), back = tr.back ? legs(tr.back) : null;
  const summary = !back || back === out ? `${out[0].toUpperCase()}${out.slice(1)} both ways` : `Out ${out}, reply ${back}`;
  el.innerHTML = `<div>${tr.back ? summary : `${out[0].toUpperCase()}${out.slice(1)}`}</div>${when}
    <div class="dir">Towards ${esc(tr.forward.at(-1).name)} · SNR measured by each receiving hop</div>${hopList(tr.forward)}
    ${tr.back ? `<div class="dir">Reply path back</div>${hopList(tr.back)}` : ""}`;
  el.querySelectorAll(".hn[data-id]").forEach((s) => s.dataset.id && (s.onclick = () => selectNode(s.dataset.id, true)));
  const pts = tr.forward.map((h) => posOf(S.nodes.get(h.id))).filter(Boolean);
  const color = getComputedStyle(document.documentElement).getPropertyValue("--accent").trim();
  if (pts.length > 1) {
    L.polyline(pts, { color, weight: 4, opacity: .85, interactive: false }).addTo(routeLayer);
    map.fitBounds(pts, { padding: [80, 80], maxZoom: 13 });
  }
}

function setTab(t) {
  S.tab = t;
  for (const b of document.querySelectorAll(".tabs button")) b.classList.toggle("on", b.dataset.tab === t);
  for (const p of ["nodes", "traffic", "messages", "detail", "debug"]) $(`panel-${p}`).hidden = p !== t;
  const wide = t === "debug";
  if (document.body.classList.contains("wide") !== wide) {
    document.body.classList.toggle("wide", wide);
    requestAnimationFrame(() => map.invalidateSize());
  }
  if (wide) openDebug();
  if (t !== "detail") routeLayer.clearLayers();
  if (t === "messages") { S.unread.delete(S.conv); if (S.conv === BROADCAST) S.chanUnread.delete(S.chan); renderMessages(); $("msgText").focus(); }
}
document.querySelector(".tabs").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) { S.selected = null; renderMarkers(); setTab(b.dataset.tab); } });

async function selectNode(id, pan) {
  S.selected = id; renderMarkers(); setTab("detail");
  const n = S.nodes.get(id), p = posOf(n);
  if (pan && p) map.panTo(p);
  await renderDetail(id);
}

async function renderDetail(id) {
  const res = await fetch(`/api/node/${encodeURIComponent(id)}?hours=72`).then((r) => r.json());
  const n = res.node || S.nodes.get(id);
  if (!n || S.selected !== id) return;
  const el = $("panel-detail");
  const prec = precisionMeters(n.precisionBits);
  el.innerHTML = `<button class="back" id="back">← All nodes</button>
    <h2>${esc(n.longName)} ${n.isBase ? '<span class="tag base">base</span>' : ""}${keyBadge(n.keyFlag)} ${v28Tag(n.v28)}</h2>
    <div class="hw">${esc([n.shortName, prettyHw(n.hwModel), prettyRole(n.role)].filter(Boolean).join(" · "))} · <span class="num">${esc(n.id)}</span></div>
    ${n.keyFlag ? `<p class="kf-note">⚠ ${esc(keyFlagText(n.keyFlag))}${n.keyFlag.with.length ? ` Same key: ${n.keyFlag.with.map((o, i) => esc(n.keyFlag.withNames?.[i] || S.nodes.get(o)?.longName || o)).join(", ")}.` : ""}</p>` : ""}
    ${n.isLocal ? "" : `<div class="actions"><button class="btn" id="dMsg">Message</button><button class="btn" id="dTrace">Traceroute</button><button class="star ${window.meshWatch?.isWatched(n.id) ? "on" : ""}" id="dStar" title="Watch: alert when it goes quiet or its battery runs low">${window.meshWatch?.isWatched(n.id) ? "★" : "☆"}</button><a class="btn" href="/analytics.html#node=${encodeURIComponent(n.id)}" style="text-decoration:none;color:inherit">Analytics</a><span class="err" id="dErr"></span></div>`}
    <div class="tiles">${tile("Battery", n.battery == null ? "—" : n.battery > 100 ? "Ext" : fmt(n.battery, 0), n.battery > 100 ? "" : "%")}${tile("Voltage", fmt(n.voltage, 2), "V")}${tile("Ch. util", fmt(n.chUtil, 1), "%")}</div>
    <dl class="kv">
      <dt>Last heard</dt><dd>${n.isLocal ? "connected by USB" : ago(n.lastHeard)}</dd>
      ${n.positionFromStation ? '<dt>Position</dt><dd>listening station\'s configured antenna location</dd>' : ""}
      <dt>Path</dt><dd>${n.isLocal ? "—" : n.hopsAway === 0 ? "direct" : n.hopsAway != null ? `${n.hopsAway} hop${n.hopsAway === 1 ? "" : "s"}` : "unknown"}</dd>
      ${n.remote ? `<dt>${n.heardHere ? "Also heard by" : "Heard only by"}</dt><dd>${esc(n.remote.stationName)}: ${ago(n.remote.ts)}${n.remote.hops === 0 ? ", direct" : n.remote.hops != null ? `, ${n.remote.hops} hop${n.remote.hops === 1 ? "" : "s"}` : ""}${n.remote.snr != null ? ` · SNR ${fmt(n.remote.snr, 2)} dB` : ""}${n.remote.rssi != null ? ` · RSSI ${n.remote.rssi} dBm` : ""}</dd>` : ""}
      <dt>${n.hopsAway ? "Last-hop signal" : "Signal"}</dt><dd>${n.snr != null ? `SNR ${fmt(n.snr, 2)} dB` : "—"}${n.rssi != null ? ` · RSSI ${n.rssi} dBm` : ""}</dd>
      <dt>Position</dt><dd>${n.lat == null && S.est.get(n.id) ? `estimated ${prov("inferred")} ${S.est.get(n.id).lat.toFixed(4)}, ${S.est.get(n.id).lon.toFixed(4)} ±${S.est.get(n.id).radiusKm.toFixed(1)} km<br><span style="color:var(--text-muted)">${esc(S.est.get(n.id).method)}; never reported its own</span>` : n.lat != null ? `${n.lat.toFixed(5)}, ${n.lon.toFixed(5)}${n.alt != null ? ` · ${n.alt} m` : ""}${prec ? `<br><span style="color:var(--text-muted)">±${prec >= 1000 ? (prec / 2000).toFixed(1) + " km" : Math.round(prec / 2) + " m"} (shared at reduced precision)</span>` : ""}` : "not shared"}</dd>
      <dt>Airtime TX</dt><dd>${fmt(n.airUtil, 2, "%")}</dd>
      <dt>Uptime</dt><dd>${n.uptime != null ? `${(n.uptime / 3600).toFixed(1)} h` : "—"}</dd>
      ${n.temperature != null ? `<dt>Temperature</dt><dd>${fmt(n.temperature, 1, " °C")}</dd>` : ""}
      <dt>Packets</dt><dd>${n.rxCount} heard this session</dd>
    </dl>
    ${n.isLocal ? "" : '<h3>Route</h3><div class="route" id="dRoute"><span class="when">No traceroute yet. It sends one small packet and shows the path both ways.</span></div>'}
    <h3>Last 72 hours</h3>
    <div class="chart" id="dVolt"></div><div class="chart" id="dBatt"></div><div class="chart" id="dUtil"></div>
    <h3>Recent packets</h3><ul class="feed" id="dFeed"></ul>`;
  $("back").onclick = () => { S.selected = null; renderMarkers(); setTab("nodes"); };
  if (!n.isLocal) {
    $("dMsg").onclick = () => openConversation(id);
    $("dTrace").onclick = () => runTraceroute(id);
    $("dStar").onclick = async (ev) => { const on = await window.meshWatch.toggle(id); ev.target.classList.toggle("on", on); ev.target.textContent = on ? "★" : "☆"; };
    const tr = S.traces.get(id) || (await fetch(`/api/traceroutes?node=${encodeURIComponent(id)}`).then((r) => r.json()))[0];
    if (tr && S.selected === id) { S.traces.set(id, tr); renderRoute(tr); }
  }
  lineChart($("dVolt"), res.telemetry, "voltage", { title: "Voltage (V)", unit: " V", digits: 2 });
  lineChart($("dBatt"), res.telemetry, "battery", { title: "Battery (%)", unit: "%", digits: 0, yMin: 0, yMax: 100 });
  lineChart($("dUtil"), res.telemetry, "ch_util", { title: "Channel utilization (%)", unit: "%", digits: 1, yMin: 0 });
  $("dFeed").innerHTML = res.packets.length ? res.packets.map((p) => feedItem(p)).join("") : '<li class="emptystate">Nothing heard from this node since the dashboard started</li>';
}

/* ---------------------------------------------------------------- base station card */

let baseTimer = 0;
async function renderBase() {
  const base = [...S.nodes.values()].find((n) => n.isBase);
  if (!base) { $("basecard").hidden = true; return; }
  $("basecard").hidden = false;
  $("bcName").textContent = base.longName;
  $("bcTiles").innerHTML = tile("Battery", base.battery == null ? "—" : base.battery > 100 ? "Ext" : fmt(base.battery, 0), base.battery > 100 ? "" : "%") +
    tile("Voltage", fmt(base.voltage, 2), "V") + tile(base.relayOnly ? "Last relay" : "Last heard", base.lastHeard ? ago(base.lastHeard).replace(" ago", "") : "—");
  if (now() - baseTimer < 30) return; // chart history refresh at most every 30 s
  baseTimer = now();
  const res = await fetch(`/api/node/${encodeURIComponent(base.id)}?hours=72`).then((r) => r.json());
  lineChart($("bcChart"), res.telemetry, "voltage", { title: "Battery voltage, last 72 h", unit: " V", digits: 2, height: 80,
    empty: base.relayOnly ? "Relaying traffic — waiting for its first telemetry report" : undefined });
}
$("bcOpen").onclick = () => { const b = [...S.nodes.values()].find((n) => n.isBase); if (b) selectNode(b.id, true); };

/* ---------------------------------------------------------------- data flow */

function renderAll() { renderStats(); renderConn(); renderList(); renderMarkers(); renderBase(); updateCount(); }

async function load() {
  const st = await fetch("/api/state").then((r) => r.json());
  S.status = st.status;
  S.nodes = new Map(st.nodes.map((n) => [n.id, n]));
  S.links = await fetch("/api/links?hours=24").then((r) => r.json());
  const pk = await fetch("/api/packets?limit=200").then((r) => r.json());
  $("feed").innerHTML = pk.length ? pk.map((p) => feedItem(p)).join("") : '<li class="emptystate">Waiting for packets…</li>';
  S.msgs = (await fetch("/api/messages").then((r) => r.json())).reverse();
  loadChannels();
  renderMessages();
  renderAll();
}

function connectEvents() {
  const es = { addEventListener: (type, fn) => lkEvents.on(type, fn) };  // the page's shared stream (prov.js)
  lkEvents.onReopen(load);  // back from a hidden tab: catch up on what arrived meanwhile
  es.addEventListener("status", (e) => { const was = S.status.connected; S.status = JSON.parse(e.data); renderConn(); updateCount(); if (!was && S.status.connected) load(); });
  es.addEventListener("node", (e) => {
    const n = JSON.parse(e.data); S.nodes.set(n.id, n);
    scheduleRender();
  });
  es.addEventListener("packet", (e) => {
    const p = JSON.parse(e.data);
    const feed = $("feed");
    feed.querySelector(".emptystate")?.remove();
    // already in the feed from another station: add this station to it rather than listing it twice
    const key = p.pkt_id != null ? `${p.from_id}:${p.pkt_id}` : null;
    const same = key && feed.querySelector(`li[data-key="${CSS.escape(key)}"]`);
    if (same) {
      if (p.station && !same.querySelector(`.st[data-st="${CSS.escape(p.station)}"]`) && p.station !== S.status?.localId) {
        same.querySelector(".who").insertAdjacentHTML("beforeend", stationBadges({ stations: `${S.status?.localId},${p.station}` }));
      }
      same.classList.remove("flash"); void same.offsetWidth; same.classList.add("flash");
      return;
    }
    feed.insertAdjacentHTML("afterbegin", feedItem(p, true));
    while (feed.children.length > 300) feed.lastElementChild.remove();
    if (p.hops === 0 || ["NEIGHBORINFO_APP", "TRACEROUTE_APP"].includes(p.portnum)) refreshLinks();
    setTimeout(() => ping(p.from_id), 50);
    if (S.selected === p.from_id) renderDetail(p.from_id);
  });
  es.addEventListener("message", (e) => {
    const m = JSON.parse(e.data);
    S.msgs.push(m);
    const k = convKey(m);
    if (!m.outgoing && !(S.tab === "messages" && S.conv === k && (k !== BROADCAST || msgChan(m) === S.chan))) {
      S.unread.set(k, (S.unread.get(k) || 0) + 1);
      if (k === BROADCAST) S.chanUnread.set(msgChan(m), (S.chanUnread.get(msgChan(m)) || 0) + 1);
    }
    renderMessages();
  });
  es.addEventListener("msgstatus", (e) => {
    const s = JSON.parse(e.data);
    const m = S.msgs.find((x) => x.outgoing && x.pkt_id === s.pkt_id);
    if (m) { m.status = s.status; renderMessages(); }
  });
  es.addEventListener("debug", (e) => onDebugBatch(JSON.parse(e.data)));
  es.addEventListener("storage", (e) => { ST.s = JSON.parse(e.data); renderStorage(); });
  es.addEventListener("traceroute", (e) => {
    const tr = JSON.parse(e.data);
    S.traces.set(tr.target, tr);
    if (S.selected === tr.target) renderRoute(tr);
    refreshLinks();
  });
  lkEvents.onError(() => { S.status = { ...S.status, connected: false, serverDown: true }; renderConn(); updateCount(); });
}

let renderPending = false;
function scheduleRender() {
  if (renderPending) return;
  renderPending = true;
  requestAnimationFrame(() => { renderPending = false; renderAll(); });
}
let linkTimer = null;
function refreshLinks() {
  clearTimeout(linkTimer);
  linkTimer = setTimeout(async () => { S.links = await fetch("/api/links?hours=24").then((r) => r.json()); renderLinks(); }, 1000);
}

setInterval(() => { renderStats(); renderList(); renderMarkers(); renderBase(); }, 30000); // age-based colors drift over time
load().then(connectEvents);
loadStorage();
setInterval(renderStorage, 60000); // "backed up 3h ago" drifts
