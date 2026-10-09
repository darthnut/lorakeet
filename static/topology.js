"use strict";
/* Network topology (RF links): the links table on Analytics; the force graph and map that replay.js
   animates on Visualizations. */

const T = { data: null, range: null, mode: "graph", sim: null, map: null };
const SRC_LABEL = { direct: "heard directly by our radio", traceroute: "traceroute hop", neighborinfo: "neighbor report", relay: "inferred from relays" };

function nodeFill(n) {
  if (n.isLocal || n.isStation) return "var(--accent)";  // our radio, or any listening station
  if (n.isBase) return "var(--base)";
  return n.role ? "var(--recent)" : "var(--stale)";
}
const edgeWidth = (e) => 1.5 + Math.log2(1 + e.count) * 1.2;
const edgeTip = (e, byId) => {
  const a = byId.get(e.a), b = byId.get(e.b);
  const src = Object.entries(e.sources).map(([k, v]) => `${v}× ${SRC_LABEL[k] || k}`).join("<br>");
  return `<b>${esc(a?.name || e.a)} ↔ ${esc(b?.name || e.b)}</b><br>${src}${e.snr != null ? `<br>avg SNR ${fmt(e.snr, 1)} dB` : ""}<br><span class="t">last seen ${esc(ago(e.last))}${e.measured ? "" : " · inferred, not measured"}</span>`;
};

async function renderTopology(range) {
  // Visualizations' Coverage view (drive.js) is its own map: no topology needed
  if ($("driveMap")) {
    const drive = T.mode === "drive";
    document.body.classList.toggle("vz-drive", drive);
    $("driveMap").hidden = !drive;
    if (drive) {
      if (typeof stopReplay === "function") stopReplay();
      $("topoReplayGraph").hidden = $("topoReplayMap").hidden = true;
      return drawDrive(range);
    }
  }
  if (T.range !== range || !T.data) {
    T.range = range;
    try {
      const d = await fetch(`/api/analytics/topology?range=${encodeURIComponent(range)}${stationQS()}`).then((r) => r.json());
      if (d.error) throw new Error(d.error);
      T.data = d;
    } catch (e) {
      $("topoNote").textContent = `Couldn't load topology: ${e.message}`;
      return;
    }
  }
  const d = T.data, s = d.stats;
  if ($("topoLegend")) $("topoLegend").innerHTML = `<span><i class="lg-node" style="background:var(--accent)"></i>our radio</span>
    <span><i class="lg-node" style="background:var(--base)"></i>base station</span>
    <span><i class="lg-node" style="background:var(--recent)"></i>node</span>
    <span><i class="lg-node" style="background:var(--stale)"></i>never announced</span>
    <span><i class="lg-line"></i>measured</span><span><i class="lg-line dash"></i>inferred</span>`;
  $("topoNote").textContent = `${s.nodes} radios, ${s.edges} links (${s.measured} measured, ${s.inferred} inferred).` +
    (s.receptionsMined ? ` Includes ${nf.format(s.receptionsMined)} over-the-air receptions (duplicate copies included) mined from the radio's debug log.` : "") +
    (s.relayResolvedByLink ? ` ${nf.format(s.relayResolvedByLink)} relay IDs shared by several nodes were resolved to the one with a measured link to our radio.` : "") +
    (s.ambiguousRelayPackets ? ` ${nf.format(s.ambiguousRelayPackets)} relayed packets skipped: their 1-byte relay ID still matched several nodes.` : "") +
    (s.unknownRelayPackets ? ` ${nf.format(s.unknownRelayPackets)} skipped: relay ID matches no known node.` : "") +
    " Built from what our radio heard, so links far from the house are under-represented.";
  if ($("topoReplay")) drawReplay(d, range);  // Visualizations: graph or map, with the player underneath
  else drawTable(d);                           // Analytics: the links table
}

