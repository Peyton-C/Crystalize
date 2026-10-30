"""Plans: checking, the timeline, every check, and the schedule the player follows."""
import pytest

from crystalize import music
from crystalize.mixes import Plan


def disc(name, song, type_, instrument=None, bpm=120, key=7, mode=0, transposes=None,
         has_major=True, has_minor=True, owned=True):
    return {"name": name, "type": type_, "instrument": instrument, "song": song.title(),
            "artist": "x", "song_id": song, "owned": owned, "bpm": bpm, "key": key, "mode": mode,
            "transposes": transposes or [0] * 12, "has_major": has_major, "has_minor": has_minor}


DISCS = {d["name"]: d for d in [
    disc("abt", "a", "beat", "Drums"), disc("abs", "a", "bass", "Bass"),
    disc("alp", "a", "loop", "Guitar"), disc("ald", "a", "lead", "Female Vocals"),
    disc("bbt", "b", "beat", "Drums"), disc("bbs", "b", "bass", "Bass"),
    disc("bld", "b", "lead", "Male Vocals"), disc("cld", "c", "lead", "Saxophone"),
    disc("slow", "s", "bass", "Bass", bpm=90),
    disc("shifty", "t", "lead", "Vocal", transposes=[4] * 12),
    disc("brightloop", "l", "loop", "Sampler", transposes=[4] * 12),
    disc("darkloop", "d", "loop", "Sampler", transposes=[-4] * 12),
    disc("lowvocal", "v", "lead", "Vocal", transposes=[-3] * 12),
    disc("majonly", "m", "loop", "Synth", has_minor=False),
]}


def plan(steps, key="G", bpm=120, **more):
    return Plan({"key": key, "bpm": bpm, "steps": [
        {"bar": s[0], "do": s[1], **({"deck": s[2]} if len(s) > 2 else {}),
         **({"disc": s[3]} if len(s) > 3 else {})} if isinstance(s, tuple) else s
        for s in steps], **more}, DISCS, [60, 200])


def checks_at(report, bar):
    return next(s for s in report["segments"] if s["bars"][0] == bar).get("checks", [])


FULL = [(0, "drop", 1, "abt"), (0, "drop", 2, "bbs"), (0, "drop", 3, "alp"), (0, "drop", 4, "bld")]


def test_clean_layers_have_no_checks():
    report = plan(FULL, end_bar=16).report()
    assert report["segments"][0].get("checks") is None
    assert report["checks"] == []
    assert report["length"] == "0:32" and report["songs"] == 2


@pytest.mark.parametrize("steps, check", [
    (FULL + [(8, "drop", 3, "ald")], "two vocals: decks 3 and 4"),
    ([(0, "drop", 1, "abs"), (0, "drop", 2, "ald")], "no beat"),
    ([(0, "drop", 1, "abt"), (0, "drop", 2, "ald")], "no bass"),
    (FULL + [(8, "drop", 1, "abs")], "two basses: decks 1 and 2"),
    ([(0, "drop", 1, "abt"), (0, "drop", 2, "abs"), (0, "drop", 3, "ald")], "one song"),
    (FULL + [(8, "drop", 2, "slow")], "stretched too far: deck 2 +33% off its 90 BPM"),
    (FULL + [(8, "drop", 4, "shifty")], "pitched far: deck 4 +4 semitones on a vocal, which shows it most"),
    (FULL + [(8, "drop", 3, "brightloop")], "pitched far: deck 3 +4 semitones, mild on a loop"),
    (FULL + [(8, "mute", 1), (8, "mute", 2), (8, "mute", 3), (8, "mute", 4)], "silence"),
])
def test_segment_checks(steps, check):
    report = plan(steps).report()
    found = [c for seg in report["segments"] for c in seg.get("checks", [])]
    assert any(c.startswith(check) for c in found), found


@pytest.mark.parametrize("name, deck", [("darkloop", 3), ("lowvocal", 4)])
def test_down_is_allowed_further_than_up(name, deck):
    report = plan(FULL + [(8, "drop", deck, name)]).report()
    assert not any("pitched far" in c for c in checks_at(report, 8))


def test_saxophone_lead_is_not_a_vocal():
    report = plan(FULL + [(8, "drop", 3, "cld")]).report()
    assert not any("two vocals" in c for c in checks_at(report, 8))


