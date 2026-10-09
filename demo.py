"""A made-up mesh to explore Lorakeet without a radio (the setup page's "Explore a demo first", `--demo`).

Everything here is invented: the radios, their names, keys and positions (scattered around downtown Portland, Oregon,
a public city centre), the messages, and the traffic. It's built fresh from a fixed seed into its own database, never
the real one, with timestamps over the last three days so every view (24 h, 7 days...) has something to show:

- "Demo Home", this computer's pretend radio, with a two-hour logging gap one night (gaps are drawn as gaps);
- "Hilltop Station", a second listening station that syncs to it, and "Demo Car", a mobile station with one drive;
- three routers on the hills and about twenty radios around town: positions, telemetry, node info, chatter on the
  public channel, a private channel this radio can read, encrypted traffic it can't, a few traceroutes, and two radios
  announcing the same key (so the key warning shows).
"""
import base64
import json
import math
import random
import time
from pathlib import Path

PORTLAND = (45.5152, -122.6784)  # the city centre the made-up mesh is scattered around
DAYS = 3
SEED = 20261009
PUBLIC_HASH = 8                   # LongFast with the well-known key, as the firmware computes it
PRIVATE_NAME = "DemoFamily"

STATIONS = [  # name, short, hw, role, (dlat, dlon)
    ("Demo Home", "DHOM", "HELTEC_V3", "CLIENT_MUTE", (0.012, 0.035)),
    ("Hilltop Station", "HILL", "RAK4631", "CLIENT_MUTE", (-0.006, -0.062)),
    ("Demo Car", "DCAR", "TRACKER_T1000_E", "CLIENT", (0.012, 0.035)),
]
ROUTERS = [
    ("Crest Router", "CRST", "RAK4631", "ROUTER", (-0.016, -0.054)),
    ("Butte Relay", "BUTE", "STATION_G2", "ROUTER_LATE", (0.034, 0.122)),
    ("Tabor Node", "TABR", "HELTEC_V3", "ROUTER", (-0.004, 0.086)),
]
CLIENTS = [
    ("Fern", "FERN", "T_ECHO"), ("Juniper", "JUNI", "HELTEC_V3"), ("Otter Den", "OTTR", "RAK4631"),
    ("Maple Node", "MAPL", "TBEAM"), ("Lantern", "LANT", "HELTEC_MESH_NODE_T114"), ("Heron Deck", "HERN", "T_DECK"),
    ("Cedar Solar", "CEDR", "RAK4631"), ("Basalt", "BSLT", "HELTEC_V3"), ("Kestrel", "KSTR", "TRACKER_T1000_E"),
    ("Willow", "WILO", "T_ECHO"), ("Sparrow Tracker", "SPRW", "TRACKER_T1000_E"), ("Moss Garden", "MOSS", "RAK4631"),
    ("Trillium", "TRIL", "HELTEC_V3"), ("Quartz", "QRTZ", "STATION_G2"), ("Pixel Pine", "PINE", "HELTEC_MESH_NODE_T114"),
    ("Bramble", "BRMB", "TBEAM"), ("Larkspur", "LARK", "T_ECHO"), ("Driftwood", "DRFT", "HELTEC_V3"),
    ("Ferry Base", "FRRY", "RAK4631"), ("Night Owl", "OWL", "T_DECK"),
]
CHATTER = [
    "Good morning, mesh!", "Anyone hearing me from the east side?", "Testing my new antenna, signal reports welcome",
    "Clear skies tonight", "Heading out for a ride, tracker on", "Coffee meetup Saturday?", "Rain's back",
    "Just put a solar node on the roof", "Hearing you 2 hops away", "Nice, got you direct!", "Who's on the hill relay?",
    "Battery at 40%, swapping it out", "Thanks for the relay!", "Bridge traffic is bad today", "Back online after a reboot",
    "Anyone tried the new firmware?", "Great sunset from the west hills", "Checking in", "Moving my node upstairs",
    "Range test from the park", "That relay covers a lot", "Hello from downtown", "Signing off for the night",
]
FAMILY = ["Home by six", "Picked up groceries", "Car's charged", "Leaving now", "Dinner's ready"]
ROUTE = [(0.012, 0.035), (0.020, 0.060), (0.028, 0.095), (0.034, 0.122), (0.050, 0.140), (0.070, 0.150),
         (0.060, 0.110), (0.040, 0.080), (0.030, 0.050), (0.012, 0.035)]  # the car's loop, as offsets from PORTLAND


