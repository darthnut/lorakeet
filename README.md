# Lorakeet

*LoRa + lorikeet: a chatty bird that sits on your mesh and listens to everything.*

Lorakeet is a logger and dashboard for a [Meshtastic](https://meshtastic.org) mesh. Plug a radio into a
computer and it records **everything that radio hears** (every packet, every over-the-air reception
including duplicates, the radio's own debug log) into SQLite, then lets you explore it: a live map,
analytics, the mesh's real topology, packet-level detail and an animated replay of the traffic.

> **v0.1 preview.** It works and runs every day, but it's young: expect rough edges, and expect the
> setup to get easier. Feedback and bug reports are welcome.

![Traffic replay on the network graph](docs/screenshots/replay.png)

## What it does

- **Logs completely.** Every packet as received (full JSON, nothing dropped), plus one row per
  over-the-air reception parsed from the firmware's debug log: who sent it, which relay you heard it
  from, hop counts, SNR/RSSI, frame length. Gaps when the logger wasn't running are shown as gaps, never
  as zeros.
- **Live map and messages.** Nodes, links, positions, a traffic feed, and messaging on any of your
  radio's channels.
- **Analytics.** Traffic by type and hour, channel utilization and airtime, who relays to you, node
  roles and hardware, per-node pages, readable vs private traffic.
- **Topology and replay.** The mesh as a graph of real RF links (measured and inferred), with a replay
  that animates every packet along the path your radio actually observed. *Grow* mode builds the graph as
  radios first appear; *Fade* mode dims radios that go quiet. Names can be anonymized for sharing
  (`?anon=1`), and `tools/record-viz.mjs` renders the replay to a smooth 60 fps MP4.
- **Packet anatomy.** Any packet rebuilt byte by byte, layer by layer (radio, header, encryption,
  envelope, payload), each field labelled with where its value came from.
- **Health and security checks.** Busy channels, misconfigured nodes, known weak keys, duplicate keys,
  possible impersonation. All passive.
- **Several listening stations.** Small collectors (a Raspberry Pi and a radio) log locally and sync to
  one hub over a private network, so you can compare what different places hear. See
  [docs/PI-SETUP.md](docs/PI-SETUP.md).
- **Alerts and backups.** Watched nodes going silent or low on battery, new nodes, nightly
  integrity-checked database backups to folders you choose.

![The network graph](docs/screenshots/graph.png)

![Analytics summary and traffic over time](docs/screenshots/analytics.png)

## Quick start

You need **Python 3.11+** and a Meshtastic radio connected over **USB** (tested with Heltec V4, Heltec
Mesh Node T1 and SenseCAP T1000-E on firmware **2.7.26**).

```sh
git clone https://github.com/darthnut/lorakeet.git
cd lorakeet
python -m venv venv
venv/bin/pip install -r requirements.txt        # Windows: venv\Scripts\pip install -r requirements.txt
cp lorakeet.example.toml lorakeet.toml          # then edit it: every option is explained inside
venv/bin/python server.py                       # Windows: venv\Scripts\python server.py
```

Open http://127.0.0.1:5190. The radio is detected automatically (or set `[radio] port`).

**For per-reception detail** (relays, duplicates, airtime, replay), turn on the radio's debug log over the
API, once: `meshtastic --set security.debug_log_api_enabled true` (stop Lorakeet first; only one program
can use the serial port at a time). Without it you still get every decoded packet.

**To keep it running:** on Windows, `supervise.pyw` runs it in the background and restarts it if it
stops (start it from Task Scheduler at logon). On Linux, see `deploy/lorakeet.service`.

## Good to know

- **Don't expose it to the internet.** There's no login yet, and the dashboard can transmit (messages,
  traceroutes). By default it only answers on this computer; `[http] lan = "view"` adds read-only access
  from your local network.
- **Listening doesn't transmit.** The only things that transmit are messages and traceroutes you send,
  and scheduled traceroutes, which are off unless you turn them on.
- **What it records about others.** It logs what your radio hears on the shared airwaves, including
  the recipients of other people's addressed packets (on by default; `[logging] store_recipients` turns
  that off). Message contents are only readable on channels you have the key for. Think about this
  before sharing your database or screenshots; the replay's `?anon=1` mode helps.
- **Maps.** OpenStreetMap and OpenTopoMap tiles by default (their usage policies apply: fine for personal
  use). `[map] tiles = "esri"` switches to Esri's maps, including satellite imagery, under Esri's terms.
- **Firmware versions.** Per-reception logging parses the firmware's debug log, whose format can change
  between versions. Tested on 2.7.26. Firmware 2.8 (in alpha) changes node ids and packet formats; it isn't
  supported yet.

## How this was built

Lorakeet was written largely with **Claude** (Anthropic's AI model, via Claude Code), directed, tested
and run on a real mesh by a human. [CLAUDE.md](CLAUDE.md) is the working notes file the AI used: how the
code is organised and the rules it follows, which is also a decent map for human contributors.

## Status and support

A hobby project, maintained on a best-effort basis. Issues and pull requests are welcome, but there are
no promises about response times. Ideas and plans: [docs/STATION-NETWORK-ROADMAP.md](docs/STATION-NETWORK-ROADMAP.md).

**Not affiliated with Meshtastic.** Meshtastic® is a registered trademark of Meshtastic LLC. Lorakeet is an
independent project that talks to Meshtastic radios through the official Python library.

## License

[GPL-3.0](LICENSE). Third-party components and their licenses: [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).
