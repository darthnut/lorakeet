"use strict";
/* The Radio page (radio_setup.py, /api/radio*): connect, check and change settings, channels, confirm logging.
   This computer only. Nothing is written to the radio except by a button, and the server backs it up first. */

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const ago = (t) => { if (!t) return "never"; const s = Date.now() / 1000 - t; return s < 90 ? `${Math.round(s)} s ago` : s < 5400 ? `${Math.round(s / 60)} min ago` : s < 129600 ? `${Math.round(s / 3600)} h ago` : `${Math.round(s / 86400)} days ago`; };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const post = (url, body) => fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
  .then(async (r) => ({ ok: r.ok, ...(await r.json().catch(() => ({ error: `error ${r.status}` }))) }))
  .catch(() => ({ error: "Couldn't reach Lorakeet: is it still running?" }));
const OPENED = Date.now() / 1000;
const BADGE = { ok: ["ok", "✓"], suggest: ["suggest", "Suggested"], needed: ["needed", "Needed"], info: ["info", "Info"] };
let R = null, busy = false, pollT = null;
document.querySelector(".pagenav [aria-current]")?.scrollIntoView({ block: "nearest", inline: "nearest" });  // narrow screens

if (new URLSearchParams(location.search).has("welcome")) {
  $("lead").textContent = "Lorakeet is set up. Now your radio: check its settings and confirm it's logging. Changes are only made when you press a button, and the radio's settings are backed up first.";
}

async function load() {
  try { R = await fetch("/api/radio").then((r) => r.json()); } catch { R = null; }
  if (R?.demo) { $("lead").textContent = R.error; $("s1").hidden = true; return; }
  if (!R || R.error) { $("err").textContent = R?.error || "Couldn't reach Lorakeet."; schedule(); return; }
  $("err").textContent = "";
  renderConnect();
  const on = R.connected;
  for (const id of ["s2", "s3", "s4"]) $(id).hidden = !on;
  if (on && !busy) { renderChecklist(); renderChannels(); }
  renderBackups();
  renderStation();
  schedule();
}
function schedule() {
  clearTimeout(pollT);
  if (!R?.connected) { pollT = setTimeout(load, 3000); return; }
  pollT = setTimeout(async () => {  // connected: a light check; a full reload would reset the Settings choices
    if (busy) { schedule(); return; }  // applying: the radio restarts on purpose, and apply() reloads afterwards
    let d = null;
    try { d = await fetch("/api/radio").then((r) => r.json()); } catch { /* restarting */ }
    if (d && !d.connected) load(); else schedule();
  }, 15000);
}

function renderConnect() {
  const r = R.radio;
  if (R.connected) {
    $("connect").innerHTML = `<p class="live ok">Connected: <b>${esc(r.longName || r.id)}</b> <span class="muted">(${esc(r.id)})</span>, ${esc((r.hw || "radio").replace(/_/g, " "))}, firmware ${esc(r.firmware || "?")}, on ${esc(R.port)}.</p>
      ${R.otherRadios.length ? `<p class="note">Also plugged in: ${R.otherRadios.map((p) => esc(p.device)).join(", ")}. Lorakeet logs from one radio; set <code>[radio] port</code> in lorakeet.toml to choose which.</p>` : ""}`;
    return;
  }
  const lines = [`<p class="live wait">No radio connected yet${R.port ? ` (trying ${esc(R.port)})` : ""}. This page updates by itself.</p>`];
  if (R.hint) lines.push(`<p class="hint">${esc(R.hint)}</p>`);
  else if (R.noPortHint) lines.push(`<p class="hint">${esc(R.noPortHint)}</p>`);
  if (R.error && !R.hint) lines.push(`<p class="note">Last error: <code>${esc(R.error)}</code></p>`);
  lines.push(`<p class="note">New radio? Put Meshtastic firmware on it first with the <a href="https://flasher.meshtastic.org" target="_blank" rel="noopener">Meshtastic web flasher</a> (close that tab afterwards: it holds the port).</p>`);
  $("connect").innerHTML = lines.join("");
}

