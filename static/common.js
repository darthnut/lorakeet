"use strict";
/* Helpers shared by the Analytics and Visualizations pages. */

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const nf = new Intl.NumberFormat();
const fmt = (v, d = 0) => (v == null ? "—" : Number(v).toLocaleString(undefined, { maximumFractionDigits: d, minimumFractionDigits: d }));
const DOW = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const GROUP_VAR = { Text: "--s1", Position: "--s2", Telemetry: "--s3", "Node info": "--s4", Routing: "--s5", Encrypted: "--s6", Other: "--s7" };
const gcol = (g) => `var(${GROUP_VAR[g] || "--s7"})`;

function ago(ts) {
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}
const when = (ts, opts = {}) => new Date(ts * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit", ...opts });
const pretty = (p) => (p || "").replace(/_APP$/, "").replace(/_/g, " ").toLowerCase().replace(/^\w/, (c) => c.toUpperCase());

/* ---------------------------------------------------------------- tooltip */
const tip = $("tip");
function showTip(e, html) {
  tip.innerHTML = html; tip.hidden = false;
  const x = Math.min(e.clientX + 14, innerWidth - tip.offsetWidth - 8);
  const y = e.clientY - tip.offsetHeight - 12 < 4 ? e.clientY + 16 : e.clientY - tip.offsetHeight - 12;
  tip.style.left = `${x}px`; tip.style.top = `${y}px`;
}
const hideTip = () => { tip.hidden = true; };
const prettyHw = (s) => (s ? s.split("_").map((w) => (/\d/.test(w) || w.length <= 2 ? w.toUpperCase() : w[0] + w.slice(1).toLowerCase())).join(" ") : "Unknown");
const prettyEnum = (s) => (s ? s.replace(/_/g, " ").toLowerCase().replace(/^\w/, (c) => c.toUpperCase()) : "Unknown");
