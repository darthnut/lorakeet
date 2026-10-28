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

import anomalies
import corruption
import insights
import nodeids
import radioid
from config import CFG
from storage import notify

log = logging.getLogger("meshdash.alerts")

# Reception logging (rx_hops / tx_log) reads the firmware's debug log text. A firmware update that rewords it
# would stop that silently, so: over this window, debug lines kept arriving and packets kept arriving over the
# air, yet not one reception line was recognised -> alert. (A radio on Wi-Fi sends no debug log: no alert.)
MINING_WINDOW_S = 30 * 60
NEW_PENDING_S = 24 * 3600   # a one-off undecryptable newcomer never heard again: not alerted
NEW_BURST_WINDOW_S = 3600   # newcomers at one station within this window count as one burst
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
    "newStationQuietH": 48,        # a station that just started logging or moved: its newcomers roll into one alert
    "newNodeBurst": 5,             # this many newcomers at one station within an hour: one alert for all of them
    "notifyHealth": True,          # daily health report: notify on NEW warnings
    "autoTraceroute": CFG["alerts"]["auto_traceroute"],  # transmits: off for new installs unless configured
    "autoTracerouteMin": 60,       # at most one scheduled traceroute per this many minutes
    "autoTracerouteMaxChUtil": 25, # skip while our radio measures the channel busier than this (%)
    "autoTracerouteRetryHours": 72,  # don't retry a node that didn't answer for this long
    "autoTracerouteRefreshHours": 24,  # re-trace a node at most this often
    "stationSilenceMin": 30,       # hub: a collector station that hasn't synced this long
    "notifyStation": True,
    "anomalyAlerts": True,         # a steady radio behaving unlike itself (anomalies.py): alert on warnings
    "notifyAnomalies": True,       # ... and notify
}
ANOMALY_EVERY_S = 15 * 60
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
        self.pairings = None  # pairing.Pairings (server.py): a station whose pairing was revoked isn't watched
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
                    if s["anomalyAlerts"] and time.time() - self.state.get("anomalyRun", 0) >= ANOMALY_EVERY_S:
                        self.state["anomalyRun"] = time.time()
                        self._check_anomalies(s)
                    if time.time() - last_health > 86400:
                        self._daily_health(s)
                        last_health = time.time()
                    self._auto_traceroute(s)
                else:
                    self.connected_since = None
            except Exception:  # noqa: BLE001 - this loop must never die
                log.exception("alerts loop error")
            time.sleep(60)

    def _watched(self, s):
        """Watched radios as they are numbered now: a radio renumbered by firmware 2.8 is watched under its new
        number, so its old, silent one can't raise a "gone quiet" alert (nodeids.py)."""
        return nodeids.follow(s["watched"], nodeids.info(self.db_path)["aliases"])

    def _last_heard(self, nid):
        r = self.store.query("SELECT MAX(t) AS t FROM (SELECT MAX(ts) AS t FROM packets WHERE from_id = ? "
                             "UNION ALL SELECT MAX(ts) FROM rx_hops WHERE from_id = ?)", nid, nid)
        return r[0]["t"] if r else None

    @staticmethod
    def _silence_limit_s(s, nid):
        # a MeshCore radio is only ever named by its adverts, which most send every 12-24 h: anything shorter than a
        # day would call it silent between two of them
        return max(s["silenceHours"], 26 if radioid.is_meshcore(nid) else 0) * 3600

    def _check_silence(self, s):
        open_ = self.state.setdefault("silent", {})
        for nid in self._watched(s):
            limit = self._silence_limit_s(s, nid)
            heard = self._last_heard(nid) or 0
            name = self.mesh.describe(nid)["name"]
            listening_since = max(heard, self.connected_since or time.time())
            quiet = time.time() - listening_since
            if nid not in open_ and quiet >= limit:
                hrs = (time.time() - heard) / 3600 if heard else None
                open_[nid] = self.raise_alert(
                    "silent", f"{name} has gone quiet",
                    f"Not heard for {limit / 3600:g} h while the dashboard was listening"
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
        setting = lambda k: json.loads((self.store.query("SELECT value FROM settings WHERE key=?", k)  # noqa: E731
                                        or [{"value": "{}"}])[0]["value"])
        stations, owners, over, meta = (setting(k) for k in ("sync_hub", "station_owners", "station_overrides",
                                                             "station_meta"))
        if "stationSilent" not in self.state:
            # alerts still open from before a restart: kept, not raised again (each restart used to toast once more)
            self.state["stationSilent"] = {r["node"]: r["rowid"] for r in self.store.query(
                "SELECT node, MAX(rowid) AS rowid FROM alerts WHERE kind = 'station-silent' AND resolved_ts IS NULL "
                "GROUP BY node")}
        open_ = self.state["stationSilent"]
        limit = s["stationSilenceMin"] * 60
        for sid, st in stations.items():
            owner = owners.get(sid) or ""
            if self.pairings and owner.startswith("pair:") and self.pairings.is_revoked(owner):
                continue  # its pairing was revoked: it can't sync, and that's the point
            # the name this hub gave it, else the one it reports, else its radio's
            name = (over.get(sid) or {}).get("name") or (meta.get(sid) or {}).get("name") or self.mesh.describe(sid)["name"]
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

    def _check_anomalies(self, s):
        """Steady radios behaving unlike themselves (anomalies.py): one alert per warning while it lasts, resolved when
        it's over. The open ones (anomaly key -> alert row) are kept in settings 'anomaly_open', so a restart doesn't
        announce them again."""
        found = {a["key"]: a for a in anomalies.detect(self.db_path, "*", self.mesh.describe) if a["severity"] == "warn"}
        if "anomalyOpen" not in self.state:
            self.state["anomalyOpen"] = {k: r for k, r in (self._get("anomaly_open") or {}).items()
                                         if self.store.query("SELECT 1 FROM alerts WHERE rowid = ? AND resolved_ts IS NULL", r)}
        open_ = self.state["anomalyOpen"]
        for key, a in found.items():
            if key not in open_:
                open_[key] = self.raise_alert("anomaly", f"{a['node']['name']}: {a['title'].lower()}", a["detail"],
                                              a["node"]["id"], "warn", s["notifyAnomalies"])
        for key in [k for k in open_ if k not in found]:
            self.store.execute("UPDATE alerts SET resolved_ts = ? WHERE rowid = ?", time.time(), open_.pop(key))
        self._set("anomaly_open", open_)

    def _check_battery(self, s):
        latch = self.state.setdefault("lowbatt", set())
        for nid in self._watched(s):
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
        rows = self.store.query("SELECT from_id, MIN(ts) AS first FROM packets WHERE from_id IS NOT NULL GROUP BY from_id "
                                "HAVING first >= ?", since)   # (a MeshCore packet often names no sender)
        # A newcomer heard once, in a packet that won't decrypt, waits for a second packet (or a clean one): one damaged
        # copy of another radio's packet looks exactly like that (corruption.py). Phantoms never alert; a one-off
        # that's never heard again is dropped after NEW_PENDING_S.
        pending = st.setdefault("new_pending", {})
        for r in rows:
            pending.setdefault(r["from_id"], r["first"])
        if not pending:
            return self._toast_new(s)
        flagged = corruption.flags(self.db_path, max_age=0 if rows else corruption.CACHE_S)["senders"]   # fresh for newcomers
        ready = []
        for nid, first in list(pending.items()):
            r = self.store.query("SELECT COUNT(DISTINCT pkt_id) AS n, SUM(portnum != 'ENCRYPTED') AS clean FROM packets "
                                 "WHERE from_id = ?", nid)[0]
            n, clean = r["n"], r["clean"]
            if corruption.phantom(flagged.get(nid)):
                del pending[nid]
                continue
            if not (clean or (n or 0) >= 2 or not radioid.is_meshtastic(nid)):
                if time.time() - first > NEW_PENDING_S:
                    del pending[nid]
                continue
            del pending[nid]
            ready.append(nid)
        self._announce_new(s, ready)
        self._toast_new(s)

    def _warmup(self, s, station, now):
        """Why a station's newcomers are rolled up rather than announced one by one: (kind, since) for a station that
        started logging or moved (Wi-Fi switched to another named network) within newStationQuietH, else None."""
        quiet = s["newStationQuietH"] * 3600
        first = self.store.query("SELECT MIN(ts) AS t FROM packets WHERE station = ?", station)[0]["t"]
        if first and now - first < quiet:
            return "started", first
        nets = []
        for r in self.store.query("SELECT ts, detail FROM events WHERE station = ? AND kind = 'network' AND ts >= ? ORDER BY ts",
                                  station, now - quiet - 30 * 86400):
            try:
                net = json.loads(r["detail"] or "{}").get("network")
            except ValueError:
                net = None
            if net and net != "no network" and (not nets or nets[-1][1] != net):
                nets.append((r["ts"], net))
        if len(nets) >= 2 and now - nets[-1][0] < quiet:
            return "moved", nets[-1][0]
        return None

    def _announce_new(self, s, ready):
        """One alert per newcomer, except at a station that just started or moved (the quiet period) or during a burst
        (newNodeBurst within an hour): those roll into one alert per station, updated as more arrive. Rolled-up alerts
        are kept in settings 'new_digest' (station -> {rowid, ids, names, last, why}) so a restart continues them."""
        st, now = self.state, time.time()
        if "newDigest" not in st:
            st["newDigest"] = {k: d for k, d in (self._get("new_digest") or {}).items()
                               if self.store.query("SELECT 1 FROM alerts WHERE rowid = ?", d.get("rowid"))}
        digests, recent = st["newDigest"], st.setdefault("new_recent", {})
        by_station = {}
        for nid in ready:
            r = self.store.query("SELECT station FROM packets WHERE from_id = ? ORDER BY ts LIMIT 1", nid)
            by_station.setdefault((r[0]["station"] if r else None) or "", []).append(nid)
        changed = False
        for station, ids in by_station.items():
            recent[station] = [t for t in recent.get(station, []) if now - t < NEW_BURST_WINDOW_S]
            why = self._warmup(s, station, now) if station else None
            d = digests.get(station)
            # still open: the same quiet period (started/moved) it was opened for, or a burst with a newcomer in the last hour
            open_ = bool(d) and ((why is not None and d["why"] == why[0] and d["since"] == why[1])
                                 or (d["why"] == "burst" and now - d["last"] < NEW_BURST_WINDOW_S))
            if not (why or open_ or len(ids) + len(recent[station]) >= s["newNodeBurst"]):
                for nid in ids:
                    name = self.mesh.describe(nid)["name"]
                    self.raise_alert("new-node", f"New node: {name}", "First time this node has been heard.", nid, "info")
                    st.setdefault("new_batch", []).append(name)
                    recent[station].append(now)
                continue
            kind = why[0] if why else "burst"
            if not open_:
                d = digests[station] = {"rowid": None, "ids": [], "names": [], "last": now, "why": kind,
                                        "since": why[1] if why else now}
            ids = [n for n in ids if n not in d["ids"]]    # never counted twice
            if not ids:
                continue
            d["ids"] += ids
            d["names"] += [self.mesh.describe(n)["name"] for n in ids]
            d["last"] = now
            self._write_digest(s, station, d, toast=d["rowid"] is None and kind == "burst")
            changed = True
        if changed:
            self._set("new_digest", digests)

    def _write_digest(self, s, station, d, toast):
        sname = self.mesh.describe(station)["name"]
        n = len(d["ids"])
        title = f"{n} new radio{'s' if n != 1 else ''} heard by {sname}"
        when = time.strftime("%b %d %H:%M", time.localtime(d["since"]))
        why = {"started": f"{sname} started logging on {when}: the radios it hears in its first "
                          f"{s['newStationQuietH']:g} hours are rolled into this one alert.",
               "moved": f"{sname} moved (its Wi-Fi changed to another network on {when}): the radios it hears in its "
                        f"first {s['newStationQuietH']:g} hours there are rolled into this one alert.",
               "burst": f"{sname} heard a burst of radios it had never heard (from {when}), rolled into this one alert."}[d["why"]]
        names = ", ".join(d["names"][:40]) + (f" and {n - 40} more" if n > 40 else "")
        detail = f"{why} {names}."
        if d["rowid"] is None:
            d["rowid"] = self.raise_alert("new-nodes", title, detail, station, "info", toast and s["notifyNewNodes"])
            return
        self.store.execute("UPDATE alerts SET title = ?, detail = ?, ts = ?, read = 0 WHERE rowid = ?",
                           title, detail, time.time(), d["rowid"])
        self.mesh.broadcast("alert", {"rowid": d["rowid"], "ts": time.time(), "kind": "new-nodes", "node": station,
                                      "title": title, "detail": detail, "severity": "info", "read": 0})

    def _toast_new(self, s):
        st = self.state
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
        # our radio's once-a-minute deviceMetrics: localStats carries it only every ~15 min (as insights.health)
        cu = self.store.query("SELECT json_extract(data, '$.channelUtilization') AS cu FROM telemetry_full "
                              "WHERE kind='deviceMetrics' AND node = ? AND ts > ? ORDER BY ts DESC LIMIT 1",
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
            if not radioid.is_meshtastic(r["from_id"]):
                continue  # a MeshCore radio (a MeshCore station's rows, synced here): a Meshtastic traceroute can't reach it
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
