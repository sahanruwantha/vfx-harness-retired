from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from tests.integration.test_lifecycle_fixture import (
    _approve_hold_decision,
    _deferred_root,
    _root_materialization,
)
from tests.unit.test_layer_finalization_state import _complete_passed_layer
from tests.unit.test_layer_publication import _write_projections
from tests.unit.test_plan_records import _candidate
from tests.unit_attempt_fixtures import pass_unit
from vfx_harness.agents.builder.layer_finalization_guard import (
    LayerFinalizationReceiptGuard,
)
from vfx_harness.domain.authority_head_records import (
    OVERLAY_ARTIFACTS,
    JitViewPointer,
    canonical_json_bytes,
    canonical_view_hash,
    materialized_layers_from_document,
)
from vfx_harness.domain.unit_completion_receipts import UnitCompletionReceipt
from vfx_harness.observability import run_artifacts
from vfx_harness.orchestration import authority_state_transaction, revalidation
from vfx_harness.orchestration.authority_capsule_resolution import (
    capture_selected_authority_capsules,
    compile_proposed_authority_capsules,
    selected_layer_capsule_digest,
)
from vfx_harness.orchestration.authority_receipt_lineage import (
    AuthorityReceiptLineageError,
    require_preserved_layer_finalization_authorization,
    require_preserved_unit_completion_authorization,
)
from vfx_harness.orchestration.authority_selection import resolve_selected_authority
from vfx_harness.orchestration.authority_selection_heads import (
    read_authority_selection_heads,
)
from vfx_harness.orchestration.authority_state_context import (
    resolve_current_authority_state,
)
from vfx_harness.orchestration.authority_state_preparation import (
    AuthorityStatePreparationError,
    prepare_authority_state_transition,
)
from vfx_harness.orchestration.authority_state_recovery import (
    AuthorityStateRecoveryError,
    recover_pending_authority_state_transition,
)
from vfx_harness.orchestration.authority_state_store import read_pending_bytes
from vfx_harness.orchestration.authority_state_transaction import (
    AuthorityStateTransitionConflict,
    commit_prepared_authority_state_transition,
)
from vfx_harness.orchestration.jit_materialization import (
    finalize_materialization_candidate,
    publish_materialization,
)
from vfx_harness.orchestration.jit_materialization.proposal import (
    serialized_documents,
    serialized_hashes,
)
from vfx_harness.orchestration.jit_materialization.view_store import (
    durably_install_or_flush_view_directory,
)
from vfx_harness.orchestration.layer_outcome_paths import layer_outcome_path
from vfx_harness.orchestration.layer_publication import (
    require_current_layer_publication,
)
from vfx_harness.orchestration.ledger import load_layers_from_path
from vfx_harness.orchestration.plan_authority import (
    prepare_consumer_view,
    publish_current,
    selected_artifact_path,
)
from vfx_harness.orchestration.unit_completion_state import (
    completed_unit_attempt_guard,
)
from vfx_harness.orchestration.unit_state import load as load_unit_state


def _published_finalized_root(root: Path):
    _candidate(root)
    _deferred_root(root)
    finalization_reference = root / "refs/f040.png"
    finalization_reference.parent.mkdir(parents=True, exist_ok=True)
    finalization_reference.write_bytes(b"fixture reference")
    layout = run_artifacts.create(root, "receipt-lineage")
    bundle = publish_current(root, layout, outcome="clean_with_deferred")
    _approve_hold_decision(root, bundle.content_hash)
    candidate = _root_materialization(root, bundle.content_hash)
    result = finalize_materialization_candidate(
        root,
        candidate,
        prepare_consumer_view(layout),
    )
    assert result.clean
    publish_materialization(root, candidate)

    selected = resolve_selected_authority(root)
    layer = load_layers_from_path(selected_artifact_path(root, "layers.json"))["1"]
    plan_hash = selected_layer_capsule_digest(root, layer.id, selected)
    pass_unit(
        root,
        layer.id,
        layer.stages[0],
        layer.stages,
        plan_hash=plan_hash,
        selection_token=selected.selection_token,
    )
    _layer, _guard, _replay, _stored, receipt = _complete_passed_layer(
        root,
        layer,
        plan_hash=plan_hash,
        selection_token=selected.selection_token,
        revalidation_manifest_factory=lambda: revalidation.input_manifest(
            root,
            layer,
            blender_version="fixture",
            selected_authority=selected,
        ),
    )
    _write_projections(root, receipt)
    require_current_layer_publication(root, layer, selected)
    return layer, receipt, selected


