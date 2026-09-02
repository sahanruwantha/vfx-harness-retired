"""A best-round restore reopens a parent-published checkpoint under confinement (HIR-0174).

Run 20260902T165518Z-004470 died at the first two-round unit: the worker was handed
``checkpoints/blender/snapshot_2@exterior_massing_r1.blend``, a root the confinement does
not mount, and Blender reported ENOENT. The parent now re-stages the exact bytes into
worker scratch before the worker opens them.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from vfx_harness.blender.session import BlenderSession
from vfx_harness.orchestration import run_owner_boundary


@pytest.mark.skipif(shutil.which("blender") is None, reason="Blender is unavailable")
def test_published_snapshot_restores_inside_the_confined_worker(tmp_path: Path) -> None:
    (tmp_path / "brief.md").write_text(
        "---\nid: checkpoint-restore\nframes: 10\nfps: 24\n---\nA cube.\n", encoding="utf-8"
    )
    with run_owner_boundary.invocation(tmp_path, "build", shot_id="checkpoint-restore"):
        session = BlenderSession(blender="blender", cwd=tmp_path).start()
        try:
            session.run(
                "import bpy\nbpy.ops.mesh.primitive_cube_add()\n"
                "bpy.context.active_object.name = 'hero'\nRESULT = 1\n",
                journal=False,
            )
            snap = session.snapshot("unit_r1")
            published = Path(snap["blend"])
            assert published.parent == session.snapshots
            assert published.is_file()
            session.run(
                "import bpy\nbpy.data.objects.remove(bpy.data.objects['hero'], do_unlink=True)\n"
                "RESULT = 1\n",
                journal=False,
            )
            assert "hero" not in _names(session)

            restored = session.restore(str(published))

            assert restored["checkpoint"] == str(published)
            assert "hero" in _names(session)
        finally:
            session.close()


def _names(session: BlenderSession) -> list[str]:
    return session.run(
        "import bpy\nRESULT = sorted(o.name for o in bpy.data.objects)\n", journal=False
    )["result"]
