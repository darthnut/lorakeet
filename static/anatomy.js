"use strict";
/* Packet anatomy: one logged packet shown layer by layer, from the radio signal down to the decoded
   fields, with linked hex dumps (hover a byte to find its field, and the reverse), beside the logged
   JSON. Fields and JSON lines are linked both ways: hover one to light up the other, click to scroll
   to it. Data from GET /api/packet/<rowid>/anatomy (anatomy.py). Needs esc() and prov() (prov.js). */

const ANAT_PROV = {  // anatomy provenance -> page-wide badge kind + wording
  received: ["reported", "as received", "Bytes exactly as our radio reported them."],
  logged: ["reported", "logged values", "Values our radio reported, placed in the firmware's byte layout."],
  rebuilt: ["inferred", "rebuilt", "Re-derived from logged values: a reconstruction, not a capture."],
  unknown: ["unknown", "not available", "Nothing was logged for this."],
};
const anatBadge = (k, note) => prov((ANAT_PROV[k] || ANAT_PROV.unknown)[0], note);
const anatPill = (k) => { const p = ANAT_PROV[k] || ANAT_PROV.unknown; return `<span class="anat-pill anat-${k}" title="${esc(p[2])}">${esc(p[1])}</span>`; };

async function openAnatomy(el, rowid) {
  el.innerHTML = '<p class="muted">Rebuilding the packet…</p>';
  let a;
  try {
    a = await fetch(`/api/packet/${rowid}/anatomy`).then((r) => r.json());
    if (a.error) throw new Error(a.error);
  } catch (e) { el.innerHTML = `<p class="muted">Couldn't build the anatomy: ${esc(e.message)}</p>`; return; }
  el.innerHTML = renderAnatomy(a);
  wireAnatomy(el);
}

/* ---------------------------------------------------------------- hex dump with linked fields */

function hexdump(hex, fields, { unknown = [], missing = null, tagged = false } = {}) {
  const bytes = hex.match(/../g) || [];
  // byte -> field: fields are listed outer before inner, so the deepest one wins
  const owner = new Array(bytes.length).fill(-1), tag = new Array(bytes.length).fill(false);
  fields.forEach((f, i) => {
    for (let k = f.start; k < f.start + f.len && k < bytes.length; k++) { owner[k] = i; tag[k] = tagged && k < f.start + (f.tagLen || 0); }
  });
  let rows = "";
  for (let r = 0; r < bytes.length; r += 16) {
    let hx = "", asc = "";
    for (let k = r; k < Math.min(r + 16, bytes.length); k++) {
      const f = owner[k], unk = unknown.includes(k), cls = `b${f >= 0 ? ` f${f % 6}` : ""}${tag[k] ? " tag" : ""}${unk ? " unk" : ""}`;
      const v = parseInt(bytes[k], 16);
      hx += `<span class="${cls}" data-f="${f}" data-k="${k}">${unk ? "??" : bytes[k]}</span>`;
      asc += `<span class="${cls}" data-f="${f}" data-k="${k}">${unk ? "?" : v >= 32 && v < 127 ? esc(String.fromCharCode(v)) : "·"}</span>`;
    }
    rows += `<div class="hx-row"><span class="hx-off">${r.toString(16).padStart(4, "0")}</span><span class="hx-bytes">${hx}</span><span class="hx-asc">${asc}</span></div>`;
  }
  if (missing) rows += `<div class="hx-row hx-missing"><span class="hx-off">${bytes.length.toString(16).padStart(4, "0")}</span><span>${esc(missing)}</span></div>`;
  return `<div class="hexdump" role="img" aria-label="Hex dump, ${bytes.length} bytes">${rows}</div>`;
}

function fieldTable(fields, { showProv = false, tagged = false } = {}) {
  return `<table class="anat-fields"><thead><tr><th>Field</th><th class="num">Bytes</th><th>Value</th></tr></thead><tbody>${fields.map((f, i) => `
    <tr data-f="${i}" data-j="${esc((f.json || []).join(" "))}" class="f${i % 6}${f.name === "unparsed" ? " bad" : ""}"><td style="padding-left:${8 + (f.depth || 0) * 14}px"><i class="sw"></i>${esc(f.name)}${tagged && f.field ? ` <span class="muted">#${f.field}</span>` : ""}${showProv && f.prov ? anatBadge(f.prov === "received" || f.prov === "logged" ? "logged" : f.prov, f.src) : ""}</td>
      <td class="num mono">${f.len === 1 ? f.start : `${f.start}–${f.start + f.len - 1}`}</td>
      <td class="mono">${esc(f.value ?? "")}${f.wire ? ` <span class="muted">${esc(f.wire)}</span>` : ""}${f.bits ? ' <span class="muted">(bits below)</span>' : ""}</td></tr>`).join("")}</tbody></table>`;
}

