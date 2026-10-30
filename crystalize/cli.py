"""Command line entry point."""
from __future__ import annotations

import argparse
import json
import sys

from . import __version__, game
from .bridge import Bridge, GameError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="crystalize", description="A FUSER MCP server")
    parser.add_argument("--version", action="version", version=f"crystalize {__version__}")
    parser.add_argument("--game", help="FUSER's folder. Default: $CRYSTALIZE_FUSER, or "
                                       "Steam's default library")
    sub = parser.add_subparsers(dest="command", required=True)
    mcp = sub.add_parser("mcp", help="run the MCP server over stdin and stdout")
    mcp.add_argument("--debug", action="store_true",
                     help="also offer debug_read and debug_lua, which reach anything in FUSER")
    sub.add_parser("install", help="copy the mod into FUSER's UE4SS and check the setup")
    sub.add_parser("state", help="print what FUSER is playing, to test the connection")
    go = sub.add_parser("launch", help="take FUSER straight to a campaign set, freestyle or "
                                       "the main menu, starting it if it is closed")
    where = go.add_mutually_exclusive_group(required=True)
    where.add_argument("--set", metavar="VENUE.SET", help="a campaign set, such as 3.1")
    where.add_argument("--freestyle", nargs="?", const=0, type=int, metavar="VENUE",
                       help="freestyle, in the last venue played or the one given")
    where.add_argument("--menu", action="store_true", help="the main menu")
    where.add_argument("--list", action="store_true", help="list the campaign's sets")
    go.add_argument("--restart", action="store_true",
                    help="install the mod, close FUSER if it is open, and start it again")
    clawd = sub.add_parser("clawd", help="put Clawd in headphones in place of FUSER's avatars, "
                                         "or take him out")
    clawd.add_argument("switch", choices=["on", "off"])
    clawd.add_argument("--pak", help="with on, install this Clawd_P.pak rather than the released "
                                     "one, to try a rebuild before publishing it")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    folder = game.game_dir(args.game)

    if args.command == "install":
        try:
            problems = game.install(folder)
        except game.SetupError as e:
            print(f"crystalize: {e}", file=sys.stderr)
            return 1
        print(f"Installed the Crystalize mod into {game.ue4ss_dir(folder)}")
        for problem in problems:
            print(f"warning: {problem}", file=sys.stderr)
        return 0

    if args.command == "clawd":
        from . import clawd
        try:
            if args.switch == "off":
                game.clawd_off(folder)
            else:
                if args.pak:
                    pak = args.pak
                    print(f"Using {pak}, SHA-256 {clawd.sha256(pak)}, not the released Clawd")
                else:
                    pak = clawd.fetch()
                game.clawd_on(folder, pak)
        except (game.SetupError, clawd.ClawdError, OSError) as e:
            print(f"crystalize: {e}", file=sys.stderr)
            return 1
        print(f"Clawd is {args.switch}. Restart FUSER for it to take effect")
        return 0

    bridge = Bridge(game.ipc_dir(folder))

    if args.command == "launch":
        return _launch(args, folder, bridge)

    if args.command == "state":
        from .mcp import Server
        try:
            state = Server(bridge, None, None)._tool_get_state({})
        except GameError as e:
            print(f"crystalize: {e}", file=sys.stderr)
            return 1
        print(json.dumps(state, indent=2, ensure_ascii=False))
        return 0

    from .mcp import Server
    for problem in game.check(folder):
        print(f"crystalize: warning: {problem}", file=sys.stderr)
    return Server(bridge, sys.stdin.buffer, sys.stdout.buffer, debug=args.debug).run()


def _launch(args, folder: str, bridge: Bridge) -> int:
    from . import launch
    try:
        if args.restart:
            for problem in game.install(folder):
                print(f"warning: {problem}", file=sys.stderr)
            if launch.running():
                print("Closing FUSER")
                launch.stop_game()
        if not launch.running():
            print("Starting FUSER")
            launch.start_game(bridge)
        if args.list:
            for venue, sets in enumerate(launch.sets(bridge), 1):
                for number, s in enumerate(sets, 1):
                    print(f"{venue}.{number}  {launch._name(s)}")
            return 0
        if args.set:
            venue, _, number = args.set.partition(".")
            if not (venue.isdigit() and number.isdigit()):
                raise ValueError(f"--set is VENUE.SET, such as 3.1, not {args.set}")
            result = launch.go_to(bridge, "set", int(venue), int(number))
        elif args.menu:
            result = launch.go_to(bridge, "menu")
        else:
            result = launch.go_to(bridge, "freestyle", args.freestyle or None)
    except (game.SetupError, launch.LaunchError, GameError, ValueError) as e:
        print(f"crystalize: {e}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
