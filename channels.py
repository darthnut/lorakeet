"""Channel hashes and keys exactly as the firmware computes them, and which on-air channel fingerprints are
public (keys are used in memory only, never stored or returned).
"""
import json
import logging

import anatomy


log = logging.getLogger("meshdash")


# ---- on-air channel hashes, reimplemented from firmware src/mesh/Channels.cpp (v2.7.26):
# hash = xorHash(name) ^ xorHash(expanded key). Verified: LongFast + default key -> 0x08, as observed on air.
DEFAULT_PSK = bytes([0xd4, 0xf1, 0xbb, 0x3a, 0x20, 0x29, 0x07, 0x59, 0xf0, 0xbc, 0xff, 0xab, 0xcf, 0x4e, 0x69, 0x01])
PRESET_NAMES = {"SHORT_TURBO": "ShortTurbo", "SHORT_SLOW": "ShortSlow", "SHORT_FAST": "ShortFast",
                "MEDIUM_SLOW": "MediumSlow", "MEDIUM_FAST": "MediumFast", "LONG_SLOW": "LongSlow",
                "LONG_FAST": "LongFast", "LONG_TURBO": "LongTurbo", "LONG_MODERATE": "LongMod"}


def _xor(b):
    h = 0
    for x in b:
        h ^= x
    return h


def _expand_psk(psk):
    """Channels::getKey: 1-byte keys select a well-known default (0 = no encryption); short keys are
    zero-padded, which doesn't change an XOR hash."""
    if len(psk) == 1:
        return b"" if psk[0] == 0 else DEFAULT_PSK[:-1] + bytes([(DEFAULT_PSK[-1] + psk[0] - 1) & 0xFF])
    return bytes(psk)


def radio_channels(iface):
    """Every enabled channel on our radio with its on-air hash. Keys are used here, in memory only."""
    from meshtastic.protobuf import channel_pb2, config_pb2

    lora = iface.localNode.localConfig.lora
    preset = config_pb2.Config.LoRaConfig.ModemPreset.Name(lora.modem_preset)
    default_name = PRESET_NAMES.get(preset, "Invalid") if lora.use_preset else "Custom"
    chans = [c for c in (iface.localNode.channels or []) if c.role != channel_pb2.Channel.Role.DISABLED]
    primary_psk = next((bytes(c.settings.psk) for c in chans if c.role == channel_pb2.Channel.Role.PRIMARY), b"")
    out = []
    for c in chans:
        psk = bytes(c.settings.psk)
        if not psk and c.role == channel_pb2.Channel.Role.SECONDARY:
            psk = primary_psk  # unset secondary key falls back to the primary's
        key = _expand_psk(psk)
        name = c.settings.name or default_name
        out.append({"index": c.index, "role": channel_pb2.Channel.Role.Name(c.role), "name": name,
                    "hash": _xor(name.encode()) ^ _xor(key),
                    "encrypted": bool(key),
                    # 1-byte keys are the publicly known defaults: anyone can read those channels
                    "publicKey": len(psk) == 1 and psk[0] != 0})
    return out


def radio_lora(iface, chans):
    """Modulation + frequency settings (packet anatomy's radio layer). Nothing secret. The frequency slot
    comes from the primary channel's name."""
    from meshtastic.protobuf import config_pb2
    lora = iface.localNode.localConfig.lora
    L = config_pb2.Config.LoRaConfig
    return {"region": L.RegionCode.Name(lora.region), "preset": L.ModemPreset.Name(lora.modem_preset),
            "use_preset": lora.use_preset, "bandwidth": lora.bandwidth, "spread_factor": lora.spread_factor,
            "coding_rate": lora.coding_rate, "channel_num": lora.channel_num,
            "override_frequency": lora.override_frequency, "frequency_offset": lora.frequency_offset,
            "channel_name": next((c["name"] for c in chans if c["role"] == "PRIMARY"), None)}


def channel_keys(iface):
    """On-air channel hash -> AES key, from the live radio. In memory only: never stored or returned."""
    from meshtastic.protobuf import channel_pb2
    keys = {}
    chans = [c for c in (iface.localNode.channels or []) if c.role != channel_pb2.Channel.Role.DISABLED]
    primary_psk = next((bytes(c.settings.psk) for c in chans if c.role == channel_pb2.Channel.Role.PRIMARY), b"")
    for ch, c in zip(radio_channels(iface), chans):
        psk = bytes(c.settings.psk) or (primary_psk if c.role == channel_pb2.Channel.Role.SECONDARY else b"")
        key = anatomy._aes_key(_expand_psk(psk))
        if key:
            keys[ch["hash"]] = key
    return keys


PRESET_HASHES = {_xor(n.encode()) ^ _xor(DEFAULT_PSK) for n in PRESET_NAMES.values()}


def channel_fingerprints(store):
    """This radio's channels as on-air fingerprints only (never names or keys): {"publicHashes", "privateHashes"}.
    Stations send it in their report, so the hub can judge each station's traffic by that station's own channels."""
    r = store.query("SELECT value FROM settings WHERE key='radio_channels'")
    chans = json.loads(r[0]["value"]).get("channels", []) if r else []
    if not chans:
        return {}
    return {"publicHashes": sorted({c["hash"] for c in chans if c.get("publicKey")}),
            "privateHashes": sorted({c["hash"] for c in chans if c.get("encrypted") and not c.get("publicKey")})}


def public_channel_hashes(store, own=()):
    """station -> the on-air fingerprints of channels anyone can read, AS THAT STATION'S RADIO SEES THEM: every preset
    name with the well-known key, plus its channels with a public key, minus any fingerprint one of ITS private
    channels shares (a fingerprint is one byte, so a collision proves nothing). This hub's own radios use its channel
    list; other stations the fingerprints in their reports; a station that never reported them (an older Lorakeet, a
    peer's station) has none: nothing it logged is proven public (sync.public_keys)."""
    mine = channel_fingerprints(store)
    r = store.query("SELECT value FROM settings WHERE key='station_meta'")
    metas = json.loads(r[0]["value"]) if r else {}
    r = store.query("SELECT value FROM settings WHERE key='own_station'")
    own = set(own) | ({r[0]["value"]} if r else set())  # stored as the bare id

    def hashes(station):
        fp = mine if station in own else {k: (metas.get(station) or {}).get(k) for k in ("publicHashes", "privateHashes")}
        if fp.get("publicHashes") is None and fp.get("privateHashes") is None:
            return set() if station not in own else set(PRESET_HASHES)
        return (PRESET_HASHES | set(fp.get("publicHashes") or ())) - set(fp.get("privateHashes") or ())
    return hashes


def _preset_name(node):
    from meshtastic.protobuf import config_pb2
    lora = node.localConfig.lora
    return PRESET_NAMES.get(config_pb2.Config.LoRaConfig.ModemPreset.Name(lora.modem_preset), "Primary") if lora.use_preset else "Primary"
