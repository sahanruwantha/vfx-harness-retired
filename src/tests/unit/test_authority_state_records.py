from __future__ import annotations

import copy
import hashlib
from dataclasses import replace

import pytest

from vfx_harness.domain.authority_head_records import AuthoritySelectionTokenProjection
from vfx_harness.domain.authority_state_records import (
    AUTHORITY_CAPSULE_SET_SCHEMA,
    AUTHORITY_STATE_HEAD_SCHEMA,
    AUTHORITY_STATE_TRANSITION_COMMIT_SCHEMA,
    AUTHORITY_STATE_TRANSITION_EVALUATION_SCHEMA,
    AUTHORITY_STATE_TRANSITION_INTENT_SCHEMA,
    AUTHORITY_STATE_TRANSITION_PROPOSAL_SCHEMA,
    AuthorityPointerImage,
    AuthorityPointerTransition,
    AuthorityStateCoordinatorHead,
    AuthorityStateLayerEffect,
    AuthorityStateMemberImage,
    AuthorityStateMemberTransition,
    AuthorityStatePendingPointer,
    AuthorityStateRecordError,
    AuthorityStateRecordRef,
    AuthorityStateTransitionCommit,
    AuthorityStateTransitionEvaluation,
    AuthorityStateTransitionIntent,
    AuthorityStateTransitionProposal,
    AuthorityUnitBinding,
    LayerAuthorityBinding,
    PredecessorLayerBinding,
    authority_selection_token_dict,
)


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _ref(
    label: str,
    schema: str,
    record_digest: str | None = None,
    *,
    locator: str | None = None,
) -> AuthorityStateRecordRef:
    return AuthorityStateRecordRef.mint(
        locator=locator or f"state/authority-state/members/{label}.json",
        sha256=_digest(f"{label}-bytes"),
        record_schema=schema,
        record_digest=record_digest or _digest(f"{label}-record"),
    )


def _token(plan: int, jit: int, suffix: str) -> AuthoritySelectionTokenProjection:
    return AuthoritySelectionTokenProjection(
        plan_revision=plan,
        plan_pointer_sha256=None if plan == 0 else _digest(f"plan-{suffix}"),
        jit_revision=jit,
        jit_pointer_sha256=None if jit == 0 else _digest(f"jit-{suffix}"),
    )


def _record_ref(record: object, label: str) -> AuthorityStateRecordRef:
    schema = record.SCHEMA  # type: ignore[attr-defined]
    digest = record.digest  # type: ignore[attr-defined]
    return _ref(label, schema, digest)


