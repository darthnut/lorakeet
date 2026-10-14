"""The Radio page's Advanced section: every setting the radio reports, edited in one batch.

The Meshtastic apps write each settings page on its own, and the radio restarts after each. Here any number of
changes, across every section, are validated together and written in ONE settings transaction (one backup, one
restart), then read back. The checklist, the names and this section share that one batch (server.py merges them).

- `schema(node)`: every section the radio reported (config and modules), each field with its current value, type,
  choices, help and warnings. Generated from the protobuf definitions, so new firmware fields appear on their own.
- Secrets are write-only: `SECRETS` show only whether they're set, and can be replaced; their values are never
  returned, logged or echoed. The radio's keys (`LOCKED`) can't be changed here.
- `RISKS`: settings that can cut Lorakeet off from the radio, break logging, transmit a lot, put traffic on the
  internet or are legal settings. Shown in red with the reason, and the page asks for a confirmation.
- `plan(node, changes)` validates {path: value} (types, ranges, choices, write-only rules, one-way switches) and
  drops no-ops; `apply(node, plan)` sets them on the node's config copies. radio_setup.apply writes the sections.
"""
import ipaddress
import math
import re

from google.protobuf.descriptor import FieldDescriptor as F

# Sections the meshtastic library can write (node.writeConfig): anything else is shown read-only.
WRITABLE = {
    "config": ("device", "position", "power", "network", "display", "lora", "bluetooth", "security"),
    "module": ("mqtt", "serial", "external_notification", "store_forward", "range_test", "telemetry", "canned_message",
               "audio", "remote_hardware", "neighbor_info", "detection_sensor", "ambient_lighting", "paxcounter",
               "traffic_management"),
}
TITLES = {
    "config.device": "Device", "config.position": "Position", "config.power": "Power", "config.network": "Network (Wi-Fi, Ethernet)",
    "config.display": "Screen", "config.lora": "LoRa (radio)", "config.bluetooth": "Bluetooth", "config.security": "Security",
    "module.mqtt": "MQTT (internet gateway)", "module.serial": "Serial module", "module.external_notification": "External notification (buzzer, LED, vibration)",
    "module.store_forward": "Store and forward", "module.range_test": "Range test", "module.telemetry": "Telemetry",
    "module.canned_message": "Canned messages", "module.audio": "Audio", "module.remote_hardware": "Remote hardware",
    "module.neighbor_info": "Neighbor info", "module.detection_sensor": "Detection sensor", "module.ambient_lighting": "Ambient lighting",
    "module.paxcounter": "Paxcounter", "module.traffic_management": "Traffic management", "module.statusmessage": "Status message",
    "module.tak": "TAK",
}
ABOUT = {
    "config.lora": "What the radio transmits and listens on. A radio only hears radios with the same region, preset and channel.",
    "config.security": "The radio's keys (shown, never changed here) and who may change its settings.",
    "module.mqtt": "Bridges the mesh to an MQTT server on the internet. Off unless you mean to publish your traffic.",
    "module.telemetry": "How often radios report their battery and sensors. Shorter intervals use more airtime.",
    "module.serial": "Uses a serial port for another device. On some boards it can take over the USB connection Lorakeet uses.",
}
LOCKED = {"config.security.public_key", "config.security.private_key", "config.security.admin_key"}
SECRETS = {"config.network.wifi_psk": "password", "module.mqtt.password": "password", "config.bluetooth.fixed_pin": "pin"}
ONE_WAY = {"config.security.is_managed": False}   # may be switched to this value only
IP_FIELDS = re.compile(r"^config\.network\.ipv4_config\.")
MAXLEN = {  # the firmware's string sizes (nanopb options), less the terminator
    "config.device.tzdef": 64, "config.network.wifi_ssid": 32, "config.network.wifi_psk": 64, "config.network.ntp_server": 32,
    "config.network.rsyslog_server": 32, "module.mqtt.address": 63, "module.mqtt.username": 63, "module.mqtt.password": 63,
    "module.mqtt.root": 31, "module.canned_message.allow_input_source": 15, "module.detection_sensor.name": 19,
}

