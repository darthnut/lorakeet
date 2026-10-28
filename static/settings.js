"use strict";
/* The Settings page: alert settings (live, /api/settings) and Lorakeet's own lorakeet.toml settings (/api/preferences,
   this computer only; saved together, most apply after a restart). One Save for both, like the Radio page. */

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const post = (url, body) => fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
  .then(async (r) => ({ status: r.status, ...(await r.json().catch(() => ({ error: `error ${r.status}` }))) }))
  .catch(() => ({ error: "Couldn't reach Lorakeet: is it still running?" }));
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

// Every field once. src "alerts" = the alert settings (apply at once); otherwise a lorakeet.toml [section] key.
const SECTIONS = [
  { id: "alerts", title: "Alerts", src: "alerts", about: "What raises an alert under the bell, and what also pops up as a notification on this computer. These apply as soon as you save.",
    fields: [
      { k: "watched", type: "watched", label: "Watched radios", help: "Star a radio on its Analytics page or in the map's node panel to watch it." },
      { k: "silenceHours", type: "num", step: 0.5, min: 0, unit: "hours", label: "A watched radio is silent for", help: "Counted only while Lorakeet is listening, so restarts never cause false alarms." },
      { k: "notifySilence", type: "bool", label: "…and notify" },
      { k: "notifyBack", type: "bool", label: "Notify when it's heard again" },
      { k: "lowBatteryPct", type: "num", step: 1, min: 0, max: 100, unit: "%", label: "A watched radio's battery is at or below" },
      { k: "notifyLowBattery", type: "bool", label: "…and notify" },
      { k: "notifyNewNodes", type: "bool", label: "Notify about new radios" },
      { k: "newNodeBatchMin", type: "num", step: 1, min: 1, unit: "min", label: "…at most once every" },
      { k: "newStationQuietH", type: "num", step: 1, min: 0, unit: "h", label: "Quiet period for a new or moved station", help: "A station that just started logging, or moved (its Wi-Fi changed to another network), hears dozens of radios for the first time. For this long they go into one alert per station instead of one each, with no notifications." },
      { k: "newNodeBurst", type: "num", step: 1, min: 2, label: "Roll a burst of new radios into one alert at", help: "This many new radios at one station within an hour become one alert, which keeps growing until an hour passes with no more." },
      { k: "notifyHealth", type: "bool", label: "Notify about new health warnings", help: "From the daily check (busy channel, weak keys, misconfigured radios…)." },
      { k: "stationSilenceMin", type: "num", step: 1, min: 1, unit: "min", label: "A station hasn't synced for", help: "On a hub: another of your Lorakeets has gone quiet." },
      { k: "notifyStation", type: "bool", label: "…and notify" },
      { k: "anomalyAlerts", type: "bool", label: "Alert when a radio behaves unlike itself", help: "A steady radio unusually quiet, sending or relaying much less than usual, or heard much weaker: judged against its own last two weeks, not a fixed threshold." },
      { k: "notifyAnomalies", type: "bool", label: "…and notify" },
      { k: "autoTraceroute", type: "bool", label: "Scheduled traceroutes", warn: "Transmits: a traceroute now and then to map routes through the mesh.", help: "Maps routes through the mesh, one radio at a time." },
      { k: "autoTracerouteMin", type: "num", step: 1, min: 5, unit: "min", label: "…at most one every" },
      { k: "autoTracerouteMaxChUtil", type: "num", step: 1, min: 1, max: 100, unit: "%", label: "…skipped while the channel is busier than" },
      { k: "autoTracerouteRefreshHours", type: "num", step: 1, min: 1, unit: "hours", label: "…re-trace a radio after" },
      { k: "autoTracerouteRetryHours", type: "num", step: 1, min: 1, unit: "hours", label: "…retry a radio that didn't answer after" },
      { k: "_lasttrace", type: "info", label: "Last scheduled traceroute" },
    ] },
  { id: "station", title: "This station", src: "toml", about: "Where this Lorakeet listens from. Also on the Radio page.",
    fields: [
      { s: "station", k: "name", type: "text", max: 60, label: "Name", help: "Default: the radio's own name." },
      { s: "station", k: "location", type: "latlon", label: "Antenna position", help: "Places this station on the maps and helps estimate where radios without a position are. Not for a station that moves." },
      { s: "station", k: "note", type: "text", max: 120, label: "Note", help: "e.g. attic window, facing north." },
      { s: "station", k: "mobile", type: "bool", label: "This station moves (a car)", help: "Its position then comes from its radio's GPS, logged along the way." },
      { s: "station", k: "drive_pings", type: "bool", label: "Drive pings", warn: "Transmits: a tiny message on a private channel as the car moves.", help: "So the Coverage view can map where the mesh heard the car. Needs a moving station." },
      { s: "station", k: "drive_ping_m", type: "num", step: 100, min: 200, unit: "m", label: "…every" },
      { s: "station", k: "drive_ping_min_s", type: "num", step: 10, min: 30, unit: "s", label: "…but not more often than" },
      { s: "station", k: "drive_ping_channel", type: "text", max: 11, label: "…on the channel named", help: "A channel with its own private key (not the public one)." },
    ] },
  { id: "radio", title: "Radio connection", src: "toml", about: "Which radio Lorakeet logs from.",
    fields: [
      { s: "radio", k: "port", type: "text", max: 64, list: "ports", label: "USB port", help: "Empty = find the radio automatically. Set it (e.g. COM7 or /dev/ttyACM0) to choose one radio when several are plugged in." },
      { s: "radio", k: "host", type: "text", max: 128, label: "Or a radio on your network", help: "Its IP address or name, for a Wi-Fi or Ethernet radio. Set this or a USB port, not both. Over the network there's no per-reception detail (radios send their debug log only over USB)." },
      { s: "radio", k: "tcp_port", type: "num", step: 1, min: 1, max: 65535, label: "…its API port", help: "4403 unless you changed it on the radio." },
    ] },
  { id: "access", title: "Access from other devices", src: "toml", about: "Who besides this computer can open the dashboard. It's plain HTTP for your own network: never expose it to the internet.",
    fields: [
      { s: "http", k: "lan", type: "select", label: "Other devices on your network",
        options: [["off", "Can't open it (this computer only)"], ["view", "See it read-only (full access after logging in)"], ["login", "See nothing until they log in"]],
        help: "On Windows, the firewall may need to allow Python (or TCP port 5190) on Private networks." },
      { k: "_password", type: "password", label: "Password" },
      { s: "http", k: "hostnames", type: "list", label: "Names it may be opened by", help: "Besides IP addresses and localhost, e.g. desk-pc or lorakeet-station.local, one per line. Other names are refused, so a website can't reach the dashboard through your browser." },
      { s: "http", k: "port", type: "num", step: 1, min: 1024, max: 65535, label: "Dashboard port", warn: "Changes the dashboard's address. On Windows, quit Lorakeet from the tray and start it again afterwards (the shortcut follows the new port).", help: "5190 by default." },
    ] },
  { id: "mapsettings", title: "Map", src: "toml", about: "",
    fields: [
      { s: "map", k: "center", type: "latlon", label: "Start the map at", help: "Before any radio has shared a position. Empty = a view of the world." },
      { s: "map", k: "zoom", type: "num", step: 1, min: 1, max: 18, label: "…zoomed to", help: "1 = the world, 10 = a city, 15 = streets." },
      { s: "map", k: "tiles", type: "select", label: "Map tiles", options: [["osm", "OpenStreetMap and OpenTopoMap"], ["esri", "Esri, with satellite imagery (Esri's terms apply)"]] },
    ] },
  { id: "base", title: "Base station", src: "toml", about: "A radio to feature on the map and watch by default, e.g. your rooftop node.",
    fields: [
      { s: "base", k: "id", type: "text", max: 9, label: "Radio id", help: "Like !1234abcd. Empty = none." },
      { s: "base", k: "name", type: "text", max: 40, label: "Name to show", help: "Until the radio announces its own." },
    ] },
  { id: "storage", title: "Backups and storage", src: "toml", about: "",
    fields: [
      { k: "_datadir", type: "info", label: "Data folder" },
      { s: "backup", k: "destinations", type: "list", label: "Back up every night to", help: "Folders, one per line (a cloud-synced folder is a good choice). Empty = no backups. Each backup is a complete, compressed copy, checked before it's kept." },
      { s: "backup", k: "time", type: "text", max: 5, label: "…at", help: "HH:MM, local time." },
      { s: "storage", k: "db_warn_gb", type: "num", step: 0.5, min: 0.1, unit: "GB", label: "Warn when the database reaches", help: "Nothing is ever deleted: you decide what to do." },
      { s: "storage", k: "debug_log_days", type: "num", step: 1, min: 1, unit: "days", label: "Keep the radio's debug log for" },
      { s: "logging", k: "store_recipients", type: "bool", label: "Record who other people's addressed packets were for", help: "Your radio hears them on the shared airwaves anyway (contents stay encrypted unless you have the key)." },
    ] },
  { id: "remote", title: "Remote commands", src: "toml", about: "Ask this station how it's doing over the mesh, when it has no internet: a direct message \"lk status\" or \"lk gps\" from one of your radios gets a reply.",
    fields: [
      { s: "remote", k: "enabled", type: "bool", label: "Answer commands", warn: "Transmits: the replies go out over the mesh." },
      { s: "remote", k: "allow", type: "list", label: "From these radios", help: "Radio ids, one per line (e.g. !1234abcd). Only private direct messages, with the key this station first recorded for them." },
    ] },
  { id: "weather", title: "Weather", src: "toml", about: "Hourly weather where each of your stations is (temperature, humidity, rain, wind), from Open-Meteo, so Lorakeet can tell a link that follows the weather from one that really changed. On a hub it covers every station.",
    fields: [
      { s: "weather", k: "enabled", type: "bool", label: "Fetch weather for my stations", warn: "Sends the centre of the ~10 km grid square each station is in to open-meteo.com, once an hour. Nothing about the mesh or an exact position." },
    ] },
  { id: "mcp", title: "LLM access", src: "toml", about: "What an LLM app connected through mcp_server.py may do besides reading (Claude Desktop, Claude Code…). Reading is always allowed; it never touches radio settings, keys or pairing.",
    fields: [
      { s: "mcp", k: "allow_changes", type: "bool", label: "Change Lorakeet", help: "Watch radios, pause logging, alert settings, mark alerts read, back up, restart." },
      { s: "mcp", k: "allow_transmit", type: "bool", label: "Transmit", warn: "Messages and traceroutes go out on the mesh under your radio's name." },
      { k: "_mcphow", type: "info", label: "To connect an app" },
    ] },
];

