"use strict";
/* Groovy mode (Visualizations page, the 🌈 Groovy button or V): a just-for-fun psychedelic skin over the replay.
   Nothing about the data changes; it only draws on top of it:

   - a back canvas under the graph: slow lava-lamp blobs on a dark ground (the map gets its tiles hue-cycled instead);
   - a front canvas over the graph and map: comet trails behind every packet in flight, a burst of sparks where each
     hop lands, rainbow ripples where a packet is sent, and glowing auras on radios that just spoke;
   - the graph's links and radios recoloured in the vibe's palette, shifting over time;
   - optionally the whole particle layer mirrored into a six-way kaleidoscope.

   It reads the replay's state (R in replay.js: the flights in the air and R.graph.pos) every frame and never changes
   it. Settings live in localStorage `meshdash.groovy`; `?groovy=rainbow|neon|lava|aurora` turns it on for one visit
   (e.g. a recording: tools/record-viz.mjs --groovy). Prefers-reduced-motion keeps the colours but no motion. */

const GROOVY_DEFAULTS = { on: false, vibe: "rainbow", intensity: "groovy", trails: true, bursts: true, auras: true,
                          background: true, kaleido: false };
const VIBES = {
  // each: a colour for a seed (0..1) at time t (seconds), as [hue, saturation %, lightness %]
  rainbow: (s, t) => [(s * 360 + t * 40) % 360, 100, 60],
  neon: (s, t) => [[315, 185, 95, 275, 45][Math.floor(s * 5 + t * 0.3) % 5], 100, 58],
  lava: (s, t) => [(345 + Math.sin(s * 9 + t * 0.6) * 35 + 360) % 360, 100, 55 + Math.sin(s * 5 + t) * 8],
  aurora: (s, t) => [s < 0.75 ? 140 + Math.sin(s * 11 + t * 0.5) * 45 : 285 + Math.sin(t * 0.7) * 20, 90, 58],
};
const LEVEL = { chill: 0.45, groovy: 1, "far-out": 2.2 };
const MAX_PARTICLES = 5000;

const G = { opts: { ...GROOVY_DEFAULTS }, parts: [], rings: [], active: new Map(), seen: new WeakMap(), raf: 0,
            last: 0, sprites: new Map(), edgeT: 0 };
try { Object.assign(G.opts, JSON.parse(localStorage.getItem("meshdash.groovy") || "{}")); } catch { /* storage blocked */ }
{
  const v = new URLSearchParams(location.search).get("groovy");  // ?groovy=neon: on for this visit, not saved
  if (v !== null) Object.assign(G.opts, { on: true }, VIBES[v] ? { vibe: v } : {});
}
const still = matchMedia("(prefers-reduced-motion: reduce)").matches;

const stageEl = document.getElementById("topoReplay");
const back = document.createElement("canvas"), front = document.createElement("canvas"), mirror = document.createElement("canvas");
back.className = "groovy-back"; front.className = "groovy-front";
back.setAttribute("aria-hidden", "true"); front.setAttribute("aria-hidden", "true");
stageEl.prepend(back);
stageEl.append(front);
const bctx = back.getContext("2d"), fctx = front.getContext("2d"), mctx = mirror.getContext("2d");

const hsl = ([h, s, l], a = 1) => `hsla(${h.toFixed(0)} ${s}% ${l}% / ${a})`;
const color = (seed, t) => VIBES[G.opts.vibe]?.(seed, t) || VIBES.rainbow(seed, t);

function sprite(h) {
  // a soft glowing dot, one per 10° of hue, drawn once and stamped thousands of times
  const key = Math.round(h / 10) % 36;
  if (G.sprites.has(key)) return G.sprites.get(key);
  const c = document.createElement("canvas"); c.width = c.height = 64;
  const x = c.getContext("2d"), g = x.createRadialGradient(32, 32, 0, 32, 32, 32);
  g.addColorStop(0, "hsla(0 0% 100% / 1)");
  g.addColorStop(0.18, `hsla(${key * 10} 100% 70% / .95)`);
  g.addColorStop(0.5, `hsla(${key * 10} 100% 55% / .35)`);
  g.addColorStop(1, `hsla(${key * 10} 100% 50% / 0)`);
  x.fillStyle = g; x.fillRect(0, 0, 64, 64);
  G.sprites.set(key, c);
  return c;
}

function fit() {
  const dpr = Math.min(2, devicePixelRatio || 1), w = stageEl.clientWidth, h = stageEl.clientHeight;
  for (const c of [back, front, mirror]) {
    if (c.width !== Math.round(w * dpr) || c.height !== Math.round(h * dpr)) { c.width = Math.round(w * dpr); c.height = Math.round(h * dpr); }
  }
  for (const x of [bctx, fctx, mctx]) x.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { w, h };
}

// the replay's layer coordinates (graph: inside the zoomed group; map: the container) -> stage pixels
function toStage() {
  const layer = typeof R !== "undefined" && R.layer;
  const m = layer?.getScreenCTM?.();
  if (!m) return null;
  const r = stageEl.getBoundingClientRect();
  return (p) => (p ? { x: m.a * p.x + m.c * p.y + m.e - r.left, y: m.b * p.x + m.d * p.y + m.f - r.top } : null);
}

