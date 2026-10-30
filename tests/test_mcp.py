"""The MCP server's protocol and tools, against a fake bridge instead of FUSER."""
import copy
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
        if op == "batch":
            return [self.call(c["op"], **c["args"]) for c in args["commands"]]
        self.calls.append((op, args))
        if op in self.fail:
            raise GameError(self.fail[op])
        # A copy each time, as FUSER's answers are, so one read never changes with the next
        return copy.deepcopy({"discs": DISCS, "state": STATE, "clock": {"beat": 3.98, "bpm": 120},
                              "set_state": SET_STATE}.get(op, {}))

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
    assert names[:5] == ["get_state", "get_set", "await_event", "search_songs", "tag_songs"]
    assert {"plan_mix", "play_mix", "stop_mix", "save_mix", "drop_disc"} <= set(names)
    assert [p["name"] for p in prompts["result"]["prompts"]] == \
        ["tag_library", "build_mix", "play_set", "review_mix"]


def test_every_tool_has_a_handler():
    server = mcp.Server(FakeBridge(), None, None)
    for tool in mcp.TOOLS + mcp.DEBUG_TOOLS:
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
    ({"year_min": 2010}, ["bad guy", "Call Me Maybe"]),
    ({"year_max": 2010}, ["Mr. Brightside"]),
    ({"year_min": 2010, "year_max": 2019}, ["bad guy", "Call Me Maybe"]),
    ({"instrument": "Bass"}, ["bad guy"]),  # Mr. Brightside's is a Bass Guitar
    ({"disc_type": "lead"}, ["Call Me Maybe"]),
    ({"instrument": "Drums", "disc_type": "lead"}, []),
    # G major's relative is E minor, not G minor, and its tempo is 120 against 135
    ({"near": "callmemaybe"}, []),
    # 135 is within 8% of 148? No: 9.6% away. And D flat against G minor clashes
    ({"near": "mrbrightside"}, []),
])
def test_search_filters(talk, args, titles):
    found, _ = call(talk, "search_songs", **args)
    assert [s["title"] for s in found["songs"]] == titles


def test_instrument_lists_only_its_discs(talk):
    found, _ = call(talk, "search_songs", instrument="Drums")
    assert [d["name"] for d in found["songs"][0]["discs"]] == ["callmemaybebt"]


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


def test_drop_waits_for_the_downbeat(talk, fake):
    # Deck 1 is playing, so the drop is timed to the next bar rather than sent at once
    call(talk, "drop_disc", disc="mrbrightsidebs", deck=4)
    assert fake.ops()[-3:] == ["state", "clock", "drop"]


def test_queue_goes_at_once(talk, fake):
    call(talk, "drop_disc", disc="mrbrightsidebs", deck=4, queue=True)
    assert fake.ops()[-1] == "drop" and "clock" not in fake.ops()
    assert fake.calls[-1] == ("drop", {"name": "mrbrightsidebs", "deck": 3, "queue": True})


def test_drop_queue_waits_for_the_downbeat(talk, fake):
    _, failed = call(talk, "drop_queue")
    assert not failed and fake.ops() == ["state", "clock", "drop_queue"]


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
    assert fake.calls[-1] == ("set_key", {"key": key, "mode": mode})
    assert fake.ops() == ["state", "clock", "set_key"]  # timed to the downbeat


def test_set_tempo_waits_for_the_downbeat(talk, fake):
    _, failed = call(talk, "set_tempo", bpm=128)
    assert not failed and fake.calls[-1] == ("set_tempo", {"bpm": 128})
    assert fake.ops() == ["state", "clock", "set_tempo"]


@pytest.mark.parametrize("text", ["H", "E#", "", "Abmm"])
def test_set_key_rejects(talk, fake, text):
    _, failed = call(talk, "set_key", key=text)
    assert failed and fake.calls == []


@pytest.mark.parametrize("name, args, op, sent", [
    ("mute_deck", {"deck": 2, "mute": True}, "mute", {"deck": 1, "on": True}),
    ("solo_deck", {"deck": 1, "solo": False}, "solo", {"deck": 0, "on": False}),
    ("eject", {"deck": 3}, "eject", {"deck": 2}),
    ("take_snapshot", {}, "snapshot", {}),
])
def test_simple_tools(talk, fake, name, args, op, sent):
    _, failed = call(talk, name, **args)
    # Timed to the downbeat, as drops are, so the change lands on the beat
    assert not failed and fake.ops() == ["state", "clock", op] and fake.calls[-1] == (op, sent)


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