let ALERTS = null, TRACE = null, PREFS = null, PREFS_ERR = null;
const P = {};  // pending: "alerts.<key>" or "toml.<section>.<key>" -> value
const keyOf = (f) => (f.s ? `toml.${f.s}.${f.k}` : `alerts.${f.k}`);
const current = (f) => (f.s ? PREFS?.saved?.[f.s]?.[f.k] : ALERTS?.[f.k]);
const pendingRestart = (f) => f.s && PREFS && !same(PREFS.saved[f.s][f.k], PREFS.running[f.s][f.k]);

async function load() {
  const [a, p] = await Promise.all([
    fetch("/api/settings").then((r) => r.json()).catch(() => null),
    fetch("/api/preferences").then(async (r) => ({ status: r.status, ...(await r.json()) })).catch(() => null),
  ]);
  ALERTS = a?.settings || null; TRACE = a?.autoTraceroute || {};
  PREFS = p && !p.error ? p : null;
  PREFS_ERR = p?.error || (p ? null : "Couldn't reach Lorakeet.");
  render();
}

function control(f) {
  const id = `f-${keyOf(f).replace(/\./g, "-")}`, k = keyOf(f), v = k in P ? P[k] : current(f), a = `id="${id}" data-k="${esc(k)}"`;
  if (f.type === "bool") return `<input type="checkbox" ${a} ${v ? "checked" : ""}>`;
  if (f.type === "num") return `<input type="number" ${a} step="${f.step}" ${f.min != null ? `min="${f.min}"` : ""} ${f.max != null ? `max="${f.max}"` : ""} value="${esc(v)}">${f.unit ? ` <span class="muted">${esc(f.unit)}</span>` : ""}`;
  if (f.type === "text") return `<input type="text" ${a} maxlength="${f.max || 64}" value="${esc(v)}" spellcheck="false" ${f.list ? 'list="portList"' : ""}>`;
  if (f.type === "select") return `<select ${a}>${f.options.map(([val, lab]) => `<option value="${esc(val)}" ${val === v ? "selected" : ""}>${esc(lab)}</option>`).join("")}</select>`;
  if (f.type === "list") return `<textarea ${a} rows="${Math.max(2, (v || []).length + 1)}" spellcheck="false">${esc((v || []).join("\n"))}</textarea>`;
  if (f.type === "latlon") {
    const [la, lo] = v && v.length ? v : ["", ""];
    return `<input type="text" ${a} data-part="lat" inputmode="decimal" placeholder="latitude" size="11" value="${esc(la)}"> <input type="text" data-k="${esc(k)}" data-part="lon" inputmode="decimal" placeholder="longitude" size="11" value="${esc(lo)}" aria-label="${esc(f.label)} longitude">`;
  }
  if (f.type === "password") return PREFS?.passwordSet ? `<span>Set. Change it with <code>python server.py --set-password</code>; <code>--clear-password</code> removes it and logs every device out.</span>`
    : `<span class="muted">Not set. Set one with <code>python server.py --set-password</code> (in Lorakeet's folder, venv\\Scripts\\python on Windows): a device that logs in gets full access.</span>`;
  if (f.type === "watched") return (ALERTS?.watched || []).length ? `<span>${ALERTS.watched.map((w) => `<a href="/analytics.html#node=${encodeURIComponent(w)}">${esc(w)}</a> <button class="linkbtn" data-unwatch="${esc(w)}" title="Stop watching">×</button>`).join(" · ")}</span>` : '<span class="muted">none</span>';
  if (f.k === "_datadir") return `<code>${esc(PREFS?.dataDir || "")}</code> <span class="muted">(set in lorakeet.toml; moving it means moving the data too)</span>`;
  if (f.k === "_lasttrace") return TRACE?.ts ? `<span>${esc(new Date(TRACE.ts * 1000).toLocaleString())}${TRACE.target ? `: <a href="/analytics.html#node=${encodeURIComponent(TRACE.target)}">${esc(TRACE.target)}</a>` : ""} <span class="muted">${esc(TRACE.reason || "")}</span></span>` : '<span class="muted">none since Lorakeet started</span>';
  if (f.k === "_mcphow") return `<span>Run <code>python mcp_server.py --print-config</code> in Lorakeet's folder: it prints what to add to your app. See docs/API.md.</span>`;
  return "";
}

