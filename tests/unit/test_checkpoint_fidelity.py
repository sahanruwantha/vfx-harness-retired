"""The finalized unit script must describe the SELECTED checkpoint's scene.

Run 20260823T154920Z restored the best round (r1) before finalizing, then dumped the
full journal — so r2's rejected key light and tunnel taper were written into
`build/units/01/iris_mechanism.py`. The published artifact therefore did not represent
the selected checkpoint, and canonical replay would have reproduced a scene no round
ever scored. The snapshot's write-ahead `journal_index` already marks the accepted
prefix; finalization now uses it."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

_WORKER = Path(__file__).resolve().parents[2] / "src" / "vfx_harness" / "blender" / "worker.py"


@pytest.fixture
def worker():
    """Load the worker standalone. It runs inside Blender's interpreter, so `bpy` is
    stubbed — journal bookkeeping is pure Python and must be testable without Blender
    (the defect it guards cost a whole build)."""
    stubs = {name: types.ModuleType(name) for name in ("bpy", "bpy.app", "bpy.app.handlers")}
    stubs["bpy"].app = stubs["bpy.app"]
    stubs["bpy.app"].handlers = stubs["bpy.app.handlers"]
    saved = {name: sys.modules.get(name) for name in stubs}
    sys.modules.update(stubs)
    spec = importlib.util.spec_from_file_location("_vfx_worker_under_test", _WORKER)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        for name, previous in saved.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous
    yield module
    sys.modules.pop(spec.name, None)


def test_journal_limit_excludes_discarded_rounds(worker, tmp_path: Path) -> None:
    worker._JOURNAL.clear()
    worker._JOURNAL.extend(["round1_call_a", "round1_call_b", "round2_key_light"])
    selected_index = 2  # snapshot taken at the end of round 1

    path = tmp_path / "journal.py"
    info = worker.h_journal({"path": str(path), "limit": selected_index})

    text = path.read_text(encoding="utf-8")
    assert info["calls"] == 2
    assert info["dropped"] == 1
    assert "round1_call_a" in text and "round1_call_b" in text
    assert "round2_key_light" not in text
    assert "Truncated to the selected checkpoint" in text


def test_journal_without_limit_is_unchanged(worker, tmp_path: Path) -> None:
    worker._JOURNAL.clear()
    worker._JOURNAL.extend(["a", "b"])

    path = tmp_path / "journal.py"
    info = worker.h_journal({"path": str(path)})

    assert info["calls"] == 2
    assert info["dropped"] == 0
    assert "Truncated" not in path.read_text(encoding="utf-8")


def test_journal_start_excludes_inherited_dependency_replay(worker, tmp_path: Path) -> None:
    worker._JOURNAL.clear()
    worker._JOURNAL.extend(["reset", "prior_material", "active_light", "active_bloom"])

    path = tmp_path / "unit-journal.py"
    info = worker.h_journal({"path": str(path), "start": 2, "limit": 4})

    text = path.read_text(encoding="utf-8")
    assert info["calls"] == 2
    assert info["dropped_before"] == 2
    assert "active_light" in text and "active_bloom" in text
    assert "prior_material" not in text and "reset" not in text
    assert "Excluded 2 inherited" in text


def test_failed_transactional_run_restores_scene_and_never_journals(
    worker, tmp_path: Path, monkeypatch
) -> None:
    """A Python exception after an authored write must not retain a partial scene."""
    worker._JOURNAL.clear()
    worker.ARTIFACTS = str(tmp_path)
    worker.bpy.marker = "accepted"
    saved: dict[str, str] = {}

    def save_as_mainfile(*, filepath, **_kwargs):
        saved["marker"] = worker.bpy.marker
        Path(filepath).write_text("rollback", encoding="utf-8")

    def open_mainfile(*, filepath):
        assert Path(filepath).is_file()
        worker.bpy.marker = saved["marker"]

    worker.bpy.ops = types.SimpleNamespace(
        wm=types.SimpleNamespace(
            save_as_mainfile=save_as_mainfile,
            open_mainfile=open_mainfile,
        )
    )
    monkeypatch.setattr(
        worker,
        "_scene_stats",
        lambda: {"objects": 0, "mesh_objects": 0, "verts": 0, "tris": 0},
    )
    mathutils = types.ModuleType("mathutils")
    mathutils.Vector = tuple
    monkeypatch.setitem(sys.modules, "mathutils", mathutils)

    with pytest.raises(RuntimeError, match="injected failure"):
        worker.h_run(
            {
                "code": "bpy.marker = 'partial'\nraise RuntimeError('injected failure')",
                "transactional": True,
            }
        )

    assert worker.bpy.marker == "accepted"
    assert worker._JOURNAL == []
    assert not (tmp_path / "run_bpy_rollback.blend").exists()


def test_blender5_helpers_encode_closed_loop_light_and_vector_blur(worker) -> None:
    import inspect

    vector_source = inspect.getsource(worker._bvfx_vector_blur)
    assert "CompositorNodeVecBlur" in vector_source
    assert '("Samples", samples)' in vector_source
    assert '("Depth", "Depth")' in vector_source
    assert "NodeGroupOutput" in vector_source
    assert "use_pass_vector = True" in vector_source
    assert "_bvfx_role(vector_blur, role" in vector_source

    light_source = inspect.getsource(worker._bvfx_light)
    assert "obj.data.type = str(light_type).upper()" in light_source
    assert "data = bpy.data.lights[data_name]" in light_source
    assert light_source.index("obj.data.type") < light_source.index(
        "data = bpy.data.lights[data_name]"
    )
    assert "spot_size requires light_type='SPOT'" in light_source


def test_volume_helpers_tag_every_semantic_host_at_creation(worker) -> None:
    import inspect

    bounded = inspect.getsource(worker._bvfx_volume)
    for statement in (
        "_bvfx_role(dom, role, owner_layer)",
        "_bvfx_role(m, material_role, owner_layer)",
        "_bvfx_role(vol, node_role, owner_layer)",
        "_bvfx_control(vol, control, owner_layer)",
        "dom.scale = (size[0], size[1], size[2])",
        "bpy.context.view_layer.update()",
    ):
        assert statement in bounded

    world = inspect.getsource(worker._bvfx_volumetric_world)
    for statement in (
        "_bvfx_role(w, role, owner_layer)",
        "_bvfx_role(vol, node_role, owner_layer)",
        "_bvfx_control(vol, control, owner_layer)",
    ):
        assert statement in world


def test_interp_traverses_object_role_and_data_block_curves(worker) -> None:
    import inspect

    source = inspect.getsource(worker._bvfx_interp)
    assert 'obj.get("bvfx_role")' in source
    assert "getattr(item, \"data\", None)" in source
    assert "for item in expanded" in source


def test_render_handler_restores_common_scene_settings(worker) -> None:
    """Workbench and draft observations must not mutate the next authored call."""
    import inspect

    source = inspect.getsource(worker.h_render)
    for field in (
        '"engine": sc.render.engine',
        '"resolution_percentage": sc.render.resolution_percentage',
        '"filepath": sc.render.filepath',
        '"frame": sc.frame_current',
        '"shading_type": sc.display.shading.type',
        '"file_format": image_settings.file_format',
        '"taa_render_samples"',
    ):
        assert field in source
    assert 'sc.render.engine = original["engine"]' in source
    assert 'sc.frame_set(original["frame"])' in source
    assert source.index("bpy.ops.render.render") < source.index(
        'sc.render.engine = original["engine"]'
    )


def test_session_passes_the_selected_index_through(monkeypatch) -> None:
    from vfx_harness.blender.session import BlenderSession

    captured: dict = {}

    def fake_call(self, op, **kwargs):
        captured.update({"op": op, **kwargs})
        return {"calls": 1, "chars": 10, "dropped": 3}

    monkeypatch.setattr(BlenderSession, "call", fake_call, raising=False)
    session = BlenderSession.__new__(BlenderSession)

    result = session.journal(path="/tmp/j.py", start=3, limit=7)

    assert captured["op"] == "journal"
    assert captured["start"] == 3
    assert captured["limit"] == 7
    assert result["dropped"] == 3


def test_camera_rig_helper_tags_both_objects(worker) -> None:
    """The helper persisted an untagged rig and camera, so any scoped unit using it
    inherited a canonical-replay rejection it could not repair — re-invoking the helper
    recreated the untagged object. Runs 045543/050954 lost two builds to this."""
    import inspect

    source = inspect.getsource(worker._bvfx_camera_rig)

    assert "role=None" in source and "owner_layer=None" in source
    assert "_bvfx_role(rig, role, owner_layer)" in source
    assert '_bvfx_role(cam, f"{role}.camera", owner_layer)' in source
    # Tagging must happen before parenting/keying so an early return cannot skip it.
    assert source.index("_bvfx_role(rig") < source.index("cam.parent = rig")
    # The role must never be derived from the display name: a label is not authority.
    assert "role or name" not in source
    assert "is required" in source


def test_camera_rig_helper_refuses_an_untagged_call(worker) -> None:
    """Run 20260824T052204Z defaulted the role to the rig's display name and tagged its
    objects `CAM_spine`/`CAM_spine.camera` while its declared scope was `cam_rig`."""
    with pytest.raises(ValueError, match="is required: both objects"):
        worker._bvfx_camera_rig(name="CAM_spine")


def test_bvfx_role_rejects_comma_membership(worker) -> None:
    """Run 20260825T143912Z-0b5ab4 stored a CSV as one token that matched neither contract."""

    class Host(dict):
        name = "proxy"

    with pytest.raises(ValueError, match="commas are not membership"):
        worker._bvfx_role(Host(), "cam.blockout_fg,cam.blockout_depth_tiers")
    tagged = worker._bvfx_role(Host(), "cam.blockout_fg")
    assert tagged["bvfx_role"] == "cam.blockout_fg"
