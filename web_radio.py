"""The Radio page's routes (/api/radio*: this computer only): checklist, Advanced settings, undo,
channels, backups, this station.
"""
import json
import logging
import os
import re
import threading
import time

from channels import _preset_name
from config import CFG
from mesh import CONNECT_TIMEOUT_S, Mesh, radio_ports, serial_ports
import radio_advanced
import radio_setup
import runtime
from station import heard_points, station_settings, supervised_now
from store import node_id


log = logging.getLogger("meshdash")


def _was_undo(backup):
    """Whether the radio's last write was an undo (its backup is a "before-undo" one): the undo button then redoes."""
    return "-before-undo" in str(backup or "")


def make(mesh, store, storage, alert_engine, syncer, login, peering, pairings, hub_id, hub_name, station_meta, station_name):
    """The routes, closed over make_handler's state (web.make_handler mixes them into its Handler)."""
    class RadioRoutes:
        # ---- the Radio page (radio_setup.py)
        def _radio_get(self, path, qs):
            iface = mesh.iface
            if path == "/api/radio" and getattr(mesh, "PROTOCOL", "meshtastic") == "meshcore":
                # what the radio says about itself; MeshCore settings can't be changed here yet (MESHCORE.md, step 7)
                info = mesh.self_info or {}
                return self._json({"protocol": "meshcore", "connected": bool(mesh.connected), "port": mesh.port,
                                   "error": mesh.last_connect_error, "station": station_settings(mesh),
                                   "mobile": CFG["station"]["mobile"],
                                   "radio": {"id": mesh.local_id, "name": info.get("name"),
                                             "freq": info.get("radio_freq"), "bw": info.get("radio_bw"),
                                             "sf": info.get("radio_sf"), "cr": info.get("radio_cr"),
                                             "txPower": info.get("tx_power"), "maxTxPower": info.get("max_tx_power")}})
            if path == "/api/radio":
                ports = serial_ports()
                out = {"connected": bool(mesh.connected and iface), "port": mesh.port, "network": bool(mesh.host),
                       "connectedAt": mesh.connected_at, "ports": ports,
                       "otherRadios": [p for p in ports if p["radio"] and p["device"] != mesh.port],
                       "choose": [p for p in ports if p["radio"]] if mesh.needs_choice else None,
                       "portSet": bool(mesh.port_hint),
                       "error": mesh.last_connect_error, "hint": radio_setup.connect_hint(mesh.last_connect_error),
                       "noPortHint": None if mesh.host or any(p["radio"] for p in ports) else radio_setup.NO_PORT_HINT,
                       "backupDir": str(runtime.RADIO_BACKUPS), "mobile": CFG["station"]["mobile"],
                       "station": station_settings(mesh),
                       "checklistPaths": {k: radio_setup.field_path(k) for k in radio_setup.FIELDS}}
                if out["connected"]:
                    node = iface.localNode
                    fw = getattr(getattr(iface, "metadata", None), "firmware_version", "") or ""
                    user = (iface.getMyNodeInfo() or {}).get("user", {})
                    out["radio"] = {"id": mesh.local_id, "longName": user.get("longName"), "shortName": user.get("shortName"),
                                    "hw": user.get("hwModel"), "firmware": fw}
                    out["checklist"] = radio_setup.checklist(node, fw, usb=not mesh.host, mobile=CFG["station"]["mobile"],
                                                             has_base=bool(runtime.BASE_ID))
                    out["channels"] = radio_setup.channel_list(node, _preset_name(node))
                    out["backups"] = radio_setup.backups(runtime.RADIO_BACKUPS, mesh.local_id)[:10]
                return self._json(out)
            if path == "/api/radio/location-check":  # would this antenna position be far from the radios we hear?
                try:
                    loc = [float(qs["lat"]), float(qs["lon"])]
                except (KeyError, ValueError):
                    return self._json({"error": "lat and lon, please"}, 400)
                return self._json({"check": radio_setup.location_check(loc, heard_points(mesh))})
            if path == "/api/radio/advanced":  # every setting the radio reported (secrets: only whether they're set)
                if not (mesh.connected and iface):
                    return self._json({"error": "no radio connected"}, 409)
                if not radio_setup.config_complete(iface.localNode):
                    return self._json({"error": "the radio's settings didn't fully load: reconnect it before changing anything"}, 409)
                return self._json({**radio_advanced.schema(iface.localNode),
                                   "checklistPaths": {k: radio_setup.field_path(k) for k in radio_setup.FIELDS}})
            if path == "/api/radio/undo":  # what "Undo last save" would change (secrets: only that they'd go back)
                if not (mesh.connected and iface) or not radio_setup.config_complete(iface.localNode):
                    return self._json({"available": False, "reason": "no radio connected"})
                plan, why = self._undo_plan(iface.localNode, iface)
                if not plan:
                    return self._json({"available": False, "reason": why})
                fields = {f["path"]: (f, s["title"]) for s in radio_advanced.schema(iface.localNode)["sections"] for f in s["fields"]}
                items = []
                for p, v in plan["before"].items():
                    f, title = fields.get(p, ({}, ""))
                    if f.get("secret"):
                        items.append({"path": p, "field": f, "section": title, "secret": True})
                    elif f and f.get("value") != v:
                        items.append({"path": p, "field": f, "section": title, "to": v})
                user = (iface.getMyNodeInfo() or {}).get("user", {})
                names = plan["names"] if plan["names"] and plan["names"] != (user.get("longName"), user.get("shortName")) else None
                return self._json({"available": bool(items or names), "ts": plan["ts"], "backup": plan["backup"], "items": items,
                                   "redo": _was_undo(plan["backup"]),
                                   "names": {"from": [user.get("longName"), user.get("shortName")], "to": list(names)} if names else None,
                                   "reason": None if items or names else "The radio already has the values from before the last save."})
            if path == "/api/radio/logging":  # is it logging? (the walkthrough's last step)
                since = float(qs.get("since") or time.time() - 600)
                st = mesh.local_id
                one = lambda sql: (store.query(sql, st, since) or [{"n": 0}])[0]["n"]  # noqa: E731
                debug = bool(iface and iface.localNode.localConfig.security.debug_log_api_enabled)
                return self._json({"connected": bool(mesh.connected and iface), "usb": not mesh.host, "since": since,
                                   "packets": one("SELECT COUNT(*) AS n FROM packets WHERE station = ? AND ts >= ?"),
                                   "receptions": one("SELECT COUNT(*) AS n FROM rx_hops WHERE station = ? AND ts >= ?"),
                                   "lastPacket": (store.query("SELECT MAX(ts) AS t FROM packets WHERE station = ?", st) or [{}])[0].get("t"),
                                   "debugLog": debug, "logLines": mesh.log_counts["lines"], "mined": mesh.log_counts["mined"]})
            if path == "/api/radio/share":  # a private channel's link and QR code, to add it in the Meshtastic app
                if not iface:
                    return self._json({"error": "no radio connected"}, 409)
                try:
                    c = radio_setup.find_channel(iface.localNode, int(qs.get("index", -1)))
                except (ValueError, radio_setup.RadioError) as e:
                    return self._json({"error": str(e)}, 400)
                if radio_setup.key_kind(c.settings.psk, c.role) != "private":
                    return self._json({"error": "only channels with their own private key are shared here"}, 400)
                url = radio_setup.share_url(c.settings)
                log.info("radio: share link shown for channel %d", c.index)
                return self._json({"name": c.settings.name, "url": url, "svg": radio_setup.qr_svg(url)})
            return self._json({"error": "not found"}, 404)

        def _undo_plan(self, node, iface):
            """Undo the newest settings save on this radio: the settings it changed (from the events log), back to the
            values in the backup taken just before it. ({ts, backup, before, names}, None) or (None, reason)."""
            import yaml
            rows = store.query("SELECT ts, detail FROM events WHERE kind = 'radio_settings' AND station = ? "
                               "ORDER BY ts DESC LIMIT 1", mesh.local_id)
            if not rows:
                return None, "No settings save on this radio yet."
            ev = json.loads(rows[0]["detail"] or "{}")
            name = str(ev.get("backup") or "")
            bk = runtime.RADIO_BACKUPS / name
            ok_name = re.fullmatch(r"[0-9a-f]{8}-[0-9]{8}-[0-9]{6}-[a-z-]+\.yaml", name)
            if not ok_name or not name.startswith(mesh.local_id[1:]) or not bk.is_file():
                return None, "The backup taken before the last save isn't there any more."
            paths = list(dict.fromkeys(list(ev.get("advanced") or {}) +
                                       [radio_setup.field_path(k) for k in (ev.get("changes") or {}) if k in radio_setup.FIELDS]))
            backup = yaml.safe_load(bk.read_text(encoding="utf-8")) or {}
            before = {}
            for p in paths:
                try:
                    v = radio_advanced.backup_values(node, backup, [p])[p]
                    radio_advanced.plan(node, {p: v})  # a value the radio can take back (an unset PIN can't be "set" to 0)
                    before[p] = v
                except radio_advanced.AdvancedError:
                    pass  # a section this radio no longer reports, one Lorakeet can't write, or no valid old value
            names = None
            if ev.get("names") and backup.get("owner"):
                names = (str(backup["owner"]), str(backup.get("owner_short") or ""))
            return {"ts": rows[0]["ts"], "backup": name, "before": before, "names": names}, None

        def _radio_post(self, path, body):
            if path == "/api/radio/station":  # [station] name and location in lorakeet.toml
                import config as cfgmod
                if CFG["station"]["mobile"] and body.get("location"):
                    return self._json({"error": "this station moves ([station] mobile): its position comes from its radio's GPS"}, 400)
                try:
                    loc = body.get("location") or []
                    if loc and not (len(loc) == 2 and -90 <= float(loc[0]) <= 90 and -180 <= float(loc[1]) <= 180):
                        raise ValueError("latitude -90..90 and longitude -180..180, please")
                    path_ = cfgmod.update_station(body.get("name"), loc)
                except (ValueError, TypeError) as e:
                    return self._json({"error": str(e)}, 400)
                log.info("station settings saved to %s (name %r, location %s)", path_, body.get("name"), "set" if loc else "none")
                return self._json({"ok": True, "path": str(path_), "station": station_settings(mesh)})
            if path == "/api/radio/use":  # "Log from this one": which of several USB radios is ours
                if mesh.host or mesh.port_hint:
                    return self._json({"error": "lorakeet.toml names the radio ([radio] port or host): clear it on the "
                                                "Settings page to choose here"}, 409)
                serial = dict(radio_ports()).get(body.get("device"))
                if not serial:
                    return self._json({"error": "that radio isn't plugged in (or has no USB serial number to know it by)"}, 400)
                mesh.remember_radio(serial)
                log.info("radio: %s chosen on the Radio page (USB serial %s)", body.get("device"), serial)
                mesh.drop_link()  # the reconnect loop picks the remembered radio within seconds
                return self._json({"ok": True})
            if path == "/api/radio/restart":
                if not supervised_now():
                    return self._json({"error": "Lorakeet isn't running under its background runner, so it can't restart "
                                                "itself: stop it and start it again (the Lorakeet shortcut starts it)"}, 409)
                log.info("restart requested from the Radio page")
                self._json({"ok": True})
                threading.Timer(1.0, lambda: os._exit(0)).start()
                return None
            iface = mesh.iface
            if not (mesh.connected and iface):
                return self._json({"error": "no radio connected"}, 409)
            if not runtime.RADIO_LOCK.acquire(timeout=1):
                return self._json({"error": "another change to the radio is still in progress"}, 409)
            try:
                node = iface.localNode
                if path == "/api/radio/apply":
                    fw = getattr(getattr(iface, "metadata", None), "firmware_version", "") or ""
                    items = radio_setup.checklist(node, fw, usb=not mesh.host, mobile=CFG["station"]["mobile"],
                                                  has_base=bool(runtime.BASE_ID))
                    changes = radio_setup.plan(items, body.get("changes"))
                    planned = radio_advanced.plan(node, body.get("advanced"))
                    for k, v in changes.items():  # the checklist and Advanced name the same setting the same way
                        p = radio_setup.field_path(k)
                        if p in planned and radio_advanced.public_view({p: planned[p]})[p] != v:
                            raise radio_setup.RadioError(f"{p} has two different new values: change it in one place")
                    risks = radio_advanced.risky(planned)
                    if risks and not body.get("confirmRisky"):
                        return self._json({"error": "some of these changes need confirming", "needsConfirm": risks}, 409)
                    names = None
                    if body.get("names"):
                        names = radio_setup.check_names(body["names"].get("long"), body["names"].get("short"))
                        user = (iface.getMyNodeInfo() or {}).get("user", {})
                        if names == (user.get("longName"), user.get("shortName")):
                            names = None
                    if not changes and not names and not planned:
                        return self._json({"ok": True, "nothing": True})
                    shown = radio_advanced.public_view(planned)  # secrets as "(changed)": never logged or echoed
                    bk = radio_setup.backup(iface, runtime.RADIO_BACKUPS, mesh.local_id, "before-settings", mesh.port)
                    sections = radio_setup.apply(iface, changes, names, planned)
                    log.info("radio: wrote %s%s in one transaction (%d change%s; backup %s)", ", ".join(sections) or "-",
                             " + names" if names else "", len(changes) + len(planned), "" if len(changes) + len(planned) == 1 else "s", bk.name)
                    mesh.event("radio_settings", changes=changes, advanced=shown, names=bool(names), backup=bk.name)
                    # the radio restarts to apply most settings; if it doesn't, reconnect anyway so what the page
                    # shows next is read back from the radio, not our own edited copy
                    threading.Timer(15, lambda: mesh._drop(iface) if mesh.iface is iface else None).start()
                    return self._json({"ok": True, "backup": bk.name, "expected": changes, "expectedAdvanced": shown,
                                       "sections": sections, "names": list(names) if names else None})
                if path == "/api/radio/undo":  # put back what the last save changed, from the backup taken before it
                    plan, why = self._undo_plan(node, iface)
                    if not plan:
                        raise radio_setup.RadioError(why)
                    planned = radio_advanced.plan(node, plan["before"])
                    user = (iface.getMyNodeInfo() or {}).get("user", {})
                    names = plan["names"] if plan["names"] and plan["names"] != (user.get("longName"), user.get("shortName")) else None
                    if names:
                        names = radio_setup.check_names(*names)
                    if not planned and not names:
                        return self._json({"ok": True, "nothing": True})
                    risks = radio_advanced.risky(planned)
                    if risks and not body.get("confirmRisky"):
                        return self._json({"error": "some of these changes need confirming", "needsConfirm": risks}, 409)
                    shown = radio_advanced.public_view(planned)
                    # undoing an undo is a redo: the backup's name records which, so the page can say Undo or Redo next
                    redo = _was_undo(plan["backup"])
                    bk = radio_setup.backup(iface, runtime.RADIO_BACKUPS, mesh.local_id, "before-redo" if redo else "before-undo",
                                            mesh.port)
                    sections = radio_setup.apply(iface, {}, names, planned)
                    log.info("radio: %s the change backed up as %s: wrote %s (backup %s)", "redid" if redo else "undid",
                             plan["backup"], ", ".join(sections) or "-", bk.name)
                    mesh.event("radio_settings", changes={}, advanced=shown, names=bool(names), backup=bk.name, undoOf=plan["backup"])
                    threading.Timer(15, lambda: mesh._drop(iface) if mesh.iface is iface else None).start()
                    return self._json({"ok": True, "backup": bk.name, "expected": {}, "expectedAdvanced": shown,
                                       "sections": sections, "names": list(names) if names else None})
                if path == "/api/radio/channel":
                    action = body.get("action")
                    if action == "create":
                        new = [radio_setup.new_channel(body.get("name"), 32 if body.get("exactPositions", True) else 13)]
                    elif action == "add-link":
                        new = radio_setup.parse_url(body.get("url"))
                    elif action == "copy-to":  # one of this radio's channels onto another radio plugged in here
                        c = radio_setup.find_channel(node, int(body.get("index", -1)))
                        if radio_setup.key_kind(c.settings.psk, c.role) != "private":
                            raise radio_setup.RadioError("only channels with their own private key can be copied")
                        return self._json(self._copy_channel(c.settings, str(body.get("port") or "")))
                    else:
                        raise radio_setup.RadioError("unknown channel action")
                    bk = radio_setup.backup(iface, runtime.RADIO_BACKUPS, mesh.local_id, "before-channel", mesh.port)
                    done = radio_setup.add_channels(node, new)
                    log.info("radio: channels %s (backup %s)", ", ".join(f"{n}@{i} {w}" for n, i, w in done), bk.name)
                    mesh.event("radio_channels", channels=[{"name": n, "index": i, "result": w} for n, i, w in done], backup=bk.name)
                    threading.Timer(15, lambda: mesh._drop(iface) if mesh.iface is iface else None).start()
                    return self._json({"ok": True, "backup": bk.name, "channels": [{"name": n, "index": i, "result": w} for n, i, w in done]})
                return self._json({"error": "not found"}, 404)
            except (radio_setup.RadioError, radio_advanced.AdvancedError) as e:
                return self._json({"error": str(e)}, 400)
            finally:
                runtime.RADIO_LOCK.release()

        def _copy_channel(self, settings, port):
            """Write a channel to ANOTHER radio on USB here (not the logging one): open it, back it up, add, close."""
            if not port or port == mesh.port or port not in {p["device"] for p in serial_ports()}:
                raise radio_setup.RadioError("pick another radio plugged into this computer")
            from meshtastic.serial_interface import SerialInterface
            try:
                other = SerialInterface(port, timeout=CONNECT_TIMEOUT_S)
            except Exception as e:  # noqa: BLE001
                raise radio_setup.RadioError(f"couldn't open {port}: {radio_setup.connect_hint(str(e)) or e}") from e
            try:
                oid = node_id(other.myInfo.my_node_num)
                bk = radio_setup.backup(other, runtime.RADIO_BACKUPS, oid, "before-channel", port)
                done = radio_setup.add_channels(other.localNode, [settings])
                log.info("radio: channel %s copied to %s on %s (backup %s)", settings.name, oid, port, bk.name)
                mesh.event("radio_channels", radio=oid, channels=[{"name": n, "index": i, "result": w} for n, i, w in done],
                           backup=bk.name)
                return {"ok": True, "radio": oid, "backup": bk.name, "channels": [{"name": n, "index": i, "result": w} for n, i, w in done]}
            finally:
                Mesh._close_quietly(other)

    return RadioRoutes