function row(f) {
  const k = keyOf(f), changed = k in P, restart = pendingRestart(f);
  return `<div class="advf${changed ? " changed" : ""}" data-row="${esc(k)}">
    <label class="advl" for="f-${esc(k.replace(/\./g, "-"))}">${esc(f.label)}${restart ? ' <span class="restarttag" title="Saved; applies after Lorakeet restarts">after restart</span>' : ""}</label>
    <div class="advc">${control(f)}</div>
    ${f.help ? `<p class="why">${esc(f.help)}</p>` : ""}${f.warn ? `<p class="warnnote">${esc(f.warn)}</p>` : ""}</div>`;
}

function render() {
  $("err").textContent = PREFS_ERR && PREFS_ERR !== "these settings are only available on the Lorakeet computer" && !/demo/i.test(PREFS_ERR) ? PREFS_ERR : "";
  const shown = SECTIONS.filter((s) => (s.src === "alerts" ? ALERTS : PREFS));
  $("setnav").innerHTML = shown.map((s) => `<a href="#${s.id}">${esc(s.title)}</a>`).join("") + (PREFS ? '<a href="#lorakeet">Lorakeet</a><a href="#help">Help</a>' : "");
  let html = shown.map((s) => `<section class="card" id="${s.id}"><h2>${esc(s.title)}${s.src === "alerts" ? ' <span class="muted applies">applies at once</span>' : ' <span class="muted applies">applies after a restart</span>'}</h2>
    ${s.about ? `<p class="note">${esc(s.about)}</p>` : ""}${s.fields.map(row).join("")}</section>`).join("");
  if (!PREFS) html += `<section class="card"><p class="note">${PREFS_ERR && /demo/i.test(PREFS_ERR) ? "Lorakeet's own settings aren't available in the demo." : "Lorakeet's own settings (access, backups, the radio connection…) are only available on the Lorakeet computer."}</p></section>`;
  else html += `<section class="card" id="lorakeet"><h2>Lorakeet</h2><p class="note">Version ${esc(PREFS.version)}. Settings file: <code>${esc(PREFS.path)}</code> (every option is explained in lorakeet.example.toml).${PREFS.syncMode !== "off" ? ` This computer is a ${PREFS.syncMode === "hub" ? "hub" : "station sending to a hub"}: see <a href="/stations.html">Stations</a>.` : ""}</p>
    <p class="actions"><button class="btn" id="restartBtn">Restart Lorakeet</button> <span class="muted">Reconnects the radio and reads the settings again; logging pauses for a few seconds.</span></p></section>
    <section class="card" id="help"><h2>Help and diagnostics</h2>
    <p class="note">Something not working? Save a diagnostics file and attach it to a bug report: versions, your radio's model and firmware, recent errors and your settings, with names, radio ids, addresses, paths, locations and keys taken out. Nothing is sent anywhere. Please read it before you attach it.</p>
    <p class="actions"><button class="btn" id="diagPreview">See what's in it</button> <a class="btn" id="diagSave" href="/api/diagnostics.zip" download>Save diagnostics</a> <a href="https://github.com/darthnut/lorakeet/issues/new?template=bug_report.yml" target="_blank" rel="noopener">Report a bug ↗</a></p>
    <div id="diagFiles"></div>
    <p class="muted">Lorakeet won't start? In its folder, run <code>python diagnostics.py</code> (<code>venv\\Scripts\\python</code> on Windows, <code>venv/bin/python</code> elsewhere): it saves the same file there.</p></section>`;
  html += `<datalist id="portList">${(PREFS?.ports || []).map((p) => `<option value="${esc(p)}">`).join("")}</datalist>`;
  $("sections").innerHTML = html;
  const pend = SECTIONS.flatMap((s) => s.fields.filter(pendingRestart).map((f) => `${s.title}: ${f.label.replace(/^…/, "")}`));
  $("restartBanner").hidden = !pend.length;
  $("restartBanner").innerHTML = pend.length ? `Saved settings waiting for a restart: ${pend.map(esc).join("; ")}. ${PREFS.supervised ? '<button class="btn" id="restartBtn2">Restart Lorakeet</button>' : "Stop Lorakeet and start it again to apply them."}` : "";
  pending();
  if (!render.done && location.hash) document.getElementById(location.hash.slice(1))?.scrollIntoView();  // the bell's link: #alerts
  render.done = true;
}

