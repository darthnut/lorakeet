# Lorakeet

Logging and analytics dashboard for a Meshtastic mesh. `server.py` holds a link to a radio (USB serial),
logs every packet and every over-the-air reception to SQLite, and serves `static/` with live updates over
server-sent events. Several listening stations can feed one hub (`sync.py`).

This file is for AI coding assistants and contributors: how the code is organised and the rules it
follows. User documentation is in `README.md` and `docs/`. Personal deployment notes, if any, go in an
untracked `CLAUDE.local.md`.

## Principles

- **The database is a clean, direct log of what was received.** Store values exactly as received; never
  "correct" them. Where a value could mislead, document it in `FIELD_NOTES` (server.py), which is rewritten
  into the `field_notes(tbl, col, note)` table on every start so the caveats travel with the data. If a
  column is derived, keep the raw source alongside it (e.g. `rx_hops.hops` plus `hop_start`/`hop_limit`).
- **Log complete packet information.** Don't drop fields to save space; `mesh.db` is never pruned by
  default (it warns at a size threshold and the user decides).
- **Anything that transmits is opt-in** (scheduled traceroutes default off). Don't send test messages while
  developing: they go out on a real mesh under the user's node. Validation paths can be tested with curl.
- **Nothing personal in code.** Every install-specific value lives in `lorakeet.toml` (git-ignored).
- **Coverage gaps are gaps, not zeros.** Hours the logger wasn't running are drawn hatched, skipped in
  playback, and excluded from rates.
- **Provenance:** derived values carry a badge (`static/prov.js`): R reported / O observed / I inferred /
  ? unknown. Keep using them for new derived values.
- **Before publishing:** `python tools/release_check.py` must pass (IPs, node ids, MACs, emails, paths,
  coordinates, callsigns, plus a private git-ignored denylist).

## Tests (`tests/`, `python -m pytest`; CI: `.github/workflows/tests.yml`)

- **Synthetic data only.** `tests/synth.py` builds a two-station mesh with made-up ids (four zeros in a row,
  e.g. `!a0000001`, which the release checker treats as placeholders) and known answers. Never commit a
  copy of a real `mesh.db` or real ids, names, places or coordinates as fixtures.
- `tests/conftest.py` points `LORAKEET_CONFIG` and the data folder at temp paths before anything is imported,
  so a developer's own `lorakeet.toml` and data are never read.
- Covers config + setup writer, firmware line parsing, channel hash, airtime, relay resolution, access
  rules, backup retention, secret redaction, the release checker, sync (duplicates, tokens, reports),
  analytics on the synthetic mesh (per-station counts, combined de-dup, coverage gaps, capture), drive
  coverage, and phone-log imports. When adding a feature, add a test that fails without it.
- CI also compiles every .py, `node --check`s every page script and tool, `bash -n`s the deploy scripts
  and runs `tools/release_check.py`.

## Logo and icons

