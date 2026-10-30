import pytest

from crystalize import launch
from crystalize.bridge import GameError

SETS = [[{"id": "M_0010", "name": "SET {Num}: Opener"}],
        [{"id": "M_0161", "name": "SET {Num}: Mute Point"}, None], [], [], [], []]


class FakeGame:
    """FUSER as the launch steps found it: a sign-in that takes a few reads, a map load
    that leaves one read unanswered, and a flow that waits to be walked."""

    def __init__(self, screen=3, title=9):
        self.state = {"screen": screen, "title": title, "mode": 9, "flow": None,
                      "flow_object": "FreestyleFlowMgr menu"}
        self.calls = []
        self._signing = 0
        self._loading = 0

    def call(self, op, **args):
        self.calls.append((op, args))
        if op == "flow":
            if self._signing:
                self._signing -= 1
                if not self._signing:
                    self.state.update(title=9, screen=3)
            if self._loading:
                self._loading -= 1
                if self._loading == 1:
                    raise GameError("FUSER took the command but did not answer")
                if not self._loading:
                    self.state.update(screen=0, flow=0, flow_object="ShowFlowMgr new")
            return dict(self.state)
        if op == "sign_in":
            self.state["title"], self._signing = 4, 3
        elif op == "sets":
            return SETS
        elif op in ("load_set", "load_freestyle"):
            self.state.update(screen=12, mode=1 if op == "load_set" else 0)
            self._loading = 3
            return {"venue": "VenueData /Game/Gameplay/Venues/VenueB.VenueB"}
        elif op == "begin":
            self.state["flow"] = 3
        elif op == "main_menu":
            self.state["screen"] = 3
        return {}

    def ops(self):
        return [op for op, _ in self.calls if op != "flow"]


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(launch, "POLL_SECONDS", 0)


def test_set_from_the_menu():
    game = FakeGame()
    result = launch.go_to(game, "set", 2, 1)
    assert result == {"at": "set", "venue": "VenueB", "set": 1, "name": "Mute Point", "flow": 3}
    assert game.ops() == ["sets", "load_set", "begin"]
    assert ("load_set", {"venue": 1, "set": 0}) in game.calls
    # A show stays in Intro unless walked on to WorkspaceReveal
    assert ("begin", {"states": [1, 2]}) in game.calls


def test_cold_start_signs_in_before_loading():
    game = FakeGame(screen=1, title=0)
    launch.go_to(game, "set", 2, 1)
    assert game.ops() == ["sign_in", "sets", "load_set", "begin"]


def test_sign_in_already_under_way_is_waited_for_not_repeated():
    game = FakeGame(screen=1, title=6)
    game._signing = 2
    launch.go_to(game, "menu")
    assert game.ops() == []


def test_back_at_the_menu_a_new_unstarted_title_screen_is_not_signed_in_again():
    game = FakeGame(screen=3, title=0)
    launch.go_to(game, "set", 2, 1)
    assert game.ops() == ["sets", "load_set", "begin"]


def test_freestyle_walks_only_to_intro():
    game = FakeGame()
    assert launch.go_to(game, "freestyle")["at"] == "freestyle"
    assert ("load_freestyle", {}) in game.calls and ("begin", {"states": [1]}) in game.calls


def test_a_null_or_missing_set_is_refused_with_the_venue_listed():
    for number in (2, 3):
        with pytest.raises(ValueError, match="venue 2 has sets 1 to 2: 1 Mute Point, 2 ?"):
            launch.go_to(FakeGame(), "set", 2, number)


def test_bad_arguments():
    with pytest.raises(ValueError):
        launch.go_to(FakeGame(), "set", 2)
    with pytest.raises(ValueError):
        launch.go_to(FakeGame(), "set", 7, 1)
    with pytest.raises(ValueError):
        launch.go_to(FakeGame(), "stage")


def test_menu_from_a_set():
    game = FakeGame()
    game.state["screen"] = 13
    assert launch.go_to(game, "menu") == {"at": "menu"}
    assert game.ops() == ["main_menu"]


def test_gives_up_with_where_fuser_was(monkeypatch):
    monkeypatch.setattr(launch, "LOAD_SECONDS", 0)
    game = FakeGame()
    game.call = lambda op, **args: {"screen": 12} if op == "flow" else \
        SETS if op == "sets" else {"venue": "x.y"}
    with pytest.raises(launch.LaunchError, match="venue to load"):
        launch.go_to(game, "set", 2, 1)


def test_process_match():
    assert launch._is_game([b"S:\\steamapps\\common\\Fuser\\Fuser/Binaries/Win64/Fuser-Win64-Shipping.exe",
                            b"Fuser"])
    assert not launch._is_game([b"S:\\steamapps\\common\\Fuser\\Fuser.exe"])
    assert launch._is_launch([b"reaper", b"SteamLaunch", b"AppId=1331440", b"--"])


def test_logos_are_waited_out_not_taken_for_signed_in():
    game = FakeGame(screen=0, title=None)
    reads = iter([{"screen": 0, "title": None}, {"screen": 1, "title": 0}])
    real = game.call

    def call(op, **args):
        if op == "flow" and game.state["title"] is None:
            game.calls.append((op, args))
            state = next(reads)
            game.state.update(state)
            return dict(state)
        return real(op, **args)
    game.call = call
    launch.go_to(game, "set", 2, 1)
    assert game.ops() == ["sign_in", "sets", "load_set", "begin"]