# Settings that need a second thought. Shown in red with the reason; the review asks to confirm them.
RISKS = {
    "config.security.serial_enabled": "Off: the radio stops talking over USB, and Lorakeet can't reach it any more "
                                      "(only Bluetooth or Wi-Fi apps can turn it back on).",
    "config.device.serial_enabled": "Off: older firmware stops talking over USB, and Lorakeet can't reach it any more.",
    "config.security.is_managed": "Managed mode: the radio refuses setting changes from apps and Lorakeet; only remote "
                                  "admin can change it. It can be turned off here, never on.",
    "config.security.debug_log_api_enabled": "Off: Lorakeet loses per-reception detail (relays, duplicates, airtime).",
    "config.security.admin_channel_enabled": "Allows the old, less secure admin channel to change this radio.",
    "config.lora.region": "A legal setting: the frequencies and power allowed where the radio is. Only the region "
                          "you're actually in.",
    "config.lora.tx_enabled": "Off: the radio can't send anything (messages, traceroutes, replies).",
    "config.lora.use_preset": "Off: the custom modem settings below apply, and the radio only hears radios using exactly the same.",
    "config.lora.modem_preset": "A radio only hears radios on the same preset: changing it can take this radio off your mesh.",
    "config.lora.bandwidth": "Custom modem setting: the radio only hears radios using exactly the same.",
    "config.lora.spread_factor": "Custom modem setting: the radio only hears radios using exactly the same.",
    "config.lora.coding_rate": "Custom modem setting: the radio only hears radios using exactly the same.",
    "config.lora.channel_num": "Moves the radio to another frequency slot: it stops hearing radios on the usual one.",
    "config.lora.override_frequency": "Moves the radio to another frequency: it stops hearing your mesh, and may be illegal.",
    "config.lora.frequency_offset": "Shifts the frequency: only for correcting a known crystal error.",
    "config.lora.override_duty_cycle": "Ignores the legal duty-cycle limit (EU): don't, unless you're licensed to.",
    "config.lora.tx_power": "Transmit power in dBm (0 = the region's legal maximum). Above the legal limit is illegal.",
    "config.lora.hop_limit": "How many relays your own packets may take. Above 3 adds airtime for everyone.",
    "config.lora.config_ok_to_mqtt": "Allows gateways to put the packets you send on public MQTT servers.",
    "config.device.role": "Router and repeater roles relay everything and change how the mesh routes: only for "
                          "well-placed, always-on radios.",
    "config.device.rebroadcast_mode": "Changes what this radio relays for others (None: it relays nothing).",
    "config.bluetooth.enabled": "Off: phones can't connect over Bluetooth (Lorakeet on USB is unaffected).",
    "config.network.wifi_enabled": "On ESP32 radios Wi-Fi turns Bluetooth off.",
    "config.power.is_power_saving": "Lets the radio sleep: on ESP32 boards it can drop off USB and stop logging.",
    "module.serial.enabled": "Can take over the serial port Lorakeet uses on some boards.",
    "module.serial.override_console_serial_port": "Uses the console port (USB on many boards) for the serial module: "
                                                  "Lorakeet can lose the radio.",
    "module.mqtt.enabled": "Sends this radio's traffic to an MQTT server on the internet.",
    "module.mqtt.encryption_enabled": "Off: traffic goes to the MQTT server unencrypted.",
    "module.mqtt.map_reporting_enabled": "Publishes this radio's position and details to public maps.",
    "module.mqtt.proxy_to_client_enabled": "Relays MQTT through a connected phone.",
    "module.range_test.enabled": "Range test sends a message every few seconds: lots of airtime. For a short test only.",
    "module.neighbor_info.transmit_over_lora": "Broadcasts this radio's neighbor list over the air (airtime).",
    "module.store_forward.is_server": "Makes this radio store and resend history for others (memory and airtime).",
    "module.remote_hardware.enabled": "Lets other radios read and set this radio's GPIO pins.",
    "module.remote_hardware.allow_undefined_pin_access": "Lets other radios touch ANY pin, including ones that can "
                                                         "damage hardware.",
    "module.paxcounter.enabled": "Scans for nearby phones (Wi-Fi/Bluetooth) and reports the count.",
}
# When a change is risky, by the NEW value (default: any change). The page evaluates the same rules, so the warning
# and the confirmation only appear for a change that needs them (hop limit 4 -> 3 doesn't; 3 -> 5 does).
RISK_WHEN = {
    "config.security.serial_enabled": {"eq": False}, "config.device.serial_enabled": {"eq": False},
    "config.security.is_managed": {"eq": False}, "config.security.debug_log_api_enabled": {"eq": False},
    "config.security.admin_channel_enabled": {"eq": True}, "config.lora.tx_enabled": {"eq": False},
    "config.lora.use_preset": {"eq": False}, "config.lora.override_duty_cycle": {"eq": True},
    "config.lora.hop_limit": {"gt": 3}, "config.lora.config_ok_to_mqtt": {"eq": True},
    "config.device.role": {"notIn": ["CLIENT", "CLIENT_MUTE", "CLIENT_HIDDEN", "CLIENT_BASE", "TRACKER", "SENSOR", "LOST_AND_FOUND"]},
    "config.device.rebroadcast_mode": {"notIn": ["ALL", "ALL_SKIP_DECODING", "CORE_PORTNUMS_ONLY"]},
    "config.bluetooth.enabled": {"eq": False}, "config.network.wifi_enabled": {"eq": True},
    "config.power.is_power_saving": {"eq": True}, "module.serial.enabled": {"eq": True},
    "module.serial.override_console_serial_port": {"eq": True}, "module.mqtt.enabled": {"eq": True},
    "module.mqtt.encryption_enabled": {"eq": False}, "module.mqtt.map_reporting_enabled": {"eq": True},
    "module.mqtt.proxy_to_client_enabled": {"eq": True}, "module.range_test.enabled": {"eq": True},
    "module.neighbor_info.transmit_over_lora": {"eq": True}, "module.store_forward.is_server": {"eq": True},
    "module.remote_hardware.enabled": {"eq": True}, "module.remote_hardware.allow_undefined_pin_access": {"eq": True},
    "module.paxcounter.enabled": {"eq": True},
}


