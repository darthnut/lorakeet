"""What every radio link does, whatever the protocol (docs/MESHCORE.md, decision 4): keep reconnecting forever, a
watchdog for a radio that goes silent, pausing and resuming (letting go of the radio's port), the events log, and
the live pages' event stream.

A protocol's link subclasses RadioLink and supplies the hooks: connect() (try once; set connected, port and local_id
on success), drop_link() (forget the current connection), tick() (every pass of the loop) and status(). mesh.Mesh is
the Meshtastic link.
"""
import json
import logging
import queue
import threading
import time

from store import dumps

log = logging.getLogger("meshdash")


class RadioLink:
    PROTOCOL = None
    WATCHDOG_S = 10 * 60   # the radio reports something at least every minute or so; silence this long means it's wedged
    LOOP_S = 5

    def __init__(self, store):
        self.store = store
        self.connected = False
        self.connected_at = None
        self.paused = False  # logging paused (tray / dashboard): the radio's port is let go until resumed
        self.port = None
        self.local_id = None
        self.last_rx = 0     # anything from the radio, including its own telemetry (the watchdog)
        self.last_connect_error = None
        self.lock = threading.RLock()
        self.subscribers = set()
        self.sub_lock = threading.Lock()

    # -- the hooks a protocol's link supplies
    def connect(self):
        raise NotImplementedError

    def drop_link(self):
        raise NotImplementedError

    def tick(self):
        """Every pass of the loop (connected or not): the protocol's periodic work."""

    def status(self):
        return {"connected": self.connected, "paused": self.paused, "port": self.port, "localId": self.local_id}

    # -- connection management (reconnects forever)
    def run(self):
        while True:
            try:
                if self.connected and not self.paused and time.time() - self.last_rx > self.WATCHDOG_S:
                    log.warning("no data from radio for %d s; reconnecting", self.WATCHDOG_S)
                    self.event("watchdog", silent_s=self.WATCHDOG_S)
                    self.drop_link()
                if not self.connected and not self.paused:
                    self.connect()
                self.tick()
            except Exception:  # noqa: BLE001 - this loop must never die
                log.exception("connection loop error")
            time.sleep(self.LOOP_S)

    def set_paused(self, paused):
        """Pause logging: let go of the radio (its USB port is free for the radio's own app, a CLI or a flasher)
        until resumed. The paused time is a gap in the log, like any time Lorakeet wasn't running."""
        paused = bool(paused)
        if paused == self.paused:
            return
        self.paused = paused
        log.info("logging %s", "paused: letting go of the radio" if paused else "resumed")
        self.event("logging_paused" if paused else "logging_resumed")
        if paused:
            self.drop_link()
        self.broadcast("status", self.status())

    def event(self, kind, **detail):
        try:
            self.store.insert("events", ts=time.time(), kind=kind, detail=dumps(detail) if detail else None)
        except Exception:  # noqa: BLE001 - logging must never break the link
            log.exception("could not record event %s", kind)

    # -- the live pages' event stream (SSE fan-out)
    def subscribe(self):
        q = queue.Queue(maxsize=500)
        with self.sub_lock:
            self.subscribers.add(q)
        return q

    def unsubscribe(self, q):
        with self.sub_lock:
            self.subscribers.discard(q)

    def broadcast(self, kind, data):
        if data is None:
            return
        msg = f"event: {kind}\ndata: {json.dumps(data, default=str)}\n\n"
        with self.sub_lock:
            for q in list(self.subscribers):
                try:
                    q.put_nowait(msg)
                except queue.Full:
                    self.subscribers.discard(q)