function fieldFor(k) { return SECTIONS.flatMap((s) => s.fields).find((f) => keyOf(f) === k); }
function readValue(f, el) {
  if (f.type === "bool") return el.checked;
  if (f.type === "num") return el.value.trim() === "" ? undefined : Number(el.value);
  if (f.type === "list") return el.value.split(/\n/).map((x) => x.trim()).filter(Boolean);
  if (f.type === "latlon") {
    const row = el.closest(".advc"), la = row.querySelector('[data-part="lat"]').value.trim(), lo = row.querySelector('[data-part="lon"]').value.trim();
    if (!la && !lo) return [];
    return [la ? Number(la) : NaN, lo ? Number(lo) : NaN];
  }
  return el.value;
}
function onEdit(e) {
  const el = e.target.closest("[data-k]"); if (!el) return;
  const k = el.dataset.k, f = fieldFor(k); if (!f) return;
  const v = readValue(f, el);
  if (v === undefined || same(v, current(f))) delete P[k]; else P[k] = v;
  document.querySelector(`[data-row="${CSS.escape(k)}"]`)?.classList.toggle("changed", k in P);
  pending();
}
$("sections").addEventListener("input", onEdit);
$("sections").addEventListener("change", onEdit);
$("sections").addEventListener("click", async (e) => {
  const u = e.target.closest("[data-unwatch]");
  if (u) { await post("/api/watch", { id: u.dataset.unwatch, watched: false }); load(); return; }
  if (e.target.id === "restartBtn") restart(e.target);
  if (e.target.id === "diagPreview") previewDiagnostics(e.target);
});

