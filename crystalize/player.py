"""Plays a mix on FUSER's beat clock, so an agent never has to time a tool call to a bar.

FUSER quantizes a drop to the next beat, half bar or bar, as the player has it set, or
to cues a track defines. A bar's downbeat is all three of the first, so each bar's steps
are sent within the last beat before it, and land on the bar whichever is set. Cues
cannot be planned for. Bars are four beats: every song in FUSER is in 4/4.
"""
from __future__ import annotations

import math
import threading

from .bridge import GameError

BEATS_PER_BAR = 4

# How early a bar's steps are sent, in beats. Under 1, so beat quantization still takes
# the downbeat and not the beat before it. A quarter beat is left on that side for clock
# jitter, and the rest is for the trip to the game thread: the poll here, the mod's 50 ms
# poll and a frame, which three quarters of a beat covers even at 200 BPM, 225 ms
FIRE_AHEAD_BEATS = 0.75

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

    def _run(self, schedule, end_bar, opening_decks, stop) -> None:
        try:
            state = self.bridge.call("state")
            playing = [d for d, held in enumerate(state["decks"]) if held and held.get("playing")]
            for deck in playing:
                if deck not in opening_decks:
                    self.bridge.call("eject", deck=deck)

            beat = self.bridge.call("clock")["beat"]
            pending = list(schedule)
            if not playing:
                # Nothing to keep in phase with, so bar 0 goes out at once and starts the
                # music where the clock stands
                start = math.ceil(beat / BEATS_PER_BAR) * BEATS_PER_BAR
                if pending and pending[0][0] == 0:
                    self._send(pending.pop(0)[1])
            else:
                # Bar 0 waits for a downbeat like any other, so it lands in phase with what
                # is playing: the first one far enough off to send ahead of
                start = math.floor((beat + FIRE_AHEAD_BEATS) / BEATS_PER_BAR + 1) * BEATS_PER_BAR
            self._update(bar=0, started_beat=start)

            last = beat
            while not stop.is_set():
                beat = self.bridge.call("clock")["beat"]
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
                while pending and beat >= start + pending[0][0] * BEATS_PER_BAR - FIRE_AHEAD_BEATS:
                    self._send(pending.pop(0)[1])
                if not pending and bar >= end_bar:
                    break
                stop.wait(POLL_SECONDS)
        except GameError as e:
            self._update(error=str(e))
        finally:
            self._update(playing=False)

    def _send(self, commands) -> None:
        for op, args in commands:
            self.bridge.call(op, **args)
