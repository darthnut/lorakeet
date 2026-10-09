"""Lorakeet for LLMs: a Model Context Protocol server (stdio), so Claude Desktop, Claude Code or any MCP client can use
Lorakeet without reading the screen.

    python mcp_server.py --print-config     how to add it to Claude Desktop / Claude Code

It talks to the Lorakeet running on this computer over its HTTP API (docs/API.md) and answers in compact JSON (times
as local ISO dates, long lists cut short). Reading is always allowed. Changing Lorakeet ([mcp] allow_changes) and
transmitting on the mesh ([mcp] allow_transmit) are off until turned on in lorakeet.toml; their tools aren't even
listed until then. Radio settings, channel keys, pairing and peering are never offered (tier "page" in api_index.py).
No dependencies beyond the standard library.
"""
import argparse
import datetime
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import api_index

HERE = Path(__file__).resolve().parent
VERSION = (HERE / "VERSION").read_text(encoding="utf-8").strip()
PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
MAX_TEXT = 60_000   # characters per answer; more is cut with a note
MAX_LIST = 40       # list items per answer unless a tool asks for more
TIME_WORDS = {"ts", "time", "since", "until", "first", "last", "heard", "received", "built", "seen", "contact", "at"}
NOT_TIMES = {"num", "nodeNum", "from", "to", "id", "pkt_id", "pktId", "requestId", "replyId"}  # node / packet numbers
RANGES = ["24h", "7d", "30d", "90d", "all"]
INSTRUCTIONS = """Lorakeet logs a Meshtastic (LoRa mesh radio) network: every packet its radio hears, over-the-air receptions,
telemetry, positions and messages, from one or more listening stations. Radios are identified by ids like !1234abcd;
the tools also accept a radio's long or short name. Ranges are 24h, 7d, 30d, 90d or all. Times are local.
Start with `status`, then `mesh_summary` or `list_nodes`. Hours when Lorakeet wasn't logging are gaps, not zero
traffic. Anything not covered by a tool is reachable read-only through `api_get` (see `list_api`).
Some tools may be missing because the owner hasn't enabled them ([mcp] allow_changes / allow_transmit in
lorakeet.toml): say so instead of looking for a way around it."""


class ToolError(Exception):
    pass


class Lorakeet:
    def __init__(self, base, allow_changes=False, allow_transmit=False):
        self.base = base.rstrip("/")
        self.allow = {"read": True, "change": allow_changes, "transmit": allow_transmit}

    # ---- HTTP
    def call(self, method, path, params=None, body=None, raw=False):
        q = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        url = self.base + path + ("?" + urllib.parse.urlencode(q) if q else "")
        data = json.dumps(body).encode() if body is not None else (b"{}" if method == "POST" else None)
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json", "X-Lorakeet-Client": "mcp"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                text = r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")
            try:
                msg = json.loads(text).get("error") or text
            except ValueError:
                msg = text[:300]
            raise ToolError(f"Lorakeet answered {e.code}: {msg}") from None
        except (urllib.error.URLError, OSError) as e:
            raise ToolError(f"Lorakeet isn't answering at {self.base} ({getattr(e, 'reason', e)}). Is it running? "
                            "(the Lorakeet shortcut starts it)") from None
        if raw:
            return text
        try:
            return json.loads(text)
        except ValueError:
            raise ToolError(f"{path} didn't answer with JSON (CSV endpoints need api_get with a .csv path)") from None

    def get(self, path, **params):
        return self.call("GET", path, params)

    def post(self, path, body):
        return self.call("POST", path, body=body)

    # ---- radios by id or name
    def nodes(self):
        return self.get("/api/state")["nodes"]

    def resolve(self, node):
        node = str(node or "").strip()
        if re.fullmatch(r"!?[0-9a-fA-F]{8}", node):
            return "!" + node.lstrip("!").lower()
        if not node:
            raise ToolError("which radio? give its id (!1234abcd) or name")
        q = node.casefold()
        ns = self.nodes()
        seen = {n["id"] for n in ns}
        try:  # listening stations (this computer's radio is often missing from the live list)
            ns += [{"id": s["id"], "longName": s.get("name")} for s in self.get("/api/stations")["stations"]
                   if s["id"] not in seen]
        except ToolError:
            pass
        for pick in (lambda n: q in ((n.get("longName") or "").casefold(), (n.get("shortName") or "").casefold()),
                     lambda n: q in (n.get("longName") or "").casefold()):
            hits = [n for n in ns if pick(n)]
            if len(hits) == 1:
                return hits[0]["id"]
            if hits:
                raise ToolError(f"'{node}' matches {len(hits)} radios: " +
                                ", ".join(f"{n.get('longName')} ({n['id']})" for n in hits[:10]))
        raise ToolError(f"no radio called '{node}' (list_nodes shows the known ones)")


