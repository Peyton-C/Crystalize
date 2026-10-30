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


POOL = [("a", 0), ("b", 0), ("c", 1), ("d", 1)]


def keeper(beat=0.0, playing=("x", None, None, None)):
    fuser = ClockBridge(beat=beat, playing=playing)
    p = Player(fuser)
    p.keep_alive(POOL)
    return fuser, p


def test_keep_alive_drops_then_lets_three_bars_pass():
    fuser, p = keeper(beat=3.95)
    wait_until(lambda: len(fuser.sent) == 1)  # nothing has happened yet, so at once
    assert fuser.sent[0][1:] == ("drop", {"name": "a", "deck": 0, "queue": False})
    # Idle is measured in seconds, and only milliseconds have passed, however far the
    # clock moves
    for beat in (7.95, 11.95, 15.95):
        fuser.to(beat)
        time.sleep(0.03)
    assert len(fuser.sent) == 1
    p.stop_keep_alive()
    assert p.keep_alive_status()["on"] is False


def test_keep_alive_drops_again_once_idle(monkeypatch):
    monkeypatch.setattr(player_module, "KEEP_ALIVE_BARS", 0)
    fuser, p = keeper(beat=3.95)
    wait_until(lambda: len(fuser.sent) == 1)
    fuser.to(7.95)
    wait_until(lambda: len(fuser.sent) == 2)
    assert fuser.sent[1][2]["name"] == "b"  # the next in turn
    p.stop_keep_alive()


def test_keep_alive_stands_off_during_a_riser():
    fuser = ClockBridge(beat=3.95, playing=("x", None, None, None))
    p = Player(fuser)
    p._riser_ends = time.monotonic() + 60
    p.keep_alive(POOL)
    time.sleep(0.2)
    assert not fuser.sent
    p._riser_ends = 0.0
    wait_until(lambda: len(fuser.sent) == 1)
    p.stop_keep_alive()


def test_riser_holds_keep_alive_off_before_it_is_sent(monkeypatch):
    p = Player(ClockBridge(beat=3.95))
    held = []
    monkeypatch.setattr(p, "on_downbeat", lambda _: held.append(p._riser_ends > time.monotonic()))
    p.riser(1)
    assert held == [True] and p._riser_ends > time.monotonic()


def test_keep_alive_skips_discs_on_a_deck_or_remembered():
    fuser, p = keeper(beat=3.95, playing=("a", None, None, None))
    wait_until(lambda: len(fuser.sent) == 1)
    assert fuser.sent[0][2]["name"] == "b"  # a is playing
    p.stop_keep_alive()
    assert p.keep_alive_status()["last"] == {"disc": "b", "deck": 0}


def test_keep_alive_waits_out_a_busy_crowd(monkeypatch):
    # Three actions just went out: a fourth inside 24 beats is overactive
    fuser = ClockBridge(beat=3.95, playing=("x", None, None, None))
    p = Player(fuser)
    p._actions = [time.monotonic()] * 3
    monkeypatch.setattr(player_module, "KEEP_ALIVE_BARS", 0)
    p.keep_alive(POOL)
    time.sleep(0.1)
    assert fuser.sent == []
    p.stop_keep_alive()


def test_a_pace_asked_for_goes_past_a_busy_crowd():
    fuser = ClockBridge(beat=3.95, playing=("x", None, None, None))
    p = Player(fuser)
    p._actions = [time.monotonic() - 1.9] * 3  # a bar ago at 120 BPM, and overactive
    p.keep_alive(POOL, bars=1)
    wait_until(lambda: len(fuser.sent) == 1)
    p.stop_keep_alive()


def test_a_drop_on_a_muted_deck_unmutes_it_in_the_same_request():
    fuser = ClockBridge()
    p = Player(fuser)
    p._send([("mute", {"deck": 2, "on": True})])
    p._send([("drop", {"name": "a", "deck": 2, "queue": False})])
    assert fuser.requests[-1] == ["mute", "drop"]
    assert fuser.sent[-2][2] == {"deck": 2, "on": False}
    p._send([("drop", {"name": "b", "deck": 2, "queue": False})])
    assert fuser.requests[-1] == ["drop"]  # already unmuted


