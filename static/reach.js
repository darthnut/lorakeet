"use strict";
/* Visualizations → Coverage → Station reach and gaps (reach.py, /api/analytics/reach).

   Each fixed station, with a line to every radio it heard directly (0 hops). Squares of ~3 km hold the radios
   that no station shown hears directly, only through relays: shaded amber by how many, those are the gaps a new
   station could fill. Click a station or a square for the radios there. Shares the Coverage map with drive.js. */

const RC = { data: null, key: null, fitted: null, sel: null,
  far: (() => { try { return Number(localStorage.getItem("meshdash.rcFar") ?? 25); } catch { return 25; } })(),
  lines: (() => { try { return localStorage.getItem("meshdash.rcLines") !== "0"; } catch { return true; } })() };
const RC_STEPS = [[1, 1], [2, 3], [4, 6], [7, Infinity]];   // radios in a gap square, light to dark
const RC_OPACITY = [0.14, 0.26, 0.4, 0.56];

async function drawReach(range) {
  const key = `${range}|${window.LK_STATION || ""}`;
  if (RC.key !== key) {
    $("dvNote").textContent = "Loading…";
    try {
      const d = await fetch(`/api/analytics/reach?range=${encodeURIComponent(range)}${stationQS()}`).then((r) => r.json());
      if (d.error) throw new Error(d.error);
      rcDecoy(d);
      RC.data = d; RC.key = key; RC.sel = null;
    } catch (e) { $("dvNote").textContent = `Couldn't load station reach: ${e.message}`; return; }
  }
  if (DV.view !== "reach") return;   // switched while it loaded
  dvEnsureMap();
  $("rcFar").value = String(RC.far);
  $("rcLines").checked = RC.lines;
  renderReach();
}

// ?anon=1: move everything to a decoy place (topology.js decoyShift), centred on the stations, and rename
function rcDecoy(d) {
  const sh = d.stations.length ? decoyShift([median(d.stations.map((s) => s.lat)), median(d.stations.map((s) => s.lon))]) : null;
  if (!sh) return;
  const n = { Station: 0, Radio: 0 }, label = new Map();
  const nm = (id, w) => { if (!label.has(id)) label.set(id, `${w} ${++n[w]}`); return label.get(id); };
  for (const s of d.stations) { s.name = nm(s.id, "Station"); [s.lat, s.lon] = sh([s.lat, s.lon]); }
  for (const s of d.skipped) s.name = nm(s.id, "Station");
  for (const r of d.radios) { r.name = nm(r.id, "Radio"); if (r.lat != null) [r.lat, r.lon] = sh([r.lat, r.lon]); }
  for (const c of d.cells) { [c.lat, c.lon] = sh([c.lat, c.lon]); c.bounds = c.bounds.map((b) => sh(b)); }
}

function rcKm(a, b) { return dvKm([a.lat, a.lon], [b.lat, b.lon]); }

