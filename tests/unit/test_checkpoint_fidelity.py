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


def test_session_passes_the_selected_index_through(monkeypatch) -> None:
    from vfx_harness.blender.session import BlenderSession

    captured: dict = {}

    def fake_call(self, op, **kwargs):
        captured.update({"op": op, **kwargs})
        return {"calls": 1, "chars": 10, "dropped": 3}

    monkeypatch.setattr(BlenderSession, "call", fake_call, raising=False)
    session = BlenderSession.__new__(BlenderSession)

    result = session.journal(path="/tmp/j.py", limit=7)

    assert captured["op"] == "journal"
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
    objects `CAM_spine`/`CAM_spine.camera` while the unit's scope was `cam_rig`."""
    with pytest.raises(ValueError, match="is required: both objects"):
        worker._bvfx_camera_rig(name="CAM_spine")
