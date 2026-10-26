"""The file channel to FUSER, with a Python thread standing in for the mod."""
import json
import os
import re
import shutil
import subprocess
import threading

import pytest

from crystalize import bridge
from crystalize.bridge import Bridge, GameError, lua


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(bridge, "PICKUP_SECONDS", 0.3)
    monkeypatch.setattr(bridge, "ANSWER_SECONDS", 0.3)
    monkeypatch.setattr(bridge, "POLL_SECONDS", 0.005)


def fake_mod(ipc, answer, picks_up=True, answers=True):
    """Answer one request the way the mod does. Returns the thread and what it read."""
    seen = {}

    def run():
        request = os.path.join(ipc, "request.lua")
        while not os.path.exists(request):
            pass
        if not picks_up:
            return
        seen["source"] = open(request, encoding="utf-8").read()
        os.remove(request)
        if not answers:
            return
        id_ = re.search(r'\["id"\]=(\d+)', seen["source"]).group(1)
        path = os.path.join(ipc, f"response-{id_}.json")
        with open(path + ".tmp", "w") as f:
            json.dump({"id": int(id_), "session": "s1", **answer}, f)
        os.rename(path + ".tmp", path)

    thread = threading.Thread(target=run)
    thread.start()
    return thread, seen


def test_round_trip(tmp_path):
    b = Bridge(str(tmp_path))
    thread, seen = fake_mod(str(tmp_path), {"ok": True, "result": {"bpm": 120}})
    assert b.call("state") == {"bpm": 120}
    thread.join()
    assert '["op"]="state"' in seen["source"]
    assert b.session == "s1"
    assert os.listdir(tmp_path) == []


def test_refusal_raises(tmp_path):
    b = Bridge(str(tmp_path))
    thread, _ = fake_mod(str(tmp_path), {"ok": False, "error": "no disc named x"})
    with pytest.raises(GameError, match="no disc named x"):
        b.call("drop", name="x", deck=0)
    thread.join()


def test_game_not_running(tmp_path):
    b = Bridge(str(tmp_path))
    with pytest.raises(GameError, match="not answering"):
        b.call("state")
    # The request is taken back, so a FUSER started later does not act on it
    assert not os.path.exists(tmp_path / "request.lua")


def test_picked_up_but_never_answered(tmp_path):
    b = Bridge(str(tmp_path))
    thread, _ = fake_mod(str(tmp_path), {}, answers=False)
    with pytest.raises(GameError, match="did not answer"):
        b.call("state")
    thread.join()


def test_clears_leftovers(tmp_path):
    (tmp_path / "request.lua").write_text("return {}")
    (tmp_path / "response-1.json").write_text("{}")
    Bridge(str(tmp_path))
    assert os.listdir(tmp_path) == []


def test_lua_literals():
    assert lua(None) == "nil"
    assert lua([1, True, 2.5]) == "{1,true,2.5}"
    assert lua({"a": "x"}) == '{["a"]="x"}'
    assert lua('say "hi"\\\n') == r'"say \"hi\"\\\010"'
    with pytest.raises(ValueError):
        lua(float("nan"))


@pytest.mark.skipif(not (shutil.which("lua5.4") or shutil.which("lua")), reason="needs lua")
def test_lua_literals_load_in_lua():
    value = {"name": 'Beyoncé "Halo"\n\t\x01end9', "deck": 3, "queue": False, "list": [1, 2]}
    script = ("local t = load(io.read('a'), 'x', 't', {})() "
              "io.write(t.name, '|', math.type(t.deck), '|', tostring(t.queue), '|', #t.list)")
    lua_bin = shutil.which("lua5.4") or shutil.which("lua")
    out = subprocess.run([lua_bin, "-e", script], input="return " + lua(value),
                         capture_output=True, text=True, check=True).stdout
    assert out == 'Beyoncé "Halo"\n\t\x01end9|integer|false|2'