function emit(x, y, n, spec) {
  for (let i = 0; i < n && G.parts.length < MAX_PARTICLES; i++) {
    const a = Math.random() * Math.PI * 2, v = spec.speed * (0.35 + Math.random() * 0.65);
    G.parts.push({ x, y, vx: Math.cos(a) * v + (spec.vx || 0), vy: Math.sin(a) * v + (spec.vy || 0), age: 0,
                   life: spec.life * (0.6 + Math.random() * 0.8), size: spec.size * (0.6 + Math.random() * 0.8),
                   seed: spec.seed ?? Math.random(), drag: spec.drag ?? 1.6 });
  }
}

function watchFlights(t, map, k) {
  // new flights -> a ripple at the sender; each finished hop -> a burst where it landed; dots in the air -> trails
  for (const f of R.flights || []) {
    let st = G.seen.get(f);
    const age = performance.now() - f.start, seg = f.seg || 420, leg = Math.floor(age / seg);
    if (!st) {
      st = { leg: 0, seed: Math.random() };
      G.seen.set(f, st);
      const o = map(R.graph?.pos(f.e.kind === "tx" ? R.data.local : f.e.from));
      if (o) {
        G.rings.push({ x: o.x, y: o.y, age: 0, life: 1100, seed: st.seed, big: f.e.group === "Text" || f.e.kind === "trace" });
        G.active.set(f.e.from, t);
      }
    }
    if (leg > st.leg && st.leg < f.legs.length) {  // a hop just landed
      const done = f.legs[Math.min(st.leg, f.legs.length - 1)];
      const p = done && map(R.graph?.pos(done[1]));
      if (p && G.opts.bursts) emit(p.x, p.y, Math.round(26 * k), { speed: 170, life: 1300, size: 9, seed: st.seed });
      if (done) G.active.set(done[1], t);
      st.leg = leg;
    }
    if (G.opts.trails && f.dot.style.opacity !== "0") {
      const p = map({ x: +f.dot.getAttribute("cx"), y: +f.dot.getAttribute("cy") });
      if (p && Number.isFinite(p.x)) {
        const prev = st.p || p, n = Math.max(1, Math.round(3 * k));
        for (let i = 0; i < n; i++) {  // spread along the frame's movement so fast dots leave a solid comet
          const q = i / n;
          emit(prev.x + (p.x - prev.x) * q, prev.y + (p.y - prev.y) * q, 1, { speed: 18, life: 900, size: 11, seed: st.seed, drag: 2.5 });
        }
        st.p = p;
      }
    }
  }
}

function drawBack(t, w, h) {
  bctx.globalCompositeOperation = "source-over";
  bctx.fillStyle = "#07040f";
  bctx.fillRect(0, 0, w, h);
  if (!G.opts.background) return;
  bctx.globalCompositeOperation = "lighter";
  const n = 7, R0 = Math.max(w, h) * 0.38;
  for (let i = 0; i < n; i++) {  // lava-lamp blobs on slow Lissajous paths
    const s = i / n, tt = still ? 0 : t;
    const x = w * (0.5 + 0.42 * Math.sin(tt * 0.11 * (1 + s) + i * 2.1));
    const y = h * (0.5 + 0.40 * Math.cos(tt * 0.09 * (1.3 - s) + i * 1.3));
    const r = R0 * (0.55 + 0.35 * Math.sin(tt * 0.2 + i));
    const g = bctx.createRadialGradient(x, y, 0, x, y, r);
    const c = color(s, tt);
    g.addColorStop(0, hsl([c[0], c[1], 45], 0.42));
    g.addColorStop(1, hsl([c[0], c[1], 30], 0));
    bctx.fillStyle = g;
    bctx.fillRect(0, 0, w, h);
  }
}

function recolorGraph(t) {
  // links and radios in the vibe's colours; a few times a second is plenty for a slow drift
  if (t - G.edgeT < 0.08) return;
  G.edgeT = t;
  const root = document.getElementById("topoReplayGraph");
  root.querySelectorAll(".edge").forEach((el, i) => { el.style.stroke = hsl(color((i * 0.137) % 1, t)); });
  root.querySelectorAll(".node > circle").forEach((el, i) => { el.style.fill = hsl(color((i * 0.291 + 0.5) % 1, t)); });
  // the map's tiles: hue-cycled through the spectrum
  stageEl.style.setProperty("--groovy-hue", `${((still ? 0 : t) * 25) % 360}deg`);
}
function uncolorGraph() {
  document.querySelectorAll("#topoReplayGraph .edge, #topoReplayGraph .node > circle").forEach((el) => { el.style.stroke = ""; el.style.fill = ""; });
}

