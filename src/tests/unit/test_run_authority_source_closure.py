"""Closed, source-addressed HIR-0172 authority observations."""

from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

from vfx_harness.domain.run_authority_source_closure import (
    AcceptedStateSourceClosure,
    DurableStateSourceClosure,
    InterruptionAuthoritySourceClosure,
    SelectedPlanSourceClosure,
)
from vfx_harness.domain.run_authority_source_identity import AuthoritySourceIdentity


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _source(
    kind: str,
    locator: str,
    record_schema: str,
    *,
    label: str | None = None,
) -> AuthoritySourceIdentity:
    identity = label or locator
    return AuthoritySourceIdentity.valid(
        source_kind=kind,
        locator=locator,
        byte_count=100 + len(identity),
        sha256=_digest(f"bytes:{identity}"),
        record_schema=record_schema,
        record_digest=_digest(f"record:{identity}"),
    )


def _opaque(
    kind: str,
    locator: str,
    *,
    label: str | None = None,
) -> AuthoritySourceIdentity:
    identity = label or locator
    return AuthoritySourceIdentity.opaque_valid(
        source_kind=kind,
        locator=locator,
        byte_count=100 + len(identity),
        sha256=_digest(f"bytes:{identity}"),
    )


def _state_record(schema: str, label: str) -> AuthoritySourceIdentity:
    sha256 = _digest(f"bytes:{label}")
    return AuthoritySourceIdentity.valid(
        source_kind="durable_state_record",
        locator=f"state/authority-state/objects/{sha256}/record.json",
        byte_count=200 + len(label),
        sha256=sha256,
        record_schema=schema,
        record_digest=_digest(f"record:{label}"),
    )


def _valid_closure(*, ledger_label: str = "ledger-a") -> InterruptionAuthoritySourceClosure:
    bundle = _digest("selected-bundle")
    view = _digest("effective-view")
    plan_pointer = _source(
        "plan_pointer",
        "plans/current.json",
        "vfx-harness.plan-pointer/v2",
    )
    manifest = _source(
        "plan_bundle_manifest",
        f"runs/planner-001/checkpoints/plans/bundles/{bundle}/bundle.json",
        "vfx-harness.plan-bundle/v1",
    )
    plan_members = (
        _opaque(
            "plan_bundle_member",
            f"runs/planner-001/checkpoints/plans/bundles/{bundle}/requirements.json",
        ),
        _opaque(
            "plan_bundle_member",
            f"runs/planner-001/checkpoints/plans/bundles/{bundle}/layers.json",
        ),
    )
    view_pointer = _source(
        "effective_view_pointer",
        "state/jit-layers/current.json",
        "vfx-harness.jit-layer-view/v2",
    )
    view_members = (
        _opaque(
            "effective_view_member",
            f"state/jit-layers/views/{view}/scene_checks.json",
        ),
        _opaque(
            "effective_view_member",
            f"state/jit-layers/views/{view}/layers.json",
        ),
    )
    selected = SelectedPlanSourceClosure.mint_present(
        plan_pointer=plan_pointer,
        bundle_manifest=manifest,
        bundle_members=reversed(plan_members),
        effective_view_pointer=view_pointer,
        effective_view_members=reversed(view_members),
        plan_amendments=_opaque(
            "plan_amendments",
            "plan_amendments.jsonl",
        ),
        plan_resolutions=_opaque(
            "plan_resolutions",
            "state/plan-resolutions.jsonl",
        ),
    )

    accepted = AcceptedStateSourceClosure.mint_present(
        ledger=_source(
            "accepted_ledger",
            "shot.json",
            "vfx-harness.shot-ledger/v1",
            label=ledger_label,
        ),
        members=(
            _opaque(
                "accepted_member",
                "build/units/camera/camera.py",
            ),
            _source(
                "accepted_member",
                "runs/builder-004/checkpoints/layers/camera/outcome.json",
                "vfx-harness.layer-finalization-receipt/v2",
            ),
        ),
        judgment_debts=_opaque(
            "judgment_debts",
            "state/judgment-debts.jsonl",
        ),
        judgment_payment_attempts=_opaque(
            "judgment_payment_attempts",
            "state/judgment-payment-attempts.jsonl",
        ),
    )

    head = _state_record("vfx-harness.authority-state-head/v1", "head")
    durable = DurableStateSourceClosure.mint_present(
        current_pointer=_source(
            "durable_state_pointer",
            "state/authority-state/current.json",
            "vfx-harness.authority-state-record-ref/v1",
        ),
        head=head,
        records=reversed(
            (
                _state_record(
                    "vfx-harness.authority-state-transition-commit/v1",
                    "commit",
                ),
                _state_record(
                    "vfx-harness.authority-state-transition-evaluation/v1",
                    "evaluation",
                ),
                _state_record(
                    "vfx-harness.authority-state-transition-intent/v1",
                    "pending intent",
                ),
                _state_record(
                    "vfx-harness.authority-state-transition-proposal/v1",
                    "pending proposal",
                ),
            )
        ),
        members=(
            _source(
                "durable_state_member",
                "state/work-units/layer_camera.json",
                "vfx-harness.work-unit-state/v3",
            ),
        ),
        pending=_source(
            "durable_state_pending",
            "state/authority-state/pending.json",
            "vfx-harness.authority-state-pending/v1",
        ),
    )
    return InterruptionAuthoritySourceClosure(selected, accepted, durable)


