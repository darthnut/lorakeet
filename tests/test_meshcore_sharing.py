"""What a peer hub gets of a MeshCore station's rows: a frame is the whole packet (an advert's exact location, a
channel message's ciphertext, readable by anyone with the channel's key), so it goes only where the share allows."""
import json
import struct

import meshcore_packet
import store as store_mod
import sync
from meshcore_frames import KEY_A, SEATTLE, advert_payload, grp_txt_payload, packet

ADVERT = packet(meshcore_packet.ADVERT, advert_payload(KEY_A)).hex()
CHANNEL = packet(5, grp_txt_payload(body=b"secret-ciphertext!")).hex()   # 5 = GRP_TXT, a channel message
EXACT_LAT = struct.pack("<i", round(SEATTLE[0] * 1e6)).hex()   # the advert's latitude as it sits in the frame


def _rows():
    mc_rx = [{"ts": 1.7e9, "payload_name": "ADVERT", "frame": ADVERT, "from_id": "mc:a0000017c2be"},
             {"ts": 1.7e9, "payload_name": "GRP_TXT", "frame": CHANNEL, "from_id": None}]
    packets = [{"ts": 1.7e9, "portnum": "MC_ADVERT", "from_id": "mc:a0000017c2be", "pkt_id": 1,
                "summary": "advert: Hilltop Rpt (repeater)",
                "raw": json.dumps({"meshcore": {"payloadName": "ADVERT"}, "frame": ADVERT,
                                   "advert": {"name": "Hilltop Rpt", "lat": SEATTLE[0], "lon": SEATTLE[1]}})},
               {"ts": 1.7e9, "portnum": "MC_GRP_TXT", "from_id": None, "pkt_id": 2, "summary": "grp_txt (flood)",
                "raw": json.dumps({"meshcore": {"payloadName": "GRP_TXT"}, "frame": CHANNEL, "advert": None})}]
    return mc_rx, packets


def _share(st, level, exact):
    mc_rx, packets = _rows()
    return (sync.share_rows(st, "mc_rx", mc_rx, level, {}, exact=exact),
            sync.share_rows(st, "packets", packets, level, {}, exact=exact))


def test_frames_go_only_where_the_share_allows(tmp_path):
    st = store_mod.Store(tmp_path / "mesh.db")
    for level in ("default", "receptions"):
        mc, pk = _share(st, level, exact=False)
        out = json.dumps([mc, pk])
        assert EXACT_LAT not in out and CHANNEL not in out           # no exact place, no channel ciphertext
        assert [r["frame"] for r in mc] == [None, None]
        adv = json.loads(pk[0]["raw"])
        assert "frame" not in adv and adv["advert"]["name"] == "Hilltop Rpt" and adv["advert"]["lat"] != SEATTLE[0]
        assert "frame" not in json.loads(pk[1]["raw"])
        mc, pk = _share(st, level, exact=True)                        # exact places: an advert keeps its bytes
        assert mc[0]["frame"] == ADVERT and mc[1]["frame"] is None and CHANNEL not in json.dumps(pk)
    mc, pk = _share(st, "everything", exact=True)                     # everything, exact: the rows as logged
    assert [r["frame"] for r in mc] == [ADVERT, CHANNEL] and CHANNEL in pk[1]["raw"]
    st.close()
