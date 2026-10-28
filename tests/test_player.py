"""Playing a mix against a fake FUSER whose beat clock the test moves by hand."""
import threading
import time

import pytest

from crystalize import player as player_module
from crystalize.bridge import GameError
from crystalize.player import Player


class ClockBridge:
    """FUSER as the player sees it: a clock, a state, and a record of what it was told."""

    def __init__(self, beat=0.0, playing=(None, None, None, None), bpm=100):
        self.beat = beat
        self.bpm = bpm  # the session's, which bar 0's set_tempo changes unless it is 120
        self.sent = []  # (beat when sent, op, args)
        self.playing = list(playing)
        self.requests = []  # the ops of each request that changed something
        self.fail_on = None
        self.lock = threading.Lock()

    def call(self, op, **args):
        with self.lock:
            if op == "clock":
                return {"beat": self.beat, "bpm": 120}
            if op == "state":
                return {"key": 0, "mode": 0, "bpm": self.bpm,
                        "decks": [{"playing": p} for p in self.playing]}
            commands = [(c["op"], c["args"]) for c in args["commands"]] if op == "batch" \
                else [(op, args)]
            if any(o == self.fail_on for o, _ in commands):
                raise GameError("FUSER is not answering")
            self.requests.append([o for o, _ in commands])
            self.sent.extend((self.beat, o, a) for o, a in commands)
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


def test_steps_go_out_to_arrive_on_their_downbeat():
    # Earlier, and the campaign does not count a drop as on the downbeat
    fuser = ClockBridge(beat=13.5)
    p = Player(fuser)
    p.start("test", SCHEDULE, 6, {0})
    wait_until(lambda: len(fuser.sent) == 2)  # the decks were empty, so bar 0 goes at once
    assert [op for _, op, _ in fuser.sent] == ["drop", "set_tempo"]
    assert p.status()["started_beat"] == 16

    fuser.to(23.4)  # bar 2 starts on beat 24, more than SETTLE_SECONDS off at 120 BPM
    time.sleep(0.02)
    assert len(fuser.sent) == 2
    fuser.to(24.0)
    wait_until(lambda: len(fuser.sent) == 3)
    assert fuser.sent[2] == (24.0, "drop", {"name": "bbs", "deck": 1, "queue": False})

    fuser.to(32.0)
    wait_until(lambda: len(fuser.sent) == 4)
    assert p.status()["playing"]
    fuser.to(40.0)  # bar 6, the end
    wait_until(lambda: not p.status()["playing"])
    assert p.status()["error"] is None


@pytest.mark.parametrize("beat, start", [(13.5, 16), (15.3, 16), (15.99, 16), (16.0, 20),
                                         (12.0, 16)])
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
    fuser.to(start)
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
    fuser.to(8.0)  # bar 2 lands on beat 8 of the restarted clock, not 508
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
    fuser.to(4.0)
    wait_until(lambda: len(fuser.sent) == 3)
    p.stop()


def test_a_bar_goes_out_in_one_request():
    # One round trip each put a bar's last drop a third of a beat behind its first
    fuser = ClockBridge(beat=0.0)
    p = Player(fuser)
    p.start("test", SCHEDULE, 6, {0})
    wait_until(lambda: len(fuser.sent) == 2)
    assert fuser.requests == [["drop", "set_tempo"]]
    p.stop()


def test_the_last_stretch_is_slept_on_the_tempo():
    # Polling there could overshoot the window, so the player waits it out without asking
    fuser = ClockBridge(beat=0.0)
    p = Player(fuser)
    p.start("test", SCHEDULE, 6, {0})
    wait_until(lambda: len(fuser.sent) == 2)
    fuser.to(7.8)  # 0.1 s and ARRIVE_SECONDS before bar 2, at 120 BPM
    asked = time.monotonic()
    wait_until(lambda: len(fuser.sent) == 3)
    assert time.monotonic() - asked >= 0.1
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
    fuser.to(16.0)  # bar 4, whose eject fails
    wait_until(lambda: not p.status()["playing"])
    assert p.status()["error"] == "FUSER is not answering"


def test_a_deck_already_playing_its_opening_disc_keeps_it():
    # Dropped again, the campaign counts it as a repeat and an action too many
    fuser = ClockBridge(beat=13.5, playing=("abt", None, None, None), bpm=120)
    p = Player(fuser)
    p.start("test", SCHEDULE, 6, {0})
    wait_until(lambda: p.status().get("started_beat") is not None)
    fuser.to(16.0)
    fuser.to(24.0)
    wait_until(lambda: len(fuser.sent) == 1)
    assert fuser.sent[0][1:] == ("drop", {"name": "bbs", "deck": 1, "queue": False})
    p.stop()


def test_a_different_opening_disc_still_drops():
    fuser = ClockBridge(beat=13.5, playing=("old", None, None, None))
    p = Player(fuser)
    p.start("test", SCHEDULE, 6, {0})
    fuser.to(16.0)
    wait_until(lambda: len(fuser.sent) == 2)
    assert fuser.requests == [["drop", "set_tempo"]]
    p.stop()


def test_on_downbeat_waits_for_the_next_bar():
    fuser = ClockBridge(beat=13.5, playing=("x", None, None, None))
    p = Player(fuser)
    drop = ("drop", {"name": "bbs", "deck": 1, "queue": False})
    sender = threading.Thread(target=p.on_downbeat, args=([drop],))
    sender.start()
    time.sleep(0.02)
    assert fuser.sent == []
    fuser.to(15.95)  # 25 ms before beat 16, inside SETTLE_SECONDS
    sender.join(2)
    assert not sender.is_alive()
    assert fuser.sent == [(15.95, "drop", drop[1])]


def test_on_downbeat_with_nothing_playing_goes_at_once():
    fuser = ClockBridge(beat=13.5)
    Player(fuser).on_downbeat([("drop_queue", {})])
    assert fuser.requests == [["drop_queue"]]