# ---- answers: local times, rounded numbers, short lists
def _iso(v):
    return datetime.datetime.fromtimestamp(v).astimezone().isoformat(timespec="seconds")


def tidy(o, max_list=MAX_LIST, key=None):
    if isinstance(o, dict):
        return {k: tidy(v, max_list, k) for k, v in o.items() if v is not None}
    if isinstance(o, list):
        out = [tidy(v, max_list) for v in o[:max_list]]
        if len(o) > max_list:
            out.append(f"... {len(o) - max_list} more (narrow the question or raise the limit)")
        return out
    if isinstance(o, (int, float)) and not isinstance(o, bool):
        if _is_time(key, o):
            return _iso(o)
        return round(o, 3) if isinstance(o, float) else o
    return o


def _is_time(key, v):
    """A unix time, judged by the key's name (ts, lastHeard, miningSince, firstBoth...) and a plausible value."""
    if not key or key in NOT_TIMES or not 1.5e9 < v < 4e9:
        return False
    words = {w.lower() for w in re.findall(r"[a-z]+|[A-Z][a-z]*", key)}  # lastOkTs -> last, ok, ts; uptimeS -> uptime, s
    return key == "t" or bool(words & TIME_WORDS)


def text(o):
    s = o if isinstance(o, str) else json.dumps(o, ensure_ascii=False, separators=(", ", ": "))
    if len(s) > MAX_TEXT:
        s = s[:MAX_TEXT] + f"\n... cut at {MAX_TEXT} characters: ask for less (a narrower range, a filter, a lower limit)"
    return s


# ---- tools
TOOLS = []


def tool(tier, description, **props):
    """props: name=(json type or schema dict, description, required?)."""
    def wrap(fn):
        schema = {"type": "object", "properties": {}, "required": []}
        for name, spec in props.items():
            typ, desc, *req = spec
            p = dict(typ) if isinstance(typ, dict) else {"type": typ}
            p["description"] = desc
            schema["properties"][name] = p
            if req and req[0]:
                schema["required"].append(name)
        TOOLS.append({"name": fn.__name__, "tier": tier, "description": description, "inputSchema": schema, "fn": fn})
        return fn
    return wrap


RANGE = ({"type": "string", "enum": RANGES}, "time window (default 7d)")
STATION = ("string", "a listening station's id or name, or * for all stations combined (default: this computer's radio)")


def _station(lk, station):
    return "*" if station == "*" else (lk.resolve(station) if station else None)


@tool("read", "What Lorakeet is doing: radio connection, listening stations, database and backups, sync mode, and "
              "which kinds of actions the owner has allowed for LLMs.")
def status(lk):
    brief, who, st = lk.get("/api/brief"), lk.get("/api/whoami"), lk.get("/api/stations")
    sto, sy = lk.get("/api/storage"), lk.get("/api/sync")
    return {"lorakeet": brief.get("version"), "demo": who.get("demo") or None,
            "radio": {k: brief.get(k) for k in ("connected", "paused", "port", "id", "name")},
            "stations": [{"id": s["id"], "name": s.get("name"), "thisComputer": s.get("current"),
                          "lastContact": s.get("lastContact"), "location": s.get("location")} for s in st["stations"]],
            "database": {"megabytes": round((sto.get("dbBytes") or 0) / 1e6, 1), "lastBackup": sto.get("lastOkTs")},
            "sync": sy.get("mode"),
            "llmMayAlso": {"change": lk.allow["change"], "transmit": lk.allow["transmit"],
                           "howToAllow": "the owner sets [mcp] allow_changes / allow_transmit = true in lorakeet.toml, "
                                         "then restarts the MCP client"}}


