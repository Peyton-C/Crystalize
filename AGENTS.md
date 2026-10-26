# Working in this repo

## Naming
The project is written `Crystalize`, with one z and a capital C, in prose and headings. The package, the command and the MCP server name are `crystalize`. The game is `FUSER`, in capitals, as Harmonix writes it.

Crystalize is one of a set of DJ software MCP servers named after gems, with Pink Diamond and Rhinestone Heart. bejeweled is a sibling project, and the house style below comes from it.

## What goes where
`README.md` is what a user needs to get in and pointed the right way, nothing more. `docs/` holds one file per subject, and those files own it: setup, the MCP tools, the bridge.

Technical detail is welcome in `docs/`. The evidence behind a decision is not. The setup doc says the ProcessEvent hook crashes FUSER and must be off; the crash address and the disassembly that found it stay out.

## Documentation voice
Match bejeweled's `AGENTS.md` and its `docs/festival.md`. In short: be short, cut the evidence and keep the consequence, name the tool as the subject ("Crystalize writes..."), address the reader directly for anything they do, and use a table for any fixed set a reader might look up.

- No hard wrapping. One long line per paragraph.
- No blank line between a heading and the text under it.
- Commas where a dash would be tempting. No em-dashes.
- No bold or italics in prose.
- Headings are short noun phrases.

## Code
Two halves, joined by a folder:

```
crystalize/mcp.py  ->  bridge.py  ->  ipc/request.lua  ->  mod/Crystalize/Scripts/main.lua
 tools, decks 1-4      file channel   <- response-<id>.json   runs on FUSER's game thread
```

Around the server: `music.py` holds FUSER's enums and the musical arithmetic, `mixes.py` checks a plan and turns it into a schedule, `player.py` plays a schedule on FUSER's beat clock, and `store.py` keeps tags, saved mixes and the last library in `~/.local/share/crystalize`. Planning follows pink diamond's: the agent writes the whole mix, the checks find what it cannot hear, and only then does anything play. A new check belongs in `mixes.py`, with its threshold as a named constant and the reason for the number beside it.

The mod stays thin. It wraps FUSER's functions, reports enums as the numbers the game stores, and numbers decks from 0. Names, keys, deck numbering, grouping discs into songs and anything else that can be decided outside the game belong in Python, in `music.py` or `mcp.py`, where they are easy to test and change without restarting FUSER.

Everything in the mod that touches a game object runs through `ExecuteInGameThread`. FUSER crashes with UE4SS's ProcessEvent hook on, so nothing may depend on it: no `RegisterHook` on Blueprint functions, no `NotifyOnNewObject`.

FUSER's classes and functions come from a UE4SS header dump. Generate one with the dumper keybind or `GenerateSDK()` when a new feature needs a function that is not wrapped yet; the dump is not kept in the repo.

Tests run with `uv run pytest`. The mod's tests run it in a real Lua 5.4 against a fake FUSER in `tests/mod_harness.lua`, and skip without `lua`. Nothing replaces trying a change in the game, so say which parts were tried there and which were not.

Code comments are the place for the reasoning the docs leave out. Explain why something is done a particular way, especially where the obvious approach crashes FUSER.

## Commits
The user commits their own work. Leave changes in the working tree.

Commit messages, when suggested, are short and plain: a subject line in the imperative, then a sentence or two on why, only when the why is not obvious from the diff.