function renderReach() {
  const d = RC.data, L_ = DV.layer;
  L_.clearLayers();
  const good = css("--good"), warn = css("--warning"), accent = css("--accent"), ink = css("--text-primary"), ring = css("--surface-1");
  if (!d.stations.length) {
    $("dvLegend").innerHTML = "";
    $("dvNote").textContent = "No fixed station with a known location in this range. Give a station a location ([station] location in its lorakeet.toml, or Radio → Station) and its reach appears here." +
      (d.skipped.length ? ` Not shown: ${d.skipped.map((s) => `${s.name} (${s.why})`).join(", ")}.` : "");
    $("dvCell").innerHTML = '<p class="muted">Nothing to show yet.</p>';
    return;
  }
  const byId = new Map(d.radios.map((r) => [r.id, r]));
  const near = (p) => Math.min(...d.stations.map((s) => rcKm(p, s)));
  const inRange = (p) => !RC.far || near(p) <= RC.far;
  // gap squares: radios no station shown hears directly
  const gaps = d.cells.filter((c) => c.gap && inRange(c));
  for (const c of gaps) {
    const n = c.relayed.length, step = RC_STEPS.findIndex(([lo, hi]) => n >= lo && n <= hi);
    L.rectangle(c.bounds, { color: c === RC.sel ? ink : warn, weight: c === RC.sel ? 2.5 : 0.8, opacity: 0.9,
      fillColor: warn, fillOpacity: RC_OPACITY[step] })
      .bindTooltip(`<b>${n} radio${n === 1 ? "" : "s"} only heard through relays</b><br>${esc(c.relayed.slice(0, 4).map((id) => byId.get(id)?.name || id).join(", "))}${n > 4 ? "…" : ""}`, { sticky: true })
      .on("click", () => rcSelect(c)).addTo(L_);
  }
  // direct neighbours: a line from each station, and the radio
  const placed = d.radios.filter((r) => r.lat != null);
  if (RC.lines) for (const lk of d.links) {
    const r = byId.get(lk.radio), s = d.stations.find((x) => x.id === lk.station);
    if (!r || r.lat == null || !s) continue;
    L.polyline([[s.lat, s.lon], [r.lat, r.lon]], { color: good, weight: 2, opacity: 0.75, dashArray: r.precisionKm ? "5 4" : null })
      .bindTooltip(`<b>${esc(s.name)} ↔ ${esc(r.name)}</b><br>heard directly ${nf.format(lk.receptions)}×` +
        `${lk.bestSnr != null ? `, best SNR ${fmt(lk.bestSnr, 1)} dB` : ""} · ${fmt(lk.km, 1)} km` +
        `${r.precisionKm ? `<br><span class="t">position rounded to a ~${fmt(r.precisionKm, 1)} km box</span>` : ""}`, { sticky: true })
      .addTo(L_);
  }
  for (const r of placed) {
    if (r.direct.length) {
      L.circleMarker([r.lat, r.lon], { radius: 5, color: ring, weight: 1.5, fillColor: good, fillOpacity: 1 })
        .bindTooltip(`<b>${esc(r.name)}</b><br>heard directly by ${esc(r.direct.map((id) => d.stations.find((s) => s.id === id)?.name || id).join(", "))}`)
        .addTo(L_);
    } else if (inRange(r)) {
      L.circleMarker([r.lat, r.lon], { radius: 3, color: warn, weight: 1.5, fill: false, opacity: 0.9, interactive: false }).addTo(L_);
    }
  }
  for (const s of d.stations) {
    L.circleMarker([s.lat, s.lon], { radius: 8, color: ring, weight: 2.5, fillColor: s.id === RC.sel?.id ? ink : accent, fillOpacity: 1 })
      .bindTooltip(`<b>${esc(s.name)}</b><br>${s.direct} radio${s.direct === 1 ? "" : "s"} heard directly` +
        `${s.farthestKm != null ? `, farthest ${fmt(s.farthestKm, 1)} km` : ""}${s.onlyHere ? `<br>${s.onlyHere} heard directly by no other station` : ""}`)
      .on("click", () => rcSelect(s)).addTo(L_);
  }
  if (RC.fitted !== RC.key + RC.far) {
    const pts = [...d.stations.map((s) => [s.lat, s.lon]), ...gaps.flatMap((c) => c.bounds),
      ...placed.filter((r) => r.direct.length).map((r) => [r.lat, r.lon])];
    const ins = typeof vizInset === "function" ? vizInset() : { top: 0, right: 0, bottom: 0, left: 0 };
    DV.map.fitBounds(L.latLngBounds(pts), { paddingTopLeft: [ins.left + 24, ins.top + 24], paddingBottomRight: [ins.right + 24, ins.bottom + 24], maxZoom: 13 });
    RC.fitted = RC.key + RC.far;
  }
  $("dvLegend").innerHTML = `<span><i class="dot" style="background:${accent}"></i>station</span>` +
    `<span><i class="dot" style="background:${good}"></i>heard directly</span>` +
    (RC.lines ? `<span><i style="background:${good};height:3px"></i>direct link (dashed: rounded position)</span>` : "") +
    RC_STEPS.map(([lo, hi], i) => `<span><i style="background:${warn};opacity:${RC_OPACITY[i]}"></i>${lo}${hi === Infinity ? "+" : hi !== lo ? `–${hi}` : ""} only through relays</span>`).join("") +
    `<span><i class="dot hollow" style="border-color:${warn}"></i>a radio heard only through relays</span>`;
  const s = d.stats, gapRadios = gaps.reduce((t, c) => t + c.relayed.length, 0);
  $("dvNote").textContent = `${nf.format(s.radios)} radios heard; ${nf.format(s.direct)} directly by a station shown, ${nf.format(s.relayedOnly)} only through relays. ` +
    `${nf.format(s.placed)} have shared a position. ` +
    `Within ${RC.far ? `${RC.far} km` : "any distance"} of a station: ${nf.format(gapRadios)} radios in ${gaps.length} squares of ~${fmt(d.cellKm, 0)} km that no station hears directly. ` +
    "Each radio is at the most precise position it sent in the last 30 days; most public-channel positions are rounded to a box ~3 km wide." +
    (d.skipped.length ? ` Not shown: ${d.skipped.map((x) => `${x.name} (${x.why})`).join(", ")}.` : "");
  if (RC.sel) rcSelect(RC.sel.id ? d.stations.find((x) => x.id === RC.sel.id) : d.cells.find((c) => c.lat === RC.sel.lat && c.lon === RC.sel.lon), true);
}

