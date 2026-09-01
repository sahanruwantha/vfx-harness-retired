from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

import pytest

from vfx_harness.domain.acceptance_outcomes import (
    AcceptanceMomentOutcome,
    AcceptanceOutcome,
)
from vfx_harness.domain.authority_head_records import AuthoritySelectionTokenProjection
from vfx_harness.domain.authority_state_records import (
    AUTHORITY_STATE_HEAD_SCHEMA,
    AuthorityStateRecordRef,
)
from vfx_harness.domain.shot_ledger_v2 import (
    AcceptanceMomentEvidenceBinding,
    AcceptedLayerAuthority,
    ShotLedgerAcceptance,
    ShotLedgerV2,
    canonical_layer_outcome_locator,
)


def _digest(character: str) -> str:
    return character * 64


def _accepted_chain_digest() -> str:
    return _digest("5")


def _selection_token() -> AuthoritySelectionTokenProjection:
    return AuthoritySelectionTokenProjection(
        plan_revision=7,
        plan_pointer_sha256=_digest("1"),
        jit_revision=4,
        jit_pointer_sha256=_digest("2"),
    )


def _head_ref(*, record_schema: str = AUTHORITY_STATE_HEAD_SCHEMA) -> AuthorityStateRecordRef:
    return AuthorityStateRecordRef.mint(
        locator=f"state/authority-state/objects/{_digest('3')}.json",
        sha256=_digest("3"),
        record_schema=record_schema,
        record_digest=_digest("4"),
    )


def _layer(
    layer_id: str,
    *,
    generation: str,
    finalization: str,
    script_hash: str,
    outcome_hash: str,
) -> AcceptedLayerAuthority:
    return AcceptedLayerAuthority(
        layer_id=layer_id,
        layer_generation_digest=_digest(generation),
        finalization_receipt_digest=_digest(finalization),
        composed_script_locator=f"build/{layer_id}.py",
        composed_script_sha256=_digest(script_hash),
        sealed_outcome_locator=canonical_layer_outcome_locator(layer_id),
        sealed_outcome_sha256=_digest(outcome_hash),
    )


def _layers() -> tuple[AcceptedLayerAuthority, ...]:
    return (
        _layer(
            "hero",
            generation="9",
            finalization="a",
            script_hash="b",
            outcome_hash="c",
        ),
        _layer(
            "camera",
            generation="5",
            finalization="6",
            script_hash="7",
            outcome_hash="8",
        ),
    )


def _moment(moment_id: str, *, evidence: str, passed: bool = True) -> AcceptanceMomentOutcome:
    return AcceptanceMomentOutcome(
        moment_id=moment_id,
        passed=passed,
        decided_by="critic",
        evidence_digest=_digest(evidence),
    )


def _evidence(moment_id: str, *, evidence: str, suffix: str) -> AcceptanceMomentEvidenceBinding:
    return AcceptanceMomentEvidenceBinding(
        moment_id=moment_id,
        evidence_digest=_digest(evidence),
        evidence_record_locator=f"runs/run-1/evidence/acceptance/{suffix}.json",
        evidence_record_sha256=_digest("d"),
        render_locator=f"runs/run-1/evidence/renders/{suffix}.png",
        render_sha256=_digest("e"),
        reference_locator=f"refs/{suffix}.png",
        reference_sha256=_digest("f"),
    )


def _acceptance(*, chain_digest: str | None = None) -> ShotLedgerAcceptance:
    outcome = AcceptanceOutcome(
        authority_digest=_digest("0"),
        bundle_digest=_digest("1"),
        view_digest=_digest("2"),
        chain_digest=chain_digest or _accepted_chain_digest(),
        moments=(
            _moment("close", evidence="3"),
            _moment("wide", evidence="4"),
        ),
    )
    return ShotLedgerAcceptance(
        outcome=outcome,
        moment_evidence=(
            _evidence("close", evidence="3", suffix="close"),
            _evidence("wide", evidence="4", suffix="wide"),
        ),
    )


def _ledger(*, with_acceptance: bool = True) -> ShotLedgerV2:
    layers = _layers()
    return ShotLedgerV2(
        selection_token=_selection_token(),
        authority_state_head_ref=_head_ref(),
        accepted_layers=layers,
        accepted_chain_digest=_accepted_chain_digest(),
        acceptance=_acceptance() if with_acceptance else None,
    )


