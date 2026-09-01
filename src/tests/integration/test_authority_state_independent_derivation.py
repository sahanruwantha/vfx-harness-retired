from __future__ import annotations

import hashlib
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from tests.integration.test_authority_receipt_lineage import (
    _add_layer_only_acceptance_authority,
    _published_finalized_root,
    _selected_documents,
)
from vfx_harness.domain.authority_head_records import (
    OVERLAY_ARTIFACTS,
    JitViewPointer,
    canonical_json_bytes,
    canonical_view_hash,
    materialized_layers_from_document,
)
from vfx_harness.domain.authority_state_records import AuthorityStateLayerEffect
from vfx_harness.orchestration import (
    authority_state_effects,
    authority_state_preparation,
    authority_state_transaction,
)
from vfx_harness.orchestration.authority_capsule_resolution import (
    compile_proposed_authority_capsules,
)
from vfx_harness.orchestration.authority_selection import resolve_selected_authority
from vfx_harness.orchestration.authority_selection_heads import (
    read_authority_selection_heads,
)
from vfx_harness.orchestration.authority_state_effects import (
    AuthorityStateEffectsProjection,
)
from vfx_harness.orchestration.authority_state_preparation import (
    AuthorityStatePreparationError,
    prepare_authority_state_transition,
)
from vfx_harness.orchestration.authority_state_recovery import (
    AuthorityStateRecoveryError,
    recover_pending_authority_state_transition,
)
from vfx_harness.orchestration.authority_state_store import (
    read_current_bytes,
    read_pending_bytes,
)
from vfx_harness.orchestration.authority_state_transaction import (
    AuthorityStateTransitionConflict,
    commit_prepared_authority_state_transition,
)
from vfx_harness.orchestration.jit_materialization.proposal import (
    serialized_documents,
    serialized_hashes,
)
from vfx_harness.orchestration.jit_materialization.view_store import (
    durably_install_or_flush_view_directory,
)


class _InjectedCrash(RuntimeError):
    pass


def _forge_projection(
    projection: AuthorityStateEffectsProjection,
    *,
    forgery: str,
    terminal_receipt_digest: str,
) -> AuthorityStateEffectsProjection:
    rows = []
    forged = False
    for row in projection.layers:
        if row.before_state is None or row.after_state is None:
            rows.append(row)
            continue
        if forgery == "effect":
            effect = row.effect
            forged_effect = AuthorityStateLayerEffect.mint(
                layer_id=effect.layer_id,
                effect_kind=effect.effect_kind,
                preserved_units=effect.preserved_units,
                preserved_finalization_receipt_digest=terminal_receipt_digest,
                invalidation_seed_unit_ids=effect.invalidation_seed_unit_ids,
                invalidated_unit_ids=effect.invalidated_unit_ids,
                invalidated_downstream_layer_ids=(
                    effect.invalidated_downstream_layer_ids
                ),
                revoked_unit_attempt_claim_ids=effect.revoked_unit_attempt_claim_ids,
                revoked_layer_finalization_claim_id=(
                    effect.revoked_layer_finalization_claim_id
                ),
            )
            after_state = deepcopy(row.after_state)
            after_state["layer_finalization"] = deepcopy(
                row.before_state["layer_finalization"]
            )
            rows.append(
                replace(
                    row,
                    after_state=after_state,
                    after_finalization_receipt_digest=terminal_receipt_digest,
                    effect=forged_effect,
                )
            )
        elif forgery == "after_state":
            after_state = deepcopy(row.after_state)
            after_state["updated"] = "2099-01-01T00:00:00Z"
            rows.append(replace(row, after_state=after_state))
        elif forgery == "after_binding":
            rows.append(replace(row, after_layer_generation_digest="f" * 64))
        else:  # pragma: no cover - test helper contract
            raise AssertionError(forgery)
        forged = True
    assert forged
    return AuthorityStateEffectsProjection(tuple(rows))


