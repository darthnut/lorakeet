"use strict";
/* Traffic replay on the topology graph: every reception our radio logged, animated in time order.

   A packet animates along what the radio actually knew about it: originator → relayer (the 1-byte relay
   ID, when it resolves to one node) → our radio. Hops between the originator and that relayer are unknown,
   so that leg is drawn dashed and never routed through nodes it may not have touched. Hours the dashboard
   wasn't logging are skipped, never played as silence. */

const R = { data: null, range: null, view: "graph", map: null, t: null, idx: 0, playing: false, speed: 600, raf: 0, last: 0,
            flights: [], off: new Set(), graph: null, layer: null, skipped: 0 };
const SPEEDS = [[60, "1 min/s"], [600, "10 min/s"], [3600, "1 h/s"], [21600, "6 h/s"]];
const REPLAY_GROUPS = ["Text", "Position", "Telemetry", "Node info", "Routing", "Encrypted", "Other", "Unknown"];
const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
const SEG_MS = reduceMotion ? 120 : 420;   // wall time per leg, whatever the playback speed
const TRACE_SEG_MS = reduceMotion ? 160 : 650;  // traceroutes step slower, hop by hop
const TRACE_LINGER_MS = 3000;                // and leave their route on screen a while
const MAX_FLIGHTS = 250;
const LOG_ROWS = 200;      // packets kept in the log panel
const rcol = (g) => (g === "Unknown" ? "var(--text-muted)" : gcol(g));
const hourKey = (ts) => { const d = new Date(ts * 1000); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")} ${String(d.getHours()).padStart(2, "0")}`; };

function stopReplay() {
  R.playing = false;
  cancelAnimationFrame(R.raf);
  R.raf = 0;
}

// Redraws keep the playhead (view switch, full screen, resize); a new range starts from the beginning.
async function drawReplay(d, range) {
  const wasPlaying = R.playing, gen = R.gen = (R.gen || 0) + 1;
  const current = () => gen === R.gen;
  stopReplay();
  if (R.range !== range || !R.data) {
    $("replayStatus").textContent = "Loading…";
    try {
      const r = await fetch(`/api/analytics/replay?range=${encodeURIComponent(range)}${stationQS()}`).then((x) => x.json());
      if (r.error) throw new Error(r.error);
      R.data = r; R.range = range; R.t = null;
    } catch (e) { $("replayStatus").textContent = `Couldn't load the replay: ${e.message}`; return; }
    R.speed = { "24h": 600, "7d": 3600 }[range] || 21600;
  }
  if (!current()) return;
  const rd = R.data;
  R.view = T.mode === "geo" ? "map" : "graph";
  R.covered = new Set(rd.coveredHours);

  // radios that appear in the replay but have no observed RF link: add them, tethered to their usual relayer
  const inGraph = new Set(d.nodes.map((n) => n.id));
  const ghosts = new Map();
  for (const e of rd.events) {
    if (e.kind === "trace") {
      for (const route of [e.path || [], e.back || []]) route.forEach((id, i) => {
        if (!id || inGraph.has(id)) return;
        const g = ghosts.get(id) || ghosts.set(id, { via: new Map(), hops: [] }).get(id);
        const nb = route[i - 1] || route[i + 1];
        if (nb) g.via.set(nb, (g.via.get(nb) || 0) + 1);
        g.hops.push(1);
      });
      continue;
    }
    for (const id of [e.from, e.relay]) {
      if (!id || inGraph.has(id)) continue;
      const g = ghosts.get(id) || ghosts.set(id, { via: new Map(), hops: [] }).get(id);
      if (id === e.from) {
        const via = e.relay && e.relay !== id ? e.relay : e.at || rd.local;
        g.via.set(via, (g.via.get(via) || 0) + 1);
        if (e.hops != null) g.hops.push(e.hops);
      }
    }
  }
  const extra = { nodes: [], tethers: [] };
  for (const [id, g] of ghosts) {
    const via = [...g.via].sort((a, b) => b[1] - a[1])[0]?.[0] || rd.local;
    const hops = g.hops.length ? g.hops.sort((a, b) => a - b)[g.hops.length >> 1] : 2;
    extra.nodes.push({ id, ...(rd.nodes[id] || { name: id, short: id.slice(-4) }), degree: 0, packets: 0, ghost: true, via });
    if (via && via !== id) extra.tethers.push({ a: id, b: via, hops });
  }
  // every listening station is drawn like our radio (combined view: receptions end at the station that heard them)
  for (const n of [...d.nodes, ...extra.nodes]) n.isStation = (rd.stations || []).includes(n.id);
  if (ANON) anonymize(rd, [...d.nodes, ...extra.nodes]);
  R.ghostCount = extra.nodes.length;
  $("topoReplayGraph").hidden = R.view !== "graph";
  $("topoReplayMap").hidden = R.view !== "map";
  if (R.map) { R.map.remove(); R.map = null; }
  R.graph = null; R.unplaced = 0;
  if (R.view === "map") {
    const m = await buildGeoMap(d, $("topoReplayMap"), () => current() && R.view === "map", extra.nodes, { trimOutliers: true, inset: vizInset() });
    if (!m) { if (current() && R.view === "map") $("replayStatus").textContent = "None of these radios have a position to put on the map."; return; }
    R.map = m.map;
    // flights draw in an SVG laid over the map; positions are re-projected every frame, so pan and zoom just work
    const svg = d3.select(m.map.getContainer()).append("svg").attr("class", "flightsvg");
    R.layer = svg.append("g").attr("class", "flights").node();
    R.graph = { byId: m.byId, r: () => 7, pos: (id) => { const ll = m.at(id); if (!ll) return null; const p = m.map.latLngToContainerPoint(ll); return { x: p.x, y: p.y }; } };
    const ids = new Set([...d.nodes, ...extra.nodes].map((n) => n.id));
    R.unplaced = [...ids].filter((id) => !m.at(id)).length;
  } else {
    R.graph = drawGraph(d, $("topoReplayGraph"), extra, { inset: vizInset });
    if (!R.graph) { $("replayStatus").textContent = "Nothing to replay in this range yet."; return; }
    R.layer = R.graph.g.append("g").attr("class", "flights").node();
  }
  R.flights = []; R.skipped = 0;
  buildReveals(rd);
  seek(R.t ?? rd.since);
  buildControls();
  drawTimeline();
  updateReadout();
  updateStatus();
  if (wasPlaying) $("replayPlay").click();
}

// ?anon=1, for sharing a recording: every radio is renamed by role and order of first appearance (Station 1,
// Router 2, Base 1, Node 14...), so no callsign, place or person's name shows on the graph or in the log.
// Deterministic for a given range and station, so redraws keep the same labels.
const ANON = new URLSearchParams(location.search).has("anon");
function anonymize(rd, drawn) {
  const stations = new Set([rd.local, ...(rd.stations || [])].filter(Boolean));
  const byId = new Map(drawn.map((n) => [n.id, n]));
  const order = [...stations];
  const seen = new Set(order);
  for (const e of rd.events) for (const id of [e.from, e.relay, e.at, ...(e.path || []), ...(e.back || [])]) if (id && !seen.has(id)) { seen.add(id); order.push(id); }
  for (const id of [...byId.keys(), ...Object.keys(rd.nodes || {})]) if (!seen.has(id)) { seen.add(id); order.push(id); }
  const count = {}, label = new Map();
  for (const id of order) {
    const role = String(byId.get(id)?.role || rd.nodes?.[id]?.role || "").toUpperCase();
    const word = stations.has(id) ? "Station" : /ROUTER|REPEATER/.test(role) ? "Router" : role === "CLIENT_BASE" ? "Base" : "Node";
    count[word] = (count[word] || 0) + 1;
    label.set(id, `${word} ${count[word]}`);
  }
  for (const n of drawn) { n.name = n.short = label.get(n.id); delete n.hw; }
  for (const id of Object.keys(rd.nodes || {})) rd.nodes[id] = { ...rd.nodes[id], name: label.get(id), short: label.get(id), hw: null };
  R.anonLabels = label;
}

// Space the panels cover (the page may define it); graph and map centre in what's left.
const vizInset = () => (typeof window.vizInset === "function" ? window.vizInset() : { top: 0, right: 0, bottom: 0, left: 0 });

// R.view (graph | map) follows the page's Graph | Geographic switch (T.mode in topology.js).

// ---------------------------------------------------------------- controls

function buildControls() {
  const rd = R.data;
  const counts = {};
  for (const e of rd.events) counts[e.group] = (counts[e.group] || 0) + 1;
  $("replayLegend").innerHTML = REPLAY_GROUPS.filter((g) => counts[g]).map((g) => `<button type="button" class="chip" data-g="${esc(g)}" aria-pressed="${!R.off.has(g)}">
      <i style="${g === "Unknown" ? `border:2px solid ${rcol(g)}` : `background:${rcol(g)}`}"></i>${esc(g === "Unknown" ? "Type not logged" : g)} <span class="n">${nf.format(counts[g])}</span></button>`).join("") +
    `<span class="lgkeys"><span><i class="lg-line"></i>hop our radio heard</span><span><i class="lg-line dash"></i>path unknown (several hops)</span><span><i class="lg-node ghost"></i>no observed link</span><span><i class="lg-line trace"></i>traceroute route (every hop measured)</span></span>`;
  $("replaySpeed").innerHTML = SPEEDS.map(([v, l]) => `<button data-v="${v}" class="${v === R.speed ? "on" : ""}">${l}</button>`).join("");
}

$("replayLegend").addEventListener("click", (ev) => {
  const b = ev.target.closest("button[data-g]"); if (!b) return;
  const g = b.dataset.g;
  if (R.off.has(g)) R.off.delete(g); else R.off.add(g);
  b.setAttribute("aria-pressed", String(!R.off.has(g)));
  drawTimeline();
});
$("replaySpeed").addEventListener("click", (ev) => {
  const b = ev.target.closest("button[data-v]"); if (!b) return;
  R.speed = Number(b.dataset.v);
  for (const x of $("replaySpeed").children) x.classList.toggle("on", x === b);
});
$("replayPlay").addEventListener("click", () => {
  if (!R.data) return;
  if (R.t >= R.data.until) seek(R.data.since);
  R.playing = !R.playing;
  if (R.playing) { R.last = performance.now(); R.raf = requestAnimationFrame(frame); }
  updateReadout();
});

function seek(t) {
  const ev = R.data.events;
  R.t = Math.max(R.data.since, Math.min(R.data.until, t));
  let lo = 0, hi = ev.length;
  while (lo < hi) { const m = (lo + hi) >> 1; if (ev[m].ts <= R.t) lo = m + 1; else hi = m; }
  R.idx = lo;
  for (const f of R.flights) f.g.remove();
  R.flights = [];
  if (R.layer) R.layer.replaceChildren();
  $("replayTicker").replaceChildren();
  R.visKey = null;
  applyVisibility();
}

// ---------------------------------------------------------------- grow and fade modes
// Each radio's first moment in the replay: as sender, relay, a traceroute hop or the station that heard it.
// In grow mode the graph shows only radios whose moment has passed (listening stations from the start).

R.grow = (() => { try { return localStorage.getItem("meshdash.grow") === "1"; } catch { return false; } })();

// Fade mode: a radio dims as it goes quiet and comes back when it's heard again. Its timeout is learned from
// its own rhythm ("auto": `misses` x its median gap between receptions, kept to 2-24 h) or fixed. Full
// brightness until half the timeout, then a linear dim to a faint outline at the timeout; with remove, it
// leaves the layout at 1.5x. Only hours we were logging count as silence. Links fade by their own last
// activity (the legs packets travelled), or by their quieter end when the replay never used them.
const FADE_DEFAULTS = { on: false, after: "auto", misses: 3, remove: false };
R.fade = (() => { try { return { ...FADE_DEFAULTS, ...JSON.parse(localStorage.getItem("meshdash.fade") || "{}") }; } catch { return { ...FADE_DEFAULTS }; } })();
const FADE_FLOOR = 0.12;   // a fully faded radio: a faint outline
const HOUR_S = 3600;

function buildReveals(rd) {
  const first = new Map(), seen = new Map(), edgeSeen = new Map();
  const see = (id, ts) => {
    if (!id) return;
    if (!first.has(id)) first.set(id, ts);
    if (Number.isFinite(ts)) { const a = seen.get(id) || seen.set(id, []).get(id); if (a[a.length - 1] !== ts) a.push(ts); }
  };
  const stations = [rd.local, ...(rd.stations || [])].filter(Boolean);
  for (const id of stations) see(id, -Infinity);
  for (const e of rd.events) {  // events are in time order
    for (const id of [e.from, e.relay, e.at, ...(e.path || []), ...(e.back || [])]) see(id, e.ts);
    if (e.kind === "tx") see(rd.local, e.ts);
    for (const [a, b] of legs(e)) {
      if (!a || !b || a === b) continue;
      const k = a < b ? `${a}|${b}` : `${b}|${a}`, arr = edgeSeen.get(k) || edgeSeen.set(k, []).get(k);
      if (arr[arr.length - 1] !== e.ts) arr.push(e.ts);
    }
  }
  // each radio's typical gap between receptions (copies within a minute count once)
  const gap = new Map();
  for (const [id, ts] of seen) {
    const d = [];
    for (let i = 1; i < ts.length; i++) { const g = ts[i] - ts[i - 1]; if (g > 60 && g < 48 * HOUR_S) d.push(g); }
    d.sort((x, y) => x - y);
    gap.set(id, d.length >= 2 ? d[d.length >> 1] : null);
  }
  // covered (logging) hours as a running count, so silence skips the gaps in O(1)
  const h0 = Math.floor(rd.since / HOUR_S), hn = Math.ceil(rd.until / HOUR_S) - h0 + 1, cum = new Float64Array(hn + 1);
  for (let i = 0; i < hn; i++) cum[i + 1] = cum[i] + (R.covered.has(hourKey((h0 + i) * HOUR_S)) ? 1 : 0);
  R.coveredBetween = (a, b) => {  // logging seconds in [a, b], to the hour
    const ia = Math.max(0, Math.min(hn, Math.floor(a / HOUR_S) - h0)), ib = Math.max(0, Math.min(hn, Math.floor(b / HOUR_S) - h0));
    return Math.max(0, b - a) * (ib > ia ? (cum[ib] - cum[ia]) / (ib - ia) : 1);
  };
  R.first = first; R.seen = seen; R.edgeSeen = edgeSeen; R.gap = gap; R.stationSet = new Set(stations);
  R.grew = false;
  R.revealTimes = [...first.values()].sort((a, b) => a - b);
  R.visKey = null;
}

function timeoutOf(id) {
  const f = R.fade;
  if (f.after !== "auto") return Number(f.after) * HOUR_S;
  const g = R.gap.get(id);
  return g ? Math.max(2 * HOUR_S, Math.min(24 * HOUR_S, f.misses * g)) : 6 * HOUR_S;
}
const lastBefore = (arr, t) => {  // latest entry <= t, or null
  if (!arr || !arr.length || arr[0] > t) return null;
  let lo = 0, hi = arr.length;
  while (lo < hi) { const m = (lo + hi) >> 1; if (arr[m] <= t) lo = m + 1; else hi = m; }
  return arr[lo - 1];
};
// 1 = bright, 0 = fully faded, -1 = gone (remove mode, past 1.5x the timeout)
function fadeLevel(last, T) {
  if (last == null) return 0;
  const s = R.coveredBetween(last, R.t);
  if (R.fade.remove && s >= 1.5 * T) return -1;
  return s <= T / 2 ? 1 : s >= T ? 0 : 1 - (s - T / 2) / (T / 2);
}
function nodeFade(id) {
  if (R.stationSet.has(id)) return 1;
  return fadeLevel(lastBefore(R.seen.get(id), R.t), timeoutOf(id));
}

// What's on the graph now: grown (first heard by now, when growing) and not faded away (when removing).
function applyVisibility(now = null) {
  if (!R.graph?.setShown || !R.first) return;
  const fadeOn = R.fade.on, levels = new Map();
  if (fadeOn) for (const id of R.graph.byId.keys()) levels.set(id, nodeFade(id));
  const shown = (id) => (!R.grow || (R.first.get(id) ?? Infinity) <= R.t) && !(fadeOn && levels.get(id) === -1);
  // re-lay out only when the set of radios changes
  let key = "";
  if (R.grow || (fadeOn && R.fade.remove)) for (const id of R.graph.byId.keys()) key += shown(id) ? "1" : "0";
  key += R.grow ? "g" : "";
  if (key !== R.visKey) {
    const relayout = R.grow || (fadeOn && R.fade.remove) || R.grew;
    R.graph.setShown(shown, relayout, now);
    R.grew = relayout && key.includes("0");
    R.visKey = key;
  }
  if (R.graph.setDecay) {
    if (!fadeOn) { if (R.decayOn) { R.graph.setDecay(null); R.decayOn = false; } return; }
    R.decayOn = true;
    const op = (id) => FADE_FLOOR + (1 - FADE_FLOOR) * Math.max(0, levels.get(id) ?? 1);
    R.graph.setDecay(op, (a, b) => {
      const k = a < b ? `${a}|${b}` : `${b}|${a}`, arr = R.edgeSeen.get(k);
      const lv = arr ? fadeLevel(lastBefore(arr, R.t), Math.max(timeoutOf(a), timeoutOf(b))) : Math.min(levels.get(a) ?? 1, levels.get(b) ?? 1);
      return FADE_FLOOR + (1 - FADE_FLOOR) * Math.max(0, Math.min(lv, Math.max(0, levels.get(a) ?? 1), Math.max(0, levels.get(b) ?? 1)));
    });
  }
}
const applyGrow = applyVisibility;  // (older name)

function setGrow(on) {
  R.grow = on;
  try { localStorage.setItem("meshdash.grow", on ? "1" : "0"); } catch { /* storage blocked */ }
  const b = document.getElementById("vzGrow"); if (b) b.setAttribute("aria-pressed", String(on));
  R.visKey = null;
  applyVisibility();
}
window.setGrow = setGrow;

function setFade(changes) {
  R.fade = { ...R.fade, ...changes };
  try { localStorage.setItem("meshdash.fade", JSON.stringify(R.fade)); } catch { /* storage blocked */ }
  R.visKey = null;
  applyVisibility();
}
window.setFade = setFade;

// ---------------------------------------------------------------- timeline (density strip + playhead)

function drawTimeline() {
  const el = $("replayTimeline"), rd = R.data;
  const W = el.clientWidth || 800, H = 60, B = H - 22, span = rd.until - rd.since;
  const bucket = Math.max(3600, Math.ceil(span / 3600 / 200) * 3600);
  const n = Math.max(1, Math.ceil(span / bucket));
  const counts = new Array(n).fill(0), covered = new Array(n).fill(false);
  for (const e of rd.events) if (!R.off.has(e.group)) counts[Math.min(n - 1, Math.floor((e.ts - rd.since) / bucket))]++;
  for (let i = 0; i < n; i++) for (let t = rd.since + i * bucket; t < rd.since + (i + 1) * bucket; t += 3600) if (R.covered.has(hourKey(t))) { covered[i] = true; break; }
  const max = Math.max(1, ...counts), bw = W / n;
  let body = "";
  counts.forEach((c, i) => {
    if (!covered[i]) { body += `<rect x="${(i * bw).toFixed(1)}" y="0" width="${bw.toFixed(1)}" height="${B}" fill="url(#rpHatch)"/>`; return; }
    const h = c ? Math.max(2, (c / max) * (B - 4)) : 0;
    if (h) body += `<rect class="bar" x="${(i * bw + 0.5).toFixed(1)}" y="${(B - h).toFixed(1)}" width="${Math.max(1, bw - 1).toFixed(1)}" height="${h.toFixed(1)}" rx="1"/>`;
  });
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" height="${H}" role="slider" tabindex="0" aria-label="Replay position" aria-valuemin="${rd.since}" aria-valuemax="${rd.until}">
    <defs><pattern id="rpHatch" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><rect width="3" height="6" fill="${css("--gap-hatch")}"/></pattern></defs>
    <line class="base" x1="0" x2="${W}" y1="${B}" y2="${B}"/>${body}
    <text class="axis" x="0" y="${H - 3}">${esc(when(rd.since))}</text><text class="axis" x="${W}" y="${H - 3}" text-anchor="end">${esc(when(rd.until))}</text>
    <line class="playhead" id="rpHead" y1="0" y2="${B}"/></svg>`;
  const svg = el.querySelector("svg");
  const toT = (e) => { const r = svg.getBoundingClientRect(); return rd.since + ((e.clientX - r.left) / r.width) * span; };
  let dragging = false;
  svg.addEventListener("pointerdown", (e) => { dragging = true; svg.setPointerCapture(e.pointerId); seek(toT(e)); updateReadout(); });
  svg.addEventListener("pointermove", (e) => {
    if (dragging) { seek(toT(e)); updateReadout(); return; }
    const t = toT(e), i = Math.min(n - 1, Math.max(0, Math.floor((t - rd.since) / bucket)));
    showTip(e, covered[i] ? `<b>${esc(when(rd.since + i * bucket))}</b><br>${nf.format(counts[i])} receptions` : `<b>${esc(when(rd.since + i * bucket))}</b><br><span class="t">Not logging: skipped during playback</span>`);
  });
  svg.addEventListener("pointerup", () => { dragging = false; });
  svg.addEventListener("pointerleave", hideTip);
  svg.addEventListener("keydown", (e) => {
    const step = { ArrowRight: bucket, ArrowLeft: -bucket }[e.key];
    if (step) { e.preventDefault(); seek(R.t + step); updateReadout(); }
    if (e.key === " ") { e.preventDefault(); $("replayPlay").click(); }
  });
  R.tl = { W, span };
  placeHead();
}

function placeHead() {
  const h = document.getElementById("rpHead"); if (!h || !R.tl) return;
  const x = ((R.t - R.data.since) / R.tl.span) * R.tl.W;
  h.setAttribute("x1", x); h.setAttribute("x2", x);
  h.parentNode.setAttribute("aria-valuenow", Math.round(R.t));
  h.parentNode.setAttribute("aria-valuetext", when(R.t));
}

// ---------------------------------------------------------------- playback

const nameOf = (id) => (id === R.data.local ? "our radio" : (ANON ? R.anonLabels?.get(id) || "a radio" : R.graph.byId.get(id)?.name || R.data.nodes[id]?.name || id));

function frame(now) {
  const rd = R.data;
  if (R.playing) {
    R.t += ((now - R.last) / 1000) * R.speed;
    // skip hours the dashboard wasn't logging: they are gaps, not quiet time
    if (!R.covered.has(hourKey(R.t)) && R.t < rd.until) {
      let t = Math.floor(R.t / 3600) * 3600 + 3600;
      while (t < rd.until && !R.covered.has(hourKey(t))) t += 3600;
      R.t = Math.min(rd.until, t);
    }
    let spawned = 0;
    const skippedBefore = R.skipped;
    while (R.idx < rd.events.length && rd.events[R.idx].ts <= R.t) {
      const e = rd.events[R.idx++];
      if (R.off.has(e.group)) continue;
      if (spawned++ < 60) launch(e, now); else R.skipped++;
    }
    if (R.skipped !== skippedBefore) updateStatus();
    if (R.t >= rd.until) { R.t = rd.until; R.playing = false; }
  }
  R.last = now;
  applyGrow(R.playing ? now : null);
  R.graph?.fade?.(now);
  animate(now);
  placeHead();
  updateReadout();
  if (R.playing || R.flights.length) R.raf = requestAnimationFrame(frame);
  else R.raf = 0;
}

function legs(e) {
  const me = e.at || R.data.local;  // the station that heard it (combined view), else our radio
  if (e.kind === "trace") {  // out along the measured route, then back along the return route
    const hops = (route) => route.slice(1).map((id, i) => [route[i], id, "trace"]).filter(([a, b]) => a && b);
    return [...hops(e.path || []), ...hops(e.back || [])];
  }
  if (e.kind === "tx") return [];
  if (e.from === me) return e.relay ? [[me, e.relay, "solid"], [e.relay, me, "solid"]] : [];  // our packet, relayed back to us
  if (e.hops === 0) return [[e.from, me, "solid"]];
  if (e.relay && e.relay !== e.from) return [[e.from, e.relay, e.hops === 1 ? "solid" : "dash"], [e.relay, me, "solid"]];
  return [[e.from, me, "dash"]];   // hop count unknown, or relay ID didn't resolve to one node
}

function launch(e, now) {
  const log = $("replayTicker");
  log.insertAdjacentHTML("afterbegin", describeEvent(e));
  while (log.childElementCount > LOG_ROWS) log.lastElementChild.remove();
  if (!R.graph || (!R.graph.pos(e.from) && e.kind !== "tx")) return;
  const ns = "http://www.w3.org/2000/svg", color = rcol(e.group);
  const g = document.createElementNS(ns, "g");
  g.setAttribute("class", `flight${e.group === "Text" ? " text" : ""}${e.group === "Unknown" ? " unknown" : ""}${e.kind === "trace" ? " trace" : ""}`);
  g.style.setProperty("--c", color);
  const route = e.kind === "trace" ? document.createElementNS(ns, "polyline") : null;
  if (route) { route.setAttribute("class", "route"); g.append(route); }
  const ring = document.createElementNS(ns, "circle"); ring.setAttribute("class", "ring"); g.append(ring);
  const trail = document.createElementNS(ns, "line"); trail.setAttribute("class", "trail"); g.append(trail);
  const dot = document.createElementNS(ns, "circle"); dot.setAttribute("class", "dot"); dot.setAttribute("r", e.group === "Text" || e.kind === "trace" ? 5 : 3.5); g.append(dot);
  R.layer.append(g);
  R.flights.push({ e, g, ring, trail, dot, route, start: now, legs: legs(e), seg: e.kind === "trace" ? TRACE_SEG_MS : SEG_MS });
  if (R.flights.length > MAX_FLIGHTS) R.flights.shift().g.remove();
}

function animate(now) {
  const keep = [];
  if (!R.graph) { R.flights = []; return; }
  for (const f of R.flights) {
    const SEG = f.seg, age = now - f.start, total = Math.max(1, f.legs.length) * SEG + (f.route ? TRACE_LINGER_MS : 500);
    if (age > total) { f.g.remove(); continue; }
    keep.push(f);
    // transmission ring at the originator
    const o = R.graph.pos(f.e.kind === "tx" ? R.data.local : f.e.from);
    if (o) {
      const k = Math.min(1, age / 700), base = R.graph.r(o);
      f.ring.setAttribute("cx", o.x); f.ring.setAttribute("cy", o.y);
      f.ring.setAttribute("r", base + (reduceMotion ? 6 : 4 + k * (f.e.kind === "tx" ? 26 : 16)));
      f.ring.style.opacity = String(1 - k);
    }
    const i = Math.floor(age / SEG), leg = f.legs[Math.min(i, f.legs.length - 1)];
    if (!leg) { f.dot.style.opacity = "0"; continue; }
    const a = R.graph.pos(leg[0]), b = R.graph.pos(leg[1]);
    if (!a || !b) { f.dot.style.opacity = "0"; continue; }
    const k = i >= f.legs.length ? 1 : (age % SEG) / SEG, ease = k < 0.5 ? 2 * k * k : 1 - (-2 * k + 2) ** 2 / 2;
    const x = a.x + (b.x - a.x) * ease, y = a.y + (b.y - a.y) * ease;
    f.dot.setAttribute("cx", x); f.dot.setAttribute("cy", y);
    f.trail.setAttribute("x1", a.x); f.trail.setAttribute("y1", a.y); f.trail.setAttribute("x2", x); f.trail.setAttribute("y2", y);
    f.trail.classList.toggle("dash", leg[2] === "dash");
    const fade = i >= f.legs.length ? 1 - Math.min(1, (age - f.legs.length * SEG) / (f.route ? TRACE_LINGER_MS : 500)) : 1;
    f.dot.style.opacity = f.trail.style.opacity = String(fade);
    if (f.route) {  // the route so far: every completed hop, then the dot
      const pts = [];
      for (let j = 0; j < Math.min(i, f.legs.length); j++) {
        const p = R.graph.pos(f.legs[j][0]), q = R.graph.pos(f.legs[j][1]);
        if (p && q) { if (!pts.length || pts[pts.length - 1] !== p) pts.push(p); pts.push(q); }
      }
      if (i < f.legs.length) pts.push({ x, y });
      f.route.setAttribute("points", pts.map((pt) => `${pt.x},${pt.y}`).join(" "));
      f.route.style.opacity = String(fade * 0.85);
    }
  }
  R.flights = keep;
}

function describeEvent(e) {
  const t = new Date(e.ts * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" });
  const type = e.group === "Unknown" ? "type not logged" : e.group;
  let path;
  if (e.kind === "trace") {
    const hop = (route, snrs) => route.map((id, i) => `${id ? esc(nameOf(id)) : "unknown hop"}${i && snrs[i] != null ? ` <span class="muted">${snrs[i]} dB</span>` : ""}`).join(" → ");
    return `<li class="trace-log"><span class="t">${esc(t)}</span><i style="background:${rcol(e.group)}"></i><b>Traceroute${e.origin ? ` (${esc(e.origin)})` : ""}</b> to ${esc(nameOf(e.to))}<br>out: ${hop(e.path || [], e.snr || [])}${(e.back || []).length ? `<br>back: ${hop(e.back, e.snrBack || [])}` : ""}</li>`;
  }
  if (e.kind === "tx") path = `our radio transmitted${e.to && e.to !== "^all" ? ` to ${esc(nameOf(e.to))}` : " a broadcast"}`;
  else if (e.from === (e.at || R.data.local)) path = `${e.at ? `${esc(nameOf(e.at))}'s` : "our"} packet, rebroadcast by ${esc(nameOf(e.relay))}`;
  else {
    const us = e.at ? esc(nameOf(e.at)) : "us";
    if (e.hops === 0) path = `${esc(nameOf(e.from))} → ${us}, direct`;
    else if (e.hops == null) path = `${esc(nameOf(e.from))} → ${us}, hops unknown`;
    else path = `${esc(nameOf(e.from))} → ${e.hops > 1 ? `${e.hops - 1} unknown hop${e.hops > 2 ? "s" : ""} → ` : ""}${e.relay ? esc(nameOf(e.relay)) : `relay 0x${(e.relayByte ?? 0).toString(16).padStart(2, "0")} (unresolved)`} → ${us}`;
  }
  const anat = e.row ? ` <a class="anat-link" href="/packets.html#packet=${e.row}" target="_blank" rel="noopener" title="Packet anatomy: bytes on air, encryption, decoded fields">bytes ↗</a>` : "";
  return `<li><span class="t">${esc(t)}</span><i style="${e.group === "Unknown" ? `border:2px solid ${rcol(e.group)}` : `background:${rcol(e.group)}`}"></i><b>${esc(type)}</b> ${path}${anat}</li>`;
}

function updateReadout() {
  const rd = R.data; if (!rd) return;
  $("replayPlay").textContent = R.playing ? "❚❚ Pause" : R.t >= rd.until ? "↺ Replay" : "▶ Play";
  $("replayPlay").setAttribute("aria-pressed", String(R.playing));
  const clock = new Date(R.t * 1000).toLocaleString([], { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  if ($("replayClock").textContent !== clock) {
    $("replayClock").textContent = clock;
    if ($("vzCleanClock")) $("vzCleanClock").textContent = clock;
  }
}

function updateStatus() {
  const rd = R.data; if (!rd) return;
  const since = rd.miningSince ? ` Every duplicate copy is included from ${when(rd.miningSince)}, when debug-log mining began; before that, only the first copy of each packet.` : "";
  $("replayStatus").textContent = `${nf.format(rd.events.length)} receptions and transmissions${rd.truncated ? " (newest only: the range holds more)" : ""}.` +
    (R.ghostCount ? ` ${R.ghostCount} radios were heard only through multi-hop relays and have no observed link; they're drawn hollow${R.view === "graph" ? ", next to their usual relayer" : ""}.` : "") +
    (R.view === "map" ? ` On the map, radios without a position use their estimate${R.unplaced ? `, and ${R.unplaced} with neither can't be placed, so their packets are left out of the animation (the log still lists them)` : ""}.` : "") + since +
    (R.skipped ? ` ${nf.format(R.skipped)} not animated: too many at once at this speed (try a slower speed).` : "");
}
