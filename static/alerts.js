"use strict";
/* Alerts bell + settings panel, injected into every page's top bar. Also exposes meshWatch() for stars. */
(() => {
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const ago = (ts) => { const s = Math.max(0, Date.now() / 1000 - ts); return s < 60 ? "just now" : s < 3600 ? `${Math.round(s / 60)}m ago` : s < 86400 ? `${Math.round(s / 3600)}h ago` : `${Math.round(s / 86400)}d ago`; };
  const post = (url, body) => fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }).then((r) => r.json());
  const ST = { alerts: [], unread: 0, settings: null, status: null, tab: "alerts" };
  const nodeLink = (id) => (location.pathname.endsWith("analytics.html") ? `#node=${encodeURIComponent(id)}` : `/analytics.html#node=${encodeURIComponent(id)}`);

  const bar = document.querySelector(".topbar");
  if (!bar) return;
  bar.insertAdjacentHTML("beforeend", `<button class="bell" id="bell" aria-haspopup="dialog" aria-expanded="false" title="Alerts">🔔<span class="badge" id="bellCount" hidden></span></button>
    <div class="alertpop" id="alertpop" role="dialog" aria-label="Alerts" hidden></div>`);
  const $ = (id) => document.getElementById(id);

  async function load() {
    try {
      const [a, s] = await Promise.all([fetch("/api/alerts?limit=100").then((r) => r.json()), fetch("/api/settings").then((r) => r.json())]);
      ST.alerts = a.alerts; ST.unread = a.unread; ST.status = s; ST.settings = s.settings;
      render();
      document.dispatchEvent(new CustomEvent("mesh-settings", { detail: ST.settings }));
    } catch { /* server restarting */ }
  }

  function render() {
    $("bellCount").hidden = !ST.unread; $("bellCount").textContent = ST.unread;
    if ($("alertpop").hidden) return;
    const s = ST.settings, at = ST.status?.autoTraceroute || {};
    const tabs = `<nav class="atabs"><button data-t="alerts" class="${ST.tab === "alerts" ? "on" : ""}">Alerts</button><button data-t="settings" class="${ST.tab === "settings" ? "on" : ""}">Settings</button></nav>`;
    let body;
    if (ST.tab === "alerts") {
      body = ST.alerts.length ? `<ul class="alist">${ST.alerts.map((a) => `<li class="${a.read ? "" : "unread"} sev-${esc(a.severity)}">
          <div class="ah"><b>${esc(a.title)}</b><span>${esc(ago(a.ts))}</span></div><div class="ad">${esc(a.detail || "")}</div>
          ${a.node ? `<a href="${nodeLink(a.node)}">open node →</a>` : ""}${a.resolved_ts ? ' <span class="muted">· resolved</span>' : ""}</li>`).join("")}</ul>`
        : '<p class="muted" style="padding:12px">No alerts yet.</p>';
    } else {
      const num = (k, label, step = 1) => `<label class="srow"><span>${label}</span><input type="number" step="${step}" min="0" data-k="${k}" value="${s[k]}"></label>`;
      const chk = (k, label) => `<label class="srow"><span>${label}</span><input type="checkbox" data-k="${k}" ${s[k] ? "checked" : ""}></label>`;
      body = `<div class="sform">
        <h4>Watched nodes</h4>
        <p class="muted">${s.watched.length ? s.watched.map((id) => `<a href="${nodeLink(id)}">${esc(id)}</a>`).join(", ") : "none"}. Star a node on its Analytics page or in the map's node panel.</p>
        ${num("silenceHours", "Alert when silent for (hours)", 0.5)}${chk("notifySilence", "…and notify")}${chk("notifyBack", "Notify when it's back")}
        ${num("lowBatteryPct", "Low battery at (%)")}${chk("notifyLowBattery", "…and notify")}
        <h4>Whole mesh</h4>
        ${chk("notifyNewNodes", "Notify about new nodes")}${num("newNodeBatchMin", "…at most every (min)")}
        ${chk("notifyHealth", "Notify on new health warnings (daily check)")}
        <h4>Scheduled traceroutes <span class="muted">(transmits)</span></h4>
        ${chk("autoTraceroute", "Enabled")}${num("autoTracerouteMin", "At most one every (min)")}${num("autoTracerouteMaxChUtil", "Skip when channel busier than (%)")}
        ${num("autoTracerouteRefreshHours", "Re-trace a node after (hours)")}${num("autoTracerouteRetryHours", "Retry a non-answering node after (hours)")}
        <p class="muted">Last: ${at.ts ? `${esc(ago(at.ts))}: ${at.target ? `<a href="${nodeLink(at.target)}">${esc(at.target)}</a> · ` : ""}${esc(at.reason || "")}` : "not run yet this session"}</p>
        <p class="muted" id="sSaved"></p></div>`;
    }
    $("alertpop").innerHTML = tabs + body + (ST.tab === "alerts" && ST.unread ? '<div class="afoot"><button class="btn" id="aRead">Mark all read</button></div>' : "");
  }

  $("bell").addEventListener("click", (e) => {
    e.stopPropagation();
    const pop = $("alertpop"); pop.hidden = !pop.hidden; $("bell").setAttribute("aria-expanded", String(!pop.hidden));
    if (!pop.hidden) { load(); render(); }
  });
  document.addEventListener("click", (e) => { if (!$("alertpop").hidden && !e.target.closest("#alertpop") && !e.target.closest("#bell")) { $("alertpop").hidden = true; $("bell").setAttribute("aria-expanded", "false"); } });
  $("alertpop").addEventListener("click", async (e) => {
    const t = e.target.closest("[data-t]"); if (t) { ST.tab = t.dataset.t; render(); return; }
    if (e.target.id === "aRead") { await post("/api/alerts/read", {}); load(); }
  });
  $("alertpop").addEventListener("change", async (e) => {
    const k = e.target.dataset.k; if (!k) return;
    const v = e.target.type === "checkbox" ? e.target.checked : Number(e.target.value);
    const r = await post("/api/settings", { [k]: v });
    if (r.error) { $("sSaved").textContent = r.error; return; }
    ST.settings = r.settings; $("sSaved").textContent = "Saved.";
  });

  // live: new alerts arrive over the page's shared event stream (prov.js lkEvents)
  window.lkEvents?.on("alert", () => load()).onReopen(() => load());

  window.meshWatch = {
    isWatched: (id) => !!ST.settings?.watched.includes(id),
    toggle: async (id) => { const r = await post("/api/watch", { id, watched: !window.meshWatch.isWatched(id) }); ST.settings = r.settings; render(); document.dispatchEvent(new CustomEvent("mesh-settings", { detail: ST.settings })); return window.meshWatch.isWatched(id); },
    ready: load(),
  };
})();
