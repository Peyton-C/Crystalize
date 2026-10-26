# Bridge
The server and the mod talk through files in the mod's `ipc` folder, `ue4ss/Mods/Crystalize/ipc`. UE4SS's Lua has no sockets, and under Proton a folder is the simplest thing both a Linux process and a Windows game can see.

## Requests
The server writes `request.lua`, a Lua table literal holding an id, a command and its arguments. The mod checks for it every 50 ms, deletes it, and loads it with an empty environment, so a request is data and can call nothing. The mod runs the command on FUSER's game thread and writes `response-<id>.json`.

One request is in flight at a time. If the mod has not taken a request within a second and a half, the server takes it back and reports that FUSER is not running. Once taken, the answer can wait up to ten seconds, since a loading screen holds up the game thread.

Each response carries a session id the mod picks when it loads. A new one tells the server FUSER restarted, and that its copy of the song library may be out of date.

## Commands
The mod's commands are thin wrappers over FUSER's own functions. The server turns tool calls into these, numbering decks from 0 as the game does.

| Command | Calls |
| --- | --- |
| `state` | `HypeAudio` for the key and tempo, each `BP_Deck_C` for its discs |
| `clock` | `HypeAudio.GetCurrentBeat`, `GetCurrentMeasure` and `GetCurrentTick` |
| `discs` | Every loaded `SongCelData`, with its `CelSongSet`, the song's `IsOwnedAndInstalled`, the disc's `Transposes` and whether it has major and minor MIDI |
| `drop` | `DeskState.NetSpawnDisc` |
| `eject` | `DeskState.EjectTrack` |
| `drop_queue` | `DeskState.DoDropQueue` |
| `set_key` | `AudioController.NetSetKeyMode`, then `NetSetKey` |
| `set_tempo` | `AudioController.NetSetTempo` |
| `mute`, `solo` | `AudioController.NetMuteTrack`, `NetSoloTrack` |

The `Net` functions are FUSER's multiplayer calls. In freestyle the player is the host, so they run locally.
