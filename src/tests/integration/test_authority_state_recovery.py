from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import vfx_harness.orchestration.jit_materialization.publish as jit_publish
from tests.architecture.test_staged_architecture import _unit
from tests.integration.test_judgment_debt_public_pipeline import (
    _pass_layer_unit,
    _payload_for_bundle,
    _public_fixture_root,
    _publish_payload,
)
from tests.integration.test_lifecycle_fixture import (
    _approve_hold_decision,
    _deferred_root,
    _root_materialization,
)
from tests.materialization_support import attest_exact_materialization_view
from tests.unit.test_judgment_debt_materialization import (
    _camera_payload,
    _form_payload,
)
from tests.unit.test_layer_finalization_state import _complete_passed_layer
from tests.unit.test_layer_publication import _write_projections
from tests.unit.test_plan_records import _candidate
from vfx_harness.application.authority_state_recovery import recover_authority_state
from vfx_harness.cli import main as vfx_main
from vfx_harness.domain.authority_head_records import canonical_json_bytes
from vfx_harness.domain.authority_state_records import (
    AuthorityStatePendingPointer,
    AuthorityStateRecoveryResult,
    AuthorityStateTransitionIntent,
)
from vfx_harness.evaluation.authority_state_transition import (
    evaluate_authority_state_transition,
)
from vfx_harness.observability import run_artifacts
from vfx_harness.orchestration import authority_state_transaction, unit_state
from vfx_harness.orchestration import unit_state_lock as state_lock_module
from vfx_harness.orchestration.authority_capsule_resolution import (
    selected_layer_capsule_digest,
)
from vfx_harness.orchestration.authority_selection import resolve_selected_authority
from vfx_harness.orchestration.authority_selection_heads import (
    read_authority_selection_heads,
)
from vfx_harness.orchestration.authority_state_context import (
    read_pending_authority_state,
    resolve_current_authority_state,
)
from vfx_harness.orchestration.authority_state_recovery import (
    AuthorityStateRecoveryError,
)
from vfx_harness.orchestration.authority_state_store import (
    read_authority_state_bytes,
    read_authority_state_record,
    read_current_bytes,
    read_pending_bytes,
    replace_pending_bytes,
)
from vfx_harness.orchestration.jit_materialization import (
    finalize_materialization_candidate,
    publish_materialization,
)
from vfx_harness.orchestration.ledger import load_layers_from_path
from vfx_harness.orchestration.plan_authority import (
    prepare_consumer_view,
    publish_current,
    selected_artifact_path,
)
from vfx_harness.orchestration.unit_state import load
from vfx_harness.orchestration.unit_state_lock import (
    remove_state_file,
    unit_state_path,
    write_state_file_bytes,
)
from vfx_harness.orchestration.unit_state_serialization import (
    serialize_work_unit_state,
)


class _InjectedCrash(RuntimeError):
    pass


def _finalized_root_candidate(root: Path):
    _candidate(root)
    _deferred_root(root)
    layout = run_artifacts.create(root, "recovery-fixture")
    bundle = publish_current(root, layout, outcome="clean_with_deferred")
    _approve_hold_decision(root, bundle.content_hash)
    candidate = _root_materialization(root, bundle.content_hash)
    result = finalize_materialization_candidate(
        root,
        candidate,
        prepare_consumer_view(layout),
    )
    assert result.clean
    return candidate


