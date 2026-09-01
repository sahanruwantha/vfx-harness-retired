"""Final media publication requires a current passing acceptance outcome."""

from __future__ import annotations

import hashlib
import signal
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from vfx_harness.application import render_shot
from vfx_harness.domain.brief import Shot
from vfx_harness.observability import run_artifacts
from vfx_harness.orchestration.authority_selection_transaction import (
    AuthoritySelectionToken,
)


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


def _token(label: str, *, revision: int = 1) -> AuthoritySelectionToken:
    return AuthoritySelectionToken(
        plan_revision=revision,
        plan_pointer_sha256=hashlib.sha256(f"plan:{label}".encode()).hexdigest(),
        jit_revision=revision,
        jit_pointer_sha256=hashlib.sha256(f"jit:{label}".encode()).hexdigest(),
    )


def _patch_render_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = tmp_path / "build" / "layer.py"
    script.parent.mkdir()
    script.write_text("# layer\n", encoding="utf-8")

    class FakeSession:
        artifacts = tmp_path / "blender-artifacts"

        def run(self, _code: str) -> None:
            return None

        def render(self, **_kwargs) -> None:
            self.artifacts.mkdir(exist_ok=True)

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        render_shot,
        "BlenderSession",
        lambda **_kwargs: SimpleNamespace(start=FakeSession),
    )
    monkeypatch.setattr(render_shot, "_run_artifact_script", lambda *_args: None)
    monkeypatch.setattr(
        render_shot,
        "_prepared_snapshot_replay_input",
        lambda _snapshot, _index: object(),
    )
    monkeypatch.setattr(
        render_shot.subprocess,
        "run",
        lambda arguments, **_kwargs: Path(arguments[-1]).write_bytes(b"new mp4"),
    )
    monkeypatch.setattr(render_shot, "_chain_scripts", lambda *_args, **_kwargs: [script])
    monkeypatch.setattr(
        render_shot,
        "capture_final_render_snapshot",
        lambda _shot, selected, outcome, scripts, **_kwargs: SimpleNamespace(
            selected_authority=selected,
            outcome_digest=outcome.digest,
            replay_scripts=tuple(scripts),
            unit_state_digests=(),
            assets_dir=_shot.folder / "assets",
            replay_root=_shot.folder,
            replay_root_binding=object(),
            worker_file_bindings=(),
        ),
    )
    monkeypatch.setattr(
        render_shot,
        "final_render_state_locks",
        lambda *_args, **_kwargs: nullcontext(),
    )
    monkeypatch.setattr(
        render_shot,
        "require_snapshot_inputs_current",
        lambda *_args, **_kwargs: None,
    )


