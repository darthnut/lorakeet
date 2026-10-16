"""The demo's MeshCore side (demo.py): a made-up MeshCore mesh around the same made-up Portland one, logged by "Demo
MeshCore", a second radio in Demo Home's box (several radios, one Lorakeet).

Built from real MeshCore frames, byte for byte (meshcore_packet's layout), so every view treats it like a live
MeshCore station's: receptions with their repeater paths (most packets heard more than once, by different routes),
first hearings in packets, adverts giving names and places, the radio's counters, and the messages it can read.
Public channel messages are really encrypted with the Public channel's published key, so packet anatomy decrypts
them; the #demo hashtag channel's open when its name is typed there. Direct messages, ACKs and returned paths stay
ciphertext, as they would for any listener. Everything is invented: keys, names, places, words.
"""
import json

import meshcore_anatomy
import meshcore_packet

STATION = ("Demo MeshCore", (0.012, 0.035))   # same box as Demo Home (its offset from PORTLAND)
REPEATERS = [("Crest Repeater", (-0.020, -0.050)), ("Rocky Butte Rptr", (0.040, 0.130)), ("Sellwood Rptr", (-0.060, 0.010))]
CHATS = ["Ada", "Bram", "Corvid", "Dune", "Ember", "Fjord"]
HASHTAG = "demo"   # the hashtag channel a visitor can open in packet anatomy by typing its name
PUBLIC = ["Morning all", "Anyone near the river?", "Testing range from the bridge", "Heard you via Crest",
          "Repeater on the butte is up", "Great coverage today", "Back on the air", "Signal check please",
          "Got you loud and clear", "Heading out, see you later"]
TAGGED = ["Meetup at the park, 6pm", "Bring a spare antenna", "Who's in for Saturday?"]
TYPES = {"chat": 1, "repeater": 2}


def _frame(ptype, payload, path=(), route=1):
    """header (route, type, version 0), path length (1-byte hashes), path, payload."""
    return bytes([(ptype << 2) | route, len(path)]) + bytes(path) + payload


def _advert(key, kind, name, pos, ts, rng):
    flags = TYPES[kind] | 0x10 | 0x80
    return (key + int(ts).to_bytes(4, "little") + rng.randbytes(64) + bytes([flags])
            + round(pos[0] * 1e6).to_bytes(4, "little", signed=True) + round(pos[1] * 1e6).to_bytes(4, "little", signed=True)
            + name.encode())


