"""The worker decides bbox feasibility through the real sealed camera (HIR-0183)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from vfx_harness.blender.session import BlenderSession
from vfx_harness.orchestration import run_owner_boundary

_SCENE = (
    "import bpy\n"
    "sc = bpy.context.scene\n"
    "sc.frame_start = 1\n"
    "sc.frame_end = 120\n"
    "camd = bpy.data.cameras.new('camera')\n"
    "cam = bpy.data.objects.new('camera', camd)\n"
    "sc.collection.objects.link(cam)\n"
    "cam.rotation_euler = (1.5708, 0.0, 0.0)\n"
    "cam.location = (0.0, -14.0, 1.5)\n"
    "cam.keyframe_insert('location', frame=1)\n"
    "cam.location = (0.0, -5.0, 1.5)\n"
    "cam.keyframe_insert('location', frame=113)\n"
    "sc.camera = cam\n"
    "bpy.ops.mesh.primitive_cube_add(size=2.0, location=(0.0, 0.0, 1.0))\n"
    "mass = bpy.context.active_object\n"
    "mass.name = 'mass'\n"
    "mass['bvfx_role'] = 'exterior.mass'\n"
    "RESULT = sorted(o.name for o in sc.objects)\n"
)


def _row(id, kind, frame, op, **bound):
    return {"id": id, "kind": kind, "frame": frame, "op": op, **bound}


@pytest.mark.skipif(shutil.which("blender") is None, reason="Blender is unavailable")
def test_worker_proves_feasible_and_infeasible_band_sets(tmp_path: Path) -> None:
    (tmp_path / "brief.md").write_text(
        "---\nid: bbox-feasibility\nframes: 120\nfps: 24\n---\nA mass under a moving camera.\n",
        encoding="utf-8",
    )
    with run_owner_boundary.invocation(tmp_path, "build", shot_id="bbox-feasibility"):
        session = BlenderSession(blender="blender", cwd=tmp_path).start()
        try:
            names = session.run(_SCENE, journal=False)["result"]
            assert "camera" in names and "mass" in names, names
            consistent = session.check(
                kind="bbox_feasibility",
                rows=[
                    _row("far-height", "bbox_height", 1, "band", lo=0.05, hi=0.35),
                    _row("near-height", "bbox_height", 113, "min", lo=0.55),
                ],
                roles=["exterior.mass"],
                seed=1,
            )
            assert consistent["ok"] is True and consistent["feasible"] is True, consistent
            assert consistent["frames"] == [1, 113]
            assert consistent["seed_objects"] == ["mass"]
            assert all(row["residual"] == 0.0 for row in consistent["rows"])

            contradictory = session.check(
                kind="bbox_feasibility",
                rows=[
                    _row("far-tall", "bbox_height", 1, "min", lo=0.9),
                    _row("near-short", "bbox_height", 113, "max", hi=0.1),
                ],
                roles=["exterior.mass"],
                seed=1,
                evaluations=1500,
            )
            assert contradictory["ok"] is False and contradictory["feasible"] is False, contradictory
            assert contradictory["binding"]
            assert "no single axis-aligned proxy box" in contradictory["issues"][0]
            assert consistent["bounds_source"] == "hosts"

            # Union framing for a shared role measures the contract's own quantity.
            session.run(
                "import bpy\n"
                "bpy.ops.mesh.primitive_cube_add(size=1.0, location=(3.0, 0.0, 0.5))\n"
                "wing = bpy.context.active_object\n"
                "wing.name = 'wing'\n"
                "wing['bvfx_role'] = 'exterior.mass.wing'\n"
                "RESULT = 1\n",
                journal=False,
            )
            union = session.check(kind="framing", role="exterior.mass", frame=1)
            assert union["hosts"] == ["mass", "wing"], union
            solo = session.check(kind="framing", object="mass", frame=1)
            assert union["frames"][0]["width"] > solo["frames"][0]["width"], (union, solo)
            crop = session.check(kind="bbox", role="exterior.mass", frame=1)
            assert crop["hosts"] == ["mass", "wing"] and crop["ok"] is True

            # Before any host exists, bounds derive from the sealed camera.
            session.run(
                "import bpy\n"
                "for name in ('mass', 'wing'):\n"
                "    bpy.data.objects.remove(bpy.data.objects[name], do_unlink=True)\n"
                "RESULT = 1\n",
                journal=False,
            )
            hostless = session.check(
                kind="bbox_feasibility",
                rows=[_row("far-height", "bbox_height", 1, "band", lo=0.05, hi=0.35)],
                roles=["exterior.mass"],
                seed=1,
                evaluations=800,
            )
            assert hostless["bounds_source"] == "camera" and hostless["seed_objects"] == []
            assert hostless["feasible"] is True, hostless
        finally:
            session.close()


_CAMERA_ONLY = (
    "import bpy\n"
    "sc = bpy.context.scene\n"
    "sc.frame_start = 1\n"
    "sc.frame_end = 120\n"
    "for obj in list(sc.objects):\n"
    "    bpy.data.objects.remove(obj, do_unlink=True)\n"
    "camd = bpy.data.cameras.new('camera')\n"
    "cam = bpy.data.objects.new('camera', camd)\n"
    "sc.collection.objects.link(cam)\n"
    "cam.rotation_euler = (1.5708, 0.0, 0.0)\n"
    "cam.location = (0.0, -14.0, 1.5)\n"
    "cam.keyframe_insert('location', frame=1)\n"
    "cam.location = (0.0, -{near}, 1.5)\n"
    "cam.keyframe_insert('location', frame=113)\n"
    "sc.camera = cam\n"
    "RESULT = sorted(o.name for o in sc.objects)\n"
)


@pytest.mark.skipif(shutil.which("blender") is None, reason="Blender is unavailable")
def test_camera_only_scene_derives_bounds_from_the_frustums(tmp_path: Path) -> None:
    """HIR-0184: a camera layer proves downstream framing before any subject exists."""
    (tmp_path / "brief.md").write_text(
        "---\nid: frustum-bounds\nframes: 120\nfps: 24\n---\nA camera with no subject yet.\n",
        encoding="utf-8",
    )
    rows = [
        _row("far-height", "bbox_height", 1, "band", lo=0.05, hi=0.35),
        _row("near-height", "bbox_height", 113, "min", lo=0.55),
    ]
    with run_owner_boundary.invocation(tmp_path, "build", shot_id="frustum-bounds"):
        session = BlenderSession(blender="blender", cwd=tmp_path).start()
        try:
            assert session.run(_CAMERA_ONLY.format(near=5.0), journal=False)["result"] == ["camera"]
            dolly = session.check(kind="bbox_feasibility", rows=rows, roles=["exterior.mass"], seed=1)
            assert dolly["bounds"]["source"] == "camera_frustum", dolly
            assert dolly["seed_objects"] == []
            assert dolly["feasible"] is True, dolly

            # A dolly-in can only enlarge a static subject: the reversed contrast is
            # infeasible anywhere the camera can see, including the near field.
            reversed_rows = [
                _row("far-tall", "bbox_height", 1, "min", lo=0.9),
                _row("near-short", "bbox_height", 113, "max", hi=0.1),
            ]
            reversed_contrast = session.check(
                kind="bbox_feasibility", rows=reversed_rows, roles=["exterior.mass"], seed=1, evaluations=1500
            )
            assert reversed_contrast["bounds"]["source"] == "camera_frustum"
            assert reversed_contrast["feasible"] is False, reversed_contrast
            assert reversed_contrast["binding"]
        finally:
            session.close()