The mesh-bird head (`static/favicon.svg`, the master: a lorikeet head as a low-poly mesh, white links and nodes,
on a #13294b rounded square) is the header mark on every page and the favicon. Derived files, rendered from it:
`favicon.ico` (16/32/48 PNG entries), `apple-touch-icon.png` (180, square corners: iOS rounds them),
`icon-192.png` / `icon-512.png` (in `site.webmanifest`). Re-render all of them if the SVG changes.

## Configuration

**First run:** with no `lorakeet.toml`, `/` redirects the dashboard PC to `static/setup.html` (`?skipsetup=1`
skips it). `GET /api/setup` (this PC only) = configured?, defaults, serial ports, the live link;
`POST /api/setup` writes a commented `lorakeet.toml` through `config.write_setup` (validated like a hand-written
file; refuses to overwrite). When supervised (`LORAKEET_SUPERVISED`, set by supervise.pyw, or systemd's
`INVOCATION_ID`) the server exits 1 s later so it restarts on the new settings; otherwise the page asks
for a restart. A configured install only gets a read-only summary.

`config.py` loads `lorakeet.toml` (next to server.py, or `$LORAKEET_CONFIG`) over defaults; every option is
documented in `lorakeet.example.toml`. Pages read `/api/config` (map center/zoom/tiles, base id/name, LAN
mode; never paths). Settings stored in the DB (alerts, watched nodes, auto-traceroute) win over config
defaults once they exist, so changing a default never changes a running install. To test a fresh install,
run server.py with `LORAKEET_CONFIG` pointing at a temp config (temp data_dir, another http.port, a
nonexistent radio.port).

## Running

- `python server.py` serves http://127.0.0.1:5190. On Windows, `supervise.pyw` runs it windowless and
  restarts it if it exits (5 s, doubling to 5 min on crash loops); on Linux, `deploy/lorakeet.service`.
- Only one program can hold the radio's serial port: stop the server before using the `meshtastic` CLI.
- Single instance: the server binds its port exclusively (SO_EXCLUSIVEADDRUSE on Windows); a second copy
  exits with code 3 and the supervisor stands down.
- **Stations (Linux):** `deploy/setup-station.sh` also makes the journal persistent (200 MB) and installs
  `deploy/wifi-watchdog.sh` (systemd timer, every minute; only while there's no network: rejoin any saved Wi-Fi in
  range after 2 min, toggle the radio after 10, reload the Wi-Fi driver after 20; never reboots; `journalctl -t
  lorakeet-wifi`). A Pi 3 lost a phone hotspot and never rejoined until power-cycled, twice in one day.
- **Network access:** loopback = full; private/link-local LAN addresses = view-only when `[http] lan =
  "view"` (every POST refused); anything else = 403. POSTs must be `application/json` from the same
  origin (the CSRF guard; keep it). There is no login: never expose the port to the internet.
- **Network radios:** `[radio] host` (or `--host`) connects to a Wi-Fi/Ethernet radio's TCP API (port 4403)
  instead of USB, through the same connect/watchdog path (`Mesh.run` builds a `TCPInterface(connectNow=False)`, then `connect()`
  (which opens the socket itself: calling `myConnect()` too made a second connection the radio kept dropping)
  + `waitForConfig()` as for serial). The status label shows host:port.
  Tested end to end on a Heltec V4 over Wi-Fi: connects in ~2 s, stable. **The firmware sends its debug log only
  over USB** (0 lines over TCP vs ~96/min over USB from the same radio), so a network radio gives decoded
  packets and telemetry but no rx_hops / tx_log (no duplicate copies, airtime or per-hop replay detail).
- **Disconnects** recover on their own: the radio thread polls every 5 s, auto-detects the port
  (`find_port`: Espressif, Adafruit nRF52, Seeed USB ids), connects with a 45 s timeout, and a 10-minute
  watchdog forces a reconnect if the radio goes silent. While unplugged, the last node DB is served.

## Data (`mesh.db`, in the configured data folder)

| table | contents |
|---|---|
| `packets` | every packet the API delivered: summary columns + `raw` = the full packet JSON (nothing dropped). Undecryptable packets are kept (portnum `ENCRYPTED`). |
| `rx_hops` | one row per over-the-air reception, duplicates included, parsed from the firmware debug log's `Lora RX (` lines: full header (from, to, pkt_id, channel hash, hops, hop_start/limit, relay byte, next_hop, want_ack, length = whole frame, encrypted, transport, snr, rssi). |
| `tx_log` | our radio's own transmissions from `Started Tx (` lines (needed for impersonation checks). |
| `telemetry` / `telemetry_full` | chart subset / every telemetry variant in full by `kind`, including the radio's own per-minute reports. |
| `node_info` | a row each time a node's name / hw / role / public key appears or changes. |
| `positions`, `links`, `messages`, `traceroutes`, `events`, `nodedb_snapshots` | as named. `positions.source = 'own'` = a station's own GPS fix. |

- **Stations:** every station-logged table has a `station` column (the logging radio's node id;
  `STATION_TABLES` in analytics.py). `analytics._connect(path, station)` shadows each table with a TEMP
  view scoped to one station, so analytics queries never filter by hand. `?station=*` is the combined
  view: packets de-duplicated by (sender, packet id), receptions kept (each is real), derived tables
  de-duplicated in 2-minute buckets, station-to-station traffic left out.
- **Sync:** collectors POST gzip batches to the hub's `/api/ingest` (hub mode + allowed networks + token;
  optional per-station tokens). `src_rowid` makes resends no-ops. Station reports (name, location, health,
  software fingerprint) ride along and double as a heartbeat.
- **Debug log** (`debug.db`, kept 7 days): needs `security.debug_log_api_enabled = true` on the radio. The
  library flattens LogRecords to text, so `_connect()` swaps in its own `_handleLogRecord` (keeps level,
  source, time). Known-harmless firmware lines are listed in `KNOWN_BENIGN` (static/app.js) and dimmed,
  never hidden.
