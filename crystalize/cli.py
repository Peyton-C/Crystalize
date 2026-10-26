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
    sub.add_parser("mcp", help="run the MCP server over stdin and stdout")
    sub.add_parser("install", help="copy the mod into FUSER's UE4SS and check the setup")
    sub.add_parser("state", help="print what FUSER is playing, to test the connection")
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

    bridge = Bridge(game.ipc_dir(folder))

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
    return Server(bridge, sys.stdin.buffer, sys.stdout.buffer).run()


if __name__ == "__main__":
    sys.exit(main())
