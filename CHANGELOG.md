# Changelog

What changed in each release of Lorakeet. Every release is a preview so far. The full notes, with videos,
are on the [releases page](https://github.com/darthnut/lorakeet/releases). Your version is in the
`VERSION` file and shows when you hover over the logo in the dashboard.

Updates never remove or rewrite data you've logged. See "Updating" in the README.

## 0.5.1 (2026-10-16)

**New**
- **Show menu on Visualizations:** filter the graph, map and replay by radio type (routers and repeaters, or
  clients), network (Meshtastic, MeshCore), link evidence (measured only) and link signal; colour radios by centrality,
  what each would cut off (single points of failure), packets heard or links. Your stations always stay; the choice is
  remembered per browser.
- **Critical relays** (Analytics): which radios the mesh, as your stations hear it, depends on: each radio's share of
  the shortest routes between others, and the radios and links whose loss would leave others unable to reach any of
  your stations (single points of failure). Worked out from the link graph the Visualizations page draws, with how
  much of it is inferred said plainly.
- **MeshCore on the network graph and in the replay:** links from the repeaters each packet came through (a
  repeater's key prefix is drawn as a radio only when exactly one known radio has it, repeaters preferred), and every
  MeshCore reception replayed along its whole path: sender (when it's named), each repeater, the station.
- **Airtime for MeshCore stations** (Analytics): channel time of everything the radio heard, by type and by the
  radio on the air, calculated from each packet at the radio's own settings, with the radio's own measured receive
  and transmit airtime alongside. MeshCore packet types share the Meshtastic groups' colours (text, node info,
  routing) instead of all being "other".
- **The demo has a MeshCore side:** "Demo MeshCore", a second radio in Demo Home's box, hearing a small made-up
  MeshCore mesh (repeaters, chat radios, adverts on the map, Public channel conversations, direct messages), so every
  MeshCore view can be tried before a radio is plugged in. Its Public messages decrypt in packet anatomy, and a
  #demo hashtag channel opens when you type its name there.

**Fixed**
- **MeshCore radios are labelled by name on the map** (they have no short name: the map showed their id's last
  digits), and a hub lists another station's MeshCore radios as heard by that station.
- **"Station isn't syncing" alerts:** raised once per silence (a hub restart used to raise it again), under the
  name you gave the station, and never for a station whose pairing you revoked. **Forget** takes a station off the
  Stations list even when its logged data stays (it used to stay listed); it comes back if it sends again.

## 0.5.0 (2026-10-14)

**New**
- **MeshCore stations.** Lorakeet can log a MeshCore mesh as well as Meshtastic: plug in a MeshCore companion radio
  (USB companion firmware) and it records every reception (with the path of repeaters it came through, its signal and
  the packet hash, so repeats of one transmission are recognised), the radios' adverts (names, types, places), the
  messages it can read and the radio's own counters. It only listens: it never makes a MeshCore radio transmit.
  Everything that only makes sense for Meshtastic (sending, traceroutes, relay bytes, airtime, station comparison)
  is kept to Meshtastic radios, and a peer hub gets MeshCore data under the same rules as Meshtastic's: rounded
  locations, and a packet's raw bytes only when the share allows. New guide: docs/MESHCORE-GUIDE.md.
- **Setting up a MeshCore station:** the setup page has a radio type choice (Meshtastic or MeshCore), and the
  installers ask whether to add the MeshCore library (`install.ps1 -MeshCore`, `install.sh --meshcore`; kept up to
  date once installed). A Pi station gets it from deploy/update-station.sh when its lorakeet.toml names a MeshCore
  radio.
- **Several radios, one Lorakeet.** `[[radios]]` entries in lorakeet.toml add more radios to one Lorakeet, each
  logged as a station of its own: a Meshtastic and a MeshCore radio in one box, say. Each has its own port, name and
  pairing with the hub; the live map and Radio page are the main radio's. Pausing logging lets go of them all. The
  radios open their ports at least 20 s apart: opening a port restarts a radio, and two restarting together tripped a
  Raspberry Pi 4's USB current limit, which cut every port, the data drive included.
- **Meshtastic and MeshCore on one map.** Where one Lorakeet has both kinds of radio, MeshCore radios carry an "MC"
  tag and the map gets a Meshtastic / MeshCore / Both switch (remembered per browser). Their channel messages are
  separate conversations, and the Messages tab shows a MeshCore station's under "MeshCore channel n".
- **MeshCore in the packet browser:** every reception with its route, the repeaters it came through, signal and
  packet hash, filtered by payload type or by a repeater anywhere in the path; a **Network** filter on Packets
  (Meshtastic, MeshCore or both), with MeshCore packets tagged.
- **MeshCore packet anatomy.** Any MeshCore packet, byte by byte from the bytes its radio received: the radio
  settings and time on air, the header, region codes, the path of repeaters (named where known), the payload by type
  (an advert's key, clock, place and name; a message's hashes and MAC), the hash that recognises it, and every time
  it was heard, by which path. Public channel messages are decrypted, and a hashtag channel's when you type its
  name; the MAC proves the key, so nothing is shown decrypted by guesswork.
- **Rename a pairing** on the Stations page (stations and peer hubs): only its name changes; the station or hub
  keeps sending on the same code.

**Fixed**
- **A USB data drive that resets comes back by itself.** When the drive dropped off the bus and came back under a
  new name (sda1 became sdb1), the old mount stayed and Lorakeet stayed on the SD card. The drive watch now clears a
  dead or unreadable mount, checks the drive (fsck) and mounts it again; a drive that fails the check is left
  unmounted for a person to look at.
- **No needless reconnect after a Pi boots.** A Pi has no clock battery: it boots on its shutdown time and network
  time then jumps it forward, which the radio watchdog read as that long a silence and reconnected the radio. The
  watchdog now measures silence on a clock that time changes don't move.
- **Scheduled traceroutes and messages go only to Meshtastic radios,** whatever else the database holds: an id the
  Meshtastic library can't address is refused rather than turned into some other radio's number.

## 0.4.1 (2026-10-14)

**New**
- **A proper download.** Each release now has a `Lorakeet-x.y.z.zip`: one folder with only what's needed to
  run Lorakeet (no tests or developer files), and a `SHA256SUMS.txt` to check the download. CI builds and
  attaches them, and the install tests run from that zip, so what's tested is what people download.
- **A USB drive for a station's data** (Linux stations): `deploy/data-drive-setup.sh` formats a thumb drive, mounts
  it at every boot and moves Lorakeet's data onto it, so the SD card takes almost no writes. If the drive is missing
  or fails, Lorakeet carries on logging to the card within a minute and moves back once the drive is healthy and
  the card's rows have reached the hub. See "A USB drive for the data" in docs/PI-SETUP.md.

**Fixed**
- **Two identical radios on one computer.** Lorakeet now remembers the radio it logs from by its USB serial number
  and finds it on whichever USB port it's in, so two Heltec V4s (say, one for Meshtastic and one for MeshCore) can
  go in any ports, in any order. With several radios plugged in and none of them the remembered one, it no longer
  takes the first it finds: the Radio page lists them (with each Heltec's radio id) and asks which one to log
  from. A radio swapped for another, with nothing else plugged in, is still picked up by itself. `[radio] port`
  can now also be a stable name like `/dev/serial/by-id/...`.
- **An occasional failed request on Windows** when a page opened many connections at once: Windows refused
  the ones that didn't fit in the server's queue of 5 waiting connections (now 128).

## 0.4.0 (2026-10-13)

**New**
- **Every radio setting, saved in one go.** The Radio page's new **Advanced** section shows every setting the radio
  reports (device, LoRa, position, power, network, screen, Bluetooth, security and every module), with plain help,
  units and a search. Change as many as you like, in any section, together with the checklist and the radio's
  name: **Review and save** writes them all at once, with one backup and one restart, then reads each one back
  from the radio. (The Meshtastic apps restart the radio after every settings page.) Changes that can cut
  Lorakeet off from the radio, break logging, transmit a lot, put traffic on the internet or are legal settings
  are marked in red and need a confirmation. Passwords and PINs are write-only; the radio's keys aren't changed
  here; managed mode can only be turned off.
- **Undo last save** (Radio page): puts back exactly the settings the last save changed, from the backup taken
  just before it (a replaced password too, never shown), after the same review; one write, one restart. Settings
  changed elsewhere since are left alone. Pressing it again redoes.
- **Settings page** (top bar → Settings): Lorakeet's own settings in one place, each explained: alerts (moved here
  from the bell, which now just lists them), this station, the radio connection, access from other devices,
  map, base station, backups and storage, remote commands and LLM access. One **Save**: alert settings apply at
  once; the rest are written to `lorakeet.toml` together (checked like a hand edit, comments kept, the previous
  copy kept as `lorakeet.toml.bak`), each marked until a restart applies it, with a Restart button. On this
  computer only (devices on your network see the alert settings).
- **Diagnostics for bug reports** (Settings → Help and diagnostics): one zip with versions, the radio's model,
  firmware and the settings that matter, the database's size, backups, sync, recent warnings and errors, and your
  settings, with names, radio ids, addresses, paths, Wi-Fi networks, locations and keys taken out before anything
  is written. **See what's in it** shows it first; nothing is sent anywhere. If Lorakeet won't start (even with a
  broken `lorakeet.toml`), `python diagnostics.py` in its folder saves the same file.
- **🌈 Groovy mode** (Visualizations, or press V), just for fun: the replay in psychedelic colours. Comet trails
  follow every packet, sparks fly where each hop lands, rainbow ripples mark each transmission, radios glow when
  they speak, links and radios shift through the palette over a lava-lamp background (the map's tiles cycle
  colour instead). Four vibes (Rainbow, Neon, Lava lamp, Aurora), three intensities up to "Far out", and a
  kaleidoscope. `?groovy=neon` in the address turns it on for sharing; `tools/record-viz.mjs --groovy lava` records it.

**Changed**
- **Much less wear on a Pi's SD card.** Lorakeet commits what it logs every 2 seconds instead of once per row, the
  radio's debug log every 15 seconds (its live view still updates every 2), and the station Wi-Fi watchdog no
  longer fills the system log with a few lines a minute. Measured on a listening Pi: card writes down from about
  1 GB to 450 MB a day. Anything logged in the last 2 seconds is still written when Lorakeet stops or restarts.
  An existing station picks up the watchdog change when `deploy/setup-station.sh` is run again.

**Fixed**
- **A replacement database no longer loses data at the hub.** A station whose database was replaced (a new SD
  card, a reinstall) counted rows from 1 again, and the hub, which recognises rows by their number, ignored every
  new one as already received. New databases now start counting at a time-based number.
- **Restarting on Linux** (a Pi) could fail for up to a minute with "port 5190 unavailable" after any recent
  connection to the dashboard.
- **Maps after a drive:** a moving station's links are drawn from where it was when it heard each radio, not from
  where it's parked now; radios only it heard are estimated near where it heard them. (After a trip, a car parked
  at the coast showed links to radios in the city it passed through, and pulled those radios to the coast.) Links
  longer than any radio link (300 km: a radio reporting a position from another state) are left off the map, and
  the replay says how many.
- **"Public key changed"** counts only keys heard over the air, dated when the new one was first heard. A radio's
  stored list of old keys, copied when a station connects, no longer looks like a change.
- **A peer that's offline** (a laptop asleep) is logged once, and again when it's back, instead of every minute.

**For contributors**
- `server.py` (3,500 lines) is split into modules by job: `store.py` (the database), `mesh.py` (the radio link),
  `web.py` with `web_radio.py` and `web_hub.py` (the HTTP routes), `station.py`, `channels.py`, `fwlog.py`,
  `peers.py` and `runtime.py`; `server.py` is the entry point. Nothing changes for users. CLAUDE.md has the map.
- **Browser tests:** `tests/browser` opens every page in headless Chromium (Playwright) against a real Lorakeet on
  the demo mesh: data renders, nothing throws or fails, Settings saves, no sideways scrolling on a phone. They found
  the Radio and Stations pages missing the Settings link (fixed).
- **CI:** a lint (`ruff check .`, `ruff.toml`), a coverage report, the browser tests, and macOS (Apple silicon) and
  Windows on ARM runs on the public repository.
- **SECURITY.md** (report privately through GitHub), **CONTRIBUTING.md**, a **code of conduct** (Contributor
  Covenant 2.1) and a short **docs/ARCHITECTURE.md**.

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
