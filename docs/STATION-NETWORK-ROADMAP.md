# Roadmap: a network of listening stations

Written 2026-10-06, after the first remote station (a Pi 3 + SenseCAP T1000-E) started syncing.

**The goal:** many listening stations spread around the area, all feeding one database, to map real
coverage, find weak spots in the mesh, locate nodes that never share a position, and see how traffic
actually moves. Stations are listen-only (CLIENT_MUTE), so they add no load to the mesh.

## Where we are

Everything below already works for any number of stations, not just two:

- Every logged row is tagged with the `station` that heard it.
- Collector → hub sync over Tailscale; stations log locally first and catch up after outages.
- Per-station views, a combined view (each packet counted once), and the live map fed by every station.
- Topology and replay end each reception at the station that heard it.
- A two-station comparison on the Analytics page.

## Next, in order

### 1. Compare N stations, not pairs  *(done 2026-10-06: the node × station capture grid on Analytics)*
- Per node: **which stations hear it, and how well** (best SNR/RSSI at each, hops, how often). A node
  heard by only one station, weakly, is a **weak spot**; a node every station hears is well covered.
- A station × node grid (heat-shaded), and "nodes only this station hears" per station.
- Topology with every station as a known vantage point.

### 2. A location for every station  *(done 2026-10-06: config, sync, maps, estimator anchors; coordinates still to be entered)*
- Fixed stations don't need GPS: `[station] lat/lon` in each station's `lorakeet.toml`, synced to the hub.
- Then: estimate the position of nodes that never share one from **signal strength at several located
  stations** (today's estimate uses one vantage point). Show the uncertainty honestly, as now.

### 3. Coverage maps
- From positioned nodes and every station's receptions: how many stations hear each area, and how
  strongly. Gaps show where a relay (or another station) would help most.
- Per-station "what I can hear" footprints on the map.

### 4. Running many stations
- **One-command provisioning** *(done 2026-10-06: `deploy/setup-station.sh`)*: packages, venv, serial access,
  config template, Tailscale, service; re-runnable; tested on the test station.
- **Station health** *(done 2026-10-06: the "Location and health" table on Analytics)*: last contact, backlog,
  power flags, temperature, uptime, radio firmware, software fingerprint; alerts fire when a station goes quiet.
- **Remote updates** *(done for one station at a time: `deploy/update-station.sh`, staged + parse-checked)*;
  next: run it across every station.

### 5. Per-station tokens  *(done 2026-10-06: `sync.station_tokens`, not yet switched on here)*
- Today every station shares one secret. With many stations, each gets its own token, bound to its
  station id on the hub, so a lost or compromised station can be revoked alone.

### 6. Scale the hub
- Each station adds roughly 1-2 MB/day. SQLite copes with dozens of stations, but the combined view's
  de-duplication runs on every query: precompute it into a table as data grows.
- The hub belongs on an always-on machine (the Raspberry Pi hub idea), not a desktop that sleeps.

## Follow up: MQTT  *(not decided)*

**What it is, in plain terms.** MQTT is a simple message-passing system: devices *publish* messages to
a server (a "broker"), and anything *subscribed* to that broker receives them. Meshtastic radios that
have Wi-Fi can publish every packet they hear to a broker. Nothing is needed between the radio and the
broker except Wi-Fi.

**Why it might matter here, two ways:**

1. **Cheap stations.** A Wi-Fi Meshtastic node on its own (~$25, no Pi) could publish what it hears to a
   **private broker** that our hub subscribes to. Setting one up at a friend's house would be: plug it in,
   join their Wi-Fi. *Trade-off:* less detail than a Pi station. MQTT carries the packets the node
   decodes (with its signal readings), but not every duplicate copy or the radio's own debug log (no
   reception mining, no airtime-by-copy). A mix could work well: a few full Pi stations where detail
   matters, many cheap MQTT stations for coverage.
2. **The whole mesh, from the internet.** Many nodes already publish to the **public** Meshtastic MQTT
   server, each message tagged with which node heard it. Subscribing would add dozens of listening points
   across the region without any hardware. *Trade-off:* it changes what this log is ("what our stations
   heard" becomes "what the internet-connected part of the mesh heard"), and those packets arrive over
   the internet, not the air. The database already records how each packet arrived (`transportMechanism`),
   so they could be kept apart, but it's a decision about the purpose of the log.

**Questions to settle before building either:** which kind (private, public, or both); where a private
broker would run (the hub, or a small cloud service); what the public feed would cost in storage; and
how MQTT-sourced data is labelled everywhere it's shown.

## Follow up: firmware 2.8 readiness  *(noted 2026-10-07; nothing to do until 2.8 is a stable release)*

All our radios run **2.7.26.54e0d8d**, still the newest stable ("Beta") release as of 2026-10-07. The 2.8
line is alpha only: 2.8.0 was revoked twice (the first build broke the LR1110 radio on **T1000-E** devices
after a fresh flash; the second had a packet-signing bug), and 2.8.1 (2026-10-01) is an alpha whose
signatures don't interoperate with 2.8.0. Release notes: github.com/meshtastic/firmware/releases.

**What 2.8 changes that matters to us:**
- **Node ids come from the public key, not the MAC.** Every station id (`station` column, `analytics.HOME`,
  `[sync] station_tokens`, `[base] id`) changes when a radio upgrades. Needed first: a
  node-id alias table (old id → new id, matched on public key) that the analytics, station picker, alerts
  and sync all resolve through, so history stays joined across the upgrade.
- **Packet signing (XEdDSA) and ack proofs.** New header/payload fields: the anatomy view, `RX_FIELDS`
  parsing and the rx_hops/tx_log columns need updating; signing status is worth showing (a signed packet
  is a strong "this really came from that node").
- **Debug log format.** Per-reception logging parses the firmware's "Packet RX"/"Started Tx" lines; check
  every regex against 2.8 output before upgrading the home logger, or rx_hops silently stops.
- **Position and telemetry become opt-in; precise position is refused on the public channel.** Expect fewer
  positions and battery readings as neighbours upgrade: a drop on the charts is the firmware, not the mesh.
  The estimator (`insights.estimate_positions`) and station tracks matter more.
- **New US nodes default to LongTurbo**, which can't hear LongFast. Worth an analytics note if the local mesh
  starts to split (new nodes that only ever appear via MQTT or not at all).
- **Automatic variable hop limits** (helps nodes stuck at the edge of hop 3), **traffic management**
  (dedup, rate limiting, congestion-aware position intervals), **noise floor tracking** (a natural new
  health chart), mesh beacons, ham regions (licensed nodes stop relaying unlicensed traffic), LoRa config
  applied without reboot, GPS clock sync every 30 min, and an official **MCP server** for driving devices.
- Upgrade advice from the notes: export the config first; on a bootloop, full erase and flash.

**Order when 2.8 goes stable:** (1) id aliasing + log-format checks in the dashboard, tested on a copied DB;
(2) upgrade one test station (with its config saved) and watch rx_hops, signing and ids for a day;
(3) the main hub's radio last. Bluetooth-only nodes are upgraded from the phone app.
