"use strict";
/* Visualizations → Coverage: what a moving station heard along its route (drive.py, /api/analytics/drive).

   The route is binned into squares. Each square is shaded by the chosen measure, in five steps of one hue
   (light = less, dark = more); squares the station passed through without hearing anything are outlined
   in amber: those are the holes. The route is drawn on top, dashed amber where nothing was heard within a
   minute. Click a square for what was heard there. */

const DV = { data: null, key: null, map: null, layer: null, sel: null, fitted: null,
  metric: (() => { try { return localStorage.getItem("meshdash.dvMetric") || "radios"; } catch { return "radios"; } })(),
  bin: (() => { try { return Number(localStorage.getItem("meshdash.dvBin")) || 250; } catch { return 250; } })(),
  sent: (() => { try { return localStorage.getItem("meshdash.dvSent") !== "0"; } catch { return true; } })(),
  dim: (() => { try { return localStorage.getItem("meshdash.dvDim") !== "0"; } catch { return true; } })() };
const DV_METRICS = {
  radios: { label: "Radios heard", fmt: (v) => nf.format(v), get: (c) => c.radios },
  perMin: { label: "Receptions per minute there", fmt: (v) => fmt(v, 1), get: (c) => c.perMin },
  bestSnr: { label: "Best signal (SNR)", fmt: (v) => `${fmt(v, 1)} dB`, get: (c) => c.bestSnr },
  direct: { label: "Radios heard directly", fmt: (v) => nf.format(v), get: (c) => c.direct },
};
const DV_STEPS = [0.16, 0.3, 0.44, 0.6, 0.76];   // fill opacity of the accent colour, light to dark

async function drawDrive(range) {
  const key = `${range}|${DV.bin}|${window.LK_STATION || ""}`;
  if (DV.key !== key) {
    $("dvNote").textContent = "Loading…";
    try {
      const d = await fetch(`/api/analytics/drive?range=${encodeURIComponent(range)}&bin=${DV.bin}${stationQS()}`).then((r) => r.json());
      if (d.error) throw new Error(d.error);
      dvDecoy(d);
      DV.data = d; DV.key = key; DV.sel = null;
    } catch (e) { $("dvNote").textContent = `Couldn't load coverage: ${e.message}`; return; }
  }
  if (!DV.map) {
    DV.map = L.map($("driveMap"), { zoomControl: true, attributionControl: true });
    lkBaseTiles().addTo(DV.map);
    DV.layer = L.layerGroup().addTo(DV.map);
  }
  DV.map.invalidateSize();
  $("dvMetric").value = DV.metric;
  $("dvBin").innerHTML = DV.data.bins.map((b) => `<option value="${b}"${b === DV.bin ? " selected" : ""}>${b < 1000 ? `${b} m` : `${b / 1000} km`}</option>`).join("");
  renderDrive();
}

function dvSteps(values) {
  // five classes at quantile breaks of the values shown (ties collapse, so fewer classes is fine)
  const v = values.filter((x) => x != null).sort((a, b) => a - b);
  if (!v.length) return [];
  const q = (p) => v[Math.min(v.length - 1, Math.floor(p * v.length))];
  const breaks = [...new Set([q(0.2), q(0.4), q(0.6), q(0.8)])];
  const ranges = [];
  let lo = v[0];
  for (const b of [...new Set([...breaks, v[v.length - 1]])]) {
    if (lo === undefined || b < lo) continue;
    ranges.push([lo, b]);
    lo = v.find((x) => x > b);  // the next class starts at the next value present, so classes never overlap
  }
  return ranges;
}

// ?anon=1: move everything to a decoy place (topology.js decoyShift), centred on the routes
function dvDecoy(d) {
  const pts = (d.tracks || []).flatMap((t) => t.segments.flat());
  const sh = pts.length ? decoyShift([median(pts.map((p) => p[0])), median(pts.map((p) => p[1]))]) : null;
  if (!sh) return;
  // names too (Station 1, Radio 4...), and never message text: a test message can carry real coordinates
  const label = new Map(), n = { Station: 0, Radio: 0 }, stations = new Set(d.stations.map((s) => s.id));
  const nm = (id) => { if (!label.has(id)) { const w = stations.has(id) ? "Station" : "Radio"; label.set(id, `${w} ${++n[w]}`); } return label.get(id); };
  for (const s of d.stations) s.name = nm(s.id);
  for (const c of d.cells) for (const k of ["topRadios", "directRadios", "relays"]) for (const r of c[k] || []) r[1] = String(r[0]).startsWith("!") ? nm(r[0]) : "an unresolved relay";
  for (const s of d.sent || []) {
    for (const h of s.heardBy) h.name = nm(h.station);
    for (const r of s.relays) r[1] = String(r[0]).startsWith("!") ? nm(r[0]) : "an unresolved relay";
    s.what = /^Lorakeet ping/.test(s.what || "") ? "ping" : /_APP$/.test(s.what || "") || s.what === "packet" ? s.what : "a text message";
  }
  const mv = (p) => { const [a, b] = sh([p[0], p[1]]); p[0] = a; p[1] = b; };
  for (const t of d.tracks) for (const seg of t.segments) for (const p of seg) mv(p);
  for (const c of d.cells) { [c.lat, c.lon] = sh([c.lat, c.lon]); c.bounds = c.bounds.map((b) => sh(b)); }
  for (const s of d.sent || []) [s.lat, s.lon] = sh([s.lat, s.lon]);
}

