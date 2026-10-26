"""A mix is a plan: the session's opening key and tempo, then steps on a bar clock. This
module checks a plan, works out what each deck plays bar by bar, and finds the trouble
an agent cannot hear, the way pink diamond checks a set before it is rendered.

Bars count from 0, the bar the first discs land on. Decks are 1 to 4.
"""
from __future__ import annotations

from collections import Counter

from . import music

ACTIONS = ("drop", "eject", "key", "tempo", "mute", "unmute")
DECK_ACTIONS = ("drop", "eject", "mute", "unmute")

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
        self.discs, self.tags = discs, tags or {}
        errors = []
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

        last = max((s["bar"] for s in self.steps), default=0)
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
        if do == "drop":
            name = raw.get("disc")
            disc = self.discs.get(name)
            if disc is None:
                errors.append(f"{where}: no disc named {name}, search_songs lists them")
                return None
            if disc["owned"] is False:
                errors.append(f"{where}: {name} is DLC you do not own and plays silence")
                return None
            step["disc"] = name
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
        return step

    # -------------------------------------------------------------- simulation

    def _simulate(self, errors):
        """Walk the steps bar by bar, keeping each deck's disc and mute, and cut the mix
        into segments where nothing changes."""
        decks = [{"disc": None, "muted": False} for _ in range(4)]
        key, mode, bpm = self.key, self.mode, self.bpm
        self.segments = []
        self.key_changes, self.tempo_changes = [], []
        self.dropped = Counter()

        bars = sorted({s["bar"] for s in self.steps} | {0})
        for i, bar in enumerate(bars):
            touched = set()
            for step in (s for s in self.steps if s["bar"] == bar):
                do = step["do"]
                if do in DECK_ACTIONS:
                    d = step["deck"]
                    if d in touched and do in ("drop", "eject"):
                        errors.append(f"bar {bar}: deck {d} is dropped on or ejected twice")
                    deck = decks[d - 1]
                    if do == "drop":
                        deck["disc"], deck["muted"] = step["disc"], False
                        self.dropped[step["disc"]] += 1
                        touched.add(d)
                    elif deck["disc"] is None:
                        errors.append(f"bar {bar}: deck {d} is empty, so it cannot {do}")
                    elif do == "eject":
                        deck["disc"], deck["muted"] = None, False
                        touched.add(d)
                    else:
                        deck["muted"] = do == "mute"
                elif do == "key":
                    if (step["key"], step["mode"]) != (key, mode):
                        self.key_changes.append({
                            "bar": bar, "from": music.key_name(key, mode),
                            "to": music.key_name(step["key"], step["mode"]),
                            "relation": music.key_relation((key, mode), (step["key"], step["mode"])),
                        })
                    key, mode = step["key"], step["mode"]
                else:
                    if step["bpm"] != bpm:
                        self.tempo_changes.append({"bar": bar, "from": bpm, "to": step["bpm"]})
                    bpm = step["bpm"]
            until = bars[i + 1] if i + 1 < len(bars) else self.end
            if until > bar:
                self.segments.append({"bars": [bar, until], "key": key, "mode": mode, "bpm": bpm,
                                      "decks": [dict(d) for d in decks]})

    # ------------------------------------------------------------------- report

    def _deck_line(self, deck, key, bpm):
        if deck["disc"] is None:
            return None
        disc = self.discs[deck["disc"]]
        notes = []
        shift = music.pitch_shift(disc.get("transposes"), key)
        if shift and disc["type"] != "beat":
            notes.append(f"{shift:+d} st")
        stretch = music.stretch_percent(disc.get("bpm"), bpm)
        if stretch is not None and abs(stretch) >= 3:
            notes.append(f"{stretch:+.0f}%")
        line = f"{disc['song']} {disc['type']}" + (f" ({', '.join(notes)})" if notes else "")
        return ("muted " if deck["muted"] else "") + line

    def _segment_checks(self, seg) -> list[str]:
        playing = [(i + 1, self.discs[d["disc"]]) for i, d in enumerate(seg["decks"])
                   if d["disc"] and not d["muted"]]
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
        if len(playing) >= 3 and len(songs) == 1:
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
                    for d in seg["decks"] if d["disc"] and not d["muted"]]
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
        change_bars = sorted({s["bar"] for s in self.steps} | {self.end})
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

        songs = {self.discs[s["disc"]]["song_id"] for s in self.steps if s["do"] == "drop"}
        return {"bars": self.end, "length": _clock(seconds), "songs": len(songs),
                "key_changes": self.key_changes, "segments": segments, "checks": checks}

    # ---------------------------------------------------------------- playback

    def schedule(self) -> list[tuple[int, list[tuple[str, dict]]]]:
        """The bridge commands for each bar, decks counted from 0 as FUSER counts them.
        Drops go first, so the opening discs are on the decks before the key and tempo
        are set: FUSER sets both from the first disc it is given on an empty desk."""
        by_bar: dict[int, list] = {}
        order = {"drop": 0, "eject": 1, "mute": 2, "unmute": 2, "key": 3, "tempo": 4}
        opening = [{"bar": 0, "do": "key", "key": self.key, "mode": self.mode},
                   {"bar": 0, "do": "tempo", "bpm": self.bpm}]
        for step in sorted(opening + self.steps, key=lambda s: (s["bar"], order[s["do"]])):
            by_bar.setdefault(step["bar"], []).append(_command(step))
        return sorted(by_bar.items())


def _command(step) -> tuple[str, dict]:
    do = step["do"]
    if do == "drop":
        return "drop", {"name": step["disc"], "deck": step["deck"] - 1, "queue": False}
    if do == "eject":
        return "eject", {"deck": step["deck"] - 1}
    if do in ("mute", "unmute"):
        return "mute", {"deck": step["deck"] - 1, "on": do == "mute"}
    if do == "key":
        return "set_key", {"key": step["key"], "mode": step["mode"]}
    return "set_tempo", {"bpm": step["bpm"]}


def _join(numbers) -> str:
    return " and ".join(str(n) for n in numbers) if len(numbers) < 3 else \
        ", ".join(str(n) for n in numbers[:-1]) + f" and {numbers[-1]}"


def _clock(seconds: float) -> str:
    seconds = int(round(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"