async function previewDiagnostics(b) {
  b.disabled = true; b.textContent = "Gathering…";
  const r = await fetch("/api/diagnostics").then((x) => x.json()).catch(() => ({ error: "Couldn't reach Lorakeet." }));
  b.disabled = false; b.textContent = "See what's in it";
  if (r.error) { $("diagFiles").innerHTML = `<p class="err">${esc(r.error)}</p>`; return; }
  $("diagFiles").innerHTML = Object.entries(r.files).map(([name, text], i) =>
    `<details class="diag"${i === 1 ? " open" : ""}><summary>${esc(name)} <span class="muted">${(text.length / 1024).toFixed(1)} KB</span></summary><pre>${esc(text)}</pre></details>`).join("");
}
$("restartBanner").addEventListener("click", (e) => { if (e.target.id === "restartBtn2") restart(e.target); });

function pending() {
  const n = Object.keys(P).length;
  $("saveBar").hidden = !n && !$("saveNote").textContent;
  $("saveCount").textContent = n ? `${n} change${n > 1 ? "s" : ""} not saved yet` : "";
  $("saveBtn").disabled = !n; $("discardBtn").disabled = !n;
}
$("discardBtn").addEventListener("click", () => { for (const k of Object.keys(P)) delete P[k]; $("saveNote").textContent = ""; render(); });

