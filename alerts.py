"""Alerts, settings, and scheduled traceroutes.

Alerts are evaluated every minute while the radio is connected. Silence is only counted while we
were listening (from max(last heard, start of the current connection)), so a PC restart never
raises a false "node went quiet". Every alert is stored (alerts table); some also raise a Windows
notification. Scheduled traceroutes are the one thing here that transmits.
"""
import json
import logging
import threading
import time
from collections import deque

import insights
from config import CFG
from storage import notify

log = logging.getLogger("meshdash.alerts")

# Reception logging (rx_hops / tx_log) reads the firmware's debug log text. A firmware update that rewords it
# would stop that silently, so: over this window, debug lines kept arriving and packets kept arriving over the
# air, yet not one reception line was recognised -> alert. (A radio on Wi-Fi sends no debug log: no alert.)
MINING_WINDOW_S = 30 * 60
MINING_MIN_LINES = 50
MINING_MIN_PACKETS = 3


def mining_stalled(lines, mined, lora_packets):
    """True when the debug log is flowing and packets are arriving, but no RX/TX line was parsed."""
    return lines >= MINING_MIN_LINES and lora_packets >= MINING_MIN_PACKETS and mined == 0

DEFAULTS = {
    "watched": [],                 # node ids; the base station is added on first run
    "silenceHours": 2.0,           # a watched node not heard this long (while we were listening)
    "lowBatteryPct": 30,           # watched node battery at or below (ignores >100 = external power)
    "notifySilence": True,
    "notifyBack": True,            # watched node heard again after a silence alert
    "notifyLowBattery": True,
    "notifyNewNodes": True,        # batched: at most one notification per newNodeBatchMin
    "newNodeBatchMin": 15,
    "notifyHealth": True,          # daily health report: notify on NEW warnings
    "autoTraceroute": CFG["alerts"]["auto_traceroute"],  # transmits: off for new installs unless configured
    "autoTracerouteMin": 60,       # at most one scheduled traceroute per this many minutes
    "autoTracerouteMaxChUtil": 25, # skip while our radio measures the channel busier than this (%)
    "autoTracerouteRetryHours": 72,  # don't retry a node that didn't answer for this long
    "autoTracerouteRefreshHours": 24,  # re-trace a node at most this often
    "stationSilenceMin": 30,       # hub: a collector station that hasn't synced this long
    "notifyStation": True,
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS alerts (ts REAL, kind TEXT, node TEXT, title TEXT, detail TEXT,
  severity TEXT, read INTEGER DEFAULT 0, resolved_ts REAL);
CREATE INDEX IF NOT EXISTS alerts_ts ON alerts(ts);
"""


class Alerts:
    def __init__(self, store, mesh, db_path, base_id):
        self.store, self.mesh, self.db_path, self.base_id = store, mesh, db_path, base_id
        with store.lock:
            store.db.executescript(SCHEMA)
            store.db.commit()
        self.state = {}  # in-memory: open silence alerts, low-battery latch, new-node batch, timers
        self.lock = threading.Lock()
        s = self.settings()
        if base_id and not s["watched"] and not self._get("initialized"):
            self.update_settings({"watched": [base_id]})
        self._set("initialized", True)
        self.connected_since = None
        self.last_auto = {"ts": None, "target": None, "reason": None}

    # ---------------------------------------------------------- settings
    def _get(self, key):
        r = self.store.query("SELECT value FROM settings WHERE key = ?", key)
        return json.loads(r[0]["value"]) if r else None

    def _set(self, key, value):
        self.store.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", key, json.dumps(value))

    def settings(self):
        stored = self._get("alerts") or {}
        return {**DEFAULTS, **{k: v for k, v in stored.items() if k in DEFAULTS}}

    def update_settings(self, patch):
        cur = self.settings()
        for k, v in patch.items():
            if k not in DEFAULTS:
                raise ValueError(f"unknown setting {k}")
            want = type(DEFAULTS[k])
            if want is float and isinstance(v, int):
                v = float(v)
            if not isinstance(v, want) or (want is int and isinstance(v, bool)):
                raise ValueError(f"{k} must be {want.__name__}")
            cur[k] = v
        self._set("alerts", cur)
        return cur

    def set_watched(self, nid, watched):
        w = [x for x in self.settings()["watched"] if x != nid]
        if watched:
            w.append(nid)
        return self.update_settings({"watched": w})

    # ---------------------------------------------------------- alert records
    def raise_alert(self, kind, title, detail, node=None, severity="warn", toast=False):
        rowid = self.store.insert("alerts", ts=time.time(), kind=kind, node=node, title=title, detail=detail,
                                  severity=severity, read=0, resolved_ts=None)
        self.mesh.event(f"alert:{kind}", node=node, title=title)
        a = {"rowid": rowid, "ts": time.time(), "kind": kind, "node": node, "title": title, "detail": detail,
             "severity": severity, "read": 0}
        self.mesh.broadcast("alert", a)
        if toast:
            threading.Thread(target=notify, args=(title, detail), daemon=True).start()
        return rowid

    def recent(self, limit=100):
        rows = self.store.query("SELECT rowid, * FROM alerts ORDER BY ts DESC LIMIT ?", limit)
        unread = self.store.query("SELECT COUNT(*) AS n FROM alerts WHERE read = 0")[0]["n"]
        return {"alerts": rows, "unread": unread}

    def mark_read(self):
        self.store.execute("UPDATE alerts SET read = 1 WHERE read = 0")
        self.mesh.broadcast("alert", {"read": True})

    # ---------------------------------------------------------- loop
    def start(self):
        threading.Thread(target=self._loop, daemon=True, name="alerts").start()

    def _loop(self):
        time.sleep(90)
        last_health = 0
        while True:
            try:
                if self.mesh.connected:
                    if self.connected_since is None:
                        self.connected_since = time.time()
                    s = self.settings()
                    self._check_silence(s)
                    self._check_battery(s)
                    self._check_new_nodes(s)
                    self._check_stations(s)
                    self._check_mining()
                    if time.time() - last_health > 86400:
                        self._daily_health(s)
                        last_health = time.time()
                    self._auto_traceroute(s)
                else:
                    self.connected_since = None
            except Exception:  # noqa: BLE001 - this loop must never die
                log.exception("alerts loop error")
            time.sleep(60)

    def _last_heard(self, nid):
        r = self.store.query("SELECT MAX(t) AS t FROM (SELECT MAX(ts) AS t FROM packets WHERE from_id = ? "
                             "UNION ALL SELECT MAX(ts) FROM rx_hops WHERE from_id = ?)", nid, nid)
        return r[0]["t"] if r else None

    def _check_silence(self, s):
        limit = s["silenceHours"] * 3600
        open_ = self.state.setdefault("silent", {})
        for nid in s["watched"]:
            heard = self._last_heard(nid) or 0
            name = self.mesh.describe(nid)["name"]
            listening_since = max(heard, self.connected_since or time.time())
            quiet = time.time() - listening_since
            if nid not in open_ and quiet >= limit:
                hrs = (time.time() - heard) / 3600 if heard else None
                open_[nid] = self.raise_alert(
                    "silent", f"{name} has gone quiet",
                    f"Not heard for {s['silenceHours']:g} h while the dashboard was listening"
                    + (f" (last heard {hrs:.1f} h ago)." if hrs else "."), nid, "warn", s["notifySilence"])
            elif nid in open_ and heard > time.time() - 120:
                rowid = open_.pop(nid)
                self.store.execute("UPDATE alerts SET resolved_ts = ? WHERE rowid = ?", time.time(), rowid)
                self.raise_alert("back", f"{name} is back", "Heard again after a silence alert.", nid, "info",
                                 s["notifyBack"])

    def _check_mining(self):
        c, now = self.mesh.log_counts, time.time()
        hist = self.state.setdefault("miningHist", deque())
        hist.append((now, c["lines"], c["mined"]))
        while len(hist) > 1 and now - hist[1][0] >= MINING_WINDOW_S:
            hist.popleft()
        t0, lines0, mined0 = hist[0]
        if now - t0 < MINING_WINDOW_S - 90:
            return  # not a full window of evidence yet
        lines, mined = c["lines"] - lines0, c["mined"] - mined0
        lora = self.store.query(
            "SELECT COUNT(*) AS n FROM packets WHERE station = ? AND ts >= ? AND from_id != ? "
            "AND json_extract(raw, '$.transportMechanism') LIKE 'TRANSPORT_LORA%'",
            self.mesh.local_id, t0, self.mesh.local_id)[0]["n"]
        open_ = self.state.get("miningAlert")
        if open_ is None and mining_stalled(lines, mined, lora):
            self.state["miningAlert"] = self.raise_alert(
                "logging", "Reception logging has stopped",
                f"In the last {MINING_WINDOW_S // 60} minutes the radio sent {lines} debug-log lines and {lora} packets "
                "arrived over the air, but no reception line was recognised, so per-reception data (hops, relays, "
                "duplicates, airtime) isn't being recorded. Most likely a firmware update changed the log format: "
                "check server.py parse_rx against the radio's debug log.", None, "warn", True)
        elif open_ is not None and mined > 0:
            self.store.execute("UPDATE alerts SET resolved_ts = ? WHERE rowid = ?", now, self.state.pop("miningAlert"))
            self.raise_alert("logging", "Reception logging is working again", "Reception lines are being recognised again.", None, "info")

    def _check_stations(self, s):
        """Hub only: a collector station that stopped syncing (its power, Wi-Fi or Tailscale is down,
        or this PC can't be reached). Raised once, resolved with a 'back' alert when batches resume."""
        if CFG["sync"]["mode"] != "hub":
            return
        r = self.store.query("SELECT value FROM settings WHERE key='sync_hub'")
        stations = json.loads(r[0]["value"]) if r else {}
        open_ = self.state.setdefault("stationSilent", {})
        limit = s["stationSilenceMin"] * 60
        for sid, st in stations.items():
            name = self.mesh.describe(sid)["name"]
            quiet = time.time() - st.get("last", 0)
            if sid not in open_ and quiet >= limit:
                open_[sid] = self.raise_alert(
                    "station-silent", f"Station {name} isn't syncing",
                    f"No data from this listening station for {quiet / 60:.0f} min. Its power, Wi-Fi or "
                    "Tailscale may be down; it keeps logging locally and catches up when it reconnects.",
                    sid, "warn", s["notifyStation"])
            elif sid in open_ and quiet < limit:
                rowid = open_.pop(sid)
                self.store.execute("UPDATE alerts SET resolved_ts = ? WHERE rowid = ?", time.time(), rowid)
                self.raise_alert("station-back", f"Station {name} is syncing again",
                                 "Batches are arriving again; anything logged meanwhile is being caught up.",
                                 sid, "info", s["notifyStation"])

    def _check_battery(self, s):
        latch = self.state.setdefault("lowbatt", set())
        for nid in s["watched"]:
            r = self.store.query("SELECT battery, voltage, ts FROM telemetry WHERE node = ? AND battery IS NOT NULL "
                                 "ORDER BY ts DESC LIMIT 1", nid)
            if not r or r[0]["battery"] > 100:
                continue
            b = r[0]["battery"]
            name = self.mesh.describe(nid)["name"]
            if b <= s["lowBatteryPct"] and nid not in latch:
                latch.add(nid)
                v = f", {r[0]['voltage']:.2f} V" if r[0]["voltage"] else ""
                self.raise_alert("low-battery", f"{name} battery low", f"Reports {b:.0f}%{v}.", nid, "warn",
                                 s["notifyLowBattery"])
            elif b > s["lowBatteryPct"] + 5:
                latch.discard(nid)  # hysteresis: re-arm only after a real recovery

    def _check_new_nodes(self, s):
        st = self.state
        since = st.get("new_since") or time.time() - 120
        st["new_since"] = time.time()
        rows = self.store.query("SELECT from_id, MIN(ts) AS first FROM packets GROUP BY from_id HAVING first >= ?", since)
        for r in rows:
            name = self.mesh.describe(r["from_id"])["name"]
            self.raise_alert("new-node", f"New node: {name}", "First time this node has been heard.", r["from_id"], "info")
            st.setdefault("new_batch", []).append(name)
        batch = st.get("new_batch") or []
        if batch and s["notifyNewNodes"] and time.time() - st.get("new_toast", 0) >= s["newNodeBatchMin"] * 60:
            notify(f"{len(batch)} new mesh node{'s' if len(batch) > 1 else ''}",
                   ", ".join(batch[:6]) + (f" and {len(batch) - 6} more" if len(batch) > 6 else ""))
            st["new_toast"], st["new_batch"] = time.time(), []

    def _daily_health(self, s):
        rep = insights.health(self.db_path, "7d", self.mesh.local_id, self.mesh.describe)
        keys = {f["key"] for f in rep["findings"] if f["severity"] == "warn"}
        prev = set(self._get("health_warn_keys") or [])
        new = [f for f in rep["findings"] if f["severity"] == "warn" and f["key"] not in prev]
        self._set("health_warn_keys", sorted(keys))
        self._set("health_last_run", time.time())
        if new and prev is not None and self._get("health_seeded"):
            titles = "; ".join(f"{f['title']}" + (f" ({f['node']['name']})" if f.get("node") else "") for f in new[:4])
            self.raise_alert("health", f"{len(new)} new mesh health warning{'s' if len(new) > 1 else ''}", titles,
                             None, "warn", s["notifyHealth"])
        self._set("health_seeded", True)  # first run only records a baseline, no alert storm

    # ---------------------------------------------------------- scheduled traceroutes
    def _auto_traceroute(self, s):
        if not s["autoTraceroute"]:
            return
        last = self._get("auto_tr_last") or 0
        if time.time() - last < s["autoTracerouteMin"] * 60:
            return
        cu = self.store.query("SELECT json_extract(data, '$.channelUtilization') AS cu FROM telemetry_full "
                              "WHERE kind='localStats' AND node = ? AND ts > ? ORDER BY ts DESC LIMIT 1",
                              self.mesh.local_id or "", time.time() - 600)
        if cu and cu[0]["cu"] is not None and cu[0]["cu"] > s["autoTracerouteMaxChUtil"]:
            self.last_auto = {"ts": time.time(), "target": None, "reason": f"skipped: channel busy ({cu[0]['cu']:.1f}%)"}
            return
        target, why = self._pick_target(s)
        if not target:
            self.last_auto = {"ts": time.time(), "target": None, "reason": why}
            self._set("auto_tr_last", time.time())  # nothing to do; check again next interval
            return
        try:
            self.mesh.traceroute(target, origin="scheduled")
            self._set("auto_tr_last", time.time())
            self.last_auto = {"ts": time.time(), "target": target, "reason": why}
            log.info("scheduled traceroute to %s (%s)", target, why)
        except (ValueError, RuntimeError) as e:  # cooldown after a manual trace, radio unplugged...
            self.last_auto = {"ts": time.time(), "target": target, "reason": f"deferred: {e}"}

    def _pick_target(self, s):
        """The node whose path we understand least: heard in the last 24 h, never/long-ago traced, far away."""
        now = time.time()
        local = self.mesh.local_id or ""
        heard = self.store.query("""SELECT from_id, MAX(ts) AS last, MAX(hops) AS hops FROM packets
                                    WHERE ts > ? AND from_id != ? AND from_id NOT IN ('^all','!ffffffff')
                                    GROUP BY from_id""", now - 86400, local)
        traces = {}
        for r in self.store.query("SELECT target, ts, status FROM traceroutes ORDER BY ts"):
            traces[r["target"]] = r
        best, best_score, best_why = None, None, "no candidates heard in the last 24 h"
        for r in heard:
            t = traces.get(r["from_id"])
            if t:
                age_h = (now - t["ts"]) / 3600
                if t["status"] != "ok" and age_h < s["autoTracerouteRetryHours"]:
                    continue
                if t["status"] == "ok" and age_h < s["autoTracerouteRefreshHours"]:
                    continue
            score = (0 if not t else 1 if t["status"] != "ok" else 2, -(r["hops"] or 0), -r["last"])
            if best_score is None or score < best_score:
                best, best_score = r["from_id"], score
                best_why = ("never traced" if not t else "previous trace failed" if t["status"] != "ok"
                            else "path last traced over a day ago") + f", {r['hops'] if r['hops'] is not None else '?'} hops away"
        return best, best_why

    def status(self):
        s = self.settings()
        last = self._get("auto_tr_last")
        return {"settings": s, "autoTraceroute": {**self.last_auto, "lastRunTs": last,
                                                  "nextEarliestTs": (last or 0) + s["autoTracerouteMin"] * 60},
                "healthLastRun": self._get("health_last_run")}
