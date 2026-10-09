"use strict";
/* Analytics overview sections: mesh health & security, coverage map, traceroute explorer.
   Uses helpers from analytics.js (esc, fmt, nf, ago, when, css, $, nodeHref, showTip, hideTip) and prov.js. */

const INS = { range: null, health: null, cov: null, covMap: null, tr: null, trMap: null, est: null };

async function insightsFetch(path) {
  const r = await fetch(path).then((x) => x.json());
  if (r.error) throw new Error(r.error);
  return r;
}
async function estimates() {
  if (!INS.est) INS.est = insightsFetch(`/api/insights/estimates?${stationQS().slice(1)}`).catch(() => ({ estimates: [], assumptions: [] }));
  return INS.est;
}

async function renderInsights(range) {
  if (INS.range !== range) { INS.range = range; INS.health = INS.cov = INS.tr = null; INS.est = null; }
  renderHealth(range); renderCoverage(range); renderTraces(range);
}

/* ---------------------------------------------------------------- health */
const KIND_GROUP = (k) => k.replace(/^chatty-.*/, "chatty");
async function renderHealth(range) {
  const el = $("healthBody");
  try { INS.health = INS.health || await insightsFetch(`/api/insights/health?range=${encodeURIComponent(range)}${stationQS()}`); }
  catch (e) { el.innerHTML = `<p class="muted">Couldn't load: ${esc(e.message)}</p>`; return; }
  const h = INS.health;
  // group repeated findings of one kind into a single row with the node list
  const groups = new Map();
  for (const f of h.findings) {
    const g = groups.get(f.kind + "|" + f.title.replace(/\d+/g, "#")) || { ...f, nodes: [], items: [] };
    if (f.node) g.nodes.push(f.node);
    g.items.push(f); groups.set(f.kind + "|" + f.title.replace(/\d+/g, "#"), g);
  }
  const rows = [...groups.values()];
  $("healthSummary").innerHTML = rows.length
    ? `${h.counts.warn || 0} warning${h.counts.warn === 1 ? "" : "s"} and ${h.counts.info || 0} note${h.counts.info === 1 ? "" : "s"} across ${rows.length} issue${rows.length === 1 ? "" : "s"}. ${provLegend()}`
    : `No issues found in this range. ${provLegend()}`;
  el.innerHTML = rows.map((g) => {
    const multi = g.items.length > 1;
    const sev = g.items.some((i) => i.severity === "warn") ? "warn" : "info";
    const title = multi ? `${g.items[0].title.replace(/\d+/, (m) => {
      const vals = [...new Set(g.items.map((i) => (i.title.match(/\d+/) || [])[0]).filter(Boolean))].sort((a, b) => a - b);
      return vals.length > 1 ? `${vals[0]}–${vals.at(-1)}` : m;
    })} · ${g.items.length} nodes` : g.title;
    const ev = (f) => f.evidence.map((e) => `<span class="ev">${esc(e.label)}: <b>${esc(e.value)}</b>${prov(e.prov)}</span>`).join("");
    return `<details class="finding sev-${sev}" ${g.kind.startsWith("weak") || g.kind.startsWith("imperson") ? "open" : ""}>
      <summary><span class="sv">${sev === "warn" ? "⚠" : "ℹ"}</span><span class="ft">${esc(title)}</span>
        ${!multi && g.node ? `<a class="nlink" href="${nodeHref(g.node.id)}">${esc(g.node.name)}</a>` : ""}</summary>
      <div class="fd">${esc(g.detail)}${multi ? "" : `<div class="evs">${ev(g)}</div>`}
      ${multi ? `<table class="nodes compact"><tbody>${g.items.map((f) => `<tr><td>${f.node ? `<a class="nlink" href="${nodeHref(f.node.id)}">${esc(f.node.name)}</a>` : ""}</td><td>${ev(f)}</td></tr>`).join("")}</tbody></table>` : ""}</div>
    </details>`;
  }).join("") + `<p class="muted">Passive: built only from packets already logged; nothing is sent. Thresholds: airtime &gt; ${h.thresholds.airtimeWarnPct}%, channel &gt; ${h.thresholds.channelUtilWarnPct}%, broadcasts &gt; 3× the default rate, SNR drop ≥ ${h.thresholds.snrDropDb} dB. Weak keys come from the Meshtastic firmware's own list.</p>`;
}

