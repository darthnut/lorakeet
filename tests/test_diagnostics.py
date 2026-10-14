"""The diagnostics bundle (diagnostics.py): useful for a bug report, with nothing personal in it."""
import io
import json
import re
import zipfile

import diagnostics
import store as store_mod

# Made-up personal details for the install below. Each is assembled from two pieces so that this file itself passes
# tools/release_check.py (which can't tell made-up from real); the bundle sees them whole.
RADIO, BASE = "!a1b2" + "c3d4", "!0bad" + "f00d"
LAN_IP, TS_IP, IP6 = "192.168." + "1.77", "100.101." + "102.103", "fd7a:115c:a1e0::1234"
WIN_HOME, HOME, DRIVE = "C:\\" + "Users\\alice", "/ho" + "me/alice", "H:\\" + "My Drive\\Backups"
LAT, LON = "47.61" + "234", "-122.33" + "456"
EMAIL, MAC = "alice@" + "example.net", "AA:BB:CC" + ":DD:EE:01"
TOKEN, CODE = "sync-token-0123456789abcdef0123456789", "lk1-eyJodWJzIjpbXX0="
SECRETS = ["Alice's Attic", "ALCE", "Bob Rooftop", "Hilltop Hub", RADIO, BASE, LAN_IP, TS_IP, IP6, WIN_HOME, HOME,
           DRIVE, LAT, LON, EMAIL, MAC, TOKEN, "CoffeeShopWiFi", "SecretChannel", CODE, "Mr Neighbour's hub"]


def _toml(s):
    return s.replace("\\", "\\\\")


def _install(tmp_path):
    """An install whose files are full of personal details."""
    data = tmp_path / "data"
    data.mkdir()
    cfg = tmp_path / "lorakeet.toml"
    cfg.write_text(f"""# my house, top floor
[storage]
data_dir = "{data.as_posix()}"
[http]
lan = "view"
hostnames = ["alice-pc", "alice-pc.tailnet.ts.net"]
[backup]
destinations = [
  "{_toml(DRIVE)}",
  "{_toml(WIN_HOME)}\\\\OneDrive",
]
[station]
name = "Hilltop Hub"
location = [{LAT}, {LON}]
note = "Bob Rooftop"
[sync]
mode = "collector"
hub_url = "http://{TS_IP}:5190"
token = "{TOKEN}"
[base]
id = "{BASE}"
""", encoding="utf-8")
    st = store_mod.Store(data / "mesh.db")
    st.claim_station(RADIO)
    st.insert("node_info", ts=1.0, node=RADIO, long_name="Alice's Attic", short_name="ALCE")
    st.insert("node_info", ts=1.0, node=BASE, long_name="Bob Rooftop", short_name="BOB")
    st.insert("events", ts=2.0, kind="network", detail=json.dumps({"network": "CoffeeShopWiFi", "prev": None}))
    st.insert("events", ts=3.0, kind="connect_failed", detail=json.dumps({"error": f"could not open {WIN_HOME}\\x"}))
    st.insert("events", ts=4.0, kind="connected", detail=json.dumps({
        "port": "COM7", "nodes": 12, "metadata": {"firmwareVersion": "2.7.26.abc", "hwModel": "HELTEC_V4", "role": "CLIENT"},
        "my_info": {"myNodeNum": 1, "pioEnv": "heltec-v4"},
        "config": {"lora": {"region": "US", "hopLimit": 3}, "network": {"wifiSsid": "CoffeeShopWiFi", "wifiEnabled": True},
                   "position": {"fixedPosition": True}},
        "channels": [{"settings": {"name": "SecretChannel"}}]}))
    st.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('radio_channels', ?)", json.dumps(
        {"channels": [{"name": "LongFast", "encrypted": True, "publicKey": True},
                      {"name": "SecretChannel", "encrypted": True, "publicKey": False}]}))
    (data / "peers.json").write_text(json.dumps([{"id": "p1", "name": "Mr Neighbour's hub"}]), encoding="utf-8")
    (data / "server.log").write_text("\n".join([
        "2026-10-01 10:00:00,000 INFO    meshdash: Lorakeet 0.3.1, dashboard on http://127.0.0.1:5190 (full)",
        f"2026-10-01 10:00:01,000 INFO    meshdash: connected: {RADIO} on COM7, 12 nodes",
        "2026-10-01 10:00:02,000 INFO    meshdash: radio channels: LongFast=0x08, SecretChannel=0x7c",
        "2026-10-01 10:00:03,000 INFO    meshdash: Bob Rooftop said hello",  # INFO chatter is left out
        "2026-10-01 11:00:00,000 WARNING lorakeet.sync: peer Mr Neighbour's hub: peer unreachable",
        f"2026-10-01 11:00:01,000 WARNING meshdash: ingest from {LAN_IP} refused for {BASE} (Hilltop Hub)",
        f"2026-10-01 11:00:02,000 WARNING meshdash.storage: backup to {DRIVE} failed: no such folder",
        "2026-10-01 11:00:03,000 ERROR   meshdash: analytics failed",
        "Traceback (most recent call last):",
        f'  File "{WIN_HOME}\\lorakeet\\analytics.py", line 10, in compute',
        f"ValueError: bad position {LAT}, {LON} from {EMAIL} via {MAC}",
        "2026-10-01 11:00:04,000 ERROR   meshtastic: Error parsing FromRadio bytes:b'\\x12Alice\\'s Attic\\x1a'",
        f"2026-10-01 11:00:05,000 WARNING meshdash: joined with {CODE} from {IP6} {HOME}",
    ]) + "\n", encoding="utf-8")
    return cfg, data


