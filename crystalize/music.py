"""FUSER's musical vocabulary: keys, disc types, genres, and discs grouped into songs.

The mod reports FUSER's enums as the numbers the game stores, so their names live here
rather than in Lua, where a table would have to be kept in step with this one.
"""
from __future__ import annotations

import re

# EKey. FUSER spells every black key as a flat
KEYS = ("C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B")
SHARPS = {"C#": "Db", "D#": "Eb", "F#": "Gb", "G#": "Ab", "A#": "Bb"}
MAJOR, MINOR = 0, 1  # EKeyMode

# FUSER's MIDI resolution: a disc's SongLengthTicks over this is its loop in beats
TICKS_PER_BEAT = 480

# ECelType. A disc is one of a song's four parts; lead is usually the vocal
DISC_TYPES = ("beat", "bass", "loop", "lead")

# EGenre
GENRES = (None, "Classical", "Country", "Dance", "Hip Hop", "Latin and Caribbean", "Pop",
          "R&B", "Rock")

# EInstrument
INSTRUMENTS = (None, "Guitar", "Bass", "Drums", "Vocal", "Synth", "Sampler", "Horns",
               "Strings", "Female Vocals", "Male Vocals", "Bass Guitar", "Acoustic Guitar",
               "Electric Guitar", "Acoustic Drums", "Electric Drums", "Steel Drums", "Piano",
               "Organ", "Saxophone", "Trumpet", "Didgeridoo", "Fiddle", "Violin", "Dogs")

# ERequestState, a crowd request's
REQUEST_STATES = ("scheduled", "active", "passed", "passed", "failed")
OPEN_REQUESTS = ("scheduled", "active")

# ECrowdMeterHealthState, the crowd meter's band
CROWD_STATES = ("low", "medium", "high", "peak")

# EYearRange
YEAR_RANGES = (None, "1600s", "1700s", "1800s") + tuple(f"{y}s" for y in range(1900, 2030, 10))

# FUSER writes a goal's name with placeholders it fills on screen. The disc types come from
# their short names, the end of every disc's name; the rest the mod cannot read yet
GOAL_PLACEHOLDERS = {"{btType}": "beat (blue)", "{bsType}": "bass (green)",
                     "{lpType}": "loop (yellow)", "{ldType}": "lead (red)"}
DISC_TYPE_WORDS = {"bt": "beat (blue)", "bs": "bass (green)", "lp": "loop (yellow)",
                   "ld": "lead (red)"}
# Not when another type follows, as in "Include only 1 {btType} & 1 {bsType}", which reads
# right as it is
_ONLY_TYPE = re.compile(r"Include only (\d+) \{(bt|bs|lp|ld)Type\}(?! &)")
# A disc type and an instrument, as in {lpGuitar}: a guitar loop
_TYPED_INSTRUMENT = re.compile(r"\{(bt|bs|lp|ld)([A-Z]\w*)\}")

# EInstrument values that sing. A lead disc with no instrument counts too, since most are
VOCALS = {"Vocal", "Female Vocals", "Male Vocals", None}

# How far a song can be stretched off its own tempo before it sounds processed, as
# pink diamond judges it: faster songs take more before the groove drags or rushes
STRETCH_PERCENT, FAST_STRETCH_PERCENT, FAST_BPM = 8, 16, 140

_KEY = re.compile(r"([A-G])([#b]?)\s*(m|min|minor|maj|major)?", re.I)


def key_name(key: int | None, mode: int | None) -> str | None:
    """Ab for A flat major, Abm for minor, as bejeweled writes keys."""
    if key is None or not 0 <= key < len(KEYS):
        return None
    return KEYS[key] + ("m" if mode == MINOR else "")


def parse_key(text: str) -> tuple[int, int]:
    """The EKey and EKeyMode for a key such as Ab, Abm, F#m or "A minor"."""
    match = _KEY.fullmatch(text.strip())
    if not match:
        raise ValueError(f"not a key: {text!r}, write it as Ab for major or Abm for minor")
    letter, accidental, mode = match.groups()
    # The pattern ignores case, so "bb" and "BB" both arrive as letter b, accidental b
    name = letter.upper() + accidental.lower()
    name = SHARPS.get(name, name)
    if name not in KEYS:
        raise ValueError(f"FUSER has no key {name}")
    is_minor = bool(mode) and mode.lower() in ("m", "min", "minor")
    return KEYS.index(name), MINOR if is_minor else MAJOR