def test_keep_alive_waits_while_a_mix_plays():
    fuser = ClockBridge(beat=3.95, playing=("x", None, None, None))
    p = Player(fuser)
    p.start("test", SCHEDULE, 6, {0})
    p.keep_alive(POOL)
    time.sleep(0.1)
    assert all(args["name"] not in "abcd" for _, op, args in fuser.sent if op == "drop")
    p.stop()
    p.stop_keep_alive()


def test_keep_alive_holds_off_while_a_request_is_open(monkeypatch):
    monkeypatch.setattr(player_module, "KEEP_ALIVE_BARS", 0)
    fuser = ClockBridge(beat=3.95, playing=("x", None, None, None))
    p = Player(fuser)
    p.hold_for_requests(4)  # 8 seconds at 120 BPM, past this test
    p.keep_alive(POOL)
    time.sleep(0.1)
    assert fuser.sent == []
    assert p.keep_alive_status()["waiting"] == "a crowd request is open"
    p.hold_for_requests(0)  # the request settled
    fuser.to(7.95)
    wait_until(lambda: len(fuser.sent) == 1)
    p.stop_keep_alive()


def test_a_request_hold_lapses_on_its_own(monkeypatch):
    monkeypatch.setattr(player_module, "KEEP_ALIVE_BARS", 0)
    fuser = ClockBridge(beat=3.95, playing=("x", None, None, None))
    p = Player(fuser)
    p.hold_for_requests(1)
    p._request_hold = (time.monotonic() - 3, 1)  # a measure is 2 seconds at 120 BPM
    p.keep_alive(POOL)
    wait_until(lambda: len(fuser.sent) == 1)
    p.stop_keep_alive()


def test_keep_alive_rides_out_a_missed_answer():
    fuser = ClockBridge(beat=3.95, playing=("x", None, None, None))
    real, misses = fuser.call, [1]

    def call(op, **args):
        if op == "state" and misses[0]:
            misses[0] -= 1
            raise GameError("FUSER took the command but did not answer")
        return real(op, **args)
    fuser.call = call
    p = Player(fuser)
    p.keep_alive(POOL)
    fuser.to(7.95)
    wait_until(lambda: len(fuser.sent) == 1)
    status = p.keep_alive_status()
    assert status["on"] and "did not answer" in status["missed"]
    p.stop_keep_alive()


def test_keep_alive_stops_when_fuser_is_gone():
    fuser = ClockBridge(beat=3.95)
    fuser.call = lambda op, **args: (_ for _ in ()).throw(GameError("FUSER is not running"))
    p = Player(fuser)
    p.keep_alive(POOL)
    wait_until(lambda: not p.keep_alive_status()["on"])
    assert p.keep_alive_status()["error"] == "FUSER is not running"


def test_on_pickup_aims_at_the_next_pickup_past_the_sync_beat():
    # The loops start at beat 3, so pickup 15 of a 128-beat loop falls on clock beat 18
    fuser = ClockBridge(beat=10.0)
    real = fuser.call
    fuser.call = lambda op, **args: {**real(op, **args), "sync_beat": 3} if op == "clock" \
        else real(op, **args)
    p = Player(fuser)
    drop = ("drop", {"name": "pushitld", "deck": 2, "queue": False})
    result = []
    sender = threading.Thread(target=lambda: result.append(
        p.on_pickup([drop], [15, 28.5], 128)))
    sender.start()
    time.sleep(0.02)
    assert fuser.sent == []
    fuser.to(17.97)
    sender.join(2)
    assert result[0] == pytest.approx(18, abs=0.01) and fuser.sent[0][1] == "drop"


def test_on_pickup_wraps_into_the_next_loop():
    fuser = ClockBridge(beat=127.0)  # past the last pickup, 28.5: next is 128 + 15
    p = Player(fuser)
    sender = threading.Thread(target=p.on_pickup,
                              args=([("drop", {"name": "x", "deck": 0, "queue": False})],
                                    [15, 28.5], 128))
    sender.start()
    fuser.to(142.98)
    sender.join(2)
    assert fuser.sent and fuser.sent[0][0] == 142.98


