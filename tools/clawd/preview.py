"""Render an FBX from the front and three-quarters, to check orientation and colours.

    blender --background --python preview.py -- model.fbx out_prefix
"""
import math
import sys

import bpy
from mathutils import Vector

fbx, out = sys.argv[sys.argv.index("--") + 1:][:2]
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.fbx(filepath=fbx)
meshes = [o for o in bpy.context.scene.objects if o.type == "MESH"]
corners = [o.matrix_world @ Vector(c) for o in meshes for c in o.bound_box]
low = Vector(min(c[i] for c in corners) for i in range(3))
high = Vector(max(c[i] for c in corners) for i in range(3))
centre, size = (low + high) / 2, max(high - low)

scene = bpy.context.scene
scene.render.engine = "BLENDER_WORKBENCH"
scene.display.shading.light = "STUDIO"
scene.display.shading.color_type = "MATERIAL"
scene.render.resolution_x, scene.render.resolution_y = 900, 700
scene.render.film_transparent = False
scene.world = bpy.data.worlds.new("w")

cam_data = bpy.data.cameras.new("cam")
cam_data.type = "ORTHO"
cam_data.ortho_scale = size * 1.3
cam = bpy.data.objects.new("cam", cam_data)
scene.collection.objects.link(cam)
scene.camera = cam

# Blender's front is -Y; Unreal's forward, after FBX export, is +X from Blender's -Y
for name, yaw, pitch in (("front", 0, 0), ("angle", 35, 20)):
    direction = Vector((math.sin(math.radians(yaw)) * math.cos(math.radians(pitch)),
                        -math.cos(math.radians(yaw)) * math.cos(math.radians(pitch)),
                        math.sin(math.radians(pitch))))
    cam.location = centre + direction * size * 3
    cam.rotation_euler = (centre - cam.location).to_track_quat("-Z", "Y").to_euler()
    scene.render.filepath = f"{out}_{name}.png"
    bpy.ops.render.render(write_still=True)
print("rendered", out)
