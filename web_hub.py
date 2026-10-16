"""Hub routes: ingest from stations and peers (/api/ingest*), and the Stations page (/api/hub*).
"""
import json
import logging
import sys
import threading
import time

import analytics
from config import CFG
import nodeids
import pairing
import radioid
import runtime
from station import _as_location, load_station_locations, station_overrides, supervised_now
import sync as sync_mod


log = logging.getLogger("meshdash")

INGEST_SLOTS = threading.BoundedSemaphore(4)
DRAIN_MAX = 16 * 1024 * 1024  # a refused station's batch is read and dropped up to this size (Handler._drain)


def make(mesh, store, storage, alert_engine, syncer, login, peering, pairings, hub_id, hub_name, station_meta, station_name):
    """The routes, closed over make_handler's state (web.make_handler mixes them into its Handler)."""
    class HubRoutes:
        def _ingest(self, hello=False):
            if not isinstance(syncer, sync_mod.Hub):
                return self._json({"error": "this Lorakeet isn't a hub"}, 404)
            ip = self.client_address[0]
            if not sync_mod.allowed_source(ip, CFG["sync"]["allow"]):
                log.warning("ingest refused from %s: not an allowed network", ip)
                return self._json({"error": "not from an allowed network"}, 403)
            claimed = self.headers.get("X-Lorakeet-Station") or ""
            if claimed and not radioid.is_radio_id(claimed):
                return self._json({"error": "bad station header"}, 400)
            given = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
            from_hub = (self.headers.get("X-Lorakeet-Hub") or "")[:64]
            hi = {"ok": True, "hub": hub_name(), "version": runtime.VERSION, "hubId": hub_id}
            e = pairings.find(given)
            if e is not None:  # made on the Stations page: a station's pairing, or a peer hub's
                if e["revoked"]:
                    log.warning("ingest refused from %s (%s): pairing revoked", ip, claimed)
                    return self._json({"error": "this pairing was revoked"}, 401)
                if e.get("kind") == "peer":
                    if e.get("hub") and from_hub and e["hub"] != from_hub:
                        log.warning("ingest refused from %s: peer pairing belongs to another hub", ip)
                        return self._json({"error": "this pairing code belongs to a different hub"}, 403)
                    if hello:
                        return self._json({**hi, "paused": True} if e.get("paused") else hi)
                    if not claimed or not from_hub:
                        return self._json({"error": "send X-Lorakeet-Station and X-Lorakeet-Hub"}, 401)
                    if not pairings.bind(e["id"], hub=from_hub):
                        return self._json({"error": "this pairing code belongs to a different hub"}, 403)
                    pairings.seen(e["id"])
                    if e.get("paused"):  # the sender keeps these pending (not acknowledged) and sends them on resume
                        return self._json({"error": "paused by the receiving hub (nothing is lost: it's sent when they "
                                                    "resume)"}, 423)
                    return self._ingest_body(ip, claimed, via=from_hub, writer=f"peer:{from_hub}")
                if e["station"] and claimed and e["station"] != claimed:
                    log.warning("ingest refused from %s (%s): pairing belongs to another station", ip, claimed)
                    return self._json({"error": "this pairing code belongs to a different station"}, 403)
                if hello:
                    return self._json(hi)
                if not claimed:
                    return self._json({"error": "send X-Lorakeet-Station"}, 401)
                writer = f"pair:{e['id']}"
                if e["station"] is None:  # first use: the station must be free for it before the code is claimed
                    try:
                        syncer.claim(claimed, writer)
                    except sync_mod.IngestError as err:
                        log.warning("ingest refused from %s (%s): %s", ip, claimed, err)
                        return self._json({"error": str(err)}, err.status)
                    if not pairings.bind(e["id"], station=claimed):
                        return self._json({"error": "this pairing code belongs to a different station"}, 403)
                return self._ingest_body(ip, claimed, writer=writer)
            tokens = sync_mod.follow_tokens(sync_mod.station_tokens(CFG["sync"]["station_tokens"]),
                                            nodeids.info(runtime.DATA_DIR / "mesh.db")["aliases"])
            bound = sync_mod.authorize(self.headers.get("Authorization"), claimed or None, CFG["sync"]["token"], tokens,
                                       CFG["sync"]["require_station_tokens"])
            if bound is None and claimed:
                log.warning("ingest refused from %s (%s): bad token", ip, claimed)
                return self._json({"error": "bad token"}, 401)
            if bound is None and not sync_mod.check_token(self.headers.get("Authorization"), CFG["sync"]["token"]):
                # an older collector that doesn't name itself: shared token only
                log.warning("ingest refused from %s: bad token", ip)
                return self._json({"error": "bad token"}, 401)
            if not claimed and (CFG["sync"]["require_station_tokens"] or sync_mod.station_tokens(CFG["sync"]["station_tokens"])):
                return self._json({"error": "send X-Lorakeet-Station (this hub uses per-station tokens)"}, 401)
            if hello:
                return self._json(hi)
            return self._ingest_body(ip, claimed, writer="token" if claimed in tokens else "shared")

        def _ingest_body(self, ip, claimed, via=None, writer="shared"):
            if not INGEST_SLOTS.acquire(timeout=30):  # a few batches at a time: each may decompress to 50 MB
                return self._json({"error": "busy, try again shortly"}, 503)
            try:
                n = self._length()
                if n > sync_mod.MAX_BODY:
                    raise sync_mod.IngestError(413, "batch too large")
                body, self._body_read = self.rfile.read(n), True
                batch = sync_mod.decode_body(body, self.headers.get("Content-Encoding"))
                return self._json(syncer.ingest(batch, bound_station=claimed or None, via=via, writer=writer))
            except sync_mod.IngestError as e:
                log.warning("ingest from %s refused: %s", ip, e)
                return self._json({"error": str(e)}, e.status)
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            except Exception as e:  # noqa: BLE001
                log.exception("ingest failed")
                return self._json({"error": f"ingest failed: {e}"}, 500)
            finally:
                INGEST_SLOTS.release()

        def _drain(self, cap=DRAIN_MAX):
            """After refusing a request without reading its body: read the rest (up to `cap`) before the connection
            closes. Closing with unread data resets the connection (Windows: WinError 10053), so the sender saw
            "unreachable" instead of the refusal."""
            if self._body_read:
                return
            try:
                n = self._length()
            except ValueError:
                n = cap + 1
            if n > cap:
                self.close_connection = True
                return
            while n > 0:
                chunk = self.rfile.read(min(n, 65536))
                if not chunk:
                    break
                n -= len(chunk)

        # ---- the Stations page (pairing.py): hub mode, pairing, joining a hub, managing stations
        def _hub_info(self):
            import config as cfgmod
            try:
                saved = cfgmod.load()["sync"]
            except Exception:  # noqa: BLE001
                saved = CFG["sync"]
            port = CFG["http"]["port"]
            out = {"mode": CFG["sync"]["mode"], "savedMode": saved["mode"],
                   "pendingRestart": (saved["mode"], saved["allow"], saved["hub_url"]) !=
                                     (CFG["sync"]["mode"], CFG["sync"]["allow"], CFG["sync"]["hub_url"]),
                   "supervised": supervised_now(), "port": port, "hubName": hub_name(), "windows": sys.platform == "win32",
                   "addresses": pairing.hub_addresses(port), "allow": pairing.allow_flags(saved["allow"]),
                   "sharedToken": bool(CFG["sync"]["token"]), "pairings": pairings.list(), "stations": self._hub_stations(),
                   "hubId": hub_id, "peersOut": peering.list() if peering else [], "shareLevels": list(sync_mod.SHARE_LEVELS)}
            if isinstance(syncer, sync_mod.Collector):
                out["collector"] = {**syncer.status, "hubUrl": CFG["sync"]["hub_url"]}
            return out

        def _hub_stations(self):
            """Every station this hub knows: ones with logged data, ones that reported, ones paired."""
            metas, over = station_meta(), station_overrides(store)
            hubst = syncer.status()["stations"] if isinstance(syncer, sync_mod.Hub) else {}
            plist = pairings.list()
            ids = {r["id"] for r in analytics.stations(runtime.DATA_DIR / "mesh.db")} | set(metas) | {e["station"] for e in plist if e["station"]}
            # forgotten stations stay out of the list (their logged data stays in Analytics) until they send again
            gone = json.loads((store.query("SELECT value FROM settings WHERE key='stations_forgotten'")
                               or [{"value": "{}"}])[0]["value"])
            ids = {i for i in ids if i not in gone or i in metas or i in hubst}
            rows = []
            for sid in sorted(ids, key=lambda x: (x != mesh.local_id, x)):
                m, p = metas.get(sid) or {}, next((e for e in plist if e["station"] == sid), None)
                loc = analytics.station_location(sid)
                via = (hubst.get(sid) or {}).get("via")
                vp = pairings.by_hub(via) if via else None
                rows.append({"id": sid, "name": station_name(sid), "here": sid == mesh.local_id,
                             "via": via and {"hub": via, "label": vp["label"] if vp else "a peer hub"},
                             "pairing": p and {"id": p["id"], "label": p["label"], "revoked": p["revoked"]},
                             "sharedToken": sid != mesh.local_id and not p and not via and bool(m or hubst.get(sid)),
                             "lastContact": (hubst.get(sid) or {}).get("last") or m.get("received"),
                             "version": m.get("version"), "backlog": m.get("backlog"), "mobile": m.get("mobile"),
                             "location": list(loc) if loc else None, "override": over.get(sid) or {}})
            return rows

        def _set_override(self, sid, **kw):
            with store.lock:
                r = store.db.execute("SELECT value FROM settings WHERE key='station_overrides'").fetchone()
                allo = json.loads(r[0]) if r else {}
                o = {**allo.get(sid, {}), **kw}
                o = {k: v for k, v in o.items() if v}
                if o:
                    allo[sid] = o
                else:
                    allo.pop(sid, None)
                store.db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('station_overrides', ?)", (json.dumps(allo),))
                store.db.commit()

        def _hub_post(self, path, body):
            import config as cfgmod
            try:
                if path == "/api/hub/mode":
                    mode = body.get("mode")
                    if mode == "hub":
                        nets = pairing.allow_networks(bool(body.get("lan")), bool(body.get("tailscale")))
                        if not nets:
                            raise ValueError("allow at least one kind of network, or stations can't reach this hub")
                        cfgmod.update_section("sync", {"mode": "hub", "allow": nets})
                    elif mode == "off":
                        cfgmod.update_section("sync", {"mode": "off"})
                    else:
                        raise ValueError("mode must be hub or off")
                    log.info("stations: sync mode set to %s (restart to apply)", mode)
                    return self._json({"ok": True, **self._hub_info()})
                if path == "/api/hub/pair":
                    if CFG["sync"]["mode"] != "hub":
                        raise ValueError("turn on hub mode first (and restart Lorakeet)")
                    allow = pairing.allow_flags(CFG["sync"]["allow"])
                    urls = [a["url"] for a in pairing.hub_addresses(CFG["http"]["port"]) if allow.get(a["kind"])]
                    if not urls:
                        raise ValueError("this computer has no address in the networks this hub allows")
                    e, token = pairings.create(body.get("label"))
                    log.info("stations: pairing code made for %r (%s)", e["label"], e["id"])
                    return self._json({"ok": True, "id": e["id"], "label": e["label"], "addresses": urls,
                                       "code": pairing.make_code(urls, token, hub_name())})
                if path == "/api/hub/station":
                    act, sid, pid = body.get("action"), body.get("station"), body.get("pairing")
                    if sid and not radioid.is_radio_id(sid):
                        raise ValueError("bad station id")
                    if act == "rename":
                        self._set_override(sid, name=str(body.get("name") or "").strip()[:60] or None)
                    elif act == "locate":
                        loc = body.get("location") or []
                        if loc and not (len(loc) == 2 and -90 <= float(loc[0]) <= 90 and -180 <= float(loc[1]) <= 180):
                            raise ValueError("latitude -90..90 and longitude -180..180, please")
                        loc = [round(float(x), 6) for x in loc]
                        self._set_override(sid, location=loc or None)
                        if loc:
                            analytics.STATION_LOCATIONS[sid] = _as_location(loc)
                        else:
                            analytics.STATION_LOCATIONS.pop(sid, None)
                            load_station_locations(store, mesh.local_id)
                    elif act == "relabel":
                        if not pairings.relabel(pid, str(body.get("label") or "")):
                            raise ValueError("no such pairing")
                        log.info("stations: pairing %s renamed", pid)
                    elif act == "revoke":
                        if not pairings.revoke(pid):
                            raise ValueError("no such pairing (or already revoked)")
                        log.info("stations: pairing %s revoked", pid)
                    elif act in ("pause", "resume"):  # a peer hub sending here: decline its batches for now
                        if not pairings.set_paused(pid, act == "pause"):
                            raise ValueError("no such peer (or it was revoked)")
                        log.info("peering: receiving from %s %s", pid, "paused" if act == "pause" else "resumed")
                    elif act == "forget":
                        if sid == mesh.local_id:
                            raise ValueError("that's this computer's own radio")
                        with store.lock:
                            for key in ("station_meta", "sync_hub", "station_overrides"):
                                r = store.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
                                if r:
                                    d = json.loads(r[0])
                                    d.pop(sid, None)
                                    store.db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, json.dumps(d)))
                            store.db.commit()
                        if sid:
                            with store.lock:   # remembered, so the list leaves it out (logged data keeps it there)
                                r = store.db.execute("SELECT value FROM settings WHERE key='stations_forgotten'").fetchone()
                                gone = {**(json.loads(r[0]) if r else {}), sid: time.time()}
                                store.db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('stations_forgotten', ?)",
                                                 (json.dumps(gone),))
                                store.db.commit()
                        pairings.forget(pid=pid, station=sid)
                        analytics.STATION_LOCATIONS.pop(sid, None)
                        mesh.station_ids.discard(sid)
                        log.info("stations: forgot %s (its logged data stays)", sid or pid)
                    else:
                        raise ValueError("unknown action")
                    return self._json({"ok": True, **self._hub_info()})
                if path == "/api/hub/peer-code":  # let another hub send to this one
                    if CFG["sync"]["mode"] != "hub":
                        raise ValueError("turn on hub mode first (and restart Lorakeet)")
                    allow = pairing.allow_flags(CFG["sync"]["allow"])
                    urls = [a["url"] for a in pairing.hub_addresses(CFG["http"]["port"]) if allow.get(a["kind"])]
                    if not urls:
                        raise ValueError("this computer has no address in the networks this hub allows")
                    e, token = pairings.create(body.get("label"), kind="peer")
                    log.info("peering: peer code made for %r (%s)", e["label"], e["id"])
                    return self._json({"ok": True, "id": e["id"], "label": e["label"], "addresses": urls,
                                       "code": pairing.make_code(urls, token, hub_name(), kind="peer", hub_id=hub_id)})
                if path == "/api/hub/peer-add":  # send this hub's log to another hub
                    if peering is None:
                        raise ValueError("peering isn't available on this install")
                    c = pairing.parse_code(body.get("code"))
                    if c["kind"] != "peer":
                        raise ValueError("that's a station pairing code: for peering, the other hub makes a code with "
                                         "'Let another hub send here'")
                    if c["hubId"] and c["hubId"] == hub_id:
                        raise ValueError("that code is from this hub itself")
                    share = body.get("share") or "default"
                    if share not in sync_mod.SHARE_LEVELS:
                        raise ValueError("unknown share level")
                    result, tried = pairing.test_hubs(c["hubs"], c["token"], hub_id=hub_id)
                    if not result["ok"] and not body.get("anyway"):
                        return self._json({**result, "tried": tried, "saved": False}, 409)
                    peer = peering.add(c["name"] or result.get("hubName") or "Peer hub", result["url"] if result["ok"] else c["hubs"][0],
                                       c["token"], share, bool(body.get("forward")), c["hubId"] or result.get("hubId") or "",
                                       exact=bool(body.get("exact")))
                    log.info("peering: now sending to %s (%s, share=%s, forward=%s)", peer["name"], peer["url"], share, peer["forward"])
                    return self._json({**result, "saved": True, **self._hub_info()})
                if path == "/api/hub/peer-forget":  # delete everything a peer hub sent here (and stop it sending)
                    e = next((x for x in pairings.list() if x["id"] == body.get("pairing") and x.get("kind") == "peer"), None)
                    if e is None:
                        raise ValueError("no such peer")
                    if not isinstance(syncer, sync_mod.Hub):
                        raise ValueError("this computer isn't a hub")
                    writer = f"peer:{e['hub']}" if e.get("hub") else None
                    counts = syncer.owned_by(writer) if writer else {}
                    if body.get("dryRun"):
                        return self._json({"ok": True, "stations": [{"id": st, "name": station_name(st), "rows": n}
                                                                    for st, n in counts.items()],
                                           "rows": sum(counts.values()), "revoked": bool(e["revoked"])})
                    if not e["revoked"]:
                        pairings.revoke(e["id"])
                    deleted = syncer.delete_from(writer) if writer else {}
                    for st in deleted:
                        analytics.STATION_LOCATIONS.pop(st, None)
                        mesh.station_ids.discard(st)
                    log.info("peering: deleted %d rows from %d stations sent by %r (pairing %s, revoked)",
                             sum(deleted.values()), len(deleted), e["label"], e["id"])
                    mesh.event("peer_data_deleted", pairing=e["id"], label=e["label"], stations=list(deleted),
                               rows=sum(deleted.values()))
                    return self._json({**self._hub_info(), "ok": True, "deleted": sum(deleted.values()), "deletedStations": len(deleted)})
                if path == "/api/hub/peer":
                    if peering is None:
                        raise ValueError("peering isn't available on this install")
                    act, pid = body.get("action"), body.get("id")
                    if act == "set":
                        kw = {}
                        if "share" in body:
                            if body["share"] not in sync_mod.SHARE_LEVELS:
                                raise ValueError("unknown share level")
                            kw["share"] = body["share"]
                        if "forward" in body:
                            kw["forward"] = bool(body["forward"])
                        if "exact" in body:
                            kw["exact"] = bool(body["exact"])
                        if "paused" in body:
                            kw["paused"] = bool(body["paused"])
                            log.info("peering: sending to %s %s", pid, "paused" if kw["paused"] else "resumed")
                        if not peering.update(pid, **kw):
                            raise ValueError("no such peer")
                    elif act == "remove":
                        peering.remove(pid)
                        log.info("peering: stopped sending to %s", pid)
                    elif act == "sync":
                        if pid in peering.senders:
                            peering.senders[pid].sync_now()
                    elif act == "test":
                        peer = peering.peers.get(pid)
                        if not peer:
                            raise ValueError("no such peer")
                        result, tried = pairing.test_hubs([peer["url"]], peer["token"], hub_id=hub_id)
                        return self._json({**result, "tried": tried})
                    else:
                        raise ValueError("unknown action")
                    return self._json({"ok": True, **self._hub_info()})
                if path == "/api/hub/test":
                    if body.get("code"):
                        c = pairing.parse_code(body["code"])
                        urls, token = c["hubs"], c["token"]
                    else:
                        if not CFG["sync"]["hub_url"]:
                            raise ValueError("this station isn't set up to send to a hub")
                        urls, token = [CFG["sync"]["hub_url"]], CFG["sync"]["token"]
                    result, tried = pairing.test_hubs(urls, token, mesh.local_id)
                    return self._json({**result, "tried": tried})
                if path == "/api/hub/join":
                    c = pairing.parse_code(body.get("code"))
                    if c["kind"] == "peer":
                        raise ValueError("that's a peer code (for another hub to share with it): use Peers → Send to another hub")
                    result, tried = pairing.test_hubs(c["hubs"], c["token"], mesh.local_id)
                    if not result["ok"] and not body.get("anyway"):
                        return self._json({**result, "tried": tried, "saved": False}, 409)
                    url = result["url"] if result["ok"] else c["hubs"][0]
                    cfgmod.update_section("sync", {"mode": "collector", "hub_url": url, "token": c["token"]})
                    log.info("stations: joined hub %s (restart to apply)", url)
                    return self._json({**result, "saved": True, "hubUrl": url, **self._hub_info()})
                if path == "/api/hub/leave":
                    cfgmod.update_section("sync", {"mode": "off"})
                    log.info("stations: stopped sending to the hub (restart to apply)")
                    return self._json({"ok": True, **self._hub_info()})
                return self._json({"error": "not found"}, 404)
            except ValueError as e:
                return self._json({"error": str(e)}, 400)

    return HubRoutes