def _prepare_same_capsule_successor(root: Path):
    selected = resolve_selected_authority(root)
    heads = read_authority_selection_heads(root)
    assert heads.jit is not None
    successor_pointer = replace(heads.jit, revision=heads.jit.revision + 1)
    successor_bytes = canonical_json_bytes(successor_pointer.as_dict())
    captured = capture_selected_authority_capsules(root, selected)
    producer = (
        f"preservation transition {successor_pointer.revision}\n"
    ).encode()
    return prepare_authority_state_transition(
        root,
        expected_base_selection=selected.selection_token,
        after_capsules=captured.capsule_set,
        after_plan_pointer_bytes=heads.plan_pointer_bytes,
        after_plan_revision=heads.token.plan_revision,
        after_jit_pointer_bytes=successor_bytes,
        after_jit_revision=successor_pointer.revision,
        producer_payload=producer,
        producer_schema="vfx-harness.test-preservation-transition/v1",
        producer_digest=hashlib.sha256(producer).hexdigest(),
    )


def _commit_same_capsule_successor(root: Path) -> None:
    prepared = _prepare_same_capsule_successor(root)
    commit_prepared_authority_state_transition(root, prepared)


def _selected_documents(root: Path) -> dict[str, object]:
    selected = resolve_selected_authority(root)
    return {
        name: json.loads(selected.artifact_paths[name].read_text(encoding="utf-8"))
        for name in OVERLAY_ARTIFACTS
    }


def _current_unit_completion_receipt(
    root: Path,
    layer_id: str,
    unit_id: str,
) -> UnitCompletionReceipt:
    state = load_unit_state(root, layer_id)
    return UnitCompletionReceipt.parse(
        state["units"][unit_id]["completion_receipt"],
        f"fixture completion receipt {layer_id}.{unit_id}",
    )


def _add_layer_only_acceptance_authority(
    documents: dict[str, object],
    *,
    identifier: str = "fixture-layer-only-acceptance",
) -> None:
    acceptance = documents["acceptance.json"]
    assert isinstance(acceptance, list)
    acceptance.append(
        {
            "id": identifier,
            "frame": 1,
            "ref": f"refs/{identifier}.png",
        }
    )


def _commit_document_successor(
    root: Path,
    documents: dict[str, object],
) -> None:
    selected = resolve_selected_authority(root)
    assert selected.plan is not None
    heads = read_authority_selection_heads(root)
    assert heads.jit is not None

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
    producer = f"document transition {successor_pointer.revision}\n".encode()
    prepared = prepare_authority_state_transition(
        root,
        expected_base_selection=selected.selection_token,
        after_capsules=captured.capsule_set,
        after_plan_pointer_bytes=heads.plan_pointer_bytes,
        after_plan_revision=heads.token.plan_revision,
        after_jit_pointer_bytes=successor_bytes,
        after_jit_revision=successor_pointer.revision,
        producer_payload=producer,
        producer_schema="vfx-harness.test-document-transition/v1",
        producer_digest=hashlib.sha256(producer).hexdigest(),
    )
    commit_prepared_authority_state_transition(root, prepared)


