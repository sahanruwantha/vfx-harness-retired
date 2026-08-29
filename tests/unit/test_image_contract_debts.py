"""Compiled unpaid image-contract debts (HIR-0048)."""

from __future__ import annotations

import inspect

from vfx_harness.domain.image_debts import (
    UNPAID_IMAGE_DEBT,
    UNPAID_IMAGE_DEBT_AUTHORITY,
    UNSATISFIABLE_PAIR_AUTHORITY,
    classify_cannot_express,
    conflict_authority,
    freeze_refusal,
    image_contract_debt_cards,
    normalize_evidence_id,
    reject_proposed_image_check,
    unpaid_image_contract_debts,
)
from vfx_harness.domain.work_units import WorkUnit


def _look_unit(
    *,
    cid: str = "form-look-f40",
    frame: int = 40,
    axis: str = "form",
    property_kind: str = "render_region_stat",
) -> WorkUnit:
    look = {
        "id": "claim.look",
        "proposition": "the plate matches the owned look",
        "axis": axis,
        "property": property_kind,
        "subject_roles": ["look.primary"],
        "subject_controls": [],
        "moments": [frame],
        "kind": "atomic",
        "required": True,
        "authority": "executable_required",
        "repair_owner": "form",
        "asserts": "image",
        "evidence": [{"kind": "image_contract", "id": cid}],
    }
    scene = {
        "id": "claim.form",
        "proposition": "form exists",
        "axis": axis,
        "property": "state.form",
        "subject_roles": ["look.primary"],
        "subject_controls": [],
        "moments": [frame],
        "kind": "atomic",
        "required": True,
        "authority": "executable_required",
        "repair_owner": "form",
        "asserts": "scene",
        "evidence": [{"kind": "scene_contract", "id": "contract.form"}],
    }
    return WorkUnit.parse(
        {
            "id": "form",
            "title": "form",
            "plan": "plans/units/form.md",
            "depends_on": [],
            "mutates": {
                "mode": "scoped",
                "roles": ["look.primary"],
                "controls": [],
                "script_spans": ["build/units/01/form.py"],
            },
            "protects": {
                "selector": "all_active_upstream_interfaces",
                "resolve_to_explicit_ids_at": "freeze",
            },
            "evaluation": {
                "primary_judge": frame,
                "judge": [{"frame": frame, "ref": "refs/f040.png"}],
                "temporal_evidence": "none",
                "claims": [scene, look],
            },
            "completion": "all_required_claims_and_protected_contracts_pass",
            "look_capabilities": ["material"],
        },
        "unit.form",
    )


def test_compiler_emits_one_card_per_image_contract_moment() -> None:
    cards = image_contract_debt_cards(_look_unit())
    assert len(cards) == 1
    card = cards[0]
    assert card.id == "form-look-f40"
    assert card.frame == 40
    assert card.property == "render_region_stat"
    assert card.axis == "form"


def test_normalize_strips_check_prefix() -> None:
    assert normalize_evidence_id("check:form-look-f40") == "form-look-f40"
    assert normalize_evidence_id("form-look-f40") == "form-look-f40"


def test_reject_wrong_id_names_requested_and_owed() -> None:
    cards = image_contract_debt_cards(_look_unit())
    message = reject_proposed_image_check(
        {"id": "other-look", "frame": 40, "axis": "form", "metric": "region_mean"},
        cards,
        unpaid=cards,
    )
    assert message is not None
    assert "other-look" in message
    assert "form-look-f40" in message


def test_reject_owed_id_wrong_frame_names_both() -> None:
    """An f150 measurement cannot pay an f72 debt (HIR-0015)."""
    unit = _look_unit(cid="materials-energy-look-f72", frame=72)
    cards = image_contract_debt_cards(unit)
    message = reject_proposed_image_check(
        {
            "id": "materials-energy-look-f72",
            "frame": 150,
            "axis": "form",
            "metric": "region_mean",
        },
        cards,
        unpaid=cards,
    )
    assert message is not None
    assert "150" in message
    assert "72" in message
    assert "materials-energy-look-f72" in message


def test_reject_strips_check_prefix_before_compare() -> None:
    cards = image_contract_debt_cards(_look_unit())
    message = reject_proposed_image_check(
        {"id": "check:form-look-f40", "frame": 40, "axis": "form", "metric": "region_mean"},
        cards,
        unpaid=cards,
    )
    assert message is None


