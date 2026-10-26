#!/usr/bin/env python3
"""Rebuild Clawd_P.pak, the model Crystalize's optional Clawd mod puts on FUSER's avatars.

    python3 tools/clawd/build.py --engine /path/to/UnrealEngine-4.24 [--obj model.obj]

With --obj, the model is converted to tools/clawd/clawd.fbx first, which needs Blender.
Then the FBX is imported into the tiny Unreal project beside this script, cooked, and
packed into dist/clawd/Clawd_P.pak. Unreal's work happens in a copy of that project
outside the repo, since it leaves over 100 MB behind.

The pak is not committed: it is published as a GitHub release, and Crystalize pins its
SHA-256, which this prints. Try it with `crystalize clawd on --pak dist/clawd/Clawd_P.pak`
first. The .sig is not built here; `clawd on` writes it from the pak.

The engine must be 4.24, FUSER's, built from source on Linux. It cooks for Linux, its only
target: a static mesh is stored the same way on Linux and Windows, so the mesh loads in
Windows FUSER. Materials would carry Linux shaders, so none are imported; the Lua mod
colours Clawd at runtime.
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
TEMPLATE = os.path.join(HERE, "unreal")
FBX = os.path.join(HERE, "clawd.fbx")
OUT = os.path.join(REPO, "dist", "clawd")
NAME = "Clawd_P"


def run(cmd, log):
    with open(log, "w") as f:
        result = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=f, stderr=subprocess.STDOUT)
    if result.returncode != 0:
        sys.exit(f"{os.path.basename(cmd[0])} failed, see {log}")


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--engine", default=os.environ.get("UE4_ROOT"),
                   help="the UE 4.24 source build. Default: $UE4_ROOT")
    p.add_argument("--obj", help="convert this OBJ, such as an Onshape export, to the FBX first")
    p.add_argument("--height", type=float, default=80, help="with --obj, height in cm")
    p.add_argument("--up", choices=["Y", "Z"], default="Z", help="with --obj, the OBJ's up axis")
    p.add_argument("--work", default=os.path.join(
        os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "crystalize", "clawd-build"),
        help="where Unreal works. Default: ~/.cache/crystalize/clawd-build")
    args = p.parse_args()
    if not args.engine:
        sys.exit("pass --engine or set UE4_ROOT")
    project = args.work
    shutil.copytree(TEMPLATE, project, dirs_exist_ok=True)
    binaries = os.path.join(args.engine, "Engine", "Binaries", "Linux")
    editor, unrealpak = os.path.join(binaries, "UE4Editor"), os.path.join(binaries, "UnrealPak")
    logs = os.path.join(project, "Saved", "Logs")
    os.makedirs(logs, exist_ok=True)

    if args.obj:
        run(["blender", "--background", "--python", os.path.join(HERE, "obj_to_fbx.py"), "--",
             args.obj, FBX, "--height", str(args.height), "--up", args.up],
            os.path.join(logs, "blender.log"))

    # A fresh import each time, so nothing stale from an earlier model is cooked
    shutil.rmtree(os.path.join(project, "Content", "Mods"), ignore_errors=True)
    shutil.rmtree(os.path.join(project, "Saved", "Cooked"), ignore_errors=True)
    settings = os.path.join(project, "Saved", "import.json")
    with open(settings, "w") as f:
        json.dump({"ImportGroups": [{
            "GroupName": "Clawd", "Filenames": [FBX], "DestinationPath": "/Game/Mods/Clawd",
            "bReplaceExisting": True, "FactoryName": "FbxFactory",
            "ImportSettings": {"bImportMaterials": False, "bImportTextures": False,
                               "bImportAsSkeletal": False,
                               "StaticMeshImportData": {"bCombineMeshes": True,
                                                        "bAutoGenerateCollision": False,
                                                        "bGenerateLightmapUVs": False}},
        }]}, f, indent=2)
    uproject = os.path.join(project, "Fuser.uproject")
    common = ["-nosourcecontrol", "-unattended", "-nopause", "-nullrhi", "-stdout"]
    run([editor, uproject, "-run=ImportAssets", f"-importsettings={settings}", *common],
        os.path.join(logs, "import.log"))
    # Unversioned, as custom song paks are, so FUSER's modified 4.24 reads the packages
    run([editor, uproject, "-run=cook", "-targetplatform=LinuxNoEditor", "-unversioned", *common],
        os.path.join(logs, "cook.log"))

    cooked = os.path.join(project, "Saved", "Cooked", "LinuxNoEditor", "Fuser", "Content")
    files = []
    for root, _, names in os.walk(os.path.join(cooked, "Mods")):
        for n in names:
            src = os.path.join(root, n)
            files.append(f'"{src}" "../../../Fuser/Content/{os.path.relpath(src, cooked)}"')
    if not files:
        sys.exit(f"nothing was cooked, see {logs}/cook.log")
    listing = os.path.join(project, "Saved", "pak_files.txt")
    with open(listing, "w") as f:
        f.write("\n".join(files) + "\n")
    os.makedirs(OUT, exist_ok=True)
    pak = os.path.join(OUT, NAME + ".pak")
    run([unrealpak, pak, f"-create={listing}"], os.path.join(logs, "unrealpak.log"))
    with open(pak, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    print(f"{pak} ({os.path.getsize(pak)} bytes, {len(files)} files)")
    print(f"SHA-256 {digest}")
    print(f"Try it: crystalize clawd on --pak {os.path.relpath(pak)}")
    print("Publish it: upload it to a new release, then set RELEASE and SHA256 in "
          "crystalize/clawd.py")


if __name__ == "__main__":
    main()
