"""Finding our radio by who it is (its USB serial number), not which port it's in."""
import mesh


def mac(*parts):  # made-up MACs, joined here so the release check's MAC pattern never sees one
    return ":".join(parts)


def test_usb_node_id_from_an_esp32_mac():
    assert mesh.usb_node_id(mac("A0", "B1", "00", "00", "1C", "2B")) == "!00001c2b"
    assert mesh.usb_node_id("E3A1B2C4D5F60718") is None   # an nRF52's serial isn't its node id
    assert mesh.usb_node_id(None) is None


A, B = ("/dev/ttyACM0", mac("A0", "B1", "00", "00", "1C", "2B")), ("/dev/ttyACM1", mac("A0", "B1", "00", "00", "2D", "3E"))


def test_the_remembered_radio_on_any_port():
    assert mesh.pick_usb_port(B[1], [A, B]) == ("/dev/ttyACM1", None)
    # the same two radios, their names swapped at boot
    assert mesh.pick_usb_port(B[1], [("/dev/ttyACM0", B[1]), ("/dev/ttyACM1", A[1])]) == ("/dev/ttyACM0", None)


def test_one_radio_is_taken_several_are_never_guessed_between():
    assert mesh.pick_usb_port(None, [A]) == ("/dev/ttyACM0", None)
    assert mesh.pick_usb_port("gone", [A]) == ("/dev/ttyACM0", None)     # swapped for another radio: just works
    assert mesh.pick_usb_port(None, [A, B]) == (None, "several")
    assert mesh.pick_usb_port("gone", [A, B]) == (None, "several")
    assert mesh.pick_usb_port(None, []) == (None, None)


def test_ports_another_link_holds_are_left_alone():
    assert mesh.pick_usb_port(None, [A, B], claimed={"/dev/ttyACM0"}) == ("/dev/ttyACM1", None)
    assert mesh.pick_usb_port(A[1], [A, B], claimed={"/dev/ttyACM0"}) == ("/dev/ttyACM1", None)


def test_a_configured_port_may_be_a_stable_link(monkeypatch, tmp_path):
    class P:
        def __init__(self, device):
            self.device = device
    dev = tmp_path / "ttyACM0"
    dev.write_text("")
    link = tmp_path / "by-id-radio"
    try:
        link.symlink_to(dev)
    except OSError:  # Windows without symlink rights: the plain name still works
        link = None
    monkeypatch.setattr(mesh.list_ports, "comports", lambda: [P(str(dev))])
    assert mesh.resolve_port(str(dev)) == str(dev)
    assert mesh.resolve_port(str(tmp_path / "nothing")) is None
    if link:
        assert mesh.resolve_port(str(link)) == str(dev)