def test_goal_text_fills_instruments_and_the_song():
    from crystalize import music
    assert music.goal_text('Drop {lpGuitar} from "{RequiredSong}"', "Rock the Casbah") == \
        'Drop guitar loop (yellow) from "Rock the Casbah"'
    assert music.goal_text("Smart {Transition} using only {Horns}") == \
        "Smart {Transition} using only {Horns}"  # not a disc type and instrument


def test_get_set_names_the_featured_song(talk, monkeypatch):
    goal = {"name": 'Drop {bsGuitar} from "{RequiredSong}"', "song": "mrbrightside",
            "performed": 0, "required": 1, "completed": False, "active": True}
    monkeypatch.setitem(SET_STATE, "goals", [goal])
    result, _ = call(talk, "get_set")
    assert result["goals"][0]["goal"] == 'Drop guitar bass (green) from "Mr. Brightside by The Killers"'


def test_get_set_hides_the_meter_before_it_is_on(talk, monkeypatch):
    monkeypatch.setitem(SET_STATE, "show", {"score": 0, "has_meter": False})
    result, _ = call(talk, "get_set")
    assert result["crowd_meter_on"] is False
    assert not {"crowd_meter", "crowd", "crowd_warnings"} & set(result)


def test_keep_alive_needs_four_discs(talk, fake):
    text, failed = call(talk, "keep_alive", discs=[
        {"disc": "callmemaybebt", "deck": 1}, {"disc": "mrbrightsidebs", "deck": 2},
        {"disc": "badguybs", "deck": 2}])
    assert failed and "at least 4" in text


def test_keep_alive_status_counts_decks_from_1(talk, fake, monkeypatch):
    from crystalize.player import Player
    monkeypatch.setattr(Player, "keep_alive_status",
                        lambda self: {"on": True, "last": {"disc": "badguybs", "deck": 1}})
    result, _ = call(talk, "get_state")
    assert result["keep_alive"]["last"] == {"disc": "badguybs", "deck": 2}


def test_goal_text_fills_the_genre_from_its_filter():
    from crystalize import music
    filters = [{"type": 0, "disc_type": 0, "genre": 0}, {"type": 2, "genre": 8}]
    assert music.goal_text("Include 2+ {Genre} discs", None, filters) == \
        "Include 2+ Rock discs"
    assert music.goal_text("Include 2+ {Genre} discs", None, [{"type": 2, "genre": 0}]) == \
        "Include 2+ {Genre} discs"  # FUSER has not picked one
    assert music.goal_text("Include 2+ {YearRange} discs", None, [{"type": 3, "years": 12}]) == \
        "Include 2+ 1980s discs"


# ------------------------------------------------------------------- events

def request(state, start=82, bars=14, instrument=5):
    return {"id": -1, "state": state, "start_measure": start, "bars": bars,
            "filter": {"type": 1, "instrument": instrument}}


def goal(name, completed=False, active=True):
    return {"name": name, "completed": completed, "active": active, "performed": 0,
            "required": 1}


def read(measure=84, requests=(), goals=(), meter=None):
    raw = {"measure": measure, "requests": list(requests), "goals": list(goals),
           "show": {"has_meter": meter is not None, "score": 0}}
    if meter is not None:
        raw["crowd"] = {"CurHealth": meter, "MaxHealth": 100}
    return raw


def events(before, after, below=30):
    server = mcp.Server(FakeBridge(), None, None)
    return [e["event"] for e in server._events(before, after, below)]


