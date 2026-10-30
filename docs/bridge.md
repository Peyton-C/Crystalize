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
| `clock` | `HypeAudio.GetCurrentBeat`, `GetCurrentMeasure`, `GetCurrentTick` and `GetDefaultSyncBeat` |
| `discs` | Every loaded `SongCelData`, with its `CelSongSet`, the song's `IsOwnedAndInstalled`, the disc's `Transposes`, its `PickupBeats` and whether it has major and minor MIDI |
| `drop` | `DeskState.NetSpawnDisc` |
| `eject` | `DeskState.EjectTrack` |
| `drop_queue` | `DeskState.DoDropQueue` |
| `set_key` | `AudioController.NetSetKeyMode`, then `NetSetKey` |
| `set_tempo` | `AudioController.NetSetTempo` |
| `mute`, `solo` | `AudioController.NetMuteTrack`, `NetSoloTrack` |
| `gain` | `AudioController.NetSetGainForTrack`, one step of a fade. `state` reads it back from `DeckState.GetDeckGain` |
| `riser` | `TransitionMgr.SetTransitionOptions` for the mode, then `WBP_TransitionTrigger_C.TriggerTransition`, what the riser key does |
| `snapshot` | `SavedMomentsMgr.SaveCurrentMoment`, what the Take Snapshot key does |
| `record_spawn` | `InstrumentCraftingState.NetSpawnCraftedDisc`, the slot's recording as a disc. `perform_press` with `record` presses in Crafting mode, which records |
| `set_state` | `RequestMgr._requests` for crowd requests, `ShowGoalsMgr` for goals, `SkillStat_Freshness` for freshness, the crowd meter from its widget, `WBP_ShowsCrowdMeter_C`, and `ShowFlowMgr` for the score, the stars and whether the set is ending. Each part is read on its own, and the ones that fail are listed |
| `flow` | `ShellFlowMgr.GetCurrentScreen`, the title screen's sign-in state, `LevelLoadConfig._gameMode`, and the session's `ShowFlowMgr` or `FreestyleFlowMgr` state |
| `sets` | Each venue's `MissionPrompts` from `VenueMgr.Venues` |
| `sign_in` | `HypeTitleScreen.StartLoginFlow`, what a key on the title screen does |
| `load_set` | `LevelLoadConfig.ConfigureShow`, `CrateMgr.ApplyCrateFilter`, then `VenueMgr.SetCurrentVenueData` and `LoadCurrentVenueMap` |
| `load_freestyle` | `LevelLoadConfig.ConfigureFreestyle`, then the venue as for a set |
| `begin` | `ShellFlowMgr.LoadShellScreen` for the gameplay screen, then the flow manager's `GoToState` for each state given |
| `main_menu` | `ShellFlowMgr.GoBackToScreen`, reloading the menu's map |
| `read` | Named fields of the first object of a class, or of every one with `all`, read without calling anything, to look for where FUSER keeps something. Arrays come back as lists |
| `lua` | Runs a chunk of Lua on the game thread with UE4SS's globals and returns what it returns and prints. The server sends it only with `--debug` |
| `batch` | Several of the above in order, on one frame. A bar's steps go as one, so they all reach FUSER within the window that counts as on the downbeat |

The `Net` functions are FUSER's multiplayer calls. In freestyle and the campaign the player is the host, so they run locally.

## Show breaks
Between some sections of a campaign set FUSER puts up a dialog to hold Continue on, and no goal or trick counts until the break ends. The mod closes it itself, without a request, since a break can open while the agent is between tool calls. It checks every half second, and once the dialog is on a page it calls `ShowFlowMgr._HandleShowBreakProgression`, which ends the whole break. Called before the dialog is up it leaves the break stuck, so a break that never shows a page is moved on with `_GoToNextBreakMessage` after 20 seconds. A break with no messages is left to end by itself, which takes 2 to 10 bars. FUSER shows the dialogs only on a set not yet finished, so try a change here on one of those.