def _prepare_forged_successor(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    forgery: str,
):
    _layer, terminal_receipt, _execution_authority = _published_finalized_root(root)
    selected = resolve_selected_authority(root)
    assert selected.plan is not None
    heads = read_authority_selection_heads(root)
    assert heads.jit is not None

    documents = _selected_documents(root)
    _add_layer_only_acceptance_authority(
        documents,
        identifier=f"forged-{forgery}-authority",
    )
    payloads = serialized_documents(documents)
    hashes = serialized_hashes(payloads)
    view_hash = canonical_view_hash(documents)
    view_root = root / "state" / "jit-layers" / "views" / view_hash
    durably_install_or_flush_view_directory(root, view_root, payloads)
    successor_pointer = JitViewPointer(
        revision=heads.jit.revision + 1,
        plan_revision=heads.token.plan_revision,
        bundle_hash=selected.plan.bundle.content_hash,
        view_hash=view_hash,
        materialized_layers=materialized_layers_from_document(
            documents["layers.json"]
        ),
        artifacts={
            name: (view_root / name).relative_to(root).as_posix()
            for name in OVERLAY_ARTIFACTS
        },
        hashes=hashes,
    )
    successor_bytes = canonical_json_bytes(successor_pointer.as_dict())
    captured = compile_proposed_authority_capsules(root, selected, documents)
    producer = f"forged {forgery} transition\n".encode()
    real_compile = authority_state_effects.compile_authority_state_effects

    def forged_compile(**kwargs):
        return _forge_projection(
            real_compile(**kwargs),
            forgery=forgery,
            terminal_receipt_digest=terminal_receipt.receipt_digest,
        )

    with monkeypatch.context() as preparation_patch:
        preparation_patch.setattr(
            authority_state_preparation,
            "compile_authority_state_effects",
            forged_compile,
        )
        return prepare_authority_state_transition(
            root,
            expected_base_selection=selected.selection_token,
            after_capsules=captured.capsule_set,
            after_plan_pointer_bytes=heads.plan_pointer_bytes,
            after_plan_revision=heads.token.plan_revision,
            after_jit_pointer_bytes=successor_bytes,
            after_jit_revision=successor_pointer.revision,
            producer_payload=producer,
            producer_schema="vfx-harness.test-forged-transition/v1",
            producer_digest=hashlib.sha256(producer).hexdigest(),
        )


def test_preparation_refuses_forged_terminal_preservation_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(
        AuthorityStatePreparationError,
        match="cannot preserve a stale terminal layer publication",
    ):
        _prepare_forged_successor(
            tmp_path,
            monkeypatch,
            forgery="effect",
        )


@pytest.mark.parametrize("forgery", ["after_state"])
def test_commit_refuses_schema_valid_forged_transition_derivation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    forgery: str,
) -> None:
    prepared = _prepare_forged_successor(
        tmp_path,
        monkeypatch,
        forgery=forgery,
    )
    predecessor_head = read_current_bytes(tmp_path)

    with pytest.raises(
        AuthorityStateTransitionConflict,
        match="independent authority-state evaluation failed",
    ):
        commit_prepared_authority_state_transition(tmp_path, prepared)

    assert read_pending_bytes(tmp_path) is not None
    assert read_current_bytes(tmp_path) == predecessor_head


@pytest.mark.parametrize("forgery", ["after_state", "after_binding"])
def test_recovery_refuses_schema_valid_forged_transition_derivation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    forgery: str,
) -> None:
    prepared = _prepare_forged_successor(
        tmp_path,
        monkeypatch,
        forgery=forgery,
    )
    predecessor_head = read_current_bytes(tmp_path)

    with monkeypatch.context() as crash_patch:
        crash_patch.setattr(
            authority_state_transaction,
            "evaluate_authority_state_transition",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(_InjectedCrash()),
        )
        with pytest.raises(_InjectedCrash):
            commit_prepared_authority_state_transition(tmp_path, prepared)

    with pytest.raises(
        AuthorityStateRecoveryError,
        match="failed independent evaluation",
    ):
        recover_pending_authority_state_transition(tmp_path)

    assert read_pending_bytes(tmp_path) is not None
    assert read_current_bytes(tmp_path) == predecessor_head