def risk_applies(path, value):
    """Does setting `path` to `value` (as the page shows it) need the warning and a confirmation?"""
    if path not in RISKS:
        return False
    rule = RISK_WHEN.get(path)
    if not rule:
        return True
    if "eq" in rule:
        return value == rule["eq"]
    if "gt" in rule:
        return isinstance(value, (int, float)) and value > rule["gt"]
    if "notIn" in rule:
        return value not in rule["notIn"]
    return True


HELP = {
    "config.device.role": "Client relays for others; Client, mute only listens.",
    "config.device.node_info_broadcast_secs": "How often the radio announces its name and key.",
    "config.device.tzdef": "Time zone for the screen, as a POSIX TZ string (e.g. PST8PDT,M3.2.0,M11.1.0).",
    "config.device.led_heartbeat_disabled": "Turns off the blinking heartbeat LED.",
    "config.device.buzzer_mode": "When the buzzer sounds.",
    "config.device.double_tap_as_button_press": "A double tap (accelerometer) counts as a button press.",
    "config.position.position_broadcast_secs": "How often the radio broadcasts its position.",
    "config.position.position_broadcast_smart_enabled": "Broadcast sooner when the radio has moved.",
    "config.position.fixed_position": "Use a fixed position instead of GPS (set the position itself in the Meshtastic app).",
    "config.position.gps_update_interval": "How often the GPS takes a fix. A moving station wants 30 s or less.",
    "config.position.gps_mode": "No GPS on this radio: it stops searching for one at every start.",
    "config.position.broadcast_smart_minimum_distance": "Smart broadcast: minimum distance moved (metres).",
    "config.position.broadcast_smart_minimum_interval_secs": "Smart broadcast: never more often than this.",
    "config.position.position_flags": "Which extra fields go with a position (a bit mask; the Meshtastic app has checkboxes).",
    "config.power.on_battery_shutdown_after_secs": "Shuts down this long after external power is lost (0 = never).",
    "config.power.wait_bluetooth_secs": "Power saving: how long Bluetooth stays on without a connection.",
    "config.power.sds_secs": "Power saving: super deep sleep after this long.",
    "config.power.ls_secs": "Power saving: light sleep length.",
    "config.power.min_wake_secs": "Power saving: minimum time awake.",
    "config.network.wifi_ssid": "The Wi-Fi network to join.",
    "config.network.wifi_psk": "The Wi-Fi password: 8 to 63 characters.",
    "config.network.ntp_server": "Time server (default meshtastic.pool.ntp.org).",
    "config.network.address_mode": "DHCP gets an address automatically; Static uses the addresses below.",
    "config.network.rsyslog_server": "Send the radio's log to a syslog server (host:port).",
    "config.display.screen_on_secs": "How long the screen stays on.",
    "config.display.units": "Metric or imperial on the screen.",
    "config.display.flip_screen": "Upside down, for a radio mounted the other way.",
    "config.lora.use_preset": "Use a named preset (LongFast...). Off = the custom bandwidth / spread factor / coding rate.",
    "config.lora.modem_preset": "LongFast is what most meshes use.",
    "config.lora.ignore_mqtt": "Ignore packets that came through MQTT gateways.",
    "config.lora.sx126x_rx_boosted_gain": "More receive sensitivity on SX126x radios, a little more power.",
    "config.lora.ignore_incoming": "Radios whose packets this radio ignores.",
    "config.bluetooth.mode": "How a phone pairs: a random PIN on the screen, a fixed PIN, or none.",
    "config.bluetooth.fixed_pin": "Six digits, for Fixed PIN mode.",
    "config.security.public_key": "The radio's public key: how others recognise it and send it direct messages.",
    "config.security.private_key": "Never shown. Changing it changes the radio's identity: use the Meshtastic app.",
    "config.security.admin_key": "Keys allowed to change this radio remotely.",
    "config.security.debug_log_api_enabled": "Sends the radio's debug log to Lorakeet over USB: per-reception detail.",
    "module.mqtt.address": "The MQTT server (default mqtt.meshtastic.org).",
    "module.mqtt.root": "The topic root (default msh).",
    "module.mqtt.json_enabled": "Also publish JSON (needs a radio with Wi-Fi or Ethernet).",
    "module.telemetry.device_update_interval": "How often the radio broadcasts battery and channel use.",
    "module.telemetry.environment_update_interval": "How often environment sensor readings go out.",
    "module.telemetry.device_telemetry_enabled": "Broadcast device telemetry at all.",
    "module.neighbor_info.update_interval": "How often the neighbor list is sent.",
    "module.range_test.sender": "Seconds between range-test messages (0 = only receive).",
    "module.external_notification.enabled": "Buzzer, LED or vibration on incoming messages.",
}
# The firmware's real limits where they're narrower than the field's type (0 often means "the default").
BOUNDS = {
    "config.lora.hop_limit": (0, 7), "config.lora.tx_power": (-10, 30), "config.lora.spread_factor": (0, 12),
    "config.lora.coding_rate": (0, 8), "config.lora.bandwidth": (0, 1625),
    "module.mqtt.map_report_settings.position_precision": (0, 32), "module.traffic_management.position_precision_bits": (0, 32),
    "module.ambient_lighting.red": (0, 255), "module.ambient_lighting.green": (0, 255), "module.ambient_lighting.blue": (0, 255),
    "module.ambient_lighting.current": (0, 31),
}
UNIT_WORDS = (("_secs", "s"), ("_ms", "ms"), ("_interval", "s"))
INT_RANGE = {F.TYPE_UINT32: (0, 2**32 - 1), F.TYPE_FIXED32: (0, 2**32 - 1), F.TYPE_INT32: (-2**31, 2**31 - 1),
             F.TYPE_SINT32: (-2**31, 2**31 - 1), F.TYPE_UINT64: (0, 2**64 - 1), F.TYPE_INT64: (-2**63, 2**63 - 1)}