$("saveBtn").addEventListener("click", async () => {
  const alerts = {}, toml = {};
  for (const [k, v] of Object.entries(P)) {
    const parts = k.split(".");
    if (parts[0] === "alerts") alerts[parts[1]] = v;
    else (toml[parts[1]] ||= {})[parts[2]] = v;
  }
  const badPos = SECTIONS.flatMap((s) => s.fields).find((f) => f.type === "latlon" && keyOf(f) in P
    && (P[keyOf(f)].length && (P[keyOf(f)].some((x) => !Number.isFinite(x)) || Math.abs(P[keyOf(f)][0]) > 90 || Math.abs(P[keyOf(f)][1]) > 180)));
  if (badPos) { $("err").textContent = $("saveNote").textContent = `${badPos.label}: enter both a latitude (-90 to 90) and a longitude (-180 to 180), or leave both empty.`; return; }
  $("saveBtn").disabled = true; $("err").textContent = "";
  if (Object.keys(toml).length) {
    const r = await post("/api/preferences", { changes: toml });
    if (r.error) { $("err").textContent = $("saveNote").textContent = r.error; $("saveBtn").disabled = false; return; }
  }
  if (Object.keys(alerts).length) {
    const r = await post("/api/settings", alerts);
    if (r.error) { $("err").textContent = $("saveNote").textContent = r.error; $("saveBtn").disabled = false; return; }
  }
  for (const k of Object.keys(P)) delete P[k];
  $("saveNote").textContent = Object.keys(toml).length ? "Saved. Settings marked \"after restart\" apply when Lorakeet restarts." : "Saved.";
  await load();
});

async function restart(b) {
  b.disabled = true; b.textContent = "Restarting…";
  const r = await post("/api/restart", {});
  if (r.error) { b.disabled = false; b.textContent = "Restart Lorakeet"; $("err").textContent = r.error; return; }
  await new Promise((res) => setTimeout(res, 3000));
  for (let i = 0; i < 60; i++) {
    try { if ((await fetch("/api/version")).ok) { location.reload(); return; } } catch { /* not up yet */ }
    await new Promise((res) => setTimeout(res, 1500));
  }
  b.textContent = "Still starting: reload in a minute";
}
window.addEventListener("beforeunload", (e) => { if (Object.keys(P).length) e.preventDefault(); });
load();