@pytest.mark.parametrize("before, after, expected", [
    (read(), read(requests=[request(0)]), ["request_coming"]),
    (read(requests=[request(0)]), read(requests=[request(1)]), ["request_started"]),
    (read(requests=[request(1)]), read(requests=[request(2)]), ["request_passed"]),
    (read(requests=[request(1)]), read(requests=[request(4)]), ["request_failed"]),
    # A request settled before the agent first looked is history, not news
    (None, read(requests=[request(2)]), []),
    (read(measure=90, requests=[request(1)]), read(measure=93, requests=[request(1)]),
     ["request_ending"]),  # 82 + 14 = 96, three measures left
    (read(goals=[goal("Drop a {btType}")]), read(goals=[goal("Drop a {btType}", True)]),
     ["goal_done"]),
    (read(goals=[goal("a")]), read(goals=[goal("a", active=False)]), ["goal_ended"]),
    (read(goals=[goal("a", True)]), read(goals=[goal("a", True), goal("b")]), ["goal_started"]),
    (read(), read(meter=50), ["meter_on"]),
    (read(meter=31), read(meter=29), ["meter_low"]),
    (read(meter=29), read(meter=28), []),  # already low: said once, on the way down
    (read(goals=[goal("a")]), {"measure": None, "requests": None, "goals": None},
     ["set_ended"]),
    (read(requests=[request(1)]), read(requests=[request(1)]), []),
    # Still readable on the results screen: FUSER counting it out is the end
    (read(), {**read(), "show": {"ending": True}}, ["set_ended"]),
    ({**read(), "show": {"ending": True}}, {**read(), "show": {"ending": True}}, []),
])
def test_events(before, after, expected):
    assert events(before, after) == expected


def test_ending_is_said_once():
    server = mcp.Server(FakeBridge(), None, None)
    r = read(measure=93, requests=[request(1)])
    assert [e["event"] for e in server._events(r, r, 30)] == ["request_ending"]
    assert server._events(r, r, 30) == []


def test_await_event_returns_what_happened_meanwhile(fake, monkeypatch):
    # The agent saw the set with a request coming; it started while the agent was busy
    server = mcp.Server(fake, None, None)
    monkeypatch.setitem(SET_STATE, "requests", [request(0)])
    server._tool_get_set({})
    monkeypatch.setitem(SET_STATE, "requests", [request(1)])
    result = server._tool_await_event({"seconds": 0})
    assert result["events"] == [{"event": "request_started", "state": "active",
                                 "wants": "a disc played on Synth", "starts": 82, "ends": 96,
                                 "measures_left": 12}]
    assert result["set"]["requests"][0]["wants"] == "a disc played on Synth"


def test_reading_the_set_holds_the_keep_alive_while_a_request_is_open(fake, monkeypatch):
    server = mcp.Server(fake, None, None)
    held = []
    monkeypatch.setattr(server.player, "hold_for_requests", held.append)
    monkeypatch.setitem(SET_STATE, "requests", [request(1), request(0, start=90)])
    server._tool_get_set({})
    monkeypatch.setitem(SET_STATE, "requests", [request(2), request(0, start=90)])
    server._tool_await_event({"seconds": 0})
    assert held == [12, 0]  # 82 + 14 - 84; a request still coming holds nothing


def test_await_event_times_out_with_the_set(fake, monkeypatch):
    monkeypatch.setattr(mcp, "AWAIT_POLL_SECONDS", 0.01)
    server = mcp.Server(fake, None, None)
    server._tool_get_set({})
    result = server._tool_await_event({"seconds": 0.05})
    assert result["events"] == [] and result["set"]["measure"] == 84


def test_await_event_rides_out_a_missed_read(fake, monkeypatch):
    monkeypatch.setattr(mcp, "AWAIT_POLL_SECONDS", 0.01)
    server = mcp.Server(fake, None, None)
    server._tool_get_set({})
    real, misses = fake.call, [1]

    def call(op, **args):
        if op == "set_state" and misses[0]:
            misses[0] -= 1
            raise GameError("FUSER took the command but did not answer")
        return real(op, **args)
    fake.call = call
    assert server._tool_await_event({"seconds": 0})["set"]["measure"] == 84


def test_get_set_gives_stars_and_points(talk, monkeypatch):
    monkeypatch.setitem(SET_STATE, "show", {"score": 23500, "has_meter": True, "ending": True,
        "results": {"stars": 2.5371, "points": {"ShowGoalPoints": 18000,
                                                "PerfectTimingBonusPoints": 800,
                                                "LateTimingBonusPoints": None}}})
    result, _ = call(talk, "get_set")
    assert result["stars"] == 2.54 and result["ended"] is True
    assert result["points"] == {"goals": 18000, "perfect_timing": 800}