def test_shot_ledger_v2_round_trips_complete_accepted_build() -> None:
    ledger = _ledger()

    parsed = ShotLedgerV2.from_dict(ledger.as_dict())

    assert parsed == ledger
    assert parsed.as_dict() == ledger.as_dict()
    assert tuple(layer.layer_id for layer in parsed.accepted_layers) == (
        "hero",
        "camera",
    )
    assert parsed.acceptance is not None
    assert tuple(binding.moment_id for binding in parsed.acceptance.moment_evidence) == ("close", "wide")


def test_shot_ledger_v2_round_trips_without_optional_acceptance() -> None:
    ledger = _ledger(with_acceptance=False)

    assert ShotLedgerV2.from_dict(ledger.as_dict()) == ledger
    assert ledger.as_dict()["acceptance"] is None


@pytest.mark.parametrize(
    "target,field",
    (
        ("root", "unexpected"),
        ("layer", "unexpected"),
        ("acceptance", "unexpected"),
        ("binding", "unexpected"),
        ("outcome", "unexpected"),
        ("head_ref", "unexpected"),
        ("selection_token", "unexpected"),
    ),
)
def test_shot_ledger_v2_rejects_extra_fields(target: str, field: str) -> None:
    raw = deepcopy(_ledger().as_dict())
    selected = {
        "root": raw,
        "layer": raw["accepted_layers"][0],
        "acceptance": raw["acceptance"],
        "binding": raw["acceptance"]["moment_evidence"][0],
        "outcome": raw["acceptance"]["outcome"],
        "head_ref": raw["authority_state_head_ref"],
        "selection_token": raw["selection_token"],
    }[target]
    selected[field] = True

    with pytest.raises(ValueError, match="fields mismatch"):
        ShotLedgerV2.from_dict(raw)


def test_shot_ledger_v2_preserves_topological_order_and_rejects_duplicate_ids() -> None:
    hero, camera = _layers()

    ledger = ShotLedgerV2(
        selection_token=_selection_token(),
        authority_state_head_ref=_head_ref(),
        accepted_layers=(hero, camera),
        accepted_chain_digest=_accepted_chain_digest(),
        acceptance=None,
    )
    assert tuple(layer.layer_id for layer in ledger.accepted_layers) == (
        "hero",
        "camera",
    )

    with pytest.raises(ValueError, match="duplicate layer ids"):
        ShotLedgerV2(
            selection_token=_selection_token(),
            authority_state_head_ref=_head_ref(),
            accepted_layers=(hero, hero),
            accepted_chain_digest=_accepted_chain_digest(),
            acceptance=None,
        )


def test_shot_ledger_v2_rejects_aliased_terminal_members() -> None:
    hero, camera = _layers()

    with pytest.raises(ValueError, match="duplicate finalization receipt digests"):
        ShotLedgerV2(
            selection_token=_selection_token(),
            authority_state_head_ref=_head_ref(),
            accepted_layers=(
                camera,
                replace(
                    hero,
                    finalization_receipt_digest=camera.finalization_receipt_digest,
                ),
            ),
            accepted_chain_digest=_accepted_chain_digest(),
            acceptance=None,
        )
    with pytest.raises(ValueError, match="duplicate composed script locators"):
        ShotLedgerV2(
            selection_token=_selection_token(),
            authority_state_head_ref=_head_ref(),
            accepted_layers=(
                camera,
                replace(hero, composed_script_locator=camera.composed_script_locator),
            ),
            accepted_chain_digest=_accepted_chain_digest(),
            acceptance=None,
        )


@pytest.mark.parametrize(
    "field",
    (
        "layer_generation_digest",
        "finalization_receipt_digest",
        "composed_script_sha256",
        "sealed_outcome_sha256",
    ),
)
def test_accepted_layer_rejects_malformed_digests(field: str) -> None:
    layer = _layers()[0]

    with pytest.raises(ValueError, match="lowercase SHA-256"):
        replace(layer, **{field: "BAD"})


@pytest.mark.parametrize(
    "locator",
    (
        "/build/camera.py",
        "build/../camera.py",
        "build\\camera.py",
        "build/camera.py#fragment",
        "build//camera.py",
        "build/units/camera/unit.py",
        "scripts/camera.py",
        "build/camera.txt",
    ),
)
def test_accepted_layer_rejects_noncanonical_composed_script_locators(
    locator: str,
) -> None:
    with pytest.raises(ValueError, match=r"locator|Python artifact|composed layer"):
        replace(_layers()[0], composed_script_locator=locator)


