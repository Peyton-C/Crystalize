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
    def ask(op, missing=None, raw=None, breaks=0, flow=None, messages=0, screen=3,
            page="none", **args):
        ipc = str(tmp_path) + os.sep
        with open(os.path.join(ipc, "request.lua"), "w", encoding="utf-8") as f:
            f.write(raw if raw is not None else "return " + lua({"id": 7, "op": op, "args": args}))
        cmd = [LUA, HARNESS, MAIN, ipc, missing or "", str(breaks),
               "" if flow is None else str(flow), str(messages), str(screen), page]
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
    assert state["decks"][0] == {"playing": "callmemaybebt", "queued": None, "gain": 1}
    assert state["decks"][1] == {"playing": None, "queued": "callmemaybebs", "gain": 0.5}
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
    ("hot_swap", {"deck": 3}, "HotSwapOnDeck", [3]),
    ("set_tempo", {"bpm": 128}, "NetSetTempo", [128]),
    ("mute", {"deck": 1, "on": True}, "NetMuteTrack", [1, True]),
    ("solo", {"deck": 0, "on": False}, "NetSoloTrack", [0, False]),
    ("gain", {"deck": 2, "gain": 0.5}, "NetSetGainForTrack", [2, 0.5]),
    ("snapshot", {}, "SaveCurrentMoment", []),
])
def test_simple_ops(ask, op, args, method, expected):
    body, calls = ask(op, **args)
    assert body["ok"]
    assert [(c["method"], c["args"]) for c in calls] == [(method, expected)]


def test_instruments_leave_out_the_placeholder(ask):
    body, _ = ask("instruments")
    assert body["result"] == [{"name": "DrumCanvas2", "title": "Electro Funk Drums"},
                              {"name": "Banjo", "title": "Banjo"}]


def test_perform_begin_loads_the_instrument_into_a_slot(ask):
    body, calls = ask("perform_begin", slot=0, name="Banjo")
    assert body["result"] == {"slot": 0, "crafting": True, "ready": True}
    assert calls == [{"object": "InstrumentCraftingState", "method": "NetBeginCrafting",
                      "args": [0, "Banjo"]}]


def test_perform_begin_unknown_instrument(ask):
    body, calls = ask("perform_begin", slot=0, name="Theremin")
    assert not body["ok"] and "no instrument named Theremin" in body["error"]
    assert calls == []


def test_perform_press_solos_the_pad(ask):
    body, calls = ask("perform_press", slot=0, x=2, y=3, on=True)
    assert body["ok"]
    assert calls == [{"object": "InstrumentCraftingState", "method": "NetSetGridCraftingState",
                      "args": [0, {"PrimaryActionOn": True, "SecondaryActionOn": False,
                                   "GridPos": {"X": 2, "Y": 3}, "mode": 1}]}]


def test_perform_end_and_a_bad_slot(ask):
    body, calls = ask("perform_end", slot=0)
    assert body["ok"] and [c["method"] for c in calls] == ["NetEndCrafting"]
    body, calls = ask("perform_end", slot=4)
    assert not body["ok"] and "slot must be 0 to 3" in body["error"] and calls == []


def test_effects_leave_out_the_hidden_transition(ask):
    body, _ = ask("effects")
    assert body["result"] == [{"name": "Filter_01", "title": "Low-Pass Filter"},
                              {"name": "Flanger_01", "title": "Flanger"}]


def test_fx_state_gives_each_deck_then_the_master(ask):
    body, _ = ask("fx_state")
    assert body["result"] == [None, {"name": "Flanger_01", "title": "Flanger", "on": True},
                              None, None, None]


def test_fx_loads_an_effect_into_a_decks_slot_and_turns_it_on(ask):
    body, calls = ask("fx", deck=0, name="Filter_01", on=True)
    assert body["result"] == {"name": "Filter_01", "title": "Low-Pass Filter", "on": True}
    assert [(c["method"], c["args"]) for c in calls] == [
        ("NetSetAddEffectToSlot", [3, "Filter_01"]), ("NetSetEffectEnabled", [3, True])]


def test_fx_keeps_the_effect_a_slot_holds(ask):
    _, calls = ask("fx", deck=1, name="Flanger_01", on=False)
    assert [(c["method"], c["args"]) for c in calls] == [("NetSetEffectEnabled", [4, False])]
    _, calls = ask("fx", deck=1, on=True)
    assert [(c["method"], c["args"]) for c in calls] == [("NetSetEffectEnabled", [4, True])]


def test_fx_without_a_deck_is_the_master(ask):
    _, calls = ask("fx", name="Flanger_01", on=True)
    assert [c["args"][0] for c in calls] == [7, 7]


