"""Installing the mod into a fake FUSER folder, and the setup checks."""
import json
import os
import struct

import pytest

from crystalize import game

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