// ---- step 2: settings
const label = (it, v) => (it.options.find((o) => o.value === v) || {}).label ?? (v === true ? "On" : v === false ? "Off" : String(v));
function renderChecklist() {
  $("longName").value = R.radio.longName || ""; $("shortName").value = R.radio.shortName || "";
  $("checklist").innerHTML = R.checklist.map((it, i) => {
    const [cls, txt] = BADGE[it.status] || BADGE.info;
    const sel = it.options.length ? `<select data-i="${i}" aria-label="${esc(it.title)}">${it.options.map((o) => `<option value="${i}:${esc(JSON.stringify(o.value))}" ${o.value === it.current ? "selected" : ""}>${esc(o.label)}${o.value === it.current ? " (now)" : ""}${o.value === it.recommended ? " · recommended" : ""}${o.note ? ` · ${esc(o.note)}` : ""}</option>`).join("")}</select>` : "";
    const rec = it.recommended != null && it.recommended !== it.current ? ` <button class="linkbtn" data-rec="${i}">Use the recommendation</button>` : "";
    return `<div class="ci ${cls}"><div class="ch"><span class="badge2 ${cls}">${txt}</span><b>${esc(it.title)}</b><span class="cur">${esc(it.options.length ? label(it, it.current) : it.current)}</span></div>
      <p class="why">${esc(it.why)}</p>${sel || rec ? `<div class="cc">${sel}${rec}</div>` : ""}</div>`;
  }).join("");
  pending();
}
function changes() {
  const out = {};
  for (const s of $("checklist").querySelectorAll("select")) {
    const it = R.checklist[s.dataset.i], v = JSON.parse(s.value.slice(s.value.indexOf(":") + 1));
    if (v !== it.current) out[it.id] = v;
  }
  return out;
}
function namesChanged() { return $("longName").value.trim() !== (R.radio.longName || "") || $("shortName").value.trim() !== (R.radio.shortName || ""); }
function pending() {
  const n = Object.keys(changes()).length + (namesChanged() ? 1 : 0);
  $("apply").disabled = !n || busy;
  $("applyNote").textContent = n ? `${n} change${n > 1 ? "s" : ""} to save` : "";
}
$("checklist").addEventListener("change", pending);
$("longName").addEventListener("input", pending); $("shortName").addEventListener("input", pending);
$("checklist").addEventListener("click", (e) => {
  const b = e.target.closest("[data-rec]"); if (!b) return;
  const it = R.checklist[b.dataset.rec], s = $("checklist").querySelector(`select[data-i="${b.dataset.rec}"]`);
  s.value = `${b.dataset.rec}:${JSON.stringify(it.recommended)}`; pending();
});

async function waitForReconnect(after, note) {
  const t0 = Date.now();
  for (;;) {
    await sleep(2000);
    try {
      const d = await fetch("/api/radio").then((r) => r.json());
      if (d.connected && d.connectedAt > after) return d;
      note(d.connected ? "Waiting for the radio to restart…" : "The radio is restarting…");
    } catch { /* the server is busy */ }
    if (Date.now() - t0 > 120000) return null;
  }
}

$("apply").addEventListener("click", async () => {
  const ch = changes(), names = namesChanged() ? { long: $("longName").value.trim(), short: $("shortName").value.trim() } : null;
  busy = true; pending(); $("err").textContent = "";
  const t0 = Date.now() / 1000, note = (t) => { $("applyNote").textContent = t; };
  note("Backing up the radio's settings and writing…");
  const r = await post("/api/radio/apply", { changes: ch, names });
  if (r.error) { busy = false; $("err").textContent = r.error; pending(); return; }
  if (r.nothing) { busy = false; note("Nothing to change."); return; }
  note(`Saved (backup ${r.backup}). The radio is restarting to apply it…`);
  const d = await waitForReconnect(t0, note);
  busy = false;
  if (!d) { note("The radio hasn't come back yet. Check it's still plugged in; this page updates when it does."); load(); return; }
  R = d;
  const bad = Object.entries(r.expected).filter(([k, v]) => (d.checklist.find((i) => i.id === k) || {}).current !== v);
  if (r.names && (d.radio.longName !== r.names[0] || d.radio.shortName !== r.names[1])) bad.push(["name", r.names.join(" / ")]);
  renderConnect(); renderChecklist(); renderChannels(); renderBackups();
  const n = Object.keys(r.expected).length + (r.names ? 1 : 0);
  note(bad.length ? `Read back after the restart, ${bad.length} of ${n} didn't take: ${bad.map(([k]) => k).join(", ")}. Try again, or restore the backup.`
    : `Done: read back from the radio after its restart, all ${n} change${n > 1 ? "s" : ""} took.`);
});