/* ---------------------------------------------------------------- coverage */
function snrColor(snr) {
  // sequential, one hue: weak signal recedes toward the surface, strong is full blue
  const t = Math.max(0, Math.min(1, ((snr ?? -20) + 20) / 30));
  return `color-mix(in srgb, ${css("--s1")} ${Math.round(15 + 85 * t)}%, ${css("--surface-2")})`;
}
async function renderCoverage(range) {
  const node = $("covNode").value, relayed = $("covRelayed").checked, precise = $("covPrecise").checked;
  let c;
  try { c = await insightsFetch(`/api/insights/coverage?range=${encodeURIComponent(range)}${stationQS()}&relayed=${relayed ? 1 : 0}${node ? `&node=${encodeURIComponent(node)}` : ""}`); }
  catch (e) { $("covNote").textContent = `Couldn't load: ${e.message}`; return; }
  INS.cov = c;
  const sel = $("covNode"), cur = sel.value;
  sel.innerHTML = `<option value="">All nodes</option>` + c.senders.map((id) => `<option value="${esc(id)}">${esc((A.data?.nodes.find((n) => n.id === id) || {}).name || id)}</option>`).join("");
  sel.value = cur;
  const pts = c.points.filter((p) => !precise || p.precisionKm <= 0.5);
  const el = $("covMap");
  if (INS.covMap) { INS.covMap.remove(); INS.covMap = null; }
  const dark = matchMedia("(prefers-color-scheme: dark)").matches;
  INS.covMap = L.map(el, { scrollWheelZoom: true });
  lkBaseTiles().addTo(INS.covMap);
  const base = (A.data?.nodes || []).find((n) => n.isBase);
  for (const p of pts) {
    if (p.precisionKm > 0) L.circle([p.lat, p.lon], { radius: p.precisionKm * 1000, color: css("--text-muted"), weight: 1, opacity: 0.25, fillOpacity: 0.03, interactive: false }).addTo(INS.covMap);
    L.circleMarker([p.lat, p.lon], { radius: 6, color: p.hops === 0 ? css("--surface-1") : snrColor(p.snr), weight: p.hops === 0 ? 1.5 : 2, fillColor: snrColor(p.snr), fillOpacity: p.hops === 0 ? 1 : 0 })
      .bindTooltip(`<b>${esc((A.data?.nodes.find((n) => n.id === p.from_id) || {}).name || p.from_id)}</b> · ${esc(when(p.ts))}<br>
        SNR ${p.snr == null ? "—" : `${fmt(p.snr, 1)} dB`} ${prov("observed", "Measured by our radio on this packet.")} · RSSI ${esc(p.rssi ?? "—")} ${prov("observed")}<br>
        ${p.hops === 0 ? "heard directly" : `relayed, ${esc(p.hops)} hop${p.hops === 1 ? "" : "s"}: shows delivery, not our radio's range`}<br>
        position ${prov("reported", "The location the node said it was at.")}${p.precisionKm ? ` shared at ±${fmt(p.precisionKm, 1)} km precision` : " (full precision)"}`).addTo(INS.covMap);
  }
  const all = pts.map((p) => [p.lat, p.lon]);
  if (all.length) INS.covMap.fitBounds(L.latLngBounds(all).pad(0.3), { maxZoom: 13 });
  else INS.covMap.setView(...fallbackView());
  const direct = pts.filter((p) => p.hops === 0).length;
  $("covNote").innerHTML = `${direct} direct point${direct === 1 ? "" : "s"}${relayed ? `, ${pts.length - direct} relayed (hollow)` : ""}.` +
    (c.points.length !== pts.length ? ` ${c.points.length - pts.length} hidden as imprecise.` : "") +
    " Each point is where the node <i>said</i> it was; most nodes share positions rounded to a few km, shown as faint circles. For a real survey, carry a GPS node set to full precision and hop limit 0.";
}

/* ---------------------------------------------------------------- traceroute explorer */
async function renderTraces(range) {
  try { INS.tr = INS.tr || await insightsFetch(`/api/insights/traceroutes?range=${encodeURIComponent(range)}${stationQS()}`); }
  catch (e) { $("trTable").innerHTML = `<p class="muted">Couldn't load: ${esc(e.message)}</p>`; return; }
  const est = await estimates();
  const f = { result: $("trResult").value, node: $("trNode").value.trim().toLowerCase(), max: $("trMax").value };
  const runs = INS.tr.runs.filter((r) => (f.result === "all" || (f.result === "ok" ? r.answered === true : r.answered === false))
    && (!f.node || [r.targetName, r.target, ...(r.forward || []).map((h) => `${h.name} ${h.id}`)].join(" ").toLowerCase().includes(f.node))
    && (f.max === "" || r.relays == null || r.relays <= +f.max));
  const hopStr = (path) => (path ? path.map((h, i) => `${h.id ? `<a class="nlink" href="${nodeHref(h.id)}">${esc(h.name)}</a>` : "?"}${i && h.snr != null ? ` <span class="muted">${fmt(h.snr, 1)} dB</span>` : ""}`).join(" → ") : "—");
  $("trTable").innerHTML = runs.length ? `<div class="tablewrap"><table class="nodes"><thead><tr><th>When</th><th>Kind</th><th>Target</th><th>Result</th><th>Path out ${prov("reported", "Each hop's node ID and the SNR it measured, as reported in the traceroute reply.")}</th><th>Path back</th></tr></thead><tbody>
    ${runs.map((r, i) => `<tr data-i="${i}"><td class="num">${esc(when(r.ts))}</td><td>${esc(r.source === "overheard" ? "overheard" : r.origin || "manual")}</td>
      <td><a class="nlink" href="${nodeHref(r.target)}">${esc(r.targetName)}</a></td>
      <td>${r.status === "ok" ? "answered" : esc(r.status)}</td><td>${hopStr(r.forward)}</td><td>${hopStr(r.back)}</td></tr>`).join("")}</tbody></table></div>`
    : '<p class="muted">No traceroutes in this range. Scheduled traceroutes run hourly (settings under the 🔔).</p>';
  // map: draw every path through known or estimated positions
  if (INS.trMap) { INS.trMap.remove(); INS.trMap = null; }
  const pos = new Map((A.data?.nodes || []).filter((n) => n.lat != null).map((n) => [n.id, [n.lat, n.lon, false]]));
  const topoNodes = (T.data?.nodes || []); for (const n of topoNodes) if (n.lat != null && !pos.has(n.id)) pos.set(n.id, [n.lat, n.lon, false]);
  const baseN = topoNodes.find((n) => n.isBase && n.lat != null), local = topoNodes.find((n) => n.isLocal);
  if (local && baseN && !pos.has(local.id)) pos.set(local.id, [baseN.lat, baseN.lon, false]);
  for (const e of est.estimates) if (!pos.has(e.id)) pos.set(e.id, [e.lat, e.lon, true]);
  const dark = matchMedia("(prefers-color-scheme: dark)").matches;
  INS.trMap = L.map($("trMap"), { scrollWheelZoom: true });
  lkBaseTiles().addTo(INS.trMap);
  const all = [];
  runs.forEach((r) => {
    const pts = (r.forward || []).map((h) => h.id && pos.get(h.id)).filter(Boolean);
    if (pts.length < 2) return;
    all.push(...pts);
    L.polyline(pts.map((p) => [p[0], p[1]]), { color: css("--accent"), weight: 3, opacity: 0.7, dashArray: pts.some((p) => p[2]) ? "6 6" : null })
      .bindTooltip(`${esc(r.targetName)} · ${esc(when(r.ts))}${pts.some((p) => p[2]) ? "<br>dashed: passes through an estimated position" : ""}`, { sticky: true }).addTo(INS.trMap);
  });
  if (all.length) INS.trMap.fitBounds(L.latLngBounds(all.map((p) => [p[0], p[1]])).pad(0.3), { maxZoom: 12 });
  else INS.trMap.setView(...fallbackView());
}

for (const id of ["covNode", "covRelayed", "covPrecise"]) document.getElementById(id)?.addEventListener("change", () => renderCoverage(A.range));
for (const id of ["trResult", "trMax"]) document.getElementById(id)?.addEventListener("change", () => renderTraces(A.range));
document.getElementById("trNode")?.addEventListener("input", () => renderTraces(A.range));
