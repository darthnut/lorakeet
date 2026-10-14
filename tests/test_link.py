"""link.RadioLink: the reconnect loop, watchdog and pausing every protocol's link shares (docs/MESHCORE.md, step 3)."""
import time

import pytest

import link


class _Stop(BaseException):
    """Ends run()'s endless loop (BaseException: the loop catches Exception)."""


class _Store:
    def __init__(self):
        self.events = []

    def insert(self, table, **row):
        self.events.append(row["kind"])


class _Link(link.RadioLink):
    LOOP_S = 0

    def __init__(self, passes):
        super().__init__(_Store())
        self.calls, self.passes = [], passes

    def connect(self):
        self.calls.append("connect")
        self.connected, self.last_rx = True, time.time()

    def drop_link(self):
        self.calls.append("drop")
        self.connected = False

    def tick(self):
        self.calls.append("tick")
        self.passes -= 1
        if not self.passes:
            raise _Stop


def test_connects_then_ticks_and_the_watchdog_drops_a_silent_radio():
    lk = _Link(passes=2)
    with pytest.raises(_Stop):
        lk.run()
    assert lk.calls == ["connect", "tick", "tick"]
    lk.passes, lk.calls, lk.last_rx = 1, [], time.time() - lk.WATCHDOG_S - 1
    with pytest.raises(_Stop):
        lk.run()
    assert lk.calls == ["drop", "connect", "tick"] and "watchdog" in lk.store.events


def test_paused_lets_go_and_stays_disconnected():
    lk = _Link(passes=1)
    lk.connected = True
    q = lk.subscribe()
    lk.set_paused(True)
    assert lk.calls == ["drop"] and lk.store.events == ["logging_paused"] and "event: status" in q.get_nowait()
    with pytest.raises(_Stop):
        lk.run()
    assert lk.calls == ["drop", "tick"]   # no reconnect while paused
    lk.set_paused(True)                   # already paused: nothing happens
    assert lk.store.events == ["logging_paused"]


def test_a_failing_pass_never_kills_the_loop():
    lk = _Link(passes=1)
    real, tries = lk.connect, []

    def flaky():
        tries.append(1)
        if len(tries) == 1:
            raise OSError("port busy")
        real()
    lk.connect = flaky
    with pytest.raises(_Stop):
        lk.run()
    assert len(tries) == 2 and lk.calls == ["connect", "tick"]   # the failed pass was logged, the next one connected