def add(rng, put, home_pos, portland, start, now, logging, km, signal):
    """Write the MeshCore station's rows through demo.build's put(). `logging(t)` = whether Demo Home's box was running
    then; km/signal are demo's distance and signal models. Returns (station id, hub entry, station report, the radio's
    SELF_INFO settings)."""
    key = rng.randbytes(32)
    station = "mc:" + key[:6].hex()
    radios = {}   # id -> {key, name, kind, pos}

    def radio(name, kind, off):
        k = rng.randbytes(32)
        rid = "mc:" + k[:6].hex()
        radios[rid] = {"key": k, "name": name, "kind": kind, "pos": (portland[0] + off[0], portland[1] + off[1])}
        return rid

    reps = [radio(n, "repeater", off) for n, off in REPEATERS]
    chats = [radio(n, "chat", (rng.uniform(-0.08, 0.08), rng.uniform(-0.14, 0.14))) for n in CHATS]
    rows, rx_n = 0, 0
    self_info = {"name": STATION[0], "radio_freq": 910.525, "radio_bw": 62.5, "radio_sf": 7, "radio_cr": 5, "tx_power": 22}
    heard_air = []   # (ts, seconds on air) of every reception: what the radio's receive-airtime counter adds up

    def hearings(src_pos, t, sender=None):
        """How the station hears a packet from src_pos: [(ts, path, snr, rssi)], first first. Flooded packets come
        direct (when near) and through each repeater in range of both ends (never the sender itself)."""
        out = []
        others = [r for r in reps if r != sender]
        d = km(home_pos, src_pos)
        if d < 5:
            out.append((t + rng.uniform(0.1, 0.5), [], *signal(d)))
        for r in others:
            rp = radios[r]["pos"]
            if km(rp, src_pos) < 15 and km(rp, home_pos) < 15 and rng.random() < 0.85:
                out.append((t + rng.uniform(0.6, 2.5), [radios[r]["key"][0]], *signal(km(rp, home_pos))))
        if out and rng.random() < 0.25:   # sometimes also via two repeaters
            a, b = rng.sample(others, 2)
            out.append((t + rng.uniform(2.5, 4.0), [radios[a]["key"][0], radios[b]["key"][0]],
                        *signal(km(radios[b]["pos"], home_pos))))
        return sorted(out)

    def packet(ptype, payload, src_pos, t, sender=None, advert=None):
        nonlocal rows, rx_n
        if not logging(t):
            return
        first = True
        for ts, path, snr, rssi in hearings(src_pos, t, sender):
            frame = _frame(ptype, payload, path)
            pkt = meshcore_packet.parse(frame)
            heard_air.append((ts, meshcore_anatomy._radio(self_info, len(frame))["airtimeMs"] / 1000))
            put("mc_rx", station, ts=ts, snr=snr, rssi=rssi, length=len(frame), route=pkt["route"], payload_type=ptype,
                payload_name=pkt["payloadName"], version=0, hop_bytes=1, hops=len(path),
                path=",".join(f"{h:02x}" for h in path), packet_hash=pkt["hash"], from_id=sender, frame=frame.hex())
            rx_n += 1
            if first:   # the first hearing: a packets row, as the link logs it
                first = False
                summary = (f"advert: {advert['name']} ({advert['type']})" if advert else
                           f"{pkt['payloadName'].lower()} ({pkt['route']})") + (
                    f" via {'>'.join(f'{h:02x}' for h in path)}" if path else "")
                put("packets", station, ts=ts, from_id=sender, to_id=None, portnum=f"MC_{pkt['payloadName']}",
                    channel=None, snr=snr, rssi=rssi, hops=len(path), summary=summary, pkt_id=int(pkt["hash"][:13], 16),
                    raw=json.dumps({"meshcore": {k: v for k, v in pkt.items() if k != "payload"}, "advert": advert,
                                    "frame": frame.hex()}))
                rows += 1

    # adverts: repeaters every few hours, chat radios now and then; each one names and places its radio
    seen = set()
    for rid, r in radios.items():
        t = start + rng.uniform(0, 3 * 3600)
        while t < now - 60:
            payload = _advert(r["key"], r["kind"], r["name"], r["pos"], t, rng)
            adv = meshcore_packet.parse_advert(payload)
            packet(meshcore_packet.ADVERT, payload, r["pos"], t, sender=rid, advert=adv)
            if logging(t) and rid not in seen:
                seen.add(rid)
                put("node_info", station, ts=t + 1, node=rid, long_name=r["name"], short_name=None, hw_model=None,
                    role=r["kind"], public_key=r["key"].hex(), data=json.dumps({"advertTime": int(t)}))
            if logging(t):
                put("positions", station, ts=t + 1, node=rid, lat=round(r["pos"][0], 6), lon=round(r["pos"][1], 6),
                    alt=None, precision_bits=32, source="advert")
            t += rng.uniform(3, 5) * 3600 if r["kind"] == "repeater" else rng.uniform(6, 12) * 3600

    # channel messages: Public conversations (readable: the station's radio has the Public channel too) and #demo
    secrets = {"Public": meshcore_anatomy.PUBLIC_SECRET, HASHTAG: meshcore_anatomy.hashtag_secret(HASHTAG)}
    t = start + 1800
    while t < now - 600:
        tagged = rng.random() < 0.2
        for _ in range(rng.randint(1, 3)):
            who = radios[rng.choice(chats)]
            text = f"{who['name']}: {rng.choice(TAGGED if tagged else PUBLIC)}"
            payload = meshcore_anatomy.seal_channel_message(secrets[HASHTAG if tagged else "Public"], t, text)
            packet(5, payload, who["pos"], t)
            if logging(t) and not tagged:   # the station's radio reads Public; #demo isn't one of its channels
                put("messages", station, ts=t + 1, from_id=None, to_id="^all", channel=0, text=text, outgoing=0, encrypted=0)
            t += rng.uniform(60, 600)
        t += rng.uniform(1.5, 4) * 3600

    # direct messages between chat radios, each acknowledged: ciphertext only, as for any listener
    t = start + 5400
    while t < now - 600:
        a, b = rng.sample(chats, 2)
        pa = radios[a]["pos"]
        body = bytes([radios[b]["key"][0], radios[a]["key"][0]]) + rng.randbytes(2) + rng.randbytes(16 * rng.randint(1, 3))
        packet(2, body, pa, t)
        packet(3, rng.randbytes(4), radios[b]["pos"], t + rng.uniform(3, 8))
        if rng.random() < 0.5:   # the path back, so later messages go direct
            packet(8, bytes([radios[a]["key"][0], radios[b]["key"][0]]) + rng.randbytes(2) + rng.randbytes(16),
                   radios[b]["pos"], t + rng.uniform(8, 15))
        t += rng.uniform(2, 6) * 3600

    # the station's own radio: who it is (once), its counters every five minutes while logging
    put("node_info", station, ts=start + 5, node=station, long_name=STATION[0], short_name=None, hw_model=None,
        role="self", public_key=key.hex(), data=None)
    heard_air.sort()
    t, i, rx_air = start, 0, 0.0
    while t < now:
        while i < len(heard_air) and heard_air[i][0] <= t:
            rx_air += heard_air[i][1]
            i += 1
        if logging(t):
            put("telemetry_full", station, ts=t, node=station, kind="meshcore_stats_core",
                data=json.dumps({"battery_mv": 4150, "uptime_secs": int(t - start), "queue_len": 0}))
            # its airtime counters (seconds since boot): receive = what it heard, plus a little it couldn't decode
            put("telemetry_full", station, ts=t, node=station, kind="meshcore_stats_radio",
                data=json.dumps({"noise_floor": -112, "rx_air_secs": int(rx_air * 1.08), "tx_air_secs": 0}))
        t += 300
    hub = {"first": start, "last": now - 20, "rows": rows + rx_n, "tables": {}}
    report = {"name": STATION[0], "radioHw": "MeshCore companion", "radioFirmware": "MeshCore 1.17", "backlog": 0,
              "received": now - 20, "uptimeS": int(now - start)}
    return station, hub, report, self_info
