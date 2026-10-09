"use strict";
/* The Stations page (pairing.py, /api/hub*): make this computer a hub, pair stations, join a hub, manage stations.
   This computer only: pairing codes carry tokens. */

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const ago = (t) => { if (!t) return "never"; const s = Date.now() / 1000 - t; return s < 90 ? `${Math.round(s)} s ago` : s < 5400 ? `${Math.round(s / 60)} min ago` : s < 129600 ? `${Math.round(s / 3600)} h ago` : `${Math.round(s / 86400)} days ago`; };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const post = (url, body) => fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
  .then(async (r) => ({ status: r.status, ...(await r.json().catch(() => ({ error: `error ${r.status}` }))) }))
  .catch(() => ({ error: "Couldn't reach Lorakeet: is it still running?" }));
const KIND = { lan: "local network", tailscale: "Tailscale" };
let H = null;
document.querySelector(".pagenav [aria-current]")?.scrollIntoView({ block: "nearest", inline: "nearest" });  // narrow screens

async function load() {
  try { H = await fetch("/api/hub").then((r) => r.json()); } catch { H = null; }
  if (H?.demo) { document.querySelector(".lead").textContent = H.error; return; }
  if (!H || H.error) { $("err").textContent = H?.error || "Couldn't reach Lorakeet."; return; }
  render();
}

function render() {
  $("err").textContent = "";
  const mode = H.mode, saved = H.savedMode;
  for (const id of ["offHub", "offJoin", "hubCard", "stationsCard", "pairCard", "colCard"]) $(id).hidden = true;
  restartBanner();
  if (mode === "hub") renderHub();
  else if (mode === "collector") renderCollector();
  else { $("offHub").hidden = saved === "hub"; $("offJoin").hidden = saved === "collector"; }
  if (mode !== "hub" && H.stations.length > 1) renderStations();  // a former hub still lists what it collected
  renderPeers();
}

function restartBanner() {
  const b = $("restartBanner");
  b.hidden = !H.pendingRestart;
  if (!H.pendingRestart) return;
  const what = { hub: "become the hub", collector: "start sending to the hub", off: "stop syncing" }[H.savedMode] || "apply it";
  b.innerHTML = `Saved. Lorakeet needs a restart to ${what}. ` + (H.supervised ? '<button class="btn" id="restartBtn">Restart Lorakeet</button>'
    : "Stop Lorakeet and start it again (the Lorakeet shortcut starts it).");
  $("restartBtn")?.addEventListener("click", restart);
}
async function restart() {
  $("restartBanner").textContent = "Restarting…";
  const r = await post("/api/radio/restart", {});
  if (r.error) { $("restartBanner").textContent = r.error; return; }
  await sleep(3000);
  for (let i = 0; i < 60; i++) {
    try { if ((await fetch("/api/version")).ok) { location.reload(); return; } } catch { /* not up yet */ }
    await sleep(1500);
  }
  $("restartBanner").textContent = "It's taking a while; reload this page in a minute.";
}

