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