// ---- step 3: channels
const KEY = { public: ["Public default key", "anyone with Meshtastic can read it"], private: ["Private key", "only radios with this channel's key can read it"],
  none: ["No encryption", "readable by anyone"], primary: ["Primary's key", "uses the primary channel's key"] };
function renderChannels() {
  $("chanKey").hidden = !R.channels.length;
  $("channels").innerHTML = `<table class="chans"><thead><tr><th>#</th><th>Channel</th><th>Key</th><th>Positions</th><th></th></tr></thead><tbody>${R.channels.map((c) => {
    const [k, kn] = KEY[c.key] || [c.key, ""];
    const pos = c.precision === 32 ? "exact" : c.precision ? `rounded (${c.precision} bits)` : "not shared";
    const copy = c.shareable && R.otherRadios.length ? `<select data-copy="${c.index}"><option value="">Copy to…</option>${R.otherRadios.map((p) => `<option>${esc(p.device)}</option>`).join("")}</select>` : "";
    return `<tr><td>${c.index}</td><td><b>${esc(c.name)}</b>${c.role === "PRIMARY" ? ' <span class="muted">primary</span>' : ""}</td><td title="${esc(kn)}">${esc(k)}${c.bits ? ` <span class="muted">${c.bits}-bit</span>` : ""}</td><td>${esc(pos)}</td>
      <td>${c.shareable ? `<button class="linkbtn" data-share="${c.index}">Share</button> ` : ""}${copy}</td></tr>`;
  }).join("")}</tbody></table>`;
}
$("channels").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-share]"); if (!b) return;
  const d = await fetch(`/api/radio/share?index=${b.dataset.share}`).then((r) => r.json());
  if (d.error) { $("err").textContent = d.error; return; }
  $("share").hidden = false;
  $("share").innerHTML = `<div class="sharebox"><div class="qr">${d.svg}</div><div>
    <h3>Share “${esc(d.name)}”</h3>
    <p class="note">In the Meshtastic app: Channels → the QR scanner (or open the link on the phone), then <b>Add</b>, so the phone's radio keeps its other channels. <b>Anyone with this code or link can read the channel</b>: share it like a password.</p>
    <p><input readonly value="${esc(d.url)}" size="34" id="shareUrl"> <button class="btn" id="shareCopy">Copy link</button> <button class="btn" id="shareHide">Hide</button></p></div></div>`;
  $("shareCopy").onclick = async () => { try { await navigator.clipboard.writeText(d.url); $("shareCopy").textContent = "Copied"; } catch { $("shareUrl").select(); } };
  $("shareHide").onclick = () => { $("share").hidden = true; $("share").innerHTML = ""; };
});
$("channels").addEventListener("change", async (e) => {
  const s = e.target.closest("[data-copy]"); if (!s || !s.value) return;
  const port = s.value; s.disabled = true; $("err").textContent = "";
  const r = await post("/api/radio/channel", { action: "copy-to", index: Number(s.dataset.copy), port });
  s.disabled = false; s.value = "";
  if (r.error) { $("err").textContent = r.error; return; }
  const c = r.channels[0];
  alert(`${c.name}: ${c.result === "added" ? `added to ${r.radio} on ${port} (slot ${c.index})` : `already on ${r.radio}`}. Its settings were backed up first (${r.backup}).`);
});
async function channelChange(body, btn) {
  btn.disabled = true; $("err").textContent = "";
  const t0 = Date.now() / 1000;
  const r = await post("/api/radio/channel", body);
  if (r.error) { btn.disabled = false; $("err").textContent = r.error; return null; }
  btn.textContent = "Saved; reconnecting…";
  const d = await waitForReconnect(t0, () => {});
  btn.disabled = false;
  if (d) { R = d; renderChannels(); renderBackups(); }
  return r;
}
$("chanCreate").addEventListener("click", async () => {
  const b = $("chanCreate");
  const r = await channelChange({ action: "create", name: $("chanName").value.trim(), exactPositions: $("chanExact").checked }, b);
  b.textContent = "Make the channel";
  if (r) { $("chanName").value = ""; $("newChan").open = false; const idx = r.channels[0].index; $("channels").querySelector(`[data-share="${idx}"]`)?.click(); }
});
$("chanAdd").addEventListener("click", async () => {
  const b = $("chanAdd");
  const r = await channelChange({ action: "add-link", url: $("chanLink").value.trim() }, b);
  b.textContent = "Add";
  if (r) { $("chanLink").value = ""; $("err").textContent = ""; alert(r.channels.map((c) => `${c.name}: ${c.result}`).join("\n")); }
});

