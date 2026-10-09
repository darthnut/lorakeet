# Changelog

What changed in each release of Lorakeet. Every release is a preview so far. The full notes, with videos,
are on the [releases page](https://github.com/darthnut/lorakeet/releases). Your version is in the
`VERSION` file and shows when you hover over the logo in the dashboard.

Updates never remove or rewrite data you've logged. See "Updating" in the README.

## 0.3.0 (2026-10-09)

The biggest release so far: a proper install, a radio setup page, several stations with no file editing, sharing
with other people's hubs, a demo, and an API an LLM can use. Re-run the installer after updating (see below).

**Getting started**
- **Install by double-click on Windows:** *Install Lorakeet* offers to install Python, adds a **Lorakeet** shortcut
  (desktop and Start menu), and asks about starting at sign-in; *Uninstall Lorakeet* removes it again and keeps
  your logged data unless you type DELETE (and then only Lorakeet's own files). It uses Python 3.11 to 3.14 (the
  Meshtastic library doesn't support 3.15 yet), never the Microsoft Store's, and rebuilds a Python environment
  that no longer works. `install.sh` sets up the same on Linux, macOS and Raspberry Pi (`--autostart`: a service
  that starts at boot on Linux; `--uninstall` always keeps your data).
- **Tray icon (Windows):** shows what Lorakeet is doing, with Open, **Pause logging** (frees the radio's USB port
  for the Meshtastic app or a flasher; the time is a gap in the log), **Restart** and Quit. Restart is also in
  the bell → Settings, and comes straight back without the crash back-off.
- **Radio page:** connect a radio, check the settings that matter for logging (each explained, changed in one
  go after a backup), make private channels and share them by QR code or onto another USB radio, set this
  station's name and antenna position, and confirm it's logging. This computer only.
- **Explore a demo:** a made-up mesh around Portland, Oregon (radios, chatter, a second station, a drive, a
  logging gap) to try every view before your radio has logged anything. Setup page, or `server.py --demo`.

**Several stations**
- **Stations page:** make a computer the hub (local network and/or Tailscale) and pair each station with a
  one-time code: in its setup page, its Stations page, or `server.py --join` on a Pi (tested before it's saved).
  The hub lists every station and can rename, place, revoke or forget it. Hand-set shared tokens still work.
- **Peering:** two hubs share what they log, each direction set up and controlled by the side that sends. By
  default no private-channel text or direct messages; every location rounded to about 3 km unless you allow
  exact ones; **Pause** from either side (nothing is lost; it catches up on resume); **Revoke and delete what it
  sent** removes exactly one peer's data.
- docs/PI-SETUP.md is rewritten around pairing codes; the station setup script asks for the code.

**Access, API and LLMs**
- **Log in from other devices:** `python server.py --set-password`; a device that logs in can send and change
  settings. `[http] lan = "login"` hides the dashboard from the rest. `--clear-password` removes it.
- **For LLMs:** `mcp_server.py`, an MCP server for Claude Desktop, Claude Code and other apps. It reads
  everything; changing Lorakeet and transmitting stay off until you allow them under `[mcp]`; radio settings,
  keys and pairing are never offered. `python mcp_server.py --print-config` shows how to add it.
- **Documented API:** docs/API.md, and `GET /api/index` lists every endpoint.
- **Versions:** `VERSION`, `--version`, `/api/version`, the logo's tooltip, and each station's version.

**Security**
- **If you open the dashboard by a computer name** (not an IP address or `localhost`), add it to the new
  `[http] hostnames`: other names are refused, so a website can't reach it through your browser.
- Everything stations and peers send is type-checked; a hub tracks which source owns each station, so one can't
  write over another or over the hub's own radios.
- Peers never get exact coordinates when locations are rounded (not even inside the raw packet), never private
  text proven "public" by planted records, and each station's traffic is judged by its own radio's channels.
- Login guesses are counted before the check and rate limited per device; connections are limited per device,
  and this computer always gets in.
- Devices on the network with view-only access no longer see file paths; pages can't be framed by other sites;
  CSV exports can't run as spreadsheet formulas; settings files that can hold a token are private.

**Fixed and changed**
- **Weekly rhythm** always shows the past 7 days, whatever the range picker says.
- Packet anatomy failed on a fresh install (a missing requirement).
- `lorakeet.toml` with a byte-order mark (some Windows editors add one) failed to load.
- Two Lorakeets on different ports can run side by side on Windows.
- The setup page scrolls with the mouse wheel, names the radio it found, and warns when a location is far from
  the radios heard or only a rough guess.
- With `[http] lan = "off"`, other devices are refused even on a hub.
- A broken `lorakeet.toml` is reported straight away instead of the shortcut doing nothing; a tray icon problem
  no longer stops logging.
- A refused station now hears why (it used to report "unreachable"). The top bar fits on phones.
- **LongTurbo:** firmware 2.8's default preset for new US radios can't hear LongFast, so those radios never
  appear. The README and the 2.8 adoption card say so.

**Updating from 0.2.x**
- **Re-run the installer** (Windows: *Install Lorakeet*; elsewhere `./install.sh`): new requirements `segno` and
  `cryptography`, plus `pystray` and `Pillow` for the tray icon on Windows.
- **Restart it:** stop the old Lorakeet (close its window, or end the Task Scheduler task you made for it) and
  start it with the new **Lorakeet** shortcut; on Linux with the service, `sudo systemctl restart lorakeet`.
- The database gains a `received_via` column on every logged-data table (how each row arrived); nothing is
  rewritten.
- Add any computer names you open the dashboard by to `[http] hostnames`.
- Update your stations too. An older station still syncs, but if you peer, its packets reach peers without their
  contents until it's updated (it doesn't yet report which of its channels are public).

## 0.2.2 (2026-10-08)

- **Station health timeline:** a Raspberry Pi station records power dips and network drop-outs as they happen.
- **Coverage:** areas a drive never explored are dimmed.
- **Placing radios from drives:** a radio heard directly from 3+ spots spread 1 km+ gets an estimated location.
- **"No hops to spare" health check:** radios whose packets mostly arrive on their last hop.
- **Firmware 2.8:** settings that name a radio by number follow it to its new number.
- **Fix:** the Messages tab showed private-channel messages from phone exports under LongFast.
- A logo, re-taken screenshots, and captions saying how each one is anonymized.

## 0.2.1 (2026-10-08)

- **Drive coverage** (Visualizations → Coverage): what a station in a car heard along its route, and where the
  mesh heard it. Optional drive pings (they transmit, off by default).
- **Moving stations** follow their GPS route on the map and in the replay.
- **Shareable maps:** `?anon=1` also moves the map to a decoy place.
- **Remote commands:** `lk status`, `lk gps`, `lk help` by private direct message (read-only, off by default).
- **Fixes:** pages stopped loading with several tabs open; "database is locked" under load (now WAL mode);
  backups to Google Drive for Desktop; firmware 2.8.1's decimal channel hash.
- **Stations:** a Wi-Fi watchdog, and system logs kept across reboots.

## 0.2.0 (2026-10-07)

- **Texts in the replay:** readable messages in a chat panel, each linked to its sender.
- **Key warnings:** radios with a compromised or shared public key are flagged everywhere.
- **Radios on your network:** `[radio] host` for Wi-Fi and Ethernet radios.
- **First-run setup page.**
- **Firmware 2.8 readiness:** renumbered radios keep one history; a 2.8 tag and adoption card; an alert if
  per-reception logging stops.
- **Fixes:** the Bluetooth PIN and Wi-Fi and MQTT passwords are never logged.
- Tests on a synthetic mesh, run on every push.

## 0.1.0 (2026-10-07)

First public preview: logging, live map and messages, analytics, topology with an animated traffic replay,
packet anatomy, health and security checks, multiple listening stations.