def test_layer_receipt_survives_only_through_contiguous_preservation_lineage(
    tmp_path: Path,
) -> None:
    layer, receipt, execution_authority = _published_finalized_root(tmp_path)

    _commit_same_capsule_successor(tmp_path)
    _commit_same_capsule_successor(tmp_path)

    selected = resolve_selected_authority(tmp_path)
    assert selected.selection_token != execution_authority.selection_token
    authorization = require_preserved_layer_finalization_authorization(
        tmp_path,
        receipt,
        selected,
    )
    assert len(authorization.traversed_head_digests) == 2
    assert authorization.receipt_digest == receipt.receipt_digest
    assert LayerFinalizationReceiptGuard.bind(
        tmp_path,
        receipt,
        layer.stages,
        selected,
    ).check("verify preserved terminal receipt") == receipt
    assert require_current_layer_publication(
        tmp_path,
        layer,
        selected,
    ).receipt == receipt


def test_changed_transition_prevents_a_like_authority_from_resurrecting_receipt(
    tmp_path: Path,
) -> None:
    _layer, receipt, execution_authority = _published_finalized_root(tmp_path)
    generation_a = _selected_documents(tmp_path)
    generation_b = deepcopy(generation_a)
    generation_b["layers.json"]["layers"][0]["stages"][0]["title"] = (
        "Changed lock"
    )

    _commit_document_successor(tmp_path, generation_b)
    changed_context = resolve_current_authority_state(tmp_path)
    assert changed_context is not None
    changed_effect = next(
        effect
        for effect in changed_context.proposal.effects
        if effect.layer_id == receipt.claim.layer_id
    )
    assert changed_effect.effect_kind == "changed"
    assert changed_effect.preserved_finalization_receipt_digest is None
    _commit_document_successor(tmp_path, generation_a)

    selected = resolve_selected_authority(tmp_path)
    assert selected.selection_token != execution_authority.selection_token
    assert (
        selected_layer_capsule_digest(tmp_path, receipt.claim.layer_id, selected)
        == receipt.claim.plan_hash
    )
    with pytest.raises(
        AuthorityReceiptLineageError,
        match="did not preserve the exact terminal receipt and unit closure",
    ):
        require_preserved_layer_finalization_authorization(
            tmp_path,
            receipt,
            selected,
        )


