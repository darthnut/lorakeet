"""The Radio page: check a radio's settings, change them safely, manage its channels, confirm logging works.

Everything here acts on the radio Lorakeet is already connected to (its meshtastic interface), so nobody has to
stop the logger and use the CLI. The page and every endpoint are for this computer only: backups hold the
radio's private key and share links hold channel keys.

- **Checklist** (`checklist`): each item says what it is, why it matters for logging, its current value, the
  choices offered and, when there's a clear answer, a recommendation. Lorakeet never picks a region: that's a
  legal setting for where the radio is.
- **Changes** (`apply`): validated against the item's own choices, written in one settings transaction (one
  reboot), always after a full backup. Refused when the radio's configuration didn't fully load: writing on
  top of an empty config resets the radio (old firmware with a newer library).
- **Backups** (`backup`): the same YAML as `meshtastic --export-config`, so `meshtastic --configure <file>`
  restores it. Private key included (that's what makes it a backup), so the folder and files are readable
  by this user only. Never served by the dashboard, never synced, not part of the database backups.
- **Channels**: a private channel with a random 256-bit key, shared through a QR code / link for the phone
  app or written to another radio on USB; channels added from someone else's link. Keys are never logged.
"""
import base64
import datetime
import os
import re
import secrets
import subprocess
import sys
from pathlib import Path

from meshtastic.protobuf import apponly_pb2, channel_pb2, config_pb2

L = config_pb2.Config.LoRaConfig
D = config_pb2.Config.DeviceConfig
P = config_pb2.Config.PositionConfig
ROLE = channel_pb2.Channel.Role

FLASHER = "https://flasher.meshtastic.org"
TESTED = (2, 7, 26)
MIN_FIRMWARE = (2, 5, 0)
CHANNEL_NAME_MAX = 11      # bytes, as the firmware stores it
SECTIONS = ("device", "lora", "position", "security")

REGION_NAMES = {"US": "United States (902-928 MHz)", "EU_868": "Europe 868 MHz", "EU_433": "Europe 433 MHz",
                "ANZ": "Australia / New Zealand 915 MHz", "NZ_865": "New Zealand 865 MHz", "CN": "China",
                "JP": "Japan", "KR": "Korea", "TW": "Taiwan", "RU": "Russia", "IN": "India", "TH": "Thailand",
                "UA_868": "Ukraine 868 MHz", "UA_433": "Ukraine 433 MHz", "MY_919": "Malaysia 919 MHz",
                "MY_433": "Malaysia 433 MHz", "SG_923": "Singapore", "PH_915": "Philippines 915 MHz",
                "PH_868": "Philippines 868 MHz", "PH_433": "Philippines 433 MHz", "BR_902": "Brazil",
                "LORA_24": "2.4 GHz (worldwide, special radios)"}

# id -> (config section, field, enum type or python type)
FIELDS = {
    "region": ("lora", "region", L.RegionCode),
    "preset": ("lora", "modem_preset", L.ModemPreset),
    "hop_limit": ("lora", "hop_limit", int),
    "tx": ("lora", "tx_enabled", bool),
    "role": ("device", "role", D.Role),
    "debug_log": ("security", "debug_log_api_enabled", bool),
    "gps_mode": ("position", "gps_mode", P.GpsMode),
    "gps_interval": ("position", "gps_update_interval", int),
    "led_off": ("device", "led_heartbeat_disabled", bool),
    "buzzer": ("device", "buzzer_mode", D.BuzzerMode),
}


def fw_tuple(s):
    nums = re.findall(r"\d+", s or "")[:3]
    return tuple(int(n) for n in nums) + (0,) * (3 - len(nums)) if nums else None


def config_complete(node):
    lc = getattr(node, "localConfig", None)
    return bool(lc) and all(lc.HasField(s) for s in SECTIONS)


def _value(section_msg, field, kind):
    v = getattr(section_msg, field)
    return kind.Name(v) if hasattr(kind, "Name") else kind(v)


def _o(value, label, note=""):
    return {"value": value, "label": label, "note": note}