def _chain() -> dict[str, object]:
    before_token = _token(1, 1, "before")
    after_token = AuthoritySelectionTokenProjection(
        plan_revision=1,
        plan_pointer_sha256=before_token.plan_pointer_sha256,
        jit_revision=2,
        jit_pointer_sha256=_digest("jit-after"),
    )
    plan_image = AuthorityPointerImage.mint(
        revision=1,
        locator="plans/pointers/plan-1.json",
        sha256=before_token.plan_pointer_sha256 or "",
    )
    jit_before = AuthorityPointerImage.mint(
        revision=1,
        locator="state/jit-layers/pointers/jit-1.json",
        sha256=before_token.jit_pointer_sha256 or "",
    )
    jit_after = AuthorityPointerImage.mint(
        revision=2,
        locator="state/authority-state/members/jit-2.json",
        sha256=after_token.jit_pointer_sha256 or "",
    )
    plan_transition = AuthorityPointerTransition.mint(
        pointer_kind="plan",
        live_locator="plans/current.json",
        before=plan_image,
        after=plan_image,
    )
    jit_transition = AuthorityPointerTransition.mint(
        pointer_kind="jit",
        live_locator="state/jit-layers/current.json",
        before=jit_before,
        after=jit_after,
    )
    completed_unit = AuthorityUnitBinding.mint(
        unit_id="camera-unit",
        unit_generation_digest=_digest("camera-unit"),
        completion_receipt_digest=_digest("camera-completion"),
    )
    effect = AuthorityStateLayerEffect.mint(
        layer_id="camera",
        effect_kind="unchanged",
        preserved_units=[completed_unit],
        preserved_finalization_receipt_digest=_digest("camera-finalization"),
        revoked_unit_attempt_claim_ids=["claim-2", "claim-1"],
        revoked_layer_finalization_claim_id="finalization-claim-2",
    )
    proposal = AuthorityStateTransitionProposal.mint(
        transaction_id="transition-2",
        transition_revision=2,
        predecessor_head_revision=1,
        predecessor_head_ref=_ref(
            "head-1",
            AUTHORITY_STATE_HEAD_SCHEMA,
            _digest("head-1"),
        ),
        predecessor_head_digest=_digest("head-1"),
        before_selection_token=before_token,
        after_selection_token=after_token,
        plan_pointer=plan_transition,
        jit_pointer=jit_transition,
        producer_ref=_ref("materialization-finalization", "vfx-harness.materialization-finalization/v4"),
        capsule_set_ref=_ref("capsules", AUTHORITY_CAPSULE_SET_SCHEMA),
        effects=[effect],
        proposed_at="2026-09-01T01:00:00Z",
    )
    before_binding = LayerAuthorityBinding.mint(
        transition_revision=1,
        transition_proposal_digest=_digest("proposal-1"),
        selection_token=before_token,
        layer_id="camera",
        layer_generation_digest=_digest("camera-generation"),
        units=[completed_unit],
        predecessors=[],
        finalization_receipt_digest=_digest("camera-finalization"),
    )
    after_binding = LayerAuthorityBinding.mint(
        transition_revision=2,
        transition_proposal_digest=proposal.digest,
        selection_token=after_token,
        layer_id="camera",
        layer_generation_digest=_digest("camera-generation"),
        units=[completed_unit],
        predecessors=[],
        finalization_receipt_digest=_digest("camera-finalization"),
    )
    before_state = AuthorityStateMemberImage.mint(
        layer_id="camera",
        locator="state/work-units/camera.json",
        sha256=_digest("camera-state-before"),
        state_revision=7,
        binding=before_binding,
    )
    after_state = AuthorityStateMemberImage.mint(
        layer_id="camera",
        locator="state/authority-state/members/camera-state-8.json",
        sha256=_digest("camera-state-after"),
        state_revision=8,
        binding=after_binding,
    )
    member = AuthorityStateMemberTransition.mint(
        layer_id="camera",
        live_locator="state/work-units/camera.json",
        before=before_state,
        after=after_state,
    )
    proposal_ref = _record_ref(proposal, "proposal-2")
    staged_after = AuthorityStateRecordRef.mint(
        locator=after_state.locator,
        sha256=after_state.sha256,
        record_schema="vfx-harness.work-unit-state/v2",
        record_digest=_digest("camera-state-record-after"),
    )
    staged_jit = AuthorityStateRecordRef.mint(
        locator=jit_after.locator,
        sha256=jit_after.sha256,
        record_schema="vfx-harness.jit-layer-view/v2",
        record_digest=_digest("jit-pointer-record-after"),
    )
    intent = AuthorityStateTransitionIntent.mint(
        proposal_ref=proposal_ref,
        proposal=proposal,
        state_members=[member],
        staged_members=[staged_after, staged_jit, proposal_ref],
        prepared_at="2026-09-01T01:01:00Z",
    )
    intent_ref = _record_ref(intent, "intent-2")
    pending = AuthorityStatePendingPointer.mint(
        intent_ref=intent_ref,
        intent=intent,
        selected_at="2026-09-01T01:02:00Z",
    )
    installed_state = AuthorityStateMemberImage.mint(
        layer_id="camera",
        locator=member.live_locator,
        sha256=after_state.sha256,
        state_revision=after_state.state_revision,
        binding=after_binding,
    )
    commit = AuthorityStateTransitionCommit.mint(
        intent_ref=intent_ref,
        intent=intent,
        observed_selection_token=after_token,
        installed_states=[installed_state],
        committed_at="2026-09-01T01:03:00Z",
    )
    commit_ref = _record_ref(commit, "commit-2")
    evaluation = AuthorityStateTransitionEvaluation.mint(
        intent_ref=intent_ref,
        commit_ref=commit_ref,
        commit=commit,
        result="satisfied",
        evaluated_at="2026-09-01T01:04:00Z",
    )
    evaluation_ref = _record_ref(evaluation, "evaluation-2")
    head = AuthorityStateCoordinatorHead.mint(
        commit_ref=commit_ref,
        commit=commit,
        evaluation_ref=evaluation_ref,
        evaluation=evaluation,
    )
    return locals()


