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
        "has_major": True, "has_minor": False, "pickups": [15, 28.5],
    }
    assert [(d["name"], d["owned"]) for d in body["result"]] == [
        ("callmemaybebt", True), ("callmemaybebs", True), ("kingsandqueensbt", False)]


def test_clock(ask):
    body, calls = ask("clock")
    assert body["ok"]
    # The fake records the calls rather than run them, so this checks they are made raw
    assert [(c["method"], c["args"]) for c in calls] == [
        ("GetCurrentBeat", [0]), ("GetCurrentMeasure", [0]), ("GetCurrentTick", [0]),
        ("GetDefaultSyncBeat", [])]


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
    ("snapshot", {}, "SaveCurrentMoment", []),
])
def test_simple_ops(ask, op, args, method, expected):
    body, calls = ask(op, **args)
    assert body["ok"]
    assert [(c["method"], c["args"]) for c in calls] == [(method, expected)]


def test_batch_runs_in_order(ask):
    body, calls = ask("batch", commands=[
        {"op": "drop", "args": {"name": "callmemaybebs", "deck": 1, "queue": False}},
        {"op": "set_tempo", "args": {"bpm": 128}}])
    assert body["ok"] and len(body["result"]) == 2
    assert [c["method"] for c in calls] == ["NetSpawnDisc", "NetSetTempo"]


def test_batch_with_an_unknown_command_runs_nothing(ask):
    body, calls = ask("batch", commands=[
        {"op": "drop", "args": {"name": "callmemaybebs", "deck": 1}}, {"op": "dance"}])
    assert not body["ok"] and "dance" in body["error"]
    assert calls == []


def test_batch_cannot_hold_a_batch(ask):
    body, _ = ask("batch", commands=[{"op": "batch", "args": {}}])
    assert not body["ok"]


def test_batch_reports_the_command_that_failed(ask):
    body, calls = ask("batch", commands=[
        {"op": "set_tempo", "args": {"bpm": 128}},
        {"op": "drop", "args": {"name": "nope", "deck": 0}}])
    assert not body["ok"] and "command 2" in body["error"] and "no disc named nope" in body["error"]
    assert [c["method"] for c in calls] == ["NetSetTempo"]


def test_set_state(ask):
    body, _ = ask("set_state")
    state = body["result"]
    assert state["problems"] == [] and state["measure"] == 84
    active, scheduled = state["requests"]
    assert active == {
        "id": -1, "state": 1, "start_measure": 82, "bars": 14, "procedural": 1,
        "prompt": "Some {Instrument} will make my day!",
        "filter": {"type": 1, "disc_type": 0, "instrument": 5, "genre": 0, "years": 0,
                   "songs": [], "discs": []},
    }
    assert "filter" not in scheduled  # FUSER picks what it wants only when it starts
    done, open_ = state["goals"]
    assert (done["name"], done["completed"], done["active"]) == ("Drop a {btType}", True, False)
    # false, not null: an answer of false must not fall through to the fallback
    assert (open_["completed"], open_["active"], open_["kind"]) == (False, True, "ShowGoalQuota")
    # The song the set features for it, and none for a goal that features none
    assert open_["song"] == "rockthecasbah" and "song" not in done
    assert [f["genre"] for f in done["filters"]] == [6]  # a stat goal's, from its mix filters
    assert open_["filters"] == [{"type": 2, "disc_type": 0, "instrument": 0, "genre": 8,
                                 "years": 0, "songs": [], "discs": []}]
    assert state["freshness"] == {"value": 0.5, "min": 0, "max": 1}
    # The live meter, not the first one found, which sits frozen at its defaults
    assert state["crowd"] == {"CurHealth": 62, "MaxHealth": 100, "CurHealthState": 2,
                              "WarningCount": 0}
    assert state["show"]["score"] == 1500
    assert state["show"]["ending"] is False
    results = state["show"]["results"]
    assert results["stars"] == 2.5 and results["points"]["PerfectTimingBonusPoints"] == 800


def test_set_state_reads_each_part_alone(ask):
    body, _ = ask("set_state", missing="RequestMgr")
    state = body["result"]
    assert body["ok"] and state["requests"] is None
    assert state["problems"] == ["requests: RequestMgr not found, open a freestyle session or "
                                 "a campaign set first"]
    assert state["freshness"]["value"] == 0.5 and len(state["goals"]) == 2


def test_read_fields(ask):
    body, calls = ask("read", **{"class": "BP_Crowd_Manager_C",
                                 "fields": ["CurrentCrowdMeterState", "_label"]})
    assert body["result"] == {"CurrentCrowdMeterState": 1, "_label": "Crowd"}
    assert calls == []  # read only: nothing in the game is called


def test_read_array(ask):
    body, _ = ask("read", **{"class": "ShowPrompt", "fields": ["FeaturedSongs"]})
    assert body["result"] == {"FeaturedSongs": ["CelSongSet /Game/Audio/Songs/rockthecasbah"]}


def test_read_all(ask):
    body, _ = ask("read", **{"class": "WBP_ShowsCrowdMeter_C", "fields": ["CurHealth"], "all": True})
    assert [(r["CurHealth"], "/Engine/Transient" in r["(object)"]) for r in body["result"]] == [
        (30, False), (62, True)]


def test_set_state_without_a_live_meter(ask):
    body, _ = ask("set_state", missing="WBP_ShowsCrowdMeter_C")
    state = body["result"]
    assert state["crowd"] is None and "crowd: no crowd meter has started" in state["problems"]


def test_read_unreadable_field_is_null(ask):
    body, _ = ask("read", **{"class": "ShowGoalsMgr", "fields": ["nope"]})
    assert body["ok"] and body["result"] == {"nope": None}


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



def test_goals_without_featured_songs(ask):
    body, _ = ask("set_state", missing="ShowFlowMgr")
    state = body["result"]
    assert any(p.startswith("featured songs: ") for p in state["problems"])
    assert len(state["goals"]) == 2 and state["goals"][1]["song"] is None
