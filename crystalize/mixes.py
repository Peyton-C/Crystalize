"""A mix is a plan: the session's opening key and tempo, then steps on a bar clock. This
module checks a plan, works out what each deck plays bar by bar, and finds the trouble
an agent cannot hear, the way pink diamond checks a set before it is rendered.

Bars count from 0, the bar the first discs land on. Decks are 1 to 4.

Some steps take time in the game: a riser, a fade, a performance, a recording. The walk
through the plan keeps what each does to the decks on the bars it does it, and the player
runs the long ones beside the bar clock.
"""
from __future__ import annotations

import math
from collections import Counter

from . import music
from .player import BEATS_PER_BAR, RECORD_BEATS

ACTIONS = ("drop", "eject", "key", "tempo", "mute", "unmute", "solo", "unsolo", "cue",
           "drop_cue", "swap", "fx", "fade", "riser", "snapshot", "perform", "record")
DECK_ACTIONS = ("drop", "eject", "mute", "unmute", "solo", "unsolo", "cue", "swap", "fade",
                "record")
DISC_ACTIONS = ("drop", "cue", "swap")

# The order a bar's steps reach FUSER. Drops go first, so the opening discs are on the decks
# before the key and tempo are set: FUSER sets both from the first disc it is given on an
# empty desk. A cue goes after drop_cue, which would otherwise land a disc still loading,
# and a riser after the cues, since it decides what comes back from what is cued as it starts
ORDER = {"drop": 0, "eject": 1, "mute": 2, "unmute": 2, "solo": 2, "unsolo": 2, "key": 3,
         "tempo": 4, "fx": 5, "drop_cue": 6, "swap": 6, "cue": 7, "riser": 8, "snapshot": 9,
         "fade": 9, "perform": 9, "record": 9}

# Which deck FUSER puts a recorded disc on, by its type. It chooses, and a plan has to say
# which it expects, since the mod cannot tell an instrument's type before it records
DECK_TYPES = ("beat", "bass", "loop", "lead")

# A riser takes 4 bars from the downbeat it starts on. The decks empty for the last of them
# and the cued discs land on the bar after
RISER_BARS = 4
RISER_EMPTY_BAR = 3

RECORD_BARS = RECORD_BEATS // BEATS_PER_BAR

# The longest performance in one call or step. Perform goals have asked for 4 and 8
PERFORM_BARS = 16
FADE_MAX_BEATS = 64
FADE_BEATS = 4

# How far ahead of its bar an instrument starts loading. One can take 5 seconds, the most
# the player waits, when it has not been loaded before, and 2 bars is 4 seconds at 120 BPM.
# One loading late only comes in a bar or so late
INSTRUMENT_LEAD_BARS = 2

# An instrument stays loaded into the bar after a performance or recording, past the lag in
# FUSER's count, and the player has one slot for both. The next one's lead starts after it
INSTRUMENT_TAIL_BARS = 1

# A disc waiting longer than this for its next pickup may land after the part it was meant for
PICKUP_WAIT_BARS = 4

# Bars past the last step a mix runs for when the plan does not say
DEFAULT_TAIL = 16

# A mix in which nothing changes for longer than this goes flat
STILL_BARS = 32

# Pitch shifts past these start to sound processed, (up, down) in semitones. Up thins a
# part sooner than down darkens it: SOPHIE's LEMONADE loop, already bright, read as a
# different song's violin at +4 in C minor. A bassline starts low enough to take +5
# unnoticed, as four did in one 15-minute mix, and +3 vocals passed there too
SHIFT_LIMITS = {"bass": (5, 5), "vocal": (3, 4), "other": (3, 5)}

TEMPO_JUMP_PERCENT = 8
ENERGY_JUMP = 3