def test_heterogeneous_source_closure_round_trips_and_sorts_members() -> None:
    closure = _valid_closure()

    assert closure.selected_plan.state == "present_valid"
    assert closure.selected_plan.decision_state == "present_valid"
    assert closure.accepted_state.state == "present_valid"
    assert closure.accepted_state.judgment_state == "present_valid"
    assert closure.durable_state.current_state == "present_valid"
    assert closure.durable_state.pending_state == "present_valid"
    assert [row.locator for row in closure.selected_plan.bundle_members] == sorted(
        row.locator for row in closure.selected_plan.bundle_members
    )
    assert [row.locator for row in closure.durable_state.records] == sorted(
        row.locator for row in closure.durable_state.records
    )
    assert {row.source_state for row in closure.selected_plan.bundle_members} == {"opaque_valid"}
    assert closure.accepted_state.members[0].source_state == "opaque_valid"
    assert closure.selected_plan.plan_amendments is not None
    assert closure.selected_plan.plan_amendments.locator == "plan_amendments.jsonl"
    assert closure.selected_plan.plan_resolutions is not None
    assert closure.selected_plan.plan_resolutions.locator == "state/plan-resolutions.jsonl"
    assert closure.accepted_state.judgment_debts is not None
    assert closure.accepted_state.judgment_debts.locator == "state/judgment-debts.jsonl"
    assert closure.accepted_state.judgment_payment_attempts is not None
    assert closure.accepted_state.judgment_payment_attempts.locator == "state/judgment-payment-attempts.jsonl"
    assert closure.durable_state.pending is not None
    assert closure.durable_state.pending.locator == "state/authority-state/pending.json"
    assert {
        row.record_schema
        for row in closure.durable_state.records
        if row.record_schema
        in {
            "vfx-harness.authority-state-transition-intent/v1",
            "vfx-harness.authority-state-transition-proposal/v1",
        }
    } == {
        "vfx-harness.authority-state-transition-intent/v1",
        "vfx-harness.authority-state-transition-proposal/v1",
    }
    assert InterruptionAuthoritySourceClosure.from_dict(closure.as_dict()) == closure