function drawFront(t, dt, w, h, map) {
  const k = LEVEL[G.opts.intensity] || 1;
  const ctx = G.opts.kaleido ? mctx : fctx;
  fctx.globalCompositeOperation = "source-over";
  fctx.clearRect(0, 0, w, h);
  if (ctx === mctx) { mctx.globalCompositeOperation = "source-over"; mctx.clearRect(0, 0, w, h); }
  ctx.globalCompositeOperation = "lighter";

  if (G.opts.auras && map && R.graph) {  // (no graph for a moment while a view is being redrawn)  // radios that just sent or received glow, fading over 2.5 s; "far out": everyone breathes
    const ids = new Set(G.active.keys());
    if (G.opts.intensity === "far-out" && R.graph?.byId) for (const id of R.graph.byId.keys()) ids.add(id);
    let i = 0;
    for (const id of ids) {
      const since = t - (G.active.get(id) ?? -99), p = map(R.graph.pos(id));
      i++;
      if (since > 2.5 && G.opts.intensity !== "far-out") { G.active.delete(id); continue; }
      if (!p) continue;
      const lit = Math.max(0, 1 - since / 2.5), breathe = G.opts.intensity === "far-out" ? 0.18 + 0.12 * Math.sin(t * 2 + i) : 0;
      const a = Math.min(1, lit * 0.9 + breathe);
      if (a < 0.02) continue;
      const c = color((i * 0.173) % 1, t), size = 26 + 50 * lit;
      ctx.globalAlpha = a;
      ctx.drawImage(sprite(c[0]), p.x - size / 2, p.y - size / 2, size, size);
    }
  }

  for (const r of G.rings) r.age += dt;  // ripples: a rainbow ring in 12 arcs, each a step round the palette
  G.rings = G.rings.filter((r) => r.age < r.life);
  ctx.lineWidth = 3;
  for (const r of G.rings) {
    const q = r.age / r.life, rad = 8 + q * (r.big ? 90 : 55);
    ctx.globalAlpha = (1 - q) * 0.9;
    for (let s = 0; s < 12; s++) {
      ctx.strokeStyle = hsl(color((r.seed + s / 12) % 1, t));
      ctx.beginPath(); ctx.arc(r.x, r.y, rad, (s / 12) * Math.PI * 2, ((s + 1) / 12) * Math.PI * 2); ctx.stroke();
    }
  }

  const keep = [];
  for (const p of G.parts) {  // sparks and comet dust: drift, slow down, shrink, fade
    p.age += dt;
    if (p.age >= p.life) continue;
    const s = dt / 1000, damp = Math.exp(-p.drag * s);
    p.vx *= damp; p.vy *= damp; p.x += p.vx * s; p.y += p.vy * s;
    const q = p.age / p.life, size = p.size * (1 - q * 0.7);
    ctx.globalAlpha = (1 - q) ** 1.3;
    ctx.drawImage(sprite(color(p.seed, t + p.age / 1000)[0]), p.x - size / 2, p.y - size / 2, size, size);
    keep.push(p);
  }
  G.parts = keep;
  ctx.globalAlpha = 1;

  if (ctx === mctx) {  // kaleidoscope: six rotated copies (alternate ones mirrored) round the middle of the stage
    fctx.globalCompositeOperation = "lighter";
    const cx = w / 2, cy = h / 2, dpr = mirror.width / w;
    for (let i = 0; i < 6; i++) {
      fctx.save();
      fctx.translate(cx, cy); fctx.rotate((i * Math.PI) / 3); if (i % 2) fctx.scale(1, -1); fctx.translate(-cx, -cy);
      fctx.globalAlpha = i === 0 ? 1 : 0.55;
      fctx.drawImage(mirror, 0, 0, mirror.width / dpr, mirror.height / dpr);
      fctx.restore();
    }
  }
}

function loop(now) {
  G.raf = 0;
  if (!G.opts.on) return;
  const dt = Math.min(64, G.last ? now - G.last : 16), t = now / 1000;
  G.last = now;
  G.raf = requestAnimationFrame(loop);  // first, so one bad frame (a view mid-redraw) can't stop the show
  const { w, h } = fit();
  if (document.body.classList.contains("vz-drive")) { fctx.clearRect(0, 0, w, h); return; }  // Coverage stays as it is
  drawBack(t, w, h);
  const map = toStage();
  if (map && !still && R.graph) watchFlights(t, map, LEVEL[G.opts.intensity] || 1);
  drawFront(t, dt, w, h, map);
  recolorGraph(t);
}

function setGroovy(changes, save = true) {
  Object.assign(G.opts, changes);
  if (save) try { localStorage.setItem("meshdash.groovy", JSON.stringify(G.opts)); } catch { /* storage blocked */ }
  document.body.classList.toggle("groovy", G.opts.on);
  if (G.opts.on) {
    if (!G.raf) { G.last = 0; G.raf = requestAnimationFrame(loop); }
  } else {
    if (G.raf) cancelAnimationFrame(G.raf);
    G.raf = 0; G.parts = []; G.rings = []; G.active.clear();
    uncolorGraph();
    fctx.clearRect(0, 0, front.width, front.height);
  }
}
window.Groovy = { set: setGroovy, opts: G.opts, VIBES };
setGroovy({}, false);  // as stored (or as ?groovy= asked, which isn't saved)