def test_keep_alive_picks_a_mix_not_heard_yet():
    # a or b on deck 0 beside y makes a mix heard before; c on deck 1 beside x does not
    fuser = ClockBridge(beat=3.95, playing=("x", "y", None, None))
    p = Player(fuser)
    p._heard = {frozenset({"a", "y"}), frozenset({"b", "y"})}
    p.keep_alive(POOL)
    wait_until(lambda: len(fuser.sent) == 1)
    assert fuser.sent[0][2]["name"] == "c"
    p.stop_keep_alive()


def test_keep_alive_prefers_the_disc_dropped_least():
    fuser = ClockBridge(beat=3.95, playing=("x", "y", None, None))
    p = Player(fuser)
    p._heard = {frozenset({n, "y"}) for n in "ab"} | {frozenset({"x", n}) for n in "cd"}
    p._played = {"a": 3, "b": 1, "c": 2, "d": 2}
    p.keep_alive(POOL)
    wait_until(lambda: len(fuser.sent) == 1)
    assert fuser.sent[0][2]["name"] == "b"
    p.stop_keep_alive()


def test_change_count_ejects_a_full_pool_deck_then_fills_it(monkeypatch):
    monkeypatch.setattr(player_module, "KEEP_ALIVE_BARS", 0)
    fuser = ClockBridge(beat=3.95, playing=("x", "y", None, None))
    real = fuser.call

    def call(op, **args):  # an eject empties its deck, as in FUSER
        result = real(op, **args)
        for c in args.get("commands", []):
            if c["op"] == "eject":
                fuser.playing[c["args"]["deck"]] = None
        return result
    fuser.call = call
    p = Player(fuser)
    p.keep_alive(POOL, change_count=True)
    wait_until(lambda: len(fuser.sent) == 2)
    assert fuser.sent[0][1] == "eject"  # decks 0 and 1, the pool's, are both full
    ejected = fuser.sent[0][2]["deck"]
    op, args = fuser.sent[1][1:]
    assert op == "drop" and args["deck"] == ejected
    p.stop_keep_alive()


class InstrumentBridge(ClockBridge):
    """FUSER with an instrument that loads once asked, or never with `loads` false."""

    def __init__(self, loads=True, **kwargs):
        super().__init__(**kwargs)
        self.loads = loads

    def call(self, op, **args):
        if op == "perform_state":
            return {"slot": args["slot"], "crafting": True, "ready": self.loads}
        return super().call(op, **args)


PRESS = {"slot": 0, "x": 2, "y": 2}


def test_perform_presses_on_each_downbeat_and_lets_go_before_the_bar_ends():
    fuser = InstrumentBridge(beat=2.0)
    p = Player(fuser)
    p.perform("DrumCanvas2", 2)
    wait_until(lambda: [op for _, op, _ in fuser.sent] == ["perform_begin"])
    assert fuser.sent[0][2] == {"slot": 0, "name": "DrumCanvas2"}
    fuser.to(3.9)  # the press goes out to land on beat 4
    wait_until(lambda: len(fuser.sent) == 2)
    assert fuser.sent[1] == (3.9, "perform_press", {**PRESS, "on": True})

    # Let go half a beat short of the bar's end, where the goal counts it
    fuser.to(6.9)
    time.sleep(0.03)
    assert len(fuser.sent) == 2
    fuser.to(7.4)
    wait_until(lambda: len(fuser.sent) == 3)
    assert fuser.sent[2][1:] == ("perform_press", {**PRESS, "on": False})
    assert p.perform_status()["played"] == 1

    fuser.to(7.9)  # the second bar
    wait_until(lambda: len(fuser.sent) == 4)
    assert fuser.sent[3][2]["on"] is True
    fuser.to(11.4)
    wait_until(lambda: len(fuser.sent) == 5)
    assert fuser.sent[4][1:] == ("perform_press", {**PRESS, "on": False})

    # Unloaded two beats into the next bar, so the goal reaches its last count first
    fuser.to(13.4)
    time.sleep(0.03)
    assert len(fuser.sent) == 5 and p.perform_status()["on"]
    fuser.to(13.9)
    wait_until(lambda: not p.perform_status()["on"])
    assert fuser.sent[5][1:] == ("perform_end", {"slot": 0})
    assert p.perform_status()["played"] == 2 and p.perform_status()["error"] is None