def test_all_absent_is_explicit_typed_authority_not_an_empty_projection() -> None:
    closure = InterruptionAuthoritySourceClosure(
        SelectedPlanSourceClosure.absent(),
        AcceptedStateSourceClosure.absent(),
        DurableStateSourceClosure.absent(),
    )

    assert InterruptionAuthoritySourceClosure.from_dict(closure.as_dict()) == closure
    assert closure.as_dict()["selected_plan"] == {
        "schema": "vfx-harness.interruption-selected-plan-source-closure/v1",
        "state": "absent",
        "plan_pointer": None,
        "bundle_manifest": None,
        "bundle_members": [],
        "effective_view_pointer": None,
        "effective_view_members": [],
        "plan_amendments": None,
        "plan_resolutions": None,
        "decision_state": "absent",
        "closure_digest": closure.selected_plan.digest,
    }

    arbitrary = {
        "schema": closure.SCHEMA,
        "selected": {"bundle_digest": _digest("caller projection")},
        "accepted_state_digest": _digest("caller projection"),
        "closure_digest": _digest("invented"),
    }
    with pytest.raises(ValueError, match="fields mismatch"):
        InterruptionAuthoritySourceClosure.from_dict(arbitrary)


def test_present_families_cannot_be_empty_or_claim_arbitrary_state() -> None:
    with pytest.raises(ValueError, match="plan_pointer"):
        SelectedPlanSourceClosure("present_valid", None, None, (), None, ())
    with pytest.raises(ValueError, match="accepted_ledger"):
        AcceptedStateSourceClosure("present_valid", None, ())
    with pytest.raises(ValueError, match="durable_state_pointer"):
        DurableStateSourceClosure("present_valid", None, None, (), ())

    valid = _valid_closure().accepted_state
    with pytest.raises(ValueError, match="must be derived"):
        replace(valid, state="present_invalid")


def test_auxiliary_authority_is_explicit_even_without_selected_heads() -> None:
    plan_amendments = _opaque(
        "plan_amendments",
        "plan_amendments.jsonl",
    )
    plan_resolutions = _opaque(
        "plan_resolutions",
        "state/plan-resolutions.jsonl",
    )
    judgment_debts = _opaque(
        "judgment_debts",
        "state/judgment-debts.jsonl",
    )
    judgment_attempts = _opaque(
        "judgment_payment_attempts",
        "state/judgment-payment-attempts.jsonl",
    )
    pending = _source(
        "durable_state_pending",
        "state/authority-state/pending.json",
        "vfx-harness.authority-state-pending/v1",
    )
    pending_records = (
        _state_record(
            "vfx-harness.authority-state-transition-intent/v1",
            "pending-only intent",
        ),
        _state_record(
            "vfx-harness.authority-state-transition-proposal/v1",
            "pending-only proposal",
        ),
    )
    closure = InterruptionAuthoritySourceClosure(
        SelectedPlanSourceClosure.absent(
            plan_amendments=plan_amendments,
            plan_resolutions=plan_resolutions,
        ),
        AcceptedStateSourceClosure.absent(
            judgment_debts=judgment_debts,
            judgment_payment_attempts=judgment_attempts,
        ),
        DurableStateSourceClosure.absent(
            pending=pending,
            records=pending_records,
        ),
    )

    assert closure.selected_plan.state == "absent"
    assert closure.selected_plan.decision_state == "present_valid"
    assert closure.accepted_state.state == "absent"
    assert closure.accepted_state.judgment_state == "present_valid"
    assert closure.durable_state.current_state == "absent"
    assert closure.durable_state.pending_state == "present_valid"
    assert closure.durable_state.records == tuple(sorted(pending_records, key=lambda source: source.locator))
    assert InterruptionAuthoritySourceClosure.from_dict(closure.as_dict()) == closure
    assert (
        closure.digest
        != InterruptionAuthoritySourceClosure(
            SelectedPlanSourceClosure.absent(),
            AcceptedStateSourceClosure.absent(),
            DurableStateSourceClosure.absent(),
        ).digest
    )


