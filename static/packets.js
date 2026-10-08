"use strict";
/* Packet browser: filtered, paged view over packets / rx_hops / tx_log, with CSV export. */
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const PAGE = 200;
const P = { source: "packets", offset: 0, facets: null, timer: null, data: null };
const NOTES = {
  packets: "Packets the radio decoded and handed to the dashboard: the first copy of each, mostly broadcasts and traffic to us. Click a row for its anatomy: the bytes on air, the encryption and every decoded field.",
  rx_hops: "Every over-the-air reception, duplicates included, from the radio's debug log: full header (incl. recipient) and our signal reading, no contents.",
  tx_log: "Every transmission by our own radio, from its debug log.",
};
const LABEL = { ts: "Time", from_id: "From", to_id: "To", portnum: "Type", channel: "Ch", hops: "Hops", hop_start: "Hop start",
  hop_limit: "Hop lim", snr: "SNR", rssi: "RSSI", relay: "Relay", next_hop: "Next hop", via_mqtt: "MQTT", pki: "PKI",
  pkt_id: "Packet ID", summary: "Summary", directed: "Addressed", want_ack: "Want ACK", length: "Len", encrypted: "Enc",
  transport: "Transport", priority: "Prio", station: "Station" };
const hex = (v, w = 2) => (v == null ? "" : `0x${Number(v).toString(16).padStart(w, "0")}`);

function filters() {
  const f = Object.fromEntries(new FormData($("filters")).entries());
  const out = {};
  for (const [k, v] of Object.entries(f)) if (v !== "") out[k] = v;
  if (out.range) { out.since = String(Date.now() / 1000 - Number(out.range)); }
  delete out.range;
  return out;
}

async function load() {
  const qs = new URLSearchParams({ source: P.source, limit: PAGE, offset: P.offset, ...filters(), ...(window.LK_STATION ? { station: window.LK_STATION } : {}) });
  const r = await fetch(`/api/packets/search?${qs}`).then((x) => x.json());
  if (r.error) { $("count").textContent = r.error; return; }
  P.data = r;
  if (!P.facets || P.facets.source !== P.source) {
    P.facets = { source: P.source, ...r.facets };
    const port = $("filters").portnum, ch = $("filters").channel;
    port.innerHTML = '<option value="">Any</option>' + (r.facets.portnum || []).map((p) => `<option>${esc(p)}</option>`).join("");
    ch.innerHTML = '<option value="">Any</option>' + (r.facets.channel || []).map((c) => `<option value="${c}">${P.source === "packets" ? c : hex(c)}</option>`).join("");
  }
  render();
  $("csv").href = `/api/packets/search.csv?${new URLSearchParams({ source: P.source, ...filters(), ...(window.LK_STATION ? { station: window.LK_STATION } : {}) })}`;
}

function cell(col, row) {
  const v = row[col];
  if (col === "ts") {
    const t = new Date(v * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });
    if (row.arrival === "local") return `${t}<span class="arr" title="The radio reported this to the dashboard itself; it never went over the air (no transport, no signal reading).">local · not over the air</span>`;
    if (row.arrival === "unknown") return `${t}<span class="arr unk" title="Logged before full packets were stored, so how it arrived wasn't recorded.">arrival unknown</span>`;
    if (row.arrival && row.arrival !== "lora") return `${t}<span class="arr">via ${esc(row.arrival.toUpperCase())}</span>`;
    return t;
  }
  if (col === "from_id" || col === "to_id") {
    const name = row[col.replace("_id", "_name")];
    const kf = col === "from_id" ? keyBadge(row.from_keyflag, { short: true }) : "";
    return v === "^all" ? "broadcast" : name && name !== v ? `${esc(name)}${kf} <span class="muted">${esc(v)}</span>` : esc(v) + kf;
  }
  if (col === "relay" || col === "next_hop") return hex(v);
  if (col === "channel") return P.source === "packets" ? (v ?? "") : hex(v);
  if (col === "pkt_id") return v == null ? "" : hex(v, 8);
  if (col === "snr") return v == null ? "" : Number(v).toFixed(2);
  return esc(v);
}

function render() {
  const r = P.data, cols = r.columns;
  $("ptable").innerHTML = `<thead><tr>${cols.map((c) => `<th>${esc(LABEL[c] || c)}</th>`).join("")}</tr></thead><tbody>${r.rows.map((row) =>
    `<tr class="${row.has_raw ? "has-raw" : ""} ${row.arrival === "local" ? "local" : ""}" data-rowid="${row.rowid}">${cols.map((c) => `<td class="${["pkt_id", "relay", "next_hop", "channel"].includes(c) ? "mono" : ""}">${cell(c, row)}</td>`).join("")}</tr>`).join("")
    || `<tr><td colspan="${cols.length}" class="muted">No rows match.</td></tr>`}</tbody>`;
  const from = r.total ? P.offset + 1 : 0, to = Math.min(P.offset + PAGE, r.total);
  $("count").textContent = `${from.toLocaleString()}–${to.toLocaleString()} of ${r.total.toLocaleString()} rows · ${r.ms} ms`;
  $("prev").disabled = P.offset === 0; $("next").disabled = to >= r.total;
}

function setSource(s) {
  P.source = s; P.offset = 0; P.facets = null;
  for (const b of $("source").children) b.classList.toggle("on", b.dataset.s === s);
  for (const el of document.querySelectorAll("[data-only]")) el.hidden = el.dataset.only !== s;
  for (const el of document.querySelectorAll("[data-not]")) el.hidden = el.dataset.not === s;
  $("srcnote").textContent = NOTES[s];
  load();
}

$("source").addEventListener("click", (e) => { const b = e.target.closest("button[data-s]"); if (b) setSource(b.dataset.s); });
$("filters").addEventListener("input", () => { clearTimeout(P.timer); P.timer = setTimeout(() => { P.offset = 0; load(); }, 300); });
$("filters").addEventListener("submit", (e) => e.preventDefault());
$("prev").addEventListener("click", () => { P.offset = Math.max(0, P.offset - PAGE); load(); });
$("next").addEventListener("click", () => { P.offset += PAGE; load(); });
// Clicking a logged packet opens its anatomy (anatomy.js) under the row; the logged JSON is inside it.
$("ptable").addEventListener("click", (e) => {
  const tr = e.target.closest("tr.has-raw"); if (!tr || P.source !== "packets") return;
  const next = tr.nextElementSibling;
  if (next && next.classList.contains("anat-row")) { next.remove(); return; }
  tr.insertAdjacentHTML("afterend", `<tr class="anat-row"><td colspan="${P.data.columns.length}"><div></div></td></tr>`);
  openAnatomy(tr.nextElementSibling.querySelector("div"), tr.dataset.rowid);
});

// #packet=<rowid> (linked from the map's traffic feed and the Visualizations log) opens one packet on top
function routePacket() {
  const m = location.hash.match(/packet=(\d+)/);
  $("anatPanel").hidden = !m;
  if (!m) return;
  $("anatTitle").textContent = `Packet anatomy · row ${m[1]}`;
  openAnatomy($("anatBody"), m[1]);
  scrollTo(0, 0);
}
$("anatClose").addEventListener("click", (e) => { e.preventDefault(); history.replaceState(null, "", location.pathname); routePacket(); });
addEventListener("hashchange", routePacket);
routePacket();

setSource("packets");
