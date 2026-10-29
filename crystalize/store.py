"""What Crystalize keeps between sessions: tags, saved mixes, the last library FUSER
reported, so a mix can be planned while the game is closed, and a record of each campaign
set played."""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time

TAG_FIELDS = ("energy", "style", "lyrics", "mood_clash")


def data_dir() -> str:
    base = os.environ.get("CRYSTALIZE_DATA") or os.path.join(
        os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "crystalize")
    os.makedirs(base, exist_ok=True)
    return base


def _read(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def _write(path: str, value) -> None:
    # Written beside and renamed over, so a crash never leaves half a file
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


# ------------------------------------------------------------------------- tags

def tags() -> dict[str, dict]:
    """Tags by song id. FUSER's song ids are its own short names, the same in every session."""
    return _read(os.path.join(data_dir(), "tags.json"), {})


def tag(song_id: str, fields: dict, clear: bool = False) -> dict:
    all_tags = tags()
    current = {} if clear else dict(all_tags.get(song_id, {}))
    for field in TAG_FIELDS:
        if field in fields and fields[field] is not None:
            current[field] = fields[field]
    if current:
        all_tags[song_id] = current
    else:
        all_tags.pop(song_id, None)
    _write(os.path.join(data_dir(), "tags.json"), all_tags)
    return current


# ---------------------------------------------------------------------- library

def save_library(discs: list[dict], bpm_range: list | None) -> None:
    _write(os.path.join(data_dir(), "library.json"),
           {"saved": time.time(), "discs": discs, "bpm_range": bpm_range})


def load_library() -> dict | None:
    return _read(os.path.join(data_dir(), "library.json"), None)


# ------------------------------------------------------------------------ mixes

def _mix_path(name: str) -> str:
    slug = re.sub(r"[^\w .-]+", "_", name).strip(" .") or "mix"
    return os.path.join(data_dir(), "mixes", slug + ".json")


def save_mix(name: str, plan: dict, client: str | None) -> str:
    path = _mix_path(name)
    _write(path, {"name": name, "saved": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                  "client": client, **plan})
    return path


def load_mix(name: str) -> dict:
    mix = _read(_mix_path(name), None)
    if mix is None:
        raise ValueError(f"no saved mix named {name}, list_mixes lists them")
    return mix


def list_mixes() -> list[dict]:
    folder = os.path.join(data_dir(), "mixes")
    if not os.path.isdir(folder):
        return []
    out = []
    for file in sorted(os.listdir(folder)):
        if file.endswith(".json"):
            mix = _read(os.path.join(folder, file), {})
            out.append({"name": mix.get("name", file[:-5]), "saved": mix.get("saved"),
                        "steps": len(mix.get("steps", []))})
    return out


# ------------------------------------------------------------------------- sets

class SetLog:
    """A campaign set's record, one JSON object a line in sets/: the set as get_set shows
    it whenever it changed, and every command sent, each with when. A run that lost points
    can be gone over after, measure by measure."""

    def __init__(self):
        self._file = None
        self._last = None
        self._lock = threading.Lock()
        self.path: str | None = None

    @property
    def open(self) -> bool:
        return self._file is not None

    def start(self) -> None:
        with self._lock:
            if self._file:
                return
            folder = os.path.join(data_dir(), "sets")
            os.makedirs(folder, exist_ok=True)
            self.path = os.path.join(folder, time.strftime("%Y-%m-%d-%H%M%S") + ".jsonl")
            self._file = open(self.path, "a", encoding="utf-8")
            self._last = None

    def read(self, view: dict) -> None:
        # The measure moves on every read, so a read goes in only when more than it changed
        rest = {k: v for k, v in view.items() if k != "measure"}
        with self._lock:
            if self._file and rest != self._last:
                self._last = rest
                self._line({"set": view})

    def sent(self, commands) -> None:
        with self._lock:
            if self._file:
                self._line({"sent": [[op, args] for op, args in commands]})

    def end(self) -> None:
        with self._lock:
            if self._file:
                self._file.close()
                self._file = None

    def _line(self, entry: dict) -> None:
        self._file.write(json.dumps({"t": round(time.time(), 3), **entry}, ensure_ascii=False) + "\n")
        self._file.flush()