def test_every_mutable_auxiliary_authority_source_changes_closure_identity() -> None:
    closure = _valid_closure()
    replacements = (
        InterruptionAuthoritySourceClosure(
            replace(
                closure.selected_plan,
                plan_amendments=_opaque(
                    "plan_amendments",
                    "plan_amendments.jsonl",
                    label="changed amendments",
                ),
            ),
            closure.accepted_state,
            closure.durable_state,
        ),
        InterruptionAuthoritySourceClosure(
            replace(
                closure.selected_plan,
                plan_resolutions=_opaque(
                    "plan_resolutions",
                    "state/plan-resolutions.jsonl",
                    label="changed resolutions",
                ),
            ),
            closure.accepted_state,
            closure.durable_state,
        ),
        InterruptionAuthoritySourceClosure(
            closure.selected_plan,
            replace(
                closure.accepted_state,
                judgment_debts=_opaque(
                    "judgment_debts",
                    "state/judgment-debts.jsonl",
                    label="changed debts",
                ),
            ),
            closure.durable_state,
        ),
        InterruptionAuthoritySourceClosure(
            closure.selected_plan,
            replace(
                closure.accepted_state,
                judgment_payment_attempts=_opaque(
                    "judgment_payment_attempts",
                    "state/judgment-payment-attempts.jsonl",
                    label="changed attempts",
                ),
            ),
            closure.durable_state,
        ),
        InterruptionAuthoritySourceClosure(
            closure.selected_plan,
            closure.accepted_state,
            replace(
                closure.durable_state,
                pending=_source(
                    "durable_state_pending",
                    "state/authority-state/pending.json",
                    "vfx-harness.authority-state-pending/v1",
                    label="changed pending",
                ),
            ),
        ),
    )

    assert all(candidate.digest != closure.digest for candidate in replacements)


def test_pending_pointer_requires_and_binds_its_immutable_record_graph() -> None:
    pending = _source(
        "durable_state_pending",
        "state/authority-state/pending.json",
        "vfx-harness.authority-state-pending/v1",
    )
    intent = _state_record(
        "vfx-harness.authority-state-transition-intent/v1",
        "pending-only intent",
    )

    with pytest.raises(ValueError, match="requires its immutable record closure"):
        DurableStateSourceClosure.absent(pending=pending)
    with pytest.raises(ValueError, match="require a valid current or pending root"):
        DurableStateSourceClosure.absent(records=(intent,))

    closure = DurableStateSourceClosure.absent(
        pending=pending,
        records=(intent,),
    )
    assert DurableStateSourceClosure.from_dict(closure.as_dict()) == closure
    assert (
        replace(
            closure,
            records=(
                _state_record(
                    "vfx-harness.authority-state-transition-intent/v1",
                    "tampered pending intent bytes",
                ),
            ),
        ).digest
        != closure.digest
    )


def test_invalid_auxiliary_streams_are_not_hidden_by_absent_primary_roots() -> None:
    amendments = AuthoritySourceIdentity.raw_invalid(
        source_kind="plan_amendments",
        locator="plan_amendments.jsonl",
        byte_count=7,
        sha256=_digest("invalid amendments"),
        invalid_reason="non_utf8",
    )
    debts = AuthoritySourceIdentity.raw_invalid(
        source_kind="judgment_debts",
        locator="state/judgment-debts.jsonl",
        byte_count=11,
        sha256=_digest("invalid debt stream"),
        invalid_reason="malformed_json",
    )

    selected = SelectedPlanSourceClosure.absent(plan_amendments=amendments)
    accepted = AcceptedStateSourceClosure.absent(judgment_debts=debts)

    assert selected.state == "absent"
    assert selected.decision_state == "present_invalid"
    assert accepted.state == "absent"
    assert accepted.judgment_state == "present_invalid"
    assert SelectedPlanSourceClosure.from_dict(selected.as_dict()) == selected
    assert AcceptedStateSourceClosure.from_dict(accepted.as_dict()) == accepted

    with pytest.raises(ValueError, match="decision_state must be derived"):
        replace(selected, decision_state="absent")
    with pytest.raises(ValueError, match="judgment_state must be derived"):
        replace(accepted, judgment_state="absent")


def test_invalid_pending_pointer_cannot_claim_derived_records() -> None:
    pending = AuthoritySourceIdentity.raw_invalid(
        source_kind="durable_state_pending",
        locator="state/authority-state/pending.json",
        byte_count=13,
        sha256=_digest("malformed pending pointer"),
        invalid_reason="malformed_json",
    )
    intent = _state_record(
        "vfx-harness.authority-state-transition-intent/v1",
        "invented pending intent",
    )

    invalid_closure = DurableStateSourceClosure.absent(pending=pending)
    assert invalid_closure.current_state == "absent"
    assert invalid_closure.pending_state == "present_invalid"

    with pytest.raises(ValueError, match="require a valid current or pending root"):
        DurableStateSourceClosure.absent(
            pending=pending,
            records=(intent,),
        )


