"use strict";
/* First-run setup: writes lorakeet.toml through POST /api/setup (this computer only). An install that already
   has a lorakeet.toml only gets a read-only summary: setup never overwrites hand-edited settings. */

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
let info = null;

async function load() {
  try { info = await fetch("/api/setup").then((r) => r.json()); } catch { info = null; }
  if (info?.demo) { $("lead").textContent = info.error; document.querySelector(".demo-card").hidden = true; return; }
  if (!info || info.error) { $("err").textContent = info?.error || "Couldn't reach Lorakeet."; $("setupForm").hidden = false; return; }
  $("cfgPath").textContent = info.path; $("cfgPath2").textContent = info.path;
  if (info.configured) {
    const s = info.summary;
    $("summary").innerHTML = [["Radio", s.radio], ["Data folder", s.dataDir], ["Local network", s.lan === "view" ? "read-only access" : "this computer only"],
      ["Record recipients", s.storeRecipients ? "yes" : "no"], ["Map tiles", s.tiles === "esri" ? "Esri" : "OpenStreetMap"],
      ["Station", (s.stationName || "unnamed") + (s.location ? ", location set" : "")]].map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("");
    $("title").textContent = "Lorakeet settings";
    $("lead").hidden = true;
    $("configured").hidden = false;
    return;
  }
  $("dataDir").value = info.defaults.dataDir;
  $("port").innerHTML = info.ports.length
    ? info.ports.map((p) => `<option value="${esc(p.device)}">${esc(p.device)}${p.description ? ` · ${esc(p.description)}` : ""}${p.radio ? " · looks like a radio" : ""}</option>`).join("")
    : '<option value="">no USB serial ports found</option>';
  showLive();
  showRadioGps();
  $("setupForm").hidden = false;
  setInterval(refreshLive, 3000);
}

function showLive() {
  const st = info.status || {}, el = $("live");
  el.className = `live ${st.connected ? "ok" : "wait"}`;
  const who = info.radioName && info.radioName !== st.localId ? `${info.radioName} (${st.localId})` : `radio ${st.localId}`;
  el.textContent = st.connected
    ? `Connected now: ${who} on ${st.port}. "Find it automatically" will keep using it.`
    : info.ports.some((p) => p.radio) ? "A radio is plugged in but not connected yet; it can take a few seconds." : "No radio connected yet. Plug one in over USB, or choose a network radio below.";
}
async function refreshLive() {
  try {
    const d = await fetch("/api/setup").then((r) => r.json());
    if (!d.error) { Object.assign(info, { status: d.status, ports: d.ports, radioName: d.radioName, radioFix: d.radioFix }); showLive(); showRadioGps(); }
  } catch { /* restarting */ }
}

function syncRadio() {
  const mode = document.querySelector('input[name="radioMode"]:checked').value;
  for (const el of document.querySelectorAll(".sub[data-for]")) el.hidden = el.dataset.for !== mode;
}
document.querySelectorAll('input[name="radioMode"]').forEach((r) => r.addEventListener("change", syncRadio));
syncRadio();

// Where's the antenna? Three ways in, each checked: typed coordinates, the radio's own GPS fix, or the browser's
// location (which on a computer with no Wi-Fi is a guess from the internet connection: can be a whole country off).
const ROUGH_M = 1000;
let geoNote = "";
function showRadioGps() { $("radioGps").hidden = !info?.radioFix; }
$("radioGps").addEventListener("click", () => {
  const f = info.radioFix; if (!f) return;
  $("lat").value = (+f.lat).toFixed(6); $("lon").value = (+f.lon).toFixed(6);
  geoNote = `From the radio's GPS (fix ${new Date(f.time * 1000).toLocaleString()}).`;
  checkLocation();
});
$("geo").addEventListener("click", () => {
  if (!navigator.geolocation) { $("err").textContent = "This browser can't share a location; type it in instead."; return; }
  $("geo").textContent = "Locating…";
  navigator.geolocation.getCurrentPosition((p) => {
    $("lat").value = p.coords.latitude.toFixed(5); $("lon").value = p.coords.longitude.toFixed(5);
    const acc = Math.round(p.coords.accuracy);
    $("geo").textContent = "Use this computer's location";
    geoNote = acc > ROUGH_M
      ? `This computer's location is only a rough guess (±${acc > 5000 ? Math.round(acc / 1000) + " km" : acc + " m"}), probably from its internet connection. Check it on a map, or type the antenna's coordinates instead.`
      : `This computer's location (±${acc} m). If the antenna is elsewhere, correct it.`;
    checkLocation(acc > ROUGH_M);
  }, (e) => { $("geo").textContent = "Use this computer's location"; $("err").textContent = `Couldn't get a location: ${e.message}`; }, { enableHighAccuracy: true, timeout: 15000 });
});
async function checkLocation(rough = false) {
  const lat = +$("lat").value, lon = +$("lon").value, w = $("locWarn");
  const parts = [];
  if ($("lat").value.trim() && $("lon").value.trim() && Number.isFinite(lat) && Number.isFinite(lon)) {
    parts.push(`${lat.toFixed(5)}, ${lon.toFixed(5)}: <a href="https://www.openstreetmap.org/?mlat=${lat}&mlon=${lon}#map=14/${lat}/${lon}" target="_blank" rel="noopener">see it on a map</a>.`);
    try {
      const r = await fetch(`/api/radio/location-check?lat=${lat}&lon=${lon}`).then((x) => x.json());
      if (r.check) parts.push(`<b>That's ${r.check.distanceKm.toLocaleString()} km from the ${r.check.radios} radios your radio knows about</b>, so it's very likely wrong.`);
    } catch { /* no check, fine */ }
  }
  w.innerHTML = [geoNote && (rough ? `<b>${esc(geoNote)}</b>` : esc(geoNote)), ...parts].filter(Boolean).join(" ");
  w.hidden = !w.innerHTML;
}
for (const id of ["lat", "lon"]) $(id).addEventListener("change", () => { geoNote = ""; checkLocation(); });

