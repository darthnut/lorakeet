"""Hub-to-hub peering: one sync.PeerSender per peer in peers.json.
"""
import json
import logging

from channels import public_channel_hashes
import pairing
import runtime
from station import station_report
import sync as sync_mod


log = logging.getLogger("meshdash")


class PeerManager:
    """The hubs this one sends to (Stations page -> Peers): one sync.PeerSender each, started at launch and as peers
    are added, stopped as they're removed."""

    def __init__(self, store, mesh, hub_id):
        self.store, self.mesh, self.hub_id = store, mesh, hub_id
        self.peers = pairing.Peers(runtime.DATA_DIR / "peers.json")
        self.senders = {}

    def _report(self, station):
        if station == self.mesh.local_id:
            return station_report(self.mesh)
        r = self.store.query("SELECT value FROM settings WHERE key='station_meta'")
        m = (json.loads(r[0]["value"]) if r else {}).get(station)
        return {k: v for k, v in m.items() if k != "received"} if m else None

    def _start(self, peer):
        snd = sync_mod.PeerSender(self.store, peer, self.hub_id,
                                  lambda: public_channel_hashes(self.store, {self.mesh.local_id}), self._report)
        self.senders[peer["id"]] = snd
        snd.start()

    def start_all(self):
        for peer in self.peers.all():
            self._start(peer)

    def add(self, name, url, token, share, forward, hub_id, exact=False):
        peer = self.peers.add(name, url, token, share, forward, hub_id, exact)
        self._start(peer)
        return peer

    def update(self, pid, **kw):
        peer = self.peers.update(pid, **kw)
        if peer and pid in self.senders:
            self.senders[pid].peer.update({k: peer.get(k) for k in ("share", "forward", "name", "url", "exact", "paused")})
            self.senders[pid].sync_now()
        return peer

    def remove(self, pid):
        snd = self.senders.pop(pid, None)
        if snd:
            snd.stop()
        self.peers.remove(pid)

    def list(self):
        out = []
        for p in self.peers.list():
            snd = self.senders.get(p["id"])
            out.append({**p, "status": dict(snd.status) if snd else None})
        return out
