"""The MCP server's protocol and tools, against a fake bridge instead of FUSER."""
import io
import json

import pytest

from crystalize import mcp, store
from crystalize.bridge import GameError

# Every key shift 0, so the plan checks here only see what a test sets up
FLAT = [0] * 12


def disc(name, title, artist, song, type_, key, mode, bpm, genre, year, instrument, **more):
    return {"name": name, "title": title, "type": type_, "key": key, "mode": mode, "bpm": bpm,
            "instrument": instrument, "song": song, "artist": artist, "genre": genre,
            "year": year, "transposes": FLAT, "has_major": True, "has_minor": True, **more}


DISCS = [
    disc("callmemaybebt", "Call Me Maybe", "Carly Rae Jepsen", "callmemaybe", 0, 7, 0, 120, 6,
         2012, 3),
    disc("callmemaybeld", "Call Me Maybe", "Carly Rae Jepsen", "callmemaybe", 3, 7, 0, 120, 6,
         2012, 9),
    disc("mrbrightsidebs", "Mr. Brightside", "The Killers", "mrbrightside", 1, 1, 0, 148, 8,
         2003, 11),
    disc("kingsandqueensld", "Kings and Queens", "Thirty Seconds to Mars", "kingsandqueens", 3,
         2, 0, 135, 8, 2009, 10, owned=False),
    disc("badguybs", "bad guy", "Billie Eilish", "badguy", 1, 7, 1, 135, 6, 2019, 2),
]

STATE = {"key": 8, "mode": 1, "bpm": 120, "bpm_min": 60, "bpm_max": 200,
         "decks": [{"playing": "callmemaybebt", "queued": None}, {"playing": None},
                   {"playing": None}, {"playing": None}]}


# What set_state gave in venue 2: one goal done, one up, a synth request active and one
# scheduled with nothing chosen yet
SET_STATE = {
    "measure": 84, "problems": [],
    "requests": [
        {"id": -1, "state": 1, "procedural": 1, "start_measure": 82, "bars": 14,
         "prompt": "Some {Instrument} will make my day!",
         "filter": {"type": 1, "instrument": 5, "disc_type": 0, "genre": 0, "years": 0,
                    "songs": [], "discs": []}},
        {"id": -1, "state": 0, "procedural": 1, "start_measure": 110, "bars": 12},
        {"id": -1, "state": 3, "start_measure": 40, "bars": 14},
    ],
    "goals": [
        {"name": "Drop a {btType}", "kind": "ShowGoalQuota", "performed": 1, "required": 1,
         "progress": 1, "bars": 0, "completed": True, "active": False},
        {"name": "Include 2+ {Genre} discs", "kind": "ShowGoalQuota", "performed": 1,
         "required": 2, "progress": 0.5, "bars": 0, "completed": False, "active": True},
    ],
    "freshness": {"min": 0, "max": 1, "value": 0.304},
    "crowd": {"CurHealth": 30, "MaxHealth": 100, "CurHealthState": 1, "WarningCount": 1},
    "show": {"score": 3000, "section": 2, "has_meter": True},
}


class FakeBridge:
    def __init__(self):
        self.calls = []
        self.session = "s1"
        self.fail = {}  # op -> the error FUSER gives for it

    def call(self, op, **args):
        self.calls.append((op, args))
        if op in self.fail:
            raise GameError(self.fail[op])
        return {"discs": DISCS, "state": STATE, "clock": {"beat": 0.0},
                "set_state": SET_STATE}.get(op, {})

    def ops(self):
        return [op for op, _ in self.calls]


@pytest.fixture(autouse=True)
def data(tmp_path, monkeypatch):
    monkeypatch.setenv("CRYSTALIZE_DATA", str(tmp_path))


@pytest.fixture
def fake():
    return FakeBridge()


@pytest.fixture
def talk(fake):
    def talk(*requests):
        lines = "".join(json.dumps({"jsonrpc": "2.0", **r}) + "\n" for r in requests)
        writer = io.BytesIO()
        mcp.Server(fake, io.BytesIO(lines.encode()), writer).run()
        return [json.loads(line) for line in writer.getvalue().decode().splitlines()]
    return talk


def call(talk, tool, **arguments):
    [reply] = talk({"id": 1, "method": "tools/call",
                    "params": {"name": tool, "arguments": arguments}})
    result = reply["result"]
    text = result["content"][0]["text"]
    return (text if result["isError"] else json.loads(text)), result["isError"]


# ---------------------------------------------------------------------- protocol