class Plan:
    """A checked plan: its steps in order, and the deck states between them."""

    def __init__(self, plan: dict, discs: dict[str, dict], bpm_range=None, tags=None):
        self.discs, self.tags = dict(discs), tags or {}
        errors = []
        self._name_recordings(plan.get("steps") or [], errors)
        self.key, self.mode = self._key(plan.get("key"), "the opening key", errors)
        self.bpm = self._bpm(plan.get("bpm"), "the opening tempo", bpm_range, errors)
        self.steps = []
        for i, raw in enumerate(plan.get("steps") or []):
            step = self._step(raw, i + 1, bpm_range, errors)
            if step:
                self.steps.append(step)
        if not self.steps and not errors:
            errors.append("the plan has no steps")
        self.steps.sort(key=lambda s: s["bar"])  # stable, so a bar keeps the order given

        last = max((_ends(s) for s in self.steps), default=0)
        self.end = plan.get("end_bar") or last + DEFAULT_TAIL
        if not isinstance(self.end, int) or self.end <= last:
            errors.append(f"end_bar must be a bar after the last step, {last}")
        # The steps that parsed are still walked, so an empty deck is reported alongside a
        # misspelt disc rather than after it is fixed
        if self.key is not None and self.bpm is not None and self.steps:
            self._simulate(errors)
        if errors:
            raise ValueError("the plan has problems:\n" + "\n".join(errors))

    # ----------------------------------------------------------------- parsing

    def _name_recordings(self, steps, errors):
        """Add the discs the plan will record, so a step can drop one by the name it gave
        it. Read first, since FUSER names the disc only once it is made."""
        self.recordings = {}
        for raw in steps:
            if not isinstance(raw, dict) or raw.get("do") != "record" or raw.get("as") is None:
                continue
            name, deck, bar = raw["as"], raw.get("deck"), raw.get("bar")
            if not isinstance(name, str) or name in self.discs:
                errors.append(f"record at bar {bar}: {name} is already the name of a disc")
            elif isinstance(deck, int) and 1 <= deck <= 4 and isinstance(bar, int):
                self.discs[name] = recorded_disc(name, raw.get("instrument"), deck - 1)
                # Its real name is read after it lands, which takes until the next bar
                self.recordings[name] = bar + RECORD_BARS + 1

    @staticmethod
    def _key(text, where, errors):
        if text is None:
            errors.append(f"{where} is missing, such as \"Abm\"")
            return None, None
        try:
            return music.parse_key(str(text))
        except ValueError as e:
            errors.append(f"{where}: {e}")
            return None, None

    @staticmethod
    def _bpm(value, where, bpm_range, errors):
        if not isinstance(value, int) or isinstance(value, bool):
            errors.append(f"{where} must be a whole BPM")
            return None
        if bpm_range and not bpm_range[0] <= value <= bpm_range[1]:
            errors.append(f"{where}, {value}, is outside the {bpm_range[0]} to {bpm_range[1]} "
                          f"BPM FUSER allows")
        return value

    def _step(self, raw, n, bpm_range, errors):
        if not isinstance(raw, dict):
            errors.append(f"step {n} is not an object")
            return None
        bar, do = raw.get("bar"), raw.get("do")
        where = f"step {n} (bar {bar}, {do})"
        if not isinstance(bar, int) or isinstance(bar, bool) or bar < 0:
            errors.append(f"step {n}: bar must be a whole number from 0")
            return None
        if do not in ACTIONS:
            errors.append(f"{where}: do must be one of {', '.join(ACTIONS)}")
            return None
        step = {"bar": bar, "do": do}
        if do in DECK_ACTIONS:
            deck = raw.get("deck")
            if not isinstance(deck, int) or isinstance(deck, bool) or not 1 <= deck <= 4:
                errors.append(f"{where}: deck must be 1 to 4")
                return None
            step["deck"] = deck
        if do in DISC_ACTIONS:
            name = raw.get("disc")
            disc = self.discs.get(name)
            if disc is None:
                errors.append(f"{where}: no disc named {name}, search_songs lists them")
                return None
            if disc["owned"] is False:
                errors.append(f"{where}: {name} is DLC you do not own and plays silence")
                return None
            step["disc"] = name
            if raw.get("on_pickup") and do != "cue":
                if not disc.get("pickups") or not disc.get("loop_beats"):
                    errors.append(f"{where}: {name} has no pickups to land on")
                    return None
                step["on_pickup"] = True
        elif do == "key":
            key, mode = self._key(raw.get("key"), where, errors)
            if key is None:
                return None
            step["key"], step["mode"] = key, mode
        elif do == "tempo":
            bpm = self._bpm(raw.get("bpm"), where, bpm_range, errors)
            if bpm is None:
                return None
            step["bpm"] = bpm
        elif do == "fx":
            deck = raw.get("deck")
            if deck is not None and (not isinstance(deck, int) or isinstance(deck, bool)
                                     or not 1 <= deck <= 4):
                errors.append(f"{where}: deck must be 1 to 4, or left out for all four")
                return None
            step.update(deck=deck, fx=raw.get("fx"), on=raw.get("on") is not False)
        elif do == "fade":
            to, beats = raw.get("to", 0), raw.get("beats", FADE_BEATS)
            if isinstance(to, bool) or not isinstance(to, (int, float)) or not 0 <= to <= 1:
                errors.append(f"{where}: to is from 0 to 1")
                return None
            if not isinstance(beats, int) or isinstance(beats, bool) \
                    or not 1 <= beats <= FADE_MAX_BEATS:
                errors.append(f"{where}: beats is 1 to {FADE_MAX_BEATS}")
                return None
            step.update(to=to, beats=beats)
        elif do == "riser":
            mode = raw.get("mode", "smart")
            if mode not in music.RISER_MODES:
                errors.append(f"{where}: mode is one of {', '.join(music.RISER_MODES)}")
                return None
            step["mode"] = mode
        elif do == "perform":
            bars = raw.get("bars")
            if not isinstance(bars, int) or isinstance(bars, bool) or not 1 <= bars <= PERFORM_BARS:
                errors.append(f"{where}: bars is 1 to {PERFORM_BARS}")
                return None
            step.update(bars=bars, instrument=raw.get("instrument"))
        elif do == "record":
            step.update(instrument=raw.get("instrument"), queue=bool(raw.get("queue")),
                        name=raw.get("as"))
        return step

    # -------------------------------------------------------------- simulation

    def _simulate(self, errors):
        """Walk the steps bar by bar, keeping what each deck holds and how it sounds, and cut
        the mix into segments where nothing changes."""
        decks = [{"disc": None, "muted": False, "solo": False, "gain": 1, "queued": None,
                  "fx": None, "fading": 0} for _ in range(4)]
        master_fx = None
        slots = {}  # the effect each slot holds, by deck, None being the master's
        key, mode, bpm = self.key, self.mode, self.bpm
        self.segments = []
        self.key_changes, self.tempo_changes = [], []
        self.dropped = Counter()
        self.notes = []
        later: dict[int, list] = {}  # what a step started does on a later bar
        riser_from, riser_until, riser_state = 0, 0, None

        def land(deck, name):
            deck["disc"] = name
            self.dropped[name] += 1

        def riser_empties(at, come_back, step):
            nonlocal riser_state
            riser_state = "empty"
            # With nothing cued as it started, FUSER cues a copy of each disc playing
            landing = [d["queued"] or (d["disc"] if come_back else None) for d in decks]
            for d in decks:
                d["disc"], d["queued"] = None, None
            later.setdefault(at + 1, []).append(lambda: riser_lands(at + 1, landing, come_back, step))

        def riser_lands(at, landing, came_back, step):
            nonlocal key, mode, bpm, riser_state
            riser_state = None
            for d, name in zip(decks, landing):
                if name:
                    d["disc"] = name
                    if not came_back:
                        self.dropped[name] += 1
            if step["mode"] == "random":
                self.notes.append(f"bar {at}: a random riser picks its own key and tempo, so "
                                  f"the checks from here on are only as true as the plan's")
            elif step["mode"] == "smart" and not came_back:
                cued = [self.discs[name] for name in landing if name]
                # The cued lead led in the one mix tried with several kinds of disc cued
                source = next((c for c in cued if c["type"] == "lead"), cued[0] if cued else None)
                if source and source.get("key") is not None and source.get("bpm"):
                    key, mode, bpm = source["key"], source.get("mode") or 0, source["bpm"]
                    self.notes.append(f"bar {at}: a smart riser moves the session to "
                                      f"{music.key_name(key, mode)} and {bpm} BPM, from the "
                                      f"cued {source['song']} {source['type']}")

        for bar in {b for s in self.steps for b in _later_bars(s)}:
            later.setdefault(bar, [])
        bars = sorted({s["bar"] for s in self.steps} | {0} | set(later))
        i = 0
        while i < len(bars):
            bar = bars[i]
            for event in later.pop(bar, []):
                event()
            if bar >= riser_until:
                riser_state = None
            touched = set()
            steps = sorted((s for s in self.steps if s["bar"] == bar), key=lambda s: ORDER[s["do"]])
            for step in steps:
                do = step["do"]
                if riser_from < bar < riser_until and do not in ("snapshot", "fx"):
                    self.notes.append(f"bar {bar}: {do} lands inside the riser from bar "
                                      f"{riser_from}, which empties the decks on bar "
                                      f"{riser_from + RISER_EMPTY_BAR}")
                # A swap cues its disc a bar ahead, so it needs it a bar sooner
                if do in DISC_ACTIONS and bar - (do == "swap") < self.recordings.get(step["disc"], 0):
                    errors.append(f"bar {bar}: {step['disc']} is not recorded until bar "
                                  f"{self.recordings[step['disc']]}")
                    continue
                if do in DECK_ACTIONS:
                    d = step["deck"]
                    if d in touched and do in ("drop", "eject", "swap"):
                        errors.append(f"bar {bar}: deck {d} is dropped on or ejected twice")
                    deck = decks[d - 1]
                    if do in ("drop", "swap", "fade") and bar < deck["fading"]:
                        errors.append(f"bar {bar}: deck {d} is fading until bar {deck['fading']}")
                        continue
                    if do == "drop":
                        if step.get("on_pickup"):
                            # It goes out through drop_queue, which lands every cue with it,
                            # and leaves the deck's mute and volume as they are
                            if any(other["queued"] for other in decks):
                                errors.append(f"bar {bar}: a pickup drop would land the discs "
                                              f"already cued with it")
                            self._note_pickup_wait(bar, step["disc"])
                        else:
                            deck["muted"], deck["gain"] = False, 1
                        land(deck, step["disc"])
                        touched.add(d)
                    elif do == "cue":
                        deck["queued"] = step["disc"]
                    elif do == "record":
                        later.setdefault(bar + RECORD_BARS, []).append(
                            lambda deck=deck, step=step, at=bar + RECORD_BARS: self._recorded(deck, step, decks, at))
                    elif deck["disc"] is None:
                        errors.append(f"bar {bar}: deck {d} is empty, so it cannot {do}")
                    elif do == "eject":
                        deck["disc"], deck["muted"] = None, False
                        touched.add(d)
                    elif do == "swap":
                        if any(s["bar"] == bar and s["do"] == "drop_cue" for s in self.steps):
                            errors.append(f"bar {bar}: a swap needs its disc cued on this deck "
                                          f"until it lands, and drop_cue would land it first")
                        if any(s["do"] == "cue" and s["deck"] == d and bar - 1 <= s["bar"] <= bar
                               for s in self.steps):
                            errors.append(f"bar {bar}: the swap on deck {d} cues its disc on the "
                                          f"bar before, over the cue there")
                        if step.get("on_pickup"):
                            self._note_pickup_wait(bar, step["disc"])
                        # The disc replaced goes back in the cue
                        deck["queued"] = deck["disc"]
                        land(deck, step["disc"])
                        touched.add(d)
                    elif do == "fade":
                        if step["to"] == deck["gain"]:
                            # FUSER counts the fader moving, so this would be no fade at all
                            errors.append(f"bar {bar}: deck {d} is already at volume {step['to']}")
                            continue
                        step["from"] = deck["gain"]
                        ends = bar + math.ceil(step["beats"] / BEATS_PER_BAR)
                        deck["fading"] = ends
                        later.setdefault(ends, []).append(
                            lambda deck=deck, to=step["to"]: deck.update(gain=to))
                    elif do in ("solo", "unsolo"):
                        deck["solo"] = do == "solo"
                    else:
                        deck["muted"] = do == "mute"
                elif do == "drop_cue":
                    cued = [(n, c) for n, c in enumerate(decks, 1) if c["queued"]]
                    if not cued:
                        errors.append(f"bar {bar}: nothing is cued to drop")
                    for n, c in cued:
                        if c["muted"] or c["gain"] == 0:
                            self.notes.append(f"bar {bar}: the cue lands on deck {n}, which is "
                                              f"silent, and FUSER rewards nothing dropped there")
                        land(c, c["queued"])
                        c["queued"] = None
                        touched.add(n)
                elif do == "fx":
                    name = step["fx"] or slots.get(step["deck"])
                    if name is None:
                        where = "the master" if step["deck"] is None else f"deck {step['deck']}"
                        errors.append(f"bar {bar}: {where}'s effect slot is empty, so name one "
                                      f"with fx")
                        continue
                    slots[step["deck"]] = name
                    on = name if step["on"] else None
                    if step["deck"] is None:
                        master_fx = on
                    else:
                        decks[step["deck"] - 1]["fx"] = on
                elif do == "riser":
                    if bar < riser_until:
                        errors.append(f"bar {bar}: the riser from bar {riser_from} is still "
                                      f"playing")
                        continue
                    riser_from, riser_until, riser_state = bar, bar + RISER_BARS, "building"
                    come_back = not any(d["queued"] for d in decks)
                    later.setdefault(bar + RISER_EMPTY_BAR, []).append(
                        lambda at=bar + RISER_EMPTY_BAR, come_back=come_back, step=step:
                        riser_empties(at, come_back, step))
                elif do == "key":
                    if (step["key"], step["mode"]) != (key, mode):
                        self.key_changes.append({
                            "bar": bar, "from": music.key_name(key, mode),
                            "to": music.key_name(step["key"], step["mode"]),
                            "relation": music.key_relation((key, mode), (step["key"], step["mode"])),
                        })
                    key, mode = step["key"], step["mode"]
                elif do == "tempo":
                    if step["bpm"] != bpm:
                        self.tempo_changes.append({"bar": bar, "from": bpm, "to": step["bpm"]})
                    bpm = step["bpm"]
            # A riser's empty bar adds its landing as it goes
            bars = sorted(set(bars) | set(later))
            i = bars.index(bar) + 1
            until = bars[i] if i < len(bars) else self.end
            if until > bar:
                self.segments.append({"bars": [bar, until], "key": key, "mode": mode, "bpm": bpm,
                                      "decks": [dict(d) for d in decks], "master_fx": master_fx,
                                      "riser": riser_state})
        self._check_instruments(errors)

    def _recorded(self, deck, step, decks, at):
        """A recording lands on its bar: the deck FUSER keeps for its type, or that deck's cue."""
        name = step["name"]
        if not any(d["disc"] and self.discs[d["disc"]]["type"] != "beat" for d in decks):
            self.notes.append(f"bar {at}: with nothing pitched playing, the recording moves the "
                              f"session to its instrument's own key")
        if name is None:  # a recording nothing drops by name still plays
            name = f"the recording from bar {step['bar']}"
            self.discs[name] = recorded_disc(name, step["instrument"], step["deck"] - 1)
        if step["queue"]:
            deck["queued"] = name
        else:
            deck["disc"] = name

    def _note_pickup_wait(self, bar, name):
        disc = self.discs[name]
        points, loop = sorted(disc["pickups"]), disc["loop_beats"]
        gap = max(b - a for a, b in zip(points, points[1:] + [points[0] + loop]))
        if gap / BEATS_PER_BAR > PICKUP_WAIT_BARS:
            self.notes.append(f"bar {bar}: {name} waits for its next pickup, up to "
                              f"{math.ceil(gap / BEATS_PER_BAR)} bars")

    def _check_instruments(self, errors):
        """FUSER's instrument slot takes one performance or recording at a time, and starting
        the next while one is still loaded would cut it short."""
        jobs = [s for s in self.steps if s["do"] in ("perform", "record")]
        for a, b in zip(jobs, jobs[1:]):
            free = _ends(a) + INSTRUMENT_TAIL_BARS + INSTRUMENT_LEAD_BARS
            if b["bar"] < free:
                errors.append(f"bar {b['bar']}: the instrument from bar {a['bar']} needs until "
                              f"bar {free} to finish and load the next")

    # ------------------------------------------------------------------- report

    def _deck_line(self, deck, key, bpm):
        if deck["disc"] is None:
            return None
        disc = self.discs[deck["disc"]]
        notes = [deck["fx"]] if deck.get("fx") else []
        shift = music.pitch_shift(disc.get("transposes"), key)
        if shift and disc["type"] != "beat":
            notes.append(f"{shift:+d} st")
        stretch = music.stretch_percent(disc.get("bpm"), bpm)
        if stretch is not None and abs(stretch) >= 3:
            notes.append(f"{stretch:+.0f}%")
        line = f"{disc['song']} {disc['type']}" + (f" ({', '.join(notes)})" if notes else "")
        heard = ("muted " if deck["muted"] else "") + ("soloed " if deck.get("solo") else "")
        if deck.get("gain", 1) < 1:
            heard += f"volume {deck['gain']} "
        return heard + line

    @staticmethod
    def _playing(seg):
        """The decks heard: the soloed ones if any are, else all not muted, and none faded out."""
        held = [(i + 1, d) for i, d in enumerate(seg["decks"]) if d["disc"] and d["gain"] > 0]
        soloed = any(d["solo"] for d in seg["decks"])
        return [(n, d) for n, d in held if (d["solo"] if soloed else not d["muted"])]

    def _segment_checks(self, seg) -> list[str]:
        if seg["riser"] == "empty":
            return []  # the riser's own sound fills this bar
        playing = [(n, self.discs[d["disc"]]) for n, d in self._playing(seg)]
        if not playing:
            return ["silence: nothing is playing"]
        checks = []
        types = Counter(disc["type"] for _, disc in playing)

        def decks_of(test):
            return [n for n, disc in playing if test(disc)]

        vocals = decks_of(music.is_vocal)
        if len(vocals) > 1:
            checks.append(f"two vocals: decks {_join(vocals)}")
        if not types["beat"]:
            checks.append("no beat")
        if not types["bass"]:
            checks.append("no bass")
        if types["bass"] > 1:
            checks.append(f"two basses: decks {_join(decks_of(lambda d: d['type'] == 'bass'))}")
        songs = {disc["song_id"] for _, disc in playing}
        if len(playing) >= 3 and len(songs) == 1 and None not in songs:
            checks.append(f"one song: every deck plays {playing[0][1]['song']}, so this is the "
                          f"original and not a mix")

        for n, disc in playing:
            if disc["type"] != "beat":
                shift = music.pitch_shift(disc.get("transposes"), seg["key"])
                kind = "vocal" if music.is_vocal(disc) else \
                    "bass" if disc["type"] == "bass" else "other"
                up, down = SHIFT_LIMITS[kind]
                if shift is not None and (shift > up or -shift > down):
                    # Said with how much it matters, so a vocal is fixed first: a shifted
                    # loop sounded mostly fine where a vocal one less far did not
                    checks.append(f"pitched far: deck {n} {shift:+d} semitones" + (
                        " on a vocal, which shows it most" if kind == "vocal" else
                        f", mild on a {disc['type']} but better in a key nearer its own"))
                has = disc.get("has_minor" if seg["mode"] == music.MINOR else "has_major")
                if has is False:
                    mode = "minor" if seg["mode"] == music.MINOR else "major"
                    checks.append(f"no {mode} version: deck {n}")
            if music.stretch_too_far(disc.get("bpm"), seg["bpm"]):
                percent = music.stretch_percent(disc["bpm"], seg["bpm"])
                checks.append(f"stretched too far: deck {n} {percent:+.0f}% off its "
                              f"{disc['bpm']} BPM")
        return checks

    def _energy(self, seg):
        energies = [self.tags.get(self.discs[d["disc"]]["song_id"], {}).get("energy")
                    for _, d in self._playing(seg)]
        energies = [e for e in energies if isinstance(e, (int, float))]
        if not energies:
            return None
        energy = round(sum(energies) / len(energies), 1)
        return int(energy) if energy.is_integer() else energy

    def report(self, from_bar: int | None = None, to_bar: int | None = None) -> dict:
        """The timeline and its checks. from_bar and to_bar limit the segments listed;
        the checks over the whole mix are always there."""
        seconds, segments, energies = 0.0, [], []
        for seg in self.segments:
            start, end = seg["bars"]
            energy = self._energy(seg)
            energies.append((start, energy))
            shown = (from_bar is None or end > from_bar) and (to_bar is None or start < to_bar)
            if shown:
                line = {"bars": seg["bars"], "at": _clock(seconds),
                        "key": music.key_name(seg["key"], seg["mode"]), "bpm": seg["bpm"],
                        "decks": [self._deck_line(d, seg["key"], seg["bpm"]) for d in seg["decks"]]}
                if energy is not None:
                    line["energy"] = energy
                if seg["master_fx"]:
                    line["master_fx"] = seg["master_fx"]
                if seg["riser"]:
                    line["riser"] = seg["riser"]
                checks = self._segment_checks(seg)
                if checks:
                    line["checks"] = checks
                segments.append(line)
            seconds += (end - start) * 4 * 60 / seg["bpm"]

        checks = []
        for step in self.steps:
            if step["bar"] % 4:
                checks.append(f"bar {step['bar']}: {step['do']} is off the phrase, which "
                              f"comes in 4s and 8s")
        # A snapshot changes nothing anyone hears
        change_bars = sorted({s["bar"] for s in self.steps if s["do"] != "snapshot"} | {self.end})
        for a, b in zip(change_bars, change_bars[1:]):
            if b - a > STILL_BARS:
                checks.append(f"bars {a} to {b}: nothing changes for {b - a} bars")
        for name, times in self.dropped.items():
            if times > 1:
                checks.append(f"{name} is dropped {times} times")
        for change in self.key_changes:
            if change["relation"] == "clash":
                checks.append(f"bar {change['bar']}: the key change from {change['from']} to "
                              f"{change['to']} clashes")
        for change in self.tempo_changes:
            jump = abs(change["to"] / change["from"] - 1) * 100
            if jump > TEMPO_JUMP_PERCENT:
                checks.append(f"bar {change['bar']}: the tempo jumps {jump:.0f}%, from "
                              f"{change['from']} to {change['to']}")
        known = [(bar, e) for bar, e in energies if e is not None]
        for (_, a), (bar, b) in zip(known, known[1:]):
            if abs(b - a) >= ENERGY_JUMP:
                checks.append(f"bar {bar}: energy jumps from {a} to {b}")

        checks.extend(self.notes)
        songs = {self.discs[s["disc"]]["song_id"] for s in self.steps if s["do"] in DISC_ACTIONS}
        songs.discard(None)  # a recording is no song
        return {"bars": self.end, "length": _clock(seconds), "songs": len(songs),
                "key_changes": self.key_changes, "segments": segments, "checks": checks}

    # ---------------------------------------------------------------- playback

    def schedule(self) -> list[tuple[int, list[tuple[str, dict]]]]:
        """The bridge commands for each bar, decks counted from 0 as FUSER counts them, in
        ORDER. Some are not FUSER's but the player's to run beside the bar clock: fade,
        perform, record and pickup."""
        opening = [{"bar": 0, "do": "key", "key": self.key, "mode": self.mode},
                   {"bar": 0, "do": "tempo", "bpm": self.bpm}]
        timed = [item for step in opening + self.steps for item in _commands(step, self.discs)]
        by_bar: dict[int, list] = {}
        for bar, _, command in sorted(timed, key=lambda t: (t[0], ORDER[t[1]])):
            by_bar.setdefault(bar, []).append(command)
        return sorted(by_bar.items())


