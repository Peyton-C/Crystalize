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

# The crowd meter, as its widget's rules read in a campaign set: it drains after 6 idle
# seconds, counts more than 3 actions in 24 beats as overactive, and remembers the last 3
# discs, a drop of one of them being a repeat. Each command a batch carries is one action
BUSY_BEATS, BUSY_ACTIONS, REMEMBERED_DISCS = 24, 3, 3

# How long the keep-alive lets the mix sit before it changes a disc. Two bars would keep
# 6 seconds idle from passing at any tempo but leave three drops in 24 beats, one more
# action from overactive. Three bars lets a little drain through below 120 BPM, a second
# or two, and a good action earns more than that costs
KEEP_ALIVE_BARS = 3


class Player:
    def __init__(self, bridge):
        self.bridge = bridge
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._status: dict = {}
        self._lock = threading.Lock()
        # What the crowd has seen, from every request this player sends: when each action
        # went out, and the discs dropped, newest last
        self._actions: list[float] = []
        self._dropped: list[str] = []
        self._keeper: threading.Thread | None = None
        self._keep_stop = threading.Event()
        self._keep_status: dict = {}

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

    def keep_alive(self, pool: list[tuple[str, int]]) -> None:
        """Keep the crowd meter from draining while nothing else changes the mix: at the
        first downbeat KEEP_ALIVE_BARS after the last action, drop the next disc of `pool`,
        (disc, deck) pairs, that is neither on a deck nor one the crowd remembers. It waits
        while a mix plays, and skips a bar that would make the set overactive."""
        self.stop_keep_alive()
        self._keep_stop = threading.Event()
        with self._lock:
            self._keep_status = {"on": True, "discs": len(pool), "dropped": 0, "last": None,
                                 "waiting": None, "error": None}
        self._keeper = threading.Thread(target=self._keep, args=(pool, self._keep_stop),
                                        daemon=True)
        self._keeper.start()

    def stop_keep_alive(self) -> None:
        if self._keeper and self._keeper.is_alive():
            self._keep_stop.set()
            self._keeper.join()
        with self._lock:
            if self._keep_status.get("on"):
                self._keep_status["on"] = False

    def keep_alive_status(self) -> dict | None:
        with self._lock:
            return dict(self._keep_status) if self._keep_status else None

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
        now = time.monotonic()
        with self._lock:
            self._actions = [t for t in self._actions if now - t < 60] + [now] * len(commands)
            for op, args in commands:
                if op == "drop" and not args.get("queue"):
                    self._dropped = (self._dropped + [args["name"]])[-REMEMBERED_DISCS:]

    def _keep(self, pool, stop) -> None:
        turn = 0
        try:
            while not stop.is_set():
                if self._thread and self._thread.is_alive():
                    stop.wait(POLL_SECONDS)  # a mix keeps the crowd busy enough
                    continue
                beat, per_second, trip = self._clock()
                downbeat = _next_downbeat(beat, per_second, trip)
                wait = _wait(downbeat, beat, per_second, trip)
                if wait >= SETTLE_SECONDS:
                    stop.wait(POLL_SECONDS)
                    continue
                lands = time.monotonic() + max(wait, 0)
                with self._lock:
                    last = self._actions[-1] if self._actions else None
                    busy = sum(1 for t in self._actions if lands - t < BUSY_BEATS / per_second)
                    remembered = list(self._dropped)
                # Half a beat short of the bars, so jitter in the clock never puts it off
                # by a whole bar
                idle = (KEEP_ALIVE_BARS * BEATS_PER_BAR - 0.5) / per_second
                pick = None
                if (last is None or lands - last >= idle) and busy < BUSY_ACTIONS:
                    state = self.bridge.call("state")
                    held = {d.get("playing") for d in state["decks"] if d}
                    for i in range(len(pool)):
                        disc, deck = pool[(turn + i) % len(pool)]
                        if disc not in held and disc not in remembered:
                            pick, turn = (disc, deck), (turn + i + 1) % len(pool)
                            break
                    with self._lock:
                        self._keep_status["waiting"] = None if pick else \
                            "every disc is on a deck or one the crowd remembers"
                if pick is None:
                    stop.wait(max(wait, 0) + POLL_SECONDS)  # past this downbeat
                    continue
                # The state took a round trip, so the wait is measured again
                beat, per_second, trip = self._clock()
                wait = _wait(downbeat, beat, per_second, trip)
                if wait > 0 and stop.wait(wait):
                    break
                self._send([("drop", {"name": pick[0], "deck": pick[1], "queue": False})])
                with self._lock:
                    self._keep_status["dropped"] += 1
                    self._keep_status["last"] = {"disc": pick[0], "deck": pick[1]}
        except GameError as e:
            with self._lock:
                self._keep_status["error"] = str(e)
        finally:
            with self._lock:
                self._keep_status["on"] = False


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
