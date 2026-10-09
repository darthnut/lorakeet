import os
import tomllib

import pytest

import config


def validated(user):
    config._validate(config._merge(config.DEFAULTS, user))


def test_defaults_are_valid():
    validated({})


@pytest.mark.parametrize("user, message", [
    ({"radio": {"port": "COM7", "host": "192.168.1.50"}}, "not both"),
    ({"radio": {"tcp_port": 70000}}, "tcp_port"),
    ({"station": {"location": [200, 0]}}, "station.location"),
    ({"station": {"mobile": True, "location": [47.6, -122.3]}}, "mobile"),
    ({"map": {"tiles": "google"}}, "map.tiles"),
    ({"base": {"id": "BASE"}}, "base.id"),
])
def test_bad_settings_are_refused_with_a_clear_message(user, message):
    with pytest.raises(ValueError, match=message):
        validated(user)


@pytest.fixture
def setup_path(tmp_path, monkeypatch):
    path = tmp_path / "lorakeet.toml"
    monkeypatch.setenv("LORAKEET_CONFIG", str(path))
    return path


def test_setup_writes_a_valid_commented_file(setup_path):
    config.write_setup({"stationName": "Test", "location": [47.6062, -122.3321], "lan": "view",
                        "storeRecipients": False, "tiles": "esri", "host": "192.168.1.50", "tcpPort": 4403})
    text = setup_path.read_text(encoding="utf-8")
    assert text.startswith("# Lorakeet settings")
    c = tomllib.loads(text)
    assert c["radio"] == {"host": "192.168.1.50", "tcp_port": 4403}
    assert c["http"]["lan"] == "view" and c["logging"]["store_recipients"] is False
    assert c["station"] == {"name": "Test", "location": [47.6062, -122.3321]}
    assert c["alerts"]["auto_traceroute"] is False  # transmitting stays off unless chosen
    validated(c)


def test_setup_never_overwrites(setup_path):
    setup_path.write_text("# hand-edited\n")
    with pytest.raises(FileExistsError):
        config.write_setup({})
    assert setup_path.read_text() == "# hand-edited\n"


def test_setup_refuses_bad_answers_and_writes_nothing(setup_path):
    with pytest.raises(ValueError):
        config.write_setup({"location": [95, 0]})
    assert not setup_path.exists()


def test_windows_paths_survive_the_round_trip(setup_path):
    config.write_setup({"dataDir": r"C:\Lorakeet data\mesh"})
    assert tomllib.loads(setup_path.read_text(encoding="utf-8"))["storage"]["data_dir"] == r"C:\Lorakeet data\mesh"


def test_tests_never_see_a_real_config():
    assert not os.path.exists(os.environ["LORAKEET_CONFIG"]) or "lktests-" in os.environ["LORAKEET_CONFIG"] \
        or "pytest" in os.environ["LORAKEET_CONFIG"]


def test_a_byte_order_mark_from_windows_tools_is_fine(tmp_path, monkeypatch):
    import config
    p = tmp_path / "lorakeet.toml"
    p.write_bytes(b"\xef\xbb\xbf[http]\nport = 5199\n")   # what PowerShell 5's Set-Content -Encoding utf8 writes
    monkeypatch.setenv("LORAKEET_CONFIG", str(p))
    assert config.load()["http"]["port"] == 5199


def test_the_example_documents_every_option_at_its_default():
    import tomllib
    from pathlib import Path
    import config
    d = tomllib.loads((Path(__file__).resolve().parent.parent / "lorakeet.example.toml").read_text(encoding="utf-8"))
    config._validate(config._merge(config.DEFAULTS, d))
    missing = [f"{s}.{k}" for s, sec in config.DEFAULTS.items() if isinstance(sec, dict) for k in sec if k not in d.get(s, {})]
    differ = [f"{s}.{k}" for s in d for k in d[s] if d[s][k] != config.DEFAULTS[s][k]]
    assert missing == [] and differ == []
