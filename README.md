# Crystalize
A FUSER MCP server. Crystalize lets an agent DJ in FUSER, Harmonix's DJ game: it searches your songs, drops discs on the decks, and changes the key and tempo, through a mod running inside the game.

## Install
Needs [uv](https://docs.astral.sh/uv/getting-started/installation/), FUSER on Steam, and UE4SS set up for it, see [Setup](docs/setup.md). Then:

```sh
git clone https://github.com/Peyton-C/Crystalize.git
cd Crystalize
uv run crystalize install
```

`install` copies the mod into FUSER and warns about anything in the setup that will crash the game or stop the mod. Crystalize looks for FUSER in Steam's default library. Pass `--game <folder>` or set `CRYSTALIZE_FUSER` if yours is elsewhere.

## Use
Start FUSER through Steam and open a freestyle session, then check the connection:

```sh
uv run crystalize state
```

Add the server to an MCP client:

```sh
claude mcp add crystalize -- uv run --directory /path/to/Crystalize crystalize mcp
```

See [MCP server](docs/mcp.md) for the tools, and [Bridge](docs/bridge.md) for how the server reaches the game.

## Clawd
Optionally, Crystalize puts Clawd, in headphones, in place of your avatar:

```sh
uv run crystalize clawd on
```

`clawd off` takes him out. See [Clawd](docs/clawd.md).