// ---- not set up
$("beHub").addEventListener("click", async () => {
  const r = await post("/api/hub/mode", { mode: "hub", lan: $("allowLan").checked, tailscale: $("allowTs").checked });
  if (r.error) { $("err").textContent = r.error; return; }
  H = r; render();
});
function showTest(el, r) {
  el.innerHTML = r.ok ? `<p class="live ok">Reached <b>${esc(r.hubName || "the hub")}</b> at ${esc(r.url)}${r.hubVersion ? ` (Lorakeet ${esc(r.hubVersion)})` : ""}.${r.paused ? " They've <b>paused</b> receiving from this hub for now: what you log waits here and goes when they resume." : ""}</p>`
    : `<p class="hint"><b>${esc(r.hint || "Couldn't reach the hub.")}</b><br><span class="muted">${esc(r.url || "")}: ${esc(r.error || "")}</span></p>`;
}
$("joinTest").addEventListener("click", async () => {
  $("joinResult").innerHTML = '<p class="muted">Testing…</p>';
  const r = await post("/api/hub/test", { code: $("joinCode").value });
  if (r.error && r.status === 400) { $("joinResult").innerHTML = `<p class="hint">${esc(r.error)}</p>`; return; }
  showTest($("joinResult"), r);
});
$("joinBtn").addEventListener("click", async () => {
  $("joinResult").innerHTML = '<p class="muted">Testing and saving…</p>';
  let r = await post("/api/hub/join", { code: $("joinCode").value });
  if (r.status === 409) {
    showTest($("joinResult"), r);
    $("joinResult").insertAdjacentHTML("beforeend", '<p><button class="linkbtn" id="joinAnyway">Save anyway (it keeps trying in the background)</button></p>');
    $("joinAnyway").onclick = async () => { r = await post("/api/hub/join", { code: $("joinCode").value, anyway: true }); if (r.error) { $("joinResult").insertAdjacentHTML("beforeend", `<p class="hint">${esc(r.error)}</p>`); return; } H = r; render(); };
    return;
  }
  if (r.error) { $("joinResult").innerHTML = `<p class="hint">${esc(r.error)}</p>`; return; }
  H = r; render();
});

// ---- hub
function renderHub() {
  $("hubCard").hidden = $("stationsCard").hidden = $("pairCard").hidden = false;
  const addrs = H.addresses.filter((a) => H.allow[a.kind]);
  $("hubAddrs").innerHTML = `<p>Stations reach it at ${addrs.length ? addrs.map((a) => `<code>${esc(a.url)}</code> <span class="muted">(${a.primary ? "main address" : KIND[a.kind] + (a.kind === "lan" ? ", another adapter" : "")})</span>`).join(", ")
    : '<b>no address</b>: this computer has none in the networks allowed below'}. Pairing codes carry these addresses, so stations don't need to type them.</p>`;
  $("hubLan").checked = H.allow.lan; $("hubTs").checked = H.allow.tailscale;
  $("fwNote").hidden = !H.windows;
  document.querySelectorAll(".port").forEach((e) => { e.textContent = H.port; });
  renderStations();
}
$("hubSave").addEventListener("click", async () => {
  const r = await post("/api/hub/mode", { mode: "hub", lan: $("hubLan").checked, tailscale: $("hubTs").checked });
  if (r.error) { $("err").textContent = r.error; return; }
  H = r; render();
});
$("hubOff").addEventListener("click", async () => {
  if (!confirm("Stop being a hub? Stations will keep logging on their own and queue what they log until a hub accepts it again.")) return;
  const r = await post("/api/hub/mode", { mode: "off" });
  if (r.error) { $("err").textContent = r.error; return; }
  H = r; render();
});

