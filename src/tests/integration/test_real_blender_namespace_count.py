"""object_count read-back names the descendants a literal selector counts (HIR-0178).

Run 20260903T024933Z-7f07e7: ``object_count eq 1`` over ``camera.targets`` read 3 with a
generic definition, the evidence row listed the matched roles under ``roles`` and an empty
control tag, and the builder retagged hosts to learn the matcher rule.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from vfx_harness.blender.session import BlenderSession
from vfx_harness.evidence.scene_checks import _blender_probe, _evidence, validate_row
from vfx_harness.orchestration import run_owner_boundary

_TARGETS = (
    "import bpy\n"
    "sc = bpy.context.scene\n"
    "parent = bpy.data.objects.new('camera_targets', None)\n"
    "sc.collection.objects.link(parent)\n"
    "parent['bvfx_role'] = 'camera.targets'\n"
    "for name, role in (('window_target_aim', 'camera.targets.window_target'),"
    " ('door_target_aim', 'camera.targets.door_target')):\n"
    "    child = bpy.data.objects.new(name, None)\n"
    "    sc.collection.objects.link(child)\n"
    "    child.parent = parent\n"
    "    child['bvfx_role'] = role\n"
    "    child['bvfx_control'] = name\n"
    "RESULT = sorted(o.name for o in sc.objects if o.get('bvfx_role'))\n"
)


def _count(id: str, roles: list[str]) -> dict:
    return {
        "id": id,
        "kind": "object_count",
        "roles": roles,
        "op": "eq",
        "value": 1,
        "owner_layer": "1",
        "fault_owner": "1",
        "activates_at": "1",
        "lifecycle": "layer",
        "axis": "window_alignment",
    }


@pytest.mark.skipif(shutil.which("blender") is None, reason="Blender is unavailable")
def test_namespace_count_read_back_names_descendants_and_selectors(tmp_path: Path) -> None:
    (tmp_path / "brief.md").write_text(
        "---\nid: namespace-count\nframes: 225\nfps: 25\n---\nCamera targets.\n",
        encoding="utf-8",
    )
    rows = [_count("parent-count", ["camera.targets"]), _count("root-count", ["camera.targets.root"])]
    for row in rows:
        assert validate_row(row) is None, validate_row(row)
    with run_owner_boundary.invocation(tmp_path, "build", shot_id="namespace-count"):
        session = BlenderSession(blender="blender", cwd=tmp_path).start()
        try:
            assert session.run(_TARGETS, journal=False)["result"] == [
                "camera_targets",
                "door_target_aim",
                "window_target_aim",
            ]
            raw = session.run(_blender_probe(rows, 1), journal=False)["result"]
            packed = {row["id"]: row for row in _evidence(rows, raw)}
        finally:
            session.close()
    parent = packed["parent-count"]
    assert parent["pass"] is False and parent["value"] == 3
    assert parent["selector_roles"] == ["camera.targets"]
    assert parent["roles"] == ["camera.targets", "camera.targets.door_target", "camera.targets.window_target"]
    assert parent["controls"] == ["door_target_aim", "window_target_aim"]
    assert "literal selector(s) ['camera.targets'] also match dotted descendants (HIR-0147)" in parent["note"]
    assert "door_target_aim(camera.targets.door_target)" in parent["note"]
    assert "count a leaf role" in parent["note"]
    root = packed["root-count"]
    assert root["pass"] is False and root["value"] == 0
    assert "matched no objects" in root["note"]