- **Secrets are stripped before logging:** `security.private_key`, `security.admin_key`, the Bluetooth
  `fixed_pin`, the Wi-Fi `wifi_psk`, the MQTT `password` and channel PSKs (`redacted_config`,
  `redacted_module_config`, `redacted_channels`); `scrub_logged_secrets` removes any from older snapshots at start.
  Channel keys are used in memory only (on-air hashes, packet anatomy) and never stored or returned.
- **WAL mode** (set by `Store` on start; stored in the file): analytics reads and the logger's writes never block
  each other. Before 2026-10-08 mesh.db ran in rollback mode, where a slow read made writes wait and a waiting
  write blocked new reads ("database is locked"). Waits are 30 s (`DB_WAIT_S`); requests over 5 s are logged with
  their path (`SLOW_REQUEST_S`). Read-only connections work whether or not the server has the database open.
- **Phone exports:** `import_datalog.py` loads the Android app's packet CSV and node-database JSON as a
  station's data (no packet ids, so the combined view leaves them out).

## Analytics (`analytics.py`, `/analytics.html`)

- Computed in SQL over a read-only connection; hourly buckets up to 8 days, daily beyond, local time.
- **Coverage:** an hour is covered if the radio's per-minute reports or any packet landed in it.
  Uncovered buckets are `null` (drawn hatched), and rates divide by covered time only.
- **Relay bytes:** `relayNode` is only the last byte of the relayer's node number. `resolve_relay`
  accepts it when exactly one known node matches, or exactly one of the matches has a measured link to
  the receiving station.
- **Airtime** (`airtime.py`): calculated from frame length and modem settings (Semtech AN1200.13). The
  firmware's "Packet RX: N ms" is the same calculation, not a measurement. Measured channel utilization
  comes from the radio's per-minute `deviceMetrics`.
- **Readable vs private traffic:** `radio_channels()` computes each enabled channel's on-air hash exactly
  as the firmware does (xor(name) ^ xor(expanded key); a secondary channel with no key uses the primary's).
- **Station comparison** (`compare.py`): only minutes when the compared stations were all logging count.
  SNR/RSSI across different radio models aren't directly comparable.
- **Placing radios from drives** (`insights.drive_estimates`, feeds `estimate_positions` for radios that share no
  position): a moving station's DIRECT receptions, each at its GPS fix (within 60 s, stay-point smoothed), grouped
  into ~200 m spots; only with 3+ spots spread 1 km+; signal-weighted centroid (10^(median SNR/20)), radius =
  weighted spread / sqrt(effective spots), at least 0.5 km. Leans toward the roads driven. The first two days of
  drives placed nothing (6 radios heard directly, nearly all while parked): the radio was inside the cab.
- **No hops to spare** (`health`, kind `hops-edge`): a radio whose packets' best copies mostly (50%+, 5+
  packets) arrived with hop_limit 0: the ones that needed one more hop never arrived. Suggests hop limit + 1.
- **Insights** (`insights.py`, all passive): position estimates, health and security findings (weak keys
  from `weak_keys.py`, generated from the firmware's `LOW_ENTROPY_HASHES`; duplicate keys; impersonation).
- **Firmware 2.8 renumbering** (`nodeids.py`): 2.8 renumbers a radio to crc32(its public key). Same key under
  an old number and the crc32 number, the old one never heard after the new one first was = one radio
  (`aliases` old -> new; both on the air at once stays a "shared key"). `analytics._connect` then reads every
  node-number column (`ID_COLUMNS`, the station column included) through TEMP `_alias` views (`_a_<table>`), so
  history, station scoping and the picker join across the upgrade; stored rows keep the number they arrived
  with. Relay bytes logged before an upgrade end in the OLD number: `resolve_relay(..., aliases)` maps them.
  Only built when an alias exists (a lookup per row and column). Radios numbered the 2.8 way get `v28` in
  `describe`/`node_json` (a "2.8" tag; `fw:2.8` in the node filter) and the Analytics "Firmware 2.8 adoption"
  card counts them per day. Settings that name radios by number follow it too (`nodeids.follow`): the `[remote]
  allow` list (the radio must still send with its recorded key, which 2.8 keeps), `[base] id` and its relay byte
  (`follow_base`, at start and every 5 min), watched radios (`Alerts._watched`), and per-station sync tokens
  (`sync.follow_tokens`). Needs the new number's NodeInfo to have been heard (that's what links the two).
- **Reception logging alarm** (`alerts._check_mining`): debug-log lines arriving and LoRa packets arriving for
  30 min with no RX/TX line recognised (`Mesh.log_counts`) raises "Reception logging has stopped" (a firmware
  log-format change); resolved when lines parse again. 2.8.1 already changed one thing: `Ch=` is decimal
  (2.7: hex), which `_parse_header` handles.