def test_unit_receipt_survives_layer_change_when_exact_unit_capsule_is_preserved(
    tmp_path: Path,
) -> None:
    layer, terminal_receipt, execution_authority = _published_finalized_root(
        tmp_path
    )
    unit = layer.stages[0]
    completion_receipt = _current_unit_completion_receipt(
        tmp_path,
        layer.id,
        unit.id,
    )
    generation_a = capture_selected_authority_capsules(
        tmp_path,
        execution_authority,
    ).capsule_set

    generation_b_documents = _selected_documents(tmp_path)
    _add_layer_only_acceptance_authority(generation_b_documents)
    _commit_document_successor(tmp_path, generation_b_documents)

    selected = resolve_selected_authority(tmp_path)
    generation_b = capture_selected_authority_capsules(
        tmp_path,
        selected,
    ).capsule_set
    assert (
        generation_b.layer(layer.id).capsule_digest
        != generation_a.layer(layer.id).capsule_digest
    )
    assert (
        generation_b.unit(layer.id, unit.id).capsule_digest
        == generation_a.unit(layer.id, unit.id).capsule_digest
    )

    context = resolve_current_authority_state(tmp_path)
    assert context is not None
    effect = next(
        row for row in context.proposal.effects if row.layer_id == layer.id
    )
    assert effect.effect_kind == "changed"
    assert effect.preserved_finalization_receipt_digest is None
    assert effect.preserved_units[0].unit_id == unit.id
    assert (
        effect.preserved_units[0].completion_receipt_digest
        == completion_receipt.receipt_digest
    )

    state = load_unit_state(tmp_path, layer.id)
    assert state["units"][unit.id]["status"] == "passed"
    assert (
        state["units"][unit.id]["completion_receipt"]
        == completion_receipt.as_dict()
    )
    assert state["layer_finalization"].get("terminal_receipt") is None
    assert state["layer_finalization"]["receipt_history"][-1]["receipt"] == (
        terminal_receipt.as_dict()
    )

    authorization = require_preserved_unit_completion_authorization(
        tmp_path,
        completion_receipt,
        selected,
    )
    assert authorization.layer_id == layer.id
    assert authorization.unit_id == unit.id
    assert authorization.receipt_digest == completion_receipt.receipt_digest
    assert authorization.unit_generation_digest == (
        generation_b.unit(layer.id, unit.id).capsule_digest
    )
    assert len(authorization.traversed_head_digests) == 1
    with completed_unit_attempt_guard(
        tmp_path,
        layer.id,
        unit.id,
        layer.stages,
        completion_receipt,
        expected_plan_hash=generation_b.layer(layer.id).capsule_digest,
        selection_token=selected.selection_token,
        lineage_authorization=authorization,
    ) as guarded:
        assert guarded == completion_receipt

    generation_c_documents = _selected_documents(tmp_path)
    _add_layer_only_acceptance_authority(
        generation_c_documents,
        identifier="fixture-second-layer-only-acceptance",
    )
    _commit_document_successor(tmp_path, generation_c_documents)
    selected_c = resolve_selected_authority(tmp_path)
    generation_c = capture_selected_authority_capsules(
        tmp_path,
        selected_c,
    ).capsule_set
    assert (
        generation_c.unit(layer.id, unit.id).capsule_digest
        == generation_b.unit(layer.id, unit.id).capsule_digest
    )
    authorization_c = require_preserved_unit_completion_authorization(
        tmp_path,
        completion_receipt,
        selected_c,
    )
    assert len(authorization_c.traversed_head_digests) == 2


def test_unit_receipt_cannot_resurrect_across_unit_changing_a_b_a_lineage(
    tmp_path: Path,
) -> None:
    layer, _terminal_receipt, execution_authority = _published_finalized_root(
        tmp_path
    )
    unit = layer.stages[0]
    completion_receipt = _current_unit_completion_receipt(
        tmp_path,
        layer.id,
        unit.id,
    )
    generation_a_documents = _selected_documents(tmp_path)
    generation_a = capture_selected_authority_capsules(
        tmp_path,
        execution_authority,
    ).capsule_set
    unit_capsule_a = generation_a.unit(layer.id, unit.id).capsule_digest

    generation_b_documents = deepcopy(generation_a_documents)
    generation_b_documents["layers.json"]["layers"][0]["stages"][0][
        "title"
    ] = "Changed lock"
    _commit_document_successor(tmp_path, generation_b_documents)
    changed_context = resolve_current_authority_state(tmp_path)
    assert changed_context is not None
    changed_effect = next(
        row for row in changed_context.proposal.effects if row.layer_id == layer.id
    )
    assert changed_effect.effect_kind == "changed"
    assert changed_effect.preserved_units == ()

    _commit_document_successor(tmp_path, generation_a_documents)
    selected = resolve_selected_authority(tmp_path)
    generation_a_again = capture_selected_authority_capsules(
        tmp_path,
        selected,
    ).capsule_set
    assert (
        generation_a_again.unit(layer.id, unit.id).capsule_digest
        == unit_capsule_a
    )
    assert selected.selection_token != execution_authority.selection_token

    with pytest.raises(AuthorityReceiptLineageError):
        require_preserved_unit_completion_authorization(
            tmp_path,
            completion_receipt,
            selected,
        )


