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