def recorded_disc(name, instrument, deck) -> dict:
    """What the checks know of a disc FUSER has not made yet, or made this session: its type,
    from the deck it lands on. A recording is written for the session's key and tempo."""
    return {"name": name, "type": DECK_TYPES[deck], "instrument": instrument,
            "song": f"recorded {instrument or 'instrument'}", "song_id": None, "owned": True}


def _ends(step) -> int:
    """The bar a step's work in the game ends on."""
    do, bar = step["do"], step["bar"]
    if do == "riser":
        return bar + RISER_BARS
    if do == "fade":
        return bar + math.ceil(step["beats"] / BEATS_PER_BAR)
    if do == "perform":
        return bar + step["bars"]
    if do == "record":
        return bar + RECORD_BARS
    return bar


def _later_bars(step) -> list[int]:
    """The bars a step changes what is heard on, besides its own."""
    if step["do"] == "riser":
        return [step["bar"] + RISER_EMPTY_BAR, step["bar"] + RISER_BARS]
    if step["do"] in ("fade", "record"):
        return [_ends(step)]
    return []


def _commands(step, discs) -> list[tuple[int, str, tuple[str, dict]]]:
    """A step's commands, each as (bar, the name it sorts by, command). Most are one on the
    step's own bar."""
    do, bar = step["do"], step["bar"]
    deck = step["deck"] - 1 if step.get("deck") else None

    def pickup(commands):
        disc = discs[step["disc"]]
        return "pickup", {"commands": commands, "pickups": disc["pickups"],
                          "loop_beats": disc["loop_beats"]}

    if do == "drop" and step.get("on_pickup"):
        # Cued first, so the disc is on the deck and loaded when the pickup comes
        return [(bar, "cue", ("drop", {"name": step["disc"], "deck": deck, "queue": True})),
                (bar, "snapshot", pickup([("drop_queue", {})]))]
    if do == "swap":
        # Cued a bar ahead, so the disc is loaded by the time it swaps in
        cue = (bar - 1, "cue", ("drop", {"name": step["disc"], "deck": deck, "queue": True}))
        swap = [("hot_swap", {"deck": deck})]
        return [cue, (bar, "snapshot", pickup(swap)) if step.get("on_pickup")
                else (bar, "swap", swap[0])]
    if do in ("perform", "record"):
        args = {"instrument": step["instrument"], "start_bar": bar}
        args.update({"bars": step["bars"]} if do == "perform" else
                    {"queue": step["queue"], "name": step["name"], "deck": deck})
        return [(max(bar - INSTRUMENT_LEAD_BARS, 0), do, (do, args))]
    return [(bar, do, _command(step, deck))]