def checklist(node, firmware, *, usb=True, mobile=False, has_base=False):
    """The settings worth checking for a logging radio, as items for the Radio page."""
    if not config_complete(node):
        return [{"id": "config", "title": "Radio settings", "status": "needed", "current": "not loaded",
                 "why": "The radio didn't send its full configuration, so nothing can be checked or changed safely "
                        "(writing on top of a partial configuration resets a radio). Update its firmware, then reconnect.",
                 "options": [], "recommended": None}]
    lc = node.localConfig
    cur = {k: _value(getattr(lc, sec), f, kind) for k, (sec, f, kind) in FIELDS.items()}
    items = []

    def add(id_, title, status, why, options=(), recommended=None, current=None, note=""):
        items.append({"id": id_, "title": title, "status": status, "why": why, "note": note,
                      "current": cur.get(id_) if current is None else current,
                      "options": list(options), "recommended": recommended})

    # firmware (nothing to change here: flashing is the web flasher's job)
    fw = fw_tuple(firmware)
    if fw is None:
        add("firmware", "Firmware", "info", "The radio didn't report its firmware version.", current="unknown")
    elif fw < MIN_FIRMWARE:
        add("firmware", "Firmware", "needed", f"Too old for Lorakeet (2.5 or newer; tested on {'.'.join(map(str, TESTED))}). "
            f"Update it with the Meshtastic web flasher: {FLASHER}", current=firmware)
    elif fw >= (2, 8, 0):
        add("firmware", "Firmware", "info", "Firmware 2.8: Lorakeet is prepared for it but tested on 2.7.26. "
            "If per-reception logging stops, Lorakeet raises an alert.", current=firmware)
    else:
        add("firmware", "Firmware", "ok", f"Supported (tested on {'.'.join(map(str, TESTED))}).", current=firmware)

    regions = [_o(n, REGION_NAMES.get(n, n.replace("_", " "))) for n in L.RegionCode.keys() if n != "UNSET"]
    if cur["region"] == "UNSET":
        add("region", "Region", "needed", "Not set: the radio can't transmit or tune in until it knows which radio "
            "rules apply. Pick the region where the radio is (it's a legal setting, so Lorakeet won't guess).", regions)
    else:
        add("region", "Region", "ok", "Sets the frequencies and power the radio may use. Only change it if the radio "
            "moved to another country.", regions)

    if not usb:
        add("debug_log", "Per-reception detail", "info", "Radios send their debug log only over USB, so a network "
            "radio gives every decoded packet but not per-reception detail (duplicates, relays, airtime).",
            [_o(True, "On"), _o(False, "Off")])
    elif cur["debug_log"]:
        add("debug_log", "Per-reception detail (debug log)", "ok", "On: Lorakeet logs every copy of every packet "
            "the radio hears, with its relay, hops and signal.", [_o(True, "On"), _o(False, "Off")])
    else:
        add("debug_log", "Per-reception detail (debug log)", "suggest", "Off: Lorakeet still logs every decoded packet, "
            "but not each over-the-air copy (relays, duplicates, airtime, the replay's paths). Turning it on sends the "
            "radio's debug log to Lorakeet over USB; nothing extra goes on the air.",
            [_o(True, "On"), _o(False, "Off")], recommended=True)

    roles = [_o("CLIENT", "Client", "relays other people's packets (the default)"),
             _o("CLIENT_MUTE", "Client, mute", "listens and logs but never relays")]
    if cur["role"] not in ("CLIENT", "CLIENT_MUTE"):
        roles.append(_o(cur["role"], cur["role"].replace("_", " ").title(), "current"))
    if cur["role"] == "CLIENT" and has_base:
        add("role", "Role", "suggest", "You have a base station that relays for this area. A logging radio beside it "
            "doesn't need to relay too: Client, mute keeps it listening without adding copies to the air.",
            roles, recommended="CLIENT_MUTE")
    elif cur["role"] in ("CLIENT", "CLIENT_MUTE"):
        add("role", "Role", "ok", "Client relays for others; Client, mute only listens. Choose mute if another of your "
            "radios nearby already relays.", roles)
    else:
        add("role", "Role", "info", f"{cur['role'].replace('_', ' ').title()}: a role for a radio with a job in the mesh. "
            "Keep it if that's this radio's job; a dedicated logger is usually Client or Client, mute.", roles)

    hops = [_o(n, str(n)) for n in range(1, 8)]
    if cur["hop_limit"] < 3:
        add("hop_limit", "Hop limit", "suggest", f"{cur['hop_limit']}: packets you send die after "
            f"{cur['hop_limit']} relay{'s' if cur['hop_limit'] != 1 else ''}. 3 is the default.", hops, recommended=3)
    else:
        add("hop_limit", "Hop limit", "ok", "How many relays your own packets (messages, traceroutes, replies) may take. "
            "3 is the default; 4 helps when your other radios are at the edge of reach. It doesn't affect listening.", hops)

    if not lc.lora.use_preset:
        add("preset", "Modem settings", "info", "Custom modem settings: it only hears radios using exactly the same ones.",
            current="custom")
    else:
        presets = [_o("LONG_FAST", "LongFast", "what most meshes use")]
        if cur["preset"] != "LONG_FAST":
            presets.append(_o(cur["preset"], cur["preset"].replace("_", " ").title(), "current"))
        if cur["preset"] == "LONG_TURBO":
            add("preset", "Modem preset", "suggest", "LongTurbo (firmware 2.8's default for new US radios) can't hear "
                "LongFast at all. If your local mesh uses LongFast, switch so this radio hears it.", presets,
                recommended="LONG_FAST")
        elif cur["preset"] == "LONG_FAST":
            add("preset", "Modem preset", "ok", "LongFast, what most meshes use. A radio only hears radios on the same preset.",
                presets)
        else:
            add("preset", "Modem preset", "info", "A radio only hears radios on the same preset: keep this if your local "
                "mesh uses it.", presets)

    if not cur["tx"]:
        add("tx", "Transmitting", "info", "Off: fine for a pure listener, but messages, traceroutes and replies to lk "
            "commands can't go out.", [_o(True, "On"), _o(False, "Off")])

    gps_modes = [_o("ENABLED", "On"), _o("DISABLED", "Off"), _o("NOT_PRESENT", "No GPS on this radio")]
    intervals = [_o(n, f"{n} s") for n in (15, 30, 60, 120, 300)]
    if cur["gps_interval"] not in [o["value"] for o in intervals]:
        intervals.append(_o(cur["gps_interval"], f"{cur['gps_interval']} s" if cur["gps_interval"] else "default (120 s)"))
    if mobile:
        add("gps_mode", "GPS", "ok" if cur["gps_mode"] == "ENABLED" else "suggest",
            "This station moves ([station] mobile), so its GPS places what it hears along the route.",
            gps_modes, recommended=None if cur["gps_mode"] == "ENABLED" else "ENABLED")
        slow = not cur["gps_interval"] or cur["gps_interval"] > 30
        add("gps_interval", "GPS fix interval", "suggest" if slow else "ok",
            "How often the radio takes a fix. At 120 s (the default) a car moves 2 km+ between fixes; 30 s or less "
            "places receptions within a few hundred metres.", intervals, recommended=30 if slow else None)
    else:
        add("gps_mode", "GPS", "info", "For a station that stays put, its location in Lorakeet's settings is enough. "
            "If the radio has no GPS at all, \"No GPS\" saves it searching for one at every start.", gps_modes)

    add("led_off", "Status LED", "info", "The blinking heartbeat light. Off is easier on a desk or bedroom.",
        [_o(False, "Blinking"), _o(True, "Off")])
    add("buzzer", "Buzzer", "info", "Beeps on messages and buttons, on radios that have one.",
        [_o("ALL_ENABLED", "On"), _o("DISABLED", "Off"), _o("SYSTEM_ONLY", "System sounds only")]
        + ([] if cur["buzzer"] in ("ALL_ENABLED", "DISABLED", "SYSTEM_ONLY") else [_o(cur["buzzer"], cur["buzzer"].title())]))
    return items


