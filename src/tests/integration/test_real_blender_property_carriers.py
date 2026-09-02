"""Data-block property contracts judge carriers, not rig pivots (HIR-0174).

Run 20260902T165518Z-004470: `bvfx_camera_rig` tags the pivot Empty `<role>` and the
camera `<role>.camera`, and the materialized `data.lens` rows on the rig role evaluated
the Empty too ("'NoneType' object has no attribute 'lens'"). The builder demolished the
rig to pass. A host with no data-block cannot carry a data-block property; a selection
with no carrier fails closed.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from vfx_harness.blender.session import BlenderSession
from vfx_harness.evidence.scene_checks import _blender_probe, _evidence, validate_row
from vfx_harness.orchestration import run_owner_boundary

_RIG = (
    "import bpy\n"
    "sc = bpy.context.scene\n"
    "rig = bpy.data.objects.new('cam_rig', None)\n"
    "sc.collection.objects.link(rig)\n"
    "rig['bvfx_role'] = 'camera.rig'\n"
    "camd = bpy.data.cameras.new('camera')\n"
    "cam = bpy.data.objects.new('camera', camd)\n"
    "sc.collection.objects.link(cam)\n"
    "cam.parent = rig\n"
    "cam['bvfx_role'] = {camera_role!r}\n"
    "camd.lens = 32.0\n"
    "camd.keyframe_insert('lens', frame=38)\n"
    "camd.lens = 38.0\n"
    "camd.keyframe_insert('lens', frame=200)\n"
    "sc.camera = cam\n"
    "RESULT = {{'tagged': sorted(o.name for o in sc.objects if o.get('bvfx_role'))}}\n"
)


def _rows() -> list[dict]:
    common = {
        "owner_layer": "1",
        "fault_owner": "1",
        "activates_at": "1",
        "lifecycle": "layer",
        "axis": "camera_motion",
        "roles": ["camera.rig"],
    }
    return [
        {
            **common,
            "id": "cam-lens-schedule",
            "kind": "keyframe_schedule",
            "samples": [
                {"frame": 38, "values": {"data.lens": 32.0}},
                {"frame": 200, "values": {"data.lens": 38.0}},
            ],
            "op": "max",
            "hi": 1.0,
        },
        {
            **common,
            "id": "cam-lens-early-band",
            "kind": "object_property",
            "property": "data.lens",
            "frame": 38,
            "op": "band",
            "lo": 28.0,
            "hi": 36.0,
        },
    ]


def _probe(session: BlenderSession, rows: list[dict]) -> dict[str, dict]:
    for row in rows:
        assert validate_row(row) is None, validate_row(row)
    raw = session.run(_blender_probe(rows, 38), journal=False)["result"]
    return {row["id"]: row for row in _evidence(rows, raw)}


@pytest.mark.skipif(shutil.which("blender") is None, reason="Blender is unavailable")
def test_rig_pivot_is_typed_out_and_a_pivot_only_selection_fails_closed(
    tmp_path: Path,
) -> None:
    (tmp_path / "brief.md").write_text(
        "---\nid: property-carriers\nframes: 225\nfps: 25\n---\nA camera rig.\n",
        encoding="utf-8",
    )
    with run_owner_boundary.invocation(tmp_path, "build", shot_id="property-carriers"):
        session = BlenderSession(blender="blender", cwd=tmp_path).start()
        try:
            assert session.run(_RIG.format(camera_role="camera.rig.camera"), journal=False)[
                "result"
            ] == {"tagged": ["cam_rig", "camera"]}
            packed = _probe(session, _rows())
            schedule = packed["cam-lens-schedule"]
            assert schedule["pass"] is True, schedule
            assert schedule["value"] == 0.0
            assert "no data-block, not a carrier: cam_rig [EMPTY]" in schedule["note"]
            band = packed["cam-lens-early-band"]
            assert band["pass"] is True, band
            assert band["value"] == 32.0
            assert "cam_rig [EMPTY]" in band["note"]

            # Injected failure: the camera leaves the role, only the pivot remains.
            session.run(
                "import bpy\nbpy.data.objects['camera']['bvfx_role'] = 'camera.body'\n"
                "RESULT = 1\n",
                journal=False,
            )
            packed = _probe(session, _rows())
            for row_id in ("cam-lens-schedule", "cam-lens-early-band"):
                row = packed[row_id]
                assert row["pass"] is False, row
                assert row["value"] is None
                assert "matched only hosts without a data-block" in row["error"]
                assert "cam_rig [EMPTY]" in row["error"]
        finally:
            session.close()