class RecordingBridge(InstrumentBridge):
    """FUSER that puts a spawned disc on the bass deck, as it picks the deck itself."""

    def call(self, op, **args):
        for c in args.get("commands", []):
            if c["op"] == "record_spawn":
                self.playing[1] = "CraftedInstrument_BassCanvas2_1"
        return super().call(op, **args)


def test_record_presses_each_beat_then_spawns_on_the_downbeat_after():
    fuser = RecordingBridge(beat=3.9)
    p = Player(fuser)
    p.record("BassCanvas2", queue=False)
    for step in range(8):  # each beat from 4 pressed, and let go half a beat on
        wait_until(lambda: len(fuser.sent) == 2 + step * 2)
        assert fuser.sent[-1][2]["on"] is True and fuser.sent[-1][2]["record"] is True
        fuser.to(4.4 + step)
        wait_until(lambda: len(fuser.sent) == 3 + step * 2)
        assert fuser.sent[-1][2]["on"] is False
        fuser.to(4.9 + step)
    wait_until(lambda: fuser.sent[-1][1] == "record_spawn")
    assert fuser.sent[-1] == (11.9, "record_spawn", {"slot": 0, "queue": False})
    wait_until(lambda: p.perform_status()["disc"])
    status = p.perform_status()
    assert status["disc"] == "CraftedInstrument_BassCanvas2_1" and status["deck"] == 1
    assert status["played"] == 8

    # Kept loaded past the spawn, then unloaded, and the disc is remembered
    assert status["on"]
    fuser.to(12.9)
    wait_until(lambda: not p.perform_status()["on"])
    assert fuser.sent[-1][1:] == ("perform_end", {"slot": 0})
    assert p.recorded() == [{"disc": "CraftedInstrument_BassCanvas2_1",
                             "instrument": "BassCanvas2", "deck": 1}]
    p.forget_recorded()
    assert p.recorded() == []


def test_record_is_not_fooled_by_a_recorded_disc_already_on_a_deck():
    older = "CraftedInstrument_BassCanvas2_0"
    fuser = RecordingBridge(beat=3.9, playing=(older, "CraftedInstrument_DrumCanvas2_0", None, None))
    p = Player(fuser)
    p.record("BassCanvas2")
    for step in range(8):
        wait_until(lambda: len(fuser.sent) == 2 + step * 2)
        fuser.to(4.4 + step)
        wait_until(lambda: len(fuser.sent) == 3 + step * 2)
        fuser.to(4.9 + step)
    wait_until(lambda: p.perform_status()["disc"])
    assert p.perform_status()["disc"] == "CraftedInstrument_BassCanvas2_1"
    assert p.perform_status()["deck"] == 1
    p.stop_perform()


def test_a_disc_that_never_lands_is_an_error(monkeypatch):
    monkeypatch.setattr(player_module, "RECORD_BEATS", 1)
    monkeypatch.setattr(player_module, "RECORD_LAND_SECONDS", 0.05)
    fuser = InstrumentBridge(beat=3.9)
    p = Player(fuser)
    p.record("BassCanvas2")
    wait_until(lambda: len(fuser.sent) == 2)
    fuser.to(4.9)
    wait_until(lambda: not p.perform_status()["on"])
    assert "did not reach a deck" in p.perform_status()["error"]
    assert fuser.sent[-1][1] == "perform_end"


def test_stopping_a_performance_lets_go_and_unloads():
    fuser = InstrumentBridge(beat=3.9)
    p = Player(fuser)
    p.perform("DrumCanvas2", 4)
    wait_until(lambda: len(fuser.sent) == 2)  # loaded and pressed
    p.stop_perform()
    assert [op for _, op, _ in fuser.sent] == ["perform_begin", "perform_press",
                                               "perform_press", "perform_end"]
    assert fuser.sent[2][2]["on"] is False
    assert p.perform_status()["on"] is False


def test_stopping_before_the_press_only_unloads():
    fuser = InstrumentBridge(beat=2.0)
    p = Player(fuser)
    p.perform("DrumCanvas2", 4)
    wait_until(lambda: len(fuser.sent) == 1)
    p.stop_perform()
    assert [op for _, op, _ in fuser.sent] == ["perform_begin", "perform_end"]