// ---- step 4: logging
async function logging() {
  let d;
  try { d = await fetch(`/api/radio/logging?since=${OPENED}`).then((r) => r.json()); } catch { setTimeout(logging, 5000); return; }
  if (d.error) { setTimeout(logging, 5000); return; }
  const row = (ok, title, text) => `<li class="${ok === true ? "ok" : ok === false ? "wait" : "na"}"><b>${esc(title)}</b> ${text}</li>`;
  const rows = [row(d.connected, "Radio connected", d.connected ? "" : "· waiting for it")];
  rows.push(row(d.packets > 0, "Packets arriving", d.packets > 0 ? `· ${d.packets} packet${d.packets === 1 ? "" : "s"} since you opened this page` :
    `· none yet since you opened this page (last ever: ${ago(d.lastPacket)}). A quiet mesh can take a few minutes; check the antenna is attached and the region is set.`));
  let detail;
  if (!d.usb) detail = row(null, "Per-reception detail", "· not available over a network connection (radios send their debug log only over USB)");
  else if (!d.debugLog) detail = row(false, "Per-reception detail", "· off: turn on <i>Per-reception detail</i> in Settings above");
  else detail = row(d.receptions > 0, "Per-reception detail", d.receptions > 0 ? `· ${d.receptions} over-the-air reception${d.receptions === 1 ? "" : "s"} recorded` : "· on; waiting for the first reception");
  rows.push(detail);
  const done = d.connected && d.packets > 0 && (!d.usb || !d.debugLog || d.receptions > 0);
  $("logging").innerHTML = `<ul class="checks">${rows.join("")}</ul>${done ? '<p class="live ok"><b>You\'re logging.</b> Everything your radio hears is being recorded.</p>' : ""}`;
  $("doneLinks").hidden = !done;
  setTimeout(logging, done ? 30000 : 5000);
}

function renderBackups() {
  const list = R?.backups || [];
  $("bk").hidden = !R?.connected;
  $("bkDir").textContent = R?.backupDir || "";
  $("bkList").innerHTML = list.length ? list.map((b) => `<li><code>${esc(b.name)}</code> <span class="muted">${esc(ago(b.ts))}</span></li>`).join("") : '<li class="muted">None yet: one is made before the first change.</li>';
}

load();
logging();