def test_request_names_its_song_by_title(talk, monkeypatch):
    monkeypatch.setitem(SET_STATE, "requests", [{"state": 1, "start_measure": 90, "bars": 14,
        "filter": {"type": 5, "songs": ["mrbrightside"]}}])
    result, _ = call(talk, "get_set")
    assert result["requests"][0]["wants"] == "a disc from Mr. Brightside by The Killers"


def test_pickup_drop_needs_pickups(talk, fake):
    # The fixture's discs carry none, as some older custom songs do
    text, failed = call(talk, "drop_disc", disc="mrbrightsidebs", deck=1, on_pickup=True)
    assert failed and "no pickups" in text and "drop" not in fake.ops()


def test_goal_text_says_only_means_the_whole_mix():
    from crystalize import music
    assert music.goal_text("Include only 1 {btType}") == "Include only 1 disc, a beat (blue)"
    assert music.goal_text("Include only 2 {lpType}") == "Include only 2 discs, all loop (yellow)"
    assert music.goal_text("Include only 3 discs") == "Include only 3 discs"


def test_play_set_prompt_needs_no_arguments(talk):
    [reply] = talk({"id": 1, "method": "prompts/get", "params": {"name": "play_set"}})
    text = reply["result"]["messages"][0]["content"]["text"]
    assert "await_event" in text and "on_pickup" in text and "keep_alive" in text


def test_keep_alive_fill_picks_discs_that_fit_the_session(fake, monkeypatch):
    # The session is G#m at 120 (STATE): the closest tempo comes first, a clashing key
    # and DLC not owned never
    def song(id_, bpm, key, genre="Pop", owned=True):
        return {"id": id_, "title": id_, "artist": None, "bpm": bpm, "key": key, "genre": genre,
                "year": 2000, "owned": owned,
                "discs": [{"name": id_ + "lp", "type": "loop"}, {"name": id_ + "ld", "type": "lead"}]}
    library = [song("slower", 113, "G#m"), song("exact", 120, "G#m"), song("relative", 121, "B"),
               song("clash", 120, "C"), song("dlc", 120, "G#m", owned=False),
               song("rock", 120, "G#m", genre="Rock")]
    server = mcp.Server(fake, None, None)
    monkeypatch.setattr(server, "_library", lambda: library)
    pool = server._fill([{"deck": 3, "disc_type": "loop"},
                         {"deck": 4, "disc_type": "lead", "genre": "Rock"}], set())
    assert pool == [("exactlp", 2), ("rocklp", 2), ("relativelp", 2), ("slowerlp", 2),
                    ("rockld", 3)]


def test_a_goal_ended_undone_is_named(fake, monkeypatch):
    server = mcp.Server(fake, None, None)
    before = read(goals=[goal("Drop a {btType}"), goal("a", True, active=False)])
    after = {**read(goals=[goal("Drop a {btType}", active=False), goal("a", True, active=False)]),
             "show": {"ending": True, "results": {"stars": 3.5}}}
    ended = server._events(before, after, 30)[-1]
    assert ended == {"event": "set_ended", "stars": 3.5, "missed": ["Drop a beat (blue)"]}
    assert server._set_view(after)["goals_missed"] == ["Drop a beat (blue)"]


def test_get_set_shows_what_could_stop_actions_counting():
    server = mcp.Server(FakeBridge(), None, None)
    view = server._set_view({**read(meter=100), "freshness": {
        "value": 0.85, "min": 0, "max": 1, "zone": [0.4, 0.7], "in_zone": False},
        "crowd": {"CurHealth": 100, "MaxHealth": 100, "ActionCount": 6,
                  "OveractiveThreshold": 3, "OveractivityEnabled": True, "DrainPaused": True}})
    assert view["freshness_zone"] == [40, 70] and view["in_freshness_zone"] is False
    assert view["crowd_actions"] == 6 and view["overactive"] is True
    assert view["meter_paused"] is True


