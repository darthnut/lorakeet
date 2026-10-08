// Record the Visualizations page's traffic replay to an MP4, frame by frame, for sharing.
// Needs Node 22+ (built-in WebSocket), a Chromium-based browser and ffmpeg, and a running Lorakeet server.
//
//   node tools/record-viz.mjs [out.mp4] [--range 7d] [--station *] [--fps 60] [--no-anon] [--seconds N]
//        [--ramp-hours 3] [--fade auto|<hours>] [--fade-misses 3] [--fade-remove] [--texts] [--url ...]
// --texts shows the texts panel (lines to each sender; "a text message" in place of the words when anonymized).
//
// Chrome (headless, 1920x1080, dark theme) is driven over the DevTools protocol with Node's built-in WebSocket:
// no npm installs. The page's clock (performance.now, requestAnimationFrame, timers) is replaced by a simulated
// one that advances exactly one frame per capture, so the video is perfectly smooth however long each frame
// takes to render. Frames are piped to ffmpeg (H.264, yuv420p, +faststart).
//
// Choreography: the Key (the type legend and node legend) over the starting graph, then straight into the
// full-window visualization in grow mode, playing at 10 min/s while the network assembles, ramping to 1 h/s
// --ramp-hours (default 3) into the range (most radios appear in the first hours), to the end, then a short hold.
// Names are anonymized (?anon=1: Station 1, Router 2, Node 14...) unless --no-anon.
import { spawn } from "node:child_process";
import { mkdtempSync, rmSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const args = process.argv.slice(2);
const opt = (name, dflt) => { const i = args.indexOf(`--${name}`); return i >= 0 ? args[i + 1] : dflt; };
const OUT = args[0] && !args[0].startsWith("--") ? args[0] : "recordings/lorakeet-replay.mp4";
const RANGE = opt("range", "7d"), STATION = opt("station", "*"), FPS = Number(opt("fps", 60));
const ANON = !args.includes("--no-anon"), MAX_S = Number(opt("seconds", 600)), RAMP_H = Number(opt("ramp-hours", 3));
// fade mode (off unless --fade): radios dim when they go quiet; see the Fade menu on the page
const FADE = { on: args.includes("--fade"), after: (() => { const v = opt("fade", "auto"); return !v || v.startsWith("--") ? "auto" : v; })(),
  misses: Number(opt("fade-misses", 3)), remove: args.includes("--fade-remove") };
const W = 1920, H = 1080, BASE = opt("url", "http://127.0.0.1:5190").replace(/\/$/, ""), PORT = 9333;
// Chrome/Edge/Chromium and ffmpeg: $CHROME / $FFMPEG, else the usual install locations, else ffmpeg on PATH.
const CHROME = [process.env.CHROME,
  "C:/Program Files/Google/Chrome/Application/chrome.exe", "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
  "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe", "C:/Program Files/Microsoft/Edge/Application/msedge.exe",
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "/Applications/Chromium.app/Contents/MacOS/Chromium",
  "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable", "/usr/bin/chromium", "/usr/bin/chromium-browser",
  "/snap/bin/chromium"].find((p) => p && existsSync(p));
const FFMPEG = [process.env.FFMPEG, join(process.env.USERPROFILE || "", "ffmpeg/bin/ffmpeg.exe")].find((p) => p && existsSync(p)) || "ffmpeg";
const INTRO_S = 4.5, INTRO_FADE_S = 0.6, OUTRO_S = 2, RAMP_S = 2.5, SLOW = 600, FAST = 3600;

// Runs in the page before any of its scripts: a simulated clock the recorder advances one frame at a time.
const FAKE_CLOCK = `(() => {
  let now = 0, rafs = [], rafId = 0, timers = [], tid = 0, faking = false;
  const real = { st: setTimeout, ct: clearTimeout, si: setInterval, ci: clearInterval };
  performance.now = () => now;
  window.requestAnimationFrame = (cb) => { rafs.push([++rafId, cb]); return rafId; };
  window.cancelAnimationFrame = (id) => { rafs = rafs.filter((r) => r[0] !== id); };
  const add = (cb, ms, every, a) => { timers.push({ id: ++tid, at: now + Math.max(0, ms || 0), cb, every, a }); return tid; };
  window.__film = {
    // after the page has loaded (its fetches done), timers move onto the simulated clock too
    fakeTimers() {
      if (faking) return; faking = true;
      window.setTimeout = (cb, ms, ...a) => add(cb, ms, 0, a);
      window.setInterval = (cb, ms, ...a) => add(cb, ms, Math.max(1, ms || 0), a);
      window.clearTimeout = window.clearInterval = (id) => { timers = timers.filter((t) => t.id !== id); };
    },
    step(ms) {
      now += ms;
      for (;;) {
        const due = timers.filter((t) => t.at <= now).sort((x, y) => x.at - y.at)[0];
        if (!due) break;
        if (due.every) due.at += due.every; else timers = timers.filter((t) => t !== due);
        try { typeof due.cb === "function" ? due.cb(...due.a) : 0; } catch (e) { console.error(e); }
      }
      const q = rafs; rafs = [];
      for (const [, cb] of q) { try { cb(now); } catch (e) { console.error(e); } }
      return now;
    },
    now: () => now,
  };
  try {
    if (location.origin === ${JSON.stringify(BASE)}) {
      localStorage.setItem("meshdash.grow", "1");
      localStorage.setItem("meshdash.range", ${JSON.stringify(RANGE)});
      localStorage.setItem("meshdash.station", ${JSON.stringify(STATION)});
      localStorage.setItem("meshdash.fade", ${JSON.stringify(JSON.stringify(FADE))});
      localStorage.setItem("meshdash.chat", ${JSON.stringify(args.includes("--texts") ? "1" : "0")});
    }
  } catch {}
})();`;

// Runs in the page once loaded: the Key intro overlay and the speed plan.
const DIRECTOR = `(() => {
  const key = document.querySelector("#vzKey .vz-box-body").cloneNode(true);
  key.querySelector(".vz-about")?.remove();
  const wrap = document.createElement("div");
  wrap.id = "filmKey";  // a dimmed backdrop with the Key centred on it
  Object.assign(wrap.style, { position: "fixed", inset: 0, zIndex: 2000, background: "rgba(0,0,0,.55)", pointerEvents: "none" });
  const box = document.createElement("div");
  box.innerHTML = '<div class="filmKey-title">Key</div>';
  box.append(key);
  Object.assign(box.style, { position: "fixed", left: "50%", top: "50%", transform: "translate(-50%, -50%) scale(1.7)",
    transformOrigin: "center", zIndex: 2000, padding: "14px 18px", maxWidth: "560px", borderRadius: "12px",
    background: "color-mix(in srgb, var(--surface-1) 94%, transparent)", border: "1px solid var(--border)",
    boxShadow: "0 10px 40px rgba(0,0,0,.45)", pointerEvents: "none" });
  wrap.append(box);
  const st = document.createElement("style");
  st.textContent = "#filmKey .filmKey-title{font-weight:600;font-size:15px;margin-bottom:8px;color:var(--text-primary)}" +
    "#filmKey .legend-row{margin:0 0 8px} #filmKey .chip{pointer-events:none}";
  document.head.append(st);
  document.body.append(wrap);
  window.__plan = { rampAt: R.data.since + ${RAMP_H} * 3600, rampStart: null };
  return { since: R.data.since, until: R.data.until, rampAt: window.__plan.rampAt, radios: R.revealTimes.length };
})()`;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function main() {
  if (!CHROME) throw new Error("Chrome, Edge or Chromium not found: set CHROME to its executable");
  const profile = mkdtempSync(join(tmpdir(), "lorakeet-rec-"));
  const chrome = spawn(CHROME, ["--headless=new", `--remote-debugging-port=${PORT}`, `--user-data-dir=${profile}`,
    `--window-size=${W},${H}`, "--hide-scrollbars", "--force-device-scale-factor=1", "--mute-audio", "--no-first-run",
    "--no-default-browser-check", "about:blank"], { stdio: "ignore" });
  let ws;
  try {
    let page;
    for (let i = 0; i < 50 && !page; i++) {
      try { page = (await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json()).find((t) => t.type === "page"); } catch { /* starting */ }
      if (!page) await sleep(200);
    }
    if (!page) throw new Error("Chrome's DevTools endpoint didn't come up");
    ws = new WebSocket(page.webSocketDebuggerUrl);
    await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
    let seq = 0; const pending = new Map();
    ws.onmessage = (m) => {
      const d = JSON.parse(m.data);
      if (d.id && pending.has(d.id)) { const [res, rej] = pending.get(d.id); pending.delete(d.id); d.error ? rej(new Error(d.error.message)) : res(d.result); }
      else if (d.method === "Runtime.exceptionThrown") console.error("page error:", d.params.exceptionDetails.exception?.description || d.params.exceptionDetails.text);
    };
    const cdp = (method, params = {}) => new Promise((res, rej) => { const id = ++seq; pending.set(id, [res, rej]); ws.send(JSON.stringify({ id, method, params })); });
    const js = async (expression) => {
      const r = await cdp("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
      if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text);
      return r.result.value;
    };

    await cdp("Page.enable"); await cdp("Runtime.enable");
    await cdp("Emulation.setDeviceMetricsOverride", { width: W, height: H, deviceScaleFactor: 1, mobile: false });
    await cdp("Emulation.setEmulatedMedia", { features: [{ name: "prefers-color-scheme", value: "dark" }, { name: "prefers-reduced-motion", value: "no-preference" }] });
    await cdp("Page.addScriptToEvaluateOnNewDocument", { source: FAKE_CLOCK });
    await cdp("Page.navigate", { url: `${BASE}/visualizations.html${ANON ? "?anon=1" : ""}` });

    // load, then hidden-UI mode (which redraws for the full window); a few frames so each redraw can run
    const ready = async (cond, what) => { for (let i = 0; i < 300; i++) { await js("__film.step(16.667)"); if (await js(cond)) return; await sleep(50); } throw new Error(`timed out waiting for ${what}`); };
    await ready("typeof R !== \"undefined\" && !!(R.data && R.graph && R.revealTimes)", "the replay to load");
    await js("setClean(true); document.getElementById('vzCleanClock')?.classList.remove('off'); R.gen");
    const gen = await js("R.gen");
    await ready(`R.gen > ${gen} && !!R.graph`, "the full-window redraw");
    await js("seek(R.data.since); R.speed = 600; __film.fakeTimers(); 1");
    const info = await js(DIRECTOR);
    console.log(`range ${new Date(info.since * 1000).toLocaleString()} to ${new Date(info.until * 1000).toLocaleString()}, ${info.radios} radios; ramp at ${new Date(info.rampAt * 1000).toLocaleString()}`);

    const ff = spawn(FFMPEG, ["-y", "-loglevel", "error", "-f", "image2pipe", "-framerate", String(FPS), "-c:v", "mjpeg", "-i", "-",
      "-c:v", "libx264", "-preset", "slow", "-crf", "18", "-pix_fmt", "yuv420p", "-movflags", "+faststart", OUT], { stdio: ["pipe", "inherit", "inherit"] });
    const done = new Promise((res, rej) => ff.on("close", (c) => (c ? rej(new Error(`ffmpeg exited ${c}`)) : res())));
    const dt = 1000 / FPS;
    let frames = 0;
    const shoot = async () => {
      const { data } = await cdp("Page.captureScreenshot", { format: "jpeg", quality: 94, captureBeyondViewport: false });
      if (!ff.stdin.write(Buffer.from(data, "base64"))) await new Promise((r) => ff.stdin.once("drain", r));
      frames++;
      if (frames % (FPS * 5) === 0) process.stdout.write(`  ${(frames / FPS).toFixed(0)} s of video\n`);
    };

    // 1. the Key over the starting graph, then fade it away
    const introN = Math.round(INTRO_S * FPS), fadeN = Math.round(INTRO_FADE_S * FPS);
    for (let i = 0; i < introN + fadeN; i++) {
      const o = i < introN ? 1 : 1 - (i - introN) / fadeN;
      await js(`document.getElementById("filmKey").style.opacity = "${o.toFixed(3)}"; __film.step(${dt}); 1`);
      await shoot();
    }
    await js(`document.getElementById("filmKey").remove(); document.getElementById("replayPlay").click(); 1`);

    // 2. play: 10 min/s, a geometric ramp to 1 h/s once most radios are in, to the end of the range
    const step = `(() => {
      const p = window.__plan;
      if (R.t >= p.rampAt) {
        if (p.rampStart == null) p.rampStart = __film.now();
        const k = Math.min(1, (__film.now() - p.rampStart) / ${RAMP_S * 1000}), e = k * k * (3 - 2 * k);
        R.speed = ${SLOW} * Math.pow(${FAST / SLOW}, e);
      }
      __film.step(${dt});
      return [R.t >= R.data.until && !R.playing, R.flights.length];
    })()`;
    let endAt = null;
    while (frames < MAX_S * FPS) {
      const [ended, flying] = await js(step);
      await shoot();
      if (ended && endAt == null) endAt = frames;
      if (endAt != null && (frames - endAt) > OUTRO_S * FPS && !flying) break;
    }
    ff.stdin.end();
    await done;
    console.log(`wrote ${OUT}: ${frames} frames, ${(frames / FPS).toFixed(1)} s at ${FPS} fps`);
  } finally {
    try { ws?.close(); } catch { /* closed */ }
    chrome.kill();
    await sleep(500);
    try { rmSync(profile, { recursive: true, force: true }); } catch { /* chrome still letting go */ }
  }
}

main().catch((e) => { console.error(e.message || e); process.exit(1); });
