"""Talks to the Crystalize mod inside FUSER through files in the mod's ipc folder.

UE4SS's Lua has no sockets, and FUSER runs under Wine anyway, so a folder both sides
can see is the simplest channel there is. The server writes request.lua, a Lua table
literal the mod can load() without carrying a JSON parser, and the mod answers with
response-<id>.json. One request is in flight at a time.
"""
from __future__ import annotations

import glob
import itertools
import json
import math
import os
import threading
import time

# The mod looks for a request on FUSER's game thread, which a loading screen can hold for
# seconds, so a request is only given up on after this long
PICKUP_SECONDS = 10.0

# The answer waits on the game thread too, and a command can itself start a load
ANSWER_SECONDS = 10.0

POLL_SECONDS = 0.02

NOT_RUNNING = ("FUSER is not answering. Start it through Steam with UE4SS and the Crystalize "
               "mod installed, see docs/setup.md")


class GameError(Exception):
    """FUSER refused a command, or is not there to take it."""


def lua(value) -> str:
    """A Python value as a Lua literal."""
    if value is None:
        return "nil"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"Lua has no literal for {value}")
        return repr(value)
    if isinstance(value, str):
        return _lua_string(value)
    if isinstance(value, dict):
        return "{" + ",".join(f"[{_lua_string(str(k))}]={lua(v)}" for k, v in value.items()) + "}"
    if isinstance(value, (list, tuple)):
        return "{" + ",".join(lua(v) for v in value) + "}"
    raise TypeError(f"cannot write {type(value).__name__} as Lua")


def _lua_string(text: str) -> str:
    out = []
    for ch in text:
        if ch in '"\\':
            out.append("\\" + ch)
        elif ord(ch) < 32 or ord(ch) == 127:
            out.append(f"\\{ord(ch):03d}")  # always three digits, so a digit after it is safe
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


class Bridge:
    def __init__(self, ipc_dir: str):
        self.dir = ipc_dir
        self.request = os.path.join(ipc_dir, "request.lua")
        os.makedirs(ipc_dir, exist_ok=True)
        self._lock = threading.Lock()
        # Ids start from the clock, so an answer an earlier server gave up on is never
        # taken for one of ours
        self._ids = itertools.count(time.time_ns() // 1_000_000)
        # The mod picks a new one each time it loads, which tells the server the game
        # restarted and its list of discs may have changed
        self.session = None
        for stale in [self.request, self.request + ".tmp",
                      *glob.glob(os.path.join(ipc_dir, "response-*"))]:
            _remove(stale)

    def call(self, op: str, **args):
        """Run `op` in FUSER and return its result."""
        with self._lock:
            id_ = next(self._ids)
            tmp = self.request + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write("return " + lua({"id": id_, "op": op, "args": args}))
            os.replace(tmp, self.request)

            if not _wait(lambda: not os.path.exists(self.request), PICKUP_SECONDS):
                _remove(self.request)
                raise GameError(NOT_RUNNING)
            response = os.path.join(self.dir, f"response-{id_}.json")
            if not _wait(lambda: os.path.exists(response), ANSWER_SECONDS):
                raise GameError("FUSER took the command but did not answer, it may be on a "
                                "loading screen")
            with open(response, encoding="utf-8") as f:
                body = json.load(f)
            _remove(response)

        self.session = body.get("session", self.session)
        if not body.get("ok"):
            raise GameError(body.get("error") or "FUSER refused the command")
        return body.get("result")


def _wait(done, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while not done():
        if time.monotonic() > deadline:
            return False
        time.sleep(POLL_SECONDS)
    return True


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