function rcSelect(x, quiet = false) {
  RC.sel = x || null;
  const d = RC.data, byId = new Map(d.radios.map((r) => [r.id, r]));
  if (!x) { $("dvCell").innerHTML = '<p class="muted">Click a station or a square on the map.</p>'; return; }
  const link = (id, name) => (isRadioId(id) && !ANON ? `<a href="${esc(nodeHref(id))}">${esc(name)}</a>` : esc(name));
  const stName = (id) => d.stations.find((s) => s.id === id)?.name || id;
  if (x.id) {   // a station: its direct neighbours
    const mine = d.links.filter((lk) => lk.station === x.id).sort((a, b) => (b.km ?? -1) - (a.km ?? -1));
    $("dvCell").innerHTML = `<dl class="dv-kv">
        <dt>Station</dt><dd>${esc(x.name)}${x.located === "gps" ? ' <span class="muted">(from its GPS)</span>' : ""}</dd>
        <dt>Heard directly</dt><dd>${x.direct} radios${x.placed < x.direct ? `, ${x.placed} with a position` : ""}</dd>
        ${x.farthestKm != null ? `<dt>Distance</dt><dd>farthest ${fmt(x.farthestKm, 1)} km, median ${fmt(x.medianKm, 1)} km</dd>` : ""}
        ${x.medSnr != null ? `<dt>Signal</dt><dd>median ${fmt(x.medSnr, 1)} dB SNR</dd>` : ""}
        <dt>Only here</dt><dd>${x.onlyHere} heard directly by no other station shown</dd>
      </dl>
      <div class="dv-h">Heard directly</div>${mine.length ? `<ul class="dv-list">${mine.map((lk) => {
        const r = byId.get(lk.radio);
        return `<li>${link(lk.radio, r?.name || lk.radio)}<span class="muted">${lk.km != null ? `${fmt(lk.km, 1)} km · ` : ""}${lk.bestSnr != null ? `${fmt(lk.bestSnr, 1)} dB` : ""}</span></li>`;
      }).join("")}</ul>` : '<p class="muted">none</p>'}`;
  } else {      // a square: who's there
    const row = (id) => { const r = byId.get(id); return `<li>${link(id, r?.name || id)}<span class="muted">${r?.minHops != null ? `${r.minHops} hop${r.minHops === 1 ? "" : "s"} at best · ` : ""}heard by ${r?.stations ?? 0} station${r?.stations === 1 ? "" : "s"}</span></li>`; };
    const nearest = Math.min(...d.stations.map((s) => rcKm(x, s)));
    $("dvCell").innerHTML = `<dl class="dv-kv">
        <dt>Square</dt><dd>~${fmt(d.cellKm, 0)} km, ${fmt(nearest, 1)} km from the nearest station</dd>
        <dt>Radios</dt><dd>${x.relayed.length} only through relays${x.direct.length ? `, ${x.direct.length} heard directly` : ""}</dd>
      </dl>
      <div class="dv-h">Only through relays</div>${x.relayed.length ? `<ul class="dv-list">${x.relayed.map(row).join("")}</ul>` : '<p class="muted">none</p>'}
      ${x.direct.length ? `<div class="dv-h">Heard directly</div><ul class="dv-list">${x.direct.map((id) => `<li>${link(id, byId.get(id)?.name || id)}<span class="muted">by ${esc((byId.get(id)?.direct || []).map(stName).join(", "))}</span></li>`).join("")}</ul>` : ""}`;
  }
  if (!quiet) { $("vzCell").open = true; renderReach(); }
}

$("rcFar").addEventListener("change", (e) => {
  RC.far = Number(e.target.value);
  try { localStorage.setItem("meshdash.rcFar", String(RC.far)); } catch { /* storage blocked */ }
  renderReach();
});
$("rcLines").addEventListener("change", (e) => {
  RC.lines = e.target.checked;
  try { localStorage.setItem("meshdash.rcLines", RC.lines ? "1" : "0"); } catch { /* storage blocked */ }
  renderReach();
});