def test_current_and_pending_roots_share_one_canonical_object_table() -> None:
    current_pointer = _source(
        "durable_state_pointer",
        "state/authority-state/current.json",
        "vfx-harness.authority-state-record-ref/v1",
    )
    head = _state_record("vfx-harness.authority-state-head/v1", "shared head")
    shared = _state_record(
        "vfx-harness.authority-state-transition-intent/v1",
        "shared transition object",
    )
    pending = _source(
        "durable_state_pending",
        "state/authority-state/pending.json",
        "vfx-harness.authority-state-pending/v1",
    )

    closure = DurableStateSourceClosure.mint_present(
        current_pointer=current_pointer,
        head=head,
        records=(shared,),
        pending=pending,
    )

    assert closure.records == (shared,)
    assert closure.current_state == "present_valid"
    assert closure.pending_state == "present_valid"
    assert DurableStateSourceClosure.from_dict(closure.as_dict()) == closure
    with pytest.raises(ValueError, match="requires its immutable record closure"):
        DurableStateSourceClosure.mint_present(
            current_pointer=current_pointer,
            head=head,
            pending=pending,
        )


def test_malformed_authority_keeps_raw_bytes_without_inventing_record_identity() -> None:
    raw = AuthoritySourceIdentity.raw_invalid(
        source_kind="plan_pointer",
        locator="plans/current.json",
        byte_count=17,
        sha256=_digest("malformed plan pointer bytes"),
        invalid_reason="malformed_json",
    )
    selected = SelectedPlanSourceClosure.mint_present(plan_pointer=raw)

    assert selected.state == "present_invalid"
    assert selected.plan_pointer is not None
    assert selected.plan_pointer.sha256 == _digest("malformed plan pointer bytes")
    assert selected.plan_pointer.record_schema is None
    assert selected.plan_pointer.record_digest is None
    assert SelectedPlanSourceClosure.from_dict(selected.as_dict()) == selected

    with pytest.raises(ValueError, match="cannot claim derived"):
        SelectedPlanSourceClosure.mint_present(
            plan_pointer=raw,
            bundle_manifest=_valid_closure().selected_plan.bundle_manifest,
        )


def test_invalid_source_cannot_invent_schema_digest_or_open_issue_vocabulary() -> None:
    with pytest.raises(ValueError, match="cannot invent"):
        AuthoritySourceIdentity(
            "accepted_ledger",
            "shot.json",
            8,
            _digest("bad ledger"),
            "raw_invalid",
            "vfx-harness.shot-ledger/v1",
            _digest("fake record"),
            "malformed_json",
        )
    with pytest.raises(ValueError, match="invalid_reason"):
        AuthoritySourceIdentity.raw_invalid(
            source_kind="accepted_ledger",
            locator="shot.json",
            byte_count=8,
            sha256=_digest("bad ledger"),
            invalid_reason="whatever_the_caller_says",
        )


@pytest.mark.parametrize(
    ("kind", "locator"),
    (
        ("plan_pointer", "plans/current.json"),
        (
            "plan_bundle_manifest",
            f"runs/planner/checkpoints/plans/bundles/{_digest('bundle')}/bundle.json",
        ),
        ("effective_view_pointer", "state/jit-layers/current.json"),
        ("durable_state_pointer", "state/authority-state/current.json"),
        ("durable_state_pending", "state/authority-state/pending.json"),
    ),
)
def test_typed_pointer_and_manifest_sources_reject_opaque_identity(
    kind: str,
    locator: str,
) -> None:
    with pytest.raises(ValueError, match="requires record_valid"):
        AuthoritySourceIdentity.opaque_valid(
            source_kind=kind,
            locator=locator,
            byte_count=41,
            sha256=_digest(f"opaque:{kind}"),
        )


