# Clawd
Crystalize can put Clawd, with headphones, in place of your avatar in FUSER: on stage, in the character editor, and in battles and lobbies. He is optional, and installing Crystalize leaves your avatar alone. Anthropic is in no way related to Crystalize.

```sh
uv run crystalize clawd on
uv run crystalize clawd off
```

Restart FUSER after either. He needs the setup in [Setup](setup.md), custom songs included: his model loads the way a custom song does.

`clawd on` downloads his model from Crystalize's GitHub releases the first time and keeps it in `~/.cache/crystalize`. Crystalize checks the download against the SHA-256 it was released with and refuses any other file. While the repository is private, the download needs a GitHub token that can see it: set `GITHUB_TOKEN`, or sign in with `gh auth login` and Crystalize uses that.

## How he works
Clawd is a pak, `Clawd_P.pak`, which `clawd on` puts in `Content/Paks/~mods` beside the `.sig` FUSER reads, and a UE4SS Lua mod. The mod hides each avatar's body and outfit and turns its hat into Clawd, attached to a spine bone so he rides the dance. He has to be one of the avatar's own parts: the stage draws the avatar's parts and nothing else.

Your outfit and profile thumbnails keep your normal avatar. FUSER crashed rendering them with Clawd in, so the mod leaves the characters that make them alone.

Press F8 in the game to turn him a quarter turn, should a venue show him from the side.

## Tuning
Three numbers at the top of `crystalize/mod/Clawd/Scripts/main.lua` place him. Change one, run `clawd on` again, and restart FUSER.

| Setting | What |
| --- | --- |
| `SCALE` | His size, 2 by default, about 160 cm tall |
| `LIFT` | How far he floats above the floor, in cm |
| `YAW` | Which way he faces. FUSER's characters face a quarter turn off their actor's forward, which is why it is 270 |

## Rebuilding him
`tools/clawd/build.py` makes a new `Clawd_P.pak` in `dist/clawd`, from `tools/clawd/clawd.fbx` or from an OBJ, such as an Onshape export, with `--obj`. It needs Unreal Engine 4.24, FUSER's version, built from source on Linux, and Blender for an OBJ.

```sh
python3 tools/clawd/build.py --engine /path/to/UnrealEngine-4.24 --obj "Clawd with Headphones.obj"
uv run crystalize clawd on --pak dist/clawd/Clawd_P.pak
```

`--pak` installs your build in place of the released one, to try it in the game. To publish it, attach it to a new release and point `RELEASE` and `SHA256` in `crystalize/clawd.py` at it; the build prints the SHA-256.

```sh
gh release create clawd-v2 dist/clawd/Clawd_P.pak --title "Clawd v2" --notes "..."
```

Model him upright, with each colour a separate part. The build sizes him from `--height`, 80 cm by default, and the mod scales that. Unreal works in `~/.cache/crystalize/clawd-build`, which takes over 100 MB; pass `--work` to put it elsewhere.

Unreal 4.24 on Linux can only cook for Linux. A static mesh is stored the same way for Linux and Windows, so his model loads in FUSER, but materials are not, so the pak has none. The mod colours each part at runtime, from the colours listed in it, which must match the model's.

To build 4.24 at all, replace its `Engine/Build/Commit.gitdeps.xml` with the one attached to the `4.24.3-release` tag on GitHub. The 4.24 branch's own file points at downloads Epic has since moved, and `Setup.sh` fails with 403 Forbidden.
