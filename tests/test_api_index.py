"""The endpoint index (api_index.py, GET /api/index, docs/API.md) and the MCP server built on it (mcp_server.py)."""
import io
import json
import re
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import pytest

import api_index
import mcp_server

ROOT = Path(__file__).resolve().parent.parent


def _server_paths():
    return set(re.findall(r'"(/api/[A-Za-z0-9/_.-]*)"', (ROOT / "server.py").read_text(encoding="utf-8")))


def test_every_endpoint_the_server_handles_is_in_the_index():
    listed = {e["path"] for e in api_index.ENDPOINTS}
    prefixes = {p.split("{")[0] for p in listed}
    missing = [p for p in _server_paths()
               if p not in listed and not any(q.startswith(p) for q in prefixes) and not p.endswith("/")]
    assert missing == [], f"add these to api_index.py: {missing}"


def test_every_indexed_endpoint_exists():
    src = (ROOT / "server.py").read_text(encoding="utf-8")
    gone = [e["path"] for e in api_index.ENDPOINTS if f'"{e["path"].split("{")[0]}' not in src]
    assert gone == []
    assert all(e["tier"] in api_index.TIERS and e["method"] in ("GET", "POST") for e in api_index.ENDPOINTS)


def test_find_matches_placeholders():
    assert api_index.find("GET", "/api/node/!a0000001")["path"] == "/api/node/{id}"
    assert api_index.find("GET", "/api/packet/12/anatomy")["path"] == "/api/packet/{rowid}/anatomy"
    assert api_index.find("POST", "/api/send")["tier"] == "transmit"
    assert api_index.find("GET", "/api/nope") is None


def test_the_api_doc_is_current():
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "api_docs.py"), "--check"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


# ---- the MCP server against a stand-in Lorakeet
NODES = [{"id": "!a0000001", "longName": "Hill Router", "shortName": "HILL", "lastHeard": 1.7e9 + 50, "lat": 1.0},
         {"id": "!a0000002", "longName": "Garden", "shortName": "GRDN", "lastHeard": 1.7e9 + 10},
         {"id": "!a0000003", "longName": "Garden Shed", "shortName": "SHED", "lastHeard": 1.7e9}]


@pytest.fixture
def fake():
    posts = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, obj, code=200):
            b = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):  # noqa: N802
            p = urlparse(self.path).path
            data = {"/api/state": {"nodes": NODES}, "/api/version": {"version": "x"},
                    "/api/messages": [{"ts": 1.7e9, "from_id": "!a0000002", "to_id": "^all", "text": "hello", "channel": 0}]}
            self._send(data[p]) if p in data else self._send({"error": "not here"}, 404)

        def do_POST(self):  # noqa: N802
            posts.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")))
            self._send({"ok": True})

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", posts
    srv.shutdown()


def _session(lk, *msgs):
    out = io.BytesIO()
    mcp_server.serve(lk, io.BytesIO(b"".join(json.dumps(m).encode() + b"\n" for m in msgs)), out)
    return [json.loads(x) for x in out.getvalue().splitlines()]


def _call(lk, name, **args):
    r = _session(lk, {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}})[0]
    return r["result"]


def test_handshake_and_tool_list_follow_the_owners_switches(fake):
    url, _ = fake
    r = _session(mcp_server.Lorakeet(url),
                 {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                 {"jsonrpc": "2.0", "method": "notifications/initialized"},
                 {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                 {"jsonrpc": "2.0", "id": 3, "method": "nope"})
    assert len(r) == 3  # the notification gets no answer
    assert r[0]["result"]["protocolVersion"] == "2025-06-18" and r[0]["result"]["serverInfo"]["name"] == "lorakeet"
    names = {t["name"] for t in r[1]["result"]["tools"]}
    assert {"status", "list_nodes", "search_packets", "api_get"} <= names
    assert not names & {"send_message", "traceroute", "watch_node", "set_logging"}
    assert r[2]["error"]["code"] == -32601
    every = {t["name"] for t in _session(mcp_server.Lorakeet(url, True, True),
                                         {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})[0]["result"]["tools"]}
    assert {"send_message", "traceroute", "watch_node"} <= every


def test_a_disallowed_tool_cant_be_called_anyway(fake):
    url, posts = fake
    r = _session(mcp_server.Lorakeet(url), {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                           "params": {"name": "send_message", "arguments": {"text": "hi"}}})[0]
    assert "error" in r and posts == []


def test_radios_by_name_and_answers_in_local_time(fake):
    url, posts = fake
    lk = mcp_server.Lorakeet(url, allow_changes=True)
    out = _call(lk, "list_nodes", query="garden")
    body = json.loads(out["content"][0]["text"])
    assert body["count"] == 2 and body["nodes"][0]["id"] == "!a0000002"
    assert re.match(r"\d{4}-\d\d-\d\dT", body["nodes"][0]["lastHeard"])
    _call(lk, "watch_node", node="hill")                       # a unique part of a name
    assert posts[-1] == ("/api/watch", {"id": "!a0000001", "watched": True})
    out = _call(lk, "watch_node", node="Garden")               # exact name beats the longer one
    assert posts[-1][1]["id"] == "!a0000002" and not out["isError"]
    out = _call(lk, "watch_node", node="gar")                  # ambiguous: says which
    assert out["isError"] and "Garden Shed" in out["content"][0]["text"]


def test_api_get_is_read_only(fake):
    url, _ = fake
    lk = mcp_server.Lorakeet(url, True, True)
    assert _call(lk, "api_get", path="/api/send")["isError"]
    assert _call(lk, "api_get", path="/api/hub")["isError"]     # a page endpoint
    assert not _call(lk, "api_get", path="/api/version")["isError"]


def test_lorakeet_not_running_is_a_plain_answer():
    out = _call(mcp_server.Lorakeet("http://127.0.0.1:9"), "status")
    assert out["isError"] and "isn't answering" in out["content"][0]["text"]


def test_long_lists_are_cut_with_a_note():
    t = mcp_server.tidy({"rows": list(range(100))}, 10)
    assert len(t["rows"]) == 11 and "90 more" in t["rows"][-1]


def test_bad_arguments_answer_the_call_instead_of_hanging(fake):
    url, _ = fake
    lk = mcp_server.Lorakeet(url)
    r = _session(lk, {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                      "params": {"name": "list_nodes", "arguments": {"limit": "abc"}}})[0]
    assert r["id"] == 7 and r["result"]["isError"]
    out = io.BytesIO()
    mcp_server.serve(lk, io.BytesIO(b"not json\n"), out)
    assert json.loads(out.getvalue())["error"]["code"] == -32700


def test_changes_alone_cant_switch_on_scheduled_traceroutes(fake):
    url, posts = fake
    out = _call(mcp_server.Lorakeet(url, allow_changes=True), "update_alert_settings",
                settings={"autoTraceroute": True, "autoTracerouteMin": 1})
    assert out["isError"] and "allow_transmit" in out["content"][0]["text"] and posts == []
    out = _call(mcp_server.Lorakeet(url, allow_changes=True, allow_transmit=True), "update_alert_settings",
                settings={"autoTraceroute": True})
    assert not out["isError"] and posts[-1][0] == "/api/settings"
    out = _call(mcp_server.Lorakeet(url, allow_changes=True), "update_alert_settings", settings={"autoTraceroute": False})
    assert not out["isError"]                    # switching them off needs no permission to transmit