const layer = (n, title, pill, explain, body) => `<section class="anat-layer"><div class="anat-lh"><span class="anat-n">${n}</span><h4>${esc(title)}</h4>${pill || ""}</div><p class="anat-x">${explain}</p>${body}</section>`;

/* ---------------------------------------------------------------- the view */

function renderAnatomy(a) {
  const r = a.radio, fr = a.frame;
  const flags = fr.fields.find((f) => f.name === "flags");
  const flagBits = flags ? parseInt(flags.value.slice(2), 2).toString(2).padStart(8, "0") : null;
  let n = 0, html = "";

  html += a.checks.length ? `<ul class="anat-checks">${a.checks.map((c) => `<li class="${c.ok ? "ok" : "bad"}"><span aria-hidden="true">${c.ok ? "✓" : "✗"}</span> ${esc(c.text)}</li>`).join("")}</ul>` : "";

  // 1. radio
  const pre = r.preambleMs, pay = r.payloadMs, tot = r.airtimeMs;
  html += layer(++n, "Radio signal (LoRa)", anatPill(r.presetAssumed ? "unknown" : "logged"),
    `Sent as LoRa chirps: tones sweeping across ${r.bwKHz} kHz, each carrying ${r.bitsPerSymbol} bits. Our radio's chip turns them back into bytes, so the chirps themselves aren't recorded (that would need an SDR); this is what can be computed from the radio's settings and the frame size.`,
    `<div class="anat-kv">
      <span><b>${r.freqMHz != null ? `${r.freqMHz} MHz` : "—"}</b>${r.slot ? `slot ${r.slot} of ${r.slots}` : "frequency"}</span>
      <span><b>${esc(r.preset.replace(/_/g, " ").toLowerCase().replace(/^\w/, (c) => c.toUpperCase()))}</b>SF${r.sf} · ${r.bwKHz} kHz · CR ${esc(r.cr)}</span>
      <span><b>${tot} ms</b>time on air</span>
      <span><b>${r.onAirBytes} B</b>frame + 2 B CRC</span>
      ${a.rssi != null ? `<span data-j="rxRssi"><b>${esc(a.rssi)} dBm</b>RSSI ${prov("observed", "Our radio's reading of this copy.")}</span>` : ""}
      ${a.snr != null ? `<span data-j="rxSnr"><b>${esc(a.snr)} dB</b>SNR ${prov("observed", "Our radio's reading of this copy.")}</span>` : ""}
    </div>
    <div class="anat-air" role="img" aria-label="Time on air: preamble ${pre} ms, header and payload ${pay} ms">
      <span class="pre" style="flex:${pre}">preamble · ${r.preambleSymbols} + 4.25 symbols · ${pre} ms</span>
      <span class="pay" style="flex:${pay}">sync ${esc(r.syncWord)} · PHY header · ${r.onAirBytes}-byte frame · CRC — ${r.payloadSymbols} symbols · ${pay} ms</span>
    </div>
    ${r.slotHow ? `<p class="muted small">Frequency: ${esc(r.slotHow)}. Symbol time ${r.symbolMs} ms.${r.presetAssumed ? " Radio settings not yet recorded: LongFast assumed." : ""}</p>` : ""}`);

  // 2. frame
  const missing = fr.bodyMissing && fr.bodyLen ? `… ${fr.bodyLen} bytes of encrypted body: ${fr.bodyProv ? fr.bodyProv[1] : "not available"}` : null;
  html += layer(++n, "On-air frame", `${anatPill("logged")}${fr.bodyProv ? anatPill(fr.bodyProv[0]) : ""}`,
    "What the radio transmits: a 16-byte header in the clear (so every radio can route it without the key), then the encrypted body. Numbers are little-endian: the bytes of a node id read backwards.",
    `<div class="anat-split">${hexdump(fr.hex, fr.fields, { unknown: fr.unknownBytes, missing })}${fieldTable(fr.fields, { showProv: true })}</div>
     ${flagBits ? `<div class="anat-bits" aria-label="Flags byte in binary">${[...flagBits].map((bit, i) => `<span class="${i < 3 ? "hs" : i === 3 ? "mq" : i === 4 ? "ack" : "hl"}">${bit}</span>`).join("")}
</div>
       <div class="anat-bitlg">${flags.bits.map((b) => `<span title="${esc(b.src || "")}"><i class="${{ "hop start": "hs", "via MQTT": "mq", "want ACK": "ack", "hop limit": "hl" }[b.name] || ""}"></i>bit${b.bits.length > 1 ? "s" : ""} ${esc(b.bits)} ${esc(b.name)} = <b>${esc(b.value ?? "?")}</b></span>`).join("")}<span class="muted">the flags byte (byte 12) in binary</span></div>` : ""}`);

  // a sender or recipient whose key can't be trusted (keyflags.py)
  for (const k of a.keyFlags || []) html += `<p class="kf-note">⚠ <b>${k.role === "sender" ? "Sender" : "Recipient"} ${esc(k.name)}</b>: ${esc(k.text)}${k.role === "recipient" && a.json?.pkiEncrypted ? " This direct message's encryption depends on that key." : ""}</p>`;

  // 3. encryption
  if (a.crypto) {
    const c = a.crypto;
    html += layer(++n, "Encryption", anatPill(c.rebuilt ? "rebuilt" : "unknown"),
      c.nonce ? `${esc(c.scheme)}: the envelope below is XORed with a keystream made by encrypting this 16-byte counter block with the channel key. The packet id and sender make every packet's keystream different. Key: ${esc(c.key)}.`
        : esc(c.note || c.scheme),
      c.nonce ? `<div class="anat-split">${hexdump(c.nonce, c.nonceFields)}${fieldTable(c.nonceFields)}</div>` : "");
  }

  // 4. envelope
  const tf = a.tracerouteFix;
  if (a.data) html += layer(++n, "Decrypted envelope (Data)", anatPill("rebuilt"),
    "Inside the encryption: a protobuf saying which app the packet is for, plus the app's bytes. Each field starts with a tag byte (field number × 8 + wire type, shown dimmed). Rebuilt from the logged fields." +
    (tf ? ` Its payload is the traceroute <b>as it was on air</b>: without the ${esc(tf.field)} entry our radio added on arrival${tf.verified ? ", which reproduces the length the radio logged exactly" : ""}.` : ""),
    `<div class="anat-split">${hexdump(a.data.hex, a.data.fields, { tagged: true })}${fieldTable(a.data.fields, { tagged: true })}</div>`);

  // 5. payload
  if (a.payload) html += layer(++n, `App payload${a.payload.type ? ` (${a.payload.type})` : ""}`, anatPill("received"),
    "The app's own message, exactly as our radio passed it on. Decoded field by field from the protobuf definition." +
    (tf ? ` <b>Note:</b> this is one entry longer than what was on air. When a traceroute addressed to us arrives, our radio appends its own reception SNR to <code>${esc(tf.field)}</code>: the last value here, ${tf.value} (÷ 4 = ${tf.db} dB). Protobuf stores a negative number in 10 bytes, a small positive one in 1.` : ""),
    `<div class="anat-split">${hexdump(a.payload.hex, a.payload.fields, { tagged: true })}${fieldTable(a.payload.fields, { tagged: true })}</div>`);
  else if (!a.data) html += layer(++n, "Contents", anatPill("unknown"), "Our radio has no key for this channel, so everything past the header stays encrypted.", "");

  if (a.notes.length) html += `<p class="muted small">${a.notes.map(esc).join(" ")}</p>`;
  return `<div class="anat-wrap"><div class="anat">${html}</div>
    <aside class="anat-jsonpane" aria-label="Logged JSON">
      <div class="anat-jh"><b>Logged JSON</b><span>the library's decode, as stored. Hover a line to find its bytes.</span></div>
      <div class="anat-jtree">${jsonLines(a.json)}</div>
    </aside></div>`;
}

