"""radioid.py: the one place that knows what a radio id is (docs/MESHCORE.md, decision 2)."""
import pytest

import config
import radioid


def test_meshtastic_ids():
    assert radioid.is_radio_id("!a0000001")
    assert not radioid.is_radio_id("!ffffffff")      # the broadcast address isn't a radio
    for bad in ("a0000001", "!A0000001", "!a000001", "!a00000011", "", None, 12, "!a0000001\n"):
        assert not radioid.is_radio_id(bad), bad
    assert radioid.node_num("!a0000001") == 0xa0000001 and radioid.relay_byte("!a00000c3") == 0xc3
    assert radioid.protocol_of("!a0000001") == "meshtastic"


def test_meshcore_ids():
    mc = radioid.from_meshcore_key("a0000017c2be" + "00" * 26)
    assert mc == "mc:a0000017c2be" and radioid.from_meshcore_key(bytes.fromhex("a0000017c2be")) == mc
    assert radioid.is_meshcore(mc) and radioid.protocol_of(mc) == "meshcore"
    assert radioid.relay_byte(mc) is None and radioid.node_num(mc) is None   # no relay byte: MeshCore paths are key prefixes (MESHCORE.md, decision 3)
    assert radioid.is_radio_id(mc) and radioid.is_radio_id("!a0000001")   # both accepted since the MeshCore link
    assert not radioid.is_radio_id("mc:a0000017c2b") and not radioid.is_radio_id("MC:a0000017c2be")
    with pytest.raises(ValueError):
        radioid.from_meshcore_key("not hex")


def test_station_tokens_split_at_the_last_colon():
    assert radioid.split_station_token("!a0000001:" + "t" * 20) == ("!a0000001", "t" * 20)
    assert radioid.split_station_token("mc:a0000017c2be:" + "t" * 20) == ("mc:a0000017c2be", "t" * 20)


def test_settings_naming_radios_go_through_radioid(tmp_path, monkeypatch):
    p = tmp_path / "lorakeet.toml"
    monkeypatch.setenv("LORAKEET_CONFIG", str(p))
    p.write_text('[base]\nid = "!a0000001"\n[remote]\nallow = ["!a0000002"]\n', encoding="utf-8")
    config.file_settings()
    for bad in ('[base]\nid = "a0000001"\n', '[remote]\nallow = ["!ffffffff"]\n', '[base]\nid = "mc:a0000017c2be"\n'):
        p.write_text(bad, encoding="utf-8")
        with pytest.raises(ValueError):
            config.file_settings()
