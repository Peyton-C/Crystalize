"""Playing a mix against a fake FUSER whose beat clock the test moves by hand."""
import threading
import time

import pytest

from crystalize import player as player_module
from crystalize.bridge import GameError
from crystalize.player import Player


class ClockBridge:
    """FUSER as the player sees it: a clock, a state, and a record of what it was told."""

    def __init__(self, beat=0.0, playing=(None, None, None, None)):
        self.beat = beat
        self.sent = []  # (beat when sent, op, args)
        self.playing = list(playing)
        self.fail_on = None
        self.lock = threading.Lock()

    def call(self, op, **args):
        with self.lock:
            if op == "clock":
                return {"beat": self.beat}
            if op == "state":
                return {"decks": [{"playing": p} for p in self.playing]}
            if op == self.fail_on:
                raise GameError("FUSER is not answering")
            self.sent.append((self.beat, op, args))
            return {}

    def to(self, beat):
        with self.lock:
            self.beat = beat


def wait_until(predicate, seconds=2.0):
    deadline = time.monotonic() + seconds
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.005)


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(player_module, "POLL_SECONDS", 0.002)


SCHEDULE = [
    (0, [("drop", {"name": "abt", "deck": 0, "queue": False}), ("set_tempo", {"bpm": 120})]),
    (2, [("drop", {"name": "bbs", "deck": 1, "queue": False})]),
    (4, [("eject", {"deck": 0})]),
]


def test_steps_go_out_in_the_last_beat_before_their_bar():
    # Inside the beat before the downbeat, so beat, half bar and bar quantization all
    # land them on it
    fuser = ClockBridge(beat=13.5)
    p = Player(fuser)
    p.start("test", SCHEDULE, 6, {0})
    wait_until(lambda: len(fuser.sent) == 2)  # the decks were empty, so bar 0 goes at once
    assert [op for _, op, _ in fuser.sent] == ["drop", "set_tempo"]
    assert p.status()["started_beat"] == 16

    fuser.to(23.2)  # bar 2 starts on beat 24
    time.sleep(0.02)
    assert len(fuser.sent) == 2
    fuser.to(23.25)
    wait_until(lambda: len(fuser.sent) == 3)
    assert fuser.sent[2] == (23.25, "drop", {"name": "bbs", "deck": 1, "queue": False})

    fuser.to(31.3)
    wait_until(lambda: len(fuser.sent) == 4)
    assert p.status()["playing"]
    fuser.to(40.0)  # bar 6, the end
    wait_until(lambda: not p.status()["playing"])
    assert p.status()["error"] is None


@pytest.mark.parametrize("beat, start", [(13.5, 16), (15.2, 16), (15.3, 20), (12.0, 16)])
def test_bar_0_waits_for_a_downbeat_when_music_is_playing(beat, start):
    # Sent at once it would land on the next beat under beat quantization, out of phase
    # with the decks already playing
    fuser = ClockBridge(beat=beat, playing=("x", None, None, None))
    p = Player(fuser)
    p.start("test", SCHEDULE, 6, {0})
    wait_until(lambda: p.status().get("started_beat") is not None)
    assert p.status()["started_beat"] == start
    time.sleep(0.02)
    assert fuser.sent == []
    fuser.to(start - 0.75)
    wait_until(lambda: len(fuser.sent) == 2)
    p.stop()


def test_empty_desk_starts_the_clock_at_zero():
    fuser = ClockBridge(beat=0.0)
    p = Player(fuser)
    p.start("test", SCHEDULE, 6, {0})
    wait_until(lambda: p.status().get("started_beat") is not None)
    assert p.status()["started_beat"] == 0


def test_clock_restart_moves_bar_0_with_it():
    # The desk stood empty at beat 500; the opening drop restarts the clock from 0
    fuser = ClockBridge(beat=500.0)
    p = Player(fuser)
    p.start("test", SCHEDULE, 6, {0})
    wait_until(lambda: len(fuser.sent) == 2)
    fuser.to(0.5)
    wait_until(lambda: p.status()["started_beat"] == 0)
    fuser.to(7.25)  # bar 2 lands on beat 8 of the restarted clock, not 508
    wait_until(lambda: len(fuser.sent) == 3)
    p.stop()


def test_jitter_does_not_move_bar_0():
    fuser = ClockBridge(beat=13.5)
    p = Player(fuser)
    p.start("test", SCHEDULE, 6, {0})
    wait_until(lambda: len(fuser.sent) == 2)
    fuser.to(17.0)
    time.sleep(0.02)
    fuser.to(16.6)  # a reading a little behind the last
    time.sleep(0.02)
    assert p.status()["started_beat"] == 16
    p.stop()


def test_decks_the_plan_does_not_fill_are_emptied():
    fuser = ClockBridge(playing=("x", None, "y", None))
    p = Player(fuser)
    p.start("test", SCHEDULE, 6, {0})
    wait_until(lambda: len(fuser.sent) == 1)
    assert fuser.sent[0][1:] == ("eject", {"deck": 2})  # deck 0 is about to be replaced
    fuser.to(3.5)
    wait_until(lambda: len(fuser.sent) == 3)
    p.stop()


def test_a_late_step_goes_out_at_once():
    fuser = ClockBridge(beat=0.0)
    p = Player(fuser)
    p.start("test", SCHEDULE, 6, {0})
    wait_until(lambda: len(fuser.sent) == 2)
    fuser.to(17.0)  # past bars 2 and 4: a slow game thread, or a stall
    wait_until(lambda: len(fuser.sent) == 4)
    p.stop()


def test_stop():
    fuser = ClockBridge()
    p = Player(fuser)
    p.start("test", SCHEDULE, 6, {0})
    wait_until(lambda: len(fuser.sent) == 2)
    p.stop()
    assert not p.status()["playing"]
    fuser.to(100.0)
    time.sleep(0.02)
    assert len(fuser.sent) == 2


def test_error_stops_and_is_reported():
    fuser = ClockBridge()
    fuser.fail_on = "eject"
    p = Player(fuser)
    p.start("test", SCHEDULE, 6, {0})
    fuser.to(15.5)  # past where bar 4's eject goes out
    wait_until(lambda: not p.status()["playing"])
    assert p.status()["error"] == "FUSER is not answering"
