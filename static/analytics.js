"use strict";
/* Analytics overview. Shared helpers ($, esc, fmt, gcol, when, showTip…) are in common.js. */

/* ---------------------------------------------------------------- scales */
function niceMax(v) {
  if (!v || v <= 0) return 1;
  const p = 10 ** Math.floor(Math.log10(v)), n = v / p;
  return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10) * p;
}
function xLabels(series, bucket, W, x) {
  // pick a label stride so labels never collide (~70px apart)
  const n = series.length, every = Math.max(1, Math.ceil(70 / (W / n)));
  const out = [];
  series.forEach((s, i) => {
    const d = new Date(s.t * 1000);
    let lab = null;
    if (bucket === "hour") {
      const strideH = [1, 2, 3, 6, 12, 24].find((h) => h >= every) || 24;
      if (d.getHours() % strideH === 0) lab = d.getHours() === 0 ? d.toLocaleDateString([], { weekday: "short", day: "numeric" }) : `${d.getHours()}:00`;
    } else if (i % every === 0) {
      lab = d.toLocaleDateString([], { month: "short", day: "numeric" });
    }
    if (lab) out.push(`<text class="axis" x="${x(i) + (W / n) / 2}" y="${H_PAD_B_LABEL}" text-anchor="middle">${esc(lab)}</text>`);
  });
  return out.join("");
}
let H_PAD_B_LABEL = 0;

