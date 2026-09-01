"""Render and attest the redistributable reference for the two-layer still eval.

This file runs inside Blender. It deliberately requires ``BLENDER_EEVEE`` so a
different engine cannot silently mint different reference authority.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

import bpy
from mathutils import Vector

RESULT_SCHEMA = "vfx-harness.eval-reference-result/v1"
ENGINE = "BLENDER_EEVEE"


def _arguments() -> argparse.Namespace:
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    return parser.parse_args(argv)


def _digest(value: Any) -> str:
    payload = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _look_at(subject: bpy.types.Object, target: tuple[float, float, float]) -> None:
    subject.rotation_euler = (Vector(target) - subject.location).to_track_quat("-Z", "Y").to_euler()


def _hero_mesh() -> bpy.types.Object:
    outline = (
        (-1.25, 0.00),
        (-1.25, 2.10),
        (-0.78, 2.62),
        (-0.42, 3.16),
        (0.00, 3.48),
        (0.42, 3.16),
        (0.78, 2.62),
        (1.25, 2.10),
        (1.25, 0.00),
    )
    half_depth = 0.34
    vertices = [(x, -half_depth, z) for x, z in outline]
    vertices.extend((x, half_depth, z) for x, z in outline)
    count = len(outline)
    faces = [tuple(range(count)), tuple(reversed(range(count, count * 2)))]
    for index in range(count):
        successor = (index + 1) % count
        faces.append((index, successor, count + successor, count + index))

    mesh = bpy.data.meshes.new("HeroMonolithMesh")
    mesh.from_pydata(vertices, [], faces)
    mesh.update()
    hero = bpy.data.objects.new("HeroMonolith", mesh)
    hero["semantic_role"] = "hero.monolith"
    bpy.context.scene.collection.objects.link(hero)

    bevel = hero.modifiers.new("Small edge bevel", "BEVEL")
    bevel.width = 0.09
    bevel.segments = 4

    material = bpy.data.materials.new("WarmIvoryEmission")
    material.use_nodes = True
    nodes = material.node_tree.nodes
    nodes.clear()
    output = nodes.new("ShaderNodeOutputMaterial")
    emission = nodes.new("ShaderNodeEmission")
    emission.inputs["Color"].default_value = (0.92, 0.73, 0.45, 1.0)
    emission.inputs["Strength"].default_value = 0.78
    material.node_tree.links.new(emission.outputs["Emission"], output.inputs["Surface"])
    hero.data.materials.append(material)
    return hero


def _configure_scene(output: Path) -> dict[str, Any]:
    bpy.ops.wm.read_factory_settings(use_empty=True)
    engines = {
        item.identifier
        for item in bpy.types.RenderSettings.bl_rna.properties["engine"].enum_items
    }
    if ENGINE not in engines:
        raise RuntimeError(
            f"reference fixture requires {ENGINE}; available engines={sorted(engines)}"
        )
    scene = bpy.context.scene
    scene.frame_start = 1
    scene.frame_end = 1
    scene.render.fps = 24
    scene.frame_set(1)
    scene.render.engine = ENGINE
    scene.render.resolution_x = 512
    scene.render.resolution_y = 512
    scene.render.resolution_percentage = 100
    scene.render.pixel_aspect_x = 1.0
    scene.render.pixel_aspect_y = 1.0
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGB"
    scene.render.image_settings.color_depth = "8"
    scene.render.image_settings.compression = 15
    scene.render.film_transparent = False
    scene.render.filepath = str(output.resolve())
    scene.view_settings.view_transform = "Standard"
    scene.view_settings.look = "Medium High Contrast"
    scene.view_settings.exposure = 0.0
    scene.view_settings.gamma = 1.0
    if hasattr(scene.render, "use_motion_blur"):
        scene.render.use_motion_blur = False
    samples_property = None
    samples_value = None
    if getattr(scene, "eevee", None) is not None and hasattr(scene.eevee, "taa_render_samples"):
        scene.eevee.taa_render_samples = 64
        samples_property = "scene.eevee.taa_render_samples"
        samples_value = 64

    world = bpy.data.worlds.new("DeepCharcoalWorld")
    world.use_nodes = True
    background = world.node_tree.nodes.get("Background")
    background.inputs["Color"].default_value = (0.008, 0.012, 0.020, 1.0)
    background.inputs["Strength"].default_value = 0.08
    scene.world = world

    _hero_mesh()
    camera_data = bpy.data.cameras.new("PrimaryCameraData")
    camera = bpy.data.objects.new("PrimaryCamera", camera_data)
    camera["semantic_role"] = "camera.primary"
    scene.collection.objects.link(camera)
    camera.location = (0.0, -9.4, 1.74)
    camera_data.lens = 62.0
    camera_data.dof.use_dof = False
    _look_at(camera, (0.0, 0.0, 1.74))
    scene.camera = camera
    bpy.context.view_layer.update()
    return {
        "engine": ENGINE,
        "frame": 1,
        "fps": 24,
        "resolution": [512, 512, 100],
        "pixel_aspect": [1.0, 1.0],
        "image": {
            "file_format": "PNG",
            "color_mode": "RGB",
            "color_depth": "8",
            "compression": 15,
        },
        "film_transparent": False,
        "view": {
            "view_transform": "Standard",
            "look": "Medium High Contrast",
            "exposure": 0.0,
            "gamma": 1.0,
        },
        "motion_blur": False,
        "samples": {"property": samples_property, "value": samples_value},
        "camera_dof": False,
    }


def main() -> None:
    args = _arguments()
    output = args.output.expanduser().resolve()
    result_path = args.result.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    settings = _configure_scene(output)
    bpy.ops.render.render(write_still=True)
    if not output.is_file() or output.stat().st_size == 0:
        raise RuntimeError(f"Blender did not publish a non-empty reference at {output}")
    identity = {
        "schema": RESULT_SCHEMA,
        "generator_sha256": _sha256(Path(__file__).resolve()),
        "blender_executable": str(Path(bpy.app.binary_path).resolve()),
        "blender_version": bpy.app.version_string,
        "render_settings": settings,
        "output": {
            "locator": "refs/hero_monolith_f001.png",
            "sha256": _sha256(output),
            "bytes": output.stat().st_size,
        },
    }
    _atomic_json(result_path, {**identity, "result_digest": _digest(identity)})


if __name__ == "__main__":
    main()