// extra = { nodes, tethers } adds radios with no observed RF link (replay: originators we only know reached
// us through a multi-hop relay). Tethers only keep them near their relayer; they are drawn as faint dots,
// never as links. Returns { g, pos(id) } for overlays, or null when there is nothing to draw.
// inset: the stage edges that panels cover, or a function returning them (re-read while framing, since
// panels can change size after the graph is drawn).
function drawGraph(d, el = $("topoGraph"), extra = { nodes: [], tethers: [] }, { inset = { top: 0, right: 0, bottom: 0, left: 0 } } = {}) {
  const insetNow = typeof inset === "function" ? inset : () => inset;
  inset = insetNow();
  if (T.sim) T.sim.stop();
  if (!d.edges.length && !extra.nodes.length) { el.innerHTML = '<p class="muted" style="padding:16px">No links observed in this range yet.</p>'; return null; }
  const W = el.clientWidth || 800, H = el.clientHeight || 460;
  const byId = new Map([...d.nodes, ...extra.nodes].map((n) => [n.id, n]));
  const nodes = [...d.nodes, ...extra.nodes].map((n) => ({ ...n }));
  const links = d.edges.map((e) => ({ ...e, source: e.a, target: e.b }));
  const tethers = extra.tethers.map((t) => ({ ...t, source: t.a, target: t.b }));
  el.innerHTML = "";
  const svg = d3.select(el).append("svg").attr("viewBox", [0, 0, W, H]).attr("role", "img").attr("aria-label", "Network topology graph");
  const g = svg.append("g");
  // The camera follows the layout, keeping it framed in the open area, until the user zooms, pans or drags;
  // double-click hands it back to the camera.
  let follow = true, growing = false;  // growing: grow mode is rebuilding the layout (gentler camera)
  const zoom = d3.zoom().scaleExtent([0.2, 5]).on("zoom", (ev) => {
    if (ev.sourceEvent) follow = false;
    g.attr("transform", ev.transform);
  });
  svg.call(zoom).on("dblclick.zoom", null);

  const tether = g.append("g").selectAll("line").data(tethers).join("line").attr("class", "tether");
  const edge = g.append("g").selectAll("line").data(links).join("line")
    .attr("class", (e) => `edge ${e.measured ? "" : "inferred"}`).attr("stroke-width", edgeWidth);
  const hit = g.append("g").selectAll("line").data(links).join("line").attr("class", "edgehit")
    .on("pointermove", (ev, e) => { showTip(ev, edgeTip(e, byId)); d3.select(edge.nodes()[links.indexOf(e)]).classed("hi", true); })
    .on("pointerleave", (ev, e) => { hideTip(); d3.select(edge.nodes()[links.indexOf(e)]).classed("hi", false); });

  const r = (n) => 6 + Math.sqrt(n.degree) * 3;
  const node = g.append("g").selectAll("g").data(nodes).join("g").attr("class", "node")
    .call(d3.drag()
      .on("start", (ev, n) => { follow = false; if (!ev.active) T.sim.alphaTarget(0.3).restart(); n.fx = n.x; n.fy = n.y; })
      .on("drag", (ev, n) => { n.fx = cx + (ev.x - cx) / sx; n.fy = cy + (ev.y - cy) / sy; })
      .on("end", (ev, n) => { if (!ev.active) T.sim.alphaTarget(0); n.fx = null; n.fy = null; }));
  node.append("circle").attr("r", r).attr("fill", nodeFill).classed("ghost", (n) => n.ghost);
  const label = node.append("text").attr("x", (n) => r(n) + 4).attr("y", 4).text((n) => (n.name.length > 22 ? `${n.short}` : n.name));
  label.each(function (n) { n.labelW = this.getComputedTextLength() || 0; });  // for framing: labels hang off the right

  const neighbors = new Map(nodes.map((n) => [n.id, new Set([n.id])]));
  for (const e of links) { neighbors.get(e.a).add(e.b); neighbors.get(e.b).add(e.a); }
  node.on("pointermove", (ev, n) => {
    const nb = neighbors.get(n.id);
    node.classed("dim", (m) => !nb.has(m.id));
    edge.classed("dim", (e) => e.a !== n.id && e.b !== n.id);
    showTip(ev, `<b>${esc(n.name)}</b>${n.isBase ? " ☀" : ""}${n.keyFlag ? `<br><span class="t">⚠ ${esc(keyFlagText(n.keyFlag))}</span>` : ""}<br>${n.ghost ? `no observed RF link · reached us via ${esc(byId.get(n.via)?.name || "a relay")}, path unknown` : `${n.degree} link${n.degree === 1 ? "" : "s"}`} · ${nf.format(n.packets)} packets heard in range${n.role ? `<br><span class="t">${esc(prettyEnum(n.role))}${n.hw ? ` · ${esc(prettyHw(n.hw))}` : ""}</span>` : ""}`);
  }).on("pointerleave", () => { node.classed("dim", false); edge.classed("dim", false); hideTip(); })
    .on("click", (ev, n) => { location.href = nodeHref(n.id); });

  // centre in the part of the stage that panels don't cover
  const iw = Math.max(200, W - inset.left - inset.right), ih = Math.max(160, H - inset.top - inset.bottom);
  const cx = inset.left + iw / 2, cy = inset.top + ih / 2;
  // Size: spacing grows with the open area per node, so a big window gets a spread-out graph rather than a
  // magnified small one. Shape: the pull towards the centre is weaker along the long side, so the layout
  // takes the window's proportions instead of settling into a circle.
  const L = Math.max(0.8, Math.min(2.4, Math.sqrt((iw * ih) / (Math.max(nodes.length, 4) * 9000))));
  const aspect = Math.max(0.4, Math.min(3.5, iw / ih)), grav = 0.05;
  // start as a small spiral in the open area's proportions, so the first frames grow out from the centre
  nodes.forEach((n, i) => {
    const t = i * 2.39996, rad = 9 * L * Math.sqrt(i + 0.5);
    n.x = cx + rad * Math.cos(t) * Math.sqrt(aspect); n.y = cy + rad * Math.sin(t) / Math.sqrt(aspect);
  });
  T.sim = d3.forceSimulation(nodes)
    .force("link", d3.forceLink(links).id((n) => n.id).distance((e) => (e.measured ? 90 : 120) * L).strength((e) => (e.measured ? 0.7 : 0.35)))
    .force("tether", d3.forceLink(tethers).id((n) => n.id).distance((t) => (70 + 25 * Math.min(6, t.hops || 2)) * L).strength(0.25))
    .force("charge", d3.forceManyBody().strength((n) => (n.ghost ? -140 : -320) * L * L))
    // no forceCenter: it re-centres by shifting every node at once, which teleported the whole graph each time
    // grow mode added a radio at the edge. The gentle x/y pull below and the camera's framing do the centring.
    .force("x", d3.forceX(cx).strength(grav / aspect)).force("y", d3.forceY(cy).strength(grav * aspect))  // keep outliers on screen
    .force("collide", d3.forceCollide().radius((n) => r(n) + 14))
    .on("tick", () => {
      for (const n of nodes) { const p = disp.get(n.id); p.x = cx + (n.x - cx) * sx; p.y = cy + (n.y - cy) * sy; }
      restretch();
      const P = (n) => disp.get(n.id);
      for (const sel of [tether, edge, hit]) {
        sel.attr("x1", (e) => P(e.source).x).attr("y1", (e) => P(e.source).y).attr("x2", (e) => P(e.target).x).attr("y2", (e) => P(e.target).y);
      }
      node.attr("transform", (n) => `translate(${P(n).x},${P(n).y})`);
      kick();
    })
    .on("end", kick);

  // One camera, eased every screen frame towards framing() for as long as it's off target. (Driving it from
  // the layout's ticks plus a separate settle animation made the two fight whenever a radio arrived mid-settle.)
  let camRaf = 0, camLast = 0;
  function camLoop(now) {
    camRaf = 0;
    if (!follow || !svg.node().isConnected) return;
    const dt = Math.min(0.1, camLast ? (now - camLast) / 1000 : 1 / 60); camLast = now;
    const cur = d3.zoomTransform(svg.node()), tgt = framing();
    const t = 1 - Math.exp(-dt / (growing ? 0.45 : 0.18));  // time constant in seconds, frame-rate independent
    const nxt = ease(cur, tgt, t);
    svg.call(zoom.transform, nxt);
    if (Math.abs(nxt.k - tgt.k) > 0.0005 || Math.hypot(nxt.x - tgt.x, nxt.y - tgt.y) > 0.2 || T.sim === sim && sim.alpha() > sim.alphaMin()) camRaf = requestAnimationFrame(camLoop);
    else camLast = 0;
  }
  function kick() { if (follow && !camRaf) camRaf = requestAnimationFrame(camLoop); }

  // Shape: a hub with many one-link neighbours always settles into a ring, whatever the forces, so the drawn
  // layout is stretched (one axis only, eased) to the open area's proportions. Distances on this graph mean
  // nothing physical, so the stretch loses nothing. sx/sy map simulation space to drawing space; pos() and
  // the replay use drawing space.
  let sx = 1, sy = 1;
  const disp = new Map(nodes.map((n) => [n.id, { id: n.id, degree: n.degree, ghost: n.ghost, x: n.x, y: n.y }]));
  // Each tick the drawn bounds (labels included, as framing() measures them) are compared with the open
  // area's proportions and the stretch nudged towards matching them, so the uniform fit then fills both ways.
  function restretch() {
    const b = bounds();
    if (!b) return;
    const ins = insetNow(), fw = Math.max(200, W - ins.left - ins.right) - 56, fh = Math.max(160, H - ins.top - ins.bottom) - 56;
    const err = (fw / fh) / ((b.x1 - b.x0) / (b.y1 - b.y0));   // > 1: too narrow for the area
    const f = Math.max(0.995, Math.min(1.005, Math.pow(err, 0.05)));  // small capped steps: a sudden change in
                                                                      // bounds (a radio joining at the edge) eases in
    if (err > 1) { if (sy > 1) sy = Math.max(1, sy / f); else sx = Math.min(3, sx * f); }
    else { if (sx > 1) sx = Math.max(1, sx * f); else sy = Math.min(3, sy / f); }
  }

  // drawn bounds of every node and its label (labels hang off the right)
  function bounds() {
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (const m of T.sim.nodes()) {  // the nodes in the layout (in grow mode, only those shown so far)
      const n = disp.get(m.id), rr = r(n), lw = m.labelW || 0;
      x0 = Math.min(x0, n.x - rr); x1 = Math.max(x1, n.x + rr + 4 + lw);
      y0 = Math.min(y0, n.y - rr - 6); y1 = Math.max(y1, n.y + rr + 6);
    }
    return Number.isFinite(x0) ? { x0, y0, x1, y1 } : null;
  }
  // the transform that fits every node and its label into the open area, with a margin
  function framing() {
    const bb = bounds();
    if (!bb) return d3.zoomIdentity;
    const { x0, y0, x1, y1 } = bb;
    const ins = insetNow(), fw = Math.max(200, W - ins.left - ins.right), fh = Math.max(160, H - ins.top - ins.bottom);
    const m = 28, k = Math.max(0.3, Math.min(1.6, (fw - 2 * m) / (x1 - x0 || 1), (fh - 2 * m) / (y1 - y0 || 1)));
    return d3.zoomIdentity.translate(ins.left + fw / 2 - k * (x0 + x1) / 2, ins.top + fh / 2 - k * (y0 + y1) / 2).scale(k);
  }
  const ease = (a, b, t) => d3.zoomIdentity.translate(a.x + (b.x - a.x) * t, a.y + (b.y - a.y) * t).scale(a.k + (b.k - a.k) * t);
  svg.on("dblclick", () => { follow = true; kick(); });
  // our radio anchors the layout at the centre
  const me = nodes.find((n) => n.isLocal);
  if (me) { me.fx = cx; me.fy = cy; setTimeout(() => { me.fx = null; me.fy = null; }, 1500); }
  // "grow" mode (replay.js): hide radios not yet seen; a link shows once both its ends do
  // relayout: the layout itself holds only the shown radios, so it grows (and shrinks on a seek back).
  // A radio joining is placed beside a shown neighbour and the layout reheats to make room for it.
  const sim = T.sim, idOf = (x) => (typeof x === "object" ? x.id : x);
  let coolT = 0;
  // now (the replay's clock) = fade newcomers in over FADE_MS, driven by fade(now) each frame rather than a CSS
  // transition, so a frame-by-frame recording (tools/record-viz.mjs) fades at the same pace as live playback.
  const FADE_MS = 900, fading = new Map();
  const toggle = (sel, hidden, now, prop) => sel.each(function (x) {
    const hide = hidden(x), was = !this.classList.contains("unseen");
    this.classList.toggle("unseen", hide);
    if (!hide && !was && now != null) { this.style[prop] = "0"; fading.set(this, [now, prop]); }
    else if (hide) { fading.delete(this); this.style[prop] = ""; }
  });
  const fade = (now) => {
    for (const [el, [t0, prop]] of fading) {
      const k = Math.min(1, (now - t0) / FADE_MS);
      el.style[prop] = k < 1 ? String(1 - (1 - k) ** 3) : "";
      if (k >= 1) fading.delete(el);
    }
  };
  const setShown = (show, relayout = false, now = null) => {
    toggle(node, (n) => !show(n.id), now, "opacity");
    for (const sel of [edge, tether]) toggle(sel, (e) => !show(e.a) || !show(e.b), now, "strokeOpacity");
    hit.classed("unseen", (e) => !show(e.a) || !show(e.b));
    if (!relayout || T.sim !== sim) return;
    const keep = nodes.filter((n) => show(n.id)), ids = new Set(keep.map((n) => n.id));
    const had = new Set(sim.nodes().map((n) => n.id));
    const both = (e) => ids.has(idOf(e.source)) && ids.has(idOf(e.target));
    for (const n of keep) {
      if (had.has(n.id)) continue;
      const nb = [...links, ...tethers].filter((e) => both(e) && (idOf(e.source) === n.id || idOf(e.target) === n.id))
        .map((e) => byIdSim.get(idOf(e.source) === n.id ? idOf(e.target) : idOf(e.source))).find((m) => m && had.has(m.id));
      // arrive about where it will settle: one link out from the neighbour, facing away from the crowd
      // (the shown radios' centre), with a little spread so siblings don't stack
      const shown = sim.nodes(), mx = shown.reduce((t, m) => t + m.x, 0) / (shown.length || 1), my = shown.reduce((t, m) => t + m.y, 0) / (shown.length || 1);
      const ox = nb ? nb.x : cx, oy = nb ? nb.y : cy;
      const a = (nb && Math.hypot(ox - mx, oy - my) > 1 ? Math.atan2(oy - my, ox - mx) : Math.random() * 2 * Math.PI) + (Math.random() - 0.5) * 1.6;
      const d = (nb ? 90 : 40) * L;
      n.x = ox + d * Math.cos(a); n.y = oy + d * Math.sin(a); n.vx = n.vy = 0;
    }
    sim.nodes(keep);
    sim.force("link").links(links.filter(both));
    sim.force("tether").links(tethers.filter(both));
    // Keep the layout gently warm while radios arrive instead of restarting it for each one: a restart from
    // rest multiplied every force at once, which jolted the whole graph. alphaTarget ramps smoothly, and
    // after a few quiet seconds the layout cools just as smoothly.
    growing = true;
    sim.velocityDecay(0.6).alphaTarget(0.04).restart();
    clearTimeout(coolT);
    coolT = setTimeout(() => { if (T.sim === sim) sim.alphaTarget(0); }, 4000);
  };
  const byIdSim = new Map(nodes.map((n) => [n.id, n]));
  // fade mode (replay.js): nodeOp(id) / edgeOp(a, b) -> 0..1, applied as --decay so it multiplies with the
  // classes' own opacities (inferred links, hover dimming) and the grow fade-in; null clears it
  const setDecay = (nodeOp, edgeOp) => {
    const put = (el, v) => { if (el.__decay !== v) { el.__decay = v; v == null ? el.style.removeProperty("--decay") : el.style.setProperty("--decay", v); } };
    node.each(function (n) { put(this, nodeOp ? nodeOp(n.id).toFixed(2) : null); });
    for (const sel of [edge, tether]) sel.each(function (e) { put(this, edgeOp ? edgeOp(e.a, e.b).toFixed(2) : null); });
  };
  return { g, r, byId, pos: (id) => disp.get(id), setShown, fade, setDecay };
}

