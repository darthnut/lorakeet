# MeshCore support: plan

Lorakeet logs Meshtastic today. [MeshCore](https://github.com/meshcore-dev/MeshCore) is a second LoRa mesh firmware with
its own protocol: a Meshtastic radio and a MeshCore radio can't hear each other, so a station logs one or the other.
This is the plan for logging MeshCore too (issue #10). Status: groundwork (steps 1–3) done; the MeshCore link (step 4) is next.

## What a MeshCore radio gives us

Checked on a Heltec Mesh Node T1 running MeshCore Companion USB v1.17.1, through the official Python library
(`meshcore` 2.3.15 on PyPI):

| Need | MeshCore source |
|---|---|
| Every reception, duplicates included | `RX_LOG_DATA` events (companion push 0x88): SNR, RSSI and the raw packet, which the library parses into route type, payload type and the path: one key prefix per repeater (see decision 3) |
| Radios, names, positions | adverts (`ADVERTISEMENT`, `NEW_CONTACT`): public key, name, type (chat, repeater, room, sensor), latitude/longitude |
| Messages | `CONTACT_MSG_RECV`, `CHANNEL_MSG_RECV` (with SNR) |
| The radio's own health | `STATS_CORE` (battery, uptime, errors), `STATS_RADIO` (noise floor, last RSSI/SNR, TX/RX airtime), `STATS_PACKETS` (received, sent, flood/direct, errors), `BATTERY` |
| Radio settings | `SELF_INFO`: frequency, bandwidth, SF, CR, TX power, name |

MeshCore packets carry every hop of their route, where Meshtastic only gives the last relay's byte. Each hop is a
short prefix of the repeater's public key, though, not its id, so a path still has to be resolved to radios (decision
3). The win is that every hop is there to resolve, not just the last one.

## Decisions

1. **Protocol belongs to the station.** One radio is one station and speaks one protocol, so `protocol` is recorded
   per station (with the station's metadata), not as a column on every table. Existing stations are `meshtastic`.
2. **Radio ids.** Meshtastic stays `!1234abcd`. A MeshCore radio is `mc:` plus the first 12 hex digits of its 32-byte
   public key (`mc:a0000017c2be`): 48 bits, short enough for URLs and labels, collision-free in practice. One module,
   `radioid.py`, decides what a radio id is; nothing else matches on `!` or on the length 9.
3. **Paths are key prefixes, resolved like relay bytes.** A MeshCore path holds one 1-, 2- or 3-byte prefix of each
   repeater's public key: the path-length byte's low 6 bits are the hop count and its top 2 bits the prefix size
   minus one. With 1-byte prefixes (common) a hop is as ambiguous as Meshtastic's relay byte, so it is resolved the
   same way, by who is close and recently heard, and a hop that can't be resolved stays a prefix rather than a guess.
   Edges are kept at the prefix length they were seen at. Meshtastic's relay byte (`radioid.relay_byte`) stays
   Meshtastic-only; MeshCore gets its own prefix helper in `radioid.py` (its `mc:` ids carry 6 bytes, enough for
   any prefix size).
4. **A radio link is one class per protocol.** `link.RadioLink` holds what any station needs (the reconnect loop and
   watchdog, pause/resume, events, the live-page stream); `mesh.Mesh` is the Meshtastic link; `meshcore_link` will
   be the MeshCore one.
5. **The MeshCore library is optional.** It's asynchronous (Lorakeet uses threads) and brings Bluetooth and crypto
   packages, so it runs on its own event loop in one thread, and is installed only for MeshCore stations.
6. **Listening only by default.** The US settings MeshCore communities use (910.525 MHz, 62.5 kHz, SF 7) are under
   the same FCC 15.247 bandwidth question as Meshtastic's narrow presets; a logging station never transmits unless
   asked (as with Meshtastic, anything that transmits is opt-in).
7. **One packet, many hearings: the packet hash.** MeshCore has no packet id. The firmware's packet hash (the first
   8 bytes of SHA-256 over the payload type and the payload; for a trace, the path-length byte too) is the same
   whichever way a packet travelled, so it is the key for repeats and for the set of paths one packet took, as the
   packet id is for Meshtastic.
8. **Every raw packet is stored; nothing is attached by guesswork.** The library reports a reception (`RX_LOG_DATA`)
   and the decoded message as separate events. A message is joined to its reception only on evidence (the packet
   hash, or the payload matching), never to "the latest packet heard": that puts one packet's route on another's
   message.
9. **Unset clocks aren't old data.** A MeshCore radio whose clock was never set sends a firmware seed time:
   `1715770351` (2024-05-15), `1772323200` (2026-03-01) or 0, and counts up from it. Lorakeet dates everything by
   when it heard it, and labels the sender's own time against that: right, not set (well behind, from a seed) or
   wrong (set, but off), so an active radio never looks stale (`meshcore_packet.clock_status`).

## Steps

1. **This plan.** Done.
2. **`radioid.py`:** one place for what a radio id is. A refactor: Meshtastic behaviour unchanged; MeshCore ids are
   accepted once `ACCEPTED` includes them. Done.
3. **`link.RadioLink`:** the protocol-neutral part of `mesh.Mesh` moves into a base class. A refactor: behaviour
   unchanged. Done.
4. **MeshCore link** (`meshcore_link.py`). Started: `meshcore_packet.py` parses the raw packets (route, region
   transport codes, path prefixes, payload) and computes the firmware's packet hash, checked against meshcore-bot's
   implementation (the library's own `pkt_hash` is a different, 4-byte hash); `radioid.path_prefix_matches` says
   which radios a hop could be. Still to do: the library's events mapped to Lorakeet's tables (receptions with their
   packet hash and path prefixes, adverts → node info and positions, messages, stats → telemetry), tested against a
   stand-in radio built from real recorded frames. Needs `meshcore` 2.3.14 or newer (earlier versions mis-parse
   and miss acks). Reconnects re-subscribe to the library's events. Channel messages on known channels (Public,
   hashtag channels) can be decrypted; direct messages can't be by a listener and are logged as encrypted.
5. **Pages:** map, Analytics, packets, alerts and replay work as they are; topology and replay use resolved paths
   (unresolved hops shown as their prefix); packet anatomy, airtime and the Radio page are hidden for MeshCore
   stations until they're written for it. New: how many channel messages were sent to the whole mesh rather than
   limited to a region (a region-limited flood carries a transport code), per channel per day, counted from what
   is heard (nothing transmitted), and only where the reception was matched to the message (decision 8).
6. **Several radios, one Lorakeet:** one process logging several radios, each its own station (Meshtastic and
   MeshCore, or Long Fast and Long Turbo, on one Pi).
7. **Later:** MeshCore packet anatomy, airtime for its presets, a settings page, sending messages and traces.
   Zero-hop neighbour discovery (one request, then a minute's listening: nearby repeaters answer with full keys and
   SNR, the best link evidence there is), opt-in like drive pings since it transmits.

## Prior art

[meshcore-bot](https://github.com/agessaman/meshcore-bot) (MIT) logs MeshCore traffic for a bot and a web viewer, and
learned several of the decisions above the hard way: prefix collisions and path resolution, packet-hash dedup,
mis-attached routes, clock seeds. Its standalone payload decoder (`modules/meshcore_payload_decode.py`, after
[meshcore-decoder](https://github.com/michaelhart/meshcore-decoder)) can be reused with attribution rather than
rewritten. Its packet capture also feeds observers' raw packets to shared MQTT analyzers, a possible source of real
traffic for testing step 4 away from any MeshCore mesh.

## Test radio

The test radio, a Heltec Mesh Node T1, runs MeshCore Companion USB v1.17.1 since 2026-10-12, set to 910.525 MHz /
62.5 kHz / SF 7 / CR 5, listening only. Its Meshtastic configuration is backed up in the private radio-backups folder;
reflashing Meshtastic and restoring that backup returns it to its old identity.
