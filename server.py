"""Lorakeet: logs everything a Meshtastic radio hears to SQLite and serves the dashboard.

    python server.py [--port COM3] [--http 5190]      (defaults come from lorakeet.toml; see config.py)

This file is the entry point: command-line options, start-up (database, radio link, sync, alerts, backups,
the web server) and the demo. The rest lives in:

    runtime.py    paths, version, the data folder, the demo's radio, the base station (read at call time)
    store.py      mesh.db: schema, migrations, field notes, Store, the debug-log database, secret stripping
    fwlog.py      parsing the firmware's RX/TX debug lines (rx_hops, tx_log)
    channels.py   channel hashes and keys as the firmware computes them; which channels are public
    mesh.py       the radio link (Mesh): connect, log, live state, send text and traceroutes
    station.py    this station's health, report to a hub, location, Radio-page station settings
    peers.py      hub-to-hub peering senders
    web.py        the HTTP server and request handler; web_radio.py and web_hub.py hold the Radio page's
                  and the hub's routes
"""
import argparse
import logging
import logging.handlers
import signal
import sys
import threading
import time

import alerts as alerts_mod
import analytics
from config import CFG
import login as login_mod
from mesh import Mesh, first_public_key, key_flag
import nodeids
import pairing
from peers import PeerManager
import remote as remote_mod
import runtime
from station import HealthWatch, _pi_health, _uptime_s, station_overrides, station_report
from storage import Storage
from store import DebugLog, Store, scrub_logged_secrets, StationStore
import sync as sync_mod
from web import bind_server, make_handler


EXIT_PORT_IN_USE = 3       # tells supervise.pyw another instance already owns the HTTP port

log = logging.getLogger("meshdash")


def set_password_interactive(path):
    """--set-password: asks twice in the terminal (never on the command line, so it stays out of shell history)."""
    import getpass
    lg = login_mod.Login(path)
    print("Set the password other devices on your network log in with (at least "
          f"{login_mod.MIN_LENGTH} characters)." + (" This replaces the old one and logs every device out." if lg.enabled else ""))
    while True:
        a = getpass.getpass("New password: ")
        if a != getpass.getpass("Again: "):
            print("They didn't match. Try again.")
            continue
        try:
            lg.set_password(a)
        except ValueError as e:
            print(f"Not set: {e}.")
            continue
        break
    print(f"Saved in {path}, as a salted hash (the password itself is never stored).")
    if CFG["http"]["lan"] == "off":
        print("Other devices can't reach the dashboard yet: set [http] lan = \"view\" or \"login\" in lorakeet.toml, then restart.")
    else:
        print("Restart Lorakeet to use it.")