const median = (a) => { const s = [...a].sort((x, y) => x - y); return s[s.length >> 1]; };

// ?anon=1 on a map: the whole picture moves to a decoy place, so the shape, distances and timing stay true but the
// tiles underneath show somewhere else. By default only east-west (DECOY_DLON degrees), which keeps every distance
// exact (a north-south move would stretch it slightly); ?decoy=lat,lon picks the spot for the mesh's centre.
// It hides WHERE, not the shape: someone who knows the local mesh could still recognise its layout.
const DECOY_DLON = 14.5;
function decoyShift(centre) {
  const q = new URLSearchParams(location.search);
  if (!q.has("anon") || !centre) return null;
  const d = (q.get("decoy") || "").split(",").map(Number);
  const to = d.length === 2 && d.every(Number.isFinite) ? d : [centre[0], centre[1] + DECOY_DLON];
  const dLat = to[0] - centre[0], dLon = to[1] - centre[1];
  return (p) => [p[0] + dLat, p[1] + dLon];
}

// Leaflet map of the topology into el. extra = radios with no observed link (replay ghosts): drawn hollow
// at their reported or estimated position. still() is re-checked after the async estimate fetch so a stale
// draw is dropped. Returns { map, at(id) -> [lat, lon] | null, placed, drawn, estimated } or null.
async function buildGeoMap(d, el, still = () => true, extra = [], { trimOutliers = false, inset = null } = {}) {
  const all = [...d.nodes, ...extra];
  const byId = new Map(all.map((n) => [n.id, n]));
  const est = new Map(((await estimates()).estimates || []).map((e) => [e.id, e]));
  if (!still()) return null;
  el.replaceChildren();
  // our radio has no GPS; it sits in the house with the base station
  const base = all.find((n) => n.isBase && n.lat != null);
  const real = (n) => (!n ? null : n.lat != null ? [n.lat, n.lon] : n.isLocal && base ? [base.lat, base.lon]
    : est.has(n.id) ? [est.get(n.id).lat, est.get(n.id).lon] : null);
  const estimated = (n) => n.lat == null && !n.isLocal && est.has(n.id);
  const placed = all.filter(real);
  const shift = placed.length ? decoyShift([median(placed.map((n) => real(n)[0])), median(placed.map((n) => real(n)[1]))]) : null;
  const at = (n) => { const p = real(n); return p && shift ? shift(p) : p; };
  if (!placed.length) { el.innerHTML = '<p class="muted" style="padding:16px">None of these radios have shared a position yet.</p>'; return null; }
  const dark = matchMedia("(prefers-color-scheme: dark)").matches;
  const map = L.map(el, { scrollWheelZoom: true, zoomSnap: inset ? 0.25 : 1 });
  lkBaseTiles().addTo(map);
  let drawn = 0;
  for (const e of d.edges) {
    const a = at(byId.get(e.a)), b = at(byId.get(e.b));
    if (!a || !b || (a[0] === b[0] && a[1] === b[1])) continue;
    drawn++;
    const viaEst = estimated(byId.get(e.a)) || estimated(byId.get(e.b));
    L.polyline([a, b], { color: css("--text-muted"), weight: edgeWidth(e), opacity: viaEst ? 0.35 : e.measured ? 0.8 : 0.5, dashArray: e.measured && !viaEst ? null : "5 6" })
      .bindTooltip(edgeTip(e, byId), { sticky: true }).addTo(map);
  }
  // moving stations (a car): their GPS route over the range, faint; the dot sits where they spent the most time
  for (const [id, tr] of Object.entries(d.tracks || {})) {
    if (tr.length > 1) L.polyline(tr.map((p) => (shift ? shift([p[1], p[2]]) : [p[1], p[2]])), { color: css("--accent"), weight: 2, opacity: 0.35, dashArray: "2 5", interactive: false }).addTo(map);
  }
  const markers = new Map();
  for (const n of placed) {
    const e = estimated(n) ? est.get(n.id) : null, fill = css(nodeFill(n).slice(4, -1));
    if (e) L.circle(at(n), { radius: e.radiusKm * 1000, color: fill, weight: 1, dashArray: "4 4", opacity: 0.6, fillOpacity: 0.04, interactive: false }).addTo(map);
    const hollow = e || n.ghost;
    const mk = L.circleMarker(at(n), { radius: 5 + Math.sqrt(n.degree || 0) * 2.5, color: hollow ? fill : css("--surface-1"), weight: 2, dashArray: hollow ? "3 3" : null, fillColor: fill, fillOpacity: hollow ? 0.15 : 1 });
    markers.set(n.id, mk);
    mk
      .bindTooltip(`<b>${esc(n.name)}</b><br>${n.ghost ? `no observed RF link · reached us via ${esc(byId.get(n.via)?.name || "a relay")}` : `${n.degree} link${n.degree === 1 ? "" : "s"}`}${e ? `<br>estimated position ${prov("inferred")} ±${fmt(e.radiusKm, 1)} km<br><span class="t">${esc(e.method)}</span>` : ""}`)
      .on("click", () => { location.href = nodeHref(n.id); }).addTo(map);
  }
  // trimOutliers: frame the core of the mesh, not one far-off radio (it's still drawn, just off-screen)
  let frame = placed.map(at);
  if (trimOutliers && frame.length > 4) {
    const c = [median(frame.map((p) => p[0])), median(frame.map((p) => p[1]))];
    const dist = (p) => Math.hypot(p[0] - c[0], (p[1] - c[1]) * Math.cos((c[0] * Math.PI) / 180));
    const cut = 3 * Math.max(0.02, median(frame.map(dist)));
    frame = frame.filter((p) => dist(p) <= cut);
  }
  map.fitBounds(L.latLngBounds(frame).pad(inset ? 0.1 : 0.3), { maxZoom: 12,
    ...(inset ? { paddingTopLeft: [inset.left + 20, inset.top + 20], paddingBottomRight: [inset.right + 20, inset.bottom + 20] } : {}) });
  return { map, at: (id) => at(byId.get(id)), byId, markers, shift, placed: placed.length, drawn, estimated: placed.filter(estimated).length };
}

