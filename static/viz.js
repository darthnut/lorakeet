"use strict";
/* Visualizations page: the topology graph or map fills the window, with the replay player's controls in
   floating panels (view switch top-left with the key + log beneath it, play bar and timeline along the bottom).
   "Hide UI" leaves only the visualization (and a small clock) for watching or screen recording.
   The time range is shared with Analytics (same localStorage key); node clicks open the node's
   Analytics page. */

const A = { range: "7d" };
try { A.range = localStorage.getItem("meshdash.range") || "7d"; } catch { /* storage blocked */ }
const nodeHref = (id) => `/analytics.html#node=${encodeURIComponent(id)}`;
const stage = $("topoReplay");

function setRange(range) {
  A.range = range;
  try { localStorage.setItem("meshdash.range", range); } catch { /* storage blocked */ }
  for (const b of $("ranges").children) b.classList.toggle("on", b.dataset.range === range);
}
$("ranges").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-range]"); if (!b) return;
  setRange(b.dataset.range);
  INS.est = null;  // position estimates are fetched once per page; refresh with the range
  renderTopology(A.range);
});

// The part of the stage each panel covers, so the graph and map centre in the open area (replay.js asks).
const narrow = () => matchMedia("(max-width: 760px)").matches;
window.vizInset = () => {
  const pad = narrow() ? 8 : 12, box = (sel) => document.querySelector(sel).getBoundingClientRect();
  const right = R.chatOn && !narrow() && !document.body.classList.contains("vz-drive") ? box("#vzChat").width + 2 * pad : 0;
  if (document.body.classList.contains("vz-clean")) return { top: 0, right, bottom: 0, left: 0 };
  const bottom = box("#vzBottom").height + 2 * pad, top = box(".vz-toolbar").height + 2 * pad;
  return narrow()
    ? { top, right: 0, left: 0, bottom: bottom + box(".vz-side").height + pad }
    : { top, right, bottom, left: box(".vz-side").width + 2 * pad };
};
// keep the side panels between the toolbar and the play bar, whatever their heights
new ResizeObserver(() => stage.style.setProperty("--bottom-h", `${$("vzBottom").offsetHeight}px`)).observe($("vzBottom"));
new ResizeObserver(() => stage.style.setProperty("--toolbar-h", `${document.querySelector(".vz-toolbar").offsetHeight}px`)).observe(document.querySelector(".vz-toolbar"));

let resizeT = null;
const redraw = () => renderTopology(A.range);
const redrawSoon = () => { clearTimeout(resizeT); resizeT = setTimeout(redraw, 150); };
addEventListener("resize", redrawSoon);
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", redraw);

// ---------------------------------------------------------------- hide UI / full screen

function setClean(on) {
  document.body.classList.toggle("vz-clean", on);
  hideTip();
  redrawSoon();  // the visualization gets the whole window: re-centre it (the replay keeps its place)
}
$("vzHide").addEventListener("click", () => setClean(true));
$("vzGrow").setAttribute("aria-pressed", String(R.grow));
function toggleChat() {
  setChat(!R.chatOn);
  $("vzChatBtn").setAttribute("aria-pressed", String(R.chatOn));
  redrawSoon();  // the panel takes the right edge: re-frame the graph / map
}
$("vzChatBtn").setAttribute("aria-pressed", String(R.chatOn));
$("vzChatBtn").addEventListener("click", toggleChat);
$("vzGrow").addEventListener("click", () => setGrow(!R.grow));
function syncFadeUI() {
  $("vzFade").setAttribute("aria-pressed", String(R.fade.on));
  $("vzFadeAfter").value = String(R.fade.after);
  $("vzFadeMisses").value = String(R.fade.misses);
  $("vzFadeRemove").value = R.fade.remove ? "1" : "0";
  $("vzFadeMissesRow").hidden = R.fade.after !== "auto";
}
$("vzFade").addEventListener("click", () => { setFade({ on: !R.fade.on }); syncFadeUI(); });
$("vzFadeAfter").addEventListener("change", (e) => { setFade({ after: e.target.value, on: true }); syncFadeUI(); });
$("vzFadeMisses").addEventListener("change", (e) => { setFade({ misses: Number(e.target.value), on: true }); syncFadeUI(); });
$("vzFadeRemove").addEventListener("change", (e) => { setFade({ remove: e.target.value === "1", on: true }); syncFadeUI(); });
syncFadeUI();
$("vzShow").addEventListener("click", () => setClean(false));

// In hidden-UI mode the "Show UI" button appears only while the mouse moves, so it stays out of recordings.
let pointerT = null;
addEventListener("pointermove", () => {
  if (!document.body.classList.contains("vz-clean")) return;
  document.body.classList.add("vz-pointer");
  clearTimeout(pointerT);
  pointerT = setTimeout(() => document.body.classList.remove("vz-pointer"), 1800);
});

function toggleFull() {
  if (document.fullscreenElement) return document.exitFullscreen().catch(() => {});
  const req = document.documentElement.requestFullscreen?.();
  if (!req) return fullUnavailable();
  req.catch(fullUnavailable);
}
function fullUnavailable() {
  const b = $("vzFull"), label = b.textContent;
  b.textContent = "Not available here: try F11";
  setTimeout(() => { b.textContent = label; }, 2500);
}
$("vzFull").addEventListener("click", toggleFull);
document.addEventListener("fullscreenchange", () => {
  $("vzFull").textContent = document.fullscreenElement ? "✕ Exit full screen" : "⛶ Full screen";
  redrawSoon();
});

// H hide/show UI · G grow mode · D fade mode · M texts panel · F full screen · Space play/pause · C clock (hidden-UI mode) · Esc show UI
addEventListener("keydown", (e) => {
  if (e.ctrlKey || e.metaKey || e.altKey || e.target.closest("input, textarea, select, [contenteditable]")) return;
  const clean = document.body.classList.contains("vz-clean");
  if (e.key === "h" || e.key === "H") setClean(!clean);
  else if (e.key === "g" || e.key === "G") setGrow(!R.grow);
  else if (e.key === "d" || e.key === "D") { setFade({ on: !R.fade.on }); syncFadeUI(); }
  else if (e.key === "m" || e.key === "M") toggleChat();
  else if (e.key === "f" || e.key === "F") toggleFull();
  else if (e.key === "c" || e.key === "C") $("vzCleanClock").classList.toggle("off");
  else if (e.key === "Escape" && clean && !document.fullscreenElement) setClean(false);
  else if (e.key === " " && !e.target.closest("button, [role=slider], a, summary")) { e.preventDefault(); $("replayPlay").click(); }
  else return;
});

// phones: start with the key and log folded so the visualization has room
if (narrow()) { $("vzKey").open = false; $("vzLog").open = false; }
for (const d of [$("vzKey"), $("vzLog")]) d.addEventListener("toggle", () => { if (narrow()) redrawSoon(); });

setRange(A.range);
redraw();