class AdvancedError(ValueError):
    pass


def _label(name):
    s = name.replace("_", " ").replace(" secs", "").replace(" ms", "")
    s = re.sub(r"\bgpio\b", "GPIO pin", s)
    for a, b in (("gps", "GPS"), ("mqtt", "MQTT"), ("tx ", "transmit "), ("rx ", "receive "), ("led", "LED"), ("ntp", "NTP"),
                 ("psk", "password"), ("ssid", "network name"), ("tls", "TLS"), ("json", "JSON"), ("ipv6", "IPv6"),
                 ("ipv4", "IPv4"), ("dns", "DNS"), ("ip", "IP"), ("oled", "OLED"), ("tz", "time zone "), ("ina", "INA")):
        s = re.sub(rf"\b{a}", b, s)
    return s[:1].upper() + s[1:]


def _unit(name, path):
    if IP_FIELDS.match(path):
        return ""
    return next((u for w, u in UNIT_WORDS if name.endswith(w) or w + "_" in name), "")


def _ip_out(v):
    return ".".join(str((v >> s) & 0xFF) for s in (0, 8, 16, 24))   # the firmware's byte order: first octet lowest


def _ip_in(s):
    a = ipaddress.IPv4Address(str(s).strip())
    b = a.packed
    return b[0] | b[1] << 8 | b[2] << 16 | b[3] << 24