function drawTable(d) {
  const byId = new Map(d.nodes.map((n) => [n.id, n]));
  $("topoTable").innerHTML = d.edges.length ? `<div class="tablewrap"><table class="nodes"><thead><tr><th>Radio</th><th>Radio</th><th class="num">Observations</th><th>Evidence</th><th class="num">Avg SNR</th><th class="num">Last seen</th></tr></thead><tbody>${d.edges.map((e) => `<tr>
      <td><a class="nlink" href="${nodeHref(e.a)}">${esc(byId.get(e.a)?.name || e.a)}</a></td>
      <td><a class="nlink" href="${nodeHref(e.b)}">${esc(byId.get(e.b)?.name || e.b)}</a></td>
      <td class="num">${nf.format(e.count)}</td>
      <td>${Object.entries(e.sources).map(([k, v]) => `${esc(k)} ${v}`).join(", ")}${e.measured ? "" : ' <span class="muted">(inferred)</span>'}</td>
      <td class="num">${e.snr == null ? "—" : `${fmt(e.snr, 1)} dB`}</td>
      <td class="num">${esc(ago(e.last))}</td></tr>`).join("")}</tbody></table></div>`
    : '<p class="muted">No links observed in this range yet.</p>';
}

// Visualizations: Graph | Geographic, each with the replay player underneath (replay.js). The choice is
// remembered. Analytics has no switch and shows only the table.
if ($("topoMode")) {
  try { const v = localStorage.getItem("meshdash.topoView"); if (v === "geo" || (v === "drive" && $("driveMap"))) T.mode = v; } catch { /* storage blocked */ }
  for (const x of $("topoMode").children) x.classList.toggle("on", x.dataset.mode === T.mode);
  $("topoMode").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-mode]"); if (!b || b.dataset.mode === T.mode) return;
    T.mode = b.dataset.mode;
    try { localStorage.setItem("meshdash.topoView", T.mode); } catch { /* storage blocked */ }
    for (const x of $("topoMode").children) x.classList.toggle("on", x === b);
    renderTopology(A.range);
  });
} else T.mode = "table";
