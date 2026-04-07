# 在 Blender 内执行: blender -b --python blender_render_fbx_frames.py -- <fbx> <out_dir> <frame_count> <size>
# 将每一帧渲染为 out_dir/frame_0001.png ...
from __future__ import annotations

import math
import os
import sys
from typing import Optional, Tuple

import bpy
from mathutils import Vector


def _parse_args() -> Optional[Tuple[str, str, int, int]]:
    if "--" not in sys.argv:
        return None
    raw = sys.argv[sys.argv.index("--") + 1 :]
    if len(raw) < 3:
        return None
    fbx_path = raw[0]
    out_dir = raw[1]
    frames = int(raw[2])
    size = int(raw[3]) if len(raw) > 3 else 256
    return fbx_path, out_dir, frames, size


def main() -> int:
    parsed = _parse_args()
    if not parsed:
        print("usage: ... -- <fbx_path> <out_dir> <frame_count> [size]", file=sys.stderr)
        return 1
    fbx_path, out_dir, n, size = parsed
    os.makedirs(out_dir, exist_ok=True)

    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete()

    bpy.ops.import_scene.fbx(filepath=os.path.abspath(fbx_path))

    meshes = [o for o in bpy.context.scene.objects if o.type == "MESH"]
    if not meshes:
        print("No mesh in FBX", file=sys.stderr)
        return 2

    corners: list[Vector] = []
    for obj in meshes:
        for c in obj.bound_box:
            corners.append(obj.matrix_world @ Vector(c))

    xs = [v.x for v in corners]
    ys = [v.y for v in corners]
    zs = [v.z for v in corners]
    min_v = Vector((min(xs), min(ys), min(zs)))
    max_v = Vector((max(xs), max(ys), max(zs)))
    center = (min_v + max_v) * 0.5
    span = max_v - min_v
    radius = max(span.x, span.y, span.z, 0.01) * 0.5

    scene = bpy.context.scene
    scene.render.engine = "BLENDER_WORKBENCH"
    scene.render.resolution_x = size
    scene.render.resolution_y = size
    try:
        scene.render.image_settings.file_format = "PNG"
    except Exception:
        pass

    bpy.ops.object.camera_add(location=center + Vector((0, 0, 0)))
    cam = bpy.context.active_object
    scene.camera = cam

    bpy.ops.object.light_add(type="SUN", location=center + Vector((0, 0, radius * 4)))
    light = bpy.context.active_object
    light.data.energy = 3.0

    dist = max(radius * 3.5, 1.0)

    for frame in range(1, n + 1):
        angle = (frame - 1) * (2 * math.pi / max(n, 1))
        cam.location = center + Vector(
            (math.cos(angle) * dist, math.sin(angle) * dist, dist * 0.55)
        )
        target = center - cam.location
        if target.length < 1e-6:
            target = Vector((0, 0, -1))
        cam.rotation_euler = target.to_track_quat("-Z", "Y").to_euler()
        fp = os.path.join(out_dir, f"frame_{frame:04d}.png")
        scene.render.filepath = fp
        bpy.ops.render.render(write_still=True)

    return 0


if bpy.app.background:
    sys.exit(main())