def test_an_instrument_that_never_loads_is_reported_and_unloaded(monkeypatch):
    monkeypatch.setattr(player_module, "PERFORM_LOAD_SECONDS", 0.01)
    fuser = InstrumentBridge(loads=False, beat=2.0)
    p = Player(fuser)
    p.perform("DrumCanvas2", 1)
    wait_until(lambda: not p.perform_status()["on"])
    assert "did not load" in p.perform_status()["error"]
    assert [op for _, op, _ in fuser.sent] == ["perform_begin", "perform_end"]


def gains(fuser):
    return [a["gain"] for _, op, a in fuser.sent if op == "gain"]


def test_a_fade_steps_the_deck_down_and_lands_exactly(monkeypatch):
    fuser = ClockBridge()
    p = Player(fuser)
    p.fade(2, 1, 0, 1)
    wait_until(lambda: len(gains(fuser)) == 4)
    assert gains(fuser) == [0.75, 0.5, 0.25, 0]
    assert {a["deck"] for _, op, a in fuser.sent if op == "gain"} == {2}


def test_a_fade_starts_without_waiting_for_a_downbeat():
    fuser = ClockBridge(beat=1.5)  # the clock never moves, as when a set has ended
    p = Player(fuser)
    p.fade(0, 1, 0, 1)
    wait_until(lambda: gains(fuser) == [0.75, 0.5, 0.25, 0])


def test_a_new_fade_replaces_the_one_going_on_that_deck():
    fuser = ClockBridge()
    p = Player(fuser)
    p.fade(1, 1, 0, 64)
    wait_until(lambda: gains(fuser))
    p.fade(1, 0.9, 1, 1)
    wait_until(lambda: gains(fuser)[-1] == 1)
    assert gains(fuser)[-4:] == [pytest.approx(0.925), pytest.approx(0.95), pytest.approx(0.975), 1]


def test_a_drop_on_a_faded_deck_brings_it_back_in_the_same_request():
    fuser = ClockBridge()
    p = Player(fuser)
    p.fade(2, 1, 0, 1)
    wait_until(lambda: gains(fuser)[-1:] == [0])
    p._send([("drop", {"deck": 2, "name": "x"})])
    assert fuser.requests[-1] == ["gain", "drop"]
    p._send([("drop", {"deck": 2, "name": "y"})])
    assert fuser.requests[-1] == ["drop"]  # already back


def test_a_deck_faded_back_to_full_needs_nothing_on_a_drop():
    fuser = ClockBridge()
    p = Player(fuser)
    p.fade(2, 0, 1, 1)
    wait_until(lambda: gains(fuser)[-1:] == [1])
    p._send([("drop", {"deck": 2, "name": "x"})])
    assert fuser.requests[-1] == ["drop"]


# ------------------------------------------------------- a plan's long-running steps

def bar_0(fuser, p, schedule, end=8):
    p.start("test", [(0, [("drop", {"name": "abt", "deck": 0, "queue": False})])] + schedule,
            end, {0})
    wait_until(lambda: fuser.sent)


def test_a_fade_takes_its_time_however_slow_the_round_trip():
    class Slow(ClockBridge):
        def call(self, op, **args):
            if op == "gain":
                time.sleep(0.02)
            return super().call(op, **args)
    fuser = Slow(beat=0.0)
    p = Player(fuser)
    began = time.monotonic()
    p.fade(0, 1, 0, 1)  # 4 steps a beat at 120 BPM, 0.125 seconds each
    wait_until(lambda: [a["gain"] for _, op, a in fuser.sent if op == "gain"][-1:] == [0])
    assert time.monotonic() - began < 0.375 + 0.05  # 4 sleeps' worth of trips would pass this


def test_a_mix_starts_a_fade_on_its_bar(monkeypatch):
    fuser = ClockBridge(beat=0.0)
    p = Player(fuser)
    bar_0(fuser, p, [(1, [("fade", {"deck": 0, "start": 1, "to": 0, "beats": 1})])])
    assert not any(op == "gain" for _, op, _ in fuser.sent)
    fuser.to(4.0)
    wait_until(lambda: [a["gain"] for _, op, a in fuser.sent if op == "gain"][-1:] == [0])
    p.stop()


