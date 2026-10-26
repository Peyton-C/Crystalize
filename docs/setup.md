# Setup
Crystalize runs inside FUSER as a UE4SS Lua mod. FUSER needs three changes before UE4SS works with it, and a fourth if you use custom songs. This was worked out on Linux with Proton, but nothing here is specific to it except the launch option.

## UE4SS
Download the experimental build of [RE-UE4SS](https://github.com/UE4SS-RE/RE-UE4SS/releases/tag/experimental-latest), the `UE4SS_v3.0.1-*.zip`, and extract it into `Fuser/Binaries/Win64`, so `dwmapi.dll` sits beside `Fuser-Win64-Shipping.exe`. From `zCustomGameConfigs.zip` in the same release, copy `Fuser/VTableLayout.ini` into the `ue4ss` folder.

On Linux, add this launch option in Steam so Wine loads UE4SS rather than its own `dwmapi.dll`. `winhttp` is there for FuserSongLoader, leave it in even without custom songs:

```
WINEDLLOVERRIDES="winhttp,dwmapi=n,b" %command%
```

## Hooks
FUSER crashes at the startup logos with UE4SS's default hooks. Its engine is a modified 4.24, and UE4SS's `ProcessEvent` hook breaks the return value of a Blueprint event the game calls there. In `ue4ss/UE4SS-settings.ini`, under `[Hooks]`, set every hook to `0` except `HookEngineTick`, which Crystalize needs:

| Setting | Value |
| --- | --- |
| `HookEngineTick` | `1` |
| `HookUObjectProcessEvent` | `0`, the one that crashes FUSER |
| Every other `Hook...` | `0` |

Turn off UE4SS's bundled mods too, by setting each to `0` in `ue4ss/Mods/mods.txt`. Some of them depend on the hooks that are now off. `crystalize install` adds its own line there.

## Custom songs
FUSER's executable is wrapped in SteamStub, which decrypts the game's code as it starts. FuserSongLoader looks for the code it patches once, the first time a thread starts, and UE4SS starts one before the code is decrypted. The loader finds nothing and custom songs silently stop loading.

Removing SteamStub from your copy with [Steamless](https://github.com/atom0s/Steamless) fixes this. Steamless is a Windows program, so on Linux run it through protontricks with FUSER closed. Copy it and the executable into FUSER's Wine prefix first, so the Steam runtime can see them, and copy `Plugins/Steamless.API.dll` and `Plugins/SharpDisasm.dll` up beside `Steamless.CLI.exe`, since Wine's .NET does not find them in `Plugins`:

```sh
protontricks -c 'wine C:/steamless/Steamless.CLI.exe C:/steamless/Fuser-Win64-Shipping.exe' 1331440
```

Keep the original executable, then replace it with `Fuser-Win64-Shipping.exe.unpacked.exe`. A Steam update or "Verify integrity of game files" puts the original back, and custom songs break again until you repeat this. `crystalize install` and `crystalize mcp` warn when they find SteamStub beside FuserSongLoader.

## Checking
Start FUSER. `ue4ss/UE4SS.log` should end with Crystalize's line, `[Crystalize] 0.1.0 loaded, watching ...`. Open a freestyle session and run `uv run crystalize state`.