def test_fx_needs_an_effect_for_an_empty_slot(ask):
    body, calls = ask("fx", deck=2, on=True)
    assert not body["ok"] and "holds no effect" in body["error"] and calls == []
    body, calls = ask("fx", deck=2, name="Theremin", on=True)
    assert not body["ok"] and "no effect named Theremin" in body["error"] and calls == []


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
    done, open_, perform, fx, tempo = state["goals"]
    assert (done["name"], done["completed"], done["active"]) == ("Drop a {btType}", True, False)
    # false, not null: an answer of false must not fall through to the fallback
    assert (open_["completed"], open_["active"], open_["kind"]) == (False, True, "ShowGoalQuota")
    # The song the set features for it, and none for a goal that features none
    assert open_["song"] == "rockthecasbah" and "song" not in done
    # The instrument a Perform goal features, by title, and the set's featured ones by name
    assert perform["custom_instrument"] == "Electro Funk Drums"
    assert "custom_instrument" not in open_
    assert state["instruments"] == [{"name": "DrumCanvas2", "title": "Electro Funk Drums"}]
    # The effect a Use FX goal features, and the tempo a tempo goal wants, 0 left out
    assert fx["effect"] == {"name": "Filter_01", "title": "Low-Pass Filter"}
    assert state["effects"] == [{"name": "Filter_01", "title": "Low-Pass Filter"}]
    assert tempo["tempo"] == 115 and "tempo" not in done and "effect" not in open_
    assert tempo["mode"] == "Minor" and "key" not in tempo
    assert [f["genre"] for f in done["filters"]] == [6]  # a stat goal's, from its mix filters
    assert open_["filters"] == [{"type": 2, "disc_type": 0, "instrument": 0, "genre": 8,
                                 "years": 0, "songs": [], "discs": []}]
    assert state["freshness"] == {"value": 0.5, "min": 0, "max": 1, "in_zone": False,
                                  "zone_points": 0, "unique_mix": True, "zone": [0.4, 0.7]}
    # The live meter, not the first one found, which sits frozen at its defaults
    assert state["crowd"] == {"CurHealth": 62, "MaxHealth": 100, "CurHealthState": 2,
                              "WarningCount": 0, "PrevWarningCount": 0,
                              "OveractivityEnabled": True, "OveractiveThreshold": 3,
                              "DrainPaused": False, "FailurePending": False, "ActionCount": 2}
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
    assert state["freshness"]["value"] == 0.5 and len(state["goals"]) == 5


def test_read_fields(ask):
    body, calls = ask("read", **{"class": "BP_Crowd_Manager_C",
                                 "fields": ["CurrentCrowdMeterState", "_label"]})
    assert body["result"] == {"CurrentCrowdMeterState": 1, "_label": "Crowd"}
    assert calls == []  # read only: nothing in the game is called


def test_read_array(ask):
    body, _ = ask("read", **{"class": "ShowPrompt", "fields": ["FeaturedSongs"]})
    assert body["result"] == {"FeaturedSongs": ["CelSongSet /Game/Audio/Songs/rockthecasbah"]}


def test_read_map_gives_pairs(ask):
    body, _ = ask("read", **{"class": "BP_Crowd_Manager_C", "fields": ["Thresholds"]})
    assert body["result"] == {"Thresholds": [[3, 1], [2, 0.75]]}


def test_read_all(ask):
    body, _ = ask("read", **{"class": "WBP_ShowsCrowdMeter_C", "fields": ["CurHealth"], "all": True})
    assert [(r["CurHealth"], "/Engine/Transient" in r["(object)"]) for r in body["result"]] == [
        (30, False), (62, True)]


def test_lua_returns_what_it_returns_and_prints(ask):
    body, _ = ask("lua", code="print('bpm', 120) "
                              "return {meter = first('BP_Crowd_Manager_C').CurrentCrowdMeterState, "
                              "songs = first('ShowPrompt').FeaturedSongs, list = {1, 2}}")
    assert body["result"] == {"printed": ["bpm\t120"], "result": {
        "meter": 1, "songs": ["CelSongSet /Game/Audio/Songs/rockthecasbah"], "list": [1, 2]}}


def test_lua_keeps_null_inside_a_table(ask):
    body, _ = ask("lua", code="return {prompt = readable(nil)}")
    assert body["result"]["result"] == {"prompt": None}


def test_lua_error_is_reported(ask):
    body, _ = ask("lua", code="error('no such thing', 0)")
    assert not body["ok"] and body["error"] == "no such thing"
    body, _ = ask("lua", code="return (")
    assert not body["ok"] and "lua" in body["error"]