def camelot(key: int, mode: int) -> tuple[int, str]:
    """The key's place on the Camelot wheel: 8B is C major, 8A A minor."""
    if mode == MINOR:
        key = (key + 3) % 12  # a minor key shares its number with its relative major
    # Each step clockwise is a fifth up, seven semitones; C major sits at 8
    number = (key * 7 % 12 + 7) % 12 + 1
    return number, "A" if mode == MINOR else "B"


def key_relation(a: tuple[int, int], b: tuple[int, int]) -> str:
    """same, relative, fifth (a neighbour on the wheel) or clash, for two (key, mode)."""
    (na, la), (nb, lb) = camelot(*a), camelot(*b)
    if (na, la) == (nb, lb):
        return "same"
    if na == nb:
        return "relative"
    if la == lb and (na - nb) % 12 in (1, 11):
        return "fifth"
    return "clash"


def stretch_percent(own_bpm: float | None, bpm: float) -> float | None:
    """How far FUSER stretches a disc off its own tempo, allowing for half and double time,
    positive when faster."""
    if not own_bpm:
        return None
    best = min((own_bpm * f for f in (0.5, 1, 2)), key=lambda b: abs(bpm / b - 1))
    return round((bpm / best - 1) * 100, 1)


def stretch_too_far(own_bpm: float | None, bpm: float) -> bool:
    percent = stretch_percent(own_bpm, bpm)
    if percent is None:
        return False
    limit = FAST_STRETCH_PERCENT if max(own_bpm, bpm) > FAST_BPM else STRETCH_PERCENT
    return abs(percent) > limit


def pitch_shift(transposes: list | None, key: int) -> int | None:
    """The semitones FUSER shifts a disc by in a session key, from the disc's own table."""
    if not transposes or not 0 <= key < len(transposes):
        return None
    return transposes[key]


def _named(table: tuple, index) -> str | None:
    return table[index] if isinstance(index, int) and 0 <= index < len(table) else None


def songs(discs: list[dict]) -> list[dict]:
    """The mod's flat list of discs, grouped into songs and sorted by artist and title.

    A song's key and tempo are its own, before FUSER conforms them to the session.
    """
    by_song: dict[str, dict] = {}
    for disc in discs:
        song_id = disc.get("song") or disc["name"]
        song = by_song.get(song_id)
        if song is None:
            song = by_song[song_id] = {
                "id": song_id,
                "title": disc.get("title"),
                "artist": disc.get("artist"),
                "year": disc.get("year") or None,
                "genre": _named(GENRES, disc.get("genre")),
                "bpm": disc.get("bpm"),
                "key": key_name(disc.get("key"), disc.get("mode")),
                # DLC the player does not own loads like any song but plays silence
                "owned": disc.get("owned") is not False,
                "discs": [],
            }
        song["discs"].append({
            "name": disc["name"],
            "type": _named(DISC_TYPES, disc.get("type")),
            "instrument": _named(INSTRUMENTS, disc.get("instrument")),
        })

    order = {name: i for i, name in enumerate(DISC_TYPES)}
    for song in by_song.values():
        song["discs"].sort(key=lambda d: (order.get(d["type"], len(order)), d["name"]))
    return sorted(by_song.values(),
                  key=lambda s: ((s["artist"] or "").casefold(), (s["title"] or "").casefold()))


def disc_index(songs_: list[dict], discs: list[dict]) -> dict[str, dict]:
    """Each disc by name, with the song it belongs to and what the mix checks need: its
    own tempo, key and mode, the shift FUSER gives it in each key, and which modes it has."""
    raw = {d["name"]: d for d in discs}
    index = {}
    for song in songs_:
        for disc in song["discs"]:
            r = raw.get(disc["name"], {})
            index[disc["name"]] = {
                "name": disc["name"], "type": disc["type"], "instrument": disc["instrument"],
                "song": song["title"], "artist": song["artist"], "song_id": song["id"],
                "owned": song["owned"], "bpm": r.get("bpm"), "key": r.get("key"),
                "mode": r.get("mode"), "transposes": r.get("transposes"),
                "has_major": r.get("has_major"), "has_minor": r.get("has_minor"),
                "pickups": r.get("pickups") or [],
                "loop_beats": (r.get("length_ticks") or 0) / TICKS_PER_BEAT or None,
            }
    return index


