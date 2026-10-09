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
- **Network access** (`login.access`): loopback = full; private/link-local LAN addresses = view-only with
  `[http] lan = "view"`, only the login page with `lan = "login"`, refused with `"off"`; anything else = 403.
  POSTs must be `application/json` from the same origin (the CSRF guard; keep it); from another device the
  Origin header is required too.
- **Login** (`login.py`): `python server.py --set-password` (getpass, twice; never an argument) stores a salted
  scrypt hash in `<data>/login.json` (not lorakeet.toml: people paste that when asking for help). `POST
  /api/login` gives a LAN device a random session token in an HttpOnly SameSite=Strict cookie (only its SHA-256
  is stored; 30 days from last use); that device then gets full access, except `/api/setup` (paths: loopback
  only). Wrong passwords: 5 free per address, then a lockout doubling from 30 s to 15 min, and 30/min overall.
  A new password ends every session. Plain HTTP: it keeps LAN guests off the radio, nothing more; never expose
  the port to the internet. Tests: `tests/test_login.py` (a real handler with every client treated as LAN).
- **Versions:** `VERSION` (`x.y.z-dev` between releases; the test requires `## Unreleased` in CHANGELOG.md then,
  and `## x.y.z (date)` for a release). Shown by `--version`, `/api/version`, whoami (the logo's tooltip) and
  station reports. The database's layout is `SCHEMA_VERSION` in `PRAGMA user_version`: bump it with any change
  to SCHEMA/MIGRATIONS; never lowered (an older Lorakeet on a newer file warns and keeps logging).
  Migrations only ever add.
- **Install scripts:** `Install Lorakeet.cmd` / `Uninstall Lorakeet.cmd` (double-click; CRLF, pinned in
  .gitattributes) run `install.ps1`, which asks as it goes (`Ask`: the default when there's no console or with
  `-Yes`; every question has a switch): install Python 3.12 if there's no 3.11+ (winget per-user, else python.org's
  installer after checking it's signed by the PSF), venv + requirements + `--version` smoke test (re-running =
  update), shortcuts (desktop + Start menu -> `venv\Scripts\pythonw.exe lorakeet.pyw`, icon static/favicon.ico),
  autostart (per-user logon task "Lorakeet" running supervise.pyw; default no), open now. `-Uninstall` stops this
  folder's processes, removes the task/shortcuts only if they point at this folder, removes the venv, and deletes
  logged data only after the user types DELETE (never with -Yes). `install.sh`: the same for Linux (app-menu
  `.desktop`, systemd with `--autostart`) and macOS (`~/Desktop/Lorakeet.command`), `--uninstall`.
  **`lorakeet.pyw`** (the shortcut target): if `/api/version` answers, open the browser; else start supervise.pyw
  (Windows) or server.py (elsewhere) detached, wait up to 90 s, then open it (`--no-browser` for tests). The future
  tray icon goes here. CI runs install, launcher and uninstall end to end on fresh Windows and Linux machines.
  Never run the autostart or shortcut variants on a developer's machine to test them; a throwaway copy with its
  own `lorakeet.toml` (another `http.port`, `data_dir`, a nonexistent `radio.port`) tests the rest safely.
- **Tray icon** (`tray.py`, run by supervise.pyw on its main thread; the server loop runs in a thread; pystray +
  Pillow, Windows-only requirements; `LORAKEET_NO_TRAY=1` or missing libraries = no icon, same behaviour as before):
  status from `GET /api/brief` every 5 s (tooltip + the menu's first line), Open (default/click), Pause/Resume
  (`POST /api/logging {paused}` -> `Mesh.set_paused`: drops the radio, the run loop doesn't reconnect while paused,
  `logging_paused`/`logging_resumed` events, `paused` in status and whoami; prov.js shows "logging paused · resume"),
  Quit (confirm, then the server is terminated and the supervisor exits: the kill-on-close job takes the rest).
  Grey icon while paused. If the supervisor stands down (port taken) the icon closes itself (`exit_when`).
- **Restart:** the tray's Restart Lorakeet (`restart_server`: terminate the server; `serve()` sees `restarting` and starts
  it again after 1 s with the back-off reset), and `POST /api/restart` (any full-access page: the Settings panel in
  alerts.js; `/api/radio/restart` is the same for the Radio/Stations pages) = exit 0 after 1 s when supervised, else a
  409 saying how. Exit code 0 always means "asked to restart" to the supervisor: no crash back-off.
