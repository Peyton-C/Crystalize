"""The in-game mod, run by a real Lua 5.4 against a fake FUSER. Skips without `lua`."""
import json
import os
import shutil
import subprocess

import pytest

from crystalize.bridge import lua

HERE = os.path.dirname(__file__)
MAIN = os.path.join(HERE, "..", "crystalize", "mod", "Crystalize", "Scripts", "main.lua")
HARNESS = os.path.join(HERE, "mod_harness.lua")
LUA = shutil.which("lua5.4") or shutil.which("lua")

pytestmark = pytest.mark.skipif(LUA is None, reason="needs lua 5.4")


@pytest.fixture
def ask(tmp_path):
    """Send the mod one request; get back its response and the game calls it made."""
    def ask(op, missing=None, raw=None, **args):
        ipc = str(tmp_path) + os.sep
        with open(os.path.join(ipc, "request.lua"), "w", encoding="utf-8") as f:
            f.write(raw if raw is not None else "return " + lua({"id": 7, "op": op, "args": args}))
        cmd = [LUA, HARNESS, MAIN, ipc] + ([missing] if missing else [])
        out = subprocess.run(cmd, capture_output=True, text=True, check=True)
        assert not os.path.exists(os.path.join(ipc, "request.lua"))
        response = os.path.join(ipc, "response-7.json")
        body = json.load(open(response)) if os.path.exists(response) else None
        return body, json.loads(out.stdout)
    return ask


def test_ping(ask):
    body, calls = ask("ping")
    assert body["ok"] and body["id"] == 7 and body["result"]["version"]
    assert body["session"]
    assert calls == []


def test_state(ask):
    body, _ = ask("state")
    state = body["result"]
    assert (state["key"], state["mode"], state["bpm"]) == (7, 1, 120)
    assert (state["bpm_min"], state["bpm_max"]) == (60, 200)
    assert state["decks"][0] == {"playing": "callmemaybebt", "queued": None}
    assert state["decks"][1] == {"playing": None, "queued": "callmemaybebs"}
    assert len(state["decks"]) == 4


def test_discs(ask):
    body, _ = ask("discs")
    assert body["result"][0] == {
        "name": "callmemaybebt", "title": "Call Me Maybe", "type": 0, "key": 7, "mode": 0,
        "bpm": 120, "instrument": 3, "song": "callmemaybe", "artist": "Carly Rae Jepsen",
        "genre": 6, "year": 2012, "owned": True, "length_ticks": 30720,
        "transposes": [-5, -6, 5, 4, 3, 2, 1, 0, -1, -2, -3, -4],
        "has_major": True, "has_minor": False,
    }
    assert [(d["name"], d["owned"]) for d in body["result"]] == [
        ("callmemaybebt", True), ("callmemaybebs", True), ("kingsandqueensbt", False)]


def test_clock(ask):
    body, calls = ask("clock")
    assert body["ok"]
    # The fake records the calls rather than run them, so this checks they are made raw
    assert [(c["method"], c["args"]) for c in calls] == [
        ("GetCurrentBeat", [0]), ("GetCurrentMeasure", [0]), ("GetCurrentTick", [0])]


def test_drop_plays_through_desk_state(ask):
    body, calls = ask("drop", name="callmemaybebs", deck=2, queue=False)
    assert body["ok"]
    assert calls == [{"object": "DeskState", "method": "NetSpawnDisc",
                      "args": ["callmemaybebs", 1, 2, True, None, 0, False]}]


def test_drop_queued(ask):
    _, calls = ask("drop", name="callmemaybebt", deck=0, queue=True)
    assert calls[0]["args"][1] == 2


def test_drop_unknown_disc(ask):
    body, calls = ask("drop", name="nope", deck=0)
    assert body == {**body, "ok": False, "error": "no disc named nope"}
    assert calls == []


def test_drop_rejects_bad_deck(ask):
    body, calls = ask("drop", name="callmemaybebt", deck=4)
    assert not body["ok"] and "deck must be 0 to 3" in body["error"]
    assert calls == []


def test_key_sets_mode_then_key(ask):
    _, calls = ask("set_key", key=8, mode=1)
    assert [(c["method"], c["args"]) for c in calls] == [("NetSetKeyMode", [1]), ("NetSetKey", [8])]


@pytest.mark.parametrize("op, args, method, expected", [
    ("eject", {"deck": 3}, "EjectTrack", [3, False]),
    ("drop_queue", {}, "DoDropQueue", []),
    ("set_tempo", {"bpm": 128}, "NetSetTempo", [128]),
    ("mute", {"deck": 1, "on": True}, "NetMuteTrack", [1, True]),
    ("solo", {"deck": 0, "on": False}, "NetSoloTrack", [0, False]),
])
def test_simple_ops(ask, op, args, method, expected):
    body, calls = ask(op, **args)
    assert body["ok"]
    assert [(c["method"], c["args"]) for c in calls] == [(method, expected)]


def test_outside_freestyle(ask):
    body, _ = ask("drop", missing="DeskState", name="callmemaybebt", deck=0)
    assert not body["ok"] and "freestyle" in body["error"]


def test_unknown_op(ask):
    body, _ = ask("dance")
    assert body["error"] == "unknown command dance"


def test_request_cannot_run_code(ask):
    # The request is loaded with an empty environment, so it has no os, io or print
    body, calls = ask(None, raw='return {id=7, op=os.exit(1)}')
    assert body is None and calls == []