def test_muted_deck_does_not_count():
    report = plan(FULL + [(8, "drop", 3, "ald"), (8, "mute", 4)]).report()
    assert not any("two vocals" in c for c in checks_at(report, 8))
    assert report["segments"][1]["decks"][3] == "muted B lead"


def test_mode_version_missing():
    report = plan([*FULL[:3], (0, "drop", 4, "majonly")], key="Gm").report()
    assert "no minor version: deck 4" in report["segments"][0]["checks"]


def test_whole_mix_checks():
    report = plan(FULL + [
        (6, "eject", 3),
        {"bar": 48, "do": "key", "key": "Db"},
        {"bar": 48, "do": "tempo", "bpm": 140},
        (56, "drop", 3, "alp"),
        (64, "drop", 3, "alp"),
    ]).report()
    checks = report["checks"]
    assert "bar 6: eject is off the phrase, which comes in 4s and 8s" in checks
    assert "bars 6 to 48: nothing changes for 42 bars" in checks
    assert "alp is dropped 3 times" in checks
    assert "bar 48: the key change from G to Db clashes" in checks
    assert "bar 48: the tempo jumps 17%, from 120 to 140" in checks
    assert report["key_changes"] == [{"bar": 48, "from": "G", "to": "Db", "relation": "clash"}]


def test_energy_from_tags():
    p = Plan({"key": "G", "bpm": 120, "steps": [
        {"bar": 0, "do": "drop", "deck": 1, "disc": "abt"},
        {"bar": 8, "do": "drop", "deck": 1, "disc": "bbt"}]},
        DISCS, None, {"a": {"energy": 3}, "b": {"energy": 8}})
    report = p.report()
    assert [s["energy"] for s in report["segments"]] == [3, 8]
    assert "bar 8: energy jumps from 3 to 8" in report["checks"]


def test_time_follows_tempo_changes():
    report = plan([(0, "drop", 1, "abt"), {"bar": 8, "do": "tempo", "bpm": 60}], end_bar=16).report()
    assert [s["at"] for s in report["segments"]] == ["0:00", "0:16"]
    assert report["length"] == "0:48"


def test_from_and_to_bar_limit_segments_not_checks():
    report = plan(FULL + [(8, "drop", 3, "ald"), (16, "eject", 3), (24, "drop", 3, "alp")])\
        .report(from_bar=16, to_bar=24)
    assert [s["bars"] for s in report["segments"]] == [[16, 24]]


@pytest.mark.parametrize("steps, problem", [
    ([], "no steps"),
    ([(0, "eject", 1)], "deck 1 is empty, so it cannot eject"),
    ([(0, "drop", 1, "abt"), (0, "drop", 1, "bbt")], "deck 1 is dropped on or ejected twice"),
    ([(0, "dance", 1)], "do must be one of"),
    ([(-1, "drop", 1, "abt")], "bar must be a whole number"),
    ([{"bar": 0, "do": "tempo", "bpm": 300}], "outside the 60 to 200"),
])
def test_refused(steps, problem):
    with pytest.raises(ValueError, match=problem):
        plan(steps)


def test_end_bar_must_follow_last_step():
    with pytest.raises(ValueError, match="end_bar"):
        plan([(8, "drop", 1, "abt")], end_bar=8)


def test_schedule_orders_drops_before_key_and_tempo():
    p = plan([(0, "drop", 2, "abt"), {"bar": 0, "do": "key", "key": "Am"},
              (8, "eject", 2), (8, "drop", 1, "bbt")], key="G", bpm=124)
    schedule = p.schedule()
    assert schedule[0] == (0, [("drop", {"name": "abt", "deck": 1, "queue": False}),
                               ("set_key", {"key": 7, "mode": 0}),
                               ("set_key", {"key": 9, "mode": 1}),
                               ("set_tempo", {"bpm": 124})])
    assert schedule[1] == (8, [("drop", {"name": "bbt", "deck": 0, "queue": False}),
                               ("eject", {"deck": 1})])


@pytest.mark.parametrize("key, mode, expected", [
    (0, 0, (8, "B")), (7, 0, (9, "B")), (5, 0, (7, "B")), (2, 0, (10, "B")),
    (9, 1, (8, "A")), (4, 1, (9, "A")), (8, 1, (1, "A")), (11, 0, (1, "B")),
])
def test_camelot(key, mode, expected):
    assert music.camelot(key, mode) == expected