def _heterogeneous_two_member_candidate(root: Path) -> Path:
    """Prepare L2 publication with one preserved L1 state and one new L2 state."""

    _public_fixture_root(root)
    layout = run_artifacts.create(root, "heterogeneous-recovery-fixture")
    bundle = publish_current(root, layout, outcome="clean_with_deferred")
    camera_candidate = _payload_for_bundle(
        root,
        "camera-jit.json",
        _camera_payload(),
        bundle.content_hash,
    )
    _publish_payload(root, camera_candidate)
    _pass_layer_unit(root, "1", "camera")
    selected = resolve_selected_authority(root)
    camera_layer = load_layers_from_path(
        selected_artifact_path(root, "layers.json")
    )["1"]
    plan_hash = selected_layer_capsule_digest(root, "1", selected)
    *_, receipt = _complete_passed_layer(
        root,
        camera_layer,
        plan_hash=plan_hash,
        selection_token=selected.selection_token,
    )
    _write_projections(root, receipt)
    form_candidate = _payload_for_bundle(
        root,
        "form-jit.json",
        _form_payload(),
        bundle.content_hash,
    )
    attest_exact_materialization_view(root, form_candidate)
    return form_candidate


def _inject_crash(
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    *,
    identity: str | None = None,
) -> None:
    def crash_at_exact_boundary(
        observed: str,
        *,
        identity: str | None = None,
    ) -> None:
        if observed == boundary and (
            target_identity is None or identity == target_identity
        ):
            raise _InjectedCrash

    target_identity = identity
    monkeypatch.setattr(
        authority_state_transaction,
        "_authority_state_write_boundary",
        crash_at_exact_boundary,
    )


def _pending_intent(root: Path):
    pending = read_pending_authority_state(root)
    assert pending is not None
    value, _stored = read_authority_state_record(
        root,
        locator=pending.intent_ref.locator,
        sha256=pending.intent_ref.sha256,
    )
    return pending, AuthorityStateTransitionIntent.parse(value)


def _pending_root(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    boundary: str = "after_pending_publication",
) -> bytes:
    candidate = _finalized_root_candidate(root)
    predecessor = read_current_bytes(root)
    assert predecessor is not None
    _inject_crash(monkeypatch, boundary)
    with pytest.raises(_InjectedCrash):
        publish_materialization(root, candidate)
    assert read_pending_bytes(root) is not None
    monkeypatch.undo()
    return predecessor


@pytest.mark.parametrize(
    "boundary",
    [
        "after_pending_publication",
        "after_state_replacement",
        "after_all_state_replacements",
        "after_jit_pointer_replacement",
        "after_commit_receipt_publication",
        "after_independent_evaluation",
        "after_evaluation_receipt_publication",
        "after_coordinator_head_publication",
        "after_current_head_replacement",
        "before_pending_removal",
    ],
)
def test_pending_transition_recovers_exact_successor_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    candidate = _finalized_root_candidate(tmp_path)
    predecessor = read_current_bytes(tmp_path)
    _inject_crash(monkeypatch, boundary)

    with pytest.raises(_InjectedCrash):
        publish_materialization(tmp_path, candidate)

    assert read_pending_bytes(tmp_path) is not None
    monkeypatch.undo()
    recovered = recover_authority_state(tmp_path)

    assert recovered.disposition == "recovered"
    assert recovered.transition_revision == 2
    assert recovered.state_member_ids == ("1",)
    assert read_pending_bytes(tmp_path) is None
    assert read_current_bytes(tmp_path) != predecessor
    assert read_authority_selection_heads(tmp_path).token.jit_revision == 1
    state = load(tmp_path, "1")
    assert set(state["units"]) == {"lock"}
    assert state["units"]["lock"]["status"] == "pending"
    selected = read_current_bytes(tmp_path)

    repeated = recover_authority_state(tmp_path)
    assert repeated.disposition == "already_current"
    assert repeated.coordinator_head_ref == recovered.coordinator_head_ref
    assert repeated.intent_ref == recovered.intent_ref
    assert read_current_bytes(tmp_path) == selected
    assert resolve_current_authority_state(tmp_path).head_ref == (
        recovered.coordinator_head_ref
    )


