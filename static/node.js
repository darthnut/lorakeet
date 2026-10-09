"use strict";
/* Per-node view for the Analytics page (#node=<id>). Reuses analytics.js helpers and charts. */

const NV = { data: null, map: null, id: null };

async function openNode(id, range) {
  NV.id = id;
  $("nodeView").innerHTML = `<p class="scope"><a href="#" class="back">← All nodes</a></p><p class="muted">Loading ${esc(id)}…</p>`;
  try {
    const r = await fetch(`/api/analytics/node?id=${encodeURIComponent(id)}&range=${encodeURIComponent(range)}${stationQS()}`);
    const d = await r.json();
    if (NV.id !== id) return; // user moved on
    if (d.error) throw new Error(d.error);
    NV.data = d;
    renderNode();
  } catch (e) {
    $("nodeView").innerHTML = `<p class="scope"><a href="#" class="back">← All nodes</a></p><div class="card"><h2>Couldn't load ${esc(id)}</h2><p class="sub">${esc(e.message)}</p></div>`;
  }
}

const dur = (s) => (s == null ? "—" : s < 3600 ? `${Math.round(s / 60)} min` : s < 86400 ? `${(s / 3600).toFixed(s < 36000 ? 1 : 0)} h` : `${(s / 86400).toFixed(1)} days`);
const nlink = (id, name) => `<a class="nlink" href="${nodeHref(id)}">${esc(name || id)}</a>`;

