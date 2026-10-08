"use strict";
/* First-run setup: writes lorakeet.toml through POST /api/setup (this computer only). An install that already
   has a lorakeet.toml only gets a read-only summary: setup never overwrites hand-edited settings. */

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
let info = null;

async function load() {
  try { info = await fetch("/api/setup").then((r) => r.json()); } catch { info = null; }
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
  $("setupForm").hidden = false;
  setInterval(refreshLive, 3000);
}

function showLive() {
  const st = info.status || {}, el = $("live");
  el.className = `live ${st.connected ? "ok" : "wait"}`;
  el.textContent = st.connected
    ? `Connected now: radio ${st.localId} on ${st.port}. "Find it automatically" will keep using it.`
    : info.ports.some((p) => p.radio) ? "A radio is plugged in but not connected yet; it can take a few seconds." : "No radio connected yet. Plug one in over USB, or choose a network radio below.";
}
async function refreshLive() {
  try { const d = await fetch("/api/setup").then((r) => r.json()); if (!d.error) { info.status = d.status; info.ports = d.ports; showLive(); } } catch { /* restarting */ }
}

function syncRadio() {
  const mode = document.querySelector('input[name="radioMode"]:checked').value;
  for (const el of document.querySelectorAll(".sub[data-for]")) el.hidden = el.dataset.for !== mode;
}
document.querySelectorAll('input[name="radioMode"]').forEach((r) => r.addEventListener("change", syncRadio));
syncRadio();

$("geo").addEventListener("click", () => {
  if (!navigator.geolocation) { $("err").textContent = "This browser can't share a location; type it in instead."; return; }
  $("geo").textContent = "Locating…";
  navigator.geolocation.getCurrentPosition((p) => {
    $("lat").value = p.coords.latitude.toFixed(5); $("lon").value = p.coords.longitude.toFixed(5);
    $("geo").textContent = `Located (±${Math.round(p.coords.accuracy)} m)`;
  }, (e) => { $("geo").textContent = "Use this computer's location"; $("err").textContent = `Couldn't get a location: ${e.message}`; }, { enableHighAccuracy: true, timeout: 15000 });
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
  };
  $("save").disabled = true;
  let r;
  try {
    r = await fetch("/api/setup", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }).then((x) => x.json());
  } catch (err) { r = { error: err.message }; }
  if (r.error) { $("err").textContent = r.error; $("save").disabled = false; return; }
  $("setupForm").hidden = true; $("done").hidden = false;
  if (r.restarting) {
    $("doneTitle").textContent = "Saved. Restarting…";
    $("doneText").innerHTML = `Settings written to <code>${esc(r.path)}</code>. Lorakeet is restarting to use them; the dashboard opens in a moment.`;
    const t0 = Date.now();
    await new Promise((res) => setTimeout(res, 2500));
    for (;;) {  // wait for the new server, then open the dashboard
      try { const d = await fetch("/api/setup").then((x) => x.json()); if (d.configured) { location.href = "/"; return; } } catch { /* not up yet */ }
      if (Date.now() - t0 > 60000) { $("doneText").innerHTML += "<br>It's taking a while; reload this page in a minute."; return; }
      await new Promise((res) => setTimeout(res, 1500));
    }
  } else {
    $("doneTitle").textContent = "Saved";
    $("doneText").innerHTML = `Settings written to <code>${esc(r.path)}</code>. <b>Restart Lorakeet</b> (stop <code>server.py</code> and start it again) to use them, then <a href="/">open the dashboard</a>.`;
  }
});

load();