@pytest.mark.parametrize(
    ("boundary", "identity", "layer_two_exists_at_crash"),
    [
        ("after_state_replacement", "1", False),
        ("after_all_state_replacements", None, True),
    ],
)
def test_heterogeneous_two_member_transition_recovers_from_state_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    identity: str | None,
    layer_two_exists_at_crash: bool,
) -> None:
    candidate = _heterogeneous_two_member_candidate(tmp_path)
    _inject_crash(monkeypatch, boundary, identity=identity)

    with pytest.raises(_InjectedCrash):
        publish_materialization(tmp_path, candidate)

    assert read_pending_bytes(tmp_path) is not None
    assert bool(load(tmp_path, "2")) is layer_two_exists_at_crash
    monkeypatch.undo()

    recovered = recover_authority_state(tmp_path)

    assert recovered.disposition == "recovered"
    assert recovered.state_member_ids == ("1", "2")
    assert read_pending_bytes(tmp_path) is None
    assert set(load(tmp_path, "1")["units"]) == {"camera"}
    assert set(load(tmp_path, "2")["units"]) == {"hall_form"}
    assert resolve_current_authority_state(tmp_path).head_ref == (
        recovered.coordinator_head_ref
    )
    repeated = recover_authority_state(tmp_path)
    assert repeated.disposition == "already_current"
    assert repeated.coordinator_head_ref == recovered.coordinator_head_ref


def test_already_current_recovery_source_verifies_direct_current_receipts(
    tmp_path: Path,
) -> None:
    _heterogeneous_two_member_candidate(tmp_path)
    current = resolve_current_authority_state(tmp_path)
    assert current is not None

    recovered = recover_authority_state(tmp_path)

    assert recovered.disposition == "already_current"
    assert recovered.coordinator_head_ref == current.head_ref


def test_already_current_recovery_refuses_loss_of_preserved_completion(
    tmp_path: Path,
) -> None:
    candidate = _heterogeneous_two_member_candidate(tmp_path)
    publish_materialization(tmp_path, candidate)
    current = read_current_bytes(tmp_path)
    state = load(tmp_path, "1")
    state["units"]["camera"].pop("completion_receipt")
    state["units"]["camera"]["status"] = "retryable"
    state.pop("layer_finalization")
    write_state_file_bytes(
        unit_state_path(tmp_path, "1"),
        serialize_work_unit_state(state),
    )

    with pytest.raises(
        AuthorityStateRecoveryError,
        match="preserved completion receipt",
    ):
        recover_authority_state(tmp_path)

    assert read_current_bytes(tmp_path) == current
    assert read_pending_bytes(tmp_path) is None


def test_already_current_recovery_refuses_loss_of_preserved_finalization(
    tmp_path: Path,
) -> None:
    candidate = _heterogeneous_two_member_candidate(tmp_path)
    publish_materialization(tmp_path, candidate)
    current = read_current_bytes(tmp_path)
    state = load(tmp_path, "1")
    state.pop("layer_finalization")
    write_state_file_bytes(
        unit_state_path(tmp_path, "1"),
        serialize_work_unit_state(state),
    )

    with pytest.raises(
        AuthorityStateRecoveryError,
        match="preserved terminal receipt",
    ):
        recover_authority_state(tmp_path)

    assert read_current_bytes(tmp_path) == current
    assert read_pending_bytes(tmp_path) is None


def test_crash_before_pending_leaves_predecessor_current_and_recovery_is_noop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = _finalized_root_candidate(tmp_path)
    predecessor = read_current_bytes(tmp_path)
    _inject_crash(monkeypatch, "before_pending_publication")

    with pytest.raises(_InjectedCrash):
        publish_materialization(tmp_path, candidate)

    assert read_pending_bytes(tmp_path) is None
    assert read_current_bytes(tmp_path) == predecessor
    monkeypatch.undo()

    result = recover_authority_state(tmp_path)
    assert result.disposition == "already_current"
    assert result.transition_revision == 1