@tool("read", "Radios Lorakeet knows: name, hardware, role, last heard, hops away, signal, battery, position, key "
              "warnings, and reception at other stations. Newest first.",
      query=("string", "part of a name or id to look for"),
      heard_within_hours=("number", "only radios heard this recently"),
      with_position=("boolean", "only radios with a known position"),
      limit=("integer", "how many (default 50)"))
def list_nodes(lk, query="", heard_within_hours=None, with_position=False, limit=50):
    ns = lk.nodes()
    if query:
        q = query.casefold()
        ns = [n for n in ns if q in " ".join(str(n.get(k) or "") for k in ("id", "longName", "shortName")).casefold()]
    if heard_within_hours:
        cut = time.time() - float(heard_within_hours) * 3600
        ns = [n for n in ns if (n.get("lastHeard") or 0) >= cut]
    if with_position:
        ns = [n for n in ns if n.get("lat") is not None]
    ns.sort(key=lambda n: -(n.get("lastHeard") or 0))
    drop = {"num", "heardHere", "positionFromStation", "rxCount"}
    return {"count": len(ns), "nodes": tidy([{k: v for k, v in n.items() if k not in drop and v not in (False, "", [], {})}
                                             for n in ns], int(limit))}


@tool("read", "Everything about one radio over a time range: activity by type, daily rhythm, signal, battery, hops, "
              "who relays it, movement, links, latest telemetry, identity history, messages, traceroutes.",
      node=("string", "radio id or name", True), range=RANGE, station=STATION)
def node_report(lk, node, range="7d", station=None):
    nid = lk.resolve(node)
    return tidy(lk.get("/api/analytics/node", id=nid, range=range, station=_station(lk, station)), 30)


@tool("read", "The mesh over a time range: packets, radios, messages, channel use, hours logged, traffic by type, hop "
              "counts, top relays and talkers, new radios, conversations, readable vs private traffic.",
      range=RANGE, station=STATION)
def mesh_summary(lk, range="7d", station=None):
    a = lk.get("/api/analytics", range=range, station=_station(lk, station))
    gaps = [b["label"] for b in a.get("series", []) if not b.get("covered")]
    keep = {k: a.get(k) for k in ("range", "since", "until", "stationName", "combined", "kpis", "ports", "hops", "relays",
                                  "talkers", "newNodes", "privacy", "firmware28")}
    keep["notLogging"] = {"buckets": len(gaps), "bucket": a.get("bucket"), "list": gaps}
    keep["conversations"] = {"count": len(a.get("conversations") or []), "newest": (a.get("conversations") or [])[-10:]}
    return tidy(keep, 15)


@tool("read", "Search the log: packets (one row per packet a station decoded), rx_hops (every over-the-air reception, "
              "duplicates included) or tx_log (this radio's own transmissions). Newest first.",
      source=({"type": "string", "enum": ["packets", "rx_hops", "tx_log"]}, "default packets"),
      node=("string", "radio id or name at either end"), from_node=("string", "sender id or name"),
      to_node=("string", "recipient id or name"), portnum=("string", "e.g. TEXT_MESSAGE_APP, POSITION_APP, TELEMETRY_APP"),
      text=("string", "text in the packet summary"), since_hours=("number", "only the last N hours"),
      kind=({"type": "string", "enum": ["broadcast", "directed"]}, "broadcasts or addressed packets"),
      station=STATION, limit=("integer", "default 50, max 500"), offset=("integer", "for paging"))
def search_packets(lk, source="packets", node=None, from_node=None, to_node=None, portnum=None, text=None,
                   since_hours=None, kind=None, station=None, limit=50, offset=0):
    r = lk.get("/api/packets/search", source=source, node=node and lk.resolve(node),
               from_id=from_node and lk.resolve(from_node), to_id=to_node and lk.resolve(to_node), portnum=portnum,
               q=text, since=since_hours and time.time() - float(since_hours) * 3600, kind=kind,
               station=_station(lk, station), limit=min(int(limit), 500), offset=offset)
    r.pop("columns", None)  # every row names its fields
    return tidy(r, min(int(limit), 500))