@pytest.mark.parametrize("source_kind", ["script", "evaluator"])
def test_transition_preparation_source_verifies_preserved_unit_completion(
    tmp_path: Path,
    source_kind: str,
) -> None:
    layer, _terminal_receipt, _execution_authority = _published_finalized_root(
        tmp_path
    )
    completion = _current_unit_completion_receipt(
        tmp_path,
        layer.id,
        layer.stages[0].id,
    )
    source = (
        tmp_path / completion.script_path
        if source_kind == "script"
        else tmp_path / completion.evaluation_receipt_locator
    )
    source.write_text("{}\n", encoding="utf-8")
    before = read_authority_selection_heads(tmp_path).token

    with pytest.raises(
        AuthorityStatePreparationError,
        match="cannot preserve stale completed work",
    ):
        _prepare_same_capsule_successor(tmp_path)

    assert read_authority_selection_heads(tmp_path).token == before
    assert read_pending_bytes(tmp_path) is None


def test_transition_commit_rechecks_prepared_preserved_unit_sources(
    tmp_path: Path,
) -> None:
    layer, _terminal_receipt, _execution_authority = _published_finalized_root(
        tmp_path
    )
    completion = _current_unit_completion_receipt(
        tmp_path,
        layer.id,
        layer.stages[0].id,
    )
    prepared = _prepare_same_capsule_successor(tmp_path)
    before = read_authority_selection_heads(tmp_path).token
    (tmp_path / completion.script_path).write_text(
        "# changed after transition preparation\n",
        encoding="utf-8",
    )

    with pytest.raises(
        AuthorityStateTransitionConflict,
        match="source closure changed before authority-state commit",
    ):
        commit_prepared_authority_state_transition(tmp_path, prepared)

    assert read_authority_selection_heads(tmp_path).token == before
    assert read_pending_bytes(tmp_path) is None


def test_transition_preparation_source_verifies_preserved_layer_finalization(
    tmp_path: Path,
) -> None:
    _layer, receipt, _execution_authority = _published_finalized_root(tmp_path)
    source = tmp_path / receipt.layer_script_path
    source.write_text(
        "# corrupt composed layer source before transition preparation\n",
        encoding="utf-8",
    )
    before = read_authority_selection_heads(tmp_path).token

    with pytest.raises(
        AuthorityStatePreparationError,
        match="cannot preserve a stale terminal layer publication",
    ):
        _prepare_same_capsule_successor(tmp_path)

    assert read_authority_selection_heads(tmp_path).token == before
    assert read_pending_bytes(tmp_path) is None


def test_transition_commit_rechecks_prepared_terminal_publication_sources(
    tmp_path: Path,
) -> None:
    layer, _receipt, _execution_authority = _published_finalized_root(tmp_path)
    prepared = _prepare_same_capsule_successor(tmp_path)
    before = read_authority_selection_heads(tmp_path).token
    layer_outcome_path(tmp_path, layer.id).write_text(
        "{}\n",
        encoding="utf-8",
    )

    with pytest.raises(
        AuthorityStateTransitionConflict,
        match="terminal publication source closure changed before authority-state commit",
    ):
        commit_prepared_authority_state_transition(tmp_path, prepared)

    assert read_authority_selection_heads(tmp_path).token == before
    assert read_pending_bytes(tmp_path) is None


def test_failed_terminal_preservation_edge_cannot_be_erased_by_source_restore(
    tmp_path: Path,
) -> None:
    layer, receipt, _execution_authority = _published_finalized_root(tmp_path)
    source = tmp_path / receipt.layer_script_path
    original = source.read_bytes()
    predecessor = resolve_current_authority_state(tmp_path)
    assert predecessor is not None
    source.write_text(
        "# corrupt composed layer source at the proposed preservation edge\n",
        encoding="utf-8",
    )

    with pytest.raises(AuthorityStatePreparationError):
        _prepare_same_capsule_successor(tmp_path)

    assert resolve_current_authority_state(tmp_path) == predecessor
    assert read_pending_bytes(tmp_path) is None
    source.write_bytes(original)
    _commit_same_capsule_successor(tmp_path)

    selected = resolve_selected_authority(tmp_path)
    successor = resolve_current_authority_state(tmp_path)
    assert successor is not None
    assert successor.head.revision == predecessor.head.revision + 1
    authorization = require_preserved_layer_finalization_authorization(
        tmp_path,
        receipt,
        selected,
    )
    assert len(authorization.traversed_head_digests) == 1
    assert require_current_layer_publication(tmp_path, layer, selected).receipt == receipt