def test_accepted_layer_rejects_noncanonical_sealed_outcome_locator() -> None:
    layer = _layers()[0]

    with pytest.raises(ValueError, match="canonical layer locator"):
        replace(
            layer,
            sealed_outcome_locator=canonical_layer_outcome_locator("camera"),
        )


def test_shot_ledger_v2_requires_exact_authority_state_head_ref() -> None:
    with pytest.raises(ValueError, match="authority-state-head/v1"):
        ShotLedgerV2(
            selection_token=_selection_token(),
            authority_state_head_ref=_head_ref(record_schema="vfx-harness.authority-state-transition-commit/v1"),
            accepted_layers=(),
            accepted_chain_digest=_accepted_chain_digest(),
            acceptance=None,
        )


def test_shot_ledger_v2_rejects_accepted_layers_without_selected_plan() -> None:
    absent_selection = AuthoritySelectionTokenProjection(
        plan_revision=0,
        plan_pointer_sha256=None,
        jit_revision=0,
        jit_pointer_sha256=None,
    )

    with pytest.raises(ValueError, match="without a selected plan"):
        ShotLedgerV2(
            selection_token=absent_selection,
            authority_state_head_ref=_head_ref(),
            accepted_layers=_layers(),
            accepted_chain_digest=_accepted_chain_digest(),
            acceptance=None,
        )


def test_shot_ledger_v2_validates_direct_selection_token_values() -> None:
    malformed_selection = AuthoritySelectionTokenProjection(
        plan_revision=1,
        plan_pointer_sha256="BAD",
        jit_revision=0,
        jit_pointer_sha256=None,
    )

    with pytest.raises(ValueError, match="lowercase SHA-256"):
        ShotLedgerV2(
            selection_token=malformed_selection,
            authority_state_head_ref=_head_ref(),
            accepted_layers=(),
            accepted_chain_digest=_accepted_chain_digest(),
            acceptance=None,
        )


def test_shot_ledger_v2_rejects_malformed_accepted_chain_digest() -> None:
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        replace(
            _ledger(with_acceptance=False),
            accepted_chain_digest="BAD",
        )


def test_shot_ledger_acceptance_requires_passing_outcome() -> None:
    outcome = AcceptanceOutcome(
        authority_digest=_digest("0"),
        bundle_digest=_digest("1"),
        view_digest=_digest("2"),
        chain_digest=_accepted_chain_digest(),
        moments=(_moment("wide", evidence="3", passed=False),),
    )

    with pytest.raises(ValueError, match="must be passing"):
        ShotLedgerAcceptance(
            outcome=outcome,
            moment_evidence=(_evidence("wide", evidence="3", suffix="wide"),),
        )


def test_shot_ledger_acceptance_rejects_passing_no_signal_outcome() -> None:
    outcome = AcceptanceOutcome(
        authority_digest=_digest("0"),
        bundle_digest=_digest("1"),
        view_digest=_digest("2"),
        chain_digest=_accepted_chain_digest(),
        moments=(
            AcceptanceMomentOutcome(
                moment_id="wide",
                passed=True,
                decided_by="no_optical_signal",
                evidence_digest=_digest("3"),
            ),
        ),
    )

    with pytest.raises(ValueError, match="no-optical-signal"):
        ShotLedgerAcceptance(
            outcome=outcome,
            moment_evidence=(_evidence("wide", evidence="3", suffix="wide"),),
        )


def test_shot_ledger_acceptance_requires_complete_ordered_moment_bindings() -> None:
    acceptance = _acceptance()

    with pytest.raises(ValueError, match="exact ordered moment set"):
        replace(acceptance, moment_evidence=acceptance.moment_evidence[:1])
    with pytest.raises(ValueError, match="exact ordered moment set"):
        replace(acceptance, moment_evidence=tuple(reversed(acceptance.moment_evidence)))


