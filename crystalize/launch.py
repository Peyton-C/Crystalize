"""Takes FUSER to a campaign set, freestyle or the main menu without its menus.

The steps were found by trying FUSER's functions one at a time with debug_lua: sign in
from the title screen, configure the session, load its venue's map, then load the
gameplay screen and walk the flow into play. Each waits on the last, read through the
mod's flow op, since a map load takes seconds and holds the game thread while it does.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time

from .bridge import GameError

STEAM_APP = 1331440
PROCESS = "Fuser-Win64-Shipping"

# Venues 1 to 6 are the campaign's. The rest are a test venue, a debug venue and the
# multiplayer copy of the first
VENUES = 6

# EShellScreenType
INTRO, TITLE, MAIN_MENU, LOADING = 1, 2, 3, 12
# ETitleScreenState: before a key is pressed, and signed in with the save loaded
TITLE_UNSTARTED, TITLE_WAITING, TITLE_DONE = 0, 1, 9
# EGameFlowState: Intro, then WorkspaceReveal, which a show does not reach on its own.
# From GameplayPending it plays, and the show starts with the first disc
INTRO_STATE, REVEAL_STATE, PENDING_STATE = 1, 2, 3

# How long each step may take. Signing in scans for DLC, which is slow with a large
# custom library; a map load holds the game thread for several seconds
SIGN_IN_SECONDS = 120
LOAD_SECONDS = 60
START_SECONDS = 20
POLL_SECONDS = 0.5

# FUSER's start, logos and all, before the mod answers
BOOT_SECONDS = 120


class LaunchError(Exception):
    """FUSER did not get where it was sent."""


def sets(bridge) -> list[list[dict | None]]:
    """Each campaign venue's sets, as the game lists them."""
    return bridge.call("sets", venues=list(range(VENUES)))


def go_to(bridge, where: str, venue: int | None = None, set_: int | None = None,
          sign_in_seconds: float = SIGN_IN_SECONDS) -> dict:
    """Take FUSER to `where`: "set" (venue and set from 1), "freestyle" (in `venue`, else
    the last one played) or "menu". Works from the intro, the menu, or inside a session,
    which it leaves."""
    if where not in ("set", "freestyle", "menu"):
        raise ValueError(f"where is set, freestyle or menu, not {where}")
    if where == "set" and (venue is None or set_ is None):
        raise ValueError("a set needs its venue and its number in the venue")
    if venue is not None and not 1 <= venue <= VENUES:
        raise ValueError(f"venue is 1 to {VENUES}")
    state = sign_in(bridge, sign_in_seconds)

    if where == "menu":
        if state.get("screen") == MAIN_MENU:
            return {"at": "menu"}
        bridge.call("main_menu")
        _wait(bridge, lambda s: s.get("screen") == MAIN_MENU, LOAD_SECONDS, "the main menu")
        return {"at": "menu"}

    before = state.get("flow_object")
    if where == "set":
        listed = sets(bridge)[venue - 1]
        if not 1 <= set_ <= len(listed) or listed[set_ - 1] is None:
            raise ValueError(f"venue {venue} has sets 1 to {len(listed)}: "
                             + ", ".join(f"{i} {_name(s)}" for i, s in enumerate(listed, 1)))
        loaded = bridge.call("load_set", venue=venue - 1, set=set_ - 1)
        states = [INTRO_STATE, REVEAL_STATE]
    else:
        loaded = bridge.call("load_freestyle", **({"venue": venue - 1} if venue else {}))
        states = [INTRO_STATE]

    # A new flow manager means the new map is up. The old one can still answer for a
    # moment after the load is asked for
    _wait(bridge, lambda s: s.get("flow_object") not in (None, before)
          and s.get("screen") != LOADING, LOAD_SECONDS, "the venue to load")
    bridge.call("begin", states=states)
    final = _wait(bridge, lambda s: (s.get("flow") or 0) >= PENDING_STATE, START_SECONDS,
                  "the session to start")

    result = {"at": where, "venue": _asset(loaded["venue"])}
    if where == "set":
        result.update(set=set_, name=_name(listed[set_ - 1]))
    result["flow"] = final.get("flow")
    return result


