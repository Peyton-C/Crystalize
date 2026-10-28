"""Plays a mix on FUSER's beat clock, so an agent never has to time a tool call to a bar.

Each bar's steps go to FUSER in one request, timed to reach it just after the bar's
downbeat. FUSER plays a drop that arrives there from the downbeat, and the campaign counts
it as a drop on the downbeat. Bars are four beats: every song in FUSER is in 4/4.
"""
from __future__ import annotations

import math
import threading
import time

from .bridge import GameError

BEATS_PER_BAR = 4

# When a bar's steps should reach FUSER, in seconds after its downbeat. In the campaign a
# drop counted as on the downbeat when it arrived from about 25 ms before to at least 75 ms
# after, at 120 BPM. One sent half a beat or more early still sounded on the downbeat, by
# quantization, but did not count, which is why the player no longer sends ahead. This is
# the middle of what counted, leaving room for a slow round trip either way.
ARRIVE_SECONDS = 0.025

# Once a bar is this close, the player stops polling and sleeps out the rest on the tempo:
# a poll and a round trip together can pass a fifth of a beat, more than the window allows
SETTLE_SECONDS = 0.25

POLL_SECONDS = 0.05


class Player:
    def __init__(self, bridge):
        self.bridge = bridge
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._status: dict = {}
        self._lock = threading.Lock()

    def start(self, name: str, schedule: list, end_bar: int, opening_decks: set[int]) -> None:
        """Play `schedule`, a Plan's, stopping any mix already playing. `opening_decks`
        are the decks the plan fills at bar 0; the rest are emptied first, since the plan
        was checked as if they were."""
        self.stop()
        self._stop = threading.Event()
        with self._lock:
            self._status = {"mix": name, "playing": True, "bar": None, "bars": end_bar,
                            "error": None}
        self._thread = threading.Thread(
            target=self._run, args=(schedule, end_bar, opening_decks, self._stop), daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._thread and self._thread.is_alive():
            self._stop.set()
            self._thread.join()
        with self._lock:
            if self._status.get("playing"):
                self._status["playing"] = False

    def status(self) -> dict | None:
        with self._lock:
            return dict(self._status) if self._status else None

    def _update(self, **fields) -> None:
        with self._lock:
            self._status.update(fields)

    def _clock(self) -> tuple[float, float, float]:
        """The beat, FUSER's beats per second, and how long asking took. The beat was read
        about half that time ago, and a command sent now arrives about half of it later,
        so one sent now reaches FUSER near `beat + trip * per_second`."""
        asked = time.monotonic()
        clock = self.bridge.call("clock")
        trip = time.monotonic() - asked
        return clock["beat"], max(clock.get("bpm") or 0, 1) / 60, trip

    def on_downbeat(self, commands) -> None:
        """Send `commands` in one request, timed as a bar of a mix is to reach FUSER just
        after the next downbeat, and return once they are sent. With nothing playing there
        is no bar to keep in phase with, so they go at once."""
        state = self.bridge.call("state")
        if not any(held and held.get("playing") for held in state["decks"]):
            self._send(commands)
            return
        beat, per_second, trip = self._clock()
        downbeat = _next_downbeat(beat, per_second, trip)
        while (wait := _wait(downbeat, beat, per_second, trip)) >= SETTLE_SECONDS:
            time.sleep(POLL_SECONDS)
            beat, per_second, trip = self._clock()
        if wait > 0:
            time.sleep(wait)
        self._send(commands)

    def _run(self, schedule, end_bar, opening_decks, stop) -> None:
        try:
            state = self.bridge.call("state")
            playing = [d for d, held in enumerate(state["decks"]) if held and held.get("playing")]
            for deck in playing:
                if deck not in opening_decks:
                    self.bridge.call("eject", deck=deck)

            beat, per_second, trip = self._clock()
            pending = list(schedule)
            if playing and pending and pending[0][0] == 0:
                # A deck already playing its opening disc keeps it: dropped again, the
                # campaign counts it as a repeat and as one action too many at once.
                # The same goes for a key or tempo the session already has
                held = [d and d.get("playing") for d in state["decks"]]
                pending[0] = (0, [c for c in pending[0][1] if not _unchanged(c, state, held)])
            if not playing:
                # Nothing to keep in phase with, so bar 0 goes out at once and starts the
                # music where the clock stands
                start = math.ceil(beat / BEATS_PER_BAR) * BEATS_PER_BAR
                if pending and pending[0][0] == 0:
                    self._send(pending.pop(0)[1])
            else:
                # Bar 0 waits for a downbeat like any other, so it lands in phase with what
                # is playing: the first one a command sent now can still reach
                start = _next_downbeat(beat, per_second, trip)
            self._update(bar=0, started_beat=start)

            last = beat
            while not stop.is_set():
                beat, per_second, trip = self._clock()
                if beat < last - 1:
                    # The clock went back, as it may when a disc lands on an empty desk:
                    # bar 0 moves with it by whole bars, or every later step waits for
                    # the clock to climb back to where it stood
                    start += round((beat - last) / BEATS_PER_BAR) * BEATS_PER_BAR
                    self._update(started_beat=start)
                last = beat
                bar = math.floor((beat - start) / BEATS_PER_BAR)
                self._update(bar=max(bar, 0),
                             next_bar=pending[0][0] if pending else None)
                if pending:
                    downbeat = start + pending[0][0] * BEATS_PER_BAR
                    wait = _wait(downbeat, beat, per_second, trip)
                    if wait < SETTLE_SECONDS:
                        if wait > 0 and stop.wait(wait):
                            break
                        self._send(pending.pop(0)[1])
                        continue
                elif bar >= end_bar:
                    break
                stop.wait(POLL_SECONDS)
        except GameError as e:
            self._update(error=str(e))
        finally:
            self._update(playing=False)

    def _send(self, commands) -> None:
        if not commands:
            return
        self.bridge.call("batch", commands=[{"op": op, "args": args} for op, args in commands])


def _next_downbeat(beat: float, per_second: float, trip: float) -> int:
    """The first downbeat a command sent now can still reach."""
    return math.floor((beat + trip * per_second) / BEATS_PER_BAR + 1) * BEATS_PER_BAR


def _wait(downbeat: float, beat: float, per_second: float, trip: float) -> float:
    """How long to wait before sending, for a command to arrive on time at `downbeat`.
    Below zero it is late, after a stall, and goes out at once."""
    return (downbeat - beat) / per_second + ARRIVE_SECONDS - trip


def _unchanged(command, state: dict, held: list) -> bool:
    op, args = command
    if op == "drop":
        return held[args["deck"]] == args["name"]
    if op == "set_key":
        return (state.get("key"), state.get("mode")) == (args["key"], args["mode"])
    if op == "set_tempo":
        return state.get("bpm") == args["bpm"]
    return False