def is_vocal(disc: dict) -> bool:
    return disc["type"] == "lead" and disc.get("instrument") in VOCALS


def request_wants(f: dict | None, titles: dict | None = None) -> str | None:
    """What a crowd request's FCelFilter asks for, in words, `titles` naming its songs by
    id. Its FilterTo says which of the filter's fields counts: ECelFilterType."""
    if not f:
        return None
    kind = f.get("type")
    if kind == 0:
        return f"a {_named(DISC_TYPES, f.get('disc_type')) or 'disc'} disc"
    if kind in (1, 4):
        return f"a disc played on {_named(INSTRUMENTS, f.get('instrument')) or 'an instrument'}"
    if kind == 2:
        return f"a {_named(GENRES, f.get('genre')) or 'genre'} disc"
    if kind == 3:
        return f"a disc from the {_named(YEAR_RANGES, f.get('years')) or 'years asked for'}"
    if kind == 5:
        songs = [(titles or {}).get(song, song) for song in f.get("songs") or []]
        return "a disc from " + ", ".join(songs or ["a song FUSER names"])
    if kind == 6:
        return "one of " + ", ".join(f.get("discs") or ["the discs FUSER names"])
    return None


def goal_text(name: str | None, song: str | None = None,
              filters: list[dict] | None = None, instrument: str | None = None,
              effect: str | None = None, tempo: int | None = None,
              key: str | None = None, mode: str | None = None) -> str | None:
    """A goal's name with the placeholders filled that can be, `song` being the title of
    the song it features, `filters` the FCelFilters it counts discs by, `instrument` and
    `effect` the titles of the instrument and effect it features, `tempo` the BPM it
    asks the mix to stay at, and `key` and `mode` the key and mode, as FUSER writes them."""
    if not name:
        return name
    # "Include only 1 {btType}" means one disc in all, a beat, not one beat among others,
    # and it tripped up a player as well as the agent
    name = _ONLY_TYPE.sub(lambda m: f"Include only {m[1]} disc{'' if m[1] == '1' else 's'}, "
                          f"{'a ' if m[1] == '1' else 'all '}{{{m[2]}Type}}", name)
    for f in filters or []:
        if f.get("type") == 1 and _named(INSTRUMENTS, f.get("instrument")):
            name = name.replace("{Instrument}", _named(INSTRUMENTS, f["instrument"]))
        if f.get("type") == 2 and _named(GENRES, f.get("genre")):
            name = name.replace("{Genre}", _named(GENRES, f["genre"]))
        if f.get("type") == 3 and _named(YEAR_RANGES, f.get("years")):
            name = name.replace("{YearRange}", _named(YEAR_RANGES, f["years"]))
    for placeholder, words in GOAL_PLACEHOLDERS.items():
        name = name.replace(placeholder, words)
    name = _TYPED_INSTRUMENT.sub(
        lambda m: f"{m[2].lower()} {DISC_TYPE_WORDS[m[1]]}", name)
    if song:
        name = name.replace("{RequiredSong}", song)
    if instrument:
        name = name.replace("{CustomInstrument}", instrument)
    # A goal featuring no instrument takes any: "Drop a {btType} {CustomInstrument} Instrument disc"
    name = name.replace("{CustomInstrument} ", "")
    if effect:
        name = name.replace("{Effect}", effect)
    if tempo:
        name = name.replace("{AbsTempo}", str(tempo))
    if key:
        name = name.replace("{MusicalKey}", key)
    if mode:
        name = name.replace("{MusicalMode}", mode.lower())
    # {FX} is an icon on screen, beside the effect's name or the word Effect
    return name.replace("{FX} ", "")