def _sections(node):
    """[(kind, name, message)] for every section the radio reported."""
    out = []
    for kind, top in (("config", node.localConfig), ("module", node.moduleConfig)):
        for f in top.DESCRIPTOR.fields:
            if f.type == F.TYPE_MESSAGE and top.HasField(f.name):
                out.append((kind, f.name, getattr(top, f.name)))
    return out


def _fields(msg, prefix):
    """Leaf fields of a section message (nested messages flattened): [(path, descriptor, parent message)]."""
    out = []
    for f in msg.DESCRIPTOR.fields:
        path = f"{prefix}.{f.name}"
        if f.type == F.TYPE_MESSAGE and not f.is_repeated:
            out += _fields(getattr(msg, f.name), path)
        else:
            out.append((path, f, msg))
    return out


def _describe(path, f, msg, writable):
    name, rep = f.name, f.is_repeated
    d = {"path": path, "label": _label(name), "help": HELP.get(path, ""), "risk": RISKS.get(path, ""), "riskWhen": RISK_WHEN.get(path),
         "unit": _unit(name, path), "readonly": not writable}
    v = getattr(msg, name)
    if path in LOCKED:
        d.update(type="locked", present=bool(len(v)) if rep else bool(v), readonly=True)
    elif path in SECRETS:
        d.update(type=SECRETS[path], secret=True, set=bool(v))
    elif rep:
        d.update(type="list", value=[x if isinstance(x, (int, float, str, bool)) else str(x) for x in v][:50], readonly=True)
    elif f.type == F.TYPE_BOOL:
        d.update(type="bool", value=bool(v))
    elif f.type == F.TYPE_ENUM:
        d.update(type="enum", value=f.enum_type.values_by_number[v].name if v in f.enum_type.values_by_number else str(v),
                 options=[e.name for e in f.enum_type.values])
    elif IP_FIELDS.match(path):
        d.update(type="ip", value=_ip_out(v))
    elif f.type in INT_RANGE:
        lo, hi = BOUNDS.get(path, INT_RANGE[f.type])
        d.update(type="int", value=int(v), min=lo, max=hi)
    elif f.type in (F.TYPE_FLOAT, F.TYPE_DOUBLE):
        d.update(type="float", value=round(float(v), 6))
    elif f.type == F.TYPE_STRING:
        d.update(type="string", value=v, maxlen=MAXLEN.get(path, 63))
    else:
        d.update(type="other", value=str(v), readonly=True)
    if path in ONE_WAY:
        d["oneWay"] = ONE_WAY[path]
    return d


