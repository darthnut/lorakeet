# Testing Lorakeet

Thanks for trying Lorakeet. It's a preview: it runs every day on a real mesh, but you'll be among the first to install
it somewhere else, and that's exactly where the rough edges show. This page says what to try, what's useful to send
back, and what to keep to yourself.

## Before you start

- **A radio is optional at first.** No radio yet? Install anyway and choose **Explore a demo** on the setup page: a
  made-up mesh around Portland, Oregon, with every view filled in. Nothing you do there touches a radio.
- **With a radio:** a Meshtastic radio on USB (tested: Heltec V4, Heltec Mesh Node T1, SenseCAP T1000-E) on firmware
  **2.7.x**. Firmware 2.8 should work but hasn't been tested on a real radio yet; reports from 2.8 are very welcome.
- **Install:** follow the README's [Quick start](README.md#quick-start). On Windows, double-click **Install Lorakeet**.
- **Your data stays on your computer.** Lorakeet sends nothing anywhere unless you set up peering or a station. The
  pages load map tiles (OpenStreetMap and OpenTopoMap, or Esri if you choose it) and two page libraries from
  cdnjs.cloudflare.com; installing fetches packages from PyPI (and Python itself, if you let it).

## What to try

Tick off whatever applies to you; every item is useful on its own.

1. **Install** (and on Windows: did the shortcut and the tray icon appear? did it offer to install Python?).
2. **Setup page:** did it find your radio? Was anything confusing?
3. **The demo:** open it from the setup page. Does every page show something? Anything broken or unclear?
4. **The Radio page:** does the checklist match your radio? Read the suggestions; change something only if you're
   comfortable (it backs up the radio's settings first). Please **don't change the region** unless it's wrong.
5. **Let it log for a day or two,** then look around: the map, Analytics, Visualizations (Graph, Geographic, the
   replay, Coverage if you have a moving station), Packets and a packet's anatomy.
6. **The tray icon (Windows):** Pause logging, open the Meshtastic app on the same radio, then Resume. Restart.
7. **Another device (optional):** set `[http] lan = "view"` in `lorakeet.toml`, restart Lorakeet (on Windows, allow
   Python or TCP port 5190 on Private networks in the firewall if asked), and open the dashboard from a phone on
   your Wi-Fi at this computer's address. With a
   password (`--set-password`), log in from the phone.
8. **A second station (optional):** a Raspberry Pi or another computer with a radio, paired with a code from the
   Stations page ([docs/PI-SETUP.md](docs/PI-SETUP.md)).
9. **Peering with us (optional):** if you'd like to trade data with another tester's hub, ask in an issue and we'll
   set it up together. The defaults keep private-channel text and direct messages back and round every location
   to about 3 km.
10. **Uninstall** when you're done (*Uninstall Lorakeet*, or `./install.sh --uninstall`): does it leave anything
    behind that it shouldn't?

**Things that transmit** (they go out on the mesh under your radio's name): messages and traceroutes you send, and
these options, which are all off unless you turn them on: scheduled traceroutes, drive pings, replies to `lk`
commands, and an LLM allowed to transmit. Plain logging never transmits.

## What to send back

Open an issue on GitHub (there's a form for bug reports and one for general feedback). The most useful details:

- **Lorakeet's version** (hover over the logo, or the `VERSION` file), **your OS**, **your radio and its firmware**.
- **What you did, what happened, and what you expected.** "I clicked X and nothing happened" is a perfect report.
- **A screenshot,** ideally of an anonymized view: add `?anon=1` to a Visualizations address and every radio is
  renamed, message text hidden and the map moved somewhere else.
- **The end of the log,** if something failed: `server.log` in Lorakeet's data folder (Windows:
  `%LOCALAPPDATA%\Lorakeet`, Linux: `~/.local/share/lorakeet`, macOS: `~/Library/Application Support/Lorakeet`).
  Read it before posting: it can include your user name in file paths, radio ids and names.

General impressions count too: what you expected to find and didn't, what wording confused you, what you'd want next.

## What to keep to yourself

Issues on GitHub are public. Never post:

- `lorakeet.toml` (it can hold a sync token), `login.json`, `stations.json` or `peers.json` (tokens);
- anything from `radio-backups` (your radio's private key and channel keys) or a channel's share link or QR code;
- `mesh.db` (everything your radio heard, including other people's radios and positions);
- your exact location or your station's antenna position.

If a problem can only be shown with something private, say so in the issue and we'll find another way.

## Known limits

- Per-reception detail (relays, duplicates, airtime, the replay's paths) needs a radio on **USB**: radios send their
  debug log only over USB.
- Radios on **LongTurbo** (firmware 2.8's default for new US radios) can't hear LongFast ones at all, so a LongFast
  station never sees them.
- No autostart on macOS yet. The tray icon is Windows-only.
- It's plain HTTP for your own network: never expose the dashboard's port to the internet.