def setup_demo():
    """--demo: a made-up mesh around Portland in <data>/demo, rebuilt when stale; nothing personal, no radio."""
    import demo
    runtime.DATA_DIR = runtime.DATA_DIR / "demo"
    runtime.DATA_DIR.mkdir(parents=True, exist_ok=True)
    db = runtime.DATA_DIR / "mesh.db"
    if demo.stale(db):
        demo.build(db)
    runtime.DEMO = demo.home_of(db)
    runtime.BASE_ID = runtime.BASE_NAME = runtime.BASE_BYTE = None
    CFG["map"].update(center=list(demo.PORTLAND), zoom=11)
    CFG["base"].update(id="", name="")
    CFG["station"].update(name="Demo Home", location=[], mobile=False, drive_pings=False)
    CFG["http"]["lan"] = "off"
    CFG["sync"]["mode"] = "off"
    CFG["remote"]["enabled"] = False
    CFG["backup"]["destinations"] = []

    def idle_exit():
        # an open tab keeps polling, so idleness alone could keep a demo running for days while "the last 24 hours"
        # empties out: past demo.stale's age it exits too, and the next "Explore a demo" builds a fresh one
        while time.time() - runtime.LAST_REQUEST[0] < runtime.DEMO_IDLE_S and not demo.stale(db, 18 * 3600):
            time.sleep(60)
        log.info("demo: idle or out of date, exiting")
        runtime.exit_now(0)
    threading.Thread(target=idle_exit, daemon=True, name="demo-idle").start()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=CFG["radio"]["port"] or None,
                    help="serial port (default: lorakeet.toml radio.port, else auto-detect)")
    ap.add_argument("--host", default=CFG["radio"]["host"] or None,
                    help="a radio on the network (IP or name) instead of USB (default: lorakeet.toml radio.host)")
    ap.add_argument("--http", type=int, default=None, help="dashboard port (default: lorakeet.toml http.port; "
                                                                "with --demo, one above it)")
    ap.add_argument("--demo", action="store_true", help="explore a made-up mesh: no radio, its own data folder "
                                                         "(<data>/demo) and port")
    ap.add_argument("--version", action="version", version=f"Lorakeet {runtime.VERSION}")
    ap.add_argument("--set-password", action="store_true",
                    help="set the password other devices on your network log in with, then exit")
    ap.add_argument("--clear-password", action="store_true", help="remove the password (and every login), then exit")
    ap.add_argument("--join", metavar="CODE", help="send what this station logs to a hub: the pairing code from the hub's "
                                                   "Stations page (or - to type or pipe it in), then restart Lorakeet")
    ap.add_argument("--force", action="store_true", help="with --join: save even if the hub can't be reached right now")
    args = ap.parse_args()
    if args.join:
        import config as cfgmod
        if args.join == "-":  # read it, so the code stays out of shell history and the process list
            if sys.stdin.isatty():
                print("Paste the pairing code from the hub's Stations page, then press Enter:")
            args.join = sys.stdin.readline().strip()
        try:
            c = pairing.parse_code(args.join)
        except ValueError as e:
            sys.exit(f"Not joined: {e}.")
        print(f"Testing the hub{' ' + c['name'] if c['name'] else ''} at {', '.join(c['hubs'])} ...")
        result, _ = pairing.test_hubs(c["hubs"], c["token"])
        if result["ok"]:
            print(f"Reached {result.get('hubName') or 'the hub'} at {result['url']}.")
        else:
            print(f"Couldn't reach the hub: {result['error']}\n{result['hint']}")
            if not args.force:
                sys.exit("Not joined. Fix that and try again, or add --force to save anyway.")
        try:
            path = cfgmod.update_section("sync", {"mode": "collector", "hub_url": result["url"] if result["ok"] else c["hubs"][0],
                                                  "token": c["token"]})
        except ValueError as e:
            sys.exit(f"Not joined: {e}")
        print(f"Saved in {path}. Restart Lorakeet: it then sends what it logs to the hub, and catches up after any outage.")
        return
    if args.set_password or args.clear_password:
        runtime.DATA_DIR.mkdir(parents=True, exist_ok=True)
        if args.set_password:
            set_password_interactive(runtime.DATA_DIR / "login.json")
        else:
            login_mod.Login(runtime.DATA_DIR / "login.json").clear_password()
            print("Password removed; other devices are logged out.")
        return
    if args.host and args.port:
        ap.error("use --port (USB) or --host (network), not both")
    if args.http is None:
        args.http = CFG["http"]["port"] + (1 if args.demo else 0)
    if args.demo:
        setup_demo()

    runtime.DATA_DIR.mkdir(parents=True, exist_ok=True)
    handlers = [logging.handlers.RotatingFileHandler(runtime.DATA_DIR / "server.log", maxBytes=2_000_000,
                                                     backupCount=3, encoding="utf-8")]
    if sys.stdout is not None:  # None under pythonw
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    logging.getLogger("meshtastic").setLevel(logging.WARNING)

    store = Store(runtime.DATA_DIR / "mesh.db")
    runtime.EXIT_HOOKS.append(store.flush)
    signal.signal(signal.SIGTERM, lambda *_: runtime.exit_now(0))  # systemd's stop: commit what's waiting, then go
    if runtime.DEMO:
        store.claim_station(runtime.DEMO)
    scrub_logged_secrets(store)
    PAIRINGS = pairing.Pairings(runtime.DATA_DIR / "stations.json")
    debug_log = DebugLog(runtime.DATA_DIR / "debug.db")
    runtime.EXIT_HOOKS.append(debug_log.flush)
    runtime.follow_base()  # [base] id: the base station's current number if firmware 2.8 renumbered it
    if CFG["radio"]["protocol"] == "meshcore":   # a MeshCore companion radio (docs/MESHCORE.md)
        from meshcore_link import MeshCoreLink
        mesh = MeshCoreLink(store, args.port, debug_log)
    else:
        mesh = Mesh(store, args.port, debug_log, host=args.host, tcp_port=CFG["radio"]["tcp_port"])
    # [[radios]]: more radios in this one Lorakeet, each its own station (MESHCORE.md step 6). They log and sync; the
    # pages' live map and Radio page are the main radio's.
    for x in CFG["radios"] if not runtime.DEMO else []:
        if x.get("protocol") == "meshcore":
            from meshcore_link import MeshCoreLink
            link = MeshCoreLink(StationStore(store), x["port"], debug_log)
        else:
            link = Mesh(StationStore(store), x["port"], debug_log)
        link.station_name = x.get("name") or ""
        mesh.extras.append(link)
    mesh.claimed_ports = {x["port"] for x in CFG["radios"]}   # auto-detect never takes one of theirs
    if runtime.DEMO:
        mesh.local_id = runtime.DEMO
        mesh._load_remote()   # the other stations' radios (Hilltop, the car, the MeshCore one): there's no connect to load it
    debug_log.on_batch = lambda rows: mesh.broadcast("debug", rows)
    # Bind before touching the radio: a second instance must exit here, not fight over the COM port.
    try:
        storage = Storage(runtime.DATA_DIR / "mesh.db", runtime.DATA_DIR / "debug.db", runtime.DATA_DIR,
                          dests=CFG["backup"]["destinations"],
                          on_change=lambda st: mesh.broadcast("storage", st), on_event=mesh.event)
        alert_engine = alerts_mod.Alerts(store, mesh, runtime.DATA_DIR / "mesh.db", runtime.BASE_ID)
        alert_engine.pairings = PAIRINGS
        # http.lan = "view" / "login": all interfaces; this PC full access, other private-LAN devices read-only
        # or nothing until they log in (login.access); "off": this computer only.
        bind = "0.0.0.0" if CFG["http"]["lan"] in ("view", "login") else "127.0.0.1"
        syncer = None
        if CFG["sync"]["mode"] == "hub":
            def on_report(station, rep):
                if (station_overrides(store).get(station) or {}).get("location"):
                    pass  # the hub's own placement wins over what the station reports
                elif rep.get("location"):
                    loc = rep["location"]
                    analytics.STATION_LOCATIONS[station] = tuple(loc) + ((None,) if len(loc) == 2 else ())
                else:
                    analytics.STATION_LOCATIONS.pop(station, None)
                mesh.station_ids.add(station)
            syncer = sync_mod.Hub(store, on_rows=mesh.ingested, on_meta=on_report, is_revoked=PAIRINGS.is_revoked)
            # collectors arrive over Tailscale, so the hub must listen beyond this PC even with lan = "off";
            # client_access() still gives non-LAN addresses no dashboard access, only /api/ingest
            bind = "0.0.0.0"
        elif CFG["sync"]["mode"] == "collector":
            syncer = sync_mod.Collector(store, CFG["sync"]["hub_url"], CFG["sync"]["token"], CFG["sync"]["interval_s"],
                                        report=lambda: station_report(mesh),
                                        extra=[{"station": lambda lk=lk: lk.local_id, "token": x.get("sync_token", ""),
                                                "report": lambda lk=lk: station_report(lk, name=lk.station_name)}
                                               for lk, x in zip(mesh.extras, CFG["radios"])])
        login = login_mod.Login(runtime.DATA_DIR / "login.json")
        peering = PeerManager(store, mesh, pairing.install_id(runtime.DATA_DIR))
        srv = bind_server((bind, args.http), make_handler(mesh, store, storage, alert_engine, syncer, login, peering,
                                                          PAIRINGS))
    except OSError as e:
        log.error("port %d unavailable (%s); another instance is probably running", args.http, e)
        sys.exit(EXIT_PORT_IN_USE)
    srv.daemon_threads = True
    if CFG["remote"]["enabled"]:
        mesh.remote = remote_mod.Remote(
            mesh, CFG["remote"]["allow"], pinned_key=lambda n: first_public_key(store, n), key_flag=key_flag,
            status=lambda: remote_mod.status_text(
                CFG["station"]["name"] or mesh.name(mesh.local_id), _uptime_s(), remote_mod.network(),
                # the backlog counted now: the sync status keeps the last SUCCESSFUL pass's (0 while offline)
                {**syncer.status, "backlog": syncer.backlog()} if isinstance(syncer, sync_mod.Collector) else None,
                _pi_health(), mesh.own_fix_now()),
            fix=mesh.own_fix_now, aliases=lambda: nodeids.info(runtime.DATA_DIR / "mesh.db")["aliases"])
        log.info("remote commands: on, from %s", ", ".join(CFG["remote"]["allow"]))
    if runtime.DEMO:  # nothing to connect to, back up, alert on or share
        log.info("Lorakeet %s demo (a made-up mesh) on http://127.0.0.1:%d", runtime.VERSION, args.http)
        srv.serve_forever()
        return
    threading.Thread(target=mesh.run, daemon=True, name="radio").start()
    for i, link in enumerate(mesh.extras, 2):
        threading.Thread(target=link.run, daemon=True, name=f"radio{i}").start()
        log.info("also logging a %s radio on %s", link.PROTOCOL, link.port_hint)
    if sys.platform.startswith("linux"):
        HealthWatch(mesh.event).start()
    storage.start()
    alert_engine.start()
    if CFG["weather"]["enabled"]:
        import weather
        weather.Weather(runtime.DATA_DIR / "mesh.db").start()
        log.info("weather: on (Open-Meteo, ~10 km squares)")
    if isinstance(syncer, sync_mod.Collector):
        syncer.start()
    elif CFG["sync"]["mode"] != "collector":
        peering.start_all()  # a station sends to its hub; peering is between hubs (or single installs)
    log.info("sync: %s", CFG["sync"]["mode"])
    log.info("config: %s", CFG["_path"] or "none (defaults)")
    lan = CFG["http"]["lan"]
    log.info("Lorakeet %s, dashboard on http://127.0.0.1:%d%s", runtime.VERSION, args.http,
             " (this computer only)" if lan == "off" else
             f" (full) and on the LAN ({'view only' if lan == 'view' else 'log in to see it'}"
             f"{', full access after logging in' if login.enabled else ''})")
    if lan == "login" and not login.enabled:
        log.warning('http.lan = "login" but no password is set, so no other device can get in: '
                    "run  python server.py --set-password")
    srv.serve_forever()


if __name__ == "__main__":
    main()