- **supervise.pyw** allows one supervisor per dashboard port (`Local\lorakeet-supervisor-<port>`); before
  0.3.0 it was one per session, so a second install's runner quietly stood down.
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
- **Weekly rhythm** (`heat`) is the one overview card that ignores the range: always the rolling past 7 days
  (`RHYTHM_S`, starting on a whole hour so no weekday-hour slot counts twice). A 24 h range filled one or two
  days and looked like lost data.
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
- **LongTurbo** (2.8's default preset for new US radios: 500 kHz, its own frequency slot) can't be heard by a
  LongFast radio, so those radios are invisible to a LongFast station: nothing to detect or log. The README and
  the Analytics 2.8 card say so, so a shrinking radio count isn't misread.
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

## API index and the MCP server (`api_index.py`, `docs/API.md`, `mcp_server.py`)

- **Every endpoint is listed in `api_index.ENDPOINTS`** (method, path, tier, params/body, summary); `GET /api/index`
  serves it and `tools/api_docs.py` writes docs/API.md from it. `tests/test_api_index.py` fails when server.py
  handles a path the index lacks (or the reverse) or the doc is stale: add the entry, run `python tools/api_docs.py`.
- **Tiers:** `read`; `change` (Lorakeet's own state); `transmit` (goes on air); `page` (Radio/Stations/setup:
  radio config, keys, pairing, peering; this PC only); `internal` (ingest, login, SSE).
- **`mcp_server.py`**: MCP over stdio (newline-delimited JSON-RPC 2.0, protocol 2025-06-18 / 2025-03-26 /
  2024-11-05; initialize, ping, tools/list, tools/call), standard library only, an HTTP client of the local
  Lorakeet (`--url`, default `http.port`). Curated tools return `tidy()` output (unix times -> local ISO by key
  name words, floats rounded, lists cut at 40 with a note, 60k characters max); `api_get` reaches any `read`
  endpoint. `[mcp] allow_changes` / `allow_transmit` (default false) decide which tools are LISTED and callable;
  `page` endpoints are never reachable. They are guardrails for the LLM, not security: any local program already
  has full access. Radios by id or name (`Lorakeet.resolve`: exact name, then a unique substring; ambiguity is an
  error listing the matches). Tool errors are results with `isError`, never exceptions.

## Demo (`demo.py`, `server.py --demo`, the setup page's "Explore a demo")

- `demo.build(path)`: a made-up mesh around downtown Portland (`PORTLAND`, allowed by the release checker as an
  example), fixed seed, timestamps over the last 3 days: "Demo Home" (with a 2 h logging gap), "Hilltop Station"
  (rows marked as synced: `src_rowid`, `received_via = "pair:demo"`), "Demo Car" (own GPS fixes during one drive),
  3 routers, 20 clients (4 never share a position, 2 announce the same key, one rename), public chatter, a private
  channel, encrypted traffic, traceroutes. Built into `<name>.building`, then swapped in. Rebuilt when over 6 h old
  (`stale`), so "the last 24 hours" always has data. `settings['demo']` = {built, seed, home}.
- `--demo` (`setup_demo`): data folder `<data>/demo`, port = `http.port + 1` unless `--http`; map centre Portland, no
  base, `lan` off, sync/remote/backups off; never starts the radio thread, storage, alerts or peering; exits after
  `DEMO_IDLE_S` (3 h) without a page request. `DEMO` (the made-up home radio) puts `demo: true` in whoami, status
  and storage status: prov.js shows a "demo data" chip and hides Radio/Stations; the map says "Demo: a made-up
  mesh, no radio" and hides the backup chip.
- `POST /api/demo` (this PC only; the setup page): `start_demo` reuses a demo already answering, else starts one on
  the first free port from `http.port + 1` (another dev server may hold it) and waits up to 60 s. Tests:
  `tests/test_demo.py`.

## Radio page (`radio_setup.py`, `/radio.html`, `/api/radio*`, this computer only, even when logged in)

Works through the logger's own connection (`mesh.iface`), so nobody stops Lorakeet to use the CLI.
- `checklist(node, firmware, usb, mobile, has_base)`: items {id, title, status ok/suggest/needed/info, why, current,
  options, recommended}. The region is never recommended (a legal setting). Refuses everything when the config
  didn't fully load (`config_complete`): writing on top of a partial config resets a radio.
- `POST /api/radio/apply {changes: {id: value}, names}`: `plan` accepts only values from the item's own options;
  a backup, then one settings transaction (`apply`); 15 s later Lorakeet drops the connection if the radio didn't
  restart, so the page reads values back from the radio, not our edited copy. One change at a time (`RADIO_LOCK`).
- **Backups** (`backup`): the library's `export_config` YAML (restorable with `meshtastic --configure`), minus
  canned messages/ringtone (round trips that can hang), private key included, in `<data>/radio-backups` with
  this-user-only permissions (icacls on Windows, 0600/0700 elsewhere), never overwritten, never served or synced.
- **Channels:** `new_channel` (random 256-bit key), `share_url`/`parse_url` (single-channel `add=true` links),
  `add_channels` (free secondary slot; same name + key = skipped; same name + other key = refused; primary and
  LoRa untouched), copy to another USB radio (`_copy_channel`: open, back up, add, close), QR via segno.
  `GET /api/radio/share` returns a key: logged as shown, this computer only.
- `GET /api/radio/logging?since=`: packets / receptions since the page opened (the "You're logging" check).
- **This station** (`station_settings`, `POST /api/radio/station`, `POST /api/radio/restart`): name + antenna position
  written into the existing lorakeet.toml by `config.update_station` (only the [station] name/location lines change,
  comments kept, validated like a hand edit, old file kept as .bak). Shown re-read from the file, so `pendingRestart`
  says when it isn't applied yet; restart = exit when supervised (supervise.pyw / systemd), else a 409 saying how.
  `radio_setup.location_check` flags a location > 150 km from the median of the radios heard (3+ positions);
  browser locations worse than 1 km are called a rough guess (an IP-based fix put a test VM in Europe). The setup
  page shares the checks (`/api/radio/location-check`) and offers the radio's own GPS fix.
- Every third-party import must be in requirements.txt: `cryptography` (anatomy's AES) once wasn't, and only a
  fresh install showed it. `tests/test_anatomy_crypto.py` now exercises it.
- Tests: `tests/test_radio_setup.py` (a stand-in node from real protobufs); access in `tests/test_login.py`.
  Never test writes on a real radio without the user's go.

## Stations page (`pairing.py`, `/stations.html`, `/api/hub*`, this computer only)

- **Hub mode** (`POST /api/hub/mode`): writes `[sync] mode` and `allow` (`pairing.allow_networks`: LAN ranges and/or
  Tailscale; loopback is always allowed for ingest) via `config.update_section`; restart to apply (the page's banner).
  A hub no longer needs a hand-set `[sync] token`.
- **Pairing** (`POST /api/hub/pair`): `Pairings` (`<data>/stations.json`, this user only) keeps `sha256(token)`, a label
  and, after first use, the station that claimed it; `pairing.make_code` wraps the hub's allowed addresses
  (`hub_addresses`) + token as `lk1-<base64 JSON>`, shown once. `_ingest` checks pairings first (`ok` / `revoked` 401 /
  `other-station` 403), then the hand-configured tokens exactly as before (`sync.authorize`). `POST /api/ingest/hello`
  = the same network + token checks without storing or claiming: the station's connection test (`pairing.hello`,
  plain-words `hint`s per failure).
- **Joining** (`POST /api/hub/join`, the setup page's `joinCode`, `server.py --join CODE|- [--force]`; `-` reads the
  code from stdin, as deploy/setup-station.sh does): tests the code's addresses in order, writes
  `[sync] mode = "collector"`, `hub_url` (the one that answered), `token`. `deploy/station.example.toml` must stay a
  valid config before pairing (`mode = "off"`: a collector without a token is refused, and `--join` loads the config
  first); `tests/test_pairing.py` checks it.
- **Managing** (`POST /api/hub/station`): rename / locate = `settings['station_overrides']` (beat the station's own
  report: `station_name`, `load_station_locations`, `on_report`), revoke, forget (meta, sync status, overrides and
  pairing go; logged rows stay). `_hub_stations` lists stations with data, reports or pairings.
- Tests: `tests/test_pairing.py` (codes, registry, a real hub handler end to end: pair, hello, claim, reuse refused,
  revoke, join).
- **Peering** (hub to hub): the receiver makes a peer code (`POST /api/hub/peer-code`: a `Pairings` entry with
  `kind: "peer"`, claimed by the first sender's install id, `pairing.install_id` in `<data>/install_id`, sent as
  `X-Lorakeet-Hub`); the sender adds it (`POST /api/hub/peer-add`: tested first; stored in `<data>/peers.json`, this
  user only, holding the token) and `PeerManager` runs one `sync.PeerSender` per peer (not on a collector). The
  sender sends every station here that didn't arrive from a peer, plus (per-peer `forward`) peers' stations, never
  back to the hub they came from (`sync_hub[station].via`, set by `Hub.ingest(via=)`). Rows keep their origin
  `src_rowid` (or the rowid where logged), so (station, src_rowid) dedupes across every route. What a peer sees:
  `sync.SHARE_LEVELS` (`default`: no text unless broadcast on a public channel, judged by an rx_hops copy carrying a
  public fingerprint, `public_channel_hashes`: preset names with the default key + this radio's public-key channels;
  no proof = private; `everything`; `receptions`: no text). Private text packets go with `decoded.text/payload`
  removed and `textWithheld`; private `messages` rows don't go. `events` never go. **Every location a peer gets is
  rounded to `PEER_GRID_DEG` (0.03 deg, ~3 km; precision bits capped at 14)**, whatever the share level: positions rows
  (own GPS tracks included), coordinates anywhere in packet raw JSON and in `nodedb_snapshots.data` (`coarsen`),
  lat/lon in packet summaries, station reports (`coarse_report`). Tests: `tests/test_peering.py`.

## Security rules (review 2026-10-09; tests in `tests/test_security.py`)

- **Host check** (`_host_ok`): browsers' requests must be addressed to an IP literal, `localhost` or a name in
  `[http] hostnames`; anything else gets 421 (DNS rebinding would otherwise give a website this PC's full access).
  `/api/ingest*` is exempt (token-checked; stations may use any name).
- **Ingest is untrusted input:** `sync._check_value` types every value by its column (`PRAGMA table_info`): numbers
  in number columns (no bools/NaN), bounded strings, node ids in `NODE_COLUMNS`, JSON in `JSON_COLUMNS`, `ts` within
  [2017, now + 1 day]. Pages still `esc()` every interpolated value, numbers included.
- **Station ownership** (`Hub.claim`, settings `station_owners`): writers are `pair:<id>`, `peer:<hub>`, `shared`,
  `token`. Never the hub's own radio (`own_stations`: connected + settings `own_station`, written by
  `claim_station`); a peer only stations it brought and never ones with existing data; a live pairing or a peer is
  never displaced; the shared token or a new pairing may take over from `shared` or a revoked pairing. A station's
  pairing code is bound only after `claim` succeeds (`Pairings.find` / `bind`). Peers can't send `events`.
- **Peer share proof** (`public_keys`): only the same station's own broadcast rx_hops copies count, never for DMs
  (`to_id` not broadcast or `pki`); public fingerprints exclude any shared with one of this radio's private channels.
  Decoded payloads only for `OPEN_PORTS` when proven public; everything else goes as a bare reception
  (`payloadWithheld` / `textWithheld`). Reports to peers: `PEER_REPORT_FIELDS` + rounded location only.
- **`received_via`** (every station table, layout 2): stamped by `Hub.ingest` with the writer tag (`pair:<id>`,
  `peer:<hub>`, `shared`, `token`); empty = logged here. Never taken from the sender (collectors and peer senders also
  drop theirs). Older synced rows have it empty with src_rowid set.
- **Pausing a peer:** sending side `peers.json` `paused` (`POST /api/hub/peer {action: "set", paused}`):
  `PeerSender.tick` sends nothing and keeps its bookmarks, so it catches up on resume. Receiving side: the peer
  pairing's `paused` (`POST /api/hub/station {action: "pause"|"resume", pairing}`, `Pairings.set_paused`): `_ingest`
  answers 423 after binding, the sender treats it as an error and keeps the rows pending. Unlike Revoke, the code
  keeps working. Tests in `tests/test_peering.py`.
- **Exact locations per peer** (`peers.json` `exact`, the Peers table / add form): `share_rows(..., exact=True)` and
  `coarse_report(..., exact=True)` skip the ~3 km rounding; default off.
- **Deleting what a peer sent** (`POST /api/hub/peer-forget {pairing, dryRun}`): `Hub.owned_by("peer:<hub>")` /
  `delete_from`: every row stamped `received_via = "peer:<hub>"` (any station), plus older unstamped synced rows of
  the stations it owns; for stations that were all its, their meta, sync status, ownership, overrides and peering
  bookmarks too. Revokes the pairing first. Rows logged here or that came another way are never touched. Logged as
  `peer_data_deleted`.
- **Login:** attempts are counted before the scrypt check (`Login.login`), one in flight per address, `HASH_SLOTS` = 2.
- **HTTP:** `_length` refuses negative/garbled Content-Length; `Handler.timeout` 60 s; `MAX_CONNECTIONS` 64
  (`ExclusiveHTTPServer.process_request`); `INGEST_SLOTS` 4; `MAX_JSON` 50 MB.
- Secret-holding files (login.json, stations.json, peers.json, and lorakeet.toml's .bak/.tmp via
  `config._write_private`) are written private from birth (`os.open(..., 0o600)`).
- **Release review (2026-10-09), rules added:**
  - Peers without exact locations never get `decoded.payload` (`_coarse_row`): the packet's own bytes hold the exact
    coordinates the decoded fields were rounded from.
  - A peer hub id can be bound by one LIVE peer pairing at a time (`Pairings.bind`): writer tags are self-declared
    ids. A revoked pairing doesn't count, so the same hub can re-pair with a new code.
  - Public-text proof is per station (`public_channel_hashes(store, own)` returns station -> set): this hub's radios
    use its channel list, other stations the `publicHashes`/`privateHashes` in their reports
    (`channel_fingerprints`, whitelisted in `sync.clean_report`); none reported = nothing proven public.
  - A radio this hub ever logged from itself (`src_rowid IS NULL`, `Hub._logged_here`) is refused to PEERS (a 409
    without "own radio", so the peer keeps the rows pending); pairings and tokens may still make it a station. A peer's 409 other than "own radio" keeps that station pending (`PeerRefused`, status `refused`).
  - Connections: `MAX_PER_ADDRESS` 8 within `MAX_CONNECTIONS` 64 for other machines, `MAX_LOCAL` for this one;
    addresses that could never be served are closed before taking a slot (`ExclusiveHTTPServer._admit`). A refused
    ingest body is read and dropped up to `DRAIN_MAX` so the sender sees the refusal (`Handler._drain`).
  - The demo refuses every `page`-tier endpoint (`_demo_refuses`): its process could otherwise reach this
    computer's radios and lorakeet.toml.
  - View-only clients get no paths (`/api/storage` dbPath/dests/lastBackup) or hub addresses/ids (`/api/sync`).
  - Every response: `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff` (`Handler.end_headers`). CSV cells
    go through `packetsearch.csv_cell` (formula prefixes).
  - MCP: `update_alert_settings` refuses `autoTraceroute*` without allow_transmit (scheduled traceroutes transmit).

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