@tool("read", "One packet: its full logged JSON, or with anatomy=true rebuilt layer by layer as bytes (radio "
              "settings, header, encryption, payload) with where each value came from.",
      rowid=("integer", "the packet's rowid (from search_packets)", True),
      anatomy=("boolean", "rebuild it as bytes"))
def get_packet(lk, rowid, anatomy=False):
    return tidy(lk.get(f"/api/packet/{int(rowid)}/anatomy" if anatomy else f"/api/packet/{int(rowid)}"), 60)


@tool("read", "The newest 200 text messages (channels and direct messages), newest first, each once across "
              "stations. For older ones use search_packets with portnum TEXT_MESSAGE_APP and text.",
      query=("string", "words to look for"), node=("string", "only to or from this radio (id or name)"),
      channel=("integer", "only this channel index"), limit=("integer", "default 50"))
def messages(lk, query=None, node=None, channel=None, limit=50):
    ms = lk.get("/api/messages")
    nid = node and lk.resolve(node)
    if query:
        ms = [m for m in ms if query.casefold() in (m.get("text") or "").casefold()]
    if nid:
        ms = [m for m in ms if nid in (m.get("from_id"), m.get("to_id"))]
    if channel is not None:
        ms = [m for m in ms if m.get("channel") == int(channel)]
    ms.sort(key=lambda m: -(m.get("ts") or 0))
    ms = [{k: v for k, v in m.items() if not k.startswith("_")} for m in ms]
    return {"count": len(ms), "messages": tidy(ms, int(limit))}


@tool("read", "Findings Lorakeet works out from the log. health: busy channel, chatty radios, routers that never "
              "relay, weak or shared keys, impersonation. estimates: likely positions of radios that never share one. "
              "coverage: where position packets were heard from. traceroutes: route results.",
      kind=({"type": "string", "enum": ["health", "estimates", "coverage", "traceroutes"]}, "which findings", True),
      range=RANGE, station=STATION)
def insights(lk, kind, range="7d", station=None):
    return tidy(lk.get(f"/api/insights/{kind}", range=range, station=_station(lk, station)))


@tool("read", "Physical radio links (who hears whom directly): measured from direct receptions, traceroutes and "
              "neighbor info, or inferred from relay bytes.", range=RANGE, station=STATION)
def network_links(lk, range="7d", station=None):
    return tidy(lk.get("/api/analytics/topology", range=range, station=_station(lk, station)), 80)


@tool("read", "Channel airtime: what was heard (calculated) vs measured channel utilization, by type and by sender.",
      range=RANGE, station=STATION)
def airtime(lk, range="7d", station=None):
    return tidy(lk.get("/api/analytics/airtime", range=range, station=_station(lk, station)), 25)


@tool("read", "Listening stations: each one's name, location, last contact and latest report (version, health), and "
              "with compare=true which station hears which radio best.",
      compare=("boolean", "include the radio x station capture matrix"), range=RANGE)
def stations(lk, compare=False, range="7d"):
    out = {"stations": lk.get("/api/stations")["stations"]}
    if compare:
        out["capture"] = lk.get("/api/analytics/stations", range=range)
    return tidy(out)


@tool("read", "Recent alerts: watched radios gone silent or back, low battery, new radios, new health warnings.",
      limit=("integer", "default 30"), unread_only=("boolean", "only unread"))
def alerts(lk, limit=30, unread_only=False):
    a = lk.get("/api/alerts", limit=max(int(limit), 1) * (4 if unread_only else 1))
    items = a.get("alerts", a) if isinstance(a, dict) else a
    if unread_only and isinstance(items, list):
        items = [x for x in items if not x.get("read")]
    return tidy({"unread": a.get("unread") if isinstance(a, dict) else None, "alerts": items,
                 "settings": lk.get("/api/settings")}, int(limit))


@tool("read", "The radio's firmware debug log (kept 7 days).",
      query=("string", "text to look for"), minutes=("number", "only the last N minutes"),
      levels=("string", "comma list, e.g. ERROR,WARN"), limit=("integer", "default 100"))