// a station that sends to a hub: a pairing code from the hub's Stations page, tested before it's saved
$("joinHub").addEventListener("change", () => { $("joinBox").hidden = !$("joinHub").checked; });
function showHubTest(r) {
  $("joinResult").innerHTML = r.ok ? `<span class="okmsg">Reached ${esc(r.hubName || "the hub")} at ${esc(r.url)}.</span>`
    : `<span class="errmsg">${esc(r.hint || r.error || "Couldn't reach the hub.")}</span>`;
  $("joinAnywayRow").hidden = !!r.ok;
}
$("joinTest").addEventListener("click", async () => {
  $("joinResult").textContent = "Testing…";
  const r = await fetch("/api/hub/test", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ code: $("joinCode").value }) }).then((x) => x.json()).catch((err) => ({ error: err.message }));
  showHubTest(r);
});

$("setupForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("err").textContent = "";
  const mode = document.querySelector('input[name="radioMode"]:checked').value;
  const lat = $("lat").value.trim(), lon = $("lon").value.trim();
  if ((lat || lon) && !(Number.isFinite(+lat) && Number.isFinite(+lon) && lat && lon)) { $("err").textContent = "Enter both latitude and longitude as numbers, or leave both empty."; return; }
  if (mode === "host" && !$("host").value.trim()) { $("err").textContent = "Enter the network radio's address."; return; }
  const body = {
    port: mode === "port" ? $("port").value : "", host: mode === "host" ? $("host").value.trim() : "",
    tcpPort: Number($("tcpPort").value) || 4403,
    stationName: $("stationName").value.trim(), location: lat && lon ? [+lat, +lon] : null,
    storeRecipients: $("storeRecipients").checked, lan: $("lanView").checked ? "view" : "off",
    autoTraceroute: $("autoTraceroute").checked,
    dataDir: $("dataDir").value.trim() === info.defaults.dataDir ? "" : $("dataDir").value.trim(),
    tiles: document.querySelector('input[name="tiles"]:checked').value,
    joinCode: $("joinHub").checked ? $("joinCode").value.trim() : "", joinAnyway: $("joinAnyway").checked,
  };
  if ($("joinHub").checked && !body.joinCode) { $("err").textContent = "Paste the hub's pairing code, or untick 'sends to a hub'."; return; }
  $("save").disabled = true;
  let r;
  try {
    r = await fetch("/api/setup", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }).then((x) => x.json());
  } catch (err) { r = { error: err.message }; }
  if (r.error) { $("err").textContent = r.error; if (r.hubTest) showHubTest(r.hubTest); $("save").disabled = false; return; }
  $("setupForm").hidden = true; $("done").hidden = false;
  if (r.restarting) {
    $("doneTitle").textContent = "Saved. Restarting…";
    $("doneText").innerHTML = `Settings written to <code>${esc(r.path)}</code>. Lorakeet is restarting to use them; the dashboard opens in a moment.`;
    const t0 = Date.now();
    await new Promise((res) => setTimeout(res, 2500));
    for (;;) {  // wait for the new server, then open the dashboard
      try { const d = await fetch("/api/setup").then((x) => x.json()); if (d.configured) { location.href = "/radio.html?welcome=1"; return; } } catch { /* not up yet */ }
      if (Date.now() - t0 > 60000) { $("doneText").innerHTML += "<br>It's taking a while; reload this page in a minute."; return; }
      await new Promise((res) => setTimeout(res, 1500));
    }
  } else {
    $("doneTitle").textContent = "Saved";
    $("doneText").innerHTML = `Settings written to <code>${esc(r.path)}</code>. <b>Restart Lorakeet</b> (stop <code>server.py</code> and start it again) to use them, then <a href="/radio.html?welcome=1">check your radio</a>.`;
  }
});

load();

// "Explore a demo": server.py starts `--demo` on a port of its own (a made-up mesh, demo.py) and says where
$("demoBtn").addEventListener("click", async () => {
  const btn = $("demoBtn"), msg = $("demoMsg");
  const tab = window.open("", "_blank");  // opened now, while the click still counts, so it isn't blocked
  btn.disabled = true;
  msg.textContent = "Starting the demo (the first time takes a few seconds)…";
  try {
    const r = await fetch("/api/demo", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    const d = await r.json();
    if (!d.url) throw new Error(d.error || "the demo didn't start");
    if (tab) tab.location = d.url; else location.href = d.url;
    msg.innerHTML = `Open: <a href="${esc(d.url)}" target="_blank" rel="noopener">${esc(d.url)}</a>`;
  } catch (e) {
    tab?.close();
    msg.textContent = e.message;
  } finally {
    btn.disabled = false;
  }
});