def test_invalidated_completion_does_not_require_historical_source_closure(
    tmp_path: Path,
) -> None:
    layer, _terminal_receipt, _execution_authority = _published_finalized_root(
        tmp_path
    )
    completion = _current_unit_completion_receipt(
        tmp_path,
        layer.id,
        layer.stages[0].id,
    )
    (tmp_path / completion.script_path).write_text(
        "# stale source belongs only to invalidated work\n",
        encoding="utf-8",
    )
    changed = _selected_documents(tmp_path)
    changed["layers.json"]["layers"][0]["stages"][0]["title"] = (
        "Changed unit authority"
    )

    _commit_document_successor(tmp_path, changed)

    context = resolve_current_authority_state(tmp_path)
    assert context is not None
    effect = next(row for row in context.proposal.effects if row.layer_id == layer.id)
    assert effect.preserved_units == ()
    assert load_unit_state(tmp_path, layer.id)["units"][layer.stages[0].id][
        "status"
    ] == "pending"


def test_independent_transition_evaluator_refuses_post_wal_source_corruption(
    tmp_path: Path,
    monkeypatch,
) -> None:
    layer, _terminal_receipt, _execution_authority = _published_finalized_root(
        tmp_path
    )
    completion = _current_unit_completion_receipt(
        tmp_path,
        layer.id,
        layer.stages[0].id,
    )
    prepared = _prepare_same_capsule_successor(tmp_path)
    original = authority_state_transaction.require_transition_preserved_unit_sources

    def corrupt_after_precommit_check(*args, **kwargs) -> None:
        original(*args, **kwargs)
        (tmp_path / completion.script_path).write_text(
            "# injected corruption after the pre-WAL source check\n",
            encoding="utf-8",
        )

    monkeypatch.setattr(
        authority_state_transaction,
        "require_transition_preserved_unit_sources",
        corrupt_after_precommit_check,
    )

    with pytest.raises(
        AuthorityStateTransitionConflict,
        match="independent authority-state evaluation failed",
    ):
        commit_prepared_authority_state_transition(tmp_path, prepared)

    assert read_pending_bytes(tmp_path) is not None
    with pytest.raises(
        AuthorityStateRecoveryError,
        match="failed independent evaluation",
    ):
        recover_pending_authority_state_transition(tmp_path)
    assert read_pending_bytes(tmp_path) is not None


def test_independent_transition_evaluator_refuses_post_wal_terminal_corruption(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _layer, receipt, _execution_authority = _published_finalized_root(tmp_path)
    prepared = _prepare_same_capsule_successor(tmp_path)
    original = (
        authority_state_transaction.require_transition_preserved_finalization_sources
    )

    def corrupt_after_precommit_check(*args, **kwargs) -> None:
        original(*args, **kwargs)
        (tmp_path / receipt.layer_script_path).write_text(
            "# injected terminal corruption after the pre-WAL source check\n",
            encoding="utf-8",
        )

    monkeypatch.setattr(
        authority_state_transaction,
        "require_transition_preserved_finalization_sources",
        corrupt_after_precommit_check,
    )

    with pytest.raises(
        AuthorityStateTransitionConflict,
        match="independent authority-state evaluation failed",
    ):
        commit_prepared_authority_state_transition(tmp_path, prepared)

    assert read_pending_bytes(tmp_path) is not None
    with pytest.raises(
        AuthorityStateRecoveryError,
        match="failed independent evaluation",
    ):
        recover_pending_authority_state_transition(tmp_path)
    assert read_pending_bytes(tmp_path) is not None