def test_initialize_and_list(talk):
    init, tools, prompts = talk(
        {"id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
        {"id": 2, "method": "tools/list"}, {"id": 3, "method": "prompts/list"})
    assert init["result"]["protocolVersion"] == "2025-03-26"
    assert init["result"]["capabilities"] == {"tools": {}, "prompts": {}}
    names = [t["name"] for t in tools["result"]["tools"]]
    assert names[:4] == ["get_state", "get_set", "search_songs", "tag_songs"]
    assert {"plan_mix", "play_mix", "stop_mix", "save_mix", "drop_disc"} <= set(names)
    assert [p["name"] for p in prompts["result"]["prompts"]] == \
        ["tag_library", "build_mix", "review_mix"]


def test_every_tool_has_a_handler():
    server = mcp.Server(FakeBridge(), None, None)
    for tool in mcp.TOOLS:
        assert callable(getattr(server, f"_tool_{tool['name']}"))


def test_prompt_fills_in_arguments(talk):
    [reply] = talk({"id": 1, "method": "prompts/get",
                    "params": {"name": "build_mix",
                               "arguments": {"minutes": "20", "name": "Friday"}}})
    text = reply["result"]["messages"][0]["content"]["text"]
    assert "about 20 minutes" in text and "save_mix Friday" in text and "one vocal" in text


def test_prompt_needs_its_required_argument(talk):
    [reply] = talk({"id": 1, "method": "prompts/get", "params": {"name": "review_mix"}})
    assert reply["error"]["message"] == "review_mix needs name"


def test_unknown_argument_is_refused(talk, fake):
    text, failed = call(talk, "search_songs", words="carly")
    assert failed and "no argument words" in text and "query" in text
    assert fake.calls == []


# ------------------------------------------------------------------------- state

def test_state_names_key_and_describes_discs(talk):
    state, failed = call(talk, "get_state")
    assert not failed
    assert state["key"] == "Abm" and state["bpm_range"] == [60, 200]
    assert state["decks"][0] == {
        "deck": 1, "queued": None,
        "playing": {"name": "callmemaybebt", "type": "beat", "song": "Call Me Maybe",
                    "artist": "Carly Rae Jepsen"},
    }
    assert "mix" not in state


def test_game_errors_reach_the_agent(talk, fake):
    fake.fail = {"state": "FUSER is not answering"}
    text, failed = call(talk, "get_state")
    assert failed and text == "FUSER is not answering"


# ----------------------------------------------------------------------- library

def test_search_groups_discs_into_songs(talk):
    found, _ = call(talk, "search_songs", query="carly")
    assert found["total"] == 1
    song = found["songs"][0]
    assert (song["title"], song["genre"], song["key"], song["year"]) == \
        ("Call Me Maybe", "Pop", "G", 2012)
    assert song["discs"] == [
        {"name": "callmemaybebt", "type": "beat", "instrument": "Drums"},
        {"name": "callmemaybeld", "type": "lead", "instrument": "Female Vocals"},
    ]
    assert "owned" not in song


@pytest.mark.parametrize("args, titles", [
    ({}, ["bad guy", "Call Me Maybe", "Mr. Brightside"]),
    ({"genre": "Rock"}, ["Mr. Brightside"]),
    ({"key": "C#"}, ["Mr. Brightside"]),
    ({"bpm_min": 130}, ["bad guy", "Mr. Brightside"]),
    ({"bpm_max": 130}, ["Call Me Maybe"]),
    ({"limit": 1, "offset": 1}, ["Call Me Maybe"]),
    # G major's relative is E minor, not G minor, and its tempo is 120 against 135
    ({"near": "callmemaybe"}, []),
    # 135 is within 8% of 148? No: 9.6% away. And D flat against G minor clashes
    ({"near": "mrbrightside"}, []),
])
def test_search_filters(talk, args, titles):
    found, _ = call(talk, "search_songs", **args)
    assert [s["title"] for s in found["songs"]] == titles


def test_unowned_dlc_is_hidden(talk):
    found, _ = call(talk, "search_songs", query="kings")
    assert found == {"total": 0, "songs": []}


def test_library_fetched_once_per_session(fake):
    server = mcp.Server(fake, None, None)
    server._library()
    server._library()
    fake.session = "s2"
    server._library()
    assert fake.ops().count("discs") == 2


def test_library_saved_and_used_while_fuser_is_closed(fake):
    mcp.Server(fake, None, None)._library()
    closed = FakeBridge()
    closed.fail = {"discs": "FUSER is not answering", "state": "FUSER is not answering"}
    server = mcp.Server(closed, None, None)
    assert [s["title"] for s in server._library()][:1] == ["bad guy"]
    assert server._bpm_range == [60, 200]
    # It does not keep knocking while the game is closed
    server._library()
    assert closed.ops().count("discs") == 1


def test_no_library_at_all(talk, fake):
    fake.fail = {"discs": "FUSER is not answering"}
    text, failed = call(talk, "search_songs")
    assert failed and "not answering" in text


# -------------------------------------------------------------------------- tags

def test_tags_show_and_filter(talk):
    result, failed = call(talk, "tag_songs", songs=[
        {"id": "callmemaybe", "energy": 7, "style": "bubblegum pop"},
        {"id": "nope", "energy": 3}])
    assert not failed and result == {"tagged": 1, "unknown_ids": ["nope"]}
    found, _ = call(talk, "search_songs", energy_min=6)
    assert [s["title"] for s in found["songs"]] == ["Call Me Maybe"]
    assert found["songs"][0]["tags"] == {"energy": 7, "style": "bubblegum pop"}
    found, _ = call(talk, "search_songs", untagged=True)
    assert [s["title"] for s in found["songs"]] == ["bad guy", "Mr. Brightside"]


def test_tags_merge_and_clear(talk):
    call(talk, "tag_songs", songs=[{"id": "badguy", "energy": 5, "style": "whisper pop"}])
    call(talk, "tag_songs", songs=[{"id": "badguy", "energy": 6}])
    assert store.tags()["badguy"] == {"energy": 6, "style": "whisper pop"}
    call(talk, "tag_songs", songs=[{"id": "badguy", "clear": True}])
    assert "badguy" not in store.tags()


def test_tag_energy_range(talk):
    text, failed = call(talk, "tag_songs", songs=[{"id": "badguy", "energy": 11}])
    assert failed and "1 to 10" in text


# ------------------------------------------------------------------------- mixes

PLAN = {"key": "G", "bpm": 120, "steps": [
    {"bar": 0, "do": "drop", "deck": 1, "disc": "callmemaybebt"},
    {"bar": 0, "do": "drop", "deck": 2, "disc": "badguybs"},
    {"bar": 8, "do": "drop", "deck": 4, "disc": "callmemaybeld"},
]}


def test_plan_mix(talk):
    report, failed = call(talk, "plan_mix", **PLAN)
    assert not failed
    assert report["bars"] == 24 and report["songs"] == 2
    first, second = report["segments"]
    assert first["bars"] == [0, 8] and first["at"] == "0:00"
    assert first["decks"] == ["Call Me Maybe beat", "bad guy bass (-11%)", None, None]
    assert second["at"] == "0:16"
    assert "stretched too far: deck 2 -11% off its 135 BPM" in second["checks"]


def test_plan_mix_refuses_with_every_problem(talk):
    text, failed = call(talk, "plan_mix", key="G", bpm=500, steps=[
        {"bar": 0, "do": "drop", "deck": 5, "disc": "callmemaybebt"},
        {"bar": 4, "do": "drop", "deck": 1, "disc": "kingsandqueensld"},
        {"bar": 8, "do": "eject", "deck": 3}])
    assert failed
    for problem in ["outside the 60 to 200", "deck must be 1 to 4", "DLC you do not own",
                    "deck 3 is empty"]:
        assert problem in text


def test_plan_mix_bad_opening_key(talk):
    text, failed = call(talk, "plan_mix", key="Q", bpm=120, steps=PLAN["steps"])
    assert failed and "the opening key: not a key: 'Q'" in text


def test_save_load_and_list(talk):
    saved, failed = call(talk, "save_mix", name="Friday", **PLAN)
    assert not failed and saved["saved"] == "Friday" and saved["bars"] == 24
    loaded, _ = call(talk, "load_mix", name="Friday")
    assert loaded["steps"] == PLAN["steps"] and loaded["key"] == "G"
    listed, _ = call(talk, "list_mixes")
    assert listed["mixes"][0]["name"] == "Friday" and listed["mixes"][0]["steps"] == 3
    report, _ = call(talk, "plan_mix", mix="Friday")
    assert report["bars"] == 24


def test_save_refuses_a_broken_plan(talk):
    text, failed = call(talk, "save_mix", name="Broken", key="G", bpm=120, steps=[])
    assert failed and "no steps" in text
    assert store.list_mixes() == []


def test_play_mix_needs_the_game(talk, fake):
    fake.fail = {"ping": "FUSER is not answering"}
    text, failed = call(talk, "play_mix", **PLAN)
    assert failed and "not answering" in text


# ------------------------------------------------------------------------ live

def test_drop_converts_deck(talk, fake):
    result, failed = call(talk, "drop_disc", disc="mrbrightsidebs", deck=4)
    assert not failed
    assert fake.calls[-1] == ("drop", {"name": "mrbrightsidebs", "deck": 3, "queue": False})
    assert result["disc"]["song"] == "Mr. Brightside"


def test_unowned_dlc_is_refused(talk, fake):
    text, failed = call(talk, "drop_disc", disc="kingsandqueensld", deck=1)
    assert failed and "do not own" in text and "Kings and Queens" in text
    assert "drop" not in fake.ops()


def test_drop_unknown_disc(talk, fake):
    fake.fail = {"drop": "no disc named zzz"}
    text, failed = call(talk, "drop_disc", disc="zzz", deck=1)
    assert failed and "search_songs" in text


@pytest.mark.parametrize("deck", [0, 5, "2", True, None])
def test_bad_deck_never_reaches_fuser(talk, fake, deck):
    text, failed = call(talk, "eject", deck=deck)
    assert failed and "deck must be 1 to 4" in text
    assert fake.calls == []


@pytest.mark.parametrize("text, key, mode, name", [
    ("Ab", 8, 0, "Ab"), ("abm", 8, 1, "Abm"), ("F#m", 6, 1, "Gbm"),
    ("A minor", 9, 1, "Am"), ("Bb major", 10, 0, "Bb"), ("bb", 10, 0, "Bb"),
])
def test_set_key(talk, fake, text, key, mode, name):
    result, failed = call(talk, "set_key", key=text)
    assert not failed and result == {"key": name}
    assert fake.calls == [("set_key", {"key": key, "mode": mode})]


@pytest.mark.parametrize("text", ["H", "E#", "", "Abmm"])
def test_set_key_rejects(talk, fake, text):
    _, failed = call(talk, "set_key", key=text)
    assert failed and fake.calls == []


@pytest.mark.parametrize("name, args, op, sent", [
    ("drop_queue", {}, "drop_queue", {}),
    ("set_tempo", {"bpm": 128}, "set_tempo", {"bpm": 128}),
    ("mute_deck", {"deck": 2, "mute": True}, "mute", {"deck": 1, "on": True}),
    ("solo_deck", {"deck": 1, "solo": False}, "solo", {"deck": 0, "on": False}),
])
def test_simple_tools(talk, fake, name, args, op, sent):
    _, failed = call(talk, name, **args)
    assert not failed and fake.calls == [(op, sent)]


def test_get_set(talk):
    result, failed = call(talk, "get_set")
    assert not failed
    assert result == {
        "measure": 84,
        "requests": [
            {"state": "active", "wants": "a disc played on Synth", "starts": 82, "ends": 96,
             "measures_left": 12},
            {"state": "scheduled", "starts": 110, "ends": 122},
        ],
        "requests_settled": {"passed": 1, "failed": 0},
        "goals": [{"goal": "Include 2+ {Genre} discs", "done": 1, "of": 2}],
        "goals_done": 1,
        "crowd_meter_on": True,
        "crowd_meter": 30,
        "crowd": "medium",
        "crowd_warnings": 1,
        "freshness": 30,
        "score": 3000,
    }


def test_get_set_says_what_it_could_not_read(talk, fake, monkeypatch):
    monkeypatch.setitem(SET_STATE, "problems", ["freshness: SkillStat_Freshness not found"])
    monkeypatch.setitem(SET_STATE, "freshness", None)
    result, _ = call(talk, "get_set")
    assert result["unreadable"] == ["freshness: SkillStat_Freshness not found"]
    assert "freshness" not in result


@pytest.mark.parametrize("f, words", [
    ({"type": 0, "disc_type": 2}, "a loop disc"),
    ({"type": 1, "instrument": 7}, "a disc played on Horns"),
    ({"type": 2, "genre": 3}, "a Dance disc"),
    ({"type": 3, "years": 13}, "a disc from the 1990s"),
    ({"type": 5, "songs": ["callmemaybe"]}, "a disc from callmemaybe"),
])
def test_request_wants(f, words):
    from crystalize import music
    assert music.request_wants(f) == words


def test_goal_text_fills_disc_types():
    from crystalize import music
    assert music.goal_text("Drop a {bsType}") == "Drop a bass (green)"


def test_get_set_hides_the_meter_before_it_is_on(talk, monkeypatch):
    monkeypatch.setitem(SET_STATE, "show", {"score": 0, "has_meter": False})
    result, _ = call(talk, "get_set")
    assert result["crowd_meter_on"] is False
    assert not {"crowd_meter", "crowd", "crowd_warnings"} & set(result)