def _km(a, b):
    dlat = math.radians(b[0] - a[0])
    dlon = math.radians(b[1] - a[1]) * math.cos(math.radians((a[0] + b[0]) / 2))
    return 6371 * math.hypot(dlat, dlon)


class _Mesh:
    def __init__(self, rng):
        self.rng = rng
        self.radios = {}  # id -> dict(name, short, hw, role, pos, key, kind)
        used = set()

        def nid():
            while True:
                v = f"!{rng.getrandbits(32):08x}"
                if v not in used:
                    used.add(v)
                    return v

        def add(name, short, hw, role, off, kind):
            i = nid()
            pos = (PORTLAND[0] + off[0], PORTLAND[1] + off[1])
            self.radios[i] = {"id": i, "name": name, "short": short, "hw": hw, "role": role, "pos": pos, "kind": kind,
                              "key": base64.b64encode(rng.randbytes(32)).decode()}
            return i

        self.home, self.hill, self.car = (add(*s, kind) for s, kind in zip(STATIONS, ("home", "hill", "car")))
        self.routers = [add(*r, "router") for r in ROUTERS]
        self.clients = []
        for name, short, hw in CLIENTS:
            off = (rng.uniform(-0.09, 0.09), rng.uniform(-0.16, 0.16))
            self.clients.append(add(name, short, hw, "CLIENT" if rng.random() > 0.2 else "CLIENT_MUTE", off, "client"))
        a, b = self.clients[3], self.clients[11]  # two radios announcing the same key: the "shared key" warning
        self.radios[b]["key"] = self.radios[a]["key"]
        self.hidden = set(self.clients[-4:])      # these never share a position

    def heard(self, station_pos, src):
        """How a station at station_pos hears radio src: None, or (hops, relay byte, snr, rssi)."""
        rng, s = self.rng, self.radios[src]
        d = _km(station_pos, s["pos"])
        if d < 7 and rng.random() < 1.1 - d / 7:
            return 0, None, *self._signal(d)
        best = None
        for r in self.routers:
            if r == src:
                continue
            rp = self.radios[r]["pos"]
            if _km(rp, s["pos"]) < 14 and _km(rp, station_pos) < 14:
                dd = _km(rp, station_pos)
                best = min(best or (dd, r), (dd, r))
        if best is None or rng.random() < 0.15:
            return None
        hops = 1 if rng.random() < 0.7 else 2
        return hops, int(best[1][-2:], 16), *self._signal(best[0])

    def _signal(self, km):
        snr = max(-19.0, min(12.0, 10 - km * 2.2 + self.rng.gauss(0, 1.5)))
        return round(snr * 4) / 4, int(-55 - km * 6 + self.rng.gauss(0, 3))