def schema(node):
    """Every section the radio reported, each field with its current value (secrets: only whether they're set)."""
    out = []
    for kind, name, msg in _sections(node):
        sid = f"{kind}.{name}"
        writable = name in WRITABLE[kind]
        out.append({"id": sid, "title": TITLES.get(sid, _label(name)), "about": ABOUT.get(sid, ""), "writable": writable,
                    "fields": [_describe(p, f, m, writable) for p, f, m in _fields(msg, sid)]})
    return {"sections": out}


def _find(node, path):
    """(descriptor, parent message, section kind, section name) for a dotted path, or AdvancedError."""
    parts = str(path).split(".")
    if len(parts) < 3 or parts[0] not in WRITABLE:
        raise AdvancedError(f"unknown setting: {path}")
    kind, sec = parts[0], parts[1]
    top = node.localConfig if kind == "config" else node.moduleConfig
    if sec not in top.DESCRIPTOR.fields_by_name or not top.HasField(sec):
        raise AdvancedError(f"this radio has no {kind} section {sec!r}")
    if sec not in WRITABLE[kind]:
        raise AdvancedError(f"{TITLES.get(f'{kind}.{sec}', sec)} can't be changed from Lorakeet (use the Meshtastic app)")
    msg = getattr(top, sec)
    for p in parts[2:-1]:
        f = msg.DESCRIPTOR.fields_by_name.get(p)
        if f is None or f.type != F.TYPE_MESSAGE or f.is_repeated:
            raise AdvancedError(f"unknown setting: {path}")
        msg = getattr(msg, p)
    f = msg.DESCRIPTOR.fields_by_name.get(parts[-1])
    if f is None or (f.type == F.TYPE_MESSAGE and not f.is_repeated):
        raise AdvancedError(f"unknown setting: {path}")
    return f, msg, kind, sec


def _coerce(path, f, msg, value):
    """The value as the protobuf field wants it, validated; AdvancedError with a plain reason otherwise."""
    label = _label(f.name)
    if path in LOCKED or f.is_repeated:
        raise AdvancedError(f"{label} can't be changed here")
    if path in ONE_WAY and value != ONE_WAY[path]:
        raise AdvancedError(f"{label} can only be turned off here")
    if SECRETS.get(path) == "pin":
        try:
            pin = int(str(value).strip())
        except ValueError:
            raise AdvancedError("the Bluetooth PIN is six digits") from None
        if not 100000 <= pin <= 999999:
            raise AdvancedError("the Bluetooth PIN is six digits")
        return pin
    if path == "config.network.wifi_psk":
        s = str(value)
        if s and not 8 <= len(s) <= 63:
            raise AdvancedError("a Wi-Fi password is 8 to 63 characters")
        return s
    if f.type == F.TYPE_BOOL:
        if not isinstance(value, bool):
            raise AdvancedError(f"{label}: on or off")
        return value
    if f.type == F.TYPE_ENUM:
        e = f.enum_type.values_by_name.get(str(value))
        if e is None:
            raise AdvancedError(f"{label}: {value!r} isn't one of its choices")
        return e.number
    if IP_FIELDS.match(path):
        try:
            return _ip_in(value)
        except ValueError:
            raise AdvancedError(f"{label}: an IPv4 address like 192.168.1.50") from None
    if f.type in INT_RANGE:
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise AdvancedError(f"{label}: a whole number")
        try:
            n = int(str(value).strip()) if isinstance(value, str) else int(value)
        except ValueError:
            raise AdvancedError(f"{label}: a whole number") from None
        if isinstance(value, float) and value != n:
            raise AdvancedError(f"{label}: a whole number")
        lo, hi = BOUNDS.get(path, INT_RANGE[f.type])
        if not lo <= n <= hi:
            raise AdvancedError(f"{label}: between {lo} and {hi}")
        return n
    if f.type in (F.TYPE_FLOAT, F.TYPE_DOUBLE):
        try:
            x = float(value)
        except (TypeError, ValueError):
            raise AdvancedError(f"{label}: a number") from None
        if isinstance(value, bool) or not math.isfinite(x):
            raise AdvancedError(f"{label}: a number")
        return x
    if f.type == F.TYPE_STRING:
        s = str(value)
        if len(s.encode()) > MAXLEN.get(path, 63) or any(ord(c) < 32 for c in s):
            raise AdvancedError(f"{label}: up to {MAXLEN.get(path, 63)} characters, one line")
        return s
    raise AdvancedError(f"{label} can't be changed here")


