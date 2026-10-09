"""Every HTTP endpoint server.py answers, described for programs and LLMs (`GET /api/index`, docs/API.md, mcp_server.py).

tier:
  read      looks, changes nothing
  change    changes Lorakeet itself (settings, watched radios, pausing): mcp_server.py needs [mcp] allow_changes
  transmit  makes the radio transmit on the mesh: mcp_server.py needs [mcp] allow_transmit
  page      the Radio, Stations and setup pages' own operations (radio settings, keys, pairing, peering): this
            computer only, and never offered to an LLM by mcp_server.py
  internal  used by Lorakeet itself (stations syncing, logging in, the live event stream)

tests/test_api_index.py checks that every path server.py handles is listed here, and that docs/API.md is current
(regenerate it with `python tools/api_docs.py`).
"""

RANGE = "range: 24h, 7d, 30d, 90d or all (default 7d)"
STATION = "station: a listening station's radio id (!xxxxxxxx), * for every station combined; default this computer's radio"

ENDPOINTS = [
    # ---- status
    {"method": "GET", "path": "/api/index", "tier": "read", "summary": "This list: every endpoint, its parameters and tier."},
    {"method": "GET", "path": "/api/version", "tier": "read", "summary": "Lorakeet, Python and meshtastic library versions."},
    {"method": "GET", "path": "/api/brief", "tier": "read",
     "summary": "One-line status: version, connected, paused, serial port, this radio's id and name."},
    {"method": "GET", "path": "/api/whoami", "tier": "read",
     "summary": "What this client may do (full / view / login), version, paused, demo, login state."},
    {"method": "GET", "path": "/api/config", "tier": "read",
     "summary": "The settings pages may see: map centre/zoom/tiles, base station, LAN mode (never paths)."},
    {"method": "GET", "path": "/api/storage", "tier": "read",
     "summary": "Database size, size limit, last backup, backup destinations' status."},
    {"method": "GET", "path": "/api/sync", "tier": "read", "summary": "Sync mode (off / collector / hub) and its status."},
    # ---- live state
    {"method": "GET", "path": "/api/state", "tier": "read",
     "summary": "Live view: connection status and every known radio (name, hardware, role, position, last heard, hops, "
                "SNR, battery, key warnings, reception at other stations)."},
    {"method": "GET", "path": "/api/packets", "tier": "read", "params": {"limit": "max entries (default 200, max 2000)"},
     "summary": "The live feed: newest packets of the last 3 days, one entry per packet with every station that heard it."},
    {"method": "GET", "path": "/api/packet/{rowid}", "tier": "read", "summary": "One packet's full logged JSON (packets.raw)."},
    {"method": "GET", "path": "/api/packet/{rowid}/anatomy", "tier": "read",
     "summary": "One packet rebuilt layer by layer as bytes (radio, header, encryption, envelope, payload), each field "
                "labelled with where its value came from."},
    {"method": "GET", "path": "/api/node/{id}", "tier": "read", "params": {"hours": "history window (default 72)"},
     "summary": "One radio: live details, its telemetry and positions in the window, its newest 50 packets."},
    {"method": "GET", "path": "/api/messages", "tier": "read",
     "summary": "Text messages (channels and direct), each once across stations, with delivery status for our own."},
    {"method": "GET", "path": "/api/channels", "tier": "read",
     "summary": "This radio's enabled channels: index, role, name, encrypted, public key (never the keys)."},
    {"method": "GET", "path": "/api/links", "tier": "read", "params": {"hours": "window (default 24)"},
     "summary": "Measured radio-to-radio links (direct receptions, traceroutes, neighbor info) with average SNR."},
    {"method": "GET", "path": "/api/traceroutes", "tier": "read", "params": {"node": "target radio id"},
     "summary": "The newest 5 traceroutes to a radio."},
    {"method": "GET", "path": "/api/keyflags", "tier": "read",
     "summary": "Radios whose public key is on the firmware's weak-key list or announced by other radios too."},
    {"method": "GET", "path": "/api/alerts", "tier": "read", "params": {"limit": "default 100"},
     "summary": "Recent alerts (watched radios silent or back, low battery, new radios, health warnings)."},
    {"method": "GET", "path": "/api/settings", "tier": "read", "summary": "Alert settings and watched radios."},
    {"method": "GET", "path": "/api/debuglog", "tier": "read",
     "params": {"limit": "default 500, max 5000", "q": "text to find", "since": "unix time", "before": "unix time",
                "levels": "comma list, e.g. ERROR,WARN", "source": "firmware module"},
     "summary": "The radio's firmware debug log (kept 7 days)."},
    {"method": "GET", "path": "/api/debuglog/sources", "tier": "read", "summary": "Firmware modules seen in the debug log."},
    # ---- stations
    {"method": "GET", "path": "/api/stations", "tier": "read",
     "summary": "Every listening station with data: name, location, last contact, its latest report (version, health)."},
    {"method": "GET", "path": "/api/stations/timeline", "tier": "read", "params": {"hours": "default 24, max 744"},
     "summary": "Power and network changes each station recorded."},
    # ---- analytics
    {"method": "GET", "path": "/api/analytics", "tier": "read", "params": {"range": RANGE, "station": STATION},
     "summary": "The Analytics overview: KPIs, traffic per hour by type (gaps = not logging), nodes, ports, hops, relays, "
                "top talkers, new radios, conversations, readable vs private traffic."},
    {"method": "GET", "path": "/api/analytics/node", "tier": "read",
     "params": {"id": "radio id", "range": RANGE, "station": STATION},
     "summary": "One radio's analytics: activity, rhythm, SNR, battery, hops, relays, movement, links, identity history."},
    {"method": "GET", "path": "/api/analytics/node.csv", "tier": "read", "params": {"id": "radio id", "range": RANGE},
     "summary": "One radio's packets as CSV (raw JSON included)."},
    {"method": "GET", "path": "/api/analytics/airtime", "tier": "read", "params": {"range": RANGE, "station": STATION},
     "summary": "Channel airtime: calculated from what was heard vs measured channel utilization; by type and sender."},
    {"method": "GET", "path": "/api/analytics/topology", "tier": "read", "params": {"range": RANGE, "station": STATION},
     "summary": "Physical RF links between radios (measured and inferred from relay bytes)."},
    {"method": "GET", "path": "/api/analytics/replay", "tier": "read", "params": {"range": "default 24h", "station": STATION},
     "summary": "Every over-the-air event in time order (large: up to 50,000 events)."},
    {"method": "GET", "path": "/api/analytics/drive", "tier": "read",
     "params": {"range": "default 24h", "station": "a moving station (default: every station that moved)",
                "bin": "square size in metres: 100, 250, 500 or 1000"},
     "summary": "Coverage along a moving station's route: squares heard / silent, receptions, SNR."},
    {"method": "GET", "path": "/api/analytics/stations", "tier": "read", "params": {"range": RANGE},
     "summary": "Radio x station capture matrix: which station hears which radio, over minutes both were logging."},
    {"method": "GET", "path": "/api/analytics/compare", "tier": "read",
     "params": {"range": RANGE, "a": "station id", "b": "station id"},
     "summary": "Two stations compared over minutes both were logging: packets heard by both / only one, SNR differences."},
    {"method": "GET", "path": "/api/insights/health", "tier": "read", "params": {"range": RANGE, "station": STATION},
     "summary": "Health and security findings (busy channel, chatty radios, weak or shared keys, impersonation...)."},
    {"method": "GET", "path": "/api/insights/estimates", "tier": "read", "params": {"station": STATION},
     "summary": "Estimated positions for radios that never share one."},
    {"method": "GET", "path": "/api/insights/coverage", "tier": "read",
     "params": {"range": RANGE, "node": "only this sender", "relayed": "1 = include relayed", "station": STATION},
     "summary": "Where position packets were heard from."},
    {"method": "GET", "path": "/api/insights/traceroutes", "tier": "read", "params": {"range": RANGE, "station": STATION},
     "summary": "Traceroute results."},
    {"method": "GET", "path": "/api/packets/search", "tier": "read",
     "params": {"source": "packets (default), rx_hops (every reception) or tx_log (our transmissions)",
                "node": "either end", "from_id": "sender", "to_id": "recipient", "portnum": "e.g. TEXT_MESSAGE_APP",
                "channel": "channel index (packets) or hash (rx_hops)", "hopsMin": "", "hopsMax": "", "snrMin": "",
                "kind": "broadcast or directed", "q": "text in the summary", "since": "unix time", "until": "unix time",
                "keyflag": "any, compromised or shared", "arrival": "air, lora, mqtt, udp, api, local or unknown",
                "limit": "default 200", "offset": "", "station": STATION},
     "summary": "Filter and page through the logged packets, receptions or transmissions."},
    {"method": "GET", "path": "/api/packets/search.csv", "tier": "read", "params": {"(same as /api/packets/search)": ""},
     "summary": "The whole filtered set as CSV."},
    # ---- changing Lorakeet
    {"method": "POST", "path": "/api/watch", "tier": "change", "body": {"id": "radio id", "watched": "true / false"},
     "summary": "Watch a radio: alerts when it goes silent, comes back or runs low on battery."},
    {"method": "POST", "path": "/api/alerts/read", "tier": "change", "body": {}, "summary": "Mark every alert read."},
    {"method": "POST", "path": "/api/settings", "tier": "change",
     "body": {"(any alert setting from GET /api/settings)": ""},
     "summary": "Change alert settings (silence hours, low battery %, new-radio toasts, scheduled traceroutes...)."},
    {"method": "POST", "path": "/api/logging", "tier": "change", "body": {"paused": "true / false"},
     "summary": "Pause logging (lets go of the radio's port) or resume it."},
    {"method": "POST", "path": "/api/backup", "tier": "change", "body": {}, "summary": "Back up the database now."},
    {"method": "POST", "path": "/api/restart", "tier": "change", "body": {},
     "summary": "Restart Lorakeet (only when it runs under its background runner)."},
    # ---- transmitting
    {"method": "POST", "path": "/api/send", "tier": "transmit",
     "body": {"text": "the message (up to ~200 bytes)", "to": "radio id for a direct message; omit for a channel",
              "channel": "channel index (default 0)"},
     "summary": "Send a text message from this radio."},
    {"method": "POST", "path": "/api/traceroute", "tier": "transmit", "body": {"to": "radio id"},
     "summary": "Run a traceroute to a radio (one every 30 s at most)."},
    # ---- the Radio, Stations and setup pages (this computer only; not for LLMs)
    {"method": "GET", "path": "/api/radio", "tier": "page", "summary": "The Radio page: connection, checklist, channels, backups."},
    {"method": "GET", "path": "/api/radio/location-check", "tier": "page", "params": {"lat": "", "lon": ""},
     "summary": "Is this antenna position far from the radios heard?"},
    {"method": "GET", "path": "/api/radio/logging", "tier": "page", "params": {"since": "unix time"},
     "summary": "Packets and receptions logged since then (the walkthrough's last step)."},
    {"method": "GET", "path": "/api/radio/share", "tier": "page", "params": {"index": "channel index"},
     "summary": "A private channel's share link and QR code (contains its key)."},
    {"method": "POST", "path": "/api/radio/apply", "tier": "page", "summary": "Change radio settings (after a backup)."},
    {"method": "POST", "path": "/api/radio/channel", "tier": "page", "summary": "Create, add or copy a channel."},
    {"method": "POST", "path": "/api/radio/station", "tier": "page", "summary": "This station's name and location."},
    {"method": "POST", "path": "/api/radio/restart", "tier": "page", "summary": "Restart Lorakeet from the Radio / Stations pages."},
    {"method": "GET", "path": "/api/hub", "tier": "page", "summary": "The Stations page: hub mode, addresses, pairings, peers."},
    {"method": "POST", "path": "/api/hub/mode", "tier": "page", "summary": "Make this computer a hub (or stop)."},
    {"method": "POST", "path": "/api/hub/pair", "tier": "page", "summary": "Make a station pairing code."},
    {"method": "POST", "path": "/api/hub/station", "tier": "page", "summary": "Rename, place, revoke or forget a station."},
    {"method": "POST", "path": "/api/hub/test", "tier": "page", "summary": "Test a pairing code's hub addresses."},
    {"method": "POST", "path": "/api/hub/join", "tier": "page", "summary": "Send this station's log to a hub."},
    {"method": "POST", "path": "/api/hub/leave", "tier": "page", "summary": "Stop sending to the hub."},
    {"method": "POST", "path": "/api/hub/peer-code", "tier": "page", "summary": "Let another hub send here."},
    {"method": "POST", "path": "/api/hub/peer-add", "tier": "page", "summary": "Send to another hub."},
    {"method": "POST", "path": "/api/hub/peer", "tier": "page", "summary": "Change or remove a peer."},
    {"method": "POST", "path": "/api/hub/peer-forget", "tier": "page", "summary": "Delete everything a peer sent."},
    {"method": "GET", "path": "/api/setup", "tier": "page", "summary": "First-run setup: defaults, serial ports, the live link."},
    {"method": "POST", "path": "/api/setup", "tier": "page", "summary": "Write lorakeet.toml (first run only)."},
    {"method": "POST", "path": "/api/demo", "tier": "page", "summary": "Start the made-up demo mesh beside this Lorakeet."},
    # ---- internal
    {"method": "GET", "path": "/api/events", "tier": "internal",
     "summary": "Server-sent events: live packets, status, storage, debug lines (what the map page listens to)."},
    {"method": "POST", "path": "/api/ingest", "tier": "internal", "summary": "Stations and peer hubs send their rows here (token)."},
    {"method": "POST", "path": "/api/ingest/hello", "tier": "internal", "summary": "A station's connection test (token)."},
    {"method": "POST", "path": "/api/login", "tier": "internal", "body": {"password": ""}, "summary": "Log in from another device."},
    {"method": "POST", "path": "/api/logout", "tier": "internal", "body": {}, "summary": "Log out."},
]

TIERS = ("read", "change", "transmit", "page", "internal")


def index(version):
    return {"version": version, "tiers": TIERS, "endpoints": ENDPOINTS,
            "notes": ["Times are unix seconds; radio ids look like !1234abcd.",
                      "POST bodies are JSON with Content-Type: application/json; from another device the Origin header "
                      "must match the dashboard's address.",
                      "Full access only from this computer (or a device that logged in); others are view-only or refused.",
                      "tier 'page' endpoints are this computer only, even when logged in."]}


def find(method, path):
    """The index entry for a concrete request path (placeholders like {id} match one path segment), or None."""
    parts = path.rstrip("/").split("/")
    for e in ENDPOINTS:
        ep = e["path"].split("/")
        if e["method"] == method and len(ep) == len(parts) and all(
                a == b or (a.startswith("{") and a.endswith("}") and b) for a, b in zip(ep, parts)):
            return e
    return None