def test_shot_ledger_acceptance_requires_matching_semantic_evidence_digest() -> None:
    acceptance = _acceptance()
    first, second = acceptance.moment_evidence

    with pytest.raises(ValueError, match="digests must match"):
        replace(
            acceptance,
            moment_evidence=(replace(first, evidence_digest=_digest("5")), second),
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        (
            "evidence_record_locator",
            "../moment.json",
            "normalized safe relative locator",
        ),
        ("evidence_record_locator", "evidence/moment.txt", "JSON record"),
        ("render_locator", "/render.png", "normalized safe relative locator"),
        ("reference_locator", "refs\\reference.png", "normalized safe relative locator"),
    ),
)
def test_moment_evidence_binding_rejects_malformed_locators(
    field: str,
    value: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        replace(_evidence("wide", evidence="3", suffix="wide"), **{field: value})


def test_moment_evidence_binding_rejects_aliased_sources() -> None:
    binding = _evidence("wide", evidence="3", suffix="wide")

    with pytest.raises(ValueError, match="three distinct sources"):
        replace(binding, reference_locator=binding.render_locator)


@pytest.mark.parametrize(
    "field",
    (
        "evidence_digest",
        "evidence_record_sha256",
        "render_sha256",
        "reference_sha256",
    ),
)
def test_moment_evidence_binding_rejects_malformed_digests(field: str) -> None:
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        replace(
            _evidence("wide", evidence="3", suffix="wide"),
            **{field: "BAD"},
        )


def test_shot_ledger_v2_rejects_acceptance_for_another_layer_chain() -> None:
    layers = _layers()
    acceptance = _acceptance()
    stale_outcome = replace(acceptance.outcome, chain_digest=_digest("6"))

    with pytest.raises(ValueError, match="chain digest does not match"):
        ShotLedgerV2(
            selection_token=_selection_token(),
            authority_state_head_ref=_head_ref(),
            accepted_layers=layers,
            accepted_chain_digest=_accepted_chain_digest(),
            acceptance=replace(acceptance, outcome=stale_outcome),
        )


def test_shot_ledger_v2_rejects_root_accepted_chain_mismatch() -> None:
    with pytest.raises(ValueError, match="accepted_chain_digest"):
        replace(
            _ledger(),
            accepted_chain_digest=_digest("6"),
        )


def test_shot_ledger_v2_rejects_acceptance_without_accepted_layers() -> None:
    with pytest.raises(ValueError, match="acceptance without accepted layers"):
        ShotLedgerV2(
            selection_token=_selection_token(),
            authority_state_head_ref=_head_ref(),
            accepted_layers=(),
            accepted_chain_digest=_accepted_chain_digest(),
            acceptance=_acceptance(),
        )


def test_shot_ledger_v2_rejects_stale_nested_and_index_digests() -> None:
    raw_layer = deepcopy(_ledger(with_acceptance=False).as_dict())
    raw_layer["accepted_layers"][0]["composed_script_sha256"] = _digest("0")
    with pytest.raises(ValueError, match="accepted_layer_digest is stale"):
        ShotLedgerV2.from_dict(raw_layer)

    ledger = _ledger(with_acceptance=False)
    changed_layer = replace(
        ledger.accepted_layers[0],
        composed_script_sha256=_digest("0"),
    )
    raw_index = deepcopy(ledger.as_dict())
    raw_index["accepted_layers"][0] = changed_layer.as_dict()
    with pytest.raises(ValueError, match="index_digest is stale"):
        ShotLedgerV2.from_dict(raw_index)


def test_shot_ledger_v2_rejects_stale_selection_with_unchanged_index_digest() -> None:
    raw = deepcopy(_ledger(with_acceptance=False).as_dict())
    raw["selection_token"]["plan_pointer_sha256"] = _digest("0")

    with pytest.raises(ValueError, match="index_digest is stale"):
        ShotLedgerV2.from_dict(raw)


def test_shot_ledger_v2_rejects_stale_accepted_chain_with_unchanged_index_digest() -> None:
    raw = deepcopy(_ledger(with_acceptance=False).as_dict())
    raw["accepted_chain_digest"] = _digest("6")

    with pytest.raises(ValueError, match="index_digest is stale"):
        ShotLedgerV2.from_dict(raw)


def test_shot_ledger_v2_rejects_stale_acceptance_binding_digest() -> None:
    raw = deepcopy(_ledger().as_dict())
    raw["acceptance"]["moment_evidence"][0]["render_sha256"] = _digest("0")

    with pytest.raises(ValueError, match="binding_digest is stale"):
        ShotLedgerV2.from_dict(raw)