def sign_in(bridge, seconds: float = SIGN_IN_SECONDS) -> dict:
    """Sign in if FUSER is still on its intro or title screen, and wait for the save to
    load: a set loaded before it would play, and save, an empty profile."""
    # During the startup logos there is no title screen yet, and the mod may not answer at
    # all, so this waits for the intro or the title screen rather than taking no title
    # screen for signed in
    state = _wait(bridge, lambda s: _signed_in(s) or s.get("screen") in (INTRO, TITLE),
                  seconds, "FUSER's intro or title screen")
    if _signed_in(state):
        return state
    if state.get("title") in (TITLE_UNSTARTED, TITLE_WAITING):
        bridge.call("sign_in")
    return _wait(bridge, lambda s: s.get("title") == TITLE_DONE and s.get("screen") == MAIN_MENU,
                 seconds, "FUSER to sign in")


def _signed_in(state: dict) -> bool:
    # The title screen can be gone once a venue has loaded since. Back at the main menu from
    # a set, FUSER makes a new one that reads unstarted and is never used, so the menu itself
    # is the proof: sign-in is the only way there
    if state.get("title") == TITLE_DONE or state.get("screen") == MAIN_MENU:
        return True
    return state.get("title") is None and state.get("screen") not in (None, 0, INTRO, TITLE)


def _wait(bridge, done, seconds: float, what: str) -> dict:
    deadline = time.monotonic() + seconds
    state: dict = {}
    while True:
        try:
            state = bridge.call("flow")
        except GameError:
            # A map load holds the game thread, so a read can go unanswered
            state = {}
        if state and done(state):
            return state
        if time.monotonic() >= deadline:
            raise LaunchError(f"gave up waiting for {what} after {int(seconds)} seconds; "
                              f"FUSER was at {state or 'no answer'}")
        time.sleep(POLL_SECONDS)


def _name(s: dict | None) -> str:
    # The game fills {Num} in itself, and the set's number is given beside the name
    return "?" if not s else (s.get("name") or s["id"]).replace("SET {Num}: ", "")


def _asset(full_name: str) -> str:
    return full_name.rsplit(".", 1)[-1]


# ------------------------------------------------------------------------- the process

def running() -> bool:
    return bool(_pids(_is_game))


def stop_game(seconds: float = 30) -> None:
    """Close FUSER and wait until Steam no longer counts it as running, so it can start
    again: Steam waits on its launch wrapper, which outlives the game by a moment."""
    for pid in _pids(_is_game):
        try:
            os.kill(pid, 15)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + seconds
    while _pids(_is_game) or _pids(_is_launch):
        if time.monotonic() >= deadline:
            raise LaunchError("FUSER did not close")
        time.sleep(POLL_SECONDS)


def start_game(bridge, seconds: float = BOOT_SECONDS) -> None:
    """Start FUSER through Steam, with its launch options, and wait for the mod."""
    steam = shutil.which("steam")
    if not steam:
        raise LaunchError("steam is not on PATH, start FUSER yourself")
    subprocess.Popen([steam, "-applaunch", str(STEAM_APP)], stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
    deadline = time.monotonic() + seconds
    while True:
        try:
            bridge.call("ping")
            return
        except GameError:
            if time.monotonic() >= deadline:
                raise LaunchError("FUSER started but the mod never answered, see "
                                  "docs/setup.md") from None
            time.sleep(POLL_SECONDS)


def _is_game(argv: list[bytes]) -> bool:
    # Under Proton the game is a Wine process whose first argument is its Windows path
    return argv[0].replace(b"\\", b"/").endswith(f"/{PROCESS}.exe".encode())


def _is_launch(argv: list[bytes]) -> bool:
    return f"AppId={STEAM_APP}".encode() in argv


def _pids(match) -> list[int]:
    pids = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as f:
                argv = f.read().split(b"\0")
        except OSError:
            continue
        if argv and argv[0] and match(argv):
            pids.append(int(entry))
    return pids
