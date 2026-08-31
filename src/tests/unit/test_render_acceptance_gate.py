"""Final media publication requires a current passing acceptance outcome."""

from __future__ import annotations

from pathlib import Path

import pytest

from vfx_harness.application import render_shot
from vfx_harness.domain.brief import Shot
from vfx_harness.observability import run_artifacts


def _shot(root: Path) -> Shot:
    return Shot(
        folder=root,
        frontmatter={
            "id": "render-gate",
            "frames": 1,
            "fps": 24,
            "resolution": [16, 16],
            "engine": "BLENDER_EEVEE_NEXT",
        },
        body="fixture",
    )


def test_full_render_refuses_before_replay_when_acceptance_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shot = _shot(tmp_path)
    monkeypatch.setattr(
        render_shot,
        "require_current_accepted_outcome",
        lambda _shot: (_ for _ in ()).throw(ValueError("typed acceptance is missing")),
    )
    monkeypatch.setattr(
        render_shot,
        "_chain_scripts",
        lambda *_args, **_kwargs: pytest.fail("unaccepted media must stop before replay"),
    )

    with pytest.raises(render_shot.IncompleteRender, match="typed acceptance is missing"):
        render_shot.render_mp4(shot)


def test_preview_defaults_never_use_the_deliverables_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    shot = _shot(tmp_path)
    layout = run_artifacts.create(tmp_path, "render-preview")

    partial = render_shot._render_output_path(
        shot,
        upto="form/2",
        force=False,
        out=None,
    )
    forced = render_shot._render_output_path(
        shot,
        upto=None,
        force=True,
        out=None,
    )
    final = render_shot._render_output_path(
        shot,
        upto=None,
        force=False,
        out=None,
    )

    assert partial == layout.scratch / "previews" / "render-gate_upto-form-2.mp4"
    assert forced == layout.scratch / "previews" / "render-gate_forced.mp4"
    assert final == layout.deliverables / "render-gate_full.mp4"