def test_flow(ask):
    body, calls = ask("flow")
    assert body["result"] == {"screen": 3, "title": 9, "mode": 1, "flow": 3, "flow_object":
        "ShowFlowMgr /Game/Maps/Venue02/Venue02.Venue02:PersistentLevel.HypeGameStateShow_7.ShowFlowMgr"}
    assert calls == []


def test_sets_skips_a_null_prompt(ask):
    body, _ = ask("sets", venues=[0, 1])
    assert body["result"] == [[{"id": "M_0010", "name": "SET {Num}: Opener"}, None],
                              [{"id": "M_0161", "name": "SET {Num}: Mute Point"}]]


def test_load_set(ask):
    body, calls = ask("load_set", venue=0, set=0)
    assert body["result"]["set"].endswith("M_0010.M_0010")
    assert [(c["object"], c["method"], c["args"]) for c in calls] == [
        ("LevelLoadConfig", "ConfigureShow", ["M_0010"]),
        # The set's own discs, which its skipped loadout screen would have added
        ("CrateMgr", "ApplyCrateFilter", ["M_0010"]),
        ("VenueMgr", "SetCurrentVenueData", ["VenueA", False]),
        ("VenueMgr", "LoadCurrentVenueMap", ["LevelLoadConfig", True]),
    ]


def test_load_set_refuses_a_null_prompt_before_touching_the_game(ask):
    body, calls = ask("load_set", venue=0, set=1)
    assert not body["ok"] and "no set 1" in body["error"] and calls == []


def test_load_freestyle_in_the_last_venue(ask):
    _, calls = ask("load_freestyle")
    assert [(c["method"], c["args"]) for c in calls] == [
        ("ConfigureFreestyle", []), ("SetCurrentVenueData", ["VenueB", True]),
        ("LoadCurrentVenueMap", ["LevelLoadConfig", True])]


def test_begin_loads_the_gameplay_screen_then_walks_the_flow(ask):
    _, calls = ask("begin", states=[1, 2])
    assert [(c["object"], c["method"], c["args"][:2]) for c in calls] == [
        ("ShellFlowMgr", "LoadShellScreen", [10, False]),
        ("ShowFlowMgr", "GoToState", [1, {}]), ("ShowFlowMgr", "GoToState", [2, {}])]


def test_sign_in_and_main_menu(ask):
    _, calls = ask("sign_in")
    assert [(c["object"], c["method"]) for c in calls] == [("TitleScreen", "StartLoginFlow")]
    _, calls = ask("main_menu")
    assert [(c["method"], c["args"]) for c in calls] == [("GoBackToScreen", [3, True])]


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
    assert len(state["goals"]) == 5 and state["goals"][1]["song"] is None


def test_show_break_is_closed_once_its_dialog_is_on_a_page(ask):
    _, calls = ask("ping", breaks=1, flow=5, messages=2, screen=10, page="first")
    assert calls == []
    # Once, though the break goes on after its dialog closes
    _, calls = ask("ping", breaks=20, flow=5, messages=2, screen=10, page="first")
    assert calls == [{"object": "ShowFlowMgr", "method": "_HandleShowBreakProgression",
                      "args": []}]
    _, calls = ask("ping", breaks=2, flow=5, messages=1, screen=10, page="last")
    assert [c["method"] for c in calls] == ["_HandleShowBreakProgression"]
    # FUSER puts the dialog on a show-break screen of its own
    _, calls = ask("ping", breaks=2, flow=5, messages=1, screen=48, page="last")
    assert [c["method"] for c in calls] == ["_HandleShowBreakProgression"]


def test_show_break_that_never_shows_a_page_is_moved_on(ask):
    _, calls = ask("ping", breaks=39, flow=5, messages=1, screen=10)
    assert calls == []
    _, calls = ask("ping", breaks=60, flow=5, messages=1, screen=10)
    assert calls == [{"object": "ShowFlowMgr", "method": "_GoToNextBreakMessage", "args": []}]


def test_show_break_without_messages_is_left_to_end(ask):
    _, calls = ask("ping", breaks=60, flow=5, messages=0, screen=10, page="first")
    assert calls == []


def test_show_break_check_waits_for_the_gameplay_screen(ask):
    _, calls = ask("ping", breaks=60, flow=5, messages=2, screen=3, page="first")
    assert calls == []


def test_show_break_check_leaves_gameplay_alone(ask):
    _, calls = ask("ping", breaks=60, flow=4, messages=2, screen=10, page="first")
    assert calls == []


def test_show_break_check_without_a_show(ask):
    body, calls = ask("ping", missing="ShowFlowMgr", breaks=8)
    assert body["ok"] and calls == []