def test_crash_after_pending_removal_is_already_committed_noop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = _finalized_root_candidate(tmp_path)
    _inject_crash(monkeypatch, "after_pending_removal")

    with pytest.raises(_InjectedCrash):
        publish_materialization(tmp_path, candidate)

    assert read_pending_bytes(tmp_path) is None
    monkeypatch.undo()

    result = recover_authority_state(tmp_path)
    assert result.disposition == "already_current"
    assert result.transition_revision == 2
    assert set(load(tmp_path, "1")["units"]) == {"lock"}


def test_process_death_before_state_replace_leaves_recoverable_staging_orphan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = _finalized_root_candidate(tmp_path)
    _inject_crash(monkeypatch, "after_pending_publication")
    with pytest.raises(_InjectedCrash):
        publish_materialization(tmp_path, candidate)
    monkeypatch.undo()
    script = """
import os
import sys

from vfx_harness.application.authority_state_recovery import recover_authority_state
from vfx_harness.orchestration import unit_state_lock as state_lock

real_replace = state_lock.os.replace

def die_before_state_replace(source, target, *args, **kwargs):
    if target == "layer_1.json" and kwargs.get("src_dir_fd") != kwargs.get("dst_dir_fd"):
        os._exit(73)
    return real_replace(source, target, *args, **kwargs)

state_lock.os.replace = die_before_state_replace
recover_authority_state(sys.argv[1])
"""

    crashed = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        check=False,
        cwd=Path(__file__).parents[3],
    )

    assert crashed.returncode == 73
    assert read_pending_bytes(tmp_path) is not None
    live_state_dir = tmp_path / "state/work-units"
    assert not list(live_state_dir.glob(".*.prepared.*"))
    staging = tmp_path / "state/work-unit-state-staging"
    assert len(list(staging.glob(".layer_1.json.prepared.*"))) == 1

    recovered = recover_authority_state(tmp_path)

    assert recovered.disposition == "recovered"
    assert read_pending_bytes(tmp_path) is None
    assert set(load(tmp_path, "1")["units"]) == {"lock"}


