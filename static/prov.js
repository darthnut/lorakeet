"use strict";
/* Provenance badges: where a displayed value came from. Shared by every page.
   reported - a field the protocol, firmware or node sent us
   observed - something this dashboard measured or recorded itself
   inferred - computed or estimated from other values
   unknown  - nothing recorded for it */
const PROV = {
  reported: ["R", "Reported: a value the protocol, firmware or node itself sent."],
  observed: ["O", "Observed: measured or recorded by this dashboard (times, counts, our radio's signal readings)."],
  inferred: ["I", "Inferred: computed or estimated from other values, not reported directly."],
  unknown: ["?", "Unknown: nothing was recorded for this."],
};
function prov(kind, note) {
  const p = PROV[kind] || PROV.unknown;
  const t = note ? `${p[1]} ${note}` : p[1];
  return `<span class="prov prov-${kind in PROV ? kind : "unknown"}" title="${t.replace(/"/g, "&quot;")}" aria-label="${p[1].split(":")[0]}">${p[0]}</span>`;
}
// Listening station: which station's data the analytics pages show (?station=). Empty = the radio this
// server logs from. Pages that show per-station data opt in with <body data-station-picker>; the picker
// appears in their top bar only when more than one station has logged anything.
window.LK_STATION = (() => { try { return localStorage.getItem("meshdash.station") || ""; } catch { return ""; } })();
const stationQS = () => (window.LK_STATION ? `&station=${encodeURIComponent(window.LK_STATION)}` : "");
(async () => {
  if (!("stationPicker" in document.body.dataset)) return;
  let d;
  try { d = await fetch("/api/stations").then((r) => r.json()); } catch { return; }
  const list = d.stations || [];
  const forget = () => { try { localStorage.removeItem("meshdash.station"); } catch { /* storage blocked */ } };
  if (window.LK_STATION && window.LK_STATION !== "*" && !list.some((s) => s.id === window.LK_STATION)) { forget(); location.reload(); return; }
  if (list.length < 2) return;
  const sel = document.createElement("select");
  sel.className = "stationpick";
  sel.setAttribute("aria-label", "Listening station");
  sel.title = "Which listening station's data to show";
  sel.innerHTML = list.map((s) => `<option value="${s.id}">${(s.name || s.id).replace(/[<&>"]/g, "")}${s.current ? " (this PC)" : ""}</option>`).join("") +
    `<option value="*">All stations (combined)</option>`;
  sel.value = window.LK_STATION || d.current || list[0].id;
  sel.addEventListener("change", () => {
    try {
      if (sel.value === d.current) localStorage.removeItem("meshdash.station");
      else localStorage.setItem("meshdash.station", sel.value);
    } catch { /* storage blocked: the choice lasts this page only */ }
    location.reload();
  });
  const bar = document.querySelector(".topbar");
  const anchor = bar.querySelector(".pagenav");
  anchor ? anchor.after(sel) : bar.append(sel);
})();

// Install settings the pages need (map fallback center, featured base station). Shared by every page.
window.LK_CFG = { map: { center: [], zoom: 10 }, base: {} };

// Map tiles for every map on every page. Default "osm": the OpenStreetMap standard tiles (dark mode = the same
// tiles through a CSS filter, .lk-dark-tiles in app.css) and OpenTopoMap terrain; both keyless and free with
// attribution for light use (tile.openstreetmap.org's usage policy applies). CARTO's styles now need an API
// key (they return an "API KEY REQUIRED" image), so they're not used. [map] tiles = "esri" uses Esri's tiles
// instead (their terms apply), the only option that includes satellite imagery.
// kind: "light" | "dark" | "topo" | "sat"; returns null for "sat" when there's no imagery provider.
const OSM_A = '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';
window.lkTilesProvider = () => (window.LK_CFG?.map?.tiles === "esri" ? "esri" : "osm");
window.lkTiles = (kind) => {
  if (lkTilesProvider() === "esri") {
    const e = (svc, a) => L.tileLayer(`https://server.arcgisonline.com/ArcGIS/rest/services/${svc}/MapServer/tile/{z}/{y}/{x}`, { maxZoom: 19, attribution: a });
    return {
      light: () => e("Canvas/World_Light_Gray_Base", "Tiles © Esri — Esri, HERE, Garmin, © OSM contributors"),
      dark: () => e("Canvas/World_Dark_Gray_Base", "Tiles © Esri — Esri, HERE, Garmin, © OSM contributors"),
      topo: () => e("World_Topo_Map", "Tiles © Esri — Esri, HERE, Garmin, USGS, © OSM contributors"),
      sat: () => e("World_Imagery", "Imagery © Esri, Maxar, Earthstar Geographics"),
    }[kind]();
  }
  const osm = (className) => L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png",
    { maxZoom: 19, attribution: OSM_A, className });
  return {
    light: () => osm(""),
    dark: () => osm("lk-dark-tiles"),
    topo: () => L.tileLayer("https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png",
      { maxZoom: 17, attribution: `Map data: ${OSM_A}, SRTM | Map style: © <a href="https://opentopomap.org">OpenTopoMap</a> (CC-BY-SA)` }),
    sat: () => null,
  }[kind]();
};
// the plain background map for the current colour scheme
window.lkBaseTiles = () => lkTiles(matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
window.LK_CFG_READY = fetch("/api/config").then((r) => r.json()).then((c) => (window.LK_CFG = c)).catch(() => window.LK_CFG);
// Where to look before any node has shared a position: configured center, else a world view.
function fallbackView() {
  const m = window.LK_CFG.map || {};
  return m.center && m.center.length === 2 ? [m.center, m.zoom || 10] : [[20, 0], 2];
}

// View-only mode for devices other than the dashboard PC: hide controls the server would refuse.
(async () => {
  try {
    const w = await fetch("/api/whoami").then((r) => r.json());
    if (w.readOnly) {
      document.documentElement.classList.add("readonly");
      const bar = document.querySelector(".topbar");
      bar?.insertAdjacentHTML("beforeend", '<span class="rotag" title="Viewing from another device: sending, traceroutes and settings only work on the dashboard PC.">view only</span>');
    }
  } catch { /* server restarting */ }
})();

function provLegend() {
  return `<span class="provlegend">${Object.keys(PROV).map((k) => `${prov(k)} ${PROV[k][1].split(":")[0].toLowerCase()}`).join("&ensp;")}</span>`;
}