function renderStations() {
  $("stationsCard").hidden = false;
  const rows = H.stations.map((s) => {
    const stale = s.lastContact && Date.now() / 1000 - s.lastContact > 600;
    const p = s.pairing;
    const how = s.here ? "this computer" : s.via ? `from peer: ${esc(s.via.label)}` : p ? (p.revoked ? `<span class="bad">revoked</span> (${esc(p.label)})` : `paired: ${esc(p.label)}`)
      : s.sharedToken ? '<span title="Set up by hand with the shared [sync] token. It keeps working; pair it again to give it its own token you can revoke.">shared token</span>' : "—";
    const loc = s.location ? `${(+s.location[0]).toFixed(4)}, ${(+s.location[1]).toFixed(4)}${s.override.location ? " <span class=\"muted\">(set here)</span>" : ""}` : '<span class="muted">not set</span>';
    const acts = s.here ? '<span class="muted">see Radio</span>' : [
      `<button class="linkbtn" data-act="rename" data-s="${esc(s.id)}">Rename</button>`,
      `<button class="linkbtn" data-act="locate" data-s="${esc(s.id)}">Set location</button>`,
      p && !p.revoked ? `<button class="linkbtn" data-act="revoke" data-p="${esc(p.id)}">Revoke</button>` : "",
      `<button class="linkbtn" data-act="forget" data-s="${esc(s.id)}" data-p="${esc(p?.id || "")}">Forget</button>`].filter(Boolean).join(" · ");
    return `<tr><td><b>${esc(s.name)}</b><br><span class="muted mono">${esc(s.id)}</span></td>
      <td class="${stale ? "bad" : ""}">${s.here ? "now" : esc(ago(s.lastContact))}</td><td>${esc(s.version || "—")}</td>
      <td class="num">${s.backlog == null ? "—" : s.backlog}</td><td>${loc}</td><td>${how}</td><td>${acts}</td></tr>`;
  });
  const waiting = H.pairings.filter((e) => e.kind !== "peer" && !e.station && !e.revoked);
  $("stationList").innerHTML = `<table class="chans"><thead><tr><th>Station</th><th>Last contact</th><th>Version</th><th class="num">Backlog</th><th>Location</th><th>Connected by</th><th></th></tr></thead><tbody>${rows.join("")}</tbody></table>
    ${waiting.length ? `<p class="note">Waiting for their first contact: ${waiting.map((e) => `${esc(e.label)} <button class="linkbtn" data-act="revoke" data-p="${esc(e.id)}">Revoke</button>`).join(", ")}</p>` : ""}
    <p class="note"><b>Revoke</b> stops a station sending (its code no longer works; give it a new one to resume). <b>Forget</b> removes it from this list and the maps; everything it logged stays in the database.</p>`;
}
$("stationList").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-act]"); if (!b) return;
  const act = b.dataset.act, body = { action: act, station: b.dataset.s || null, pairing: b.dataset.p || null };
  const st = H.stations.find((s) => s.id === body.station);
  if (act === "rename") {
    const name = prompt("Name for this station on this hub (empty = the name it reports itself):", st?.name || "");
    if (name === null) return;
    body.name = name;
  } else if (act === "locate") {
    const v = prompt("Where is this station's antenna? latitude, longitude (empty = the location it reports itself):", st?.location ? `${st.location[0]}, ${st.location[1]}` : "");
    if (v === null) return;
    const m = v.trim() ? v.split(/[,\s]+/).filter(Boolean).map(Number) : [];
    if (m.length && (m.length !== 2 || m.some((x) => !Number.isFinite(x)))) { $("err").textContent = "Type it as latitude, longitude"; return; }
    body.location = m;
  } else if (act === "revoke") {
    if (!confirm("Revoke this pairing? The station can no longer send to this hub until it's paired again.")) return;
  } else if (act === "forget") {
    if (!confirm("Forget this station? It disappears from this list and the maps (its logged data stays). If it's still paired, its pairing goes too.")) return;
  }
  const r = await post("/api/hub/station", body);
  if (r.error) { $("err").textContent = r.error; return; }
  H = r; render();
});

$("pairBtn").addEventListener("click", async () => {
  $("pairBtn").disabled = true;
  const r = await post("/api/hub/pair", { label: $("pairLabel").value });
  $("pairBtn").disabled = false;
  if (r.error) { $("err").textContent = r.error; return; }
  $("pairLabel").value = "";
  $("pairOut").innerHTML = `<div class="sharebox"><div style="flex:1 1 100%">
    <h3>Pairing code for “${esc(r.label)}”</h3>
    <textarea readonly rows="3" id="codeBox" spellcheck="false">${esc(r.code)}</textarea>
    <p><button class="btn" id="codeCopy">Copy</button></p>
    <p class="note">On the station: in Lorakeet's setup choose <b>This station sends to a hub</b> and paste it (or its Stations page → Send to a hub). On a Raspberry Pi without a screen: <code>venv/bin/python server.py --join &lt;code&gt;</code>, then restart Lorakeet.<br>
    It's shown only now and works for one station. Treat it like a password until it's used. It points the station at ${r.addresses.map((u) => `<code>${esc(u)}</code>`).join(", ")}.</p></div></div>`;
  $("codeCopy").onclick = async () => { try { await navigator.clipboard.writeText(r.code); $("codeCopy").textContent = "Copied"; } catch { $("codeBox").select(); } };
  load();
});