@pytest.mark.parametrize(
    ("name", "record_type"),
    [
        ("proposal", AuthorityStateTransitionProposal),
        ("before_binding", LayerAuthorityBinding),
        ("member", AuthorityStateMemberTransition),
        ("intent", AuthorityStateTransitionIntent),
        ("pending", AuthorityStatePendingPointer),
        ("commit", AuthorityStateTransitionCommit),
        ("evaluation", AuthorityStateTransitionEvaluation),
        ("head", AuthorityStateCoordinatorHead),
    ],
)
def test_authority_state_records_round_trip_closed_canonical_rows(name: str, record_type: type) -> None:
    record = _chain()[name]
    encoded = record.as_dict()

    assert record_type.parse(encoded) == record
    with pytest.raises(AuthorityStateRecordError, match="fields mismatch"):
        record_type.parse({**encoded, "unexpected": True})
    with pytest.raises(AuthorityStateRecordError, match="is stale"):
        record_type.parse({**encoded, record.DIGEST_FIELD: _digest("tampered")})


def test_record_chain_is_acyclic_and_closes_each_exact_join() -> None:
    chain = _chain()
    proposal = chain["proposal"]
    after_binding = chain["after_binding"]
    intent = chain["intent"]
    commit = chain["commit"]
    evaluation = chain["evaluation"]
    head = chain["head"]

    assert after_binding.transition_proposal_digest == proposal.digest
    assert intent.proposal.digest == proposal.digest
    assert commit.intent_ref.record_digest == intent.digest
    assert evaluation.commit_ref.record_digest == commit.digest
    assert head.evaluation_ref.record_digest == evaluation.digest
    assert "commit_receipt_digest" not in after_binding.as_dict()
    assert "successor_head_digest" not in commit.as_dict()


def test_audit_timestamps_are_the_only_excluded_record_fields() -> None:
    chain = _chain()
    timestamp_fields = {
        "proposal": "proposed_at",
        "intent": "prepared_at",
        "pending": "selected_at",
        "commit": "committed_at",
        "evaluation": "evaluated_at",
    }
    for name, field in timestamp_fields.items():
        record = chain[name]
        assert replace(record, **{field: "2027-01-01T00:00:00Z"}).digest == record.digest

    proposal = chain["proposal"]
    assert replace(proposal, transaction_id="transition-3").digest != proposal.digest
    commit = chain["commit"]
    assert replace(commit, effects_digest=_digest("different-effects")).digest != commit.digest


def test_selection_tokens_and_pointer_images_must_match_exactly() -> None:
    chain = _chain()
    proposal = chain["proposal"]
    wrong_after = replace(
        proposal.after_selection_token,
        jit_pointer_sha256=_digest("wrong-jit-pointer"),
    )
    with pytest.raises(AuthorityStateRecordError, match="exact selection token"):
        replace(proposal, after_selection_token=wrong_after)

    assert authority_selection_token_dict(proposal.after_selection_token) == proposal.as_dict()[
        "after_selection_token"
    ]


def test_absent_pointer_head_is_represented_explicitly() -> None:
    absent = AuthorityPointerTransition.mint(
        pointer_kind="jit",
        live_locator="state/jit-layers/current.json",
        before=None,
        after=None,
    )
    assert AuthorityPointerTransition.parse(absent.as_dict()) == absent


@pytest.mark.parametrize(
    "locator",
    ["/absolute.json", "../escape.json", "state/../escape.json", "state//double.json", "state\\bad.json"],
)
def test_record_references_reject_unsafe_or_noncanonical_locators(locator: str) -> None:
    with pytest.raises(AuthorityStateRecordError, match=r"locator|POSIX"):
        AuthorityStateRecordRef.mint(
            locator=locator,
            sha256=_digest("bytes"),
            record_schema="example/v1",
            record_digest=_digest("record"),
        )


def test_lowercase_digests_and_positive_monotone_revisions_are_mandatory() -> None:
    with pytest.raises(AuthorityStateRecordError, match="lowercase"):
        AuthorityUnitBinding.mint(
            unit_id="unit",
            unit_generation_digest="A" * 64,
        )
    proposal = _chain()["proposal"]
    with pytest.raises(AuthorityStateRecordError, match="immediately follow"):
        replace(proposal, transition_revision=4)