def debug_log(lk, query=None, minutes=None, levels=None, limit=100):
    return tidy(lk.get("/api/debuglog", q=query, since=minutes and time.time() - float(minutes) * 60,
                       levels=levels, limit=min(int(limit), 2000)), int(limit))


@tool("read", "Every HTTP endpoint Lorakeet has, with parameters, and which of them you may use here.")
def list_api(lk):
    return {"endpoints": [{k: e[k] for k in ("method", "path", "tier", "summary", "params", "body") if k in e}
                          for e in api_index.ENDPOINTS if e["tier"] in ("read", "change", "transmit")],
            "youMayUse": [t for t, ok in lk.allow.items() if ok], "notes": api_index.index(VERSION)["notes"]}


@tool("read", "Call any read-only Lorakeet endpoint (GET, tier read in list_api) that the other tools don't cover.",
      path=("string", "e.g. /api/analytics/compare", True),
      params=({"type": "object", "additionalProperties": {"type": ["string", "number", "boolean"]}}, "query parameters"))
def api_get(lk, path, params=None):
    e = api_index.find("GET", urllib.parse.urlparse(path).path)
    if not e or e["tier"] != "read":
        raise ToolError(f"{path} isn't a read-only endpoint (list_api shows them)")
    if urllib.parse.urlparse(path).path.endswith(".csv"):
        return lk.call("GET", path, params, raw=True)
    return tidy(lk.call("GET", path, params))


# -- changing Lorakeet ([mcp] allow_changes)
@tool("change", "Watch a radio (alerts when it goes silent, comes back or runs low on battery), or stop watching it.",
      node=("string", "radio id or name", True), watched=("boolean", "true to watch (default), false to stop"))
def watch_node(lk, node, watched=True):
    return lk.post("/api/watch", {"id": lk.resolve(node), "watched": bool(watched)})


@tool("change", "Mark every alert read.")
def mark_alerts_read(lk):
    return lk.post("/api/alerts/read", {})


@tool("change", "Change alert settings. Read the current ones with alerts() first; send only the keys to change.",
      settings=({"type": "object"}, "e.g. {\"silenceHours\": 6}", True))
def update_alert_settings(lk, settings):
    if not isinstance(settings, dict):
        raise ToolError("settings must be an object, e.g. {\"silenceHours\": 6}")
    transmits = sorted(k for k in settings if k.startswith("autoTraceroute"))
    switching_off = transmits == ["autoTraceroute"] and settings["autoTraceroute"] is False
    if transmits and not switching_off and not lk.allow["transmit"]:
        raise ToolError(f"{', '.join(transmits)}: scheduled traceroutes transmit, and the owner hasn't allowed "
                        "transmitting ([mcp] allow_transmit in lorakeet.toml)")
    return lk.post("/api/settings", settings)


@tool("change", "Pause logging (Lorakeet lets go of the radio, so another program can use it; the time is a gap in "
                "the log) or resume it.", paused=("boolean", "true to pause, false to resume", True))
def set_logging(lk, paused):
    return lk.post("/api/logging", {"paused": bool(paused)})


@tool("change", "Back up the database now.")
def backup_now(lk):
    return lk.post("/api/backup", {})


@tool("change", "Restart Lorakeet (a few seconds; only when it runs under its background runner).")
def restart_lorakeet(lk):
    return lk.post("/api/restart", {})


# -- transmitting ([mcp] allow_transmit)
@tool("transmit", "Send a text message from this radio: on a channel (everyone with that channel can read it), or "
                  "directly to one radio. It goes out over the air under the owner's radio name.",
      text=("string", "the message (keep it short: about 200 bytes at most)", True),
      to=("string", "a radio id or name for a direct message; leave out for a channel message"),
      channel=("integer", "channel index for a channel message (default 0, the primary)"))
def send_message(lk, text, to=None, channel=0):
    return lk.post("/api/send", {"text": text, "to": to and lk.resolve(to), "channel": int(channel or 0)})


