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
  if (R.protocol === "meshcore") { renderMeshCore(); return; }
  renderConnect();
  const on = R.connected;
  for (const id of ["s2", "s3", "s4"]) $(id).hidden = !on;
  if (on && !busy) { renderChecklist(); renderChannels(); loadAdvanced().then(loadUndo); }
  if (!on) $("adv").hidden = true;
  renderBackups();
  renderStation();
  schedule();
}
// A MeshCore station: what its radio reports, read-only. Its settings are changed in the MeshCore app (or its
// config tool) for now; the Meshtastic steps don't apply.
function renderMeshCore() {
  const r = R.radio || {};
  const row = (k, v) => `<tr><th>${esc(k)}</th><td>${v == null || v === "" ? '<span class="muted">not reported</span>' : esc(v)}</td></tr>`;
  $("connect").innerHTML = (R.connected
    ? `<p class="live ok">Connected: <b>${esc(r.name || r.id)}</b> <span class="muted">(${esc(r.id)})</span>, a MeshCore radio, on ${esc(R.port)}.</p>`
    : `<p class="live wait">MeshCore radio not connected yet${R.port ? ` (trying ${esc(R.port)})` : ""}. This page updates by itself.</p>`
      + (R.error ? `<p class="note">Last error: <code>${esc(R.error)}</code></p>` : "")) +
    `<table class="kv">${row("Frequency", r.freq != null ? `${r.freq} MHz` : null)}${row("Bandwidth", r.bw != null ? `${r.bw} kHz` : null)}` +
    `${row("Spreading factor", r.sf)}${row("Coding rate", r.cr != null ? `4/${r.cr}` : null)}` +
    `${row("Transmit power", r.txPower != null ? `${r.txPower} dBm (max ${r.maxTxPower ?? "?"})` : null)}</table>` +
    `<p class="note">This station logs a MeshCore radio. Lorakeet can't change MeshCore settings yet: use the MeshCore app or its configuration tool (and let Lorakeet's logging pause while it holds the radio). Lorakeet's Meshtastic steps (settings, channels and the logging check) don't apply to it, so they aren't shown.</p>`;
  for (const id of ["s2", "s3", "s4", "adv", "bk"]) if ($(id)) $(id).hidden = true;
  renderStation();
  clearTimeout(pollT);
  pollT = setTimeout(load, R.connected ? 15000 : 3000);
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

const radioName = (p) => `${esc(p.device)}${p.nodeId ? ` (${esc(p.nodeId)})` : ""}`;
const useBtn = (p) => `<button type="button" class="usebtn" data-device="${esc(p.device)}">Log from ${radioName(p)}</button>`;
document.addEventListener("click", async (e) => {
  const b = e.target.closest(".usebtn"); if (!b) return;
  b.disabled = true;
  const r = await fetch("/api/radio/use", {method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({device: b.dataset.device})}).then((x) => x.json()).catch(() => ({error: "Lorakeet isn't answering"}));
  if (r.error) { b.disabled = false; alert(r.error); return; }
  b.textContent = "Connecting…"; setTimeout(load, 4000);
});
function renderConnect() {
  const r = R.radio;
  if (R.connected) {
    $("connect").innerHTML = `<p class="live ok">Connected: <b>${esc(r.longName || r.id)}</b> <span class="muted">(${esc(r.id)})</span>, ${esc((r.hw || "radio").replace(/_/g, " "))}, firmware ${esc(r.firmware || "?")}, on ${esc(R.port)}.</p>
      ${R.otherRadios.length ? `<p class="note">Also plugged in: ${R.otherRadios.map(radioName).join(", ")}. Lorakeet logs from one radio and remembers it, on whichever USB port it's in.${R.portSet ? "" : ` ${R.otherRadios.map(useBtn).join(" ")}`}</p>` : ""}`;
    return;
  }
  if (R.choose) {
    $("connect").innerHTML = `<p class="live wait">Several radios are plugged in. Which one should Lorakeet log from?</p>
      <p>${R.choose.map(useBtn).join(" ")}</p>
      <p class="note">Lorakeet remembers your choice and finds that radio again on any USB port.</p>`;
    return;
  }
  const lines = [`<p class="live wait">No radio connected yet${R.port ? ` (trying ${esc(R.port)})` : ""}. This page updates by itself.</p>`];
  if (R.hint) lines.push(`<p class="hint">${esc(R.hint)}</p>`);
  else if (R.noPortHint) lines.push(`<p class="hint">${esc(R.noPortHint)}</p>`);
  if (R.error && !R.hint) lines.push(`<p class="note">Last error: <code>${esc(R.error)}</code></p>`);
  lines.push(`<p class="note">New radio? Put Meshtastic firmware on it first with the <a href="https://flasher.meshtastic.org" target="_blank" rel="noopener">Meshtastic web flasher</a> (close that tab afterwards: it holds the port).</p>`);
  $("connect").innerHTML = lines.join("");
}

// ---- step 2: settings, and Advanced. ONE pending batch for the whole page: checklist choices and Advanced edits
// both live in PA (setting path -> new value, as the page shows it), the names on their own. Review and save sends
// all of it in one request: one backup, one settings transaction, one restart, then everything is read back.
const label = (it, v) => (it.options.find((o) => o.value === v) || {}).label ?? (v === true ? "On" : v === false ? "Off" : String(v));
// enum names for people: words with digits and known abbreviations stay as they are (EU 868, US, OLED SH1106)
const ACRONYMS = new Set(["US", "EU", "CN", "JP", "ANZ", "KR", "TW", "RU", "IN", "NZ", "TH", "UA", "MY", "SG", "PH", "KZ", "NP", "BR",
  "LORA", "GPS", "OLED", "TAK", "DHCP", "MQTT", "PIN", "LED", "PWM", "UDP", "IP", "RX", "TX", "NMEA", "WS85", "VE", "MS", "TCP", "HQ", "RTO", "K9"]);
const nice = (s) => String(s).split("_").map((w, i) => (/\d/.test(w) || ACRONYMS.has(w)) ? w
  : i ? w.toLowerCase() : w.charAt(0) + w.slice(1).toLowerCase()).join(" ");
// does this new value need the warning and a confirmation? (the server applies the same rules: radio_advanced.RISK_WHEN)
function riskNow(f, v) {
  if (!f || !f.risk) return false;
  const r = f.riskWhen;
  if (!r) return true;
  if ("eq" in r) return v === r.eq;
  if ("gt" in r) return typeof v === "number" && v > r.gt;
  if ("notIn" in r) return !r.notIn.includes(v);
  return true;
}
let ADV = null, FIELDS = {}, SECT = {};
const PA = {}, REVEAL = new Set();  // pending values; secrets whose "replace" box is open
const pathOf = (it) => R?.checklistPaths?.[it.id];

function renderChecklist() {
  $("longName").value = R.radio.longName || ""; $("shortName").value = R.radio.shortName || "";
  $("checklist").innerHTML = R.checklist.map((it, i) => {
    const [cls, txt] = BADGE[it.status] || BADGE.info;
    const sel = it.options.length ? `<select data-i="${i}" aria-label="${esc(it.title)}">${it.options.map((o) => `<option value="${i}:${esc(JSON.stringify(o.value))}" ${o.value === it.current ? "selected" : ""}>${esc(o.label)}${o.value === it.current ? " (now)" : ""}${o.value === it.recommended ? " · recommended" : ""}${o.note ? ` · ${esc(o.note)}` : ""}</option>`).join("")}</select>` : "";
    const rec = it.recommended != null && it.recommended !== it.current ? ` <button class="linkbtn" data-rec="${i}">Use the recommendation</button>` : "";
    return `<div class="ci ${cls}"><div class="ch"><span class="badge2 ${cls}">${txt}</span><b>${esc(it.title)}</b><span class="cur">${esc(it.options.length ? label(it, it.current) : it.current)}</span></div>
      <p class="why">${esc(it.why)}</p>${sel || rec ? `<div class="cc">${sel}${rec}</div>` : ""}</div>`;
  }).join("");
  syncChecklist();
  pending();
}
function syncChecklist() {  // show PA in the checklist's own selects (a value only Advanced offers gets an extra option)
  for (const s of $("checklist").querySelectorAll("select")) {
    const it = R.checklist[s.dataset.i], p = pathOf(it); if (!p) continue;
    const want = p in PA ? PA[p] : it.current, key = `${s.dataset.i}:${JSON.stringify(want)}`;
    if (![...s.options].some((o) => o.value === key)) s.add(new Option(`${nice(want)} (set in Advanced)`, key));
    s.value = key;
  }
}
function setPending(p, v, current) {
  if (JSON.stringify(v) === JSON.stringify(current)) delete PA[p]; else PA[p] = v;
  const row = document.querySelector(`[data-row="${CSS.escape(p)}"]`);
  row?.classList.toggle("changed", p in PA);
  row?.classList.toggle("riskon", p in PA && riskNow(FIELDS[p], PA[p]));
  syncChecklist();
  pending();
}
$("checklist").addEventListener("change", (e) => {
  const s = e.target.closest("select"); if (!s) return;
  const it = R.checklist[s.dataset.i], p = pathOf(it); if (!p) return;
  const v = JSON.parse(s.value.slice(s.value.indexOf(":") + 1));
  setPending(p, v, it.current);
  rerenderField(p);
});
$("checklist").addEventListener("click", (e) => {
  const b = e.target.closest("[data-rec]"); if (!b) return;
  const it = R.checklist[b.dataset.rec], p = pathOf(it); if (!p) return;
  setPending(p, it.recommended, it.current);
  rerenderField(p);
});
function namesChanged() { return $("longName").value.trim() !== (R.radio.longName || "") || $("shortName").value.trim() !== (R.radio.shortName || ""); }
$("longName").addEventListener("input", pending); $("shortName").addEventListener("input", pending);

function pending() {
  const n = Object.keys(PA).length + (namesChanged() ? 1 : 0);
  const txt = n ? `${n} change${n > 1 ? "s" : ""} waiting` : "";
  $("apply").disabled = !n || busy;
  if (!busy) { $("applyNote").textContent = txt; $("saveNote").textContent = ""; }
  $("saveCount").textContent = txt;
  $("saveReview").disabled = !n || busy; $("saveDiscard").disabled = !n || busy;
  $("saveBar").hidden = !n && !busy;
  for (const el of document.querySelectorAll("[data-count]")) {
    const c = Object.keys(PA).filter((p) => p.startsWith(el.dataset.count + ".")).length;
    el.textContent = c ? `${c} changed` : "";
  }
}

// -- Advanced
async function loadAdvanced() {
  let d = null;
  try { d = await fetch("/api/radio/advanced").then((r) => r.json()); } catch { /* restarting */ }
  if (!d || d.error) { ADV = null; $("adv").hidden = true; return; }
  ADV = d; FIELDS = {}; SECT = {};
  for (const s of d.sections) for (const f of s.fields) { FIELDS[f.path] = f; SECT[f.path] = s; }
  for (const p of Object.keys(PA)) if (!FIELDS[p] || FIELDS[p].readonly) delete PA[p];  // gone from this radio
  renderAdvanced();
}
function renderAdvanced() {
  if (!ADV) return;
  $("adv").hidden = false;
  const open = new Set([...document.querySelectorAll(".advsec[open]")].map((d) => d.dataset.sec));
  const covered = new Set(Object.values(R?.checklistPaths || {}));
  $("advSections").innerHTML = ADV.sections.map((s) => `<details class="advsec" data-sec="${esc(s.id)}" ${open.has(s.id) ? "open" : ""}>
    <summary><b>${esc(s.title)}</b> <span class="advcount" data-count="${esc(s.id)}"></span>${s.writable ? "" : ' <span class="muted">· read-only here</span>'}</summary>
    ${s.about ? `<p class="note">${esc(s.about)}</p>` : ""}${s.writable ? "" : '<p class="note">This firmware section can\'t be changed from Lorakeet: use the Meshtastic app.</p>'}
    <div class="advfields">${s.fields.map((f) => fieldRow(f, covered.has(f.path))).join("")}</div></details>`).join("");
  applyFilter();
  pending();
}
function shown(f, v) {
  if (f.secret) return v === undefined ? (f.set ? "set" : "not set") : v === "" ? "cleared" : "a new one";
  if (f.type === "bool") return v ? "On" : "Off";
  if (f.type === "enum") return nice(v);
  if (f.type === "list") return v.length ? v.join(", ") : "none";
  if (f.type === "int" && v >= 2147483647) return "never";
  return v === "" ? "(empty)" : `${v}${f.unit ? ` ${f.unit}` : ""}`;
}
function fieldRow(f, covered) {
  const p = f.path, has = p in PA, v = has ? PA[p] : f.value, dis = f.readonly || busy ? "disabled" : "", a = `data-p="${esc(p)}" aria-label="${esc(f.label)}"`;
  let ctl;
  if (f.type === "bool") ctl = `<input type="checkbox" ${a} ${v ? "checked" : ""} ${dis || (f.oneWay === false && !f.value ? "disabled" : "")}>`;
  else if (f.type === "enum") ctl = `<select ${a} ${dis}>${f.options.map((o) => `<option value="${esc(o)}" ${o === v ? "selected" : ""}>${esc(nice(o))}${o === f.value ? " (now)" : ""}</option>`).join("")}</select>`;
  else if (f.type === "int") ctl = `<input type="number" ${a} step="1" min="${f.min}" max="${f.max}" value="${esc(v)}" ${dis}>`;
  else if (f.type === "float") ctl = `<input type="number" ${a} step="any" value="${esc(v)}" ${dis}>`;
  else if (f.type === "string" || f.type === "ip") ctl = `<input type="text" ${a} maxlength="${f.maxlen || 64}" value="${esc(v)}" ${dis} spellcheck="false">`;
  else if (f.secret) ctl = `<span class="muted">${f.set ? "set" : "not set"}</span> ${has || REVEAL.has(p)
    ? `<input type="password" ${a} autocomplete="new-password" ${f.type === "pin" ? 'inputmode="numeric" maxlength="6"' : 'maxlength="63"'} value="${esc(has ? PA[p] : "")}" placeholder="${f.type === "pin" ? "six digits" : "new password"}" ${dis}>`
    : `<button class="linkbtn" data-reveal="${esc(p)}" ${dis}>Replace…</button>`}`;
  else if (f.type === "locked") ctl = `<span class="muted">${f.present ? "present" : "none"} · not changed here</span>`;
  else ctl = `<span class="muted">${esc(shown(f, f.value))}</span>`;
  return `<div class="advf${has ? " changed" : ""}${f.risk ? " risky" : ""}${has && riskNow(f, v) ? " riskon" : ""}" data-row="${esc(p)}" title="${esc(f.risk)}" data-q="${esc(`${f.label} ${p} ${f.help} ${f.risk}`.toLowerCase())}">
    <div class="advl">${esc(f.label)}${f.unit ? ` <span class="muted">(${esc(f.unit)})</span>` : ""}${f.type === "int" && f.value >= 2147483647 ? ' <span class="muted">· now: never</span>' : ""}</div>
    <div class="advc">${ctl}${f.readonly ? "" : ` <button class="linkbtn undo" data-undo="${esc(p)}">undo</button>`}</div>
    ${f.help ? `<p class="why">${esc(f.help)}</p>` : ""}${f.risk ? `<p class="risk">${esc(f.risk)}</p>` : ""}${covered ? '<p class="why">Also in Settings above: either place changes the same setting.</p>' : ""}</div>`;
}
function rerenderField(p) {
  const row = document.querySelector(`[data-row="${CSS.escape(p)}"]`), f = FIELDS[p];
  if (row && f) row.outerHTML = fieldRow(f, Object.values(R?.checklistPaths || {}).includes(p));
  applyFilter();
}
function readControl(el) {  // a control's value as the page keeps it, or undefined to drop the change
  const f = FIELDS[el.dataset.p];
  if (f.type === "bool") return el.checked;
  if (f.type === "int" || f.type === "float") return el.value.trim() === "" ? undefined : Number(el.value);
  return el.value;
}
function onAdvEdit(e) {
  const el = e.target.closest("[data-p]"); if (!el) return;
  const f = FIELDS[el.dataset.p], v = readControl(el);
  if (f.secret) { if (v === "" && !REVEAL.has(f.path)) delete PA[f.path]; else PA[f.path] = v; setPending(f.path, PA[f.path], Symbol("secret")); return; }
  if (v === undefined) { delete PA[f.path]; setPending(f.path, f.value, f.value); return; }
  setPending(f.path, v, f.value);
}
$("advSections").addEventListener("input", (e) => { if (!e.target.matches("select, input[type=checkbox]")) onAdvEdit(e); });
$("advSections").addEventListener("change", (e) => { if (e.target.matches("select, input[type=checkbox]")) onAdvEdit(e); });
$("advSections").addEventListener("click", (e) => {
  const r = e.target.closest("[data-reveal]");
  if (r) { REVEAL.add(r.dataset.reveal); rerenderField(r.dataset.reveal); document.querySelector(`[data-p="${CSS.escape(r.dataset.reveal)}"]`)?.focus(); return; }
  const u = e.target.closest("[data-undo]");
  if (u) { delete PA[u.dataset.undo]; REVEAL.delete(u.dataset.undo); rerenderField(u.dataset.undo); syncChecklist(); pending(); }
});
function applyFilter() {
  const q = $("advSearch").value.trim().toLowerCase(), only = $("advOnlyChanged").checked;
  for (const sec of document.querySelectorAll(".advsec")) {
    let any = false;
    for (const row of sec.querySelectorAll(".advf")) {
      const show = (!q || row.dataset.q.includes(q)) && (!only || row.classList.contains("changed"));
      row.hidden = !show; any ||= show;
    }
    sec.hidden = !any;
    if ((q || only) && any) sec.open = true;
  }
}
$("advSearch").addEventListener("input", applyFilter); $("advOnlyChanged").addEventListener("change", applyFilter);

// -- review and save (one request for everything)
let MODE = "batch", UNDO = null;  // the review dialog saves the pending batch, or undoes the last save
async function loadUndo() {
  try { UNDO = await fetch("/api/radio/undo").then((r) => r.json()); } catch { UNDO = null; }
  const b = $("undoBtn"), n = UNDO?.available ? UNDO.items.length + (UNDO.names ? 1 : 0) : 0;
  b.hidden = !n;
  const when = n ? new Date(UNDO.ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "";
  if (n) b.textContent = `${UNDO.redo ? "Redo" : "Undo"} last ${UNDO.redo ? "undo" : "save"} (${when}, ${n} setting${n > 1 ? "s" : ""})`;
}
// A setting that's also on the checklist is reviewed in the checklist's words ("Status LED: Blinking → Off"), not
// the radio's field name ("LED heartbeat disabled: Off → On"), when both values are among its options.
function checklistWords(f, from, to) {
  const it = !f.secret && (R?.checklist || []).find((c) => pathOf(c) === f.path);
  const has = (v) => it && it.options.some((o) => JSON.stringify(o.value) === JSON.stringify(v));
  return it && has(from) && has(to) ? { title: it.title, from: label(it, from), to: label(it, to) } : null;
}
function reviewRow(f, s, from, to) {
  const w = to === undefined ? null : checklistWords(f, f.value, to);
  const title = w ? esc(w.title) : `${esc(s || "")} · ${esc(f.label)}`;
  const toText = to === undefined ? "the previous one" : w ? w.to : shown(f, to);
  return `<li class="${riskNow(f, to) ? "risky" : ""}"><b>${title}</b>: ${esc(w ? w.from : from)} → <b>${esc(toText)}</b>${riskNow(f, to) ? `<br><span class="risk">${esc(f.risk)}</span>` : ""}</li>`;
}
function showReview(rows, risky, verb) {
  $("revList").innerHTML = rows.join("") || "<li>Nothing to save.</li>";
  $("revConfirmRow").hidden = !risky; $("revConfirm").checked = false;
  $("revSave").disabled = risky; $("revSave").textContent = `${verb} ${rows.length} change${rows.length === 1 ? "" : "s"} ${verb === "Save" ? "to the radio" : ""}`.trim();
  $("revErr").textContent = "";
  $("review").showModal();
}
function openUndo() {
  if (!UNDO?.available) return;
  MODE = "undo";
  $("revTitle").textContent = UNDO.redo ? "Redo what the last undo put back" : "Undo the last save";
  $("revUndoNote").textContent = UNDO.redo
    ? "Puts back the settings the last undo reversed, from the backup taken just before it. Settings changed elsewhere since (in the Meshtastic app, say) aren't touched, and changes waiting here stay waiting."
    : "Puts back what the last save changed, from the backup taken just before it. Settings changed elsewhere since (in the Meshtastic app, say) aren't touched, and changes waiting here stay waiting.";
  $("revUndoNote").hidden = false;
  const rows = UNDO.items.map((i) => reviewRow(i.field, i.section, shown(i.field, i.secret ? undefined : i.field.value), i.secret ? undefined : i.to));
  if (UNDO.names) rows.unshift(`<li><b>Name</b>: ${esc(UNDO.names.from.join(" / "))} → <b>${esc(UNDO.names.to.join(" / "))}</b></li>`);
  showReview(rows, UNDO.items.some((i) => !i.secret && riskNow(i.field, i.to)), UNDO.redo ? "Redo" : "Undo");
}
$("undoBtn").addEventListener("click", openUndo);
function openReview() {
  MODE = "batch";
  $("revTitle").textContent = "Review the changes"; $("revUndoNote").hidden = true;
  const items = Object.entries(PA).map(([p, v]) => ({ p, v, f: FIELDS[p] || { label: p, path: p }, s: SECT[p] }));
  const rows = items.map(({ v, f, s }) => reviewRow(f, s ? s.title : "", shown(f, f.secret ? undefined : f.value), v));
  if (namesChanged()) rows.unshift(`<li><b>Name</b>: ${esc(R.radio.longName)} / ${esc(R.radio.shortName)} → <b>${esc($("longName").value.trim())} / ${esc($("shortName").value.trim())}</b></li>`);
  showReview(rows, items.some((i) => riskNow(i.f, i.v)), "Save");
}
$("revConfirm").addEventListener("change", () => { $("revSave").disabled = !$("revConfirm").checked; });
$("revCancel").addEventListener("click", () => $("review").close());
$("apply").addEventListener("click", openReview);
$("saveReview").addEventListener("click", openReview);
$("saveDiscard").addEventListener("click", () => {
  if (!confirm("Discard every change you haven't saved?")) return;
  for (const p of Object.keys(PA)) delete PA[p];
  REVEAL.clear();
  renderChecklist(); renderAdvanced();
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

$("revSave").addEventListener("click", async () => {
  const undo = MODE === "undo", redo = undo && !!UNDO.redo, undoItems = undo ? UNDO.items : [];
  const sent = undo ? Object.fromEntries(undoItems.filter((i) => !i.secret).map((i) => [i.path, i.to])) : { ...PA };
  const names = undo ? null : namesChanged() ? { long: $("longName").value.trim(), short: $("shortName").value.trim() } : null;
  $("revSave").disabled = true; $("revErr").textContent = "";
  const r = undo ? await post("/api/radio/undo", { confirmRisky: $("revConfirm").checked })
    : await post("/api/radio/apply", { advanced: sent, names, confirmRisky: $("revConfirm").checked });
  if (r.error) { $("revErr").textContent = r.error; $("revSave").disabled = false; return; }
  $("review").close();
  if (r.nothing) { $("applyNote").textContent = "Nothing to change."; return; }
  busy = true; pending();
  const t0 = Date.now() / 1000, note = (t) => { $("applyNote").textContent = t; $("saveNote").textContent = t; };
  note(`${redo ? "Redone" : undo ? "Undone" : "Saved"} in one write (backup ${r.backup}). The radio is restarting to apply it…`);
  const d = await waitForReconnect(t0, note);
  busy = false;
  if (!d) { note("The radio hasn't come back yet. Check it's still plugged in; this page updates when it does."); load(); return; }
  R = d;
  await loadAdvanced();
  // read back: every change compared with what the radio now reports
  const bad = [];
  for (const [p, v] of Object.entries(sent)) {
    const f = FIELDS[p];
    const ok = f && (f.secret ? f.set === (v !== "") : f.type === "float" ? Math.abs(Number(f.value) - Number(v)) < 1e-5 : String(f.value) === String(v));
    if (ok) { if (!undo) delete PA[p]; } else bad.push(f ? `${SECT[p].title} · ${f.label}` : p);
  }
  if (r.names && (d.radio.longName !== r.names[0] || d.radio.shortName !== r.names[1])) bad.push("Name");
  if (!undo) REVEAL.clear();
  renderConnect(); renderChecklist(); renderAdvanced(); renderChannels(); renderBackups(); loadUndo();
  const n = Object.keys(sent).length + (r.names ? 1 : 0) + undoItems.filter((i) => i.secret).length;
  note(bad.length ? `Read back after the restart, ${bad.length} of ${n} didn't take: ${bad.join(", ")}${undo ? "" : " (still listed as waiting)"}. Try again, or restore the backup.`
    : `Done: read back from the radio after its restart, all ${n} change${n > 1 ? "s" : ""} ${redo ? "redone" : undo ? "undone" : "took"}.`);
  $("saveBar").hidden = false;
});
window.addEventListener("beforeunload", (e) => { if (Object.keys(PA).length && !busy) e.preventDefault(); });

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
