// README screenshots, anonymized, from a running Lorakeet server (Node 22+, a Chromium-based browser).
//
//   node tools/screenshots.mjs [out-dir] [--station *] [--range 7d] [--url http://127.0.0.1:5190]
//
// Writes replay.png (Visualizations with the Key and Log, packets in flight), graph.png (the same graph,
// UI hidden) and analytics.png (the summary tiles and traffic chart). Names on the Visualizations page are
// anonymized with ?anon=1; the analytics crop is checked for anything that looks like a node name or id
// and refused if it finds one. Look at every image before publishing it anyway.
import { spawn } from "node:child_process";
import { mkdtempSync, rmSync, existsSync, mkdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const args = process.argv.slice(2);
const opt = (name, dflt) => { const i = args.indexOf(`--${name}`); return i >= 0 ? args[i + 1] : dflt; };
const OUT = args[0] && !args[0].startsWith("--") ? args[0] : "docs/screenshots";
const STATION = opt("station", "*"), RANGE = opt("range", "7d"), BASE = opt("url", "http://127.0.0.1:5190").replace(/\/$/, "");
const W = 1600, H = 900, PORT = 9334;
const CHROME = [process.env.CHROME,
  "C:/Program Files/Google/Chrome/Application/chrome.exe", "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "/usr/bin/google-chrome", "/usr/bin/chromium",
  "/usr/bin/chromium-browser"].find((p) => p && existsSync(p));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const PRESET = `try { if (location.origin === ${JSON.stringify(BASE)}) {
  localStorage.setItem("meshdash.station", ${JSON.stringify(STATION)});
  localStorage.setItem("meshdash.range", ${JSON.stringify(RANGE)});
  localStorage.setItem("meshdash.grow", "0");
  localStorage.setItem("meshdash.fade", JSON.stringify({ on: false }));
} } catch {}`;

async function main() {
  if (!CHROME) throw new Error("no Chromium-based browser found: set CHROME");
  mkdirSync(OUT, { recursive: true });
  const profile = mkdtempSync(join(tmpdir(), "lorakeet-shots-"));
  const chrome = spawn(CHROME, ["--headless=new", `--remote-debugging-port=${PORT}`, `--user-data-dir=${profile}`,
    `--window-size=${W},${H}`, "--hide-scrollbars", "--force-device-scale-factor=1", "--no-first-run", "about:blank"], { stdio: "ignore" });
  let ws;
  try {
    let page;
    for (let i = 0; i < 50 && !page; i++) {
      try { page = (await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json()).find((t) => t.type === "page"); } catch { /* starting */ }
      if (!page) await sleep(200);
    }
    ws = new WebSocket(page.webSocketDebuggerUrl);
    await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
    let seq = 0; const pending = new Map();
    ws.onmessage = (m) => { const d = JSON.parse(m.data); if (d.id && pending.has(d.id)) { const [res, rej] = pending.get(d.id); pending.delete(d.id); d.error ? rej(new Error(d.error.message)) : res(d.result); } };
    const cdp = (method, params = {}) => new Promise((res, rej) => { const id = ++seq; pending.set(id, [res, rej]); ws.send(JSON.stringify({ id, method, params })); });
    const js = async (expression) => {
      const r = await cdp("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
      if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text);
      return r.result.value;
    };
    const until = async (cond, what, ms = 30000) => { const t = Date.now(); while (Date.now() - t < ms) { if (await js(cond).catch(() => false)) return; await sleep(250); } throw new Error(`timed out: ${what}`); };
    const shot = async (file, clip) => {
      const { data } = await cdp("Page.captureScreenshot", { format: "png", ...(clip ? { clip: { ...clip, scale: 1 } } : {}) });
      writeFileSync(join(OUT, file), Buffer.from(data, "base64"));
      console.log(`wrote ${join(OUT, file)}`);
    };

    await cdp("Page.enable"); await cdp("Runtime.enable");
    await cdp("Emulation.setDeviceMetricsOverride", { width: W, height: H, deviceScaleFactor: 1, mobile: false });
    await cdp("Emulation.setEmulatedMedia", { features: [{ name: "prefers-color-scheme", value: "dark" }] });
    await cdp("Page.addScriptToEvaluateOnNewDocument", { source: PRESET });

    // 1. Visualizations, mid-replay, with the Key and the Log
    await cdp("Page.navigate", { url: `${BASE}/visualizations.html?anon=1` });
    await until(`typeof R !== "undefined" && !!(R.data && R.graph)`, "the replay");
    await sleep(6000);  // the layout settles
    await js(`seek(R.data.since + (R.data.until - R.data.since) * 0.55); R.speed = 600; document.getElementById("replayPlay").click(); 1`);
    await sleep(4500);
    await js(`document.getElementById("replayPlay").click(); 1`);  // pause with packets in flight
    await shot("replay.png");

    // 2. the same graph, UI hidden
    await js(`setClean(true); 1`);
    await until(`!!R.graph && document.body.classList.contains("vz-clean")`, "hidden UI");
    await sleep(7000);
    await shot("graph.png");

    // 3. Analytics: summary tiles and the traffic chart, refused if it shows anything name-like
    await cdp("Page.navigate", { url: `${BASE}/analytics.html` });
    await until(`document.querySelectorAll("#kpis > *").length > 0 && !!document.querySelector("section.card svg")`, "analytics");
    await sleep(2500);
    const box = await js(`(() => {
      const a = document.getElementById("kpis").getBoundingClientRect(), card = document.querySelector("#kpis ~ section.card, section.card");
      const b = card.getBoundingClientRect();
      const text = document.getElementById("kpis").innerText + " " + card.innerText;
      return { x: Math.floor(Math.min(a.x, b.x)) - 8, y: Math.floor(a.y) - 8, width: Math.ceil(Math.max(a.right, b.right) - Math.min(a.x, b.x)) + 16,
               height: Math.ceil(b.bottom - a.y) + 16, text };
    })()`);
    const suspicious = box.text.match(/![0-9a-f]{8}\b|\b[AKNW][A-Z]?\d[A-Z]{2,3}\b/g);
    if (suspicious) console.error(`analytics.png NOT written: the crop shows ${[...new Set(suspicious)].join(", ")}`);
    else await shot("analytics.png", { x: Math.max(0, box.x), y: Math.max(0, box.y), width: box.width, height: box.height });
  } finally {
    try { ws?.close(); } catch { /* closed */ }
    chrome.kill();
    await sleep(500);
    try { rmSync(profile, { recursive: true, force: true }); } catch { /* still letting go */ }
  }
}

main().catch((e) => { console.error(e.message || e); process.exit(1); });