@tool("transmit", "Run a traceroute to a radio (transmits; one every 30 s at most). The result appears in "
                  "node_report / insights traceroutes when the reply arrives.",
      node=("string", "radio id or name", True))
def traceroute(lk, node):
    return lk.post("/api/traceroute", {"to": lk.resolve(node)})


# ---- MCP over stdio (JSON-RPC 2.0, one message per line)
def tools_for(lk):
    return [t for t in TOOLS if lk.allow[t["tier"]]]


def handle(lk, msg):
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}

    def ok(result):
        return {"jsonrpc": "2.0", "id": mid, "result": result}
    if mid is None:  # a notification (initialized, cancelled...): nothing to answer
        return None
    if method == "initialize":
        want = params.get("protocolVersion")
        return ok({"protocolVersion": want if want in PROTOCOLS else PROTOCOLS[0],
                   "capabilities": {"tools": {"listChanged": False}},
                   "serverInfo": {"name": "lorakeet", "version": VERSION}, "instructions": INSTRUCTIONS})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": [{k: t[k] for k in ("name", "description", "inputSchema")} for t in tools_for(lk)]})
    if method == "tools/call":
        t = next((t for t in tools_for(lk) if t["name"] == params.get("name")), None)
        if t is None:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"unknown tool {params.get('name')}"}}
        try:
            out, err = t["fn"](lk, **(params.get("arguments") or {})), False
        except ToolError as e:
            out, err = str(e), True
        except (TypeError, ValueError) as e:
            out, err = f"bad arguments: {e}", True
        except Exception as e:  # noqa: BLE001 - still an answer to THIS call
            out, err = f"{type(e).__name__}: {e}", True
        return ok({"content": [{"type": "text", "text": text(out)}], "isError": err})
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}


def serve(lk, stdin=None, stdout=None):
    stdin, stdout = stdin or sys.stdin.buffer, stdout or sys.stdout.buffer
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            msg, reply = None, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        if msg is not None:
            try:
                reply = handle(lk, msg) if isinstance(msg, dict) else \
                    {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "expected one JSON-RPC message"}}
            except Exception as e:  # noqa: BLE001 - one bad request must not end the session
                reply = {"jsonrpc": "2.0", "id": msg.get("id") if isinstance(msg, dict) else None,
                         "error": {"code": -32603, "message": f"internal error: {e}"}}
        if reply is not None:
            stdout.write((json.dumps(reply, ensure_ascii=False) + "\n").encode("utf-8"))
            stdout.flush()


def print_config():
    py = Path(sys.executable)
    if py.name.lower() == "pythonw.exe":
        py = py.with_name("python.exe")
    me = HERE / "mcp_server.py"
    print("Claude Desktop: Settings > Developer > Edit Config, and add this inside \"mcpServers\":\n")
    print(json.dumps({"lorakeet": {"command": str(py), "args": [str(me)]}}, indent=2))
    print("\nClaude Code:\n")
    print(f'  claude mcp add lorakeet -- "{py}" "{me}"')
    print("\nLorakeet must be running. Reading is always allowed; to let the LLM change settings or transmit, set\n"
          "[mcp] allow_changes / allow_transmit = true in lorakeet.toml (see lorakeet.example.toml).")


def main():
    ap = argparse.ArgumentParser(description="Lorakeet's MCP server (stdio), for LLM apps")
    ap.add_argument("--url", help="the Lorakeet to talk to (default http://127.0.0.1:<http.port from lorakeet.toml>)")
    ap.add_argument("--print-config", action="store_true", help="show how to add this to Claude Desktop / Claude Code")
    args = ap.parse_args()
    if args.print_config:
        return print_config()
    try:
        from config import CFG
        port, mcp = CFG["http"]["port"], CFG["mcp"]
    except Exception as e:  # noqa: BLE001 - a broken lorakeet.toml: read-only on the default port
        print(f"lorakeet.toml: {e}; reading only, port 5190", file=sys.stderr)
        port, mcp = 5190, {"allow_changes": False, "allow_transmit": False}
    serve(Lorakeet(args.url or f"http://127.0.0.1:{port}", mcp["allow_changes"], mcp["allow_transmit"]))


if __name__ == "__main__":
    main()