// ---- station (collector)
function renderCollector() {
  $("colCard").hidden = false;
  const c = H.collector || {};
  const total = c.backlog ? Object.values(c.backlog).reduce((a, b) => a + b, 0) : null;
  $("colStatus").innerHTML = `<p>Sending to <code>${esc(c.hubUrl)}</code>.</p>
    <ul class="checks"><li class="${c.lastOk ? "ok" : "wait"}"><b>Last successful sync</b> · ${esc(ago(c.lastOk))}</li>
    ${c.lastError ? `<li class="wait"><b>Last problem</b> · ${esc(ago(c.lastErrorAt))}: ${esc(c.lastError)}</li>` : ""}
    <li class="${total ? "wait" : "ok"}"><b>Waiting to send</b> · ${total == null ? "—" : `${total} row${total === 1 ? "" : "s"}`}</li></ul>`;
}
$("colTest").addEventListener("click", async () => {
  $("colResult").innerHTML = '<p class="muted">Testing…</p>';
  showTest($("colResult"), await post("/api/hub/test", {}));
});
$("colLeave").addEventListener("click", async () => {
  if (!confirm("Stop sending to the hub? This station keeps logging here; nothing is deleted.")) return;
  const r = await post("/api/hub/leave", {});
  if (r.error) { $("err").textContent = r.error; return; }
  H = r; render();
});

load();
setInterval(() => { if (H && H.mode !== "off" && !document.hidden) load(); }, 15000);