@pytest.mark.parametrize("a, b, relation", [
    ((0, 0), (0, 0), "same"), ((0, 0), (9, 1), "relative"), ((0, 0), (7, 0), "fifth"),
    ((0, 0), (5, 0), "fifth"), ((0, 0), (2, 0), "clash"), ((0, 0), (0, 1), "clash"),
])
def test_key_relation(a, b, relation):
    assert music.key_relation(a, b) == relation


@pytest.mark.parametrize("own, bpm, percent", [
    (120, 120, 0.0), (120, 126, 5.0), (70, 140, 0.0), (150, 75, 0.0), (90, 120, 33.3),
])
def test_stretch(own, bpm, percent):
    assert music.stretch_percent(own, bpm) == percent


# ------------------------------------------------------------- what used to be live only

def with_pickups(**more):
    return {**DISCS, "pick": disc("pick", "p", "lead", "Saxophone") | {
        "pickups": [2.0, 66.0], "loop_beats": 128.0, **more}}


def test_solo_leaves_only_that_deck_heard():
    report = plan(FULL + [(8, "drop", 3, "ald"), (8, "solo", 4), (16, "unsolo", 4)]).report()
    assert not any("two vocals" in c for c in checks_at(report, 8))
    assert "two vocals: decks 3 and 4" in checks_at(report, 16)
    assert report["segments"][1]["decks"][3] == "soloed B lead"
    assert ("solo", {"deck": 3, "on": True}) in dict(plan(FULL + [(8, "solo", 4)]).schedule())[8]


def test_a_fade_stops_counting_only_once_it_has_ended():
    report = plan(FULL + [(8, "drop", 3, "ald"), {"bar": 8, "do": "fade", "deck": 4, "beats": 8}]).report()
    assert "two vocals: decks 3 and 4" in checks_at(report, 8)
    assert report["segments"][1]["bars"] == [8, 10]
    assert not any("two vocals" in c for c in checks_at(report, 10))
    assert report["segments"][2]["decks"][3] == "volume 0 B lead"


def test_fade_schedule_says_where_it_starts():
    p = plan(FULL + [{"bar": 8, "do": "fade", "deck": 1},
                     {"bar": 12, "do": "fade", "deck": 1, "to": 0.5}])
    schedule = dict(p.schedule())
    assert schedule[8] == [("fade", {"deck": 0, "start": 1, "to": 0, "beats": 4})]
    assert schedule[12] == [("fade", {"deck": 0, "start": 0, "to": 0.5, "beats": 4})]


def test_a_drop_brings_a_faded_deck_back_as_the_player_does():
    report = plan(FULL + [{"bar": 8, "do": "fade", "deck": 4}, (12, "drop", 4, "cld")]).report()
    assert report["segments"][-1]["decks"][3] == "C lead"


@pytest.mark.parametrize("steps, problem", [
    ([{"bar": 8, "do": "fade", "deck": 1, "beats": 16}, (8 + 2, "drop", 1, "bbt")], "fading until bar 12"),
    ([{"bar": 8, "do": "fade", "deck": 1, "to": 1}], "already at volume 1"),
    ([{"bar": 8, "do": "fade", "deck": 1, "to": 2}], "to is from 0 to 1"),
    ([{"bar": 8, "do": "fade", "deck": 1, "beats": 0}], "beats is 1 to"),
    ([{"bar": 8, "do": "fx", "deck": 1}], "effect slot is empty"),
    ([{"bar": 8, "do": "fx", "deck": 9, "fx": "Flanger"}], "deck must be 1 to 4"),
    ([{"bar": 8, "do": "riser", "mode": "manual"}], "mode is one of"),
    ([{"bar": 8, "do": "riser"}, {"bar": 10, "do": "riser"}], "still playing"),
    ([{"bar": 8, "do": "drop_cue"}], "nothing is cued"),
    ([{"bar": 8, "do": "swap", "deck": 2, "disc": "abs"}, {"bar": 8, "do": "drop_cue"}], "drop_cue would land"),
    ([{"bar": 7, "do": "cue", "deck": 2, "disc": "cld"}, {"bar": 8, "do": "swap", "deck": 2, "disc": "abs"}], "over the cue"),
    ([{"bar": 8, "do": "perform"}], "bars is 1 to"),
    ([{"bar": 8, "do": "perform", "bars": 4}, {"bar": 13, "do": "perform", "bars": 4}], "needs until bar 15"),
    ([{"bar": 8, "do": "record", "deck": 1, "as": "mine"}, (9, "drop", 2, "mine")], "not recorded until bar 11"),
    ([{"bar": 8, "do": "record", "deck": 1, "as": "abt"}], "already the name"),
    ([{"bar": 8, "do": "drop", "deck": 2, "disc": "cld", "on_pickup": True}], "no pickups"),
])
def test_new_steps_refused(steps, problem):
    with pytest.raises(ValueError, match=problem):
        plan(FULL + steps)


