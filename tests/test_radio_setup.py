"""The Radio page's logic on a stand-in radio built from the real protobuf messages: the checklist, validated
writes in one transaction, private backups, channels (create, share, add from a link, never replace)."""
import os
import sys
import types

import pytest
from meshtastic.protobuf import channel_pb2, config_pb2, localonly_pb2

import radio_setup as rs

L = config_pb2.Config.LoRaConfig
ROLE = channel_pb2.Channel.Role


class FakeNode:
    def __init__(self, complete=True):
        self.localConfig = localonly_pb2.LocalConfig()
        self.moduleConfig = localonly_pb2.LocalModuleConfig()
        if complete:
            for s in rs.SECTIONS:
                getattr(self.localConfig, s).SetInParent()
            self.localConfig.lora.region = L.RegionCode.Value("US")
            self.localConfig.lora.use_preset = True
            self.localConfig.lora.hop_limit = 3
            self.localConfig.lora.tx_enabled = True
        self.channels = [channel_pb2.Channel(index=i, role=ROLE.DISABLED) for i in range(8)]
        self.channels[0].role = ROLE.PRIMARY
        self.channels[0].settings.psk = b"\x01"
        self.calls = []

    def beginSettingsTransaction(self):
        self.calls.append("begin")

    def commitSettingsTransaction(self):
        self.calls.append("commit")

    def writeConfig(self, name):
        self.calls.append(f"write {name}")

    def writeChannel(self, i):
        self.calls.append(f"channel {i}")

    def setOwner(self, long_name=None, short_name=None):
        self.calls.append(f"owner {long_name}/{short_name}")

    def getURL(self, includeAll=True):
        return "https://meshtastic.org/e/#CgMSAQE"


def item(items, id_):
    return next(i for i in items if i["id"] == id_)


def test_a_partly_loaded_configuration_is_never_written():
    node = FakeNode(complete=False)
    items = rs.checklist(node, "2.7.26")
    assert [i["id"] for i in items] == ["config"] and items[0]["status"] == "needed"
    with pytest.raises(rs.RadioError):
        rs.apply(types.SimpleNamespace(localNode=node), {"hop_limit": 4})
    assert node.calls == []


def test_region_is_never_guessed():
    node = FakeNode()
    node.localConfig.lora.region = L.RegionCode.Value("UNSET")
    reg = item(rs.checklist(node, "2.7.26"), "region")
    assert reg["status"] == "needed" and reg["recommended"] is None
    assert "UNSET" not in [o["value"] for o in reg["options"]]
    with pytest.raises(rs.RadioError):
        rs.plan(rs.checklist(node, "2.7.26"), {"region": "UNSET"})
    assert rs.plan(rs.checklist(node, "2.7.26"), {"region": "EU_868"}) == {"region": "EU_868"}


def test_recommendations_follow_the_situation():
    node = FakeNode()
    items = rs.checklist(node, "2.7.26", usb=True)
    assert item(items, "debug_log")["recommended"] is True                 # off over USB: turn it on
    assert item(rs.checklist(node, "2.7.26", usb=False), "debug_log")["status"] == "info"  # pointless over a network
    assert item(items, "role")["status"] == "ok"
    assert item(rs.checklist(node, "2.7.26", has_base=True), "role")["recommended"] == "CLIENT_MUTE"
    node.localConfig.lora.modem_preset = L.ModemPreset.Value("LONG_TURBO")
    assert item(rs.checklist(node, "2.7.26"), "preset")["recommended"] == "LONG_FAST"
    mobile = rs.checklist(node, "2.7.26", mobile=True)
    assert item(mobile, "gps_mode")["recommended"] == "ENABLED" and item(mobile, "gps_interval")["recommended"] == 30
    assert item(rs.checklist(node, "2.4.2"), "firmware")["status"] == "needed"
    assert item(rs.checklist(node, "2.8.1.abc"), "firmware")["status"] == "info"