function colChart(el, rows, { unit = "", digits = 1, label }) {
  const W = el.clientWidth || 500, H = 150, pl = 34, pr = 6, pt = 8, pb = 20;
  const vals = rows.map((r) => r.value);
  if (!vals.some((v) => v)) { el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" height="${H}"><text class="empty" x="${W / 2}" y="${H / 2}" text-anchor="middle">Nothing heard in this range</text></svg>`; return; }
  const max = niceMax(Math.max(...vals.map((v) => v || 0))), n = rows.length, bw = (W - pl - pr) / n;
  const y = (v) => pt + (1 - v / max) * (H - pt - pb);
  const bars = rows.map((r, i) => {
    const x = pl + i * bw + bw * 0.15, w = bw * 0.7;
    if (r.value == null) return `<rect x="${pl + i * bw}" y="${pt}" width="${bw}" height="${H - pt - pb}" fill="url(#hatch-${el.id})"/>`;
    const top = y(r.value), h = Math.max(0, y(0) - top);
    // 4px rounded data end, anchored flat on the baseline
    return h < 1 ? "" : `<path d="M${x},${y(0)}V${top + Math.min(4, h)}Q${x},${top} ${x + Math.min(4, w / 2)},${top}H${x + w - Math.min(4, w / 2)}Q${x + w},${top} ${x + w},${top + Math.min(4, h)}V${y(0)}Z" fill="var(--s1)"/>`;
  }).join("");
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" height="${H}" role="img" aria-label="${esc(label)}">
    <defs><pattern id="hatch-${el.id}" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><rect width="3" height="6" fill="${css("--gap-hatch")}"/></pattern></defs>
    ${[0, max / 2, max].map((v, i) => `<line class="${i ? "grid" : "base"}" x1="${pl}" x2="${W - pr}" y1="${y(v)}" y2="${y(v)}"/><text class="axis" x="${pl - 6}" y="${y(v) + 3}" text-anchor="end">${fmt(v, max < 5 ? 1 : 0)}</text>`).join("")}
    ${bars}
    ${rows.map((r, i) => (i % 3 === 0 ? `<text class="axis" x="${pl + i * bw + bw / 2}" y="${H - 5}" text-anchor="middle">${esc(r.label)}</text>` : "")).join("")}
    <rect class="hot" id="${el.id}-hot" y="${pt}" width="${bw}" height="${H - pt - pb}" visibility="hidden"/>
    <rect x="${pl}" y="0" width="${W - pl - pr}" height="${H}" fill="transparent"/>
  </svg>`;
  const svg = el.querySelector("svg"), hot = svg.querySelector(`#${el.id}-hot`);
  svg.addEventListener("pointermove", (e) => {
    const r = svg.getBoundingClientRect(), i = Math.max(0, Math.min(n - 1, Math.floor((((e.clientX - r.left) / r.width) * W - pl) / bw)));
    hot.setAttribute("x", pl + i * bw); hot.setAttribute("visibility", "visible");
    showTip(e, rows[i].tip);
  });
  svg.addEventListener("pointerleave", () => { hot.setAttribute("visibility", "hidden"); hideTip(); });
}

function card(title, sub, body, extra = "") {
  return `<section class="card"><div class="card-head"><div><h2>${title}</h2>${sub ? `<p class="sub">${sub}</p>` : ""}</div>${extra}</div>${body}</section>`;
}

async function renderNode() {
  const d = NV.data; if (!d) return;
  const s = d.stats, est = s.coveredHours < 24;
  const hasTel = d.series.some((x) => x.battery != null || x.voltage != null);
  const hasSnr = d.series.some((x) => x.snr != null);
  const sub = [d.short, (d.hw || "").replace(/_/g, " ").toLowerCase(), (d.role || "").replace(/_/g, " ").toLowerCase(), d.id].filter(Boolean).join(" · ");
  const sil = s.longestSilence;
  const tile = (label, value, detail, pv) => `<div class="kpi"><div class="k">${label}${pv ? prov(pv) : ""}</div><div class="v">${value}</div><div class="d">${detail}</div></div>`;

  const telKinds = Object.entries(d.latestTelemetry || {});
  const telHtml = telKinds.length ? telKinds.map(([k, v]) => `<div class="telblock"><div class="muted">${esc(k.replace(/Metrics$/, " metrics").replace(/([a-z])([A-Z])/g, "$1 $2").toLowerCase())} · ${esc(ago(v.ts))}</div><dl class="kv">${Object.entries(v.data).filter(([, x]) => typeof x !== "object").map(([kk, x]) => `<dt>${esc(kk.replace(/([a-z])([A-Z])/g, "$1 $2").toLowerCase())}</dt><dd>${esc(typeof x === "number" ? fmt(x, Number.isInteger(x) ? 0 : 2) : x)}</dd>`).join("")}</dl></div>`).join("")
    : `<p class="muted">This node hasn't sent any telemetry we've heard${d.isBase ? ". For a solar base station that usually means device-telemetry broadcasts are turned off in its settings (Telemetry → device update interval)." : "."}</p>`;

  const identity = d.identity.length ? `<table class="nodes compact"><thead><tr><th>Since</th><th>Name</th><th>Hardware</th><th>Role</th><th>Key</th></tr></thead><tbody>${d.identity.map((r) => `<tr><td>${esc(when(r.ts))}</td><td>${esc(r.long_name)} <span class="muted">${esc(r.short_name)}</span></td><td>${esc((r.hw_model || "").replace(/_/g, " ").toLowerCase())}</td><td>${esc((r.role || "").replace(/_/g, " ").toLowerCase())}</td><td class="mono" title="${esc(r.public_key || "")}">${r.public_key ? esc(r.public_key.slice(0, 10)) + "…" : "—"}</td></tr>`).join("")}</tbody></table>${d.identity.length === 1 ? '<p class="muted">No changes since first seen.</p>' : ""}`
    : '<p class="muted">Never announced its identity while we were listening.</p>';

  const msgs = d.messages.length ? `<div class="transcript" style="max-height:340px;padding:0">${d.messages.map((m) => `<div class="msg ${m.outgoing ? "out" : ""} ${m.encrypted ? "enc" : ""}"><div class="h"><b>${esc(m.outgoing ? "You" : m.name)}</b> · ${esc(when(m.ts))} · ${m.direct ? "direct" : "channel"}</div><div class="b">${m.encrypted ? "🔒 couldn't decrypt" : esc(m.text)}</div></div>`).join("")}</div>`
    : '<p class="muted">No text messages to or from this node in range.</p>';
  const traces = d.traceroutes.length ? d.traceroutes.map((t) => `<div class="trace"><div class="muted">${esc(when(t.ts))} · ${esc(t.status)}</div>${t.forward ? `<div>Out: ${t.forward.map((h) => (h.id ? nlink(h.id, h.name) : "?")).join(" → ")}</div>` : ""}${t.back ? `<div>Back: ${t.back.map((h) => (h.id ? nlink(h.id, h.name) : "?")).join(" → ")}</div>` : ""}</div>`).join("")
    : '<p class="muted">No traceroutes yet. Run one from the map dashboard\'s node panel.</p>';

  const relayOnly = !s.packetsEver
    ? `<div class="card" style="border-color:var(--accent)"><b>We've never received a packet from this node itself.</b> It's known only from relaying other nodes' traffic to us and from traceroutes, so the activity charts are empty but its links below are real.</div>`
    : "";
  $("nodeView").innerHTML = `
    <p class="scope"><a href="#" class="back">← All nodes</a> · Showing ${esc(when(d.since))} – now · ${s.coveredHours} h logged</p>
    ${relayOnly}
    ${d.keyFlag ? `<p class="kf-note">⚠ ${esc(keyFlagText(d.keyFlag))}${d.keyFlagWith?.length ? ` Same key: ${d.keyFlagWith.map((o) => nlink(o.id, o.name)).join(", ")}.` : ""}</p>` : ""}
    <div class="nvhead"><div><h1>${esc(d.name)}${d.isBase ? ' <span class="tag base">base</span>' : ""}${d.isLocal ? ' <span class="tag">this radio</span>' : ""}${keyBadge(d.keyFlag)} ${v28Tag(d.v28)}</h1><div class="muted">${esc(sub)}</div></div>
      <div class="ctrls"><button class="star ${window.meshWatch?.isWatched(d.id) ? "on" : ""}" id="nvStar" title="Watch this node: alert when it goes quiet or its battery runs low">${window.meshWatch?.isWatched(d.id) ? "★" : "☆"}</button>
      <a class="btn" href="/api/analytics/node.csv?id=${encodeURIComponent(d.id)}&range=${encodeURIComponent(d.range)}${stationQS()}" download>Download CSV</a></div></div>
    <section class="kpis">
      ${tile("Packets", nf.format(s.packets), `${nf.format(s.packetsEver)} since logging began`, "observed")}
      ${tile("Seen", s.presence == null ? "—" : `${Math.round(s.presence * 100)}<small>%</small>`, `heard in ${s.hoursHeard} of ${s.coveredHours} logged hours`, "inferred")}
      ${tile("Per day", s.perDay == null ? "—" : `${est ? "~" : ""}${fmt(s.perDay, s.perDay < 10 ? 1 : 0)}`, est ? "estimate: under a day logged" : "packets per logged day", "inferred")}
      ${tile("Last heard", s.lastEver ? esc(ago(s.lastEver)) : "—", s.firstEver ? `first heard ${esc(when(s.firstEver))}` : "", "observed")}
      ${tile("Longest silence", sil ? dur(sil.seconds) : "—", sil ? (sil.ongoing ? "and counting" : `ended ${esc(when(sil.to))}`) : "no gap over an hour while logging", "inferred")}
      ${tile("Heard directly", nf.format(s.direct), `of ${nf.format(s.packets)} packets (0 hops)`, "observed")}
      ${tile("Relayed to us" + prov("inferred", "Attributed by the packet's 1-byte relay ID."), `${s.relayByteShared.length ? "≤" : ""}${nf.format(s.relayedToUs)}`, s.relayByteShared.length
        ? `<span title="${esc(s.relayByteShared.map((c) => c.name).join(", "))}">upper bound: relay ID …${esc(d.id.slice(-2))} is shared with ${s.relayByteShared.length} other node${s.relayByteShared.length === 1 ? "" : "s"}</span>`
        : "other nodes' packets it was last hop for")}
    </section>
    ${card("Activity", `Packets per ${d.bucket} by type. Hatched = dashboard wasn't running.`, `<div class="legend-row">${d.groups.map((g) => `<span><i style="background:${gcol(g)}"></i>${esc(g)}</span>`).join("")}<span><i class="gapkey"></i>not logged</span></div><div class="chart" id="nvTraffic"></div>`)}
    <div class="grid2">
      ${card("Daily rhythm", "Average packets per logged hour, by local hour of day.", '<div class="chart" id="nvRhythm"></div>')}
      ${card("Direct signal", "Average SNR of packets heard with no relays (dB).", hasSnr ? '<div class="chart" id="nvSnr"></div>' : '<p class="muted">Never heard directly in this range: every packet came through a relay.</p>')}
    </div>
    ${hasTel ? `<div class="grid2">${card("Battery", "Reported battery level (%). Above 100 means external power.", '<div class="chart" id="nvBatt"></div>')}${card("Voltage", "Reported battery voltage (V).", '<div class="chart" id="nvVolt"></div>')}</div>` : ""}
    <div class="grid3">
      ${card("Hops travelled", "How far its packets came to reach us.", '<div id="nvHops"></div>')}
      ${card("Relayed to us by", "Last hop before our radio.", '<div id="nvRelays"></div>')}
      ${card("Packet types", "", '<div id="nvPorts"></div>')}
    </div>
    <div class="grid2">
      ${card("Movement", d.positions.length ? `${d.positions.length} position report${d.positions.length === 1 ? "" : "s"} in range. Circles show the precision the node shares at.` : "", d.positions.length ? '<div id="nvMap" class="nvmap"></div>' : '<p class="muted">No position reports in this range.</p>')}
      ${card("Radio links", "Links seen involving this node: direct reception, traceroutes, neighbor reports.", '<div id="nvLinks"></div>')}
    </div>
    <div class="grid2">
      ${card("Latest telemetry", "Most recent report of each kind, as sent.", telHtml)}
      ${card("Identity history", "A row each time its name, hardware, role or key changed.", identity)}
    </div>
    <div class="grid2">
      ${card("Messages", "To or from this node (newest first).", msgs)}
      ${card("Traceroutes", "Paths we traced to this node.", traces)}
    </div>`;

  trafficChart($("nvTraffic"), d);
  colChart($("nvRhythm"), d.rhythm.map((r) => ({
    label: `${r.hour}`, value: r.hours ? r.perHour : null,
    tip: r.hours ? `<b>${r.hour}:00–${r.hour + 1}:00</b><br>${fmt(r.perHour, 2)} packets/hour<br><span class="t">${r.packets} over ${r.hours} logged hour${r.hours === 1 ? "" : "s"}</span>` : `<b>${r.hour}:00</b><br><span class="t">never logged at this hour</span>`,
  })), { label: "Packets per hour by hour of day" });
  if (hasSnr) lineChart($("nvSnr"), d, "snr", { unit: " dB", digits: 1, yMin: Math.min(-20, ...d.series.filter((x) => x.snr != null).map((x) => Math.floor(x.snr))), label: "Direct SNR" });
  if (hasTel) {
    lineChart($("nvBatt"), d, "battery", { unit: "%", digits: 0, label: "Battery" });
    lineChart($("nvVolt"), d, "voltage", { unit: " V", digits: 2, yMin: Math.min(3, ...d.series.filter((x) => x.voltage != null).map((x) => Math.floor(x.voltage))), label: "Voltage" });
  }
  const hk = d.hops.filter((h) => h.hops != null), hu = d.hops.find((h) => h.hops == null);
  bars($("nvHops"), [...hk.map((h) => ({ label: h.hops === 0 ? "0 (direct)" : `${h.hops}`, value: h.count })), ...(hu ? [{ label: "unknown", value: hu.count, color: "var(--text-muted)" }] : [])], { empty: "Nothing heard in range." });
  bars($("nvRelays"), d.relays.map((r) => ({
    label: r.candidates.length === 1 ? r.candidates[0].name : `…${r.byte}`, value: r.count,
    labelHtml: r.candidates.length === 1 ? nlink(r.candidates[0].id, r.candidates[0].name) : `…${esc(r.byte)} <span class="muted">(${r.candidates.length || "unknown"}${r.candidates.length > 1 ? " candidates" : ""})</span>`,
  })), { empty: d.stats.direct ? "Everything was heard directly." : "No relayed packets in range." });
  bars($("nvPorts"), d.ports.map((p) => ({ label: pretty(p.port), value: p.count, color: gcol(p.group) })), { empty: "Nothing heard in range." });
  const SRC = { direct: "heard directly", traceroute: "traceroute", neighborinfo: "neighbor report" };
  bars($("nvLinks"), d.neighbors.map((n) => ({ label: n.name, labelHtml: `${nlink(n.id, n.name)} <span class="muted">${esc(SRC[n.source] || n.source)}${n.snr != null ? ` · ${fmt(n.snr, 1)} dB` : ""}</span>`, value: n.count })), { empty: "No links seen in range." });
  drawMovement(d);
  $("nvStar")?.addEventListener("click", async (e) => {
    const on = await window.meshWatch.toggle(d.id);
    e.target.classList.toggle("on", on); e.target.textContent = on ? "★" : "☆";
  });
  if (!d.positions.length) {
    const e = ((await estimates()).estimates || []).find((x) => x.id === d.id);
    const card = $("nvMap") ? null : [...document.querySelectorAll("#nodeView .card")].find((c) => c.querySelector("h2")?.textContent === "Movement");
    if (e && card) {
      card.querySelector(".muted").innerHTML = `No position reports. Estimated position ${prov("inferred")}: ${fmt(e.lat, 4)}, ${fmt(e.lon, 4)} ±${fmt(e.radiusKm, 1)} km (${esc(e.method)}), from links to ${e.anchors.map((a) => `${nlink(a.id, a.name)}${a.assumed ? " (assumed at the base station)" : ""}`).join(", ")}.`;
    }
  }
}

function drawMovement(d) {
  if (NV.map) { NV.map.remove(); NV.map = null; }
  const el = $("nvMap"); if (!el || !d.positions.length) return;
  const dark = matchMedia("(prefers-color-scheme: dark)").matches;
  NV.map = L.map(el, { scrollWheelZoom: false, attributionControl: true });
  lkBaseTiles().addTo(NV.map);
  const col = css("--s1"), pts = d.positions.map((p) => [p.lat, p.lon]);
  if (pts.length > 1) L.polyline(pts, { color: col, weight: 2, opacity: 0.7 }).addTo(NV.map);
  d.positions.forEach((p, i) => {
    const last = i === d.positions.length - 1;
    if (p.precision_bits && p.precision_bits < 32) {
      L.circle([p.lat, p.lon], { radius: (23300 * 2 ** (10 - p.precision_bits)) / 2, color: col, weight: 1, opacity: last ? 0.6 : 0.2, fillOpacity: last ? 0.06 : 0.02, interactive: false }).addTo(NV.map);
    }
    L.circleMarker([p.lat, p.lon], { radius: last ? 7 : 4, color: css("--surface-1"), weight: 2, fillColor: col, fillOpacity: 1 })
      .bindTooltip(`${when(p.ts)}${p.alt != null ? ` · ${esc(p.alt)} m` : ""}`).addTo(NV.map);
  });
  const b = L.latLngBounds(pts);
  NV.map.fitBounds(b.pad(0.4), { maxZoom: 13 });
  if (pts.length === 1) NV.map.setZoom(11);
}

document.addEventListener("click", (e) => {
  if (e.target.closest("a.back")) { e.preventDefault(); history.pushState("", "", location.pathname); route(); }
});