class RadioError(Exception):
    pass


def plan(items, changes):
    """Validate {item id: value} against the checklist's own choices; return {id: value} for real changes."""
    by_id = {i["id"]: i for i in items}
    out = {}
    for k, v in (changes or {}).items():
        item = by_id.get(k)
        if item is None or k not in FIELDS:
            raise RadioError(f"unknown setting: {k}")
        if v not in [o["value"] for o in item["options"]]:
            raise RadioError(f"{item['title']}: {v!r} isn't one of the choices")
        if v != item["current"]:
            out[k] = v
    return out


def check_names(long_name, short_name):
    long_name, short_name = (long_name or "").strip(), (short_name or "").strip()
    if not long_name or len(long_name.encode()) > 39:
        raise RadioError("the long name needs 1 to 39 bytes")
    if not short_name or len(short_name) > 4 or len(short_name.encode()) > 4:
        raise RadioError("the short name needs 1 to 4 characters")
    return long_name, short_name


def set_fields(node, changes):
    """Edit the in-memory config; return the sections to write."""
    sections = []
    for k, v in changes.items():
        sec, field, kind = FIELDS[k]
        msg = getattr(node.localConfig, sec)
        setattr(msg, field, kind.Value(v) if hasattr(kind, "Value") else kind(v))
        if sec not in sections:
            sections.append(sec)
    return sections


