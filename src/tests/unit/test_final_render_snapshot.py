"""Final replay is attributed to immutable accepted bytes and checkpoint state."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.architecture.test_staged_architecture import _unit
from tests.unit_attempt_fixtures import (
    ABSENT_SELECTION_TOKEN,
    claim_for_build,
    freeze_unit,
    publish_passed_evaluation,
)
from vfx_harness.agents.builder.prior import _run_artifact_script
from vfx_harness.application import final_render_snapshot, render_shot
from vfx_harness.blender.filesystem_confinement import prepared_worker_command
from vfx_harness.blender.session import BlenderError
from vfx_harness.domain.acceptance_outcomes import (
    AcceptanceMomentOutcome,
    AcceptanceOutcome,
)
from vfx_harness.domain.brief import Shot
from vfx_harness.domain.refobs import PROMOTED_CONSTRUCTION_SCHEMA
from vfx_harness.observability import run_artifacts
from vfx_harness.orchestration import unit_state, unit_state_claims
from vfx_harness.orchestration.authority_selection_transaction import (
    AuthoritySelectionToken,
)
from vfx_harness.orchestration.generate_construction import (
    CONSTRUCTION_PIN,
    FINAL_RENDER_CONSTRUCTION_POINTER_SCHEMA,
    pin_for_script,
)


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _shot(root: Path) -> Shot:
    return Shot(
        folder=root,
        frontmatter={
            "id": "final-render-snapshot",
            "frames": 1,
            "fps": 24,
            "resolution": [16, 16],
            "engine": "BLENDER_EEVEE_NEXT",
        },
        body="fixture",
    )


def _capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    with_construction: bool = False,
) -> tuple[
    final_render_snapshot.FinalRenderSnapshot,
    Path,
    Path | None,
]:
    shot = _shot(tmp_path)
    (tmp_path / "brief.md").write_text("accepted brief\n", encoding="utf-8")
    refs = tmp_path / "refs"
    refs.mkdir()
    reference = refs / "moment.png"
    reference.write_bytes(b"reference")
    asset = tmp_path / "assets" / "hero" / "model.glb"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"glTFaccepted-asset")
    (tmp_path / "assets" / "empty-library").mkdir()
    render = tmp_path / "runs" / "acceptance" / "evidence" / "moment.png"
    render.parent.mkdir(parents=True)
    render.write_bytes(b"accepted render")

    unit = _unit("form", script_span="build/units/01/form.py")
    script = tmp_path / unit.mutates.script_spans[0]
    script.parent.mkdir(parents=True)
    script_bytes = b"print('accepted')\n"
    script.write_bytes(script_bytes)
    script_digest = _digest(script_bytes)
    construction_glb = None
    if with_construction:
        construction_bytes = b"glTFaccepted-construction"
        construction_digest = _digest(construction_bytes)
        construction_glb = (
            tmp_path / "build" / "construction" / f"{construction_digest}.glb"
        )
        construction_glb.parent.mkdir(parents=True)
        construction_glb.write_bytes(construction_bytes)
        script.with_suffix(".construction.json").write_text(
            json.dumps(
                {
                    "schema": PROMOTED_CONSTRUCTION_SCHEMA,
                    "unit_id": "form",
                    "layer_id": "1",
                    "unit_digest": unit_state.unit_digest(unit),
                    "sha256": construction_digest,
                    "glb": construction_glb.relative_to(tmp_path).as_posix(),
                    "witnesses": [],
                    "view_count": 1,
                    "generator": "fixture",
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    layer = SimpleNamespace(id="1", stages=(unit,))

    plan_hash = "1" * 64
    unit_state.initialize(tmp_path, "1", (unit,), plan_hash=plan_hash)
    attempt = claim_for_build(
        tmp_path,
        "1",
        (unit,),
        unit.id,
        plan_hash=plan_hash,
    )
    freeze_unit(
        tmp_path,
        "1",
        unit,
        attempt,
        active_contract_ids=(),
        candidate_hash="missing",
        settings_hash=_digest(b"settings"),
        script_hash=script_digest,
        input_hash=_digest(b"inputs"),
    )
    unit_state.transition(
        tmp_path,
        "1",
        unit.id,
        "evaluating",
        reason="fixture",
        attempt=attempt,
        selection_token=ABSENT_SELECTION_TOKEN,
    )
    publish_passed_evaluation(tmp_path, "1", unit, attempt)
    completion = unit_state_claims.complete_unit_attempt(
        tmp_path,
        "1",
        unit.id,
        (unit,),
        attempt,
        expected_plan_hash=plan_hash,
        selection_token=ABSENT_SELECTION_TOKEN,
        reason="fixture",
        evidence=["fixture:accepted"],
    )

    layer_outcome = tmp_path / "plans" / "outcomes" / "layer-1.json"
    layer_outcome.parent.mkdir(parents=True)
    layer_outcome.write_text('{"schema":3,"fixture":true}\n', encoding="utf-8")
    finalization_receipt_digest = _digest(b"layer-finalization-receipt")

    chain = (
        {
            "layer_id": "1",
            "status": "passed",
            "script": unit.mutates.script_spans[0],
            "script_sha256": script_digest,
            "finalization_receipt_digest": finalization_receipt_digest,
            "layer_outcome": layer_outcome.relative_to(tmp_path).as_posix(),
            "layer_outcome_sha256": _digest(layer_outcome.read_bytes()),
            "units": [
                {
                    "unit_id": unit.id,
                    "unit_digest": unit_state.unit_digest(unit),
                    "completion_receipt_digest": completion.receipt_digest,
                    "script": unit.mutates.script_spans[0],
                    "script_sha256": script_digest,
                }
            ],
        },
    )
    authority = final_render_snapshot.acceptance_stop.AcceptanceAuthoritySnapshot(
        bundle_digest="a" * 64,
        view_digest="b" * 64,
        judgment_debt_state_digest="c" * 64,
        acceptance_artifact_sha256="d" * 64,
        layers_artifact_sha256="e" * 64,
        selected_moments=(
            {
                "id": "M1",
                "frame": 1,
                "ref": "refs/moment.png",
                "ref_sha256": _digest(reference.read_bytes()),
                "fingerprint": {},
            },
        ),
        chain=chain,
    )
    outcome = AcceptanceOutcome(
        authority_digest=authority.digest,
        bundle_digest=authority.bundle_digest,
        view_digest=authority.view_digest,
        chain_digest=authority.chain_digest,
        moments=(
            AcceptanceMomentOutcome(
                moment_id="M1",
                passed=True,
                decided_by="critic",
                evidence_digest="f" * 64,
            ),
        ),
    )
    ledger = {
        "shot": shot.id,
        "milestones": {
            "1": {
                "status": "passed",
                "script": unit.mutates.script_spans[0],
                "script_sha256": script_digest,
                "finalization_receipt_digest": finalization_receipt_digest,
            }
        },
        "acceptance": {
            "outcome": outcome.as_dict(),
            "moments": {
                "M1": {
                    "render": render.relative_to(tmp_path).as_posix(),
                    "ref": reference.relative_to(tmp_path).as_posix(),
                }
            },
        },
    }
    (tmp_path / "shot.json").write_text(
        json.dumps(ledger, indent=2) + "\n",
        encoding="utf-8",
    )
    selected_artifact = tmp_path / "selected" / "layers.json"
    selected_artifact.parent.mkdir()
    selected_artifact.write_text("{}\n", encoding="utf-8")
    selected = SimpleNamespace(
        selection_token=AuthoritySelectionToken(
            plan_revision=1,
            plan_pointer_sha256="1" * 64,
            jit_revision=0,
            jit_pointer_sha256=None,
        ),
        artifact_paths={"layers.json": selected_artifact},
    )
    monkeypatch.setattr(
        final_render_snapshot,
        "load_milestones",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        final_render_snapshot.acceptance_stop,
        "capture_acceptance_authority",
        lambda *_args, **_kwargs: authority,
    )
    monkeypatch.setattr(
        final_render_snapshot,
        "selected_layer_chain",
        lambda *_args, **_kwargs: (layer,),
    )
    scratch = run_artifacts.create(tmp_path, "final-render-snapshot").scratch
    snapshot = final_render_snapshot.capture_final_render_snapshot(
        shot,
        selected,
        outcome,
        (script,),
        scratch=scratch,
    )
    return snapshot, script, construction_glb


def test_replay_uses_immutable_accepted_script_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot, script, _construction = _capture(tmp_path, monkeypatch)

    script.write_bytes(b"print('changed while rendering')\n")

    assert snapshot.replay_scripts[0].read_bytes() == b"print('accepted')\n"
    with pytest.raises(
        final_render_snapshot.FinalRenderSnapshotError,
        match=r"accepted source script .* changed during final render",
    ):
        final_render_snapshot.require_snapshot_inputs_current(_shot(tmp_path), snapshot)


def test_recreated_replay_root_cannot_bless_detached_worker_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot, _script, _construction = _capture(tmp_path, monkeypatch)
    retired = snapshot.replay_root.with_name(f"{snapshot.replay_root.name}-retired")
    snapshot.replay_root.rename(retired)
    shutil.copytree(retired, snapshot.replay_root)

    with pytest.raises(
        final_render_snapshot.FinalRenderSnapshotError,
        match=r"final-render replay root.*changed before publication",
    ):
        final_render_snapshot.require_snapshot_inputs_current(_shot(tmp_path), snapshot)


def test_transient_replay_script_swap_executes_prebound_snapshot_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot, _script, _construction = _capture(tmp_path, monkeypatch)
    replay = snapshot.replay_scripts[0]
    prepared = render_shot._prepared_snapshot_replay_input(snapshot, 0)
    retired = replay.with_name(f".{replay.name}.captured")
    executed: list[tuple[str, str]] = []

    class RecordingSession:
        artifacts = None
        cwd = None

        def run(self, code: str, **kwargs):
            executed.append((code, str(kwargs.get("execution_policy") or "live")))
            if len(executed) == 1:
                replay.rename(retired)
                replay.write_bytes(b"print('transient replacement')\n")
                replay.unlink()
                retired.rename(replay)
            return {}

    with pytest.raises(BlenderError, match="trusted path changed"):
        _run_artifact_script(RecordingSession(), replay, prepared)

    assert executed[0] == ("print('accepted')\n", "artifact")
    assert replay.read_bytes() == b"print('accepted')\n"
    with pytest.raises(
        final_render_snapshot.FinalRenderSnapshotError,
        match="immutable replay script 0 changed during final render",
    ):
        final_render_snapshot.require_snapshot_inputs_current(_shot(tmp_path), snapshot)


def test_transient_worker_dependency_swap_reads_descriptor_pinned_member(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot, _script, _construction = _capture(tmp_path, monkeypatch)
    dependency = snapshot.assets_dir / "hero" / "model.glb"
    member = next(
        binding
        for binding in snapshot.worker_file_bindings
        if binding.path == dependency
    )
    layout = run_artifacts.active(tmp_path)
    worker_scratch = layout.scratch / "blender"
    worker_scratch.mkdir(exist_ok=True)
    retired = dependency.with_name(f".{dependency.name}.captured")
    source = (
        "from pathlib import Path\n"
        f"print(Path({str(dependency)!r}).read_bytes().decode('ascii'))\n"
    )

    with prepared_worker_command(
        ["/usr/bin/python3", "-c", source],
        writable_roots=(worker_scratch,),
        readable_roots=(snapshot.replay_root,),
        readable_root_bindings=(snapshot.replay_root_binding,),
        readable_file_bindings=(member,),
        authority_root=tmp_path,
        current_run_root=layout.root,
    ) as command:
        dependency.rename(retired)
        dependency.write_bytes(b"glTFtransient-replacement")
        try:
            completed = subprocess.run(
                command.argv,
                capture_output=True,
                text=True,
                check=False,
                pass_fds=command.pass_fds,
            )
        finally:
            dependency.unlink()
            retired.rename(dependency)

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "glTFaccepted-asset"
    with pytest.raises(
        final_render_snapshot.FinalRenderSnapshotError,
        match="immutable replay dependency 0 changed during final render",
    ):
        final_render_snapshot.require_snapshot_inputs_current(_shot(tmp_path), snapshot)


def test_final_render_locks_reverse_topology_in_canonical_location_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    @contextmanager
    def state_lock(_folder: Path, layer_id: str, *, exclusive: bool):
        assert exclusive is False
        events.append(f"enter:{layer_id}")
        try:
            yield
        finally:
            events.append(f"exit:{layer_id}")

    @contextmanager
    def shot_lock(_path: Path, *, exclusive: bool):
        assert exclusive is False
        events.append("enter:ledger")
        try:
            yield
        finally:
            events.append("exit:ledger")

    monkeypatch.setattr(final_render_snapshot, "unit_state_lock", state_lock)
    monkeypatch.setattr(final_render_snapshot, "ledger_lock", shot_lock)
    snapshot = SimpleNamespace(unit_state_digests=(("z", "1"), ("a", "2")))

    with final_render_snapshot.final_render_state_locks(_shot(tmp_path), snapshot):
        events.append("held")

    assert events == [
        "enter:a",
        "enter:z",
        "enter:ledger",
        "held",
        "exit:ledger",
        "exit:z",
        "exit:a",
    ]


def test_checkpoint_invalidation_during_render_invalidates_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot, _script, _construction = _capture(tmp_path, monkeypatch)
    layer = snapshot.layers[0]
    unit_state.invalidate_checkpoint(
        tmp_path,
        "1",
        "form",
        layer.stages,
        reason="injected final-render race",
        evidence=["fixture"],
    )

    with pytest.raises(
        final_render_snapshot.FinalRenderSnapshotError,
        match="no current passed checkpoint",
    ):
        final_render_snapshot.require_snapshot_inputs_current(_shot(tmp_path), snapshot)


def test_ledger_mutation_during_render_invalidates_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot, _script, _construction = _capture(tmp_path, monkeypatch)
    ledger_path = tmp_path / "shot.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    ledger["milestones"]["1"]["status"] = "in_progress"
    ledger_path.write_text(json.dumps(ledger, indent=2) + "\n", encoding="utf-8")

    with pytest.raises(
        final_render_snapshot.FinalRenderSnapshotError,
        match="acceptance ledger changed during final render",
    ):
        final_render_snapshot.require_snapshot_inputs_current(_shot(tmp_path), snapshot)


def test_layer_outcome_mutation_during_render_invalidates_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot, _script, _construction = _capture(tmp_path, monkeypatch)
    snapshot.layer_outcomes[0].path.write_text(
        '{"schema":3,"fixture":"replaced"}\n',
        encoding="utf-8",
    )

    with pytest.raises(
        final_render_snapshot.FinalRenderSnapshotError,
        match="accepted layer outcome 0 changed during final render",
    ):
        final_render_snapshot.require_snapshot_inputs_current(_shot(tmp_path), snapshot)


def test_authored_input_mutation_during_render_invalidates_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot, _script, _construction = _capture(tmp_path, monkeypatch)
    (tmp_path / "brief.md").write_text("changed brief\n", encoding="utf-8")

    with pytest.raises(
        final_render_snapshot.FinalRenderSnapshotError,
        match="authored plan inputs changed during final render",
    ):
        final_render_snapshot.require_snapshot_inputs_current(_shot(tmp_path), snapshot)


def test_authored_snapshot_digest_is_derived_from_the_exact_copied_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = final_render_snapshot.authored_input_bytes
    calls = 0

    def read_then_mutate(root: Path) -> dict[str, bytes]:
        nonlocal calls
        inputs = original(root)
        calls += 1
        if calls == 1:
            (root / "brief.md").write_text("changed after copy read\n", encoding="utf-8")
        return inputs

    monkeypatch.setattr(
        final_render_snapshot,
        "authored_input_bytes",
        read_then_mutate,
    )

    with pytest.raises(
        final_render_snapshot.FinalRenderSnapshotError,
        match="authored plan inputs changed during final render",
    ):
        _capture(tmp_path, monkeypatch)


def test_asset_mutation_during_render_invalidates_snapshot_but_not_replay_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot, _script, _construction = _capture(tmp_path, monkeypatch)
    source_asset = tmp_path / "assets" / "hero" / "model.glb"
    replay_asset = snapshot.assets_dir / "hero" / "model.glb"

    source_asset.write_bytes(b"glTFchanged-live-asset")

    assert replay_asset.read_bytes() == b"glTFaccepted-asset"
    assert (snapshot.assets_dir / "empty-library").is_dir()
    with pytest.raises(
        final_render_snapshot.FinalRenderSnapshotError,
        match="shot asset tree changed during final render",
    ):
        final_render_snapshot.require_snapshot_inputs_current(_shot(tmp_path), snapshot)


def test_construction_replay_uses_snapshot_glb_not_live_promoted_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot, _script, construction = _capture(
        tmp_path,
        monkeypatch,
        with_construction=True,
    )
    assert construction is not None
    construction.write_bytes(b"glTFchanged-live-construction")

    replay_pointer = snapshot.replay_scripts[0].with_suffix(".construction.json")
    pointer = json.loads(replay_pointer.read_text(encoding="utf-8"))
    assert pointer["schema"] == FINAL_RENDER_CONSTRUCTION_POINTER_SCHEMA
    replay_glb = replay_pointer.parents[3] / pointer["glb"]
    assert replay_glb.read_bytes() == b"glTFaccepted-construction"

    artifacts = run_artifacts.active(tmp_path).scratch / "blender"
    artifacts.mkdir(exist_ok=True)
    session = SimpleNamespace(artifacts=artifacts, cwd=tmp_path)
    pin_for_script(session, snapshot.replay_scripts[0])
    pin = json.loads((artifacts / CONSTRUCTION_PIN).read_text(encoding="utf-8"))
    assert Path(pin["snapshot_glb"]) == replay_glb
    assert Path(pin["snapshot_glb"]).read_bytes() == b"glTFaccepted-construction"

    with pytest.raises(
        final_render_snapshot.FinalRenderSnapshotError,
        match=r"acceptance evidence dependency .* changed during final render",
    ):
        final_render_snapshot.require_snapshot_inputs_current(_shot(tmp_path), snapshot)