def test_pending_recovery_refuses_live_state_matching_neither_recorded_side(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    predecessor = _pending_root(tmp_path, monkeypatch)
    pending_bytes = read_pending_bytes(tmp_path)
    _pending, intent = _pending_intent(tmp_path)
    member = intent.state_members[0]
    write_state_file_bytes(
        tmp_path / member.live_locator,
        serialize_work_unit_state({"schema": 1, "marker": "neither-side"}),
    )

    with pytest.raises(AuthorityStateRecoveryError, match="neither recorded"):
        recover_authority_state(tmp_path)

    assert read_pending_bytes(tmp_path) == pending_bytes
    assert read_current_bytes(tmp_path) == predecessor


def test_pending_recovery_refuses_missing_staged_member(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    predecessor = _pending_root(tmp_path, monkeypatch)
    pending_bytes = read_pending_bytes(tmp_path)
    _pending, intent = _pending_intent(tmp_path)
    reference = intent.staged_members[0]
    (tmp_path / reference.locator).unlink()

    with pytest.raises(AuthorityStateRecoveryError, match=r"missing|unreadable"):
        recover_authority_state(tmp_path)

    assert read_pending_bytes(tmp_path) == pending_bytes
    assert read_current_bytes(tmp_path) == predecessor


def test_pending_recovery_refuses_corrupt_selected_pending_pointer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    predecessor = _pending_root(tmp_path, monkeypatch)
    corrupt = b'{"corrupt": true}\n'
    replace_pending_bytes(tmp_path, corrupt)

    with pytest.raises(AuthorityStateRecoveryError, match="pending pointer"):
        recover_authority_state(tmp_path)

    assert read_pending_bytes(tmp_path) == corrupt
    assert read_current_bytes(tmp_path) == predecessor


def test_pending_recovery_refuses_schema_valid_conflicting_pending_pointer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    predecessor = _pending_root(tmp_path, monkeypatch)
    pending, _intent = _pending_intent(tmp_path)
    conflicting = AuthorityStatePendingPointer(
        transaction_id="conflicting-transition",
        transition_revision=pending.transition_revision,
        intent_ref=pending.intent_ref,
        selected_at=pending.selected_at,
    )
    payload = canonical_json_bytes(conflicting.as_dict())
    replace_pending_bytes(tmp_path, payload)

    with pytest.raises(AuthorityStateRecoveryError, match="does not close"):
        recover_authority_state(tmp_path)

    assert read_pending_bytes(tmp_path) == payload
    assert read_current_bytes(tmp_path) == predecessor


def test_pending_recovery_refuses_corrupt_selected_commit_object(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pending_root(
        tmp_path,
        monkeypatch,
        boundary="after_current_head_replacement",
    )
    selected_head = read_current_bytes(tmp_path)
    pending_bytes = read_pending_bytes(tmp_path)
    context = resolve_current_authority_state(
        tmp_path,
        verify_live_selection=False,
    )
    assert context is not None
    (tmp_path / context.head.commit_ref.locator).write_bytes(b'{"corrupt": true}\n')

    with pytest.raises(
        AuthorityStateRecoveryError,
        match=r"commit|sha256|content|immutable object|locator",
    ):
        recover_authority_state(tmp_path)

    assert read_pending_bytes(tmp_path) == pending_bytes
    assert read_current_bytes(tmp_path) == selected_head


def test_pending_recovery_refuses_symlinked_live_state_member(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    predecessor = _pending_root(tmp_path, monkeypatch)
    pending_bytes = read_pending_bytes(tmp_path)
    _pending, intent = _pending_intent(tmp_path)
    member = intent.state_members[0]
    assert member.after is not None
    staged = read_authority_state_bytes(
        tmp_path,
        locator=member.after.locator,
        sha256=member.after.sha256,
    )
    outside = tmp_path / "outside-live-state.json"
    outside.write_bytes(staged.payload)
    (tmp_path / member.live_locator).symlink_to(outside)

    with pytest.raises(AuthorityStateRecoveryError, match=r"unsafe|regular file"):
        recover_authority_state(tmp_path)

    assert read_pending_bytes(tmp_path) == pending_bytes
    assert read_current_bytes(tmp_path) == predecessor


def test_pending_recovery_refuses_symlinked_staged_member(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    predecessor = _pending_root(tmp_path, monkeypatch)
    pending_bytes = read_pending_bytes(tmp_path)
    _pending, intent = _pending_intent(tmp_path)
    reference = intent.staged_members[0]
    staged = read_authority_state_bytes(
        tmp_path,
        locator=reference.locator,
        sha256=reference.sha256,
    )
    path = tmp_path / reference.locator
    outside = tmp_path / "outside-staged-member.json"
    outside.write_bytes(staged.payload)
    path.unlink()
    path.symlink_to(outside)

    with pytest.raises(AuthorityStateRecoveryError, match=r"real regular|symlink"):
        recover_authority_state(tmp_path)

    assert read_pending_bytes(tmp_path) == pending_bytes
    assert read_current_bytes(tmp_path) == predecessor


def test_pending_recovery_refuses_symlinked_current_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pending_root(tmp_path, monkeypatch)
    pending_bytes = read_pending_bytes(tmp_path)
    path = tmp_path / "state/authority-state/current.json"
    outside = tmp_path / "outside-current-head.json"
    outside.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(outside)

    with pytest.raises(AuthorityStateRecoveryError, match=r"real regular|symlink"):
        recover_authority_state(tmp_path)

    assert read_pending_bytes(tmp_path) == pending_bytes
    assert path.is_symlink()


def test_pending_recovery_refuses_mid_read_state_parent_substitution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    predecessor = _pending_root(
        tmp_path,
        monkeypatch,
        boundary="after_all_state_replacements",
    )
    pending_bytes = read_pending_bytes(tmp_path)
    state_parent = tmp_path / "state/work-units"
    retired = state_parent.with_name("work-units-retired")
    original_read = state_lock_module.os.read
    rebound = False

    def read_after_parent_rebind(descriptor: int, size: int) -> bytes:
        nonlocal rebound
        descriptor_path = os.readlink(f"/proc/self/fd/{descriptor}")
        if not rebound and descriptor_path.endswith("/state/work-units/layer_1.json"):
            rebound = True
            state_parent.rename(retired)
            state_parent.mkdir()
        return original_read(descriptor, size)

    monkeypatch.setattr(state_lock_module.os, "read", read_after_parent_rebind)

    with pytest.raises(AuthorityStateRecoveryError, match="parent lineage changed"):
        recover_authority_state(tmp_path)

    assert rebound is True
    assert read_pending_bytes(tmp_path) == pending_bytes
    assert read_current_bytes(tmp_path) == predecessor


def _published_root_state(root: Path) -> None:
    candidate = _finalized_root_candidate(root)
    publish_materialization(root, candidate)
    assert read_pending_bytes(root) is None


def test_already_current_recovery_refuses_missing_live_state_member(
    tmp_path: Path,
) -> None:
    _published_root_state(tmp_path)
    current = read_current_bytes(tmp_path)
    remove_state_file(unit_state_path(tmp_path, "1"))

    with pytest.raises(AuthorityStateRecoveryError, match=r"missing=\['1'\]"):
        recover_authority_state(tmp_path)

    assert read_current_bytes(tmp_path) == current
    assert read_pending_bytes(tmp_path) is None


def test_already_current_recovery_refuses_extra_live_state_member(
    tmp_path: Path,
) -> None:
    _published_root_state(tmp_path)
    current = read_current_bytes(tmp_path)
    payload = unit_state_path(tmp_path, "1").read_bytes()
    write_state_file_bytes(unit_state_path(tmp_path, "extra"), payload)

    with pytest.raises(AuthorityStateRecoveryError, match=r"unexpected=\['extra'\]"):
        recover_authority_state(tmp_path)

    assert read_current_bytes(tmp_path) == current
    assert read_pending_bytes(tmp_path) is None


def test_independent_evaluator_refuses_uncommitted_live_state_member(
    tmp_path: Path,
) -> None:
    _published_root_state(tmp_path)
    context = resolve_current_authority_state(tmp_path)
    assert context is not None
    payload = unit_state_path(tmp_path, "1").read_bytes()
    write_state_file_bytes(unit_state_path(tmp_path, "extra"), payload)

    evaluation = evaluate_authority_state_transition(
        tmp_path,
        intent_ref=context.commit.intent_ref,
        intent=context.intent,
        commit_ref=context.head.commit_ref,
        commit=context.commit,
        evaluated_at="2026-09-01T10:01:00+00:00",
    )

    assert evaluation.result == "failed"
    assert "unexpected=['extra']" in " ".join(evaluation.findings)


@pytest.mark.parametrize("tamper", ["plan_hash", "unit_hash", "state_revision"])
def test_already_current_recovery_refuses_live_generation_tamper(
    tmp_path: Path,
    tamper: str,
) -> None:
    _published_root_state(tmp_path)
    current = read_current_bytes(tmp_path)
    state = load(tmp_path, "1")
    if tamper == "plan_hash":
        state["plan_hash"] = "f" * 64
    elif tamper == "unit_hash":
        state["units"]["lock"]["unit_hash"] = "f" * 64
    else:
        state["revision"] += 1
    write_state_file_bytes(
        unit_state_path(tmp_path, "1"),
        serialize_work_unit_state(state),
    )

    with pytest.raises(
        AuthorityStateRecoveryError,
        match=r"generation|hashes do not match|current-schema shape",
    ):
        recover_authority_state(tmp_path)

    assert read_current_bytes(tmp_path) == current
    assert read_pending_bytes(tmp_path) is None


def test_already_current_recovery_accepts_ordinary_lifecycle_progress(
    tmp_path: Path,
) -> None:
    _published_root_state(tmp_path)
    current = read_current_bytes(tmp_path)
    unit_state.transition(
        tmp_path,
        "1",
        "lock",
        "blocked",
        reason="ordinary non-spend lifecycle progress",
    )

    recovered = recover_authority_state(tmp_path)

    assert recovered.disposition == "already_current"
    assert read_current_bytes(tmp_path) == current
    assert load(tmp_path, "1")["units"]["lock"]["status"] == "blocked"


def test_detached_prepare_refuses_new_live_state_before_wal_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = _finalized_root_candidate(tmp_path)
    captured: list[object] = []
    original_commit = jit_publish.commit_prepared_authority_state_transition_locked

    class _CapturedPrepared(RuntimeError):
        pass

    def capture_prepared(_shot, prepared, **_kwargs):
        captured.append(prepared)
        raise _CapturedPrepared

    monkeypatch.setattr(
        jit_publish,
        "commit_prepared_authority_state_transition_locked",
        capture_prepared,
    )
    with pytest.raises(_CapturedPrepared):
        publish_materialization(tmp_path, candidate)
    monkeypatch.setattr(
        jit_publish,
        "commit_prepared_authority_state_transition_locked",
        original_commit,
    )
    assert len(captured) == 1
    predecessor = read_current_bytes(tmp_path)
    unit_state.initialize(
        tmp_path,
        "extra",
        (_unit("extra"),),
        plan_hash="e" * 64,
    )

    with pytest.raises(
        authority_state_transaction.AuthorityStateTransitionConflict,
        match=r"unexpected=\['extra'\]",
    ):
        authority_state_transaction.commit_prepared_authority_state_transition(
            tmp_path,
            captured[0],
        )

    assert read_pending_bytes(tmp_path) is None
    assert read_current_bytes(tmp_path) == predecessor


def test_genesis_plan_pointer_boundary_recovers_through_public_application(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _candidate(tmp_path)
    _deferred_root(tmp_path)
    layout = run_artifacts.create(tmp_path, "plan-pointer-recovery-fixture")
    _inject_crash(monkeypatch, "after_plan_pointer_replacement")

    with pytest.raises(_InjectedCrash):
        publish_current(tmp_path, layout, outcome="clean_with_deferred")

    assert read_pending_bytes(tmp_path) is not None
    monkeypatch.undo()

    recovered = recover_authority_state(tmp_path)

    assert recovered.disposition == "recovered"
    assert recovered.transition_revision == 1
    assert recovered.selection_token.plan_revision == 1
    assert recovered.selection_token.jit_revision == 0
    assert recovered.state_member_ids == ()


def test_public_recovery_cli_prints_strict_typed_current_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _finalized_root_candidate(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        ["vfx", "recover-authority-state", str(tmp_path)],
    )

    assert vfx_main() == 0

    printed = capsys.readouterr().out
    parsed = AuthorityStateRecoveryResult.parse(json.loads(printed))
    assert parsed.disposition == "already_current"
    assert parsed.transition_revision == 1


def test_public_recovery_refuses_legacy_state_without_coordinator_head(
    tmp_path: Path,
) -> None:
    unit_state.initialize(
        tmp_path,
        "legacy",
        (_unit("legacy"),),
        plan_hash="a" * 64,
    )
    before = unit_state_path(tmp_path, "legacy").read_bytes()

    with pytest.raises(AuthorityStateRecoveryError, match=r"no evaluated.*head"):
        recover_authority_state(tmp_path)

    assert read_pending_bytes(tmp_path) is None
    assert read_current_bytes(tmp_path) is None
    assert unit_state_path(tmp_path, "legacy").read_bytes() == before