def test_opaque_source_binds_only_exact_bytes_and_cannot_invent_record_identity() -> None:
    source = _opaque(
        "accepted_member",
        "build/units/form/mesh.py",
    )

    assert source.record_schema is None
    assert source.record_digest is None
    assert source.invalid_reason is None
    assert AuthoritySourceIdentity.from_dict(source.as_dict()) == source

    with pytest.raises(ValueError, match="cannot invent record identity"):
        AuthoritySourceIdentity(
            "accepted_member",
            "build/units/form/mesh.py",
            source.byte_count,
            source.sha256,
            "opaque_valid",
            "vfx-harness.accepted-script/v1",
            _digest("invented record"),
            None,
        )


@pytest.mark.parametrize(
    ("kind", "locator"),
    (
        ("plan_amendments", "plan_amendments.jsonl"),
        ("plan_resolutions", "state/plan-resolutions.jsonl"),
        ("judgment_debts", "state/judgment-debts.jsonl"),
        (
            "judgment_payment_attempts",
            "state/judgment-payment-attempts.jsonl",
        ),
    ),
)
def test_append_only_authority_streams_cannot_invent_typed_record_identity(
    kind: str,
    locator: str,
) -> None:
    with pytest.raises(ValueError, match="exact raw-byte stream"):
        AuthoritySourceIdentity.valid(
            source_kind=kind,
            locator=locator,
            byte_count=21,
            sha256=_digest(f"bytes:{kind}"),
            record_schema="vfx-harness.invented/v1",
            record_digest=_digest(f"record:{kind}"),
        )


@pytest.mark.parametrize(
    ("kind", "locator"),
    (
        ("plan_amendments", "state/plan_amendments.jsonl"),
        ("plan_resolutions", "plan-resolutions.jsonl"),
        ("judgment_debts", "judgment-debts.jsonl"),
        ("judgment_payment_attempts", "state/other-attempts.jsonl"),
        ("durable_state_pending", "state/authority-state/other.json"),
    ),
)
def test_auxiliary_authority_streams_have_fixed_locators(
    kind: str,
    locator: str,
) -> None:
    with pytest.raises(ValueError, match="exact locator"):
        AuthoritySourceIdentity.raw_invalid(
            source_kind=kind,
            locator=locator,
            byte_count=8,
            sha256=_digest(f"bad:{kind}"),
            invalid_reason="malformed_json",
        )


def test_locator_namespace_and_content_addressing_fail_closed() -> None:
    with pytest.raises(ValueError, match="exact locator"):
        _source(
            "plan_pointer",
            "scratch/current.json",
            "vfx-harness.plan-pointer/v2",
        )
    with pytest.raises(ValueError, match="content-addressed by its byte SHA"):
        AuthoritySourceIdentity.valid(
            source_kind="durable_state_record",
            locator=(f"state/authority-state/objects/{_digest('another payload')}/record.json"),
            byte_count=33,
            sha256=_digest("actual payload"),
            record_schema="vfx-harness.authority-state-head/v1",
            record_digest=_digest("head record"),
        )


def test_wire_tampering_and_unknown_fields_fail_closed() -> None:
    closure = _valid_closure()
    wire = closure.as_dict()
    wire["accepted_state"]["ledger"]["sha256"] = _digest("substituted bytes")
    with pytest.raises(ValueError, match="source_digest is stale"):
        InterruptionAuthoritySourceClosure.from_dict(wire)

    wire = closure.as_dict()
    wire["authority_projection"] = {"accepted": True}
    with pytest.raises(ValueError, match=r"unexpected=.*authority_projection"):
        InterruptionAuthoritySourceClosure.from_dict(wire)


def test_any_authority_source_change_changes_the_canonical_closure_digest() -> None:
    first = _valid_closure(ledger_label="ledger-before")
    second = _valid_closure(ledger_label="ledger-after")

    assert first.accepted_state.ledger is not None
    assert second.accepted_state.ledger is not None
    assert first.accepted_state.ledger.sha256 != second.accepted_state.ledger.sha256
    assert first.digest != second.digest
