"""Where FUSER is, putting the Crystalize mod into it, and checking the setup around it."""
from __future__ import annotations

import configparser
import json
import os
import shutil
import struct
from importlib import resources

DEFAULT_GAME = "~/.local/share/Steam/steamapps/common/Fuser"
MOD = "Crystalize"


class SetupError(Exception):
    """FUSER or UE4SS is not where Crystalize needs it."""


def game_dir(arg: str | None = None) -> str:
    return os.path.expanduser(arg or os.environ.get("CRYSTALIZE_FUSER") or DEFAULT_GAME)


def win64_dir(game: str) -> str:
    return os.path.join(game, "Fuser", "Binaries", "Win64")


def ue4ss_dir(game: str) -> str:
    return os.path.join(win64_dir(game), "ue4ss")


def ipc_dir(game: str) -> str:
    return os.path.join(ue4ss_dir(game), "Mods", MOD, "ipc")


def install(game: str) -> list[str]:
    """Copy the mod into UE4SS and switch it on. Returns any problems found in the setup."""
    ue4ss = ue4ss_dir(game)
    if not os.path.isdir(ue4ss):
        raise SetupError(f"UE4SS is not installed in {win64_dir(game)}, see docs/setup.md")

    source = resources.files("crystalize").joinpath("mod", MOD)
    target = os.path.join(ue4ss, "Mods", MOD)
    with resources.as_file(source) as path:
        shutil.copytree(path, target, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("ipc"))
    os.makedirs(os.path.join(target, "ipc"), exist_ok=True)

    _enable_txt(os.path.join(ue4ss, "Mods", "mods.txt"))
    _enable_json(os.path.join(ue4ss, "Mods", "mods.json"))
    return check(game)


def check(game: str) -> list[str]:
    """Problems with the setup that would stop Crystalize or crash FUSER."""
    problems = []
    ue4ss = ue4ss_dir(game)
    settings = os.path.join(ue4ss, "UE4SS-settings.ini")
    if os.path.exists(settings):
        hooks = _hooks(settings)
        if hooks.get("hookuobjectprocessevent", "1") != "0":
            problems.append("HookUObjectProcessEvent is on in UE4SS-settings.ini, which crashes "
                            "FUSER at the startup logos. Set it to 0")
        if hooks.get("hookenginetick", "1") != "1":
            problems.append("HookEngineTick is off in UE4SS-settings.ini, so Crystalize cannot "
                            "run anything on the game thread. Set it to 1")

    exe = os.path.join(win64_dir(game), "Fuser-Win64-Shipping.exe")
    loader = os.path.join(win64_dir(game), "winhttp.dll")
    if os.path.exists(loader) and os.path.exists(exe) and has_steamstub(exe):
        problems.append("FUSER's executable still has SteamStub, so FuserSongLoader runs too early "
                        "beside UE4SS and custom songs will not load. See docs/setup.md")
    return problems


def has_steamstub(exe: str) -> bool:
    """Whether a PE still carries SteamStub's .bind section."""
    with open(exe, "rb") as f:
        head = f.read(4096)
    pe = struct.unpack_from("<I", head, 0x3C)[0]
    sections = struct.unpack_from("<H", head, pe + 6)[0]
    optional = struct.unpack_from("<H", head, pe + 20)[0]
    table = pe + 24 + optional
    return any(head[table + i * 40:table + i * 40 + 8].rstrip(b"\0") == b".bind"
               for i in range(sections))


def _hooks(path: str) -> dict[str, str]:
    parser = configparser.ConfigParser(strict=False, inline_comment_prefixes=(";",))
    with open(path, encoding="utf-8-sig") as f:
        parser.read_file(f)
    if not parser.has_section("Hooks"):
        return {}
    return {k: v.strip() for k, v in parser.items("Hooks")}


def _enable_txt(path: str) -> None:
    """Switch the mod on in mods.txt, ahead of UE4SS's Keybinds line, which must stay last."""
    lines = open(path, encoding="utf-8").read().splitlines() if os.path.exists(path) else []
    entry = f"{MOD} : 1"
    for i, line in enumerate(lines):
        if line.split(":")[0].strip() == MOD:
            lines[i] = entry
            break
    else:
        at = next((i for i, line in enumerate(lines)
                   if line.startswith("; Built-in keybinds") or line.startswith("Keybinds")),
                  len(lines))
        lines.insert(at, entry)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def _enable_json(path: str) -> None:
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        mods = json.load(f)
    for mod in mods:
        if mod.get("mod_name") == MOD:
            mod["mod_enabled"] = True
            break
    else:
        at = next((i for i, mod in enumerate(mods) if mod.get("mod_name") == "Keybinds"),
                  len(mods))
        mods.insert(at, {"mod_name": MOD, "mod_enabled": True})
    with open(path, "w", encoding="utf-8") as f:
        json.dump(mods, f, indent=4)
