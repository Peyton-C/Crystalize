"""Installing the mod into a fake FUSER folder, and the setup checks."""
import json
import os
import struct

import pytest

from crystalize import clawd, game

SETTINGS = """\
[General]
EnableHotReloadSystem = 0

[Hooks]
HookProcessInternal = 0
HookEngineTick = 1
HookUObjectProcessEvent = 0 ; crashes FUSER
"""

MODS_TXT = """\
CheatManagerEnablerMod : 0
FuserRecon : 1

; Built-in keybinds, do not move up!
Keybinds : 0
"""


def pe(sections):
    """The first page of a PE with the given section names."""
    head = bytearray(4096)
    struct.pack_into("<I", head, 0x3C, 0x80)
    struct.pack_into("<H", head, 0x80 + 6, len(sections))
    struct.pack_into("<H", head, 0x80 + 20, 0xF0)
    for i, name in enumerate(sections):
        at = 0x80 + 24 + 0xF0 + i * 40
        head[at:at + 8] = name.ljust(8, b"\0")
    return bytes(head)


@pytest.fixture
def fuser(tmp_path):
    ue4ss = tmp_path / "Fuser" / "Binaries" / "Win64" / "ue4ss"
    (ue4ss / "Mods").mkdir(parents=True)
    (ue4ss / "UE4SS-settings.ini").write_text(SETTINGS)
    (ue4ss / "Mods" / "mods.txt").write_text(MODS_TXT)
    (ue4ss / "Mods" / "mods.json").write_text(json.dumps(
        [{"mod_name": "FuserRecon", "mod_enabled": True},
         {"mod_name": "Keybinds", "mod_enabled": False}]))
    return tmp_path


def test_install(fuser):
    assert game.install(str(fuser)) == []
    mod = os.path.join(game.ue4ss_dir(str(fuser)), "Mods", "Crystalize")
    assert os.path.exists(os.path.join(mod, "Scripts", "main.lua"))
    assert os.path.isdir(os.path.join(mod, "ipc"))

    lines = open(os.path.join(mod, "..", "mods.txt")).read().splitlines()
    assert lines.index("Crystalize : 1") < lines.index("; Built-in keybinds, do not move up!")
    mods = json.load(open(os.path.join(mod, "..", "mods.json")))
    assert [m["mod_name"] for m in mods] == ["FuserRecon", "Crystalize", "Keybinds"]


def test_install_twice_keeps_one_entry(fuser):
    game.install(str(fuser))
    game.install(str(fuser))
    txt = open(os.path.join(game.ue4ss_dir(str(fuser)), "Mods", "mods.txt")).read()
    assert txt.count("Crystalize") == 1


def test_install_without_ue4ss(tmp_path):
    with pytest.raises(game.SetupError, match="UE4SS is not installed"):
        game.install(str(tmp_path))


def test_check_flags_process_event_hook(fuser):
    settings = os.path.join(game.ue4ss_dir(str(fuser)), "UE4SS-settings.ini")
    open(settings, "w").write(SETTINGS.replace("HookUObjectProcessEvent = 0",
                                               "HookUObjectProcessEvent = 1"))
    [problem] = game.check(str(fuser))
    assert "HookUObjectProcessEvent" in problem


def test_check_flags_steamstub_beside_song_loader(fuser):
    win64 = game.win64_dir(str(fuser))
    open(os.path.join(win64, "Fuser-Win64-Shipping.exe"), "wb").write(pe([b".text", b".bind"]))
    assert game.check(str(fuser)) == []  # no song loader, so SteamStub does no harm
    open(os.path.join(win64, "winhttp.dll"), "wb").write(b"")
    [problem] = game.check(str(fuser))
    assert "SteamStub" in problem


def test_unpacked_exe_passes(tmp_path):
    exe = tmp_path / "x.exe"
    exe.write_bytes(pe([b".text", b".rdataE"]))
    assert not game.has_steamstub(str(exe))



def lines(fuser):
    return open(os.path.join(game.ue4ss_dir(str(fuser)), "Mods", "mods.txt")).read().splitlines()


def test_crystalize_alone_leaves_clawd_out(fuser):
    game.install(str(fuser))
    assert not game.clawd_installed(str(fuser))
    assert not any(line.startswith("Clawd") for line in lines(fuser))


@pytest.fixture
def pak(tmp_path):
    path = tmp_path / "Clawd_P.pak"
    path.write_bytes(b"pak" * 30000)  # over 64 KB, so the .sig has two chunks
    return str(path)


def test_clawd_on_and_off(fuser, pak):
    g = str(fuser)
    game.clawd_on(g, pak)
    assert game.clawd_installed(g)
    installed = os.path.join(game.mod_paks_dir(g), "Clawd_P")
    assert open(installed + ".pak", "rb").read() == open(pak, "rb").read()
    assert open(installed + ".sig", "rb").read() == clawd.signature(open(pak, "rb").read())
    assert os.path.exists(os.path.join(game.ue4ss_dir(g), "Mods", "Clawd", "Scripts", "main.lua"))
    assert "Clawd : 1" in lines(fuser)
    assert lines(fuser).index("Clawd : 1") < lines(fuser).index("Keybinds : 0")
    mods = json.load(open(os.path.join(game.ue4ss_dir(g), "Mods", "mods.json")))
    assert {"mod_name": "Clawd", "mod_enabled": True} in mods

    game.clawd_off(g)
    assert not game.clawd_installed(g)
    assert os.listdir(game.mod_paks_dir(g)) == []
    assert not os.path.exists(os.path.join(game.ue4ss_dir(g), "Mods", "Clawd"))
    assert "Clawd : 0" in lines(fuser)
    game.clawd_off(g)  # twice is fine


def test_clawd_needs_ue4ss(tmp_path, pak):
    with pytest.raises(game.SetupError, match="UE4SS is not installed"):
        game.clawd_on(str(tmp_path / "nofuser"), pak)