/* ---------------------------------------------------------------- stacked traffic chart */
function trafficChart(el, data, color = gcol, label = (g) => g) {
  const series = data.series, groups = data.groups;
  const W = el.clientWidth || 800, H = 220, pl = 40, pr = 8, pt = 8, pb = 22;
  H_PAD_B_LABEL = H - 6;
  if (!series.length) { el.innerHTML = '<p class="muted">No data in this range.</p>'; return; }
  const max = niceMax(Math.max(1, ...series.map((s) => s.total || 0)));
  const n = series.length, bw = (W - pl - pr) / n;
  const x = (i) => pl + i * bw, y = (v) => pt + (1 - v / max) * (H - pt - pb);
  const barW = Math.max(1, bw * (n > 100 ? 0.9 : 0.72)), off = (bw - barW) / 2;
  let body = "";
  series.forEach((s, i) => {
    if (!s.covered) { body += `<rect fill="url(#hatch-${el.id})" x="${x(i)}" y="${pt}" width="${bw}" height="${H - pt - pb}"/>`; return; }
    let acc = 0;
    groups.forEach((g) => {
      const v = s.byGroup[g]; if (!v) return;
      const y0 = y(acc), y1 = y(acc + v);
      // 1px surface gap between stacked segments
      body += `<rect x="${(x(i) + off).toFixed(2)}" y="${y1.toFixed(2)}" width="${barW.toFixed(2)}" height="${Math.max(0.5, y0 - y1 - (acc ? 1 : 0)).toFixed(2)}" fill="${color(g)}"/>`;
      acc += v;
    });
  });
  const ticks = [0, max / 2, max];
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" height="${H}" role="img" aria-label="Packets per ${data.bucket} by type">
    <defs><pattern id="hatch-${el.id}" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><rect width="3" height="6" fill="${css("--gap-hatch")}"/></pattern></defs>
    ${ticks.map((v) => `<line class="${v ? "grid" : "base"}" x1="${pl}" x2="${W - pr}" y1="${y(v)}" y2="${y(v)}"/><text class="axis" x="${pl - 6}" y="${y(v) + 3}" text-anchor="end">${fmt(v, v < 10 && v % 1 ? 1 : 0)}</text>`).join("")}
    ${body}
    ${xLabels(series, data.bucket, W - pl - pr, (i) => pl + i * bw - pl + pl)}
    <rect class="hot" id="trafficHot" x="0" y="${pt}" width="${bw}" height="${H - pt - pb}" visibility="hidden"/>
    <rect x="${pl}" y="0" width="${W - pl - pr}" height="${H}" fill="transparent" id="trafficHit"/>
  </svg>`;
  const svg = el.querySelector("svg"), hot = svg.querySelector("#trafficHot");
  svg.addEventListener("pointermove", (e) => {
    const r = svg.getBoundingClientRect(), mx = ((e.clientX - r.left) / r.width) * W;
    const i = Math.max(0, Math.min(n - 1, Math.floor((mx - pl) / bw))), s = series[i];
    hot.setAttribute("x", x(i)); hot.setAttribute("visibility", "visible");
    const when_ = data.bucket === "hour" ? when(s.t) : new Date(s.t * 1000).toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });
    if (!s.covered) return showTip(e, `<b>${esc(when_)}</b><br><span class="t">${esc(data.gapText || "No data: the dashboard wasn't running")}</span>`);
    const rows = groups.filter((g) => s.byGroup[g]).reverse().map((g) => `<span class="sw" style="background:${color(g)}"></span>${esc(label(g))} <b>${nf.format(s.byGroup[g])}</b>`).join("<br>");
    showTip(e, `<b>${esc(when_)}</b> · ${nf.format(s.total)} packets${rows ? `<br>${rows}` : ""}`);
  });
  svg.addEventListener("pointerleave", () => { hot.setAttribute("visibility", "hidden"); hideTip(); });
}

/* ---------------------------------------------------------------- single-series line chart with gaps */
function lineChart(el, data, key, { unit = "", digits = 1, yMin = 0, label }) {
  const series = data.series;
  const pts = series.map((s, i) => [i, s[key]]);
  const W = el.clientWidth || 500, H = 150, pl = 40, pr = 8, pt = 8, pb = 22;
  H_PAD_B_LABEL = H - 6;
  if (!pts.some((p) => p[1] != null)) { el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" height="${H}"><text class="empty" x="${W / 2}" y="${H / 2}" text-anchor="middle">No readings in this range yet</text></svg>`; return; }
  const n = series.length, bw = (W - pl - pr) / n;
  const max = niceMax(Math.max(...pts.map((p) => p[1] ?? 0)) * 1.1);
  const x = (i) => pl + i * bw + bw / 2, y = (v) => pt + (1 - (v - yMin) / (max - yMin)) * (H - pt - pb);
  let d = "", area = "", seg = [];
  const flush = () => {
    if (!seg.length) return;
    d += seg.map((p, j) => `${j ? "L" : "M"}${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join("");
    if (seg.length > 1) area += `M${x(seg[0][0])},${y(yMin)}` + seg.map((p) => `L${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join("") + `L${x(seg.at(-1)[0])},${y(yMin)}Z`;
    seg = [];
  };
  pts.forEach((p) => (p[1] == null ? flush() : seg.push(p))); flush();
  const singles = pts.filter((p, i) => p[1] != null && pts[i - 1]?.[1] == null && pts[i + 1]?.[1] == null);
  const gaps = series.map((s, i) => (!s.covered ? `<rect fill="url(#hatch-${el.id})" x="${pl + i * bw}" y="${pt}" width="${bw}" height="${H - pt - pb}"/>` : "")).join("");
  const ticks = [yMin, (yMin + max) / 2, max];
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" height="${H}" role="img" aria-label="${esc(label)}">
    <defs><pattern id="hatch-${el.id}" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><rect width="3" height="6" fill="${css("--gap-hatch")}"/></pattern></defs>
    ${gaps}
    ${ticks.map((v, i) => `<line class="${i ? "grid" : "base"}" x1="${pl}" x2="${W - pr}" y1="${y(v)}" y2="${y(v)}"/><text class="axis" x="${pl - 6}" y="${y(v) + 3}" text-anchor="end">${fmt(v, max < 5 ? 1 : 0)}</text>`).join("")}
    <path class="area" d="${area}"/><path class="line" d="${d}"/>
    ${singles.map((p) => `<circle cx="${x(p[0])}" cy="${y(p[1])}" r="3" fill="var(--s1)"/>`).join("")}
    ${xLabels(series, data.bucket, W - pl - pr, (i) => pl + i * bw - pl + pl)}
    <line class="grid" id="xh" y1="${pt}" y2="${H - pb}" visibility="hidden" stroke-dasharray="2 3"/>
    <circle id="dot" r="4" fill="var(--s1)" stroke="var(--surface-1)" stroke-width="2" visibility="hidden"/>
    <rect x="${pl}" y="0" width="${W - pl - pr}" height="${H}" fill="transparent"/>
  </svg>`;
  const svg = el.querySelector("svg"), xh = svg.querySelector("#xh"), dot = svg.querySelector("#dot");
  svg.addEventListener("pointermove", (e) => {
    const r = svg.getBoundingClientRect(), mx = ((e.clientX - r.left) / r.width) * W;
    const i = Math.max(0, Math.min(n - 1, Math.floor((mx - pl) / bw))), s = series[i], v = s[key];
    xh.setAttribute("x1", x(i)); xh.setAttribute("x2", x(i)); xh.setAttribute("visibility", "visible");
    if (v != null) { dot.setAttribute("cx", x(i)); dot.setAttribute("cy", y(v)); dot.setAttribute("visibility", "visible"); } else dot.setAttribute("visibility", "hidden");
    const lab = data.bucket === "hour" ? when(s.t) : new Date(s.t * 1000).toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });
    showTip(e, `<b>${esc(lab)}</b><br>${v == null ? '<span class="t">No reading</span>' : `${esc(label)}: <b>${fmt(v, digits)}${unit}</b>`}`);
  });
  svg.addEventListener("pointerleave", () => { xh.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden"); hideTip(); });
}

/* ---------------------------------------------------------------- heatmap */
function heatmap(el, data) {
  const cell = new Map(data.heat.map((h) => [`${h.dow}-${h.hour}`, h]));
  const cov = new Map(data.heatCoverage.map((h) => [`${h.dow}-${h.hour}`, h.hours]));
  const max = Math.max(0, ...data.heat.map((h) => h.perHour || 0));
  const order = [1, 2, 3, 4, 5, 6, 0]; // Monday first
  let html = '<table role="grid" aria-label="Average packets per hour by weekday and hour"><thead><tr><th></th>';
  for (let h = 0; h < 24; h++) html += `<th scope="col">${h % 3 === 0 ? h : ""}</th>`;
  html += "</tr></thead><tbody>";
  for (const d of order) {
    html += `<tr><th scope="row">${DOW[d]}</th>`;
    for (let h = 0; h < 24; h++) {
      const c = cell.get(`${d}-${h}`), hours = cov.get(`${d}-${h}`) || 0;
      if (!hours) { html += `<td class="none" data-d="${d}" data-h="${h}"></td>`; continue; }
      const v = c?.perHour || 0, pct = max ? 8 + 92 * (v / max) : 8;
      html += `<td data-d="${d}" data-h="${h}" style="background:color-mix(in srgb, var(--s1) ${pct.toFixed(0)}%, var(--surface-2))"></td>`;
    }
    html += "</tr>";
  }
  html += `</tbody></table><div class="scale"><span>0</span><span class="bar"></span><span>${fmt(max, 1)} packets/hour</span><span class="legend-row" style="margin:0 0 0 12px"><span><i class="gapkey"></i>not logged</span></span></div>`;
  el.innerHTML = html;
  el.querySelector("table").addEventListener("pointermove", (e) => {
    const td = e.target.closest("td"); if (!td) return hideTip();
    const d = +td.dataset.d, h = +td.dataset.h, c = cell.get(`${d}-${h}`), hours = cov.get(`${d}-${h}`) || 0;
    const slot = `${DOW[d]} ${h}:00–${h + 1}:00`;
    showTip(e, hours ? `<b>${slot}</b><br>${fmt(c?.perHour || 0, 1)} packets/hour<br><span class="t">${nf.format(c?.packets || 0)} packets over ${hours} logged hour${hours === 1 ? "" : "s"}</span>`
      : `<b>${slot}</b><br><span class="t">Never logged at this time yet</span>`);
  });
  el.querySelector("table").addEventListener("pointerleave", hideTip);
}

/* ---------------------------------------------------------------- simple horizontal bars */
function bars(el, rows, { color = "var(--s1)", empty = "Nothing yet.", title, value = (v) => nf.format(v) } = {}) {
  if (!rows.length) { el.innerHTML = `<p class="muted">${esc(empty)}</p>`; return; }
  const max = Math.max(...rows.map((r) => r.value));
  el.innerHTML = `<div class="bars">${rows.map((r, i) => `<div class="r" data-i="${i}"><span class="l" title="${esc(r.label)}">${r.labelHtml || esc(r.label)}</span><span class="t"><i style="width:${((r.value / max) * 100).toFixed(1)}%;background:${r.color || color}"></i></span><span class="n">${value(r.value, r)}</span></div>`).join("")}</div>`;
  if (title) {
    el.querySelector(".bars").addEventListener("pointermove", (e) => { const r = e.target.closest(".r"); if (!r) return hideTip(); showTip(e, title(rows[+r.dataset.i])); });
    el.querySelector(".bars").addEventListener("pointerleave", hideTip);
  }
}

/* ---------------------------------------------------------------- nodes table */
const NODE_COLS = [
  { key: "name", label: "Node", sort: (n) => n.name.toLowerCase() },
  { key: "packets", label: "Packets", num: true, sort: (n) => n.packets },
  { key: "perDay", label: "Per day", num: true, sort: (n) => n.perDay ?? -1 },
  { key: "presence", label: "Seen", num: true, sort: (n) => n.presence ?? -1 },
  { key: "mix", label: "Mix", sort: (n) => (n.byGroup.Text || 0) / n.packets },
  { key: "last", label: "Last heard", num: true, sort: (n) => n.last },
  { key: "hops", label: "Hops", num: true, sort: (n) => n.avgHops ?? 99 },
  { key: "snr", label: "Direct SNR", num: true, sort: (n) => n.snrDirect ?? -999 },
];
const NT = { sort: "packets", dir: -1, showAll: false };

function renderNodes(data) {
  const q = $("nodeFilter").value.trim().toLowerCase();
  const col = NODE_COLS.find((c) => c.key === NT.sort);
  const tok = q.match(/^(role|hw):(.*)$/);
  let rows = data.nodes.filter((n) => {
    if (!q) return true;
    if (q === "fw:2.8") return n.v28;
    if (q === "key:flagged" || q === "key:compromised" || q === "key:shared") return n.keyFlag && (q === "key:flagged" || `key:${n.keyFlag.kind}` === q);
    if (tok) return String((tok[1] === "role" ? n.role : n.hw) ?? "unknown").toLowerCase() === tok[2].trim();
    return `${n.name} ${n.short} ${n.id} ${n.hw} ${n.role}`.toLowerCase().includes(q);
  });
  rows.sort((a, b) => { const x = col.sort(a), y = col.sort(b); return (x < y ? -1 : x > y ? 1 : 0) * NT.dir; });
  const total = rows.length;
  if (!NT.showAll) rows = rows.slice(0, 50);
  const maxP = Math.max(1, ...data.nodes.map((n) => n.packets));
  const est = data.kpis.coveredHours < 24; // a daily rate from less than a day of logging is a guess
  const head = `<thead><tr>${NODE_COLS.map((c) => `<th class="${c.num ? "num" : ""}" data-k="${c.key}" ${NT.sort === c.key ? `aria-sort="${NT.dir > 0 ? "ascending" : "descending"}"` : ""}>${c.label}</th>`).join("")}</tr></thead>`;
  const body = rows.map((n) => {
    const mix = data.groups.filter((g) => n.byGroup[g]).map((g) => `<i style="width:${((n.byGroup[g] / n.packets) * 100).toFixed(1)}%;background:${gcol(g)}"></i>`).join("");
    const hops = n.minHops == null ? "—" : n.minHops === n.maxHops ? `${esc(n.minHops)}` : `${esc(n.minHops)}–${esc(n.maxHops)}`;
    const tag = n.isBase ? " ☀" : "";
    return `<tr data-id="${esc(n.id)}">
      <td><div class="nm" title="${esc(n.name)}">${esc(n.name)}${tag}${keyBadge(n.keyFlag, { short: true })}${n.v28 ? " " + v28Tag(true) : ""}<small>${esc([n.short, (n.hw || "").replace(/_/g, " ").toLowerCase(), n.role && n.role !== "CLIENT" ? n.role.replace(/_/g, " ").toLowerCase() : ""].filter(Boolean).join(" · "))}</small></div></td>
      <td class="num"><div class="pbar"><span>${nf.format(n.packets)}</span><span class="b"><i style="width:${((n.packets / maxP) * 100).toFixed(1)}%"></i></span></div></td>
      <td class="num" ${est ? `title="Estimated from only ${data.kpis.coveredHours} logged hour${data.kpis.coveredHours === 1 ? "" : "s"}"` : ""}>${n.perDay == null ? "—" : `${est ? "~" : ""}${fmt(n.perDay, n.perDay < 10 ? 1 : 0)}`}</td>
      <td class="num"><div class="pbar"><span>${n.presence == null ? "—" : `${Math.round(n.presence * 100)}%`}</span><span class="b"><i style="width:${((n.presence || 0) * 100).toFixed(1)}%"></i></span></div></td>
      <td><div class="mix" data-mix>${mix}</div></td>
      <td class="num" title="${esc(when(n.last))}">${ago(n.last)}</td>
      <td class="num">${hops}</td>
      <td class="num">${n.snrDirect == null ? "—" : `${fmt(n.snrDirect, 1)} dB`}</td></tr>`;
  }).join("");
  $("nodes").innerHTML = head + `<tbody>${body || `<tr><td colspan="8" class="muted">No nodes match.</td></tr>`}</tbody>` +
    (total > 50 ? `<tfoot><tr><td colspan="8"><button class="btn more" id="nodesAll">${NT.showAll ? "Show top 50" : `Show all ${total}`}</button></td></tr></tfoot>` : "");
}

/* ---------------------------------------------------------------- readable vs private traffic */

// Fixed colour per category; private channels take the remaining slots in hash order (stable per channel).
// Readable channels come from the radio's own config (server computes their on-air hashes on connect).
// Colours follow the entity: first readable channel blue/aqua, DMs orange, others take later slots.
function privacyColor(groups) {
  const readable = [...new Set(groups.filter((g) => g.startsWith("rd-")).map((g) => g.slice(0, 5)))];
  const rdPairs = [["var(--s1)", "var(--s3)"], ["var(--s4)", "var(--s6)"]];
  const rest = ["var(--s5)", "var(--s7)", "var(--s4)", "var(--s6)"];
  const priv = groups.filter((g) => !g.startsWith("rd-") && g !== "dm");
  return (g) => {
    if (g.startsWith("rd-")) { const pair = rdPairs[readable.indexOf(g.slice(0, 5)) % rdPairs.length]; return g.endsWith("-d") ? pair[1] : pair[0]; }
    if (g === "dm") return "var(--s2)";
    return rest[priv.indexOf(g) % rest.length] || "var(--text-muted)";
  };
}
let PRIV_CHANS = [];
function privacyLabel(g) {
  if (g.startsWith("rd-")) {
    const h = parseInt(g.slice(3, 5), 16), c = PRIV_CHANS.find((x) => x.hash === h);
    return `${c ? c.name : `0x${g.slice(3, 5)}`}${c?.publicKey ? " (public)" : ""} · ${g.endsWith("-d") ? "addressed" : "broadcast"}`;
  }
  if (g === "dm") return "Encrypted direct messages";
  if (g === "unknown") return "Channel not logged";
  return `Private channel 0x${g.slice(3)}`;
}

function renderRadioChannels(p) {
  const chans = (p.readable || []).slice().sort((a, b) => a.index - b.index);
  const key = (c) => !c.encrypted
    ? '<span class="keytag none" title="No encryption: anyone can read this channel.">no encryption</span>'
    : c.publicKey
      ? '<span class="keytag public" title="One of Meshtastic\'s well-known default keys: every Meshtastic radio can read this channel.">public default key</span>'
      : '<span class="keytag own" title="A custom key: only radios given this channel can read it.">own key</span>';
  const when_ = p.readableSource === "radio" && p.readableTs
    ? `Read from the radio ${esc(ago(p.readableTs))} (${esc(when(p.readableTs))}), on its last connect. Re-read automatically whenever the radio reconnects or reboots, which a channel change triggers.`
    : "Not read from the radio yet; assuming the default LongFast channel until it connects.";
  $("radioChannels").innerHTML = `<h3>Our radio's channels ${prov(p.readableSource === "radio" ? "reported" : "inferred", p.readableSource === "radio" ? "Read from the radio's own configuration." : "Fallback until the radio connects.")}</h3>
    <table><thead><tr><th>Slot</th><th>Role</th><th>Name</th><th>On-air ID ${prov("inferred", "Computed from the channel's name and key the same way the firmware does; the key itself is never stored.")}</th><th>Key</th></tr></thead><tbody>
    ${chans.map((c) => `<tr><td class="num">${c.index}</td><td>${esc(c.role.toLowerCase())}</td><td><b>${esc(c.name)}</b></td>
      <td class="mono">0x${c.hash.toString(16).padStart(2, "0")}</td><td>${key(c)}</td></tr>`).join("")}</tbody></table>
    <div class="foot">${when_} Traffic on these IDs counts as readable below; everything else is private to us.</div>`;
}

function renderPrivacy(d) {
  const p = d.privacy;
  PRIV_CHANS = p.readable || [];
  renderRadioChannels(p);
  const color = privacyColor(p.groups);
  const pct = (n) => (p.total ? Math.round((n / p.total) * 100) : 0);
  $("privacySummary").innerHTML = p.total
    ? `<b>${pct(p.unreadable)}%</b> of ${nf.format(p.total)} distinct packets heard couldn't be read by our radio:
       ${nf.format(p.totals.dm || 0)} encrypted direct message${(p.totals.dm || 0) === 1 ? "" : "s"} and
       ${nf.format(p.unreadable - (p.totals.dm || 0))} on ${p.privateChannels} private channel${p.privateChannels === 1 ? "" : "s"}.`
    : "No over-the-air receptions logged in this range yet.";
  $("privacyLegend").innerHTML = p.groups.map((g) => `<span><i style="background:${color(g)}"></i>${esc(privacyLabel(g))}</span>`).join("") +
    '<span><i class="gapkey"></i>no data</span>';
  trafficChart($("privacyChart"), { ...d, series: p.series, groups: p.groups, gapText: p.miningSince && p.series.some((s) => s.t < p.miningSince) ? "No data: dashboard not running, or before reception logging began" : "No data: the dashboard wasn't running" }, color, privacyLabel);
  $("privacyTable").innerHTML = p.channels.length ? `<div class="tablewrap"><table class="nodes"><thead><tr>
      <th>Channel</th><th>What it is</th><th class="num">Packets</th><th class="num">Copies heard</th><th class="num">Broadcast</th><th class="num">Addressed</th><th class="num">Senders</th><th class="num">First seen</th><th class="num">Last seen</th></tr></thead><tbody>
    ${p.channels.map((c) => {
      const hex = c.channel == null ? "—" : `0x${c.channel.toString(16).padStart(2, "0")}`;
      const what = c.readable ? `${c.name} (${c.publicKey ? "public default key" : "your key"}): readable by our radio`
        : c.channel === 0 ? (c.bcast ? "Channel 0: mixed" : "Encrypted DMs (end-to-end, channel 0)") : "Private channel: not readable by our radio";
      return `<tr><td class="mono">${hex}</td><td>${esc(what)}</td><td class="num">${nf.format(c.packets)}</td><td class="num">${nf.format(c.copies)}</td>
        <td class="num">${nf.format(c.bcast)}</td><td class="num">${nf.format(c.direct)}</td><td class="num">${nf.format(c.senders)}</td>
        <td class="num" title="${esc(when(c.first))}">${esc(ago(c.first))}</td><td class="num" title="${esc(when(c.last))}">${esc(ago(c.last))}</td></tr>`;
    }).join("")}</tbody></table></div>` : "";
}

/* ---------------------------------------------------------------- node types */

// One-line meanings for Meshtastic device roles (shown on hover).
const ROLE_INFO = {
  CLIENT: "Standard node: sends its own traffic and rebroadcasts others'.",
  CLIENT_MUTE: "Sends its own traffic but never rebroadcasts. Good for nodes next to a better relay.",
  CLIENT_HIDDEN: "Broadcasts as little as possible; mostly speaks only when spoken to.",
  CLIENT_BASE: "Base station: a fixed client that gives priority to relaying its favourite nodes.",
  ROUTER: "Infrastructure node: always rebroadcasts, ahead of clients. Usually high and well placed.",
  ROUTER_LATE: "Rebroadcasts only after other nodes have had their chance, to fill coverage holes.",
  REPEATER: "Pure relay with minimal identity (deprecated in newer firmware).",
  TRACKER: "Prioritises sending its GPS position.",
  SENSOR: "Prioritises sending telemetry readings.",
  TAK: "Optimised for ATAK (Android Team Awareness Kit) clients.",
  TAK_TRACKER: "ATAK-style position tracker.",
  LOST_AND_FOUND: "Broadcasts its location so a lost device can be found.",
};
// Hardware: keep model codes readable ("RAK4631", "Heltec Mesh Tower V2"), not "Rak4631".

function typeBreakdown(el, data, key) {
  const total = data.nodes.reduce((a, n) => a + n.packets, 0) || 1;
  const groups = new Map();
  for (const n of data.nodes) {
    const k = n[key] || null;
    const g = groups.get(k) || { key: k, nodes: [], packets: 0 };
    g.nodes.push(n); g.packets += n.packets; groups.set(k, g);
  }
  // most nodes first; "unknown" always last so it never reads as a category that won
  const rows = [...groups.values()].sort((a, b) => (a.key === null) - (b.key === null) || b.nodes.length - a.nodes.length || b.packets - a.packets);
  if (!rows.length) { el.innerHTML = '<p class="muted">No nodes heard in this range.</p>'; return; }
  const max = Math.max(...rows.map((r) => r.nodes.length));
  const current = $("nodeFilter").value.trim().toLowerCase();
  const known = data.nodes.filter((n) => n[key]).length;
  el.innerHTML = `<div class="types">${rows.map((r, i) => {
    const tokenVal = (r.key ?? "unknown").toLowerCase();
    return `<div class="r ${r.key ? "" : "unk"} ${current === `${key === "role" ? "role" : "hw"}:${tokenVal}` ? "on" : ""}" data-i="${i}" data-token="${key === "role" ? "role" : "hw"}:${esc(tokenVal)}">
      <span class="l">${esc(r.key ? (key === "hw" ? prettyHw(r.key) : prettyEnum(r.key)) : "Unknown")}${r.key ? "" : " <small>(never announced)</small>"}</span>
      <span class="t"><i style="width:${((r.nodes.length / max) * 100).toFixed(1)}%"></i></span>
      <span class="n"><b>${r.nodes.length}</b> node${r.nodes.length === 1 ? "" : "s"} · ${Math.round((r.packets / total) * 100)}%</span></div>`;
  }).join("")}</div>
  <div class="foot">${known} of ${data.nodes.length} nodes have announced their ${key === "role" ? "role" : "hardware"}. A node's identity is broadcast every few hours, so unknowns shrink as logging continues.</div>`;
  const box = el.querySelector(".types");
  box.addEventListener("pointermove", (e) => {
    const r = e.target.closest(".r"); if (!r) return hideTip();
    const g = rows[+r.dataset.i];
    const names = g.nodes.slice().sort((a, b) => b.packets - a.packets).slice(0, 8).map((n) => esc(n.name)).join(", ");
    const info = key === "role" && g.key ? `<br><span class="t">${esc(ROLE_INFO[g.key] || "")}</span>` : "";
    showTip(e, `<b>${esc(g.key ? (key === "hw" ? prettyHw(g.key) : prettyEnum(g.key)) : "Unknown")}</b>: ${g.nodes.length} node${g.nodes.length === 1 ? "" : "s"}, ${nf.format(g.packets)} packets${info}<br>${names}${g.nodes.length > 8 ? ` +${g.nodes.length - 8} more` : ""}`);
  });
  box.addEventListener("pointerleave", hideTip);
  box.addEventListener("click", (e) => {
    const r = e.target.closest(".r"); if (!r) return;
    const f = $("nodeFilter");
    f.value = f.value.trim().toLowerCase() === r.dataset.token ? "" : r.dataset.token; // click again to clear
    renderNodes(A.data); renderTypes(A.data);
    if (f.value) $("nodes").closest(".card").scrollIntoView({ behavior: "smooth", block: "start" });
  });
}
// Firmware 2.8 adoption (nodeids.py): of the radios heard each day whose key we know, how many number
// themselves the 2.8 way. 2.8 makes position and telemetry opt-in, so this explains falling counts elsewhere.
function renderFw28(d) {
  const f = d.firmware28; if (!f) return;
  const pc = (a, b) => (b ? `${Math.round((a / b) * 100)} %` : "—");
  const max = Math.max(1, ...f.days.map((x) => x.keyed));
  $("fw28").innerHTML = `<p class="fw28-sum"><b>${f.radios.length}</b> of ${f.keyed} radios heard in this range (whose key we know, of ${f.heard}) are likely on 2.8: <b>${pc(f.radios.length, f.keyed)}</b>${prov("inferred", "From the node number: 2.8 derives it from the public key.")}</p>
    ${f.radios.length ? `<p class="fw28-list">${f.radios.map((r) => `<a class="nlink" href="${nodeHref(r.id)}">${esc(r.name)}</a>`).join(", ")}</p>` : ""}
    ${f.renumbered.length ? `<p class="muted small">Renumbered on upgrade, history joined: ${f.renumbered.map((r) => `${esc(r.name)} (${esc(r.old)} → ${esc(r.new)})`).join(", ")}.</p>` : ""}
    <div class="bars">${f.days.map((x) => `<div class="r" title="${x.v28} of ${x.keyed} radios with a known key (${x.heard} heard)"><span class="l">${esc(new Date(x.day + "T12:00").toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" }))}</span><span class="fw28-bar"><i style="width:${((x.keyed / max) * 100).toFixed(1)}%"></i><i class="v" style="width:${((x.v28 / max) * 100).toFixed(1)}%"></i></span><span class="n">${x.v28} / ${x.keyed}</span></div>`).join("")}</div>`;
}

function renderTypes(d) { typeBreakdown($("roles"), d, "role"); typeBreakdown($("hardware"), d, "hw"); }

/* ---------------------------------------------------------------- conversations */
let CONV_SHOWN = 20;
function renderConvs(data) {
  const cs = data.conversations;
  if (!cs.length) { $("convs").innerHTML = '<p class="muted">No text messages in this range.</p>'; return; }
  $("convs").innerHTML = cs.slice(0, CONV_SHOWN).map((c, ci) => {
    const people = c.people.map((p) => p.name);
    const who = people.slice(0, 3).join(", ") + (people.length > 3 ? ` +${people.length - 3}` : "");
    const last = c.messages.at(-1);
    const span = c.start === c.end ? when(c.start) : `${when(c.start)} – ${new Date(c.end * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`;
    return `<details class="conv" data-i="${ci}"><summary>
        <span class="who"><span class="kind">${c.kind === "channel" ? "channel" : "direct"}</span>${esc(who)}</span>
        <span class="when">${esc(span)} · ${c.count} msg${c.count === 1 ? "" : "s"}</span>
        <span class="snip">${esc(last.name)}: ${last.encrypted ? "🔒 (couldn't decrypt)" : esc(last.text)}</span>
      </summary><div class="transcript"></div></details>`;
  }).join("") + (cs.length > CONV_SHOWN ? `<button class="btn more" id="convMore">Show ${Math.min(20, cs.length - CONV_SHOWN)} more</button>` : "");
}
function transcript(c) {
  return c.messages.map((m) => `<div class="msg ${m.outgoing ? "out" : ""} ${m.encrypted ? "enc" : ""} ${m.emoji ? "emoji" : ""}">
    <div class="h"><b>${esc(m.outgoing ? "You" : m.name)}</b> · ${esc(when(m.ts))}${m.hops != null ? ` · ${esc(m.hops)} hop${m.hops === 1 ? "" : "s"}` : ""}${m.snr != null && m.hops === 0 ? ` · SNR ${fmt(m.snr, 1)}` : ""}${m.emoji ? " · reaction" : ""}</div>
    ${m.replyTo ? `<div class="rp">↩ ${esc(m.replyTo.name)}: ${esc(m.replyTo.text)}</div>` : ""}
    <div class="b">${m.encrypted ? "🔒 Direct message our radio couldn't decrypt (missing key)" : esc(m.text)}</div></div>`).join("");
}

/* ---------------------------------------------------------------- airtime */
const AIR = { range: null, data: null };
const AIR_COL = (g) => (g === "Not decoded" ? "var(--text-muted)" : g === "Our radio" ? "var(--stale)" : gcol(g));
const secs = (s) => (s >= 3600 ? `${fmt(s / 3600, 1)} h` : s >= 60 ? `${fmt(s / 60, 1)} min` : `${fmt(s, 1)} s`);

async function renderAirtime(range) {
  if (AIR.range !== range || !AIR.data) {
    try {
      const d = await fetch(`/api/analytics/airtime?range=${encodeURIComponent(range)}${stationQS()}`).then((r) => r.json());
      if (d.error) throw new Error(d.error);
      AIR.data = d; AIR.range = range;
    } catch (e) { $("airSummary").textContent = `Couldn't load airtime: ${e.message}`; return; }
  }
  const d = AIR.data, t = d.totals;
  if (!t.loggedS) { $("airSummary").textContent = "No receptions logged in this range yet."; ["airChart", "airTx", "airOrig", "airLegend"].forEach((id) => ($(id).innerHTML = "")); return; }
  $("airSummary").innerHTML = `Transmissions our radio heard used <b>${secs(t.heardS)}</b> of airtime: <b>${fmt(t.heardPct, 2)} %</b> of the ${fmt(t.loggedS / 3600, 0)} logged hours ${prov("inferred", "Calculated from each frame's length and the LoRa settings.")}. ` +
    `Our own radio transmitted for ${secs(t.oursS)} (${fmt(t.oursPct, 2)} %). Our radio measured the channel busy <b>${fmt(t.chUtilAvg, 2)} %</b> of the time ${prov("reported", "Our radio's channel utilization, from its once-a-minute device metrics.")}` +
    (t.chUtilAvg != null ? `; the ${fmt(Math.max(0, t.chUtilAvg - t.heardPct - t.oursPct), 2)} points not explained by logged packets are receptions it rejected${t.badPackets != null ? ` (${nf.format(t.badPackets)} failed CRC)` : ""}, partial receptions and interference.` : ".");
  const groups = [...d.groups, "Our radio"];
  $("airLegend").innerHTML = groups.map((g) => `<span><i style="background:${AIR_COL(g)}"></i>${esc(g)}</span>`).join("") +
    '<span><i class="lg-line" style="border-top-color:var(--text-primary)"></i>measured channel utilization</span><span><i class="gapkey"></i>not logged</span>';
  airtimeChart($("airChart"), d, groups);
  const label = (r) => (r.id ? `<a class="nlink" href="${nodeHref(r.id)}">${esc(r.name)}</a>` : `<span class="muted">${esc(r.name)}</span>`);
  bars($("airTx"), d.transmitters.slice(0, 12).map((r) => ({ label: r.name, labelHtml: label(r), value: r.s, r })),
    { value: (v, row) => `${secs(v)} · ${fmt(row.r.share * 100, 1)} %`, title: (row) => `<b>${esc(row.r.name)}</b><br>${secs(row.r.s)} on air in ${nf.format(row.r.n)} transmissions our radio heard<br>${row.r.id || row.r.name.startsWith("sender") || row.r.name === "relay not logged" ? "" : `<span class="t">relay ID matches no single known node</span>`}` });
  bars($("airOrig"), d.originators.slice(0, 12).map((r) => ({ label: r.name, labelHtml: label(r), value: r.s, r })),
    { color: "var(--s2)", value: (v, row) => `${secs(v)} · ${fmt(row.r.share * 100, 1)} %`, title: (row) => `<b>${esc(row.r.name)}</b><br>${nf.format(row.r.packets)} packets, ${nf.format(row.r.copies)} copies heard (relays retransmit)<br>${secs(row.r.s)} of airtime in total` });
  $("airNote").textContent = `Modem: ${d.radio.preset.replace(/_/g, " ").toLowerCase()} (SF${d.radio.sf}, ${d.radio.bwKHz} kHz, CR ${d.radio.cr}). ` +
    (d.miningSince ? `Receptions are logged from ${when(d.miningSince)}, when debug-log mining began; hours before that are gaps.` : "") +
    " The firmware's own per-packet airtime log line is the same calculation, so it isn't stored separately.";
}

// stacked % of time by type (calculated) + measured channel utilization line, same unit, one axis
function airtimeChart(el, d, groups) {
  const s = d.series, W = el.clientWidth || 800, H = 220, pl = 40, pr = 8, pt = 8, pb = 22;
  const val = (x) => (x.covered ? x.total + x.ours : 0);
  const max = niceMax(Math.max(0.1, ...s.map((x) => Math.max(val(x), x.chUtil || 0))));
  const n = s.length, bw = (W - pl - pr) / n, x = (i) => pl + i * bw, y = (v) => pt + (1 - v / max) * (H - pt - pb);
  const barW = Math.max(1, bw * (n > 100 ? 0.9 : 0.72)), off = (bw - barW) / 2;
  let body = "";
  s.forEach((b, i) => {
    if (!b.covered) { body += `<rect fill="url(#hatch-${el.id})" x="${x(i)}" y="${pt}" width="${bw}" height="${H - pt - pb}"/>`; return; }
    let acc = 0;
    for (const g of groups) {
      const v = g === "Our radio" ? b.ours : b.pct[g]; if (!v) continue;
      const y0 = y(acc), y1 = y(acc + v);
      body += `<rect x="${(x(i) + off).toFixed(2)}" y="${y1.toFixed(2)}" width="${barW.toFixed(2)}" height="${Math.max(0.5, y0 - y1 - (acc ? 1 : 0)).toFixed(2)}" fill="${AIR_COL(g)}"/>`;
      acc += v;
    }
  });
  // measured line, broken wherever there's no reading
  let path = "", pen = false;
  s.forEach((b, i) => { if (b.chUtil == null) { pen = false; return; } path += `${pen ? "L" : "M"}${(x(i) + bw / 2).toFixed(1)},${y(b.chUtil).toFixed(1)}`; pen = true; });
  const ticks = [0, max / 2, max];
  H_PAD_B_LABEL = H - 6;
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" height="${H}" role="img" aria-label="Airtime per ${d.bucket} by type, with measured channel utilization">
    <defs><pattern id="hatch-${el.id}" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><rect width="3" height="6" fill="${css("--gap-hatch")}"/></pattern></defs>
    ${ticks.map((v) => `<line class="${v ? "grid" : "base"}" x1="${pl}" x2="${W - pr}" y1="${y(v)}" y2="${y(v)}"/><text class="axis" x="${pl - 6}" y="${y(v) + 3}" text-anchor="end">${fmt(v, v < 1 ? 2 : 1)}%</text>`).join("")}
    ${body}<path d="${path}" fill="none" stroke="var(--text-primary)" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
    ${xLabels(s, d.bucket, W - pl - pr, (i) => pl + i * bw)}
    <rect class="hot" id="airHot" x="0" y="${pt}" width="${bw}" height="${H - pt - pb}" visibility="hidden"/>
  </svg>`;
  const svg = el.querySelector("svg"), hot = svg.querySelector("#airHot");
  svg.addEventListener("pointermove", (e) => {
    const r = svg.getBoundingClientRect(), mx = ((e.clientX - r.left) / r.width) * W;
    const i = Math.max(0, Math.min(n - 1, Math.floor((mx - pl) / bw))), b = s[i];
    hot.setAttribute("x", x(i)); hot.setAttribute("visibility", "visible");
    const head = `<b>${esc(d.bucket === "hour" ? when(b.t) : new Date(b.t * 1000).toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" }))}</b>`;
    if (!b.covered) return showTip(e, `${head}<br><span class="t">Not logged: no receptions recorded</span>`);
    const rows = [...groups].reverse().map((g) => [g, g === "Our radio" ? b.ours : b.pct[g]]).filter(([, v]) => v)
      .map(([g, v]) => `<span class="sw" style="background:${AIR_COL(g)}"></span>${esc(g)} <b>${fmt(v, 2)} %</b>`).join("<br>");
    showTip(e, `${head}<br>Calculated airtime <b>${fmt(b.total + b.ours, 2)} %</b> of the time${rows ? `<br>${rows}` : ""}<br>Measured channel utilization <b>${b.chUtil == null ? "—" : `${fmt(b.chUtil, 2)} %`}</b>${b.chUtilN ? ` <span class="t">(${b.chUtilN} readings)</span>` : ""}`);
  });
  svg.addEventListener("pointerleave", () => { hot.setAttribute("visibility", "hidden"); hideTip(); });
}

/* ---------------------------------------------------------------- every station: node x station grid */
const MX = { range: null, data: null, meta: null };

// Raspberry Pi power flags (vcgencmd get_throttled): low bits = now, bits 16+ = since boot
function powerText(flags) {
  if (!flags) return ["—", ""];
  const v = parseInt(flags, 16);
  if (Number.isNaN(v)) return [flags, ""];
  if (v & 0x1) return ["⚠ under-voltage now", "bad"];
  if (v & 0x4) return ["⚠ throttled now", "bad"];
  if (v & 0x50000) return ["OK now (dipped earlier)", "warn"];
  return ["OK", ""];
}
const upText = (s) => (s == null ? "—" : s >= 86400 ? `${fmt(s / 86400, 1)} d` : s >= 3600 ? `${fmt(s / 3600, 1)} h` : `${fmt(s / 60, 0)} min`);
const pct = (x) => (x == null ? "—" : `${fmt(100 * x, 0)} %`);

async function renderMatrix(range) {
  if (MX.range !== range) {
    try {
      const d = await fetch(`/api/analytics/stations?range=${encodeURIComponent(range)}`).then((r) => r.json());
      if (d.error) throw new Error(d.error);
      MX.data = d; MX.range = range;
    } catch (e) { $("mxScope").textContent = `Couldn't load the station grid: ${e.message}`; return; }
  }
  const d = MX.data, st = d.stations;
  if (st.length < 2) return;
  try { MX.meta = await fetch("/api/stations").then((r) => r.json()); } catch { MX.meta = null; }
  renderStationTimeline();
  renderStationHealth();
  if (!d.coMinutes) {
    $("mxScope").textContent = "No two stations have been logging at the same time in this range yet.";
    ["mxStations", "mxGrid", "mxLegend"].forEach((id) => ($(id).innerHTML = ""));
    return;
  }
  const dur = (m) => (m >= 120 ? `${fmt(m / 60, 1)} h` : `${m} min`);
  $("mxScope").innerHTML = `${st.length} stations · compared over <b>${dur(d.coMinutes)}</b> when at least two were logging · ${nf.format(d.packets)} packets, ${nf.format(d.together)} heard by every station.`;
  $("mxStations").innerHTML = `<table class="nodes compact"><thead><tr><th>Station</th><th class="num">Logged</th><th class="num">Compared</th><th class="num">Packets</th><th class="num">Nodes</th><th class="num" title="Nodes only this station heard, while the others were listening and missed them">Only this one</th><th class="num" title="Median over nodes with 3+ comparable packets">Median capture</th></tr></thead><tbody>${st.map((s) => `
    <tr><td><a class="nlink" href="${nodeHref(s.id)}">${esc(s.name)}</a></td><td class="num">${dur(s.loggedMinutes)}</td><td class="num">${dur(s.minutes)}</td><td class="num">${nf.format(s.packets)}</td><td class="num">${nf.format(s.nodes)}</td><td class="num">${s.unique ? `<b>${nf.format(s.unique)}</b>` : "0"}</td><td class="num">${pct(s.medianCapture)}</td></tr>`).join("")}</tbody></table>`;
  $("mxLegend").innerHTML = `<span><i class="mx-key" style="--cap:1"></i>heard everything it could</span><span><i class="mx-key" style="--cap:.5"></i>half</span><span><i class="mx-key" style="--cap:0"></i>listening, heard nothing</span><span><i class="mx-key off"></i>not listening at the time</span><span class="muted">cell: capture · SNR</span>`;
  const cell = (v) => (!v ? '<td class="mx off" title="This station wasn\'t listening while the node transmitted">·</td>'
    : `<td class="mx" style="--cap:${v.capture.toFixed(3)}" title="Heard ${v.heard} of ${v.eligible} packets it could have${v.snr != null ? `\nmedian SNR ${fmt(v.snr, 1)} dB, RSSI ${fmt(v.rssi, 0)} dBm` : ""}${v.hops != null ? `\nmedian hops ${fmt(v.hops, 1)}` : ""}"><b>${pct(v.capture)}</b>${v.snr != null ? `<small>${fmt(v.snr, 1)} dB</small>` : ""}</td>`);
  $("mxGrid").innerHTML = `<table class="nodes compact mxgrid"><thead><tr><th>Node</th>${st.map((s) => `<th class="num">${esc(s.name)}</th>`).join("")}<th class="num">Heard by</th><th></th></tr></thead><tbody>${d.nodes.map((n) => `
    <tr><td><a class="nlink" href="${nodeHref(n.id)}">${esc(n.name)}</a></td>${st.map((s) => cell(n.stations[s.id])).join("")}
      <td class="num">${n.heardBy.length} of ${st.length}</td>
      <td>${n.single ? `<span class="tag" title="Only one station heard it while the others listened: a weak spot in coverage">only ${esc(st.find((s) => s.id === n.heardBy[0])?.name || "")}</span>` : ""}${n.weak ? '<span class="tag low" title="No station caught even half its packets">weak</span>' : ""}</td></tr>`).join("")}</tbody></table>`;
}

// Each station's location and health: last contact, backlog, power, temperature, uptime, versions
function renderStationHealth() {
  const m = MX.meta; if (!m) return;
  const hubSw = m.software;
  $("mxHealth").innerHTML = `<table class="nodes compact"><thead><tr><th>Station</th><th>Location</th><th class="num">Last contact</th><th class="num">Backlog</th><th>Power</th><th class="num">Temp</th><th class="num">Uptime</th><th>Radio firmware</th><th>Software</th></tr></thead><tbody>${m.stations.map((s) => {
    const r = s.report || {}, [pw, pwc] = powerText(r.throttled);
    const loc = s.location ? `<span title="${esc(r.note || "")}">${fmt(s.location[0], 5)}, ${fmt(s.location[1], 5)}</span>` : '<span class="muted" title="Set [station] location in this station\'s lorakeet.toml">not set</span>';
    const ago_ = s.lastContact ? ago(s.lastContact) : "—";
    const stale = s.lastContact && Date.now() / 1000 - s.lastContact > 600;
    const ver = r.version || (s.current ? m.version : null);
    const sw = r.software ? `${ver ? `${esc(ver)} ` : ""}<span class="mono ${r.software !== hubSw ? "warn" : ""}" title="${r.software !== hubSw ? "Different code from this hub (" + hubSw + ")" : "Same code as this hub"}">${esc(r.software)}</span>` : ver ? esc(ver) : "—";
    return `<tr><td>${esc(s.name)}${s.current ? ' <span class="muted">(this PC)</span>' : ""}</td><td>${loc}</td>
      <td class="num ${stale ? "bad" : ""}">${s.current ? "connected" : esc(ago_)}</td><td class="num">${r.backlog == null ? "—" : nf.format(r.backlog)}</td>
      <td class="${pwc}">${esc(pw)}</td><td class="num">${r.tempC == null ? "—" : `${fmt(r.tempC, 0)} °C`}</td><td class="num">${upText(r.uptimeS)}</td>
      <td class="mono">${esc(r.radioFirmware || "—")}</td><td>${sw}</td></tr>`;
  }).join("")}</tbody></table>`;
}

// Each station's own power and network changes, last 24 h (server.py HealthWatch)
async function renderStationTimeline() {
  let d; try { d = await fetch("/api/stations/timeline?hours=24").then((r) => r.json()); } catch { return; }
  const names = new Map((MX.meta?.stations || []).map((s) => [s.id, s.name]));
  const clock = (t) => new Date(t * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  const line = (e) => e.kind === "network"
    ? `<span class="${e.network === "no network" ? "bad" : ""}">network: ${e.prev ? `${esc(e.prev)} → ` : ""}${esc(e.network)}</span>`
    : e.underVoltageNow ? '<span class="bad">⚠ under-voltage now</span>'
    : e.throttledNow ? '<span class="warn">slowed down (throttled) now</span>'
    : e.prev == null ? `power: ${e.underVoltageSinceBoot ? '<span class="warn">under-voltage earlier since boot</span>' : "OK"}`
    : `power OK again${e.underVoltageSinceBoot ? " (dipped since boot)" : ""}`;
  const rows = Object.entries(d.stations || {}).filter(([, ev]) => ev.length);
  $("mxTimeline").innerHTML = rows.length ? `<h3 class="minor" style="margin-top:14px">Station timeline (24 h)</h3>` + rows.map(([sid, ev]) =>
    `<div class="mx-tl"><b>${esc(names.get(sid) || sid)}</b>${ev.slice(-40).map((e) => `<span class="mx-ev"><span class="muted">${clock(e.ts)}</span> ${line(e)}</span>`).join("")}</div>`).join("") : "";
}

/* ---------------------------------------------------------------- station comparison */
const CMP = { range: null, data: null, stations: null, a: null, b: null };
const CMP_COL = { a: "var(--s1)", both: "var(--s3)", b: "var(--s2)" };

async function renderCompare(range) {
  if (!CMP.stations) {
    try { CMP.stations = (await fetch("/api/stations").then((r) => r.json())); } catch { return; }
  }
  const list = CMP.stations.stations || [];
  $("cmpCard").hidden = list.length < 2;
  if (list.length < 2) return;
  if (!CMP.a) {
    CMP.a = CMP.stations.current || list[0].id;
    CMP.b = (list.find((s) => s.id !== CMP.a) || list[1]).id;
    const opts = list.map((s) => `<option value="${esc(s.id)}">${esc(s.name || s.id)}${s.current ? " (this PC)" : ""}</option>`).join("");
    $("cmpA").innerHTML = opts; $("cmpB").innerHTML = opts;
  }
  $("cmpA").value = CMP.a; $("cmpB").value = CMP.b;
  const key = `${range}|${CMP.a}|${CMP.b}`;
  if (CMP.key !== key) {
    try {
      const d = await fetch(`/api/analytics/compare?range=${encodeURIComponent(range)}&a=${encodeURIComponent(CMP.a)}&b=${encodeURIComponent(CMP.b)}`).then((r) => r.json());
      if (d.error) throw new Error(d.error);
      CMP.data = d; CMP.key = key;
    } catch (e) { $("cmpScope").textContent = `Couldn't compare: ${e.message}`; return; }
  }
  const d = CMP.data, A = esc(d.aName), B = esc(d.bName);
  if (!d.minutesBoth) {
    $("cmpScope").textContent = "These two stations haven't been logging at the same time in this range yet.";
    ["cmpKpis", "cmpLegend", "cmpChart", "cmpRelays", "cmpPaths", "cmpNodes"].forEach((id) => ($(id).innerHTML = ""));
    return;
  }
  const mins = d.minutesBoth, dur = mins >= 120 ? `${fmt(mins / 60, 1)} hours` : `${mins} minutes`;
  $("cmpScope").innerHTML = `Compared over the <b>${dur}</b> both stations were logging, since ${esc(when(d.firstBoth))}.`;
  const p = d.packets, n = d.nodes, total = p.both + p.a + p.b;
  const tile = (label, value, detail) => `<div class="kpi"><div class="k">${label}</div><div class="v">${value}</div><div class="d">${detail}</div></div>`;
  $("cmpKpis").innerHTML = [
    tile("Heard by both", nf.format(p.both), `${total ? fmt((100 * p.both) / total, 0) : 0} % of the ${nf.format(total)} packets either heard`),
    tile(`Only ${A}`, nf.format(p.a), "packets the other station missed"),
    tile(`Only ${B}`, nf.format(p.b), "packets the other station missed"),
    tile("Nodes", `${nf.format(n.both)}<small> shared</small>`, `${nf.format(n.a)} only ${A} · ${nf.format(n.b)} only ${B}`),
    tile("Same packets", d.snrDiff == null ? "—" : `${d.snrDiff > 0 ? "+" : ""}${fmt(d.snrDiff, 1)}<small> dB SNR</small>`,
      `${B} vs ${A}, median${d.arrivalGap != null ? ` · ${B} logged it ${fmt(Math.abs(d.arrivalGap), 1)} s ${d.arrivalGap >= 0 ? "later" : "earlier"}` : ""}`),
  ].join("");
  const label = { a: `Only ${d.aName}`, both: "Both", b: `Only ${d.bName}` };
  $("cmpLegend").innerHTML = ["a", "both", "b"].map((g) => `<span><i style="background:${CMP_COL[g]}"></i>${esc(label[g])}</span>`).join("") + '<span><i class="gapkey"></i>not both logging</span>';
  trafficChart($("cmpChart"), {
    bucket: d.bucket, groups: ["a", "both", "b"], gapText: "Both stations weren't logging",
    series: d.series.map((s) => ({ t: s.t, covered: s.covered, total: s.covered ? s.a + s.both + s.b : null, byGroup: s.covered ? { a: s.a, both: s.both, b: s.b } : null })),
  }, (g) => CMP_COL[g], (g) => label[g]);
  const relayName = (r) => (r === "direct" ? "direct (no relay)" : r === "unknown" ? "unknown" : r.startsWith("!") ? (S_NAME(r)) : `relay ${r}`);
  const rel = (rows) => rows.length ? rows.map((r) => `<div><span>${esc(relayName(r.relay))}</span> <b>${nf.format(r.n)}</b></div>`).join("") : '<p class="muted">—</p>';
  $("cmpRelays").innerHTML = `<div class="cmp-two"><div><h4>${A}</h4>${rel(d.topRelays.a)}</div><div><h4>${B}</h4>${rel(d.topRelays.b)}</div></div>`;
  $("cmpPaths").innerHTML = `<p>Of the ${nf.format(p.both)} packets both heard, <b>${nf.format(d.relaysSame)}</b> reached both through the same last relay and <b>${nf.format(d.relaysDifferent)}</b> through different ones.</p>` +
    (d.relayPairs.length ? `<div class="bars">${d.relayPairs.map((x) => `<div class="r" style="grid-template-columns:1fr auto"><span class="l">${esc(d.aName)} via ${esc(relayName(x.a))}, ${esc(d.bName)} via ${esc(relayName(x.b))}</span><span class="n">${nf.format(x.n)}</span></div>`).join("")}</div>` : "");
  const sig = (x) => (x ? `<td class="num">${nf.format(x.packets)}</td><td class="num">${x.snr == null ? "—" : fmt(x.snr, 1)}</td><td class="num">${x.rssi == null ? "—" : fmt(x.rssi, 0)}</td>` : '<td class="num muted">—</td><td></td><td></td>');
  $("cmpNodes").innerHTML = `<table class="nodes compact cmp"><thead><tr><th rowspan="2">Node</th><th colspan="3" class="grp">${A}</th><th colspan="3" class="grp">${B}</th><th rowspan="2" class="num">Both</th><th rowspan="2" class="num" title="${B} minus ${A}, median over packets both heard">Δ SNR</th></tr>
    <tr><th class="num">pkts</th><th class="num">SNR</th><th class="num">RSSI</th><th class="num">pkts</th><th class="num">SNR</th><th class="num">RSSI</th></tr></thead><tbody>${d.nodesTable.map((r) => `
    <tr class="${!r.a ? "only-b" : !r.b ? "only-a" : ""}"><td><a class="nlink" href="${nodeHref(r.id)}">${esc(r.name)}</a>${!r.a ? ` <span class="tag">only ${B}</span>` : !r.b ? ` <span class="tag">only ${A}</span>` : ""}</td>
      ${sig(r.a)}${sig(r.b)}<td class="num">${nf.format(r.both)}</td><td class="num">${r.snrDiff == null ? "—" : `${r.snrDiff > 0 ? "+" : ""}${fmt(r.snrDiff, 1)}`}</td></tr>`).join("")}</tbody></table>`;
  $("cmpNote").textContent = `SNR and RSSI are each station's best reception of each packet (median per node). ` +
    (d.aHw && d.bHw && d.aHw !== d.bHw ? `The two stations use different radios (${prettyHw(d.aHw)} and ${prettyHw(d.bHw)}), which estimate SNR and RSSI differently, so compare the stations' readings with care; differences between nodes at the same station are reliable. ` : "") +
    "Arrival times are when each radio logged the packet (the computers' clocks are internet-synced, so the gap includes a small clock difference).";
}
const S_NAME = (id) => (A.data?.nodes?.find((n) => n.id === id)?.name) || id;
for (const [el, k] of [["cmpA", "a"], ["cmpB", "b"]]) {
  $(el).addEventListener("change", () => { CMP[k] = $(el).value; if (CMP.a === CMP.b) return; renderCompare(A.range); });
}

/* ---------------------------------------------------------------- page */
const A = { data: null, range: "7d" };
try { A.range = localStorage.getItem("meshdash.range") || "7d"; } catch { /* storage blocked */ }

function renderScope(d) {
  const k = d.kpis;
  const span = `${when(d.since)} – now`;
  const started = d.firstEver > Date.now() / 1000 - ({ "24h": 1, "7d": 7, "30d": 30, "90d": 90 }[d.range] || 0) * 86400 && d.range !== "all";
  $("scope").innerHTML = `Showing <b>${esc(span)}</b>: ${fmt(k.coveredHours)} hour${k.coveredHours === 1 ? "" : "s"} logged` +
    (started ? `. Logging began ${esc(when(d.firstEver))}, so this range isn't full yet.` : ".") +
    (d.combined
      ? ` Combined view of ${(d.stationNames || []).length} listening stations (${(d.stationNames || []).map(esc).join(", ")}): a packet heard by more than one counts once. Channel utilization and nodes online are <b>${esc(d.homeName || "this PC's radio")}</b>'s own measurements.`
      : d.stationIsLocal === false
      ? ` Everything counts what the listening station <b>${esc(d.stationName || d.station)}</b> heard.`
      : ` Everything counts what <i>our</i> radio${d.stationName ? ` (${esc(d.stationName)})` : ""} heard.`);
}

function renderKpis(d) {
  const k = d.kpis;
  const tile = (label, value, detail) => `<div class="kpi"><div class="k">${label}</div><div class="v">${value}</div><div class="d">${detail}</div></div>`;
  $("kpis").innerHTML = [
    tile("Packets heard", nf.format(k.packets), `${fmt(k.perHour, 1)} per logged hour`),
    tile("Nodes heard", nf.format(k.nodes), `${nf.format(k.newNodes)} new in this range`),
    tile("Text messages", nf.format(k.messages), `${nf.format(k.conversations)} conversation${k.conversations === 1 ? "" : "s"}`),
    tile("Channel utilization", k.chUtilAvg == null ? "—" : `${fmt(k.chUtilAvg, 1)}<small>%</small>`, "average airtime in use"),
    tile("Logged", `${fmt(k.coveredHours)}<small> h</small>`, k.spanHours ? `${Math.min(100, Math.round((k.coveredHours / Math.max(1, Math.ceil(k.spanHours))) * 100))}% of the range` : ""),
  ].join("");
}

function renderAll() {
  const d = A.data; if (!d) return;
  document.querySelectorAll(".bucket").forEach((e) => (e.textContent = d.bucket));
  renderScope(d); renderKpis(d);
  $("trafficLegend").innerHTML = d.groups.map((g) => `<span><i style="background:${gcol(g)}"></i>${esc(g)}</span>`).join("") + '<span><i class="gapkey"></i>not logged</span>';
  trafficChart($("traffic"), d);
  lineChart($("util"), d, "chUtil", { unit: "%", digits: 1, label: "Channel utilization" });
  lineChart($("online"), d, "online", { digits: 0, label: "Nodes online" });
  heatmap($("heat"), d);
  renderTopology(d.range);
  renderAirtime(d.range);
  renderMatrix(d.range);
  renderCompare(d.range);
  renderPrivacy(d);
  renderInsights(d.range);
  renderNodes(d);
  renderTypes(d);
  renderFw28(d);
  bars($("ports"), d.ports.map((p) => ({ label: pretty(p.port), value: p.count, color: gcol(p.group) })),
    { title: (r) => `<b>${esc(r.label)}</b><br>${nf.format(r.value)} packets` });
  const hopsKnown = d.hops.filter((h) => h.hops != null), unknown = d.hops.find((h) => h.hops == null);
  bars($("hops"), [...hopsKnown.map((h) => ({ label: h.hops === 0 ? "0 (direct)" : `${h.hops}`, value: h.count })), ...(unknown ? [{ label: "unknown", value: unknown.count, color: "var(--text-muted)" }] : [])],
    { title: (r) => `<b>${esc(r.label)} hop${r.label === "1" ? "" : "s"}</b><br>${nf.format(r.value)} packets${r.label === "unknown" ? "<br><span class='t'>sender's firmware doesn't report hop count</span>" : ""}` });
  bars($("relays"), d.relays.map((r) => {
    const c = r.candidates;
    const label = c.length === 1 ? c[0].name : c.length > 1 ? `…${r.byte} (${c.length} candidates)` : `…${r.byte} (unknown node)`;
    return { label, value: r.count, cands: c, labelHtml: c.length === 1 ? `<a class="nlink" href="${nodeHref(c[0].id)}">${esc(c[0].name)}</a>` : null };
  }), { empty: "No relayed packets yet.", title: (r) => `<b>${esc(r.label)}</b><br>${nf.format(r.value)} packets relayed to us${r.cands.length > 1 ? `<br><span class="t">${r.cands.map((c) => esc(c.name)).join(", ")}</span>` : ""}` });
  bars($("talkers"), d.talkers.map((t) => ({ label: t.name, value: t.count, labelHtml: `<a class="nlink" href="${nodeHref(t.id)}">${esc(t.name)}</a>` })), { empty: "No text messages in this range." });
  $("newnodes").innerHTML = d.newNodes.length ? `<div class="bars">${d.newNodes.slice(0, 15).map((n) => `<div class="r" style="grid-template-columns:1fr auto"><span class="l"><a class="nlink" href="${nodeHref(n.id)}">${esc(n.name)}</a></span><span class="n" style="min-width:auto">${esc(when(n.firstEver))}</span></div>`).join("")}${d.newNodes.length > 15 ? `<p class="muted">+${d.newNodes.length - 15} more</p>` : ""}</div>` : '<p class="muted">No new nodes in this range.</p>';
  CONV_SHOWN = 20; renderConvs(d);
  if (!$("traffic-table").hidden) renderTrafficTable();
}

function renderTrafficTable() {
  const d = A.data;
  const rows = d.series.map((s) => `<tr><td>${esc(d.bucket === "hour" ? when(s.t) : new Date(s.t * 1000).toLocaleDateString())}</td>${s.covered ? d.groups.map((g) => `<td class="num">${nf.format(s.byGroup[g])}</td>`).join("") + `<td class="num"><b>${nf.format(s.total)}</b></td>` : `<td colspan="${d.groups.length + 1}" class="muted">not logged</td>`}</tr>`).join("");
  $("traffic-table").innerHTML = `<div class="tablewrap" style="max-height:320px;overflow:auto"><table class="nodes"><thead><tr><th>${d.bucket === "hour" ? "Hour" : "Day"}</th>${d.groups.map((g) => `<th class="num">${esc(g)}</th>`).join("")}<th class="num">Total</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

async function load(range) {
  setRange(range);
  document.querySelector(".wrap").classList.add("loading");
  try {
    const r = await fetch(`/api/analytics?range=${encodeURIComponent(range)}${stationQS()}`);
    A.data = await r.json();
    if (A.data.error) throw new Error(A.data.error);
    renderAll();
  } catch (e) {
    $("scope").textContent = `Couldn't load analytics: ${e.message}`;
  } finally {
    document.querySelector(".wrap").classList.remove("loading");
  }
}

/* ---------------------------------------------------------------- routing: #node=<id> shows the node view */
const nodeHref = (id) => `#node=${encodeURIComponent(id)}`;
function setRange(range) {
  A.range = range;
  try { localStorage.setItem("meshdash.range", range); } catch { /* storage blocked */ }
  for (const b of $("ranges").children) b.classList.toggle("on", b.dataset.range === range);
}
function route() {
  const m = location.hash.match(/node=([^&]+)/);
  const nid = m ? decodeURIComponent(m[1]) : null;
  $("nodeView").hidden = !nid; $("overview").hidden = !!nid;
  hideTip();
  if (nid) { openNode(nid, A.range); window.scrollTo(0, 0); return; }
  if (!A.data || A.data.range !== A.range) load(A.range); else renderAll();
}
addEventListener("hashchange", route);
$("ranges").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-range]"); if (!b) return;
  setRange(b.dataset.range); route();
});
$("nodeFilter").addEventListener("input", () => { renderNodes(A.data); renderTypes(A.data); });
$("nodes").addEventListener("click", (e) => {
  if (e.target.id === "nodesAll") { NT.showAll = !NT.showAll; return renderNodes(A.data); }
  const row = e.target.closest("tbody tr[data-id]");
  if (row) { location.hash = nodeHref(row.dataset.id); return; }
  const th = e.target.closest("th[data-k]"); if (!th) return;
  if (NT.sort === th.dataset.k) NT.dir = -NT.dir; else { NT.sort = th.dataset.k; NT.dir = th.dataset.k === "name" ? 1 : -1; }
  renderNodes(A.data);
});
$("nodes").addEventListener("pointermove", (e) => {
  const m = e.target.closest("[data-mix]"); if (!m) return hideTip();
  const n = A.data.nodes.find((x) => x.id === m.closest("tr").dataset.id);
  showTip(e, `<b>${esc(n.name)}</b><br>${A.data.groups.filter((g) => n.byGroup[g]).map((g) => `<span class="sw" style="background:${gcol(g)}"></span>${esc(g)} <b>${nf.format(n.byGroup[g])}</b>`).join("<br>")}`);
});
$("nodes").addEventListener("pointerleave", hideTip);
$("convs").addEventListener("toggle", (e) => {
  const d = e.target; if (!d.open || d.dataset.filled) return;
  d.querySelector(".transcript").innerHTML = transcript(A.data.conversations[+d.dataset.i]);
  d.dataset.filled = "1";
}, true);
$("convs").addEventListener("click", (e) => { if (e.target.id === "convMore") { CONV_SHOWN += 20; renderConvs(A.data); } });
document.querySelector("[data-table=traffic]").addEventListener("click", (e) => {
  const t = $("traffic-table"); t.hidden = !t.hidden; e.target.textContent = t.hidden ? "Table" : "Chart only";
  if (!t.hidden) renderTrafficTable();
});
let resizeT = null;
const rerender = () => (location.hash.includes("node=") ? renderNode() : renderAll());
addEventListener("resize", () => { clearTimeout(resizeT); resizeT = setTimeout(rerender, 150); });
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", rerender);

setRange(A.range);
// node.js (loaded after this file) defines openNode/renderNode; route once it's there
addEventListener("DOMContentLoaded", route);