def build(path, now=None):
    """Write the demo mesh to a new database at `path` (replacing it). Returns the demo's own radio id."""
    import server  # the schema and Store; imported here so `import demo` stays cheap
    from analytics import STATION_TABLES

    rng = random.Random(SEED)
    now = now or time.time()
    start = now - DAYS * 86400
    path = Path(path)
    tmp = path.with_suffix(".building")
    for p in (tmp, Path(str(tmp) + "-wal"), Path(str(tmp) + "-shm")):
        p.unlink(missing_ok=True)
    m = _Mesh(rng)
    rows = {t: [] for t in STATION_TABLES}
    src = {}  # a synced station's rows carry their own row numbers, as if they came from it

    def put(table, station, **r):
        r["station"] = station
        if station != m.home:  # Hilltop and the car "synced" these rows to Demo Home
            src[(station, table)] = src.get((station, table), 0) + 1
            r["src_rowid"], r["received_via"] = src[(station, table)], "pair:demo"
        rows[table].append(r)

    gap = (start + 86400 + 2 * 3600, start + 86400 + 4 * 3600)  # Demo Home wasn't running then
    drive = (start + 2 * 86400 + 14 * 3600, start + 2 * 86400 + 15.5 * 3600)

    def car_at(t):
        """The car's position during the drive (along ROUTE at ~40 km/h), else None."""
        if not drive[0] <= t <= drive[1]:
            return None
        f = (t - drive[0]) / (drive[1] - drive[0]) * (len(ROUTE) - 1)
        i = min(int(f), len(ROUTE) - 2)
        a, b = ROUTE[i], ROUTE[i + 1]
        k = f - i
        return (PORTLAND[0] + a[0] + (b[0] - a[0]) * k, PORTLAND[1] + a[1] + (b[1] - a[1]) * k)

    def listeners(t):
        out = []
        if not gap[0] <= t < gap[1]:
            out.append((m.home, m.radios[m.home]["pos"]))
        out.append((m.hill, m.radios[m.hill]["pos"]))
        cp = car_at(t)
        if cp:
            out.append((m.car, cp))
        return out

    # ---- per-minute reports from each listening station's own radio (what makes an hour "logged")
    for minute in range(DAYS * 1440):
        t = start + minute * 60
        for st, _ in listeners(t):
            util = round(1.5 + 2.5 * (0.5 + 0.5 * math.sin((t % 86400) / 86400 * 2 * math.pi - 1.2)) + rng.random(), 2)
            put("telemetry_full", st, ts=t, node=st, kind="deviceMetrics",
                data=json.dumps({"batteryLevel": 101, "voltage": 4.2, "channelUtilization": util,
                                 "airUtilTx": round(util / 12, 3), "uptimeSeconds": minute * 60}))
            put("telemetry_full", st, ts=t + 1, node=st, kind="localStats",
                data=json.dumps({"uptimeSeconds": minute * 60, "channelUtilization": util, "numPacketsTx": minute // 20,
                                 "numPacketsRx": minute * 2, "numPacketsRxBad": minute // 50,
                                 "numOnlineNodes": 15 + round(4 * math.sin(t / 86400 * 2 * math.pi) + rng.random()),
                                 "numTotalNodes": 27}))

    # ---- node info, the first time each radio is heard (and one rename, for the identity history)
    for r in m.radios.values():
        put("node_info", m.home, ts=start + rng.uniform(0, 3600), node=r["id"], long_name=r["name"], short_name=r["short"],
            hw_model=r["hw"], role=r["role"], public_key=r["key"], is_licensed=0,
            data=json.dumps({"id": r["id"], "longName": r["name"], "shortName": r["short"], "hwModel": r["hw"], "role": r["role"]}))
    renamed = m.clients[1]
    put("node_info", m.home, ts=start + 1.5 * 86400, node=renamed, long_name="Juniper Ridge", short_name="JUNI",
        hw_model="HELTEC_V3", role="CLIENT", public_key=m.radios[renamed]["key"], is_licensed=0, data="{}")

    # ---- traffic: every radio sends on its own schedule; each listening station logs what it hears
    pkt = rng.getrandbits(24)
    senders = m.routers + m.clients + [m.hill]
    for s in senders:
        r = m.radios[s]
        phase = rng.uniform(0, 1800)
        t = start + phase
        while t < now - 60:
            pkt += 1
            kind = rng.choices(["pos", "tel", "info", "text", "enc", "fam"], [3, 4, 0.4, 0.5, 0.8, 0.15])[0]
            if kind == "pos" and s in m.hidden:
                kind = "tel"
            jitter = (rng.gauss(0, 0.0004), rng.gauss(0, 0.0004))
            lat, lon = round(r["pos"][0] + jitter[0], 5), round(r["pos"][1] + jitter[1], 5)
            batt = max(5, min(100, int(70 + 30 * math.sin(t / 40000 + phase))))
            if kind == "pos":
                port, summary, dec = "POSITION_APP", f"{lat:.4f}, {lon:.4f}", {"position": {
                    "latitudeI": int(lat * 1e7), "longitudeI": int(lon * 1e7), "latitude": lat, "longitude": lon,
                    "altitude": int(30 + rng.random() * 200), "precisionBits": 32 if rng.random() < 0.6 else 13}}
            elif kind == "tel":
                port, summary, dec = "TELEMETRY_APP", f"{batt}% {3.3 + batt / 110:.2f} V", {"telemetry": {"deviceMetrics": {
                    "batteryLevel": batt, "voltage": round(3.3 + batt / 110, 2), "channelUtilization": round(rng.uniform(1, 6), 2),
                    "airUtilTx": round(rng.uniform(0.05, 0.6), 3), "uptimeSeconds": int(t - start)}}}
            elif kind == "info":
                port, summary, dec = "NODEINFO_APP", r["name"], {"user": {"id": s, "longName": r["name"], "shortName": r["short"],
                                                                         "hwModel": r["hw"], "role": r["role"], "publicKey": r["key"]}}
            elif kind == "text":
                words = rng.choice(CHATTER)
                port, summary, dec = "TEXT_MESSAGE_APP", words, {"text": words}
            elif kind == "fam" and s in m.clients[:3]:
                words = rng.choice(FAMILY)
                port, summary, dec = "TEXT_MESSAGE_APP", words, {"text": words}
            else:
                port, summary, dec = "ENCRYPTED", None, None
            channel_hash = PUBLIC_HASH if kind != "fam" else 124
            for st, spos in listeners(t):
                if st == s:
                    continue
                h = m.heard(spos, s)
                if h is None:
                    continue
                hops, relay, snr, rssi = h
                ts = t + rng.uniform(0.1, 2.0)
                raw = {"from": int(s[1:], 16), "fromId": s, "toId": "^all", "id": pkt, "rxSnr": snr, "rxRssi": rssi,
                       "hopStart": 3, "hopLimit": 3 - hops, "relayNode": relay, "transportMechanism": "TRANSPORT_LORA"}
                if dec is not None:
                    raw["decoded"] = {"portnum": port, **dec}
                else:
                    raw["encrypted"] = base64.b64encode(rng.randbytes(40)).decode()
                ch = None if dec is None else (1 if kind == "fam" else 0)
                put("packets", st, ts=ts, from_id=s, to_id="^all", portnum=port, channel=ch, snr=snr, rssi=rssi, hops=hops,
                    via_mqtt=0, summary=summary, relay=relay, pkt_id=pkt, pki=0, raw=json.dumps(raw))
                for copy in range(1 + (hops > 0 and rng.random() < 0.4)):
                    put("rx_hops", st, ts=ts + copy * 0.8, from_id=s, to_id="^all", pkt_id=pkt, relay=relay, hops=hops,
                        hop_start=3, hop_limit=3 - hops, snr=round(snr - copy * 1.5, 2), rssi=rssi - copy * 4,
                        channel=channel_hash, directed=0, want_ack=0, next_hop=0, length=rng.randint(40, 120),
                        encrypted=1, transport=0)
                if hops == 0:
                    put("links", st, ts=ts, a=s, b=st, snr=snr, source="direct")
                if kind == "pos":
                    put("positions", st, ts=ts, node=s, lat=lat, lon=lon, alt=dec["position"]["altitude"],
                        precision_bits=dec["position"]["precisionBits"])
                elif kind == "tel":
                    tm = dec["telemetry"]["deviceMetrics"]
                    put("telemetry", st, ts=ts, node=s, battery=tm["batteryLevel"], voltage=tm["voltage"],
                        ch_util=tm["channelUtilization"], air_util=tm["airUtilTx"], uptime=tm["uptimeSeconds"])
                    put("telemetry_full", st, ts=ts, node=s, kind="deviceMetrics", data=json.dumps(tm))
                elif port == "TEXT_MESSAGE_APP":
                    put("messages", st, ts=ts, from_id=s, to_id="^all", channel=ch, text=dec["text"], pkt_id=pkt,
                        outgoing=0, encrypted=0)
            t += rng.uniform(900, 2400) * (0.6 if s in m.routers else 1)

    # ---- the car's own GPS fixes during its drive (a moving station: Coverage shows what it heard along the way)
    t = drive[0]
    while t <= drive[1]:
        cp = car_at(t)
        put("positions", m.car, ts=t, node=m.car, lat=round(cp[0], 6), lon=round(cp[1], 6), alt=60, precision_bits=32,
            source="own", fix_time=int(t))
        t += 30

    # ---- a few traceroutes from Demo Home to far radios
    for k, target in enumerate(rng.sample(m.clients, 4)):
        via = rng.choice(m.routers)
        put("traceroutes", m.home, ts=start + (k + 1) * 50000, target=target, status="ok", origin="manual",
            forward=json.dumps([{"id": via, "snr": round(rng.uniform(-5, 8), 2)}, {"id": target, "snr": round(rng.uniform(-12, 4), 2)}]),
            back=json.dumps([{"id": via, "snr": round(rng.uniform(-5, 8), 2)}, {"id": m.home, "snr": round(rng.uniform(-6, 6), 2)}]),
            done_ts=start + (k + 1) * 50000 + 20)

    # ---- the database itself
    store = server.Store(tmp)
    store.claim_station(m.home)
    for table, rs in rows.items():
        if not rs:
            continue
        cols = sorted(set().union(*rs))
        store.db.executemany(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                             [[r.get(c) for c in cols] for r in rs])
    hub = {m.hill: {"first": start, "last": now - 20, "rows": sum(n for (st, _), n in src.items() if st == m.hill), "tables": {}},
           m.car: {"first": drive[0], "last": drive[1], "rows": sum(n for (st, _), n in src.items() if st == m.car), "tables": {}}}
    meta = {m.hill: {"name": "Hilltop Station", "location": list(m.radios[m.hill]["pos"]), "version": server.VERSION,
                     "platform": "linux aarch64", "radioHw": "RAK4631", "radioFirmware": "2.7.26", "uptimeS": DAYS * 86400,
                     "backlog": 0, "received": now - 20},
            m.car: {"name": "Demo Car", "mobile": True, "version": server.VERSION, "radioHw": "TRACKER_T1000_E",
                    "radioFirmware": "2.7.26", "received": drive[1]}}
    private_hash = 124
    settings = {
        "sync_hub": hub, "station_meta": meta, "station_owners": {m.hill: "pair:demo", m.car: "pair:demo"},
        "radio_channels": {"ts": now, "channels": [
            {"index": 0, "role": "PRIMARY", "name": "LongFast", "hash": PUBLIC_HASH, "encrypted": True, "publicKey": True},
            {"index": 1, "role": "SECONDARY", "name": PRIVATE_NAME, "hash": private_hash, "encrypted": True, "publicKey": False}]},
        "radio_lora": {"ts": now, "region": "US", "preset": "LONG_FAST", "use_preset": True, "channel_name": "LongFast"},
        "demo": {"built": now, "seed": SEED, "home": m.home},
    }
    for k, v in settings.items():
        store.db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, json.dumps(v)))
    store.db.execute("INSERT INTO events (ts, kind, detail, station) VALUES (?, 'connected', ?, ?)", (start, json.dumps({
        "port": "demo", "local": m.home, "channels": [{"index": 0, "role": "PRIMARY", "settings": {"name": ""}},
                                                       {"index": 1, "role": "SECONDARY", "settings": {"name": PRIVATE_NAME}}]}), m.home))
    store.db.commit()
    store.db.close()
    for p in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        p.unlink(missing_ok=True)
    tmp.replace(path)
    for suffix in ("-wal", "-shm"):
        Path(str(tmp) + suffix).unlink(missing_ok=True)
    return m.home


def home_of(path):
    """The demo's own radio id, from a demo database (or None)."""
    import sqlite3
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        r = db.execute("SELECT value FROM settings WHERE key='demo'").fetchone()
        db.close()
        return json.loads(r[0])["home"] if r else None
    except Exception:  # noqa: BLE001
        return None


def stale(path, max_age_s=6 * 3600):
    """Rebuild when missing or older than this, so "the last 24 hours" always has data."""
    import sqlite3
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        r = db.execute("SELECT value FROM settings WHERE key='demo'").fetchone()
        db.close()
        return not r or time.time() - json.loads(r[0])["built"] > max_age_s
    except Exception:  # noqa: BLE001
        return True