- **Key warnings** (`keyflags.py`, `GET /api/keyflags`): a radio whose newest announced public key is on the
  firmware's weak-key list ("compromised") or is announced by other radios too ("shared"; the firmware 2.8
  renumber case is exempt). Uses every station's identity log, worked out on request (cached 120 s), never
  stored. Carried as `keyFlag` by `Mesh.describe` / `node_json`, as `from_keyflag` on packet search rows, and as
  `keyFlags` in the anatomy; drawn by `keyBadge()` (prov.js) on the map's node list, panel and feed, node
  pages, the Analytics node table (`key:flagged` filter), the Packets browser ("Sender key" filter), the
  replay log and texts panel (not on `?anon=1` pages). insights.py's weak/duplicate findings share its rules.
- **Packet anatomy** (`anatomy.py`): one packet rebuilt layer by layer (radio, 16-byte header, AES-CTR
  encryption, Data envelope, payload), every field labelled with where its value came from. Traceroutes
  addressed to us gain the radio's own SNR entry before the API sees them; `_traceroute_on_air` accounts
  for it.

## Visualizations (`/visualizations.html`, `topology.js`, `replay.js`, `viz.js`)

- Full-window app: graph (d3 force) or geographic (Leaflet) view, floating panels, replay player.
- **Topology** = physical RF adjacency, not who-messages-whom: measured links (direct receptions,
  traceroutes, neighbor info) plus links inferred from relay bytes.
- **Replay:** every reception in time order; a packet animates only along what the receiving station
  knew (sender → resolved relay → station; unknown middle hops dashed). Traceroutes animate hop by hop.
- **Graph fit:** spacing scales with the open area; the *drawn* layout is stretched to the area's shape
  (simulation coordinates untouched; `pos(id)` returns drawn coordinates); one rAF camera eases the zoom
  to frame it. No `forceCenter`: it re-centres by shifting every node at once, which jolts the graph
  when nodes join.
- **Grow mode** (radios appear at their first packet, the layout grows with them) and **Fade mode** (radios
  dim after a learned per-radio timeout; silence counts only logging hours). Fade-ins run on the replay's
  clock, not CSS transitions, so recordings match live playback.
- **Coverage view** (`drive.py`, `/api/analytics/drive?range=&bin=&station=`, `static/drive.js`): what a moving
  station heard along its route. Receptions (rx_hops, else packets) are placed at the station's latest exact
  own fix within 12 min (never interpolated; precision-rounded fixes are ignored) and binned into squares
  (100 m-1 km): minutes there, receptions/min, radios, heard directly, best/median SNR, relays. Silence is judged
  against the station's own reception rate (`ratePerMin`): it counts as a gap only after `silentAfterS` = ln(10) /
  rate (a covered station would have heard something 90% of the time). Cells: `status` heard / hole (most of its
  time inside such a silence) / brief (crossed between packets, too quickly to tell; drawn faintly); the route is
  dashed inside the silences. (The first version called every square crossed without a packet a hole: at ~1
  packet/min that was most squares on a highway drive.) Squares are a FIXED worldwide grid (latitude rows from the
  equator, columns sized per row), so a place is in the same square on every drive and range.
- **Stay points** (`analytics.smooth_track`, used by drive.py and the moving map; analysis only, stored fixes
  untouched): a run of fixes within 100 m of its running centre for 3 min+ is one parked spot. A GPS in a vehicle
  cab jumps tens of metres to 100 m+ (multipath) while the radio's PDOP/satellite count look fine, and the exact
  own fixes from the node list carry no quality fields anyway (only the rounded broadcasts do). With no station
  picked (or combined), only stations whose track spans 200 m+ count.
- **Moving stations on the map** (`topology.station_tracks`, `_positions`): a station with its own exact GPS fixes in
  range comes with `tracks` (its route, starting at the last fix before the range) and is drawn at its "dwell" spot
  (fixes weighted by time until the next, in ~200 m squares: where it spent the range). The map draws the route
  faintly; the replay moves the dot along it (latest fix at or before the playhead, under 12 min old, else the
  dwell spot), draws the route so far, and flies receptions to where the station was. Other stations use their
  newest EXACT position: a channel-rounded copy of their broadcast never places a station.
