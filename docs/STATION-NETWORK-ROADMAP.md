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

### 7. Cheap Wi-Fi stations over MQTT  *(planned 2026-10-07)*

**Why:** a full station is a Raspberry Pi plus a USB radio. A Wi-Fi Meshtastic radio on its own (an ESP32
board such as a Heltec V3/V4, ~$25, just a USB power supply) can publish everything it hears over MQTT
(see below), so a new site becomes "plug it in, join the Wi-Fi". Use full Pi stations where detail and
outage-proof logging matter, and cheap MQTT stations to add coverage.

**What a cheap station gives, and doesn't** (checked 2026-10-07 on a V4 over Wi-Fi):
- **Gives:** every packet the radio decodes, with its signal readings (SNR/RSSI), hop start/limit and relay
  byte, the radio's own telemetry, and its identity. Enough for the live map, topology, analytics,
  station comparisons and (for a radio with GPS) drive coverage.
- **Doesn't:** the firmware debug log only goes over USB, so no duplicate copies, no `rx_hops`/`tx_log`, no
  airtime by copy, no hop-by-hop replay detail. And no local buffering: while its internet is down,
  what it hears is lost (an honest gap, drawn as one).

**Design:**
1. **A private broker both sides can reach.** The radio can't run Tailscale, so the broker has to be
   reachable from the internet: a small cloud VM running Mosquitto, a managed broker's free tier, or a
   port forward to the hub (least preferred: it exposes the home network). TLS on, one username/password
   per station (the radio supports both), an ACL so each station can only publish to its own topic.
2. **Radio settings** (`deploy/` gets a helper like `tools/radio_wifi.py`): MQTT module on, the broker's
   address and the station's credentials, a private root topic, uplink on the channels to log, downlink
   off (nothing comes back onto the air), "encryption enabled" (packets stay encrypted in transit; the hub
   decrypts with the channel keys it already has), and the map-report option off.
3. **Hub side** (`mqtt_ingest.py`, `[mqtt]` in lorakeet.toml): subscribe, unwrap each ServiceEnvelope
   (packet + channel + gateway id), and store it like any station's reception: `station` = the gateway's
   node id, `src_rowid` = a stable hash (gateway + sender + packet id) so redeliveries are no-ops,
   `raw.transportMechanism` = MQTT so the source is never ambiguous. Coverage minutes come from the
   station's own telemetry, as for other stations.
4. **Everywhere it shows:** MQTT stations get a badge in the station picker and station table, and the
   comparison grid notes that they can't count duplicate copies.

**Effort:** the hub side is moderate (one new module, the paho-mqtt dependency, tests with a local
Mosquitto); the broker is the main decision. **Not doing:** the public Meshtastic MQTT feed (that's the
"whole mesh from the internet" idea below, a separate decision about what this log is for).

## Follow up: MQTT  *(not decided)*

*The practical plan for cheap stations is item 7 above; this section is the background.*

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

**Done ahead of the release (2026-10-07):** node-number linking across the upgrade (`nodeids.py`, analytics
views), a "likely 2.8" tag and adoption card, the reception-logging alarm, and the one log-format change found
by diffing `printPacket` in v2.7.26 against v2.8.1 (`Ch=` now decimal; it would have stored wrong channel hashes,
not stopped). Already on the local mesh: 5 radios numbered the 2.8 way, 3 of them with their old number joined.
Still to do at upgrade time: edit `[base] id` / `station_tokens` to the new numbers; check signing fields.

**Order when 2.8 goes stable:** (1) id aliasing + log-format checks in the dashboard, tested on a copied DB;
(2) upgrade one test station (with its config saved) and watch rx_hops, signing and ids for a day;
(3) the main hub's radio last. Bluetooth-only nodes are upgraded from the phone app.
