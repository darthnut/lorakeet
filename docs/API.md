# Lorakeet's API, and using it from an LLM

Everything the dashboard shows comes from a JSON API on the same port (http://127.0.0.1:5190 by default), so a
script or an LLM can use Lorakeet without reading the screen. `GET /api/index` returns the list below as JSON.
This file is generated from `api_index.py` by `python tools/api_docs.py`: edit that, not this.

## From an LLM app (MCP)

`mcp_server.py` is a [Model Context Protocol](https://modelcontextprotocol.io) server for Claude Desktop, Claude Code
and other MCP clients. It needs nothing beyond Lorakeet's own Python. To add it:

```sh
python mcp_server.py --print-config
```

prints the lines to paste into Claude Desktop's config (Settings > Developer > Edit Config) and the `claude mcp add`
command for Claude Code, with this computer's paths filled in. Lorakeet must be running.

It offers tools that answer in compact JSON (local times, long lists cut short): `status`, `list_nodes`,
`node_report`, `mesh_summary`, `search_packets`, `get_packet`, `messages`, `insights`, `network_links`, `airtime`,
`stations`, `alerts`, `debug_log`, and `list_api` / `api_get` for any other read-only endpoint. Radios can be named
by id (`!1234abcd`) or by name.

**What it may do is yours to decide, in `lorakeet.toml`:**

```toml
[mcp]
allow_changes = false   # watch_node, mark_alerts_read, update_alert_settings, set_logging, backup_now, restart_lorakeet
allow_transmit = false  # send_message, traceroute: these go out on the mesh under your radio's name
```

Reading is always allowed. A tool that isn't allowed isn't offered to the LLM at all, and the LLM has no tool to
change these switches. Radio settings, channels and keys, pairing and peering (tier `page` below) are never
offered: use the Radio and Stations pages. Your MCP client may also ask you before each tool call.

These switches guard against an LLM doing more than you meant; they are not a security boundary against programs
on this computer, which have full access to the dashboard anyway (as the browser does).

## Plain HTTP

- **Who may call what:** this computer has full access. Other devices on your network get read-only access with
  `[http] lan = "view"`, nothing until they log in with `lan = "login"` (`POST /api/login`, then the `lk_session`
  cookie), or are refused (`lan = "off"`, the default). Tier `page` endpoints are this computer only, even after a
  login. The `Host` header must be an IP address, `localhost` or a name in `[http] hostnames`.
- **POST** bodies are JSON with `Content-Type: application/json`; from another device the `Origin` header must
  match the address you opened the dashboard at (the cross-site request guard).
- **Times** are unix seconds. **Radio ids** look like `!1234abcd`. **Ranges**: `24h`, `7d`, `30d`, `90d`, `all`.
- **station**: analytics are about one listening station (default: this computer's radio); `*` combines every
  station, with packets heard by several counted once.
- Hours when Lorakeet wasn't logging come back as `null` / `covered: false`: no data, not zero traffic.

```sh
curl -s http://127.0.0.1:5190/api/brief
curl -s "http://127.0.0.1:5190/api/packets/search?portnum=TEXT_MESSAGE_APP&limit=5"
curl -s -X POST -H "Content-Type: application/json" -d '{"id": "!1234abcd", "watched": true}' http://127.0.0.1:5190/api/watch
```


## Reading (always allowed)

| | Endpoint | What it does | Parameters |
|---|---|---|---|
| GET | `/api/index` | This list: every endpoint, its parameters and tier. |  |
| GET | `/api/version` | Lorakeet, Python and meshtastic library versions. |  |
| GET | `/api/brief` | One-line status: version, connected, paused, serial port, this radio's id and name. |  |
| GET | `/api/whoami` | What this client may do (full / view / login), version, paused, demo, login state. |  |
| GET | `/api/config` | The settings pages may see: map centre/zoom/tiles, base station, LAN mode (never paths). |  |
| GET | `/api/storage` | Database size, size limit, last backup, backup destinations' status. |  |
| GET | `/api/sync` | Sync mode (off / collector / hub) and its status. |  |
| GET | `/api/state` | Live view: connection status and every known radio (name, hardware, role, position, last heard, hops, SNR, battery, key warnings, reception at other stations). |  |
| GET | `/api/packets` | The live feed: newest packets of the last 3 days, one entry per packet with every station that heard it. | `limit`: max entries (default 200, max 2000) |
| GET | `/api/packet/{rowid}` | One packet's full logged JSON (packets.raw). |  |
| GET | `/api/packet/{rowid}/anatomy` | One packet rebuilt layer by layer as bytes (radio, header, encryption, envelope, payload), each field labelled with where its value came from. |  |
| GET | `/api/node/{id}` | One radio: live details, its telemetry and positions in the window, its newest 50 packets. | `hours`: history window (default 72) |
| GET | `/api/messages` | Text messages (channels and direct), each once across stations, with delivery status for our own. |  |
| GET | `/api/channels` | This radio's enabled channels: index, role, name, encrypted, public key (never the keys). |  |
| GET | `/api/links` | Measured radio-to-radio links (direct receptions, traceroutes, neighbor info) with average SNR. | `hours`: window (default 24) |
| GET | `/api/traceroutes` | The newest 5 traceroutes to a radio. | `node`: target radio id |
| GET | `/api/keyflags` | Radios whose public key is on the firmware's weak-key list or announced by other radios too. |  |
| GET | `/api/alerts` | Recent alerts (watched radios silent or back, low battery, new radios, health warnings). | `limit`: default 100 |
| GET | `/api/settings` | Alert settings and watched radios. |  |
| GET | `/api/debuglog` | The radio's firmware debug log (kept 7 days). | `limit`: default 500, max 5000<br>`q`: text to find<br>`since`: unix time<br>`before`: unix time<br>`levels`: comma list, e.g. ERROR,WARN<br>`source`: firmware module |
| GET | `/api/debuglog/sources` | Firmware modules seen in the debug log. |  |
| GET | `/api/stations` | Every listening station with data: name, location, last contact, its latest report (version, health). |  |
| GET | `/api/stations/timeline` | Power and network changes each station recorded. | `hours`: default 24, max 744 |
| GET | `/api/analytics` | The Analytics overview: KPIs, traffic per hour by type (gaps = not logging), nodes, ports, hops, relays, top talkers, new radios, conversations, readable vs private traffic. | `range`: range: 24h, 7d, 30d, 90d or all (default 7d)<br>`station`: station: a listening station's radio id (!xxxxxxxx), * for every station combined; default this computer's radio |
| GET | `/api/analytics/node` | One radio's analytics: activity, rhythm, SNR, battery, hops, relays, movement, links, identity history. | `id`: radio id<br>`range`: range: 24h, 7d, 30d, 90d or all (default 7d)<br>`station`: station: a listening station's radio id (!xxxxxxxx), * for every station combined; default this computer's radio |
| GET | `/api/analytics/node.csv` | One radio's packets as CSV (raw JSON included). | `id`: radio id<br>`range`: range: 24h, 7d, 30d, 90d or all (default 7d) |
| GET | `/api/analytics/airtime` | Channel airtime: calculated from what was heard vs measured channel utilization; by type and sender. | `range`: range: 24h, 7d, 30d, 90d or all (default 7d)<br>`station`: station: a listening station's radio id (!xxxxxxxx), * for every station combined; default this computer's radio |
| GET | `/api/analytics/topology` | Physical RF links between radios (measured and inferred from relay bytes). | `range`: range: 24h, 7d, 30d, 90d or all (default 7d)<br>`station`: station: a listening station's radio id (!xxxxxxxx), * for every station combined; default this computer's radio |
| GET | `/api/analytics/replay` | Every over-the-air event in time order (large: up to 50,000 events). | `range`: default 24h<br>`station`: station: a listening station's radio id (!xxxxxxxx), * for every station combined; default this computer's radio |
| GET | `/api/analytics/drive` | Coverage along a moving station's route: squares heard / silent, receptions, SNR. | `range`: default 24h<br>`station`: a moving station (default: every station that moved)<br>`bin`: square size in metres: 100, 250, 500 or 1000 |
| GET | `/api/analytics/stations` | Radio x station capture matrix: which station hears which radio, over minutes both were logging. | `range`: range: 24h, 7d, 30d, 90d or all (default 7d) |
| GET | `/api/analytics/compare` | Two stations compared over minutes both were logging: packets heard by both / only one, SNR differences. | `range`: range: 24h, 7d, 30d, 90d or all (default 7d)<br>`a`: station id<br>`b`: station id |
| GET | `/api/insights/health` | Health and security findings (busy channel, chatty radios, weak or shared keys, impersonation...). | `range`: range: 24h, 7d, 30d, 90d or all (default 7d)<br>`station`: station: a listening station's radio id (!xxxxxxxx), * for every station combined; default this computer's radio |
| GET | `/api/insights/estimates` | Estimated positions for radios that never share one. | `station`: station: a listening station's radio id (!xxxxxxxx), * for every station combined; default this computer's radio |
| GET | `/api/insights/coverage` | Where position packets were heard from. | `range`: range: 24h, 7d, 30d, 90d or all (default 7d)<br>`node`: only this sender<br>`relayed`: 1 = include relayed<br>`station`: station: a listening station's radio id (!xxxxxxxx), * for every station combined; default this computer's radio |
| GET | `/api/insights/traceroutes` | Traceroute results. | `range`: range: 24h, 7d, 30d, 90d or all (default 7d)<br>`station`: station: a listening station's radio id (!xxxxxxxx), * for every station combined; default this computer's radio |
| GET | `/api/packets/search` | Filter and page through the logged packets, receptions or transmissions. | `source`: packets (default), rx_hops (every reception) or tx_log (our transmissions)<br>`node`: either end<br>`from_id`: sender<br>`to_id`: recipient<br>`portnum`: e.g. TEXT_MESSAGE_APP<br>`channel`: channel index (packets) or hash (rx_hops)<br>`hopsMin`<br>`hopsMax`<br>`snrMin`<br>`kind`: broadcast or directed<br>`q`: text in the summary<br>`since`: unix time<br>`until`: unix time<br>`keyflag`: any, compromised or shared<br>`arrival`: air, lora, mqtt, udp, api, local or unknown<br>`limit`: default 200<br>`offset`<br>`station`: station: a listening station's radio id (!xxxxxxxx), * for every station combined; default this computer's radio |
| GET | `/api/packets/search.csv` | The whole filtered set as CSV. | `(same as /api/packets/search)` |

## Changing Lorakeet (`[mcp] allow_changes`)

| | Endpoint | What it does | Parameters |
|---|---|---|---|
| POST | `/api/watch` | Watch a radio: alerts when it goes silent, comes back or runs low on battery. | `id`: radio id<br>`watched`: true / false |
| POST | `/api/alerts/read` | Mark every alert read. |  |
| POST | `/api/settings` | Change alert settings (silence hours, low battery %, new-radio toasts, scheduled traceroutes...). | `(any alert setting from GET /api/settings)` |
| POST | `/api/logging` | Pause logging (lets go of the radio's port) or resume it. | `paused`: true / false |
| POST | `/api/backup` | Back up the database now. |  |
| POST | `/api/restart` | Restart Lorakeet (only when it runs under its background runner). |  |

## Transmitting on the mesh (`[mcp] allow_transmit`)

| | Endpoint | What it does | Parameters |
|---|---|---|---|
| POST | `/api/send` | Send a text message from this radio. | `text`: the message (up to ~200 bytes)<br>`to`: radio id for a direct message; omit for a channel<br>`channel`: channel index (default 0) |
| POST | `/api/traceroute` | Run a traceroute to a radio (one every 30 s at most). | `to`: radio id |

## The Radio, Stations and setup pages (this computer only; never offered to an LLM)

| | Endpoint | What it does | Parameters |
|---|---|---|---|
| GET | `/api/radio` | The Radio page: connection, checklist, channels, backups. |  |
| GET | `/api/radio/location-check` | Is this antenna position far from the radios heard? | `lat`<br>`lon` |
| GET | `/api/radio/logging` | Packets and receptions logged since then (the walkthrough's last step). | `since`: unix time |
| GET | `/api/radio/share` | A private channel's share link and QR code (contains its key). | `index`: channel index |
| POST | `/api/radio/apply` | Change radio settings (after a backup). |  |
| POST | `/api/radio/channel` | Create, add or copy a channel. |  |
| POST | `/api/radio/station` | This station's name and location. |  |
| POST | `/api/radio/restart` | Restart Lorakeet from the Radio / Stations pages. |  |
| GET | `/api/hub` | The Stations page: hub mode, addresses, pairings, peers. |  |
| POST | `/api/hub/mode` | Make this computer a hub (or stop). |  |
| POST | `/api/hub/pair` | Make a station pairing code. |  |
| POST | `/api/hub/station` | Rename, place, revoke or forget a station. |  |
| POST | `/api/hub/test` | Test a pairing code's hub addresses. |  |
| POST | `/api/hub/join` | Send this station's log to a hub. |  |
| POST | `/api/hub/leave` | Stop sending to the hub. |  |
| POST | `/api/hub/peer-code` | Let another hub send here. |  |
| POST | `/api/hub/peer-add` | Send to another hub. |  |
| POST | `/api/hub/peer` | Change or remove a peer. |  |
| POST | `/api/hub/peer-forget` | Delete everything a peer sent. |  |
| GET | `/api/setup` | First-run setup: defaults, serial ports, the live link. |  |
| POST | `/api/setup` | Write lorakeet.toml (first run only). |  |
| POST | `/api/demo` | Start the made-up demo mesh beside this Lorakeet. |  |

## Internal

| | Endpoint | What it does | Parameters |
|---|---|---|---|
| GET | `/api/events` | Server-sent events: live packets, status, storage, debug lines (what the map page listens to). |  |
| POST | `/api/ingest` | Stations and peer hubs send their rows here (token). |  |
| POST | `/api/ingest/hello` | A station's connection test (token). |  |
| POST | `/api/login` | Log in from another device. | `password` |
| POST | `/api/logout` | Log out. |  |