function renderDrive() {
  const d = DV.data, m = DV_METRICS[DV.metric], L_ = DV.layer;
  L_.clearLayers();
  const accent = css("--accent"), warn = css("--warning"), muted = css("--text-muted");
  if (!d.stations.length) {
    $("dvLegend").innerHTML = "";
    $("dvNote").textContent = "No moving station in this range. A station with [station] mobile = true in its lorakeet.toml logs its own GPS track; drive around with it and its route appears here, shaded by what it heard.";
    $("dvCell").innerHTML = '<p class="muted">Nothing to show yet.</p>';
    return;
  }
  // drive.py judges silence against each station's own reception rate: "hole" = inside a silence long enough to
  // mean no coverage; "brief" = crossed between packets, too quickly to tell
  // Explored vs unknown: dim everything outside the squares a station actually passed through. Blank map means
  // nobody has been there, not "no coverage" (what Helium's mappers warn about unshaded hexes).
  if (DV.dim && d.cells.length) {
    const lat = d.cells.map((c) => c.lat), lon = d.cells.map((c) => c.lon);
    const pad = 3, outer = [[Math.min(...lat) - pad, Math.min(...lon) - pad], [Math.min(...lat) - pad, Math.max(...lon) + pad],
      [Math.max(...lat) + pad, Math.max(...lon) + pad], [Math.max(...lat) + pad, Math.min(...lon) - pad]];
    const holesRings = d.cells.map((c) => [c.bounds[0], [c.bounds[0][0], c.bounds[1][1]], c.bounds[1], [c.bounds[1][0], c.bounds[0][1]]]);
    L.polygon([outer, ...holesRings], { stroke: false, fillColor: css("--surface-2"), fillOpacity: 0.55, fillRule: "evenodd", interactive: false }).addTo(L_);
  }
  const heard = d.cells.filter((c) => c.receptions > 0), holes = d.cells.filter((c) => c.status === "hole"),
    brief = d.cells.filter((c) => c.status === "brief");
  for (const c of brief) {
    L.rectangle(c.bounds, { color: muted, weight: 0.8, dashArray: "2 4", opacity: 0.6, fill: false })
      .bindTooltip(`<b>Nothing heard, too briefly to tell</b><br>${fmt(c.minutes * 60, 0)} s here, between packets`, { sticky: true })
      .on("click", () => dvSelect(c)).addTo(L_);
  }
  const steps = dvSteps(heard.map(m.get));
  const stepOf = (v) => { if (v == null) return 0; const i = steps.findIndex((r) => v <= r[1]); return i < 0 ? steps.length - 1 : i; };
  for (const c of holes) {
    L.rectangle(c.bounds, { color: warn, weight: 1.5, dashArray: "4 3", fill: true, fillOpacity: 0.04, fillColor: warn })
      .bindTooltip(`<b>Nothing heard: a gap</b><br>${fmt(c.minutes, 1)} min here, inside a long silence`, { sticky: true })
      .on("click", () => dvSelect(c)).addTo(L_);
  }
  for (const c of heard) {
    const v = m.get(c), op = DV_STEPS[Math.round(stepOf(v) * (DV_STEPS.length - 1) / Math.max(1, steps.length - 1))];
    L.rectangle(c.bounds, { color: c === DV.sel ? css("--text-primary") : accent, weight: c === DV.sel ? 2.5 : 0.6, opacity: 0.9,
      fillColor: accent, fillOpacity: op })
      .bindTooltip(`<b>${esc(m.label)}: ${v == null ? "—" : esc(m.fmt(v))}</b><br>${nf.format(c.receptions)} receptions · ${c.radios} radios · ${fmt(c.minutes, 1)} min here`, { sticky: true })
      .on("click", () => dvSelect(c)).addTo(L_);
  }
  // the route, dashed amber inside silences long enough to mean a gap (drive.py silentAfterS)
  const all = [];
  for (const t of d.tracks) for (const seg of t.segments) {
    const pts = seg.map((p) => [p[0], p[1]]);
    all.push(...pts);
    if (pts.length > 1) L.polyline(pts, { color: muted, weight: 2, opacity: 0.8, interactive: false }).addTo(L_);
    let run = [];
    const flush = () => { if (run.length > 1) L.polyline(run, { color: warn, weight: 3, dashArray: "6 5", opacity: 0.95, interactive: false }).addTo(L_); run = []; };
    seg.forEach((p, i) => { if (p[3] === 0) { if (!run.length && i) run.push([seg[i - 1][0], seg[i - 1][1]]); run.push([p[0], p[1]]); } else { if (run.length) run.push([p[0], p[1]]); flush(); } });
    flush();
  }
  // the other direction: where the station TRANSMITTED, and whether that got out (drive.py _sent)
  const sentCol = { station: css("--good"), mesh: accent, none: warn };
  const sentLabel = { station: "heard by one of our stations", mesh: "repeated by the mesh (not heard by our stations)", none: "no sign it was heard" };
  if (DV.sent) for (const s of d.sent || []) {
    const col = sentCol[s.result];
    L.circleMarker([s.lat, s.lon], { radius: 5, color: col, weight: 2, fillColor: col, fillOpacity: s.result === "none" ? 0 : 0.85 })
      .bindTooltip(`<b>${esc(s.what)}</b> · ${esc(when(s.ts))}<br>${esc(sentLabel[s.result])}` +
        (s.heardBy.length ? `<br>${s.heardBy.map((h) => `${esc(h.name)}: ${h.hops == null ? "?" : h.hops} hop${h.hops === 1 ? "" : "s"}${h.snr != null ? `, SNR ${fmt(h.snr, 1)}` : ""}`).join("<br>")}` : "") +
        (s.relays.length ? `<br><span class="t">repeated by ${s.relays.map((r) => esc(r[1])).join(", ")}</span>` : ""), { sticky: true })
      .addTo(L_);
  }
  for (const t of d.tracks) {  // start and end of each station's route
    const segs = t.segments.filter((s) => s.length), first = segs[0]?.[0], last = segs.at(-1)?.at(-1);
    const name = d.stations.find((s) => s.id === t.station)?.name || t.station;
    if (first) L.circleMarker([first[0], first[1]], { radius: 5, color: css("--surface-1"), weight: 2, fillColor: css("--good"), fillOpacity: 1 }).bindTooltip(`${esc(name)}: start ${esc(when(first[2]))}`).addTo(L_);
    if (last) L.circleMarker([last[0], last[1]], { radius: 5, color: css("--surface-1"), weight: 2, fillColor: css("--critical"), fillOpacity: 1 }).bindTooltip(`${esc(name)}: end ${esc(when(last[2]))}`).addTo(L_);
  }
  if (all.length && DV.fitted !== DV.key) {
    const ins = typeof vizInset === "function" ? vizInset() : { top: 0, right: 0, bottom: 0, left: 0 };
    DV.map.fitBounds(L.latLngBounds(all), { paddingTopLeft: [ins.left + 24, ins.top + 24], paddingBottomRight: [ins.right + 24, ins.bottom + 24], maxZoom: 15 });
    DV.fitted = DV.key;
  }
  // legend + note
  $("dvLegend").innerHTML = steps.map((r, i) => `<span><i style="background:${accent};opacity:${DV_STEPS[Math.round(i * (DV_STEPS.length - 1) / Math.max(1, steps.length - 1))]}"></i>${esc(m.fmt(r[0]))}${r[1] !== r[0] ? `–${esc(m.fmt(r[1]))}` : ""}</span>`).join("") +
    (DV.sent && (d.sent || []).length ? `<span><i class="dot" style="background:${css("--good")}"></i>sent: reached our station</span><span><i class="dot" style="background:${accent}"></i>sent: repeated by mesh</span><span><i class="dot hollow" style="border-color:${warn}"></i>sent: no sign</span>` : "") +
    (DV.dim ? '<span><i class="unexplored"></i>dimmed: not explored (unknown)</span>' : "") +
    `<span><i class="hole"></i>gap: nothing heard</span><span><i class="brief"></i>too brief to tell</span><span><i style="background:${warn};height:3px"></i>silent stretch</span>`;
  const s = d.stats, names = d.stations.map((x) => x.name).join(", ");
  // distance moved: steps under 25 m (the logger's "moved" threshold) are GPS jitter while parked, not travel
  const km = d.tracks.reduce((t, tr) => t + tr.segments.reduce((u, seg) => u + seg.slice(1).reduce((w, p, i) => { const s = dvKm(seg[i], p); return w + (s >= 0.025 ? s : 0); }, 0), 0), 0);
  $("dvNote").textContent = `${names}: ${nf.format(s.receptions)} receptions from ${s.radios} radios along ${fmt(km, 1)} km of route (${fmt(s.minutes / 60, 1)} h logged). ` +
    `Each reception is placed at the station's latest GPS fix (within 12 min), never interpolated. Squares: ${DV.bin < 1000 ? `${DV.bin} m` : `${DV.bin / 1000} km`}.` +
    (d.stations.some((x) => x.source === "packets") ? " Some stations have no debug log, so only first copies of packets count there." : "") +
    ((d.sent || []).length ? (() => { const c = (k) => d.sent.filter((x) => x.result === k).length;
      return ` Sent from the route: ${d.sent.length} packets, ${c("station")} heard by one of our stations, ${c("mesh")} only repeated by the mesh, ${c("none")} with no sign of being heard.`; })() : "") +
    d.tracks.filter((t) => t.silentAfterS).map((t) => ` A gap means nothing heard for over ${fmt(t.silentAfterS / 60, 1)} min: at ${d.stations.find((x) => x.id === t.station)?.name || t.station}'s usual ${fmt(t.ratePerMin, 1)} receptions/min, it would have heard something 9 times out of 10. Shorter quiet spells are drawn faintly ("too brief to tell").`).join("");
  if (DV.sel) dvSelect(d.cells.find((c) => c.lat === DV.sel.lat && c.lon === DV.sel.lon) || null, true);
}

