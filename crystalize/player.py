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

# A pickup drop is aimed like a downbeat one, to arrive just after the beat, until the game
# says otherwise: it is the window that counted for downbeats
PICKUP_ARRIVE_SECONDS = ARRIVE_SECONDS

# How far ahead the next pickup has to be to aim for it: time for a sent command to arrive
# and for the queued disc to load onto its deck
PICKUP_LEAD_BEATS = 2

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
# or two, and a good action earns more than that costs. It is the pace when none is asked
# for, and the only one held to the overactive rule: a 5 star player was seen changing a
# disc nearly every bar and ending each section in the top band, which this pace never
# reached, so a faster one asked for goes as asked
KEEP_ALIVE_BARS = 3

# Unanswered commands in a row before the keep-alive takes FUSER for gone
KEEP_ALIVE_MISSES = 3

# A performance presses a pad on each downbeat and lets go this far short of the bar's
# end, as a player phrases it, a bar at a time
PERFORM_RELEASE_BEATS = 0.5

# A Perform goal counts the time the instrument plays, once for every 4 beats, through the
# gaps between presses. Its count ran a third of a bar behind the bars played, and
# unloading the instrument loses the count not yet reached, so it stays loaded this far
# into the bar after: past the lag, and well short of the 4 beats another count needs
PERFORM_END_BEATS = 2

# How long an instrument may take to load before a performance gives up on it. One loaded
# at once in the game, but only after it had been loaded before
PERFORM_LOAD_SECONDS = 5

# A fade moves the deck's fader this many times a beat. FUSER counts a fade goal only for a
# fader that moved on its way down: cut straight to 0, the same goal stayed at nothing. A
# 1 beat fade in 4 steps counted, and so did a 4 beat one in 16. Unlike every other action
# it has no perfect timing: FUSER grades each fade Ok wherever it starts or lands and pays
# no timing points, so a fade starts at once instead of waiting for a downbeat
FADE_STEPS_PER_BEAT = 4

# A recording fills FUSER's loop, 8 beats whatever the instrument, with a press on each beat
# let go this far on. FUSER keeps it in steps of a quarter beat
RECORD_BEATS = 8
RECORD_HOLD_BEATS = 0.5

# FUSER names a recorded disc from its instrument and the time
RECORDED_PREFIX = "CraftedInstrument_"

# How long a spawned disc may take to show on a deck, and how long the instrument stays
# loaded after, past the spawn on the downbeat
RECORD_LAND_SECONDS = 5
RECORD_END_BEATS = 1

# How long keep-alive leaves the decks to a riser. Both of FUSER's take 4 bars from the
# downbeat they start on, emptying the decks a bar before the end, so a disc dropped in that
# time would not last. In seconds, not beats: smart and random move the tempo during the
# riser, random as low as 60 BPM, and 16 beats at that is the longest it can take
RISER_HOLD_SECONDS = 16

# Commands in a schedule that are the player's to run beside the bar clock, not FUSER's
JOBS = ("fade", "perform", "record", "pickup")

# The instrument slot performances play in. FUSER has four, apart from the decks
PERFORM_SLOT = 0