def apply(iface, changes, names=None):
    """Write the changes in one settings transaction. The caller has made a backup first."""
    node = iface.localNode
    if not config_complete(node):
        raise RadioError("the radio's settings didn't fully load; refusing to write (it could reset the radio)")
    sections = set_fields(node, changes)
    node.beginSettingsTransaction()
    for s in sections:
        node.writeConfig(s)
    if names:
        node.setOwner(long_name=names[0], short_name=names[1])
    node.commitSettingsTransaction()
    return sections


# ---- backups

def _private_dir(path):
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        _icacls(path, "(OI)(CI)F")
    else:
        os.chmod(path, 0o700)


def _icacls(path, perm):
    """This user only: drop inherited permissions and grant the current user full control."""
    user = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}".lstrip("\\")
    subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:{perm}"],
                   check=True, capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def write_private(path, text):
    _private_dir(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    if sys.platform == "win32":
        _icacls(path, "F")


class _ExportView:
    """What the library's export_config reads, minus canned messages and ringtone (each a round trip to the
    radio that can hang)."""
    def __init__(self, iface):
        self._i, self.localNode = iface, iface.localNode

    def getLongName(self):
        return self._i.getLongName()

    def getShortName(self):
        return self._i.getShortName()

    def getMyNodeInfo(self):
        return self._i.getMyNodeInfo()

    def getCannedMessage(self):
        return None

    def getRingtone(self):
        return None


def backup(iface, folder, node_id, reason, port=None):
    """Save the radio's full settings (keys included) where only this user can read them. Returns the path."""
    from meshtastic.__main__ import export_config
    text = export_config(_ExportView(iface))
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = Path(folder) / f"{node_id.lstrip('!')}-{stamp}-{reason}.yaml"
    head = (f"# Lorakeet backup of radio {node_id}, {datetime.datetime.now():%Y-%m-%d %H:%M} ({reason}).\n"
            "# Holds the radio's private key and channel keys: keep it private.\n"
            f"# Restore: meshtastic --port {port or '<port>'} --configure \"{path.name}\"\n")
    write_private(path, head + text)
    return path


def backups(folder, node_id=None):
    p = Path(folder)
    if not p.is_dir():
        return []
    files = sorted(p.glob("*.yaml"), key=lambda f: f.stat().st_mtime, reverse=True)
    return [{"name": f.name, "ts": f.stat().st_mtime} for f in files
            if node_id is None or f.name.startswith(node_id.lstrip("!") + "-")]


# ---- channels

def key_kind(psk, role):
    psk = bytes(psk)
    if not psk:
        return "primary" if role == ROLE.SECONDARY else "none"
    if len(psk) == 1:
        return "none" if psk[0] == 0 else "public"
    return "private"


def channel_list(node, preset_name="LongFast"):
    out = []
    for c in node.channels or []:
        if c.role == ROLE.DISABLED:
            continue
        kind = key_kind(c.settings.psk, c.role)
        out.append({"index": c.index, "role": ROLE.Name(c.role), "name": c.settings.name or preset_name,
                    "key": kind, "bits": len(bytes(c.settings.psk)) * 8 if kind == "private" else 0,
                    "precision": c.settings.module_settings.position_precision,
                    "shareable": kind == "private"})
    return out


def new_channel(name, precision=32):
    name = (name or "").strip()
    if not name or len(name.encode()) > CHANNEL_NAME_MAX or not re.fullmatch(r"[A-Za-z0-9_\-]+", name):
        raise RadioError(f"a channel name needs 1 to {CHANNEL_NAME_MAX} letters, digits, - or _")
    s = channel_pb2.ChannelSettings(name=name, psk=secrets.token_bytes(32))
    s.module_settings.position_precision = precision
    return s


def share_url(settings):
    cs = apponly_pb2.ChannelSet()
    cs.settings.append(settings)
    return "https://meshtastic.org/e/?add=true#" + base64.urlsafe_b64encode(cs.SerializeToString()).decode().rstrip("=")


def parse_url(url):
    """The channels in a Meshtastic channel link (https://meshtastic.org/e/#...)."""
    url = (url or "").strip()
    if "#" not in url or "meshtastic.org/e/" not in url:
        raise RadioError("that isn't a Meshtastic channel link (https://meshtastic.org/e/#...)")
    frag = url.split("#", 1)[1]
    try:
        cs = apponly_pb2.ChannelSet()
        cs.ParseFromString(base64.urlsafe_b64decode(frag + "=" * (-len(frag) % 4)))
    except Exception as e:  # noqa: BLE001
        raise RadioError("the link is damaged (couldn't read its channels)") from e
    if not cs.settings:
        raise RadioError("the link has no channels in it")
    return list(cs.settings)


def add_channels(node, settings_list):
    """Add each channel in a free secondary slot. Same name and key already there: skipped. Same name with a
    different key: refused, never replaced. The primary channel and LoRa settings are never touched.
    Returns [(name, index, "added" | "already there")]."""
    active = [c for c in node.channels if c.role != ROLE.DISABLED]
    plan_ = []
    free = [c for c in node.channels if c.index > 0 and c.role == ROLE.DISABLED]
    for s in settings_list:
        if not s.name:
            raise RadioError("the link's channel has no name (a primary channel): add it in the Meshtastic app instead")
        same = [c for c in active if c.settings.name == s.name]
        if same:
            if bytes(same[0].settings.psk) != bytes(s.psk):
                raise RadioError(f"{s.name!r} is already on this radio with a different key; not replacing it")
            plan_.append((s, same[0], "already there"))
            continue
        if not free:
            raise RadioError("no free channel slot (a radio has 8)")
        plan_.append((s, free.pop(0), "added"))
    out = []
    for s, slot, what in plan_:
        if what == "added":
            slot.settings.CopyFrom(s)
            slot.role = ROLE.SECONDARY
            node.writeChannel(slot.index)
        out.append((s.name, slot.index, what))
    return out


def find_channel(node, index):
    for c in node.channels or []:
        if c.index == index and c.role != ROLE.DISABLED:
            return c
    raise RadioError("no such channel")


def qr_svg(text):
    import segno
    return segno.make(text, error="m").svg_inline(scale=5, border=3, dark="#000", light="#fff")


# ---- connecting and logging

def connect_hint(error, platform=sys.platform):
    """A plain-words reason for a connection failure."""
    e = (error or "").lower()
    if not e:
        return None
    if "permission" in e and platform.startswith("linux"):
        return ("Linux won't let Lorakeet open the port: add your user to the dialout group "
                "(sudo usermod -aG dialout $USER), then log out and in again.")
    if any(s in e for s in ("access is denied", "permissionerror", "resource busy", "could not exclusively lock",
                            "being used", "busy")):
        return ("Another program is using the radio's port. Close the Meshtastic app or CLI, a serial monitor, "
                "or a browser tab with the web flasher or web client, and Lorakeet connects by itself.")
    if "timed out" in e or "timeout" in e:
        return ("The radio didn't answer. It may still be starting: wait a few seconds. If it keeps happening, "
                "unplug it and plug it back in, or try another cable.")
    if "could not open port" in e or "filenotfound" in e or "no such file" in e:
        return "The port went away: the radio was unplugged or restarted. Lorakeet reconnects when it's back."
    return None


NO_PORT_HINT = ("No radio found on USB. Check the cable carries data (some only charge), and on Windows that the "
                "radio's USB driver is installed (CP210x or CH9102 for many ESP32 boards; nRF52 boards need none).")


# ---- this station's location (the Radio page's "This station", and the setup page)

GEO_ROUGH_M = 1000      # a browser location worse than this is a guess from the internet connection, not a place
FAR_KM = 150            # a station this far from the middle of the radios it hears is almost certainly misplaced
FAR_MIN_RADIOS = 3


def location_check(loc, points):
    """Is `loc` ([lat, lon]) far from the radios this station hears (`points`: [(lat, lon)], their positions)?
    None when it looks fine or there's too little to judge; else {distanceKm, radios, middle: [lat, lon]}.
    Uses the median, so a few radios far away (or wrongly placed) don't move the middle."""
    from analytics import dist_m
    pts = [(float(a), float(b)) for a, b in points if a is not None and b is not None and (a, b) != (0, 0)]
    if not loc or len(pts) < FAR_MIN_RADIOS:
        return None
    lats, lons = sorted(p[0] for p in pts), sorted(p[1] for p in pts)
    mid = (lats[len(lats) // 2], lons[len(lons) // 2])
    km = dist_m(float(loc[0]), float(loc[1]), *mid) / 1000
    return {"distanceKm": round(km), "radios": len(pts), "middle": [round(mid[0], 3), round(mid[1], 3)]} if km > FAR_KM else None
