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

# Optional: Clawd in headphones in place of FUSER's avatars, a Lua mod and the pak with
# his model, which comes from a GitHub release. Paks in Content/Paks/~mods are mounted
# like the game's own
CLAWD = "Clawd"
CLAWD_PAK = "Clawd_P"


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


def mod_paks_dir(game: str) -> str:
    return os.path.join(game, "Fuser", "Content", "Paks", "~mods")


def install(game: str) -> list[str]:
    """Copy the mod into UE4SS and switch it on. Returns any problems found in the setup."""
    _copy_mod(game, MOD)
    os.makedirs(os.path.join(ue4ss_dir(game), "Mods", MOD, "ipc"), exist_ok=True)
    return check(game)


def clawd_on(game: str, pak: str) -> None:
    """Put Clawd's model, the pak at `pak`, and his mod into FUSER, and switch the mod on.
    The .sig is written here from the pak, so the two can never disagree."""
    from .clawd import signature

    _copy_mod(game, CLAWD)
    os.makedirs(mod_paks_dir(game), exist_ok=True)
    target = os.path.join(mod_paks_dir(game), CLAWD_PAK)
    shutil.copy(pak, target + ".pak")
    with open(pak, "rb") as f:
        data = f.read()
    with open(target + ".sig", "wb") as f:
        f.write(signature(data))


def clawd_off(game: str) -> None:
    """Take Clawd out again, model and mod, and switch the mod off."""
    _require_ue4ss(game)
    for ext in (".pak", ".sig"):
        _remove(os.path.join(mod_paks_dir(game), CLAWD_PAK + ext))
    shutil.rmtree(os.path.join(ue4ss_dir(game), "Mods", CLAWD), ignore_errors=True)
    _switch(game, CLAWD, False)


def clawd_installed(game: str) -> bool:
    return os.path.exists(os.path.join(mod_paks_dir(game), CLAWD_PAK + ".pak"))


def _require_ue4ss(game: str) -> str:
    ue4ss = ue4ss_dir(game)
    if not os.path.isdir(ue4ss):
        raise SetupError(f"UE4SS is not installed in {win64_dir(game)}, see docs/setup.md")
    return ue4ss


def _copy_mod(game: str, mod: str) -> None:
    ue4ss = _require_ue4ss(game)
    source = resources.files("crystalize").joinpath("mod", mod)
    with resources.as_file(source) as path:
        shutil.copytree(path, os.path.join(ue4ss, "Mods", mod), dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("ipc"))
    _switch(game, mod, True)


def _switch(game: str, mod: str, on: bool) -> None:
    mods = os.path.join(ue4ss_dir(game), "Mods")
    _enable_txt(os.path.join(mods, "mods.txt"), mod, on)
    _enable_json(os.path.join(mods, "mods.json"), mod, on)


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


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


def _enable_txt(path: str, mod: str, on: bool) -> None:
    """Switch a mod in mods.txt, adding it ahead of UE4SS's Keybinds line, which must stay last."""
    lines = open(path, encoding="utf-8").read().splitlines() if os.path.exists(path) else []
    entry = f"{mod} : {1 if on else 0}"
    for i, line in enumerate(lines):
        if line.split(":")[0].strip() == mod:
            lines[i] = entry
            break
    else:
        at = next((i for i, line in enumerate(lines)
                   if line.startswith("; Built-in keybinds") or line.startswith("Keybinds")),
                  len(lines))
        lines.insert(at, entry)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def _enable_json(path: str, mod: str, on: bool) -> None:
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        mods = json.load(f)
    for entry in mods:
        if entry.get("mod_name") == mod:
            entry["mod_enabled"] = on
            break
    else:
        at = next((i for i, entry in enumerate(mods) if entry.get("mod_name") == "Keybinds"),
                  len(mods))
        mods.insert(at, {"mod_name": mod, "mod_enabled": on})
    with open(path, "w", encoding="utf-8") as f:
        json.dump(mods, f, indent=4)
