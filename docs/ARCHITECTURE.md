# How Lorakeet fits together

A short overview for people new to the code. [CLAUDE.md](../CLAUDE.md) has the detail on every part and the rules
the code follows.

## The pieces

```
 radio (USB or TCP)
      │  packets, node info, the firmware's debug log
      ▼
 mesh.py  Mesh ──► store.py  mesh.db (SQLite: every packet, every reception, telemetry, positions…)
      │                 ▲            debug.db (the firmware's log, kept 7 days)
      │ live state      │ reads
      ▼                 │
 web.py  HTTP + server-sent events ◄── analytics.py, topology.py, insights.py … (read-only queries)
      │
      ▼
 static/  the pages (plain HTML + JavaScript, no build step)
```

- **One process** (`server.py`) holds the radio, writes the database and serves the dashboard on port 5190.
  `supervise.pyw` (Windows, with the tray icon) or systemd (Linux) keeps it running and restarts it when asked.
- **The radio link** (`mesh.py`) connects over USB serial or TCP through the official Meshtastic Python library,
  reconnects on its own, and logs everything as it arrives. Per-reception detail (every copy heard, with signal and
  hop counts) comes from parsing the firmware's debug log (`fwlog.py`), which only arrives over USB.
- **The database is a log, not a cache.** Values are stored as received and never corrected or pruned. Analysis
  happens at read time, in SQL over a read-only connection, scoped per listening station (`analytics._connect`).
- **The pages** are static files that fetch JSON from `/api/...` and update live from `/api/events`. Every endpoint
  is listed in `api_index.py` ([API.md](API.md)), which is also what the MCP server (`mcp_server.py`) is built on.

## Background work (threads in the same process)

| | |
|---|---|
| radio | connect, receive, a 10-minute watchdog, own GPS fixes and drive pings on a moving station |
| alerts.py | watched radios, new radios, the daily health check, scheduled traceroutes (off by default) |
| storage.py | the nightly backup (snapshot, integrity check, gzip, copy) and the size warning |
| sync.py | a station sending to its hub; a hub's senders to peer hubs (peers.py) |
| station.py | the station's own health (Raspberry Pi power and temperature) |

## More than one Lorakeet

A **hub** collects what its **stations** log: each station keeps its own database and sends new rows in batches
(`/api/ingest`), so a station without a connection just catches up later. Rows keep their station's id and their
row number there, which makes resending harmless. Stations join with a one-time pairing code (`pairing.py`).
Two people's hubs can also **peer**, each choosing what the other sees (locations rounded by default).

## Who can do what

This computer gets full access. Other devices on your network get nothing, a read-only view, or the login page,
depending on `[http] lan`. Logging in from another device allows sending and Lorakeet's day-to-day controls; the
pages that touch the radio's settings and keys, pairing, peering and `lorakeet.toml` (setup, Radio, Stations,
Settings beyond alerts) answer on this computer only. Anything that transmits on its own is off until you turn it
on. See [SECURITY.md](../SECURITY.md) for what's in scope.
