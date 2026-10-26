"""Turn a CAD export (OBJ) into an FBX Unreal 4.24 imports cleanly, for a model made of
flat-coloured parts such as Clawd.

    blender --background --python obj_to_fbx.py -- in.obj out.fbx [--height 80]
        [--up Y|Z] [--origin center|bottom]

Each OBJ object becomes one material slot of a single mesh, so Unreal gets one static
mesh with a material per colour. Colours from the OBJ's MTL file are kept; parts
without one get grey, to recolour in Unreal.
"""
import argparse
import sys

import bmesh
import bpy
from mathutils import Vector


def parse():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    p = argparse.ArgumentParser(prog="obj_to_fbx")
    p.add_argument("obj")
    p.add_argument("fbx")
    p.add_argument("--height", type=float, default=80.0,
                   help="final height in centimetres, as Unreal measures it")
    p.add_argument("--up", choices=["Y", "Z"], default="Y",
                   help="the OBJ's up axis. OBJ is usually Y up; try Z if the model lies down")
    p.add_argument("--origin", choices=["center", "bottom"], default="center",
                   help="where the pivot goes: center to hang from a bone, bottom to stand")
    return p.parse_args(argv)


def dedupe_materials(obj):
    """One slot per colour, named by its hex. Joining parts repeats slots, which Unreal
    would turn into separate sections, and Onshape names materials by raw colour floats."""
    by_colour, names = {}, []
    for mat in obj.data.materials:
        rgb = tuple(round(c * 255) for c in mat.diffuse_color[:3])
        name = "Mat_%02X%02X%02X" % rgb
        if name not in by_colour:
            by_colour[name] = len(names)
            names.append((name, mat))
    remap = [by_colour["Mat_%02X%02X%02X" % tuple(round(c * 255) for c in m.diffuse_color[:3])]
             for m in obj.data.materials]
    # Read every face's slot before the slots change: clearing them resets each face to 0
    indices = [remap[poly.material_index] for poly in obj.data.polygons]
    obj.data.materials.clear()
    for name, mat in names:
        mat.name = name
        obj.data.materials.append(mat)
    for poly, index in zip(obj.data.polygons, indices):
        poly.material_index = index


def main():
    args = parse()
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.wm.obj_import(filepath=args.obj, up_axis=args.up,
                          forward_axis="NEGATIVE_Z" if args.up == "Y" else "Y")
    parts = [o for o in bpy.context.scene.objects if o.type == "MESH"]
    if not parts:
        sys.exit(f"no meshes in {args.obj}")

    # One material per part, named after it, so each colour is its own slot in Unreal
    for part in parts:
        if not part.data.materials:
            mat = bpy.data.materials.new(part.name)
            mat.diffuse_color = (0.5, 0.5, 0.5, 1.0)
            part.data.materials.append(mat)

    bpy.ops.object.select_all(action="DESELECT")
    for part in parts:
        part.select_set(True)
    bpy.context.view_layer.objects.active = parts[0]
    if len(parts) > 1:
        bpy.ops.object.join()
    mesh_obj = bpy.context.view_layer.objects.active
    mesh_obj.name = "Clawd"
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)

    dedupe_materials(mesh_obj)

    # CAD exports give every face its own vertices; merged, the mesh is lighter and
    # Unreal's collision and lightmap UVs behave
    bm = bmesh.new()
    bm.from_mesh(mesh_obj.data)
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=1e-5)
    bm.to_mesh(mesh_obj.data)
    bm.free()
    for poly in mesh_obj.data.polygons:
        poly.use_smooth = False  # flat shading keeps the blocky edges crisp

    # Vertex positions are sized in centimetres, as plain numbers. These export settings
    # pass the numbers through unchanged and Unreal's importer, which ignores the file's
    # unit by default, reads them as centimetres: sized in metres, Clawd came in 0.8 cm
    # tall, and with no unit scaling Blender multiplied the numbers by 100 instead
    corners = [mesh_obj.matrix_world @ Vector(c) for c in mesh_obj.bound_box]
    low = Vector(min(c[i] for c in corners) for i in range(3))
    high = Vector(max(c[i] for c in corners) for i in range(3))
    height = high.z - low.z
    if height <= 0:
        sys.exit("the model has no height; check --up")
    factor = args.height / height
    pivot = Vector(((low.x + high.x) / 2, (low.y + high.y) / 2,
                    low.z if args.origin == "bottom" else (low.z + high.z) / 2))
    for v in mesh_obj.data.vertices:
        v.co = (v.co - pivot) * factor
    mesh_obj.location = (0, 0, 0)

    bpy.ops.export_scene.fbx(
        filepath=args.fbx, use_selection=True, object_types={"MESH"},
        apply_unit_scale=True, apply_scale_options="FBX_SCALE_UNITS",
        bake_space_transform=True, mesh_smooth_type="FACE", add_leaf_bones=False)

    size = (high - low) * factor
    print(f"wrote {args.fbx}: {size.x:.1f} x {size.y:.1f} x {size.z:.1f} cm, "
          f"{len(mesh_obj.data.polygons)} faces, materials: "
          f"{', '.join(m.name for m in mesh_obj.data.materials)}")


main()
