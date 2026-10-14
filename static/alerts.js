"use strict";
/* Alerts bell, injected into every page's top bar (its settings are on the Settings page). Also exposes meshWatch() for stars. */
(() => {
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const ago = (ts) => { const s = Math.max(0, Date.now() / 1000 - ts); return s < 60 ? "just now" : s < 3600 ? `${Math.round(s / 60)}m ago` : s < 86400 ? `${Math.round(s / 3600)}h ago` : `${Math.round(s / 86400)}d ago`; };
  const post = (url, body) => fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }).then((r) => r.json());
  const ST = { alerts: [], unread: 0, settings: null };
  const nodeLink = (id) => (location.pathname.endsWith("analytics.html") ? `#node=${encodeURIComponent(id)}` : `/analytics.html#node=${encodeURIComponent(id)}`);

  const bar = document.querySelector(".topbar");
  if (!bar) return;
  bar.insertAdjacentHTML("beforeend", `<button class="bell" id="bell" aria-haspopup="dialog" aria-expanded="false" title="Alerts">🔔<span class="badge" id="bellCount" hidden></span></button>
    <div class="alertpop" id="alertpop" role="dialog" aria-label="Alerts" hidden></div>`);
  const $ = (id) => document.getElementById(id);

  async function load() {
    try {
      const [a, s] = await Promise.all([fetch("/api/alerts?limit=100").then((r) => r.json()), fetch("/api/settings").then((r) => r.json())]);
      ST.alerts = a.alerts; ST.unread = a.unread; ST.settings = s.settings;
      render();
      document.dispatchEvent(new CustomEvent("mesh-settings", { detail: ST.settings }));
    } catch { /* server restarting */ }
  }

  function render() {
    $("bellCount").hidden = !ST.unread; $("bellCount").textContent = ST.unread;
    if ($("alertpop").hidden) return;
    const head = `<nav class="atabs"><b class="atitle">Alerts</b>${ST.unread ? '<button class="amark" id="aRead">Mark all read</button>' : ""}<a class="amark asettings" href="/settings.html#alerts">Settings</a></nav>`;
    const body = ST.alerts.length ? `<ul class="alist">${ST.alerts.map((a) => `<li class="${a.read ? "" : "unread"} sev-${esc(a.severity)}">
        <div class="ah"><b>${esc(a.title)}</b><span>${esc(ago(a.ts))}</span></div><div class="ad">${esc(a.detail || "")}</div>
        ${a.node ? `<a href="${nodeLink(a.node)}">open node →</a>` : ""}${a.resolved_ts ? ' <span class="muted">· resolved</span>' : ""}</li>`).join("")}</ul>`
      : '<p class="muted" style="padding:12px">No alerts yet.</p>';
    $("alertpop").innerHTML = head + body;
  }

  $("bell").addEventListener("click", (e) => {
    e.stopPropagation();
    const pop = $("alertpop"); pop.hidden = !pop.hidden; $("bell").setAttribute("aria-expanded", String(!pop.hidden));
    if (!pop.hidden) { load(); render(); }
  });
  document.addEventListener("click", (e) => { if (!$("alertpop").hidden && !e.target.closest("#alertpop") && !e.target.closest("#bell")) { $("alertpop").hidden = true; $("bell").setAttribute("aria-expanded", "false"); } });
  $("alertpop").addEventListener("click", async (e) => {
    if (e.target.id === "aRead") { await post("/api/alerts/read", {}); load(); }
  });

  // live: new alerts arrive over the page's shared event stream (prov.js lkEvents)
  window.lkEvents?.on("alert", () => load()).onReopen(() => load());

  window.meshWatch = {
    isWatched: (id) => !!ST.settings?.watched.includes(id),
    toggle: async (id) => { const r = await post("/api/watch", { id, watched: !window.meshWatch.isWatched(id) }); ST.settings = r.settings; render(); document.dispatchEvent(new CustomEvent("mesh-settings", { detail: ST.settings })); return window.meshWatch.isWatched(id); },
    ready: load(),
  };
})();