def test_swap_cues_a_bar_ahead_and_puts_the_old_disc_in_the_cue():
    p = plan(FULL + [{"bar": 8, "do": "swap", "deck": 4, "disc": "cld"}, {"bar": 16, "do": "drop_cue"}])
    schedule = dict(p.schedule())
    assert schedule[7] == [("drop", {"name": "cld", "deck": 3, "queue": True})]
    assert schedule[8] == [("hot_swap", {"deck": 3})]
    assert schedule[16] == [("drop_queue", {})]
    decks = p.report()["segments"]
    assert decks[1]["decks"][3] == "C lead" and decks[2]["decks"][3] == "B lead"


def test_cue_then_drop_cue():
    p = plan(FULL[:2] + [{"bar": 4, "do": "cue", "deck": 3, "disc": "alp"},
                         {"bar": 4, "do": "cue", "deck": 4, "disc": "bld"},
                         {"bar": 8, "do": "drop_cue"}])
    report = p.report()
    assert report["segments"][1]["decks"][2] is None
    assert report["segments"][2]["decks"][2:] == ["A loop", "B lead"]
    assert dict(p.schedule())[4] == [("drop", {"name": "alp", "deck": 2, "queue": True}),
                                     ("drop", {"name": "bld", "deck": 3, "queue": True})]


def test_a_cue_landing_on_a_muted_deck_is_warned_of():
    report = plan(FULL + [(8, "mute", 4), {"bar": 8, "do": "cue", "deck": 4, "disc": "cld"},
                          {"bar": 12, "do": "drop_cue"}]).report()
    assert any("deck 4, which is silent" in c for c in report["checks"])


def test_fx_stays_in_its_slot_and_shows_while_on():
    p = plan(FULL + [{"bar": 8, "do": "fx", "deck": 1, "fx": "Flanger"},
                     {"bar": 12, "do": "fx", "deck": 1, "on": False},
                     {"bar": 16, "do": "fx", "fx": "Delay"}])
    assert dict(p.schedule())[8] == [("fx", {"deck": 0, "name": "Flanger", "on": True})]
    assert dict(p.schedule())[12] == [("fx", {"deck": 0, "name": None, "on": False})]
    assert dict(p.schedule())[16] == [("fx", {"deck": None, "name": "Delay", "on": True})]
    segments = p.report()["segments"]
    assert "Flanger" in segments[1]["decks"][0] and "Flanger" not in segments[2]["decks"][0]
    assert segments[3]["master_fx"] == "Delay"


def test_snapshot_is_no_change():
    report = plan(FULL + [{"bar": 16, "do": "snapshot"}, {"bar": 32, "do": "eject", "deck": 1}]).report()
    assert "bars 0 to 32: nothing changes for 32 bars" not in report["checks"]
    report = plan(FULL + [{"bar": 20, "do": "snapshot"}, {"bar": 40, "do": "eject", "deck": 1}]).report()
    assert "bars 0 to 40: nothing changes for 40 bars" in report["checks"]


def test_riser_empties_the_decks_and_lands_what_is_cued():
    report = plan(FULL + [{"bar": 8, "do": "cue", "deck": 1, "disc": "bbt"},
                          {"bar": 8, "do": "riser", "mode": "steady"}]).report()
    by_bar = {s["bars"][0]: s for s in report["segments"]}
    assert by_bar[8]["riser"] == "building" and by_bar[8]["decks"][0] == "A beat"
    assert by_bar[11]["riser"] == "empty" and by_bar[11]["decks"] == [None] * 4
    assert "checks" not in by_bar[11]  # not silence: the riser's own sound fills it
    assert by_bar[12]["decks"] == ["B beat", None, None, None]
    assert "riser" not in by_bar[12]


def test_riser_with_nothing_cued_brings_the_mix_back():
    report = plan(FULL + [{"bar": 8, "do": "riser", "mode": "steady"}]).report()
    last = report["segments"][-1]
    assert last["bars"][0] == 12 and last["decks"][0] == "A beat" and last["decks"][3] == "B lead"
    assert not any("dropped" in c for c in report["checks"])