def test_changes_are_validated_and_written_in_one_transaction():
    node = FakeNode()
    items = rs.checklist(node, "2.7.26")
    with pytest.raises(rs.RadioError):
        rs.plan(items, {"hop_limit": 9})                  # not one of the choices
    with pytest.raises(rs.RadioError):
        rs.plan(items, {"private_key": "x"})              # not a setting the page offers
    changes = rs.plan(items, {"hop_limit": 4, "debug_log": True, "role": "CLIENT_MUTE", "led_off": False})
    assert changes == {"hop_limit": 4, "debug_log": True, "role": "CLIENT_MUTE"}  # LED unchanged: not written
    sections = rs.apply(types.SimpleNamespace(localNode=node), changes, ("Desk logger", "DESK"))
    assert node.calls[0] == "begin" and node.calls[-1] == "commit"
    assert sorted(sections) == ["device", "lora", "security"] and "owner Desk logger/DESK" in node.calls
    assert node.localConfig.lora.hop_limit == 4 and node.localConfig.security.debug_log_api_enabled
    assert node.localConfig.device.role == config_pb2.Config.DeviceConfig.Role.Value("CLIENT_MUTE")
    with pytest.raises(rs.RadioError):
        rs.check_names("x", "TOOLONG")


def test_backups_are_restorable_yaml_readable_by_this_user_only(tmp_path):
    node = FakeNode()
    node.localConfig.security.private_key = b"\x07" * 32
    iface = types.SimpleNamespace(localNode=node, getLongName=lambda: "Desk", getShortName=lambda: "DSK",
                                  getMyNodeInfo=lambda: {})
    path = rs.backup(iface, tmp_path / "radio-backups", "!a0000001", "before-settings", "COM9")
    text = path.read_text()
    assert path.name.startswith("a0000001-") and "--configure" in text and "channel_url" in text
    assert "privateKey" in text or "private_key" in text          # a real backup keeps the key
    if sys.platform != "win32":
        assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    else:
        import subprocess
        acl = subprocess.run(["icacls", str(path)], capture_output=True, text=True).stdout
        assert os.environ["USERNAME"].lower() in acl.lower() and "Everyone" not in acl and "Users:" not in acl
    assert rs.backups(tmp_path / "radio-backups", "!a0000001")[0]["name"] == path.name
    with pytest.raises(FileExistsError):                           # never overwritten
        rs.write_private(path, "x")


def test_channels_are_added_but_never_replaced():
    node = FakeNode()
    mine = rs.new_channel("Logger")
    assert len(mine.psk) == 32
    url = rs.share_url(mine)
    back = rs.parse_url(url)[0]
    assert back.name == "Logger" and bytes(back.psk) == bytes(mine.psk)
    assert rs.add_channels(node, [mine]) == [("Logger", 1, "added")] and node.calls == ["channel 1"]
    assert rs.add_channels(node, rs.parse_url(url)) == [("Logger", 1, "already there")]
    other = rs.new_channel("Logger")                               # same name, different key
    with pytest.raises(rs.RadioError):
        rs.add_channels(node, [other])
    assert node.channels[0].role == ROLE.PRIMARY and bytes(node.channels[0].settings.psk) == b"\x01"
    for n in range(6):
        rs.add_channels(node, [rs.new_channel(f"c{n}")])
    with pytest.raises(rs.RadioError):
        rs.add_channels(node, [rs.new_channel("onemore")])       # all 8 slots used
    kinds = {c["name"]: c["key"] for c in rs.channel_list(node)}
    assert kinds["LongFast"] == "public" and kinds["Logger"] == "private"
    for bad in ("", "has space", "waytoolongname", "émoji"):
        with pytest.raises(rs.RadioError):
            rs.new_channel(bad)
    with pytest.raises(rs.RadioError):
        rs.parse_url("https://example.com/#abc")


def test_qr_code_and_connection_hints():
    assert rs.qr_svg("https://meshtastic.org/e/#x").startswith("<svg")
    assert "Another program" in rs.connect_hint("could not open port 'COM7': PermissionError(13, 'Access is denied.')", "win32")
    assert "dialout" in rs.connect_hint("[Errno 13] Permission denied: '/dev/ttyACM0'", "linux")
    assert "didn't answer" in rs.connect_hint("Timed out waiting for connection completion")
    assert rs.connect_hint("") is None