def test_a_set_is_logged_from_its_first_read_to_its_end(fake, monkeypatch, tmp_path):
    monkeypatch.setattr(mcp, "AWAIT_POLL_SECONDS", 0.01)
    server = mcp.Server(fake, None, None)
    server._tool_get_set({})
    server.player._send([("drop", {"name": "a", "deck": 2, "queue": False})])
    monkeypatch.setitem(SET_STATE, "show", {**SET_STATE["show"], "ending": True})
    server._tool_await_event({"seconds": 0})
    assert not server._log.open
    lines = [json.loads(l) for l in open(server._log.path, encoding="utf-8")]
    assert lines[0]["set"]["measure"] == 84
    assert lines[1]["sent"] == [["drop", {"name": "a", "deck": 2, "queue": False}]]
    assert {"event": "set_ended"} in [l["set"].get("event") for l in lines if "set" in l]


def test_debug_tools_only_with_debug(fake):
    def talk(debug, *requests):
        lines = "".join(json.dumps({"jsonrpc": "2.0", **r}) + "\n" for r in requests)
        writer = io.BytesIO()
        mcp.Server(fake, io.BytesIO(lines.encode()), writer, debug=debug).run()
        return [json.loads(line) for line in writer.getvalue().decode().splitlines()]

    lua = {"id": 2, "method": "tools/call",
           "params": {"name": "debug_lua", "arguments": {"code": "return 1"}}}
    tools, refused = talk(False, {"id": 1, "method": "tools/list"}, lua)
    assert "debug_lua" not in [t["name"] for t in tools["result"]["tools"]]
    assert "unknown tool" in refused["error"]["message"] and fake.calls == []

    tools, ran = talk(True, {"id": 1, "method": "tools/list"}, lua)
    assert {"debug_read", "debug_lua"} <= {t["name"] for t in tools["result"]["tools"]}
    assert not ran["result"]["isError"] and fake.calls == [("lua", {"code": "return 1"})]


def test_go_to_stops_what_plays_and_forgets_the_set(fake, monkeypatch):
    server = mcp.Server(fake, None, None)
    server._seen = {"measure": 3}
    went = []
    monkeypatch.setattr(mcp.launch, "go_to", lambda bridge, *a, **k: went.append(a) or {"at": "menu"})
    assert server._tool_go_to({"where": "menu"}) == {"at": "menu"}
    assert went == [("menu", None, None)] and server._seen is None


INSTRUMENTS = [{"name": "DrumCanvas2", "title": "Electro Funk Drums"},
               {"name": "Banjo", "title": "Banjo"}]


@pytest.fixture
def stage(fake, monkeypatch):
    """A set featuring Electro Funk Drums, and the performances started, as (name, bars)."""
    from crystalize.player import Player
    played = []
    monkeypatch.setattr(Player, "perform", lambda self, name, bars: played.append((name, bars)))
    real = fake.call

    def call(op, **args):
        if op == "instruments":
            return copy.deepcopy(INSTRUMENTS)
        if op == "set_state":
            return {**real(op, **args), "instruments": [INSTRUMENTS[0]]}
        return real(op, **args)
    fake.call = call
    return played


def test_perform_plays_the_featured_instrument(talk, stage):
    result, failed = call(talk, "perform", bars=4)
    assert not failed and result["performing"]["title"] == "Electro Funk Drums"
    assert stage == [("DrumCanvas2", 4)]


def test_perform_takes_an_instrument_by_title(talk, stage):
    call(talk, "perform", bars=8, instrument="banjo")
    assert stage == [("Banjo", 8)]


def test_perform_names_the_instruments_there_are(talk, stage):
    text, failed = call(talk, "perform", bars=4, instrument="Theremin")
    assert failed and "Electro Funk Drums, Banjo" in text and stage == []


def test_perform_needs_bars(talk, stage):
    text, failed = call(talk, "perform")
    assert failed and "bars is 1 to" in text and stage == []


def test_perform_stop(talk, monkeypatch):
    from crystalize.player import Player
    stopped = []
    monkeypatch.setattr(Player, "stop_perform", lambda self: stopped.append(True))
    result, _ = call(talk, "perform", stop=True)
    assert result == {"performing": False} and stopped


def test_goal_text_fills_the_featured_instrument():
    from crystalize import music
    assert music.goal_text("Perform on {CustomInstrument} Instrument", None, None,
                           "Electro Funk Drums") == "Perform on Electro Funk Drums Instrument"
