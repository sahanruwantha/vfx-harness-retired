from __future__ import annotations

import shutil

import pytest

from tests.unit.test_layer_finalization_state import (
    _finalize,
    _layer,
)
from tests.unit.test_layer_finalization_state import (
    _lower_boundary_receipt_authority as _lower_boundary_receipt_authority,
)
from tests.unit.test_layer_publication import _selected, _write_projections
from vfx_harness.domain.brief import load_shot
from vfx_harness.domain.work_units import canonical_unit_script_path
from vfx_harness.evaluation import determinism

pytestmark = pytest.mark.usefixtures("_lower_boundary_receipt_authority")


@pytest.mark.skipif(shutil.which("blender") is None, reason="Blender is unavailable")
def test_receipt_backed_layer_replays_twice_in_real_confined_blender(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the accepted composed bytes in a real empty-scene worker."""

    (tmp_path / "brief.md").write_text(
        "---\n"
        "id: real-confined-layer-replay\n"
        "frames: 40\n"
        "fps: 24\n"
        "resolution: [64, 64]\n"
        "engine: BLENDER_EEVEE\n"
        "---\n"
        "A deterministic cube under one camera and light.\n",
        encoding="utf-8",
    )
    layer = _layer()
    unit_script = tmp_path / canonical_unit_script_path(layer.id, layer.stages[0].id)
    unit_script.parent.mkdir(parents=True, exist_ok=True)
    unit_script.write_text(
        "import bpy\n"
        "from mathutils import Vector\n"
        "bpy.ops.mesh.primitive_cube_add(location=(0.0, 0.0, 0.0))\n"
        "cube = bpy.data.objects.get('Cube')\n"
        "cube['semantic_role'] = 'form'\n"
        "camera_data = bpy.data.cameras.new('ReceiptCameraData')\n"
        "camera = bpy.data.objects.new('ReceiptCamera', camera_data)\n"
        "bpy.context.scene.collection.objects.link(camera)\n"
        "camera_location = (4.0, -6.0, 3.0)\n"
        "camera.location = camera_location\n"
        "camera.rotation_euler = "
        "((Vector((0.0, 0.0, 0.0)) - Vector(camera_location)).to_track_quat('-Z', 'Y').to_euler())\n"
        "bpy.context.scene.camera = camera\n"
        "light_data = bpy.data.lights.new('ReceiptKeyData', type='AREA')\n"
        "light_data.energy = 900.0\n"
        "light_data.shape = 'DISK'\n"
        "light_data.size = 4.0\n"
        "light = bpy.data.objects.new('ReceiptKey', light_data)\n"
        "bpy.context.scene.collection.objects.link(light)\n"
        "light.location = (3.0, -4.0, 6.0)\n"
        "bpy.context.view_layer.update()\n",
        encoding="utf-8",
    )
    finalized_layer, _guard, _replay, _stored, receipt = _finalize(tmp_path)
    _write_projections(tmp_path, receipt)
    selected = _selected()
    monkeypatch.setattr(
        determinism,
        "resolve_selected_authority",
        lambda _folder: selected,
    )
    monkeypatch.setattr(
        determinism,
        "selected_layer_chain",
        lambda *_args, **_kwargs: (finalized_layer,),
    )
    monkeypatch.setattr(
        "vfx_harness.orchestration.layer_publication."
        "selected_finalization_predecessor_layers",
        lambda *_args, **_kwargs: (),
    )

    result = determinism.replay_equivalence(
        load_shot(tmp_path),
        passes=2,
        frame=40,
        scale=0.5,
        mode="eevee",
    )

    assert result.ok is True, result.detail
    assert result.data["layers_replayed"] == 1
    assert result.data["layers_total"] == 1
    assert result.data["passes"] == 2