def test_a_mix_sends_cues_and_riser_together_and_holds_keep_alive():
    fuser = ClockBridge(beat=0.0)
    p = Player(fuser)
    bar_0(fuser, p, [(1, [("drop", {"name": "c", "deck": 1, "queue": True}), ("riser", {"mode": 1})])])
    fuser.to(4.0)
    wait_until(lambda: ["drop", "riser"] in fuser.requests)
    assert p._riser_ends > time.monotonic()
    p.stop()


def test_a_mix_starts_a_performance_on_the_bar_it_names(monkeypatch):
    fuser = InstrumentBridge(beat=0.0)
    p = Player(fuser)
    bar_0(fuser, p, [(1, [("perform", {"instrument": "Banjo", "start_bar": 3, "bars": 1})])])
    fuser.to(4.0)
    wait_until(lambda: any(op == "perform_begin" for _, op, _ in fuser.sent))
    fuser.to(11.0)
    time.sleep(0.02)
    assert not any(op == "perform_press" for _, op, _ in fuser.sent)
    fuser.to(12.0)
    wait_until(lambda: any(op == "perform_press" for _, op, _ in fuser.sent))
    press = next(s for s in fuser.sent if s[1] == "perform_press")
    assert press[0] == 12.0
    p.stop()  # the mix's performance goes with it
    wait_until(lambda: any(op == "perform_end" for _, op, _ in fuser.sent))


def test_stopping_a_mix_leaves_a_performance_of_the_agents_own():
    fuser = InstrumentBridge(beat=2.0)
    p = Player(fuser)
    p.perform("Banjo", 1)
    wait_until(lambda: any(op == "perform_begin" for _, op, _ in fuser.sent))
    p.stop()
    assert p.perform_status()["on"]
    p.stop_perform()


def test_a_mix_drops_its_recording_by_the_name_fuser_gave_it():
    fuser = ClockBridge(beat=0.0)
    p = Player(fuser)
    p._named["mine"] = "CraftedInstrument_Bass_1"
    p._awaited.add("mine")
    p._play_bar([("drop", {"name": "mine", "deck": 3, "queue": False})], threading.Event())
    assert fuser.sent[-1][2]["name"] == "CraftedInstrument_Bass_1"


def test_a_recording_that_failed_is_skipped_and_reported():
    fuser = ClockBridge(beat=0.0)
    p = Player(fuser)
    p._awaited.add("mine")
    p._play_bar([("drop", {"name": "mine", "deck": 3, "queue": False}),
                 ("mute", {"deck": 0, "on": True})], threading.Event())
    assert [op for _, op, _ in fuser.sent] == ["mute"]
    assert "mine was not recorded" in p.status()["error"]


def test_a_mix_lands_a_pickup_without_holding_up_its_bars(monkeypatch):
    fuser = ClockBridge(beat=0.0)
    p = Player(fuser)
    bar_0(fuser, p, [(1, [("pickup", {"commands": [("drop_queue", {})], "pickups": [40.0],
                                      "loop_beats": 128.0})]),
                     (2, [("eject", {"deck": 0})])])
    fuser.to(4.0)
    time.sleep(0.02)
    fuser.to(8.0)
    wait_until(lambda: any(op == "eject" for _, op, _ in fuser.sent))
    assert not any(op == "drop_queue" for _, op, _ in fuser.sent)
    fuser.to(40.0)
    wait_until(lambda: any(op == "drop_queue" for _, op, _ in fuser.sent))
    p.stop()


def test_a_new_mix_puts_back_what_the_last_left():
    fuser = ClockBridge(beat=0.0)
    p = Player(fuser)
    p._muted.add(2)
    p._soloed.add(1)
    p._faded.add(3)
    p.start("test", SCHEDULE, 6, {0})
    wait_until(lambda: any(op == "drop" for _, op, _ in fuser.sent))
    sent = {(op, tuple(sorted(a.items()))) for _, op, a in fuser.sent}
    assert ("mute", (("deck", 2), ("on", False))) in sent
    assert ("solo", (("deck", 1), ("on", False))) in sent
    assert ("gain", (("deck", 3), ("gain", 1))) in sent
    p.stop()