def test_unpaid_until_coherent_row() -> None:
    cards = image_contract_debt_cards(_look_unit())
    wrong_frame = {
        "id": "form-look-f40",
        "frame": 12,
        "axis": "form",
        "metric": "region_mean",
        "origin": "builder",
    }
    paid = {
        "id": "form-look-f40",
        "frame": 40,
        "axis": "form",
        "metric": "region_mean",
        "origin": "builder",
    }
    assert unpaid_image_contract_debts(cards, [wrong_frame]) == cards
    assert unpaid_image_contract_debts(cards, [paid]) == ()


def test_freeze_refuses_unpaid_without_abstention() -> None:
    cards = image_contract_debt_cards(_look_unit())
    refusal = freeze_refusal(cards, None)
    assert refusal is not None
    assert refusal["ids"] == ["form-look-f40"]
    assert refusal["classification"] == UNPAID_IMAGE_DEBT
    assert "propose_checks" in refusal["reason"]


def test_freeze_allows_paid_row() -> None:
    assert freeze_refusal((), None) is None


def test_freeze_allows_typed_unpaid_abstention() -> None:
    cards = image_contract_debt_cards(_look_unit())
    refusal = freeze_refusal(
        cards,
        {
            "classification": UNPAID_IMAGE_DEBT,
            "contract_ids": ["check:form-look-f40"],
            "reason": "no honest adversary on this plate",
        },
    )
    assert refusal is None


def test_conflict_authority_is_not_interpolation_on_image_debt() -> None:
    assert "interpolation" not in conflict_authority(UNPAID_IMAGE_DEBT)
    assert conflict_authority(UNPAID_IMAGE_DEBT) == UNPAID_IMAGE_DEBT_AUTHORITY
    assert conflict_authority("unsatisfiable_in_scope") == UNSATISFIABLE_PAIR_AUTHORITY
    assert "interpolation" in UNSATISFIABLE_PAIR_AUTHORITY


def test_classify_cannot_express_uses_bare_debt_ids() -> None:
    cards = image_contract_debt_cards(_look_unit())
    assert classify_cannot_express(["check:form-look-f40"], cards) == UNPAID_IMAGE_DEBT
    assert classify_cannot_express(["haze-shaft-gradient-link"], cards) == (
        "unsatisfiable_in_scope"
    )


def test_build_unit_freeze_gate_is_wired() -> None:
    from vfx_harness.agents import builder

    source = inspect.getsource(builder.build_unit)
    assert "freeze_refusal" in source
    assert "skip_canonical" in source


def test_hf_authority_is_compiled_not_interpolation_copy() -> None:
    from vfx_harness.agents import builder

    source = inspect.getsource(builder._record_unsatisfiable_pair_falsification)
    assert "conflict_authority" in source
    assert "builders cannot invent a third interpolation" not in source


def test_live_probe_uses_scene_ids_not_image_debts() -> None:
    from vfx_harness.agents.builder import _unit_scene_evidence_ids

    ids = _unit_scene_evidence_ids(_look_unit())
    assert ids == {"contract.form"}


def test_new_region_metric_pays_render_region_stat_without_a_copied_list() -> None:
    from vfx_harness.domain.image_debts import metric_matches_property

    assert metric_matches_property("region_mean_r", "render_region_stat")
    assert not metric_matches_property("frame_mean", "render_region_stat")


def test_frame_scalar_pays_frame_delta_proved_against_adversary() -> None:
    from vfx_harness.domain.image_debts import metric_matches_property

    assert metric_matches_property("frame_mean", "frame_delta")
    assert metric_matches_property("frame_black_pct", "frame_delta")
    assert not metric_matches_property("region_mean", "frame_delta")


def test_property_mismatch_enumerates_compatible_metrics() -> None:
    unit = _look_unit(
        cid="atmoscale-f150",
        frame=150,
        axis="atmosphere",
        property_kind="frame_delta",
    )
    cards = image_contract_debt_cards(unit)
    message = reject_proposed_image_check(
        {
            "id": "atmoscale-f150",
            "frame": 150,
            "axis": "atmosphere",
            "metric": "region_mean",
        },
        cards,
        unpaid=cards,
        registry={"frame_mean", "frame_black_pct", "region_mean"},
    )
    assert message is not None
    assert "Compatible metrics: frame_black_pct, frame_mean" in message
    assert "region_mean" not in message.split("Compatible metrics:", 1)[1]
