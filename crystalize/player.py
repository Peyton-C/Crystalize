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
        # When the set was last read and how many measures its open crowd requests had left
        self._request_hold: tuple[float, int] | None = None

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

    def on_pickup(self, commands, pickups: list[float], loop_beats: float) -> float:
        """Send `commands`, a drop, to reach FUSER on the next of its disc's pickups, the
        beats in its loop where a drop can come in ahead of the bar. Every disc's loop runs
        from FUSER's sync beat, so a pickup p falls on each clock beat that is p past it,
        wrapped at the loop's length. Returns the clock beat it was aimed at."""
        beat, per_second, trip = self._clock()
        reach = beat + trip * per_second + PICKUP_LEAD_BEATS
        place = (reach - self._sync_beat) % loop_beats
        ahead = min((p - place) % loop_beats for p in pickups)
        target = reach + ahead
        self._send_at(commands, target, beat, per_second, trip, PICKUP_ARRIVE_SECONDS)
        return target

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
        with self._lock:
            unmute = []
            for op, args in commands:
                if op == "drop" and not args.get("queue") and args["deck"] in self._muted \
                        and ("mute", {"deck": args["deck"], "on": False}) not in commands:
                    unmute.append(("mute", {"deck": args["deck"], "on": False}))
            commands = unmute + list(commands)
            for op, args in commands:
                if op == "mute":
                    (self._muted.add if args.get("on") else self._muted.discard)(args["deck"])
        self.bridge.call("batch", commands=[{"op": op, "args": args} for op, args in commands])
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
        return held[args["deck"]] == args["name"]
    if op == "set_key":
        return (state.get("key"), state.get("mode")) == (args["key"], args["mode"])
    if op == "set_tempo":
        return state.get("bpm") == args["bpm"]
    return False