// ---- peering: hubs sharing with each other
const SHARE = { default: "All but private text (recommended)", everything: "Everything, including private text and DMs", receptions: "Receptions only (no message text)" };
function renderPeers() {
  $("peerCard").hidden = H.mode === "collector";
  if (H.mode === "collector") return;
  $("peerShare").innerHTML = H.shareLevels.map((k) => `<option value="${k}">${esc(SHARE[k] || k)}</option>`).join("");
  $("peersOut").innerHTML = H.peersOut.length ? `<table class="chans"><thead><tr><th>Hub</th><th>They see</th><th>Pass on</th><th>Exact locations</th><th>Last sync</th><th>Waiting</th><th></th></tr></thead><tbody>${H.peersOut.map((p) => {
    const st = p.status || {};
    return `<tr><td><b>${esc(p.name)}</b><br><span class="muted mono">${esc(p.url)}</span>${st.lastError ? `<br><span class="bad">${esc(st.lastError)}</span>` : ""}${Object.keys(st.refused || {}).length ? `<br><span class="bad" title="${esc(Object.values(st.refused).join("; "))}">They won't take ${Object.keys(st.refused).length} of your stations (they already have them from somewhere else); kept here and retried.</span>` : ""}</td>
      <td><select data-peer="${esc(p.id)}" data-set="share">${H.shareLevels.map((k) => `<option value="${k}" ${k === p.share ? "selected" : ""}>${esc(SHARE[k] || k)}</option>`).join("")}</select></td>
      <td><input type="checkbox" data-peer="${esc(p.id)}" data-set="forward" ${p.forward ? "checked" : ""} title="Also pass on what other peers share with this hub"></td>
      <td><input type="checkbox" data-peer="${esc(p.id)}" data-set="exact" ${p.exact ? "checked" : ""} title="On: exact positions (your antennas, a moving station's route). Off: rounded to about 3 km"></td>
      <td>${p.paused ? '<span class="paused">paused</span>' : esc(ago(st.lastOk))}</td><td class="num">${st.backlog == null ? "—" : st.backlog}</td>
      <td><button class="linkbtn" data-pact="test" data-peer="${esc(p.id)}">Test</button> · ${p.paused ? "" : `<button class="linkbtn" data-pact="sync" data-peer="${esc(p.id)}">Sync now</button> · `}<button class="linkbtn" data-pact="${p.paused ? "resume" : "pause"}" data-peer="${esc(p.id)}">${p.paused ? "Resume" : "Pause"}</button> · <button class="linkbtn" data-pact="remove" data-peer="${esc(p.id)}">Remove</button></td></tr>`;
  }).join("")}</tbody></table><div id="peerTest"></div>` : '<p class="muted">None yet.</p>';
  $("peerInBox").hidden = H.mode !== "hub";
  const incoming = H.pairings.filter((e) => e.kind === "peer");
  $("peersIn").innerHTML = incoming.length ? `<ul class="bklist">${incoming.map((e) => `<li><b>${esc(e.label)}</b> · ${e.revoked ? '<span class="bad">revoked</span>'
    : `${e.paused ? '<span class="paused">paused</span> (what it sends waits on their side)' : e.hub ? `last contact ${esc(ago(e.lastContact))}` : "waiting for its first contact"} · <button class="linkbtn" data-pause="${esc(e.id)}" data-paused="${e.paused ? 1 : ""}">${e.paused ? "Resume" : "Pause"}</button> · <button class="linkbtn" data-act="revoke" data-p="${esc(e.id)}">Revoke</button>`}${e.hub
    ? ` · <button class="linkbtn danger" data-forget="${esc(e.id)}">${e.revoked ? "Delete what it sent" : "Revoke and delete what it sent"}</button>` : ""}</li>`).join("")}</ul>`
    : '<p class="muted">None yet.</p>';
}
$("peersIn").addEventListener("click", async (e) => {
  const pz = e.target.closest("[data-pause]");
  if (pz) {
    const r = await post("/api/hub/station", { action: pz.dataset.paused ? "resume" : "pause", pairing: pz.dataset.pause });
    if (r.error) { $("err").textContent = r.error; return; }
    H = r; render();
    return;
  }
  const f = e.target.closest("[data-forget]");
  if (f) {  // show exactly what goes, then delete (and revoke, so it doesn't just send it again)
    const pv = await post("/api/hub/peer-forget", { pairing: f.dataset.forget, dryRun: true });
    if (pv.error) { $("err").textContent = pv.error; return; }
    if (!pv.rows) { alert("This hub has nothing that only this peer sent." + (pv.revoked ? "" : "\n\n(Use Revoke to stop it sending.)")); return; }
    const list = pv.stations.map((s) => `  ${s.name}: ${s.rows.toLocaleString()} rows`).join("\n");
    if (!confirm(`${pv.revoked ? "" : "Revoke this peer and "}delete everything it sent?\n\n${list}\n\n${pv.rows.toLocaleString()} rows from ${pv.stations.length} station${pv.stations.length === 1 ? "" : "s"}. Data your own stations logged isn't touched. This can't be undone.`)) return;
    const r = await post("/api/hub/peer-forget", { pairing: f.dataset.forget });
    if (r.error) { $("err").textContent = r.error; return; }
    H = r; render();
    alert(`Deleted ${r.deleted.toLocaleString()} rows from ${r.deletedStations} station${r.deletedStations === 1 ? "" : "s"}.`);
    return;
  }
  const b = e.target.closest("[data-act]"); if (b) $("stationList").dispatchEvent(new CustomEvent("peer-revoke", { detail: b.dataset.p }));
});
$("stationList").addEventListener("peer-revoke", async (e) => {
  if (!confirm("Revoke? That hub can no longer send here (what it already sent stays).")) return;
  const r = await post("/api/hub/station", { action: "revoke", pairing: e.detail });
  if (r.error) { $("err").textContent = r.error; return; }
  H = r; render();
});
$("peersOut").addEventListener("change", async (e) => {
  const el = e.target.closest("[data-set]"); if (!el) return;
  if (el.dataset.set === "exact" && el.checked && !confirm("Share exact locations with this hub? They'll see exactly where your antennas are, and the route of any moving station. Only for someone you'd give your address to.\n\n(Locations already sent stay rounded; new ones go exact.)")) { el.checked = false; return; }
  const body = { action: "set", id: el.dataset.peer, [el.dataset.set]: el.type === "checkbox" ? el.checked : el.value };
  const r = await post("/api/hub/peer", body);
  if (r.error) { $("err").textContent = r.error; return; }
  H = r; render();
});
$("peersOut").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-pact]"); if (!b) return;
  const act = b.dataset.pact, id = b.dataset.peer;
  if (act === "remove" && !confirm("Stop sending to this hub? What it already has stays there.")) return;
  if (act === "test") { $("peerTest").innerHTML = '<p class="muted">Testing…</p>'; showTest($("peerTest"), await post("/api/hub/peer", { action: "test", id })); return; }
  const r = await post("/api/hub/peer", act === "pause" || act === "resume" ? { action: "set", id, paused: act === "pause" } : { action: act, id });
  if (r.error) { $("err").textContent = r.error; return; }
  H = r; render();
});
$("peerAdd").addEventListener("click", async () => {
  $("peerAddResult").innerHTML = '<p class="muted">Testing and saving…</p>';
  const body = { code: $("peerCode").value, share: $("peerShare").value, forward: $("peerFwd").checked, exact: $("peerExact").checked };
  let r = await post("/api/hub/peer-add", body);
  if (r.status === 409) {
    showTest($("peerAddResult"), r);
    $("peerAddResult").insertAdjacentHTML("beforeend", '<p><button class="linkbtn" id="peerAnyway">Save anyway (it keeps trying in the background)</button></p>');
    $("peerAnyway").onclick = async () => { r = await post("/api/hub/peer-add", { ...body, anyway: true }); if (r.error) { $("peerAddResult").insertAdjacentHTML("beforeend", `<p class="hint">${esc(r.error)}</p>`); return; } H = r; $("peerCode").value = ""; render(); };
    return;
  }
  if (r.error) { $("peerAddResult").innerHTML = `<p class="hint">${esc(r.error)}</p>`; return; }
  $("peerCode").value = ""; $("peerAddResult").innerHTML = ""; $("peerAddBox").open = false;
  H = r; render();
});
$("peerCodeBtn").addEventListener("click", async () => {
  $("peerCodeBtn").disabled = true;
  const r = await post("/api/hub/peer-code", { label: $("peerLabel").value });
  $("peerCodeBtn").disabled = false;
  if (r.error) { $("err").textContent = r.error; return; }
  $("peerLabel").value = "";
  $("peerCodeOut").innerHTML = `<div class="sharebox"><div style="flex:1 1 100%">
    <h3>Peer code for “${esc(r.label)}”</h3>
    <textarea readonly rows="3" id="peerCodeBox" spellcheck="false">${esc(r.code)}</textarea>
    <p><button class="btn" id="peerCodeCopy">Copy</button></p>
    <p class="note">Send it to them privately. On their hub: Stations → Peers → <b>Send to another hub</b>, paste it, and choose what you'll see. It works for one hub and you can revoke it any time. It's shown only now. It points their hub at ${r.addresses.map((u) => `<code>${esc(u)}</code>`).join(", ")}, so they must be able to reach that (same network, or Tailscale).</p></div></div>`;
  $("peerCodeCopy").onclick = async () => { try { await navigator.clipboard.writeText(r.code); $("peerCodeCopy").textContent = "Copied"; } catch { $("peerCodeBox").select(); } };
  load();
});