/* ---------------------------------------------------------------- JSON pane */

// a JSON path with array indexes dropped: "decoded.traceroute.route.0" -> "decoded.traceroute.route"
const jnorm = (p) => p.replace(/\.\d+(?=\.|$)/g, "");

function jsonLines(v) {
  const out = [];
  const val = (x) => (typeof x === "string" ? `<span class="js">${esc(JSON.stringify(x))}</span>`
    : x === null ? '<span class="jn">null</span>' : `<span class="jn">${esc(String(x))}</span>`);
  const walk = (x, path, key, depth, comma) => {
    const pre = key != null ? `<span class="jk">${esc(JSON.stringify(key))}</span>: ` : "";
    const line = (html) => out.push(`<div class="jl" data-p="${esc(jnorm(path))}" style="padding-left:${depth * 2}ch">${html}</div>`);
    const c = comma ? "," : "";
    if (x && typeof x === "object") {
      const entries = Array.isArray(x) ? x.map((y, i) => [i, y]) : Object.entries(x);
      const [o, z] = Array.isArray(x) ? ["[", "]"] : ["{", "}"];
      if (!entries.length) return line(`${pre}${o}${z}${c}`);
      line(`${pre}${o}`);
      entries.forEach(([k, y], i) => walk(y, path ? `${path}.${k}` : String(k), Array.isArray(x) ? null : k, depth + 1, i < entries.length - 1));
      return line(`${z}${c}`);
    }
    line(`${pre}${val(x)}${c}`);
  };
  walk(v, "", null, 0, false);
  return out.join("");
}