function dvKm(a, b) {
  const R = 6371, dLat = (b[0] - a[0]) * Math.PI / 180, dLon = (b[1] - a[1]) * Math.PI / 180;
  const x = Math.sin(dLat / 2) ** 2 + Math.cos(a[0] * Math.PI / 180) * Math.cos(b[0] * Math.PI / 180) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(x));
}

function dvSelect(c, quiet = false) {
  DV.sel = c;
  if (!c) { $("dvCell").innerHTML = '<p class="muted">Click a square on the map.</p>'; return; }
  const link = (id, name) => (id.startsWith("!") && !ANON ? `<a href="${esc(nodeHref(id))}">${esc(name)}</a>` : esc(name));  // anon: no real ids in links
  const list = (rows, val) => rows.length ? `<ul class="dv-list">${rows.map((r) => `<li>${link(r[0], r[1])}${val ? `<span class="muted">${esc(val(r))}</span>` : ""}</li>`).join("")}</ul>` : '<p class="muted">none</p>';
  $("dvCell").innerHTML = c.receptions === 0
    ? `<dl class="dv-kv"><dt>Time here</dt><dd>${fmt(c.minutes, 1)} min</dd></dl><p>Nothing heard in this square: a gap in coverage (or the radio was busy elsewhere). Worth another pass to confirm.</p>`
    : `<dl class="dv-kv">
        <dt>Time here</dt><dd>${fmt(c.minutes, 1)} min</dd>
        <dt>Receptions</dt><dd>${nf.format(c.receptions)}${c.perMin != null ? ` · ${fmt(c.perMin, 1)}/min` : ""}</dd>
        <dt>Radios</dt><dd>${c.radios} heard, ${c.direct} directly</dd>
        <dt>Signal</dt><dd>best ${fmt(c.bestSnr, 1)} dB SNR${c.medSnr != null ? `, median ${fmt(c.medSnr, 1)}` : ""}${c.bestRssi != null ? ` · ${c.bestRssi} dBm` : ""}</dd>
        <dt>Heard</dt><dd>${esc(when(c.first))} – ${esc(when(c.last))}</dd>
      </dl>
      <div class="dv-h">Heard directly</div>${list(c.directRadios)}
      <div class="dv-h">Most heard</div>${list(c.topRadios, (r) => `${r[2]}×`)}
      <div class="dv-h">Carried by (last relay)</div>${list(c.relays, (r) => `${r[2]}×`)}`;
  if (!quiet) { $("vzCell").open = true; renderDrive(); }
}

$("dvMetric").addEventListener("change", (e) => {
  DV.metric = e.target.value;
  try { localStorage.setItem("meshdash.dvMetric", DV.metric); } catch { /* storage blocked */ }
  renderDrive();
});
$("dvBin").addEventListener("change", (e) => {
  DV.bin = Number(e.target.value);
  try { localStorage.setItem("meshdash.dvBin", String(DV.bin)); } catch { /* storage blocked */ }
  drawDrive(A.range);
});
$("dvSent").checked = DV.sent;
$("dvSent").addEventListener("change", (e) => {
  DV.sent = e.target.checked;
  try { localStorage.setItem("meshdash.dvSent", DV.sent ? "1" : "0"); } catch { /* storage blocked */ }
  renderDrive();
});
$("dvDim").checked = DV.dim;
$("dvDim").addEventListener("change", (e) => {
  DV.dim = e.target.checked;
  try { localStorage.setItem("meshdash.dvDim", DV.dim ? "1" : "0"); } catch { /* storage blocked */ }
  renderDrive();
});