# The pad pressed, from 0 in each direction. Any pad counts; a 5 by 5 drum kit's middle
# was the one tried
PERFORM_PAD = (2, 2)


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
        self._sync_beat = 0.0
        # Every mix heard this session, as the discs on the decks together, and how often
        # each disc has been dropped: freshness rewards a mix that is new
        self._heard: set[frozenset] = set()
        self._played: dict[str, int] = {}
        self._deck_since: dict[int, float] = {}  # when the keep-alive last changed each deck
        # Decks this player muted and has not unmuted. FUSER's state does not say, and a
        # disc dropped on a muted deck earns nothing: no goal, request or crowd counts it
        self._muted: set[int] = set()
        # The same for decks faded below full, where a disc would drop unheard
        self._faded: set[int] = set()
        self._fades: dict[int, tuple[threading.Thread, threading.Event]] = {}
        self._riser_ends = 0.0  # when the riser going stops holding the decks, by the clock
        self.on_send = None  # told every batch sent, for the set's log
        # When the set was last read and how many measures its open crowd requests had left
        self._request_hold: tuple[float, int] | None = None
        self._performer: threading.Thread | None = None
        self._perform_stop = threading.Event()
        self._perform_status: dict = {}
        self._recorded: list[dict] = []  # discs made this session, newest last
        self._soloed: set[int] = set()  # decks this player soloed and has not unsoloed
        self._named: dict[str, str] = {}  # what the mix calls its recordings, to FUSER's names
        self._mix_performs = False  # whether the instrument going is the mix's, to stop with it
        self._awaited: set[str] = set()  # the names of the mix's recordings

    def start(self, name: str, schedule: list, end_bar: int, opening_decks: set[int]) -> None:
        """Play `schedule`, a Plan's, stopping any mix already playing. `opening_decks`
        are the decks the plan fills at bar 0; the rest are emptied first, since the plan
        was checked as if they were."""
        self.stop()
        self._stop = threading.Event()
        with self._lock:
            self._named.clear()
            self._awaited.clear()
            self._status = {"mix": name, "playing": True, "bar": None, "bars": end_bar,
                            "error": None}
        self._thread = threading.Thread(
            target=self._run, args=(schedule, end_bar, opening_decks, self._stop), daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._thread and self._thread.is_alive():
            self._stop.set()
            self._thread.join()
        if self._mix_performs:
            self.stop_perform()
            self._mix_performs = False
        with self._lock:
            if self._status.get("playing"):
                self._status["playing"] = False

    def status(self) -> dict | None:
        with self._lock:
            return dict(self._status) if self._status else None

    def keep_alive(self, pool: list[tuple[str, int]], change_count: bool = False,
                   bars: int | None = None) -> None:
        """Keep the crowd meter from draining while nothing else changes the mix: at the
        first downbeat KEEP_ALIVE_BARS after the last action, drop a disc of `pool`,
        (disc, deck) pairs, that is neither on a deck nor one the crowd remembers. It waits
        while a mix plays, and skips a bar that would make the set overactive.

        With `change_count`, it takes turns ejecting the pool's longest-playing deck and
        filling a pool deck left empty, so every change also changes how many decks play,
        which freshness pays for on top of a new mix.

        With `bars`, it changes a disc that many bars after the last action instead, and
        no longer skips a bar for being overactive."""
        self.stop_keep_alive()
        self._keep_stop = threading.Event()
        with self._lock:
            self._keep_status = {"on": True, "discs": len(pool), "dropped": 0, "last": None,
                                 "waiting": None, "error": None}
        self._keeper = threading.Thread(target=self._keep,
                                        args=(pool, self._keep_stop, change_count, bars),
                                        daemon=True)
        self._keeper.start()

    def stop_keep_alive(self) -> None:
        if self._keeper and self._keeper.is_alive():
            self._keep_stop.set()
            self._keeper.join()
        with self._lock:
            if self._keep_status.get("on"):
                self._keep_status["on"] = False

    def hold_for_requests(self, measures: int) -> None:
        """Hold the keep-alive off for `measures` from now, while a crowd request is open.
        A disc it dropped could fill the request by chance, and with two open that spends
        the one a combo needed. Counting from now lets the hold lapse on its own when
        nobody reads the set again, and 0 ends it."""
        with self._lock:
            self._request_hold = (time.monotonic(), measures) if measures > 0 else None

    def keep_alive_status(self) -> dict | None:
        with self._lock:
            return dict(self._keep_status) if self._keep_status else None

    def perform(self, instrument: str, bars: int, when=None) -> None:
        """Play `instrument` over the mix as a player would for `bars` one-bar
        performances: load it, press a pad on each downbeat from the next and let go just
        before the bar ends. Stops any performance already going. `when` gives the clock
        beat to start on instead, if it can still be reached once loaded."""
        self.stop_perform()
        self._mix_performs = False
        self._perform_stop = threading.Event()
        with self._lock:
            self._perform_status = {"instrument": instrument, "bars": bars, "on": True,
                                    "played": 0, "error": None}
        self._performer = threading.Thread(
            target=self._perform, args=(instrument, bars, self._perform_stop, when), daemon=True)
        self._performer.start()

    def record(self, instrument: str, queue: bool = False, when=None, name=None,
               deck=None) -> None:
        """Record a loop on `instrument` and turn it into a disc: load it, press a pad on
        each beat of the next RECORD_BEATS from a downbeat, and spawn the disc on the downbeat
        after, cued with `queue`. Shares the slot with a performance, so it stops any going.
        For a mix, `when` gives the clock beat to start on, as perform's does, `name` what
        the mix calls the disc, and `deck` where the plan expects it to land."""
        self.stop_perform()
        self._mix_performs = False
        self._perform_stop = threading.Event()
        with self._lock:
            self._perform_status = {"instrument": instrument, "beats": RECORD_BEATS, "on": True,
                                    "played": 0, "disc": None, "deck": None, "queued": queue,
                                    "error": None}
        self._performer = threading.Thread(
            target=self._record, args=(instrument, queue, self._perform_stop, when, name, deck),
            daemon=True)
        self._performer.start()

    def recorded(self) -> list[dict]:
        """The discs recorded this session, which drop by name like any other."""
        with self._lock:
            return [dict(r) for r in self._recorded]

    def forget_recorded(self) -> None:
        """Forget the recorded discs, as when a new set is loaded."""
        with self._lock:
            self._recorded.clear()
            self._named.clear()

    def stop_perform(self) -> None:
        """Let go of the pad and unload the instrument, if a performance or recording is going."""
        if self._performer and self._performer.is_alive():
            self._perform_stop.set()
            self._performer.join()

    def fade(self, deck: int, start: float, to: float, beats: int) -> None:
        """Move `deck`'s volume from `start` to `to` over `beats`, in the background.
        Replaces any fade already going on that deck."""
        self._stop_fade(deck)
        stop = threading.Event()
        thread = threading.Thread(target=self._fade, args=(deck, start, to, beats, stop),
                                  daemon=True)
        self._fades[deck] = (thread, stop)
        thread.start()

    def _stop_fade(self, deck: int) -> None:
        thread, stop = self._fades.pop(deck, (None, None))
        if thread and thread.is_alive():
            stop.set()
            thread.join()

    def stop_fades(self) -> None:
        """Stop every fade where it stands, as when the session is left. FUSER puts every
        deck back at full when it loads a set, so nothing stays faded."""
        for deck in list(self._fades):
            self._stop_fade(deck)
        with self._lock:
            self._faded.clear()

    def _fade(self, deck, start, to, beats, stop) -> None:
        # Paced by the wall clock, not FUSER's beat: that clock stands still once a set ends,
        # and a fade waiting on it would never finish
        steps = beats * FADE_STEPS_PER_BEAT
        try:
            step_seconds = 60 / max(self.bridge.call("clock").get("bpm") or 0, 1) / FADE_STEPS_PER_BEAT
            with self._lock:
                self._faded.add(deck)
            if self.on_send:
                self.on_send([("fade", {"deck": deck, "to": to, "beats": beats})])
            # Each step aims at its own time: a sleep between them adds every round trip, and
            # an 8 beat fade ran a bar over, into the plan's next step on that deck
            began = time.monotonic()
            for step in range(1, steps + 1):
                # Sent alone rather than through _send: the steps are one action to the
                # crowd and the log, not dozens
                self.bridge.call("gain", deck=deck, gain=start + (to - start) * step / steps)
                if step < steps and stop.wait(max(began + step * step_seconds - time.monotonic(), 0)):
                    return
            if to >= 1:
                with self._lock:
                    self._faded.discard(deck)
        except GameError:
            pass  # FUSER has gone, and the deck with it

    def perform_status(self) -> dict | None:
        with self._lock:
            return dict(self._perform_status) if self._perform_status else None

    def _start_beat(self, when) -> int:
        """The clock beat an instrument starts on: the one `when` asks for, or failing that,
        being unset or already past, the next downbeat."""
        soonest = _next_downbeat(*self._clock())
        return soonest if when is None else max(when(), soonest)

    def _perform(self, instrument, bars, stop, when=None) -> None:
        slot = {"slot": PERFORM_SLOT}
        pad = {"x": PERFORM_PAD[0], "y": PERFORM_PAD[1]}
        begun = held = False
        try:
            self.bridge.call("perform_begin", name=instrument, **slot)
            begun = True
            if not self._loaded(instrument, stop):
                return
            start = self._start_beat(when)
            for bar in range(bars):
                press = start + bar * BEATS_PER_BAR
                if not self._until(press, stop):
                    return
                self._send([("perform_press", {**slot, **pad, "on": True})])
                held = True
                if not self._until(press + BEATS_PER_BAR - PERFORM_RELEASE_BEATS, stop):
                    return
                self._send([("perform_press", {**slot, **pad, "on": False})])
                held = False
                with self._lock:
                    self._perform_status["played"] = bar + 1
            self._until(start + bars * BEATS_PER_BAR + PERFORM_END_BEATS, stop)
        except GameError as e:
            with self._lock:
                self._perform_status["error"] = str(e)
        finally:
            self._let_go(slot, pad, held, begun)

    def _loaded(self, instrument, stop) -> bool:
        """Wait for the instrument to load. Returns whether it did before `stop` was set."""
        deadline = time.monotonic() + PERFORM_LOAD_SECONDS
        while not self.bridge.call("perform_state", slot=PERFORM_SLOT).get("ready"):
            if time.monotonic() > deadline:
                raise GameError(f"{instrument} did not load in {PERFORM_LOAD_SECONDS} seconds")
            if stop.wait(POLL_SECONDS):
                return False
        return True

    def _record(self, instrument, queue, stop, when=None, name=None, expected=None) -> None:
        slot = {"slot": PERFORM_SLOT}
        pad = {"x": PERFORM_PAD[0], "y": PERFORM_PAD[1], "record": True}
        begun = held = False
        try:
            self.bridge.call("perform_begin", name=instrument, **slot)
            begun = True
            if not self._loaded(instrument, stop):
                return
            before = _discs_on_decks(self.bridge.call("state"))
            start = self._start_beat(when)
            for beat in range(RECORD_BEATS):
                if not self._until(start + beat, stop):
                    return
                self._send([("perform_press", {**slot, **pad, "on": True})])
                held = True
                if not self._until(start + beat + RECORD_HOLD_BEATS, stop):
                    return
                self._send([("perform_press", {**slot, **pad, "on": False})])
                held = False
                with self._lock:
                    self._perform_status["played"] = beat + 1
            spawn = start + RECORD_BEATS
            if not self._until(spawn, stop):
                return
            self._send([("record_spawn", {**slot, "queue": queue})])
            self._find_recorded(instrument, before, stop, name, expected)
            self._until(spawn + RECORD_END_BEATS, stop)
        except GameError as e:
            with self._lock:
                self._perform_status["error"] = str(e)
        finally:
            self._let_go(slot, pad, held, begun)

    def _find_recorded(self, instrument, before, stop, name=None, expected=None) -> None:
        """Read which disc the spawn made and where it went: FUSER names it and picks its
        deck, and says neither. It is the one of this instrument's on a deck that was not
        there before the recording, which also covers discs from before a server restart."""
        prefix = f"{RECORDED_PREFIX}{instrument}_"
        deadline = time.monotonic() + RECORD_LAND_SECONDS
        while not stop.is_set():
            for deck, held in enumerate(self.bridge.call("state")["decks"]):
                for where in ("playing", "queued"):
                    disc = held and held.get(where)
                    if disc and disc.startswith(prefix) and disc not in before:
                        with self._lock:
                            self._recorded.append(
                                {"disc": disc, "instrument": instrument, "deck": deck})
                            self._perform_status.update(disc=disc, deck=deck)
                            if name:
                                self._named[name] = disc
                            if expected is not None and deck != expected:
                                self._status["error"] = (
                                    f"{name or disc} landed on deck {deck + 1}, not the deck "
                                    f"{expected + 1} the plan expects")
                        return
            if time.monotonic() > deadline:
                raise GameError(f"the disc recorded on {instrument} did not reach a deck in "
                                f"{RECORD_LAND_SECONDS} seconds")
            stop.wait(POLL_SECONDS)

    def _let_go(self, slot, pad, held, begun) -> None:
        # Let go and unload even when stopped early or after an error, so the instrument
        # never sounds on by itself
        try:
            if held:
                self._send([("perform_press", {**slot, **pad, "on": False})])
            if begun:
                self._send([("perform_end", slot)])
        except GameError as e:
            with self._lock:
                self._perform_status["error"] = self._perform_status["error"] or str(e)
        with self._lock:
            self._perform_status["on"] = False

    def _until(self, target, stop) -> bool:
        """Wait until a command sent now would reach FUSER just after clock beat `target`,
        as _send_at does, but give up when `stop` is set. Returns whether it waited it out."""
        while not stop.is_set():
            beat, per_second, trip = self._clock()
            wait = _wait(target, beat, per_second, trip)
            if wait < SETTLE_SECONDS:
                return not stop.wait(max(wait, 0))
            stop.wait(POLL_SECONDS)
        return False

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
        self._sync_beat = clock.get("sync_beat") or 0
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
        self._send_at(commands, _next_downbeat(beat, per_second, trip), beat, per_second, trip)

    def riser(self, mode: int) -> None:
        """Play a riser from the next downbeat. Sent just after it, FUSER starts the riser on
        that very downbeat and grades it well: sent earlier it starts on the next one, and
        tallies a Bad. Keep-alive stands off until it ends, from now and not from the send:
        the wait for the downbeat can take a bar, and a drop in it changes what comes back."""
        with self._lock:
            self._riser_ends = time.monotonic() + RISER_HOLD_SECONDS
        self.on_downbeat([("riser", {"mode": mode})])

    def on_pickup(self, commands, pickups: list[float], loop_beats: float) -> float:
        """Send `commands`, a drop, to reach FUSER on the next of its disc's pickups, the
        beats in its loop where a drop can come in ahead of the bar. Every disc's loop runs
        from FUSER's sync beat, so a pickup p falls on each clock beat that is p past it,
        wrapped at the loop's length. Returns the clock beat it was aimed at."""
        beat, per_second, trip = self._clock()
        target = self._next_pickup(beat, per_second, trip, pickups, loop_beats)
        self._send_at(commands, target, beat, per_second, trip, PICKUP_ARRIVE_SECONDS)
        return target

    def _next_pickup(self, beat, per_second, trip, pickups, loop_beats) -> float:
        reach = beat + trip * per_second + PICKUP_LEAD_BEATS
        place = (reach - self._sync_beat) % loop_beats
        return reach + min((p - place) % loop_beats for p in pickups)

    def _mix_pickup(self, commands, pickups, loop_beats, stop) -> None:
        # Its own thread: a pickup can be many bars off, and the mix's other steps do not wait
        try:
            beat, per_second, trip = self._clock()
            target = self._next_pickup(beat, per_second, trip, pickups, loop_beats)
            if self._until(target, stop):
                self._send(commands)
        except GameError as e:
            self._update(error=str(e))

    def _play_bar(self, commands, stop) -> None:
        """One bar of a mix: what FUSER can take goes out in one request, and the rest
        starts beside the bar clock."""
        batch = []
        for op, args in commands:
            if op in JOBS:
                continue
            if op == "drop":
                with self._lock:
                    real = self._named.get(args["name"], args["name"])
                if args["name"] in self._awaited and real == args["name"]:
                    # Its recording failed, which get_state reports. The mix goes on without it
                    self._update(error=f"{real} was not recorded, so it was not dropped")
                    continue
                args = {**args, "name": real}
            batch.append((op, args))
        self._send(batch)
        for op, args in commands:
            if op == "fade":
                self.fade(args["deck"], args["start"], args["to"], args["beats"])
            elif op == "pickup":
                threading.Thread(target=self._mix_pickup, daemon=True, args=(
                    args["commands"], args["pickups"], args["loop_beats"], stop)).start()
            elif op in ("perform", "record"):
                when = self._bar_beat(args["start_bar"])
                if op == "perform":
                    self.perform(args["instrument"], args["bars"], when)
                else:
                    self._awaited.add(args["name"])
                    self.record(args["instrument"], args["queue"], when, args["name"], args["deck"])
                self._mix_performs = True

    def _bar_beat(self, bar):
        # Asked as the instrument starts and not now: bar 0 can still move with the clock
        def when():
            with self._lock:
                return self._status["started_beat"] + bar * BEATS_PER_BAR
        return when

    def _reset_decks(self) -> None:
        """Put back what an earlier mix or tool left on the decks, since the plan was checked
        with every deck at full and heard."""
        with self._lock:
            muted, soloed, faded = set(self._muted), set(self._soloed), set(self._faded)
            self._muted.clear()
            self._soloed.clear()
        self.stop_fades()
        for deck in muted:
            self.bridge.call("mute", deck=deck, on=False)
        for deck in soloed:
            self.bridge.call("solo", deck=deck, on=False)
        for deck in faded:
            self.bridge.call("gain", deck=deck, gain=1)

    def _send_at(self, commands, target, beat, per_second, trip,
                 arrive=None) -> None:
        """Wait until `commands` sent now would reach FUSER `arrive` seconds after clock
        beat `target`, and send them."""
        while (wait := _wait(target, beat, per_second, trip, arrive)) >= SETTLE_SECONDS:
            time.sleep(POLL_SECONDS)
            beat, per_second, trip = self._clock()
        if wait > 0:
            time.sleep(wait)
        self._send(commands)

    def _run(self, schedule, end_bar, opening_decks, stop) -> None:
        try:
            self._reset_decks()
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
            else:
                # Bar 0 waits for a downbeat like any other, so it lands in phase with what
                # is playing: the first one a command sent now can still reach
                start = _next_downbeat(beat, per_second, trip)
            self._update(bar=0, started_beat=start)
            if not playing and pending and pending[0][0] == 0:
                self._play_bar(pending.pop(0)[1], stop)

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
                        self._play_bar(pending.pop(0)[1], stop)
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
        with self._lock:
            unmute = []
            for op, args in commands:
                if op == "drop" and not args.get("queue") and args["deck"] in self._muted \
                        and ("mute", {"deck": args["deck"], "on": False}) not in commands:
                    unmute.append(("mute", {"deck": args["deck"], "on": False}))
                if op == "drop" and not args.get("queue") and args["deck"] in self._faded:
                    unmute.append(("gain", {"deck": args["deck"], "gain": 1}))
                    self._faded.discard(args["deck"])
            commands = unmute + list(commands)
            for op, args in commands:
                if op == "mute":
                    (self._muted.add if args.get("on") else self._muted.discard)(args["deck"])
                elif op == "solo":
                    (self._soloed.add if args.get("on") else self._soloed.discard)(args["deck"])
                elif op == "riser":
                    self._riser_ends = time.monotonic() + RISER_HOLD_SECONDS
        self.bridge.call("batch", commands=[{"op": op, "args": args} for op, args in commands])
        if self.on_send:
            self.on_send(commands)
        now = time.monotonic()
        with self._lock:
            self._actions = [t for t in self._actions if now - t < 60] + [now] * len(commands)
            for op, args in commands:
                if op == "drop" and not args.get("queue"):
                    self._dropped = (self._dropped + [args["name"]])[-REMEMBERED_DISCS:]
                    self._played[args["name"]] = self._played.get(args["name"], 0) + 1

    def _hear(self, decks: list) -> None:
        with self._lock:
            self._heard.add(_mix(decks))

    def _keep(self, pool, stop, change_count=False, bars=None) -> None:
        turn, missed = 0, 0
        try:
            while not stop.is_set():
                try:
                    turn = self._keep_turn(pool, turn, stop, change_count, bars)
                    missed = 0
                except GameError as e:
                    # FUSER now and then leaves one command unanswered and takes the next,
                    # and a keep-alive that stopped at the first would leave the meter to
                    # drain until the agent looked
                    missed += 1
                    if missed >= KEEP_ALIVE_MISSES:
                        raise
                    with self._lock:
                        self._keep_status["missed"] = str(e)
                    stop.wait(POLL_SECONDS)
        except GameError as e:
            with self._lock:
                self._keep_status["error"] = str(e)
        finally:
            with self._lock:
                self._keep_status["on"] = False

    def _keep_turn(self, pool, turn, stop, change_count=False, bars=None) -> int:
        """One look at the clock, and a drop or an eject if one is due. Returns the turn to
        start the next pick from."""
        if self._thread and self._thread.is_alive():
            stop.wait(POLL_SECONDS)  # a mix keeps the crowd busy enough
            return turn
        beat, per_second, trip = self._clock()
        downbeat = _next_downbeat(beat, per_second, trip)
        wait = _wait(downbeat, beat, per_second, trip)
        if wait >= SETTLE_SECONDS:
            stop.wait(POLL_SECONDS)
            return turn
        lands = time.monotonic() + max(wait, 0)
        with self._lock:
            last = self._actions[-1] if self._actions else None
            busy = sum(1 for t in self._actions if lands - t < BUSY_BEATS / per_second)
            remembered = list(self._dropped)
            hold = self._request_hold
            riser_ends = self._riser_ends
        if lands < riser_ends:
            stop.wait(max(wait, 0) + POLL_SECONDS)
            return turn
        if hold and lands < hold[0] + hold[1] * BEATS_PER_BAR / per_second:
            with self._lock:
                self._keep_status["waiting"] = "a crowd request is open"
            stop.wait(max(wait, 0) + POLL_SECONDS)
            return turn
        with self._lock:
            if self._keep_status.get("waiting") == "a crowd request is open":
                self._keep_status["waiting"] = None
        # Half a beat short of the bars, so jitter in the clock never puts it off
        # by a whole bar
        idle = ((KEEP_ALIVE_BARS if bars is None else bars) * BEATS_PER_BAR - 0.5) / per_second
        pick = None
        if (last is None or lands - last >= idle) and (bars is not None or busy < BUSY_ACTIONS):
            state = self.bridge.call("state")
            decks = [d.get("playing") if d else None for d in state["decks"]]
            self._hear(decks)
            # Freshness pays most for a mix not heard yet, so of the discs the crowd would
            # take, the one that makes a new mix wins, then the one dropped least, then the
            # next in turn. A few discs cycled over two decks soon run out of new mixes
            best = None
            own = sorted({deck for _, deck in pool})
            empty = [deck for deck in own if not decks[deck]]
            if change_count and not empty and len(own) > 1:
                # Every pool deck is full: take the one playing longest off
                with self._lock:
                    deck = min(own, key=lambda d: self._deck_since.get(d, 0))
                best = ((), ("eject", None, deck), turn)
            for i in range(len(pool)):
                disc, deck = pool[(turn + i) % len(pool)]
                if best and best[1][0] == "eject":
                    break
                if disc in decks or disc in remembered:
                    continue
                if change_count and empty and deck not in empty:
                    continue  # an empty deck is filled first, which changes the count
                mix = list(decks)
                mix[deck] = disc
                with self._lock:
                    rank = (_mix(mix) in self._heard, self._played.get(disc, 0), i)
                if best is None or rank < best[0]:
                    best = (rank, ("drop", disc, deck), (turn + i + 1) % len(pool))
            if best:
                _, pick, turn = best
            with self._lock:
                self._keep_status["waiting"] = None if pick else \
                    "every disc is on a deck or one the crowd remembers"
        if pick is None:
            stop.wait(max(wait, 0) + POLL_SECONDS)  # past this downbeat
            return turn
        # The state took a round trip, so the wait is measured again
        beat, per_second, trip = self._clock()
        wait = _wait(downbeat, beat, per_second, trip)
        if wait > 0 and stop.wait(wait):
            return turn
        kind, disc, deck = pick
        if kind == "eject":
            self._send([("eject", {"deck": deck})])
        else:
            self._send([("drop", {"name": disc, "deck": deck, "queue": False})])
        mix = list(decks)
        mix[deck] = disc
        self._hear(mix)
        with self._lock:
            self._deck_since[deck] = time.monotonic()
            self._keep_status["dropped" if kind == "drop" else "ejected"] = \
                self._keep_status.get("dropped" if kind == "drop" else "ejected", 0) + 1
            self._keep_status["last"] = {"disc": disc, "deck": deck} if kind == "drop" \
                else {"ejected": True, "deck": deck}
        return turn

def _discs_on_decks(state: dict) -> set[str]:
    return {held[where] for held in state["decks"] if held
            for where in ("playing", "queued") if held.get(where)}


def _mix(decks) -> frozenset:
    """A mix as freshness tells mixes apart, by its discs, whichever decks they are on."""
    return frozenset(d for d in decks if d)


def _next_downbeat(beat: float, per_second: float, trip: float) -> int:
    """The first downbeat a command sent now can still reach."""
    return math.floor((beat + trip * per_second) / BEATS_PER_BAR + 1) * BEATS_PER_BAR


def _wait(downbeat: float, beat: float, per_second: float, trip: float,
          arrive: float | None = None) -> float:
    """How long to wait before sending, for a command to arrive on time at `downbeat`.
    Below zero it is late, after a stall, and goes out at once."""
    return (downbeat - beat) / per_second + (ARRIVE_SECONDS if arrive is None else arrive) - trip


def _unchanged(command, state: dict, held: list) -> bool:
    op, args = command
    if op == "drop":
        return not args.get("queue") and held[args["deck"]] == args["name"]
    if op == "set_key":
        return (state.get("key"), state.get("mode")) == (args["key"], args["mode"])
    if op == "set_tempo":
        return state.get("bpm") == args["bpm"]
    return False