// ---- this station: name and antenna position, saved to lorakeet.toml (used after a restart)
let stDirty = false, stGeoNote = "", stRough = false;
const fmtKm = (m) => (m > 5000 ? `${Math.round(m / 1000)} km` : `${Math.round(m)} m`);
function renderStation() {
  const st = R?.station; if (!st) return;
  $("stationCard").hidden = false;
  $("cfgFile").textContent = st.configPath;
  $("stMobile").hidden = !st.mobile; $("stLocRow").hidden = st.mobile;
  if (!stDirty) {
    $("stName").value = st.name || "";
    $("stLat").value = st.location?.length ? st.location[0] : ""; $("stLon").value = st.location?.length ? st.location[1] : "";
    stGeoNote = "";
  }
  $("stRadioGps").hidden = !st.radioFix || st.mobile;
  $("stRestart").hidden = !st.pendingRestart;
  if (st.pendingRestart && !stDirty) $("stNote").textContent = st.supervised ? "Saved. Restart Lorakeet to use it." : "Saved. Stop Lorakeet and start it again to use it.";
  const c = st.check;
  $("locBanner").hidden = !c;
  if (c) $("locBanner").innerHTML = `<b>This station's location looks wrong:</b> it's ${c.distanceKm.toLocaleString()} km from the ${c.radios} radios it knows about, so links on the maps are drawn to the wrong place. <a href="#stationCard">Fix it under This station</a>.`;
  if (!stDirty) stCheck();
}
async function stCheck() {
  const w = $("stWarn"), lat = +$("stLat").value, lon = +$("stLon").value, parts = [];
  if ($("stLat").value.trim() && $("stLon").value.trim() && Number.isFinite(lat) && Number.isFinite(lon)) {
    parts.push(`<a href="https://www.openstreetmap.org/?mlat=${lat}&mlon=${lon}#map=14/${lat}/${lon}" target="_blank" rel="noopener">See it on a map</a>.`);
    try {
      const r = await fetch(`/api/radio/location-check?lat=${lat}&lon=${lon}`).then((x) => x.json());
      if (r.check) parts.unshift(`<b>That's ${r.check.distanceKm.toLocaleString()} km from the ${r.check.radios} radios this station knows about</b>, so it's very likely wrong.`);
    } catch { /* fine */ }
  }
  w.innerHTML = [stGeoNote && (stRough ? `<b>${esc(stGeoNote)}</b>` : esc(stGeoNote)), ...parts].filter(Boolean).join(" ");
  w.hidden = !w.innerHTML;
}
function stEdited() { stDirty = true; $("stNote").textContent = "Not saved yet."; }
for (const id of ["stName", "stLat", "stLon"]) $(id).addEventListener("input", stEdited);
for (const id of ["stLat", "stLon"]) $(id).addEventListener("change", () => { stGeoNote = ""; stRough = false; stCheck(); });
$("stClear").addEventListener("click", () => { $("stLat").value = ""; $("stLon").value = ""; stGeoNote = ""; stEdited(); stCheck(); });
$("stRadioGps").addEventListener("click", () => {
  const f = R.station.radioFix;
  $("stLat").value = (+f.lat).toFixed(6); $("stLon").value = (+f.lon).toFixed(6);
  stGeoNote = `From the radio's GPS (fix ${new Date(f.time * 1000).toLocaleString()}).`; stRough = false;
  stEdited(); stCheck();
});
$("stGeo").addEventListener("click", () => {
  if (!navigator.geolocation) { $("err").textContent = "This browser can't share a location; type it in instead."; return; }
  $("stGeo").textContent = "Locating…";
  navigator.geolocation.getCurrentPosition((p) => {
    $("stGeo").textContent = "Use this computer's location";
    $("stLat").value = p.coords.latitude.toFixed(5); $("stLon").value = p.coords.longitude.toFixed(5);
    const acc = p.coords.accuracy; stRough = acc > R.station.roughM;
    stGeoNote = stRough ? `This computer's location is only a rough guess (±${fmtKm(acc)}), probably from its internet connection. Check it on the map, or type the antenna's coordinates.`
      : `This computer's location (±${fmtKm(acc)}). If the antenna is elsewhere, correct it.`;
    stEdited(); stCheck();
  }, (e) => { $("stGeo").textContent = "Use this computer's location"; $("err").textContent = `Couldn't get a location: ${e.message}`; }, { enableHighAccuracy: true, timeout: 15000 });
});
$("stSave").addEventListener("click", async () => {
  const lat = $("stLat").value.trim(), lon = $("stLon").value.trim();
  if ((lat || lon) && !(lat && lon && Number.isFinite(+lat) && Number.isFinite(+lon))) { $("stNote").textContent = "Enter both latitude and longitude as numbers, or clear both."; return; }
  $("stSave").disabled = true; $("err").textContent = "";
  const r = await post("/api/radio/station", { name: $("stName").value.trim(), location: lat ? [+lat, +lon] : [] });
  $("stSave").disabled = false;
  if (r.error) { $("stNote").textContent = r.error; return; }
  stDirty = false; R.station = r.station; renderStation();
});
$("stRestart").addEventListener("click", async () => {
  $("stRestart").disabled = true; $("stNote").textContent = "Restarting…";
  const r = await post("/api/radio/restart", {});
  if (r.error) { $("stRestart").disabled = false; $("stNote").textContent = r.error; return; }
  await sleep(3000);
  for (let i = 0; i < 60; i++) {  // the runner starts it again within seconds
    try { if ((await fetch("/api/version")).ok) { location.reload(); return; } } catch { /* not up yet */ }
    await sleep(1500);
  }
  $("stNote").textContent = "It's taking a while; reload this page in a minute.";
});