def test_nothing_personal_gets_through(tmp_path):
    cfg, data = _install(tmp_path)
    files = diagnostics.build(config_path=cfg, data_dir=data, live={"sync": {"lastError": f"hub http://{TS_IP}:5190 said no to "
                                                                              "Hilltop Hub"}})
    blob = "\n".join(files.values())
    leaks = [s for s in SECRETS if s.lower() in blob.lower()]
    assert leaks == [], f"leaked: {leaks}"
    assert "alice-pc" not in blob and "my house" not in blob


def test_but_it_keeps_what_a_bug_report_needs(tmp_path):
    cfg, data = _install(tmp_path)
    files = diagnostics.build(config_path=cfg, data_dir=data)
    s = json.loads(files["summary.json"])
    assert s["radio"]["hwModel"] == "HELTEC_V4" and s["radio"]["firmwareVersion"] == "2.7.26.abc"
    assert s["radio"]["config"]["lora"] == {"region": "US", "hopLimit": 3} and s["radio"]["link"] == "usb"
    assert s["radio"]["channels"] == {"count": 2, "publicKey": 1, "private": 1}
    assert s["config"]["sync"] == "collector" and s["config"]["backupFolders"] == 2 and s["config"]["hostnames"] == 2
    assert s["database"]["rows"]["node_info"] == 2 and s["system"]["python"]
    log = files["log.txt"]
    assert "analytics failed" in log and "Traceback" in log and "ValueError" in log      # errors with their tracebacks
    assert "said hello" not in log                                                       # not everyday chatter
    own = re.search(r"connected: (!node\d\d) on COM7, 12 nodes", log).group(1)        # ids replaced, consistently
    assert log.count(own) == 1 and "!node01" in log and "!node02" in log
    assert "LongFast=0x08" in log                                                        # public channels stay
    conf = files["config.txt"]
    assert 'lan = "view"' in conf and "destinations = [2 items]" in conf and "token = <set>" in conf
    assert "location = [2 items]" in conf and "mode = \"collector\"" in conf


def test_a_broken_config_still_gives_a_bundle(tmp_path):
    cfg, data = _install(tmp_path)
    cfg.write_text(cfg.read_text(encoding="utf-8").replace('lan = "view"', 'lan = "sometimes"'), encoding="utf-8")
    files = diagnostics.build(config_path=cfg, data_dir=data)
    s = json.loads(files["summary.json"])
    assert "http.lan" in s["config"]["error"] and s["radio"]["hwModel"] == "HELTEC_V4"


def test_the_zip(tmp_path):
    cfg, data = _install(tmp_path)
    z = zipfile.ZipFile(io.BytesIO(diagnostics.zip_bytes(diagnostics.build(config_path=cfg, data_dir=data))))
    names = z.namelist()
    assert "lorakeet-diagnostics/README.txt" in names and "lorakeet-diagnostics/summary.json" in names
    assert diagnostics.filename().startswith("lorakeet-diagnostics-") and diagnostics.filename().endswith(".zip")


def test_redactor_rules():
    r = diagnostics.Redactor(["Hilltop Hub", "SELF", "node"])
    assert r("self.store and SELF") == "self.store and <name>"            # names are case-sensitive
    assert r("alert:new-node") == "alert:new-node"                       # short lowercase words aren't names
    assert r("http://127.0.0.1:5190 and http://desk:5190/x") == "http://127.0.0.1:5190 and <url>"
    assert r("LongFast=0x08 lat=45.5 longitude: -122.6") == "LongFast=0x08 lat=<n> longitude: <n>"
    assert r(f"!ffffffff {RADIO.upper()} {RADIO}") == "!ffffffff !node01 !node01"
    assert r("at 12:34:56") == "at 12:34:56"                             # a time isn't an IPv6 address