def _command(step, deck) -> tuple[str, dict]:
    do = step["do"]
    if do == "drop":
        return "drop", {"name": step["disc"], "deck": deck, "queue": False}
    if do == "cue":
        return "drop", {"name": step["disc"], "deck": deck, "queue": True}
    if do == "drop_cue":
        return "drop_queue", {}
    if do == "eject":
        return "eject", {"deck": deck}
    if do in ("mute", "unmute"):
        return "mute", {"deck": deck, "on": do == "mute"}
    if do in ("solo", "unsolo"):
        return "solo", {"deck": deck, "on": do == "solo"}
    if do == "fx":
        return "fx", {"deck": deck, "name": step["fx"], "on": step["on"]}
    if do == "fade":
        return "fade", {"deck": deck, "start": step["from"], "to": step["to"], "beats": step["beats"]}
    if do == "riser":
        return "riser", {"mode": music.RISER_MODES[step["mode"]]}
    if do == "snapshot":
        return "snapshot", {}
    if do == "key":
        return "set_key", {"key": step["key"], "mode": step["mode"]}
    return "set_tempo", {"bpm": step["bpm"]}


def _join(numbers) -> str:
    return " and ".join(str(n) for n in numbers) if len(numbers) < 3 else \
        ", ".join(str(n) for n in numbers[:-1]) + f" and {numbers[-1]}"


def _clock(seconds: float) -> str:
    seconds = int(round(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"
