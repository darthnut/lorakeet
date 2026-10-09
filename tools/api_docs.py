"""Write docs/API.md from api_index.py (tests/test_api_index.py fails when it's out of date).

    python tools/api_docs.py           rewrite docs/API.md
    python tools/api_docs.py --check   exit 1 if it would change
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import api_index  # noqa: E402

OUT = ROOT / "docs" / "API.md"

HEAD = """# Lorakeet's API, and using it from an LLM

Everything the dashboard shows comes from a JSON API on the same port (http://127.0.0.1:5190 by default), so a
script or an LLM can use Lorakeet without reading the screen. `GET /api/index` returns the list below as JSON.
This file is generated from `api_index.py` by `python tools/api_docs.py`: edit that, not this.

## From an LLM app (MCP)

`mcp_server.py` is a [Model Context Protocol](https://modelcontextprotocol.io) server for Claude Desktop, Claude Code
and other MCP clients. It needs nothing beyond Lorakeet's own Python. To add it:

```sh
python mcp_server.py --print-config
```

prints the lines to paste into Claude Desktop's config (Settings > Developer > Edit Config) and the `claude mcp add`
command for Claude Code, with this computer's paths filled in. Lorakeet must be running.

It offers tools that answer in compact JSON (local times, long lists cut short): `status`, `list_nodes`,
`node_report`, `mesh_summary`, `search_packets`, `get_packet`, `messages`, `insights`, `network_links`, `airtime`,
`stations`, `alerts`, `debug_log`, and `list_api` / `api_get` for any other read-only endpoint. Radios can be named
by id (`!1234abcd`) or by name.

**What it may do is yours to decide, in `lorakeet.toml`:**

```toml
[mcp]
allow_changes = false   # watch_node, mark_alerts_read, update_alert_settings, set_logging, backup_now, restart_lorakeet
allow_transmit = false  # send_message, traceroute: these go out on the mesh under your radio's name
```

Reading is always allowed. A tool that isn't allowed isn't offered to the LLM at all, and the LLM has no tool to
change these switches. Radio settings, channels and keys, pairing and peering (tier `page` below) are never
offered: use the Radio and Stations pages. Your MCP client may also ask you before each tool call.

These switches guard against an LLM doing more than you meant; they are not a security boundary against programs
on this computer, which have full access to the dashboard anyway (as the browser does).

## Plain HTTP

- **Who may call what:** this computer has full access. Other devices on your network get read-only access with
  `[http] lan = "view"`, nothing until they log in with `lan = "login"` (`POST /api/login`, then the `lk_session`
  cookie), or are refused (`lan = "off"`, the default). Tier `page` endpoints are this computer only, even after a
  login. The `Host` header must be an IP address, `localhost` or a name in `[http] hostnames`.
- **POST** bodies are JSON with `Content-Type: application/json`; from another device the `Origin` header must
  match the address you opened the dashboard at (the cross-site request guard).
- **Times** are unix seconds. **Radio ids** look like `!1234abcd`. **Ranges**: `24h`, `7d`, `30d`, `90d`, `all`.
- **station**: analytics are about one listening station (default: this computer's radio); `*` combines every
  station, with packets heard by several counted once.
- Hours when Lorakeet wasn't logging come back as `null` / `covered: false`: no data, not zero traffic.

```sh
curl -s http://127.0.0.1:5190/api/brief
curl -s "http://127.0.0.1:5190/api/packets/search?portnum=TEXT_MESSAGE_APP&limit=5"
curl -s -X POST -H "Content-Type: application/json" -d '{"id": "!1234abcd", "watched": true}' http://127.0.0.1:5190/api/watch
```
"""

TITLES = {"read": "Reading (always allowed)", "change": "Changing Lorakeet (`[mcp] allow_changes`)",
          "transmit": "Transmitting on the mesh (`[mcp] allow_transmit`)",
          "page": "The Radio, Stations and setup pages (this computer only; never offered to an LLM)",
          "internal": "Internal"}


def _args(e):
    items = e.get("params") or e.get("body") or {}
    return "<br>".join(f"`{k}`" + (f": {v}" if v else "") for k, v in items.items())


def render():
    out = [HEAD]
    for tier in api_index.TIERS:
        out.append(f"\n## {TITLES[tier]}\n\n| | Endpoint | What it does | Parameters |\n|---|---|---|---|")
        for e in api_index.ENDPOINTS:
            if e["tier"] == tier:
                out.append(f"| {e['method']} | `{e['path']}` | {e['summary']} | {_args(e)} |")
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    text = render()
    if "--check" in sys.argv:
        current = OUT.read_text(encoding="utf-8").replace("\r\n", "\n") if OUT.exists() else ""
        sys.exit(0 if current == text else "docs/API.md is out of date: run python tools/api_docs.py")
    OUT.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {OUT}")