def test_full_render_refuses_before_replay_when_acceptance_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shot = _shot(tmp_path)
    monkeypatch.setattr(
        render_shot,
        "require_current_accepted_outcome",
        lambda _shot, _selected: (_ for _ in ()).throw(
            ValueError("typed acceptance is missing")
        ),
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


def test_final_render_reuses_one_snapshot_and_rechecks_it_before_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shot = _shot(tmp_path)
    _patch_render_runtime(tmp_path, monkeypatch)
    token = _token("selected")
    selected = SimpleNamespace(selection_token=token, assertion=object())
    outcome = SimpleNamespace(bundle_digest="a" * 64, digest="b" * 64)
    resolutions: list[Path] = []
    acceptance_inputs: list[object] = []
    chain_inputs: list[object] = []

    def resolve(root: Path) -> object:
        resolutions.append(Path(root))
        return selected

    def require_acceptance(_shot: Shot, authority: object) -> object:
        acceptance_inputs.append(authority)
        return outcome

    def chain(*_args, selected_authority: object, **_kwargs) -> list[Path]:
        chain_inputs.append(selected_authority)
        return [tmp_path / "build" / "layer.py"]

    monkeypatch.setattr(
        render_shot.authority_selection,
        "resolve_selected_authority",
        resolve,
    )
    monkeypatch.setattr(render_shot, "require_current_accepted_outcome", require_acceptance)
    monkeypatch.setattr(render_shot, "_chain_scripts", chain)
    monkeypatch.setattr(
        render_shot,
        "authority_selection_lock",
        lambda *_args, **_kwargs: nullcontext(),
    )
    monkeypatch.setattr(
        render_shot,
        "read_authority_selection_heads",
        lambda _root: SimpleNamespace(token=token),
    )
    monkeypatch.setattr(
        render_shot.authority_selection,
        "resolve_selected_authority_from_heads",
        lambda _root, _heads: selected,
    )
    output = tmp_path / "deliverable.mp4"

    result = render_shot.render_mp4(shot, out=output)

    assert result == output
    assert output.read_bytes() == b"new mp4"
    assert resolutions == [shot.folder]
    assert acceptance_inputs == [selected, selected, selected]
    assert chain_inputs == [selected]
    assert not list(tmp_path.glob(".deliverable.pending-*.mp4"))


def test_final_render_publication_does_not_reenter_selection_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shot = _shot(tmp_path)
    selected = render_shot.authority_selection.resolve_selected_authority(tmp_path)
    snapshot = SimpleNamespace(
        selected_authority=selected,
        outcome_digest="b" * 64,
        unit_state_digests=(),
    )
    staged = tmp_path / ".deliverable.pending.mp4"
    staged.write_bytes(b"new mp4")
    output = tmp_path / "deliverable.mp4"
    monkeypatch.setattr(
        render_shot,
        "require_current_accepted_outcome",
        lambda *_args, **_kwargs: SimpleNamespace(digest=snapshot.outcome_digest),
    )
    monkeypatch.setattr(
        render_shot,
        "require_snapshot_inputs_current",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        render_shot,
        "final_render_state_locks",
        lambda *_args, **_kwargs: nullcontext(),
    )

    prior_handler = signal.getsignal(signal.SIGALRM)

    def timeout(_signum, _frame) -> None:
        raise TimeoutError("final-render publication re-entered its selection lock")

    signal.signal(signal.SIGALRM, timeout)
    signal.setitimer(signal.ITIMER_REAL, 3.0)
    try:
        render_shot._publish_final_render(shot, staged, output, snapshot)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        signal.signal(signal.SIGALRM, prior_handler)

    assert output.read_bytes() == b"new mp4"


def test_final_render_postverify_conflict_restores_exact_predecessor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shot = _shot(tmp_path)
    _patch_render_runtime(tmp_path, monkeypatch)
    token = _token("selected")
    selected = SimpleNamespace(selection_token=token, assertion=object())
    outcome = SimpleNamespace(bundle_digest="a" * 64, digest="b" * 64)
    output = tmp_path / "deliverable.mp4"
    output.write_bytes(b"old mp4")
    observed_outputs: list[bytes] = []

    def require_current(_shot: Shot, _snapshot: object) -> None:
        observed_outputs.append(output.read_bytes())
        if len(observed_outputs) == 2:
            raise render_shot.FinalRenderSnapshotError(
                "script changed at the final rename seam"
            )

    monkeypatch.setattr(
        render_shot.authority_selection,
        "resolve_selected_authority",
        lambda _root: selected,
    )
    monkeypatch.setattr(
        render_shot,
        "require_current_accepted_outcome",
        lambda *_args, **_kwargs: outcome,
    )
    monkeypatch.setattr(
        render_shot,
        "require_snapshot_inputs_current",
        require_current,
    )
    monkeypatch.setattr(
        render_shot,
        "authority_selection_lock",
        lambda *_args, **_kwargs: nullcontext(),
    )
    monkeypatch.setattr(
        render_shot,
        "read_authority_selection_heads",
        lambda _root: SimpleNamespace(token=token),
    )
    monkeypatch.setattr(
        render_shot.authority_selection,
        "resolve_selected_authority_from_heads",
        lambda _root, _heads: selected,
    )

    with pytest.raises(render_shot.IncompleteRender, match="changed during final render"):
        render_shot.render_mp4(shot, out=output)

    assert observed_outputs == [b"old mp4", b"new mp4"]
    assert output.read_bytes() == b"old mp4"
    assert not list(tmp_path.glob(".deliverable.mp4.predecessor-*.mp4"))
    assert not list(tmp_path.glob(".deliverable.pending-*.mp4"))


def test_final_render_postverify_conflict_restores_output_absence(
    tmp_path: Path,
) -> None:
    staged = tmp_path / ".deliverable.pending.mp4"
    staged.write_bytes(b"new mp4")
    output = tmp_path / "deliverable.mp4"

    def reject() -> None:
        raise render_shot.FinalRenderSnapshotError(
            "accepted input changed at the final rename seam"
        )

    with pytest.raises(render_shot.FinalRenderSnapshotError):
        render_shot._publish_media_transaction(
            staged,
            output,
            postcondition=reject,
        )

    assert not output.exists()
    assert not staged.exists()


def test_final_render_refuses_to_replace_output_after_authority_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shot = _shot(tmp_path)
    _patch_render_runtime(tmp_path, monkeypatch)
    selected = SimpleNamespace(
        selection_token=_token("selected"),
        assertion=object(),
    )
    monkeypatch.setattr(
        render_shot.authority_selection,
        "resolve_selected_authority",
        lambda _root: selected,
    )
    monkeypatch.setattr(
        render_shot,
        "require_current_accepted_outcome",
        lambda _shot, _selected: SimpleNamespace(
            bundle_digest="a" * 64,
            digest="b" * 64,
        ),
    )
    monkeypatch.setattr(
        render_shot,
        "authority_selection_lock",
        lambda *_args, **_kwargs: nullcontext(),
    )
    monkeypatch.setattr(
        render_shot,
        "read_authority_selection_heads",
        lambda _root: SimpleNamespace(token=_token("replacement", revision=2)),
    )
    output = tmp_path / "deliverable.mp4"
    output.write_bytes(b"old mp4")

    with pytest.raises(render_shot.IncompleteRender, match="changed during final render"):
        render_shot.render_mp4(shot, out=output)

    assert output.read_bytes() == b"old mp4"
    assert not list(tmp_path.glob(".deliverable.pending-*.mp4"))


@pytest.mark.parametrize("mutated_input", ["script", "construction_glb"])
def test_final_render_refuses_live_accepted_input_mutation_and_keeps_old_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutated_input: str,
) -> None:
    shot = _shot(tmp_path)
    _patch_render_runtime(tmp_path, monkeypatch)
    source_script = tmp_path / "build" / "layer.py"
    original_script = source_script.read_bytes()
    construction = tmp_path / "build" / "construction" / f"{'a' * 64}.glb"
    construction.parent.mkdir()
    construction.write_bytes(b"glTFaccepted")
    mutation_target = source_script if mutated_input == "script" else construction
    expected_target_digest = hashlib.sha256(mutation_target.read_bytes()).hexdigest()

    token = _token("selected")
    selected = SimpleNamespace(selection_token=token, assertion=object())
    outcome = SimpleNamespace(bundle_digest="a" * 64, digest="b" * 64)
    snapshot_script = tmp_path / "runs" / "render" / "scratch" / "build" / "layer.py"
    executed: list[bytes] = []

    def capture(_shot, authority, accepted, _scripts, **_kwargs):
        snapshot_script.parent.mkdir(parents=True)
        snapshot_script.write_bytes(original_script)
        return SimpleNamespace(
            selected_authority=authority,
            outcome_digest=accepted.digest,
            replay_scripts=(snapshot_script,),
            unit_state_digests=(),
            assets_dir=tmp_path / "assets",
            replay_root=snapshot_script.parents[3],
            replay_root_binding=object(),
            worker_file_bindings=(),
        )

    class MutatingSession:
        artifacts = tmp_path / "blender-artifacts"

        def start(self):
            mutation_target.write_bytes(b"changed during render")
            return self

        def run(self, _code: str) -> None:
            return None

        def render(self, **_kwargs) -> None:
            self.artifacts.mkdir(exist_ok=True)

        def close(self) -> None:
            return None

    def require_current(_shot, snapshot) -> None:
        observed = hashlib.sha256(mutation_target.read_bytes()).hexdigest()
        if observed != expected_target_digest:
            raise render_shot.FinalRenderSnapshotError(
                f"{mutated_input} changed during final render"
            )

    monkeypatch.setattr(
        render_shot.authority_selection,
        "resolve_selected_authority",
        lambda _root: selected,
    )
    monkeypatch.setattr(
        render_shot,
        "require_current_accepted_outcome",
        lambda *_args, **_kwargs: outcome,
    )
    monkeypatch.setattr(render_shot, "capture_final_render_snapshot", capture)
    monkeypatch.setattr(render_shot, "BlenderSession", lambda **_kwargs: MutatingSession())
    monkeypatch.setattr(
        render_shot,
        "_run_artifact_script",
        lambda _session, path, _prepared: executed.append(path.read_bytes()),
    )
    monkeypatch.setattr(render_shot, "require_snapshot_inputs_current", require_current)
    monkeypatch.setattr(
        render_shot,
        "authority_selection_lock",
        lambda *_args, **_kwargs: nullcontext(),
    )
    monkeypatch.setattr(
        render_shot,
        "read_authority_selection_heads",
        lambda _root: SimpleNamespace(token=token),
    )
    monkeypatch.setattr(
        render_shot.authority_selection,
        "resolve_selected_authority_from_heads",
        lambda _root, _heads: selected,
    )
    output = tmp_path / "deliverable.mp4"
    output.write_bytes(b"old mp4")

    with pytest.raises(render_shot.IncompleteRender, match="changed during final render"):
        render_shot.render_mp4(shot, out=output)

    assert executed == [original_script]
    assert output.read_bytes() == b"old mp4"
    assert not list(tmp_path.glob(".deliverable.pending-*.mp4"))
