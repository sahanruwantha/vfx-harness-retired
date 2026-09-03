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
            assert "no axis-aligned proxy box" in contradictory["issues"][0]
        finally:
            session.close()
