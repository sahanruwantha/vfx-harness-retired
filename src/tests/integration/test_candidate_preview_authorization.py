"""Unpublished JIT candidates carry receipt authority only through typed previews."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from tests.integration.test_authority_receipt_lineage import (
    _current_unit_completion_receipt,
    _published_finalized_root,
)
from tests.integration.test_lifecycle_fixture import _root_materialization
from tests.unit.test_plan_records import _declaring, _write
from vfx_harness.domain.authority_head_records import (
    JIT_CURRENT_PATH,
    canonical_json_bytes,
    decode_canonical_json_object,
    parse_jit_view_pointer,
)
from vfx_harness.domain.authority_preview_records import (
    AUTHORITY_PREVIEW_REFERENCE_PATH,
    AuthorityPreviewReference,
)
from vfx_harness.domain.authority_state_records import (
    AuthorityStateLayerEffect,
    AuthorityStateMemberImage,
    AuthorityStateMemberTransition,
    AuthorityStateRecordRef,
    AuthorityStateTransitionIntent,
    AuthorityStateTransitionProposal,
    LayerAuthorityBinding,
)
from vfx_harness.evaluation.plan_gate.hierarchical import (
    _check_hierarchical_plans,
)
from vfx_harness.evaluation.plan_gate.preview_authorization import (
    candidate_preview_authorization,
    candidate_preview_unit_authorization,
)
from vfx_harness.observability import run_artifacts
from vfx_harness.orchestration.authority_selection import resolve_selected_authority
from vfx_harness.orchestration.authority_state_preparation import (
    PreparedAuthorityStateTransition,
)
from vfx_harness.orchestration.authority_state_store import (
    install_authority_state_bytes,
    install_authority_state_record,
)
from vfx_harness.orchestration.jit_materialization import (
    MATERIALIZATION_SCHEMA,
    revert_materialization,
    stage_candidate_view,
)
from vfx_harness.orchestration.layer_outcome_paths import layer_outcome_path
from vfx_harness.orchestration.plan_authority import prepare_consumer_view
from vfx_harness.orchestration.unit_state import load as load_unit_state


def _dependent_materialization(root: Path, bundle_hash: str) -> Path:
    """Materialize layer 2 while leaving finalized layer 1 semantically unchanged."""

    base = json.loads((root / "layers.json").read_text(encoding="utf-8"))["layers"][1]
    layer = {key: value for key, value in base.items() if key != "jit"}
    layer["execution"] = "ready"
    layer["stages"] = [
        {
            "id": "set",
            "title": "Build the set",
            "plan": "plans/02_set/set.md",
            "depends_on": [],
            "mutates": {
                "mode": "scoped",
                "roles": ["set.mass"],
                "controls": [],
                "control_roles": {},
                "script_spans": ["build/units/02/set.py"],
            },
            "protects": {
                "selector": "all_active_upstream_interfaces",
                "resolve_to_explicit_ids_at": "freeze",
            },
            "evaluation": {
                "primary_judge": 240,
                "judge": [{"frame": 240, "ref": "refs/a.png"}],
                "temporal_evidence": "none",
                "composition_context": {
                    "frames": [240],
                    "contract_ids": ["set-vis-f239", "set-vis-f240"],
                },
                "claims": [
                    {
                        "id": "set-count-claim",
                        "proposition": "the closing set exists",
                        "axis": "set_dressing",
                        "property": "object_count",
                        "subject_roles": ["set.mass"],
                        "subject_controls": [],
                        "moments": [240],
                        "kind": "atomic",
                        "required": True,
                        "authority": "executable_required",
                        "repair_owner": "set",
                        "asserts": "scene",
                        "evidence": [
                            {"kind": "scene_contract", "id": "set-count"}
                        ],
                    }
                ],
            },
            "completion": "all_required_claims_and_protected_contracts_pass",
            "look_capabilities": [],
            "provides": ["geometry"],
        }
    ]
    payload = root / "dependent-jit.json"
    _write(
        payload,
        {
            "schema": MATERIALIZATION_SCHEMA,
            "bundle_hash": bundle_hash,
            "base_selection": resolve_selected_authority(
                root
            ).selection_token.to_dict(),
            "layer": _declaring(layer),
            "scene_contracts": [
                {
                    "id": "set-count",
                    "kind": "object_count",
                    "owner_layer": "2",
                    "fault_owner": "2",
                    "activates_at": "2",
                    "lifecycle": "layer",
                    "axis": "set_dressing",
                    "roles": ["set.mass"],
                    "frame": 240,
                    "op": "min",
                    "lo": 1,
                },
                *(
                    {
                        "id": f"set-vis-f{frame}",
                        "kind": "visible_fraction",
                        "owner_layer": "2",
                        "fault_owner": "2",
                        "activates_at": "2",
                        "lifecycle": "layer",
                        "axis": "set_dressing",
                        "roles": ["set.mass"],
                        "frame": frame,
                        "op": "min",
                        "lo": 0.25,
                    }
                    for frame in (239, 240)
                ),
            ],
            "image_contracts": [],
            "requirement_bindings": [
                {
                    "requirement_id": "R-set-dressed",
                    "contract_ids": ["set-count"],
                }
            ],
            "acceptance": [],
        },
    )
    return payload


def _forge_changed_layer_terminal_preservation(
    shot: Path,
    view: Path,
    *,
    layer_id: str,
    terminal_receipt_digest: str,
    prepared: PreparedAuthorityStateTransition,
) -> None:
    """Install a schema-valid intent that lies about deterministic invalidation."""

    intent = prepared.intent
    proposal = intent.proposal
    assert len(proposal.effects) == len(intent.state_members) == 1
    effect = proposal.effects[0]
    member = intent.state_members[0]
    assert effect.layer_id == member.layer_id == layer_id
    assert member.before is not None and member.after is not None

    forged_effect = AuthorityStateLayerEffect.mint(
        layer_id=effect.layer_id,
        effect_kind=effect.effect_kind,
        preserved_units=effect.preserved_units,
        preserved_finalization_receipt_digest=terminal_receipt_digest,
        invalidation_seed_unit_ids=effect.invalidation_seed_unit_ids,
        invalidated_unit_ids=effect.invalidated_unit_ids,
        invalidated_downstream_layer_ids=effect.invalidated_downstream_layer_ids,
        revoked_unit_attempt_claim_ids=effect.revoked_unit_attempt_claim_ids,
        revoked_layer_finalization_claim_id=effect.revoked_layer_finalization_claim_id,
    )
    forged_proposal = AuthorityStateTransitionProposal.mint(
        transaction_id=proposal.transaction_id,
        transition_revision=proposal.transition_revision,
        predecessor_head_revision=proposal.predecessor_head_revision,
        predecessor_head_ref=proposal.predecessor_head_ref,
        predecessor_head_digest=proposal.predecessor_head_digest,
        before_selection_token=proposal.before_selection_token,
        after_selection_token=proposal.after_selection_token,
        plan_pointer=proposal.plan_pointer,
        jit_pointer=proposal.jit_pointer,
        producer_ref=proposal.producer_ref,
        capsule_set_ref=proposal.capsule_set_ref,
        effects=(forged_effect,),
        proposed_at=proposal.proposed_at,
    )
    forged_binding = LayerAuthorityBinding.mint(
        transition_revision=member.after.binding.transition_revision,
        transition_proposal_digest=forged_proposal.digest,
        selection_token=member.after.binding.selection_token,
        layer_id=layer_id,
        layer_generation_digest=member.after.binding.layer_generation_digest,
        units=member.after.binding.units,
        predecessors=member.after.binding.predecessors,
        finalization_receipt_digest=terminal_receipt_digest,
    )

    predecessor_state = load_unit_state(shot, layer_id)
    forged_state = deepcopy(load_unit_state(view, layer_id))
    forged_state["layer_finalization"] = deepcopy(
        predecessor_state["layer_finalization"]
    )
    forged_state_payload = canonical_json_bytes(forged_state)
    stored_state = install_authority_state_bytes(shot, forged_state_payload)
    state_ref = AuthorityStateRecordRef.mint(
        locator=stored_state.locator,
        sha256=stored_state.sha256,
        record_schema="vfx-harness.work-unit-state/v1",
        record_digest=stored_state.sha256,
    )
    forged_after = AuthorityStateMemberImage.mint(
        layer_id=layer_id,
        locator=stored_state.locator,
        sha256=stored_state.sha256,
        state_revision=int(forged_state["revision"]),
        binding=forged_binding,
    )
    forged_member = AuthorityStateMemberTransition.mint(
        layer_id=layer_id,
        live_locator=member.live_locator,
        before=member.before,
        after=forged_after,
    )
    stored_proposal = install_authority_state_record(
        shot,
        forged_proposal.as_dict(),
    )
    proposal_ref = AuthorityStateRecordRef.mint(
        locator=stored_proposal.locator,
        sha256=stored_proposal.sha256,
        record_schema=forged_proposal.SCHEMA,
        record_digest=forged_proposal.digest,
    )
    forged_intent = AuthorityStateTransitionIntent.mint(
        proposal_ref=proposal_ref,
        proposal=forged_proposal,
        state_members=(forged_member,),
        staged_members=(*intent.staged_members, proposal_ref, state_ref),
        prepared_at=intent.prepared_at,
    )
    stored_intent = install_authority_state_record(shot, forged_intent.as_dict())
    intent_ref = AuthorityStateRecordRef.mint(
        locator=stored_intent.locator,
        sha256=stored_intent.sha256,
        record_schema=forged_intent.SCHEMA,
        record_digest=forged_intent.digest,
    )
    original_reference = AuthorityPreviewReference.from_bytes(
        (view / AUTHORITY_PREVIEW_REFERENCE_PATH).read_bytes()
    )
    forged_reference = AuthorityPreviewReference.mint(
        transition_intent_ref=intent_ref,
        predecessor_head_ref=original_reference.predecessor_head_ref,
        before_selection_token=original_reference.before_selection_token,
        after_selection_token=original_reference.after_selection_token,
        capsule_set_digest=original_reference.capsule_set_digest,
        effects_digest=forged_proposal.effects_digest,
        before_state_hashes=dict(original_reference.before_state_hashes),
        after_state_hashes={layer_id: stored_state.sha256},
    )
    (view / member.live_locator).write_bytes(forged_state_payload)
    (view / AUTHORITY_PREVIEW_REFERENCE_PATH).write_bytes(
        forged_reference.to_bytes()
    )


@pytest.mark.parametrize(
    "source_kind",
    [
        "composed_script",
        "evaluation_receipt",
        "replay_group_0",
        "sealed_outcome",
        "ledger",
    ],
)
def test_candidate_preview_refuses_stale_preserved_terminal_source_closure(
    tmp_path: Path,
    source_kind: str,
) -> None:
    layer, terminal_receipt, selected = _published_finalized_root(tmp_path)
    candidate = _dependent_materialization(
        tmp_path,
        selected.plan.bundle.content_hash,
    )
    layout = run_artifacts.create(
        tmp_path,
        f"candidate-terminal-source-{source_kind}",
    )
    view = prepare_consumer_view(layout)
    proposed = stage_candidate_view(tmp_path, candidate, view)
    assert proposed.publication.transition is not None
    effect = next(
        row
        for row in proposed.publication.transition.intent.proposal.effects
        if row.layer_id == layer.id
    )
    assert (
        effect.preserved_finalization_receipt_digest
        == terminal_receipt.receipt_digest
    )
    authorized = candidate_preview_authorization(
        view,
        shot_folder=tmp_path,
        selected_authority=selected,
    )
    assert authorized is not None
    assert (
        authorized.finalization_receipt(layer.id)
        == terminal_receipt.receipt_digest
    )

    source = {
        "composed_script": tmp_path / terminal_receipt.layer_script_path,
        "evaluation_receipt": (
            tmp_path / terminal_receipt.evaluation_receipt_locator
        ),
        "replay_group_0": (
            tmp_path
            / terminal_receipt.evaluation_receipt.replay_receipts[0].locator
        ),
        "sealed_outcome": layer_outcome_path(tmp_path, layer.id),
        "ledger": tmp_path / "shot.json",
    }[source_kind]
    original = source.read_bytes()
    source.write_bytes(
        original + b"\n# mutated after candidate preview staging\n"
        if source_kind == "composed_script"
        else b"{}\n"
    )
    try:
        with pytest.raises(ValueError):
            candidate_preview_authorization(
                view,
                shot_folder=tmp_path,
                selected_authority=selected,
            )
    finally:
        source.write_bytes(original)


def test_layer_only_preview_preserves_source_verified_unit_for_finalization(
    tmp_path: Path,
) -> None:
    layer, terminal_receipt, selected = _published_finalized_root(tmp_path)
    unit = layer.stages[0]
    completion = _current_unit_completion_receipt(tmp_path, layer.id, unit.id)

    candidate_path = _root_materialization(
        tmp_path,
        selected.plan.bundle.content_hash,
    )
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    candidate["base_selection"] = selected.selection_token.to_dict()
    candidate["acceptance"].append(
        {
            "id": "preview-layer-only-acceptance",
            "frame": 240,
            "ref": "refs/a.png",
        }
    )
    _write(candidate_path, candidate)

    layout = run_artifacts.create(tmp_path, "candidate-preview-authorization")
    view = prepare_consumer_view(layout)
    overlay = revert_materialization(tmp_path, layer.id, select=False)
    assert overlay is not None
    proposed = stage_candidate_view(
        tmp_path,
        candidate_path,
        view,
        overlay_root=overlay,
    )
    reference_path = view / AUTHORITY_PREVIEW_REFERENCE_PATH
    assert reference_path.is_file()
    assert not reference_path.is_symlink()
    assert proposed.publication.transition is not None
    effect = next(
        row
        for row in proposed.publication.transition.intent.proposal.effects
        if row.layer_id == layer.id
    )
    assert effect.preserved_finalization_receipt_digest is None
    assert [row.unit_id for row in effect.preserved_units] == [unit.id]

    preview_state = load_unit_state(view, layer.id)
    authorized = candidate_preview_unit_authorization(
        view,
        shot_folder=tmp_path,
        selected_authority=selected,
        layer_id=layer.id,
        state=preview_state,
    )
    assert authorized is not None
    assert authorized.layer_id == layer.id
    assert authorized.receipt_digest(unit.id) == completion.receipt_digest

    findings, stats = _check_hierarchical_plans(view)
    assert stats["unit_plans_required"] == 0
    assert not any(
        finding.check == "hierarchy" and finding.blocking
        for finding in findings
    )

    reference_bytes = reference_path.read_bytes()
    reference_path.unlink()
    findings, _stats = _check_hierarchical_plans(view)
    assert any(
        finding.check == "hierarchy"
        and "lacks a typed authority-state preview reference" in finding.what
        for finding in findings
    )
    reference_path.write_bytes(reference_bytes)

    script_path = tmp_path / completion.script_path
    original_script = script_path.read_bytes()
    script_path.write_bytes(original_script + b"\n# mutated after preview\n")
    try:
        findings, _stats = _check_hierarchical_plans(view)
    finally:
        script_path.write_bytes(original_script)
    authorization_failures = [
        finding
        for finding in findings
        if finding.check == "hierarchy"
        and "candidate authority preview is not authorized" in finding.what
    ]
    assert len(authorization_failures) == 1, findings
    assert "canonical evaluator input" in authorization_failures[0].what

    pointer = parse_jit_view_pointer(
        decode_canonical_json_object(
            (view / JIT_CURRENT_PATH).read_bytes(),
            "candidate preview test pointer",
        )
    )
    target = view / pointer.artifacts["layers.json"]
    original_target = target.read_bytes()
    target.write_bytes(original_target + b"\n")
    try:
        findings, _stats = _check_hierarchical_plans(view)
    finally:
        target.write_bytes(original_target)
    authorization_failures = [
        finding
        for finding in findings
        if finding.check == "hierarchy"
        and "candidate authority preview is not authorized" in finding.what
    ]
    assert len(authorization_failures) == 1, findings
    assert "JIT target differs" in authorization_failures[0].what

    producer_path = (
        tmp_path / proposed.publication.transition.intent.proposal.producer_ref.locator
    )
    producer_bytes = producer_path.read_bytes()
    producer_path.unlink()
    try:
        findings, _stats = _check_hierarchical_plans(view)
    finally:
        producer_path.write_bytes(producer_bytes)
    authorization_failures = [
        finding
        for finding in findings
        if finding.check == "hierarchy"
        and "candidate authority preview is not authorized" in finding.what
    ]
    assert len(authorization_failures) == 1, findings
    assert "missing" in authorization_failures[0].what

    _forge_changed_layer_terminal_preservation(
        tmp_path,
        view,
        layer_id=layer.id,
        terminal_receipt_digest=terminal_receipt.receipt_digest,
        prepared=proposed.publication.transition,
    )
    findings, stats = _check_hierarchical_plans(view)
    authorization_failures = [
        finding
        for finding in findings
        if finding.check == "hierarchy"
        and "candidate authority preview is not authorized" in finding.what
    ]
    assert len(authorization_failures) == 1, findings
    assert "effects differ from independent" in authorization_failures[0].what
    assert stats["layers_passed"] == 0