def plan(node, changes):
    """Validate {path: value}; returns {path: (kind, section, field descriptor, parent, protobuf value)} for real
    changes only (a value equal to the current one is dropped). Secrets always count as a change when given."""
    out = {}
    for path, value in (changes or {}).items():
        f, msg, kind, sec = _find(node, path)
        v = _coerce(path, f, msg, value)
        if path not in SECRETS and getattr(msg, f.name) == v:
            continue
        out[path] = (kind, sec, f, msg, v)
    return out


def apply(planned):
    """Set the planned values on the node's config copies; returns the sections to write, as writeConfig names."""
    sections = []
    for _path, (_kind, sec, f, msg, v) in planned.items():
        setattr(msg, f.name, v)
        if sec not in sections:
            sections.append(sec)
    return sections


def public_view(planned):
    """What may be shown, logged or echoed about a plan: {path: value as the page shows it}, secrets as "(changed)"."""
    out = {}
    for path, (_kind, _sec, f, _msg, v) in planned.items():
        if path in SECRETS:
            out[path] = "(changed)"
        elif f.type == F.TYPE_ENUM:
            out[path] = f.enum_type.values_by_number[v].name
        elif IP_FIELDS.match(path):
            out[path] = _ip_out(v)
        else:
            out[path] = v
    return out


def risky(planned):
    """The planned changes that need a confirmation (by their new value), with the reason."""
    view = public_view(planned)
    return {p: RISKS[p] for p in planned if risk_applies(p, view[p])}


# ---- undo: the values a backup (the library's export_config YAML) holds for given settings

def _default(f):
    if f.type == F.TYPE_ENUM:
        return f.enum_type.values_by_number[0].name if 0 in f.enum_type.values_by_number else f.enum_type.values[0].name
    if f.type == F.TYPE_BOOL:
        return False
    if f.type == F.TYPE_STRING:
        return ""
    if f.type in (F.TYPE_FLOAT, F.TYPE_DOUBLE):
        return 0.0
    return 0


def backup_values(node, backup, paths):
    """{path: value as the page shows it} for each path, from a parsed backup (`config` / `module_config`, camelCase
    names as export_config writes them). A setting the backup leaves out was at its default: export_config omits
    defaults, and a whole module section at defaults. Secrets come back too: for writing only, never for showing."""
    out = {}
    for path in paths:
        f, msg, kind, sec = _find(node, path)
        top = (backup or {}).get("config" if kind == "config" else "module_config") or {}
        top_desc = (node.localConfig if kind == "config" else node.moduleConfig).DESCRIPTOR.fields_by_name[sec]
        cur = top.get(top_desc.json_name, top.get(sec)) or {}
        parts = path.split(".")[2:]
        d = top_desc.message_type
        for p_ in parts[:-1]:  # nested messages (network.ipv4_config...)
            fd = d.fields_by_name[p_]
            cur = (cur.get(fd.json_name, cur.get(p_)) if isinstance(cur, dict) else None) or {}
            d = fd.message_type
        v = cur.get(f.json_name, cur.get(f.name)) if isinstance(cur, dict) else None
        if v is None:
            v = _default(f)
        if IP_FIELDS.match(path) and isinstance(v, int):
            v = _ip_out(v)
        if f.type in (F.TYPE_FLOAT, F.TYPE_DOUBLE):
            v = float(v)
        out[path] = v
    return out