def test_parsers_reject_unsorted_or_duplicate_rows_instead_of_normalizing() -> None:
    effect = AuthorityStateLayerEffect.mint(
        layer_id="layer",
        effect_kind="changed",
        invalidation_seed_unit_ids=["a"],
        invalidated_unit_ids=["a", "b"],
        revoked_unit_attempt_claim_ids=["a", "b"],
    )
    encoded = effect.as_dict()
    encoded["revoked_unit_attempt_claim_ids"] = ["b", "a"]
    encoded["effect_digest"] = effect.digest
    with pytest.raises(AuthorityStateRecordError, match="sorted and unique"):
        AuthorityStateLayerEffect.parse(encoded)

    duplicate = copy.deepcopy(effect.as_dict())
    duplicate["invalidated_unit_ids"] = ["a", "a"]
    with pytest.raises(AuthorityStateRecordError, match="sorted and unique"):
        AuthorityStateLayerEffect.parse(duplicate)


def test_intent_requires_immediate_predecessor_and_all_staged_after_bytes() -> None:
    chain = _chain()
    intent = chain["intent"]
    omitted = tuple(item for item in intent.staged_members if item.locator != chain["after_state"].locator)
    with pytest.raises(AuthorityStateRecordError, match="omits staged after-state"):
        replace(intent, staged_members=omitted)

    stale_before = replace(
        chain["before_binding"],
        transition_revision=3,
    )
    stale_image = replace(chain["before_state"], binding=stale_before)
    stale_member = replace(chain["member"], before=stale_image)
    with pytest.raises(AuthorityStateRecordError, match="immediate predecessor"):
        replace(intent, state_members=(stale_member,))


def test_commit_and_head_require_exact_satisfied_independent_evaluation() -> None:
    chain = _chain()
    commit = chain["commit"]
    intent = chain["intent"]
    intent_ref = chain["intent_ref"]
    with pytest.raises(AuthorityStateRecordError, match="proposed successor"):
        AuthorityStateTransitionCommit.mint(
            intent_ref=intent_ref,
            intent=intent,
            observed_selection_token=chain["before_token"],
            installed_states=[chain["installed_state"]],
            committed_at="2026-09-01T02:00:00Z",
        )

    failed = AuthorityStateTransitionEvaluation.mint(
        intent_ref=intent_ref,
        commit_ref=chain["commit_ref"],
        commit=commit,
        result="failed",
        findings=["state hash mismatch"],
        evaluated_at="2026-09-01T02:01:00Z",
    )
    with pytest.raises(AuthorityStateRecordError, match="satisfied independent evaluation"):
        AuthorityStateCoordinatorHead.mint(
            commit_ref=chain["commit_ref"],
            commit=commit,
            evaluation_ref=_record_ref(failed, "failed-evaluation"),
            evaluation=failed,
        )


def test_predecessor_binding_rows_are_sorted_by_mint_and_strict_on_parse() -> None:
    unit = AuthorityUnitBinding.mint(
        unit_id="unit",
        unit_generation_digest=_digest("unit"),
    )
    binding = LayerAuthorityBinding.mint(
        transition_revision=2,
        transition_proposal_digest=_digest("proposal"),
        selection_token=_token(1, 1, "selection"),
        layer_id="target",
        layer_generation_digest=_digest("target"),
        units=[unit],
        predecessors=[
            PredecessorLayerBinding.mint(
                layer_id="z", layer_generation_digest=_digest("z")
            ),
            PredecessorLayerBinding.mint(
                layer_id="a", layer_generation_digest=_digest("a")
            ),
        ],
    )
    assert [item.layer_id for item in binding.predecessors] == ["a", "z"]
    encoded = binding.as_dict()
    encoded["predecessors"] = list(reversed(encoded["predecessors"]))
    with pytest.raises(AuthorityStateRecordError, match="sorted"):
        LayerAuthorityBinding.parse(encoded)


def test_record_schema_is_part_of_every_semantic_digest() -> None:
    chain = _chain()
    assert chain["proposal"].as_dict()["schema"] == AUTHORITY_STATE_TRANSITION_PROPOSAL_SCHEMA
    assert chain["intent"].as_dict()["schema"] == AUTHORITY_STATE_TRANSITION_INTENT_SCHEMA
    assert chain["commit"].as_dict()["schema"] == AUTHORITY_STATE_TRANSITION_COMMIT_SCHEMA
    assert chain["evaluation"].as_dict()["schema"] == AUTHORITY_STATE_TRANSITION_EVALUATION_SCHEMA