- `?anon=1` renames every radio by role and order of appearance for sharing. On maps (Geographic, its replay,
  Coverage) it also moves everything by one offset to a decoy place (`decoyShift` in topology.js: default
  14.5 degrees east at the same latitude, which keeps distances exact; `?decoy=lat,lon`), and Coverage renames
  stations/radios, drops links with real ids and never shows message text (test messages carry coordinates).
- **Texts panel** (toolbar "💬 Texts" / M, `localStorage meshdash.chat`, off by default): a chat log on the right
  that fills as the replay reaches each readable text, with a line from each entry to its sender (new lines
  bright for 6 s, then faint; hover an entry to light its line; lines redrawn every frame by their own rAF loop
  so they follow the graph and the map while paused). New texts join the bottom; after 25 s of playing (or when
  the panel is full) the oldest fades out and closes up so the rest flow to the top, one at a time. Ages run
  on a clock that only advances while playing (JS, not CSS transitions, so recordings match). Playback speed is
  never changed. Seeking empties the panel. It stays in hidden-UI mode (for recordings) and `vizInset` reserves its width. The
  replay output carries `messages` (once each by sender + packet id, `channelName` from the LOGGING station's
  channel list in its latest connect snapshot: channel numbers differ between radios). `?anon=1` shows "a text
  message", never the words. The log's 0-hop wording is "heard directly (0 hops)" so it isn't read as a DM.
- `tools/record-viz.mjs` records the replay to MP4 frame by frame (headless Chrome over CDP with Node's
  built-in WebSocket and a simulated page clock, piped to ffmpeg).

## Sending

- `POST /api/send {text, to?, channel?}` and `POST /api/traceroute {to}`. Directed packets use
  `Mesh.hop_limit_for` (known hops + 1, capped at 7). Text goes through `sendData(...,
  onResponseAckPermitted=True)`, because `sendText()` drops that flag and ACKs never arrive. Late ACKs
  (after the library gave up) are caught from ROUTING packets.

## Drive mode (`[station] drive_pings`, mobile only, off by default, TRANSMITS)

`Mesh._drive_ping`: "Lorakeet ping #n" on a private channel (by name; public-key channels refused) every
`drive_ping_m` metres moved from the last ping, at most once per `drive_ping_min_s` (floors 200 m / 30 s: shared
airtime), only with a GPS fix under 60 s old, never while parked. Coverage's "Where the mesh heard it" layer
(`drive._sent`) then places each of the station's own transmissions (tx_log + outgoing messages) on its route:
`station` (another of our stations heard it), `mesh` (the station heard it repeated) or `none`.

## Remote commands (`remote.py`, `[remote]`, off by default)

"lk status" / "lk gps" / "lk help" sent to a station's radio as a direct message get a direct-message reply
(status: uptime, network, sync backlog, temperature + Pi power flags, GPS age). Accepted only when PKI-encrypted
(proves the sender), to this radio, from `[remote] allow`, with the sender key equal to the FIRST key this
database recorded for it (`first_public_key`), and not compromised/shared (keyflags). Channel messages never
count. One reply per sender per 10 s; repeated copies answered once; every command, accepted or not, is an
`events` row (`remote_command`). Read-only by design so far: restart/reboot would need a confirm step.

## Mobile stations

`[station] mobile = true`: the station logs its radio's own GPS fixes (`positions.source = 'own'`) and
`analytics.station_position(db, station, ts)` places it at any moment. The radio hands the client its own
position rounded to the channel's precision; exact fixes come from asking for the node list
(`want_config_id = 69421`, firmware 2.7+) every 10 s; set the radio's `position.gps_update_interval` low too
(default 120 s), or the fix itself is stale at speed. A Pi with no network sets its clock from the radio's
GPS time once (`set_clock_from_gps`).

## Gotchas

- After a config write the radio reboots ~7 s later; reading config back inside that window shows the old
  value.
- Opening the serial port resets ESP32 boards, so each (re)connect reboots the radio for a few seconds.
- Firmware 2.7.26 logs the RX line before setting the transport, so `rx_hops.transport` is always 0
  there; use `packets.raw → transportMechanism` for the real value.
- Old firmware with a current library may report empty configuration; writing on top of that resets
  settings. Always read the full config back before writing.
- CARTO basemaps now need an API key; the default maps are OpenStreetMap and OpenTopoMap.
- Windows checkouts with `core.autocrlf=true` produce CRLF shell scripts; `.gitattributes` pins `*.sh`,
  `*.service` and `deploy/*.toml` to LF.
- Parse-check every `.py` before restarting a remote station (`deploy/update-station.sh` does).
