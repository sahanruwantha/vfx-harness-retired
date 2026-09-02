"""Journals publish under the session's one checkpoint-owned root (HIR-0174).

Run 20260902T165518Z-004470 refused every unit journal: the session contained journal
destinations against its Blender snapshot root while the finalizer published beside
it, and a broad handler turned the refusal into a log line. The session now mints the
destination and the finalizer takes it from the session.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from vfx_harness.agents.builder import unit_finalize
from vfx_harness.blender.session import BlenderError, BlenderSession
from vfx_harness.observability import run_artifacts


def _session(tmp_path: Path) -> BlenderSession:
    shot = tmp_path / "journal_shot"
    shot.mkdir()
    (shot / "brief.md").write_text("---\nid: journal_shot\n---\n", encoding="utf-8")
    return BlenderSession(blender="blender", cwd=shot)


def _fake_worker_journal(session: BlenderSession, calls: int):
    def _call(op, **kwargs):
        assert op == "journal"
        path = kwargs.get("path")
        if path is not None:
            Path(path).write_text("bvfx_role(obj, 'hero')\n" * calls, encoding="utf-8")
        return {"calls": calls, "chars": 22 * calls, "dropped": 0}

    session.call = _call  # type: ignore[method-assign]


def test_session_owns_a_journal_root_beside_its_snapshots(tmp_path: Path) -> None:
    session = _session(tmp_path)
    layout = run_artifacts.ensure(session.cwd, command="blender-session")

    assert session.journals == layout.checkpoints / "journals"
    assert session.snapshots == layout.checkpoints / "blender"
    assert session.journals.is_dir()
    assert session.journal_destination("layer-1.py") == session.journals / "layer-1.py"
    with pytest.raises(BlenderError, match="plain file name"):
        session.journal_destination("../layer-1.py")
    with pytest.raises(BlenderError, match="plain file name"):
        session.journal_destination("")


def test_journal_publishes_to_the_minted_destination(tmp_path: Path) -> None:
    session = _session(tmp_path)
    _fake_worker_journal(session, calls=3)
    destination = session.journal_destination("layer-1.py")

    result = session.journal(path=str(destination), start=2, limit=9)

    assert result["calls"] == 3
    assert result["path"] == str(destination)
    assert destination.read_text(encoding="utf-8").count("bvfx_role") == 3
    assert not (session.snapshots / "layer-1.py").exists()


def test_journal_outside_the_journal_root_is_refused_naming_the_root(tmp_path: Path) -> None:
    session = _session(tmp_path)
    _fake_worker_journal(session, calls=1)

    with pytest.raises(BlenderError, match="journal publication must stay under") as exc:
        session.journal(path=str(session.snapshots / "layer-1.py"))
    assert str(session.journals) in str(exc.value)
    assert "journal_destination" in str(exc.value)
    assert not (session.snapshots / "layer-1.py").exists()


def test_finalizer_takes_the_journal_destination_from_the_session() -> None:
    source = inspect.getsource(unit_finalize._persist_journal_and_finalize_script)
    assert 'session.journal_destination(f"layer-{m.id}.py")' in source
    assert '"journals"' not in source
    # Capture failure is a phase boundary, not a nicety: no broad handler may turn a
    # refused journal into a finalizer kicked off without its evidence.
    assert "never block finalize on a nicety" not in source
    assert "journal unavailable" not in source


def test_restore_restages_a_published_checkpoint_for_the_confined_worker(tmp_path: Path) -> None:
    """The worker sees only its scratch; a published checkpoint is re-staged there."""
    session = _session(tmp_path)
    published = session.snapshots / "snapshot_2@unit_r1.blend"
    published.write_bytes(b"BLENDER-checkpoint-bytes")
    calls: list[dict] = []

    def _call(op, **kwargs):
        calls.append({"op": op, **kwargs})
        return {"restored": kwargs["blend"]}

    session.call = _call  # type: ignore[method-assign]
    result = session.restore(str(published))

    staged = session._worker_publications / "snapshots" / published.name
    assert calls == [{"op": "restore", "blend": str(staged)}]
    assert staged.read_bytes() == b"BLENDER-checkpoint-bytes"
    assert result["checkpoint"] == str(published)

    # A stale staged copy is replaced by the exact published bytes.
    published.write_bytes(b"BLENDER-checkpoint-bytes-v2")
    session.restore(str(published))
    assert staged.read_bytes() == b"BLENDER-checkpoint-bytes-v2"

    with pytest.raises(BlenderError, match="published checkpoint is absent"):
        session.restore(str(session.snapshots / "missing.blend"))
    with pytest.raises(BlenderError, match="accepts a published checkpoint under"):
        session.restore(str(tmp_path / "elsewhere.blend"))