def test_a_step_inside_a_riser_is_warned_of():
    report = plan(FULL + [{"bar": 8, "do": "riser", "mode": "steady"}, (10, "drop", 3, "cld")]).report()
    assert any(c.startswith("bar 10: drop lands inside the riser from bar 8") for c in report["checks"])


def test_smart_riser_moves_to_the_cued_lead():
    report = plan(FULL + [{"bar": 8, "do": "cue", "deck": 2, "disc": "slow"},
                          {"bar": 8, "do": "cue", "deck": 4, "disc": "cld"},
                          {"bar": 8, "do": "riser"}], key="C").report()
    assert report["segments"][-1]["key"] == "G"
    assert any("smart riser moves the session to G and 120 BPM" in c for c in report["checks"])


def test_random_riser_says_the_checks_may_not_hold():
    report = plan(FULL + [{"bar": 8, "do": "riser", "mode": "random"}]).report()
    assert any("random riser" in c for c in report["checks"])


def test_end_bar_must_clear_a_riser():
    with pytest.raises(ValueError, match="after the last step, 12"):
        plan(FULL + [{"bar": 8, "do": "riser"}], end_bar=12)


def test_riser_goes_after_the_cues_of_its_bar():
    p = plan(FULL + [{"bar": 8, "do": "riser", "mode": "steady"},
                     {"bar": 8, "do": "cue", "deck": 1, "disc": "bbt"}])
    assert [op for op, _ in dict(p.schedule())[8]] == ["drop", "riser"]


def test_perform_starts_loading_ahead_of_its_bar():
    p = plan(FULL + [{"bar": 8, "do": "perform", "bars": 4, "instrument": "Banjo"}])
    assert dict(p.schedule())[6] == [("perform", {"instrument": "Banjo", "start_bar": 8, "bars": 4})]


def test_record_lands_on_its_deck_and_drops_by_its_name():
    p = plan(FULL[:1] + [(0, "drop", 3, "alp"),
                         {"bar": 8, "do": "record", "instrument": "Synth Bass", "deck": 2, "as": "mine"},
                         (12, "drop", 4, "mine")])
    by_bar = {s["bars"][0]: s for s in p.report()["segments"]}
    assert by_bar[8]["decks"][1] is None
    assert by_bar[10]["decks"][1] == "recorded Synth Bass bass"
    assert by_bar[12]["decks"][3] == "recorded Synth Bass bass"
    assert "checks" not in by_bar[10] or "no bass" not in by_bar[10]["checks"]
    schedule = dict(p.schedule())
    assert schedule[6] == [("record", {"instrument": "Synth Bass", "start_bar": 8, "queue": False,
                                       "name": "mine", "deck": 1})]
    assert schedule[12] == [("drop", {"name": "mine", "deck": 3, "queue": False})]
    assert p.report()["songs"] == 1  # a recording is no song


def test_recording_with_nothing_pitched_warns_of_the_key():
    report = plan([(0, "drop", 1, "abt"),
                   {"bar": 8, "do": "record", "deck": 2, "as": "mine"}]).report()
    assert any("moves the session to its instrument's own key" in c for c in report["checks"])


def test_pickup_drop_queues_then_lands_on_the_pickup():
    p = Plan({"key": "G", "bpm": 120, "steps": [
        {"bar": 0, "do": "drop", "deck": 1, "disc": "abt"},
        {"bar": 8, "do": "drop", "deck": 4, "disc": "pick", "on_pickup": True}]},
        with_pickups(), [60, 200])
    assert dict(p.schedule())[8] == [
        ("drop", {"name": "pick", "deck": 3, "queue": True}),
        ("pickup", {"commands": [("drop_queue", {})], "pickups": [2.0, 66.0], "loop_beats": 128.0})]
    assert any("waits for its next pickup, up to 16 bars" in c for c in p.report()["checks"])


def test_a_pickup_drop_would_land_other_cues_too():
    with pytest.raises(ValueError, match="already cued"):
        Plan({"key": "G", "bpm": 120, "steps": [
            {"bar": 0, "do": "cue", "deck": 1, "disc": "abt"},
            {"bar": 8, "do": "drop", "deck": 4, "disc": "pick", "on_pickup": True}]},
            with_pickups(), [60, 200])
