"""Blender resolution is decided inside the mandatory confinement, never on the host."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from vfx_harness.blender import resolution as blender_resolution
from vfx_harness.blender.resolution import BlenderResolutionError, resolve_blender
from vfx_harness.blender.session import BlenderSession

needs_bwrap = pytest.mark.skipif(
    shutil.which("bwrap") is None,
    reason="bubblewrap is unavailable",
)


def _host_only_launcher(directory: Path) -> Path:
    """A launcher that answers ``--version`` on the host but is invisible in the sandbox."""

    launcher = directory / "blender"
    launcher.write_text(
        "#!/bin/sh\nprintf 'Blender 0.0.0 (host-only launcher)\\n'\n",
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    return launcher


@pytest.fixture
def isolated_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the requested launcher is a candidate; the host's Blender must not rescue it."""

    monkeypatch.setattr(blender_resolution, "_FALLBACK_CANDIDATES", ())
    monkeypatch.delenv("BLENDER_BIN", raising=False)
    resolve_blender.cache_clear()
    yield
    resolve_blender.cache_clear()


@needs_bwrap
def test_host_only_launcher_is_rejected_with_the_confinement_diagnostic(
    tmp_path: Path,
    isolated_candidates: None,
) -> None:
    launcher = _host_only_launcher(tmp_path)
    unconfined = subprocess.run(
        [str(launcher), "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert unconfined.returncode == 0
    assert "Blender" in unconfined.stdout

    with pytest.raises(BlenderResolutionError) as captured:
        resolve_blender(str(launcher), shot_bound=True)

    failure = captured.value
    assert failure.requested == str(launcher)
    assert [item.candidate for item in failure.rejections] == [str(launcher)]
    assert failure.rejections[0].reason
    message = str(failure)
    assert "inside the mandatory filesystem confinement" in message
    assert str(launcher) in message
    assert "BLENDER_BIN" in message


@needs_bwrap
def test_session_start_refuses_a_host_only_launcher_before_spawning_a_worker(
    tmp_path: Path,
    isolated_candidates: None,
) -> None:
    launcher_home = tmp_path / "launcher"
    launcher_home.mkdir()
    launcher = _host_only_launcher(launcher_home)
    shot = tmp_path / "shot"
    shot.mkdir()
    session = BlenderSession(blender=str(launcher), cwd=shot)

    with pytest.raises(BlenderResolutionError, match="mandatory filesystem confinement"):
        session.start()

    assert session.proc is None


@needs_bwrap
def test_selected_blender_is_proven_inside_the_confinement() -> None:
    resolve_blender.cache_clear()
    try:
        selected = resolve_blender("blender", shot_bound=True)
    except BlenderResolutionError as exc:
        pytest.skip(f"no confinement-capable Blender on this host: {exc}")
    assert blender_resolution._confined_version_probe(selected, shot_bound=True) is None
    assert resolve_blender("blender", shot_bound=True) == selected