function wireAnatomy(el) {
  const pane = el.querySelector(".anat-jsonpane");
  const lines = [...el.querySelectorAll(".jl")];
  const clear = () => el.querySelectorAll(".hi, .jhi").forEach((x) => x.classList.remove("hi", "jhi"));
  const light = (sec, f) => { for (const x of sec.querySelectorAll(`[data-f="${f}"]`)) x.classList.add("hi"); };
  const pathsOf = (node) => (node?.dataset.j || "").split(" ").filter(Boolean);
  // JSON lines for a field: its own keys and everything under them
  const linesFor = (paths) => lines.filter((l) => l.dataset.p && paths.some((p) => l.dataset.p === p || l.dataset.p.startsWith(`${p}.`)));
  // fields for a JSON line: an exact key match, else the closest enclosing field (e.g. a whole message)
  const fieldsFor = (p) => {
    let best = 0, rows = [];
    for (const tr of el.querySelectorAll("tr[data-j]")) {
      for (const fp of pathsOf(tr)) {
        const score = fp === p ? 1000 : p.startsWith(`${fp}.`) ? fp.length : 0;
        if (score > best) { best = score; rows = [tr]; } else if (score && score === best && !rows.includes(tr)) rows.push(tr);
      }
    }
    return rows;
  };
  const reveal = (node) => {  // scroll the JSON pane (not the page) so a line is visible
    if (!pane || !node) return;
    const top = node.offsetTop - pane.querySelector(".anat-jtree").offsetTop;
    if (top < pane.scrollTop + 30 || top > pane.scrollTop + pane.clientHeight - 30) pane.scrollTop = top - pane.clientHeight / 3;
  };

  el.addEventListener("pointerover", (e) => {
    const jl = e.target.closest(".jl");
    if (jl) {
      clear();
      jl.classList.add("jhi");
      for (const tr of fieldsFor(jl.dataset.p)) { light(tr.closest(".anat-layer"), tr.dataset.f); for (const x of linesFor(pathsOf(tr))) x.classList.add("jhi"); }
      return;
    }
    const t = e.target.closest("[data-f], [data-j]"); if (!t || t.dataset.f === "-1") return;
    clear();
    const sec = t.closest(".anat-layer");
    const row = t.matches("[data-f]") ? sec.querySelector(`tr[data-f="${t.dataset.f}"]`) : t;
    if (t.dataset.f != null) light(sec, t.dataset.f);
    const ls = linesFor(pathsOf(row));
    ls.forEach((x) => x.classList.add("jhi"));
    reveal(ls[0]);
  });
  el.addEventListener("pointerleave", clear);
  // click a JSON line: bring its field into view; click a field: bring its JSON into view
  el.addEventListener("click", (e) => {
    const jl = e.target.closest(".jl");
    if (jl) { fieldsFor(jl.dataset.p)[0]?.scrollIntoView({ block: "center", behavior: "smooth" }); return; }
    const t = e.target.closest("tr[data-j]");
    if (t) reveal(linesFor(pathsOf(t))[0]);
  });
}
