"""A required image contract is paid by the only mechanism that can pay it (HIR-0205).

hansa run 20260904T143358Z-238376, unit ``hero_facade``: the canonical evaluator
produced all three bound image rows and every one passed — 39.0413, 41.5796, 54.409 —
and the unit-outcome receipt raised "canonical evaluator did not produce passing
required evidence" about rows it had just produced. The rows carried
``authoritative: false`` because ``origin`` is ``builder``, which ``evidence/checks.py``
sets deliberately, and materialization mints required ``image_contract`` ids that only a
builder payment can discharge. Every consumer that closed over a unit's REQUIRED
bindings read that autonomy flag, so no unit with a required image claim could publish.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from vfx_harness.agents.builder.verdicts import _executable_unit_verdict
from vfx_harness.domain import evidence_authority
from vfx_harness.domain.work_units.claims import Claim, EvidenceBinding
from vfx_harness.evidence.claim_evidence import Observation, reconcile_observation
from vfx_harness.orchestration import revalidation
from vfx_harness.orchestration.unit_evaluation_receipts import (
    UnitEvaluationConflict,
    _passing_evidence,
)

FRAME = 51


def _claim(*bindings: EvidenceBinding, asserts: str = "image") -> Claim:
    return Claim(
        id="hero-look",
        proposition="The facade reads as weathered stone.",
        axis="hero_tower",
        property="region_mean",
        subject_roles=("hero.facade",),
        subject_controls=(),
        moments=(FRAME,),
        kind="atomic",
        required=True,
        authority="executable_required",
        repair_owner="hero_facade",
        evidence=bindings,
        asserts=asserts,
    )


def _unit(*bindings: EvidenceBinding, look: tuple[str, ...] = ("surface",)) -> SimpleNamespace:
    return SimpleNamespace(
        id="hero_facade",
        look_capabilities=look,
        evaluation=SimpleNamespace(
            claims=(_claim(*bindings),),
            judges=(SimpleNamespace(frame=FRAME),),
        ),
    )


def _builder_image_row(*, passes: bool = True, cid: str = "hero-look-f51-mean") -> dict:
    """The exact row shape hansa emitted, verbatim in the fields that decided it."""
    return {
        "id": cid,
        "source": "image_contract",
        "origin": "builder",
        "metric": "region_mean",
        "target": "20..65",
        "value": 41.5796,
        "pass": passes,
        "authoritative": False,
        "axis": "hero_tower",
        "owner_layer": "2",
        "fault_owner": "2",
        "activates_at": "2",
        "lifecycle": "layer",
    }


def _canonical(*rows: dict) -> list[dict]:
    return [{"frame": FRAME, "ref": "refs/a.png", "verdict": {"pass": True, "evidence": list(rows)}}]


def test_a_builder_paid_image_row_settles_the_claim_that_binds_it() -> None:
    """The shipped defect: three passing rows discarded, then reported as not produced."""
    unit = _unit(EvidenceBinding("image_contract", "hero-look-f51-mean"))

    passed = _passing_evidence(unit, "2", _canonical(_builder_image_row()))

    assert ("image_contract", "hero-look-f51-mean") in passed
    assert ("replay", "2.hero_facade") in passed


def test_a_failing_bound_row_still_refuses_and_says_it_was_produced() -> None:
    unit = _unit(EvidenceBinding("image_contract", "hero-look-f51-mean"))

    with pytest.raises(UnitEvaluationConflict) as excinfo:
        _passing_evidence(unit, "2", _canonical(_builder_image_row(passes=False)))

    message = str(excinfo.value)
    assert "produced by the canonical evaluator but not passing" in message
    assert "image_contract:hero-look-f51-mean" in message
    assert "never produced" not in message


def test_an_absent_binding_is_reported_as_never_produced() -> None:
    """The old message said this about rows that existed; it must only say it now."""
    unit = _unit(EvidenceBinding("image_contract", "hero-look-f51-mean"))

    with pytest.raises(UnitEvaluationConflict) as excinfo:
        _passing_evidence(unit, "2", _canonical(_builder_image_row(cid="some-other-row")))

    message = str(excinfo.value)
    assert "never produced by the canonical evaluator" in message
    assert "image_contract:hero-look-f51-mean" in message
    assert "hero_facade" in message


def test_the_live_verdict_and_the_receipt_agree_on_what_satisfies_a_binding() -> None:
    """The two consumers of one required closure disagreed for a whole shot's spend.

    The builder sealed: probe_candidate reported every row passing. The receipt then
    refused the same rows. Whatever satisfies a binding live must satisfy it durably.
    """
    unit = _unit(EvidenceBinding("image_contract", "hero-look-f51-mean"))
    rows = [_builder_image_row()]

    live = _executable_unit_verdict(unit, FRAME, [("hero_tower", "region_mean")], rows)

    assert live is not None and live["pass"] is True, live
    # Same unit, same rows: the durable receipt must not contradict the live verdict.
    assert ("image_contract", "hero-look-f51-mean") in _passing_evidence(unit, "2", _canonical(*rows))


def test_a_critic_cannot_override_a_passing_bound_image_measurement() -> None:
    """AGENTS.md: critics never override a passing measurement of the same fact."""
    observation = Observation(
        id="o1",
        kind="measurable",
        axis="hero_tower",
        property="region_mean",
        observation="the facade reads too dark",
        action="raise the base albedo",
        moment=FRAME,
        roles=("hero.facade",),
        claim_id="hero-look",
        check_ids=("hero-look-f51-mean",),
    )
    bindings = {"hero-look": {"hero-look-f51-mean"}}

    reconciled = reconcile_observation(
        observation, [_builder_image_row()], claim_bindings=bindings
    )
    assert reconciled["state"] == "contradicted"

    # A failing bound row supports the critic instead of vanishing into a contract gap.
    failing = reconcile_observation(
        observation, [_builder_image_row(passes=False)], claim_bindings=bindings
    )
    assert failing["state"] == "actionable"


def test_an_unbound_builder_row_still_has_no_autonomous_authority() -> None:
    """The anti-marking-your-own-homework rule is unchanged where it applies."""
    row = _builder_image_row()

    assert evidence_authority.blocks_without_a_binding(row) is False
    assert evidence_authority.settles_bound_requirement(row) is True

    # Cited for a claim that does not bind it: still not authority, and not a
    # contradiction of the critic.
    observation = Observation(
        id="o2",
        kind="measurable",
        axis="hero_tower",
        property="region_mean",
        observation="the facade reads too dark",
        action="raise the base albedo",
        moment=FRAME,
        roles=("hero.facade",),
        claim_id=None,
        check_ids=("hero-look-f51-mean",),
    )
    assert reconcile_observation(observation, [row])["state"] == "contract_gap"


def test_the_sealed_record_keeps_the_bound_image_evidence() -> None:
    """A look unit's outcome sealed a record with no image rows in it."""
    projection = revalidation._authoritative_projection(
        {"evidence": [_builder_image_row(), {"id": "vis-f51", "source": "interface_contract",
                                             "pass": True, "authoritative": True}]}
    )

    assert [row["id"] for row in projection] == ["hero-look-f51-mean", "vis-f51"]


def test_an_autonomous_row_without_a_source_still_decides_its_bound_claim() -> None:
    """Autonomy is sufficient, never necessary: nothing that decided before stops."""
    row = {"id": "rib-floor-gap", "authoritative": True, "pass": True}

    assert evidence_authority.is_typed_measurement(row) is False
    assert evidence_authority.is_recorded_evidence(row) is True

    observation = Observation(
        id="o3",
        kind="measurable",
        axis="layout",
        property="gap",
        observation="the rib floats above the floor",
        action="lower the rib",
        moment=FRAME,
        roles=("layout.rib",),
        claim_id="layout.rib_floor_contact",
        check_ids=("rib-floor-gap",),
    )
    reconciled = reconcile_observation(
        observation, [row], claim_bindings={"layout.rib_floor_contact": {"rib-floor-gap"}}
    )
    assert reconciled["state"] == "contradicted"


def test_untyped_rows_are_not_evidence_for_anything() -> None:
    for row in ({"id": "w", "source": "builder_state", "pass": True}, {"source": "image_contract", "pass": True}, "x"):
        assert evidence_authority.settles_bound_requirement(row) is False
        assert evidence_authority.is_typed_measurement(row) is False
        assert evidence_authority.is_recorded_evidence(row) is False
