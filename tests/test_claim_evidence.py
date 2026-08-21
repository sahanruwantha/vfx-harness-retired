from __future__ import annotations

import json

import pytest

from bambi_vfx.claim_evidence import (
    Observation,
    append_gap_record,
    reconcile_observation,
    reconcile_observations,
)


def _observation(**updates):
    row = {
        "id": "rib-floor-contact",
        "kind": "measurable",
        "axis": "layout_and_architecture",
        "property": "role_floor_contact",
        "observation": "The flanking ribs terminate above the floor.",
        "action": "Extend the rib feet to meet the floor plane.",
        "moment": 1,
        "roles": ["architecture.rib.left", "architecture.rib.right"],
        "claim_id": "layout.rib_floor_contact",
        "check_ids": [],
        "panel_ids": ["rib-left", "rib-right"],
    }
    row.update(updates)
    return Observation.parse(row)


def test_missing_relevant_check_is_contract_gap_not_contradiction():
    result = reconcile_observation(
        _observation(),
        [{"id": "rib-count", "authoritative": True, "pass": True}],
        claim_bindings={"layout.rib_floor_contact": set()},
    )
    assert result["state"] == "contract_gap"


def test_directly_bound_passing_check_contradicts_measurement():
    result = reconcile_observation(
        _observation(check_ids=["rib-floor-gap"]),
        [{"id": "rib-floor-gap", "authoritative": True, "pass": True}],
        claim_bindings={"layout.rib_floor_contact": {"rib-floor-gap"}},
    )
    assert result["state"] == "contradicted"


def test_failed_bound_check_makes_observation_actionable():
    result = reconcile_observation(
        _observation(check_ids=["rib-floor-gap"]),
        [{"id": "rib-floor-gap", "authoritative": True, "pass": False}],
        claim_bindings={"layout.rib_floor_contact": {"rib-floor-gap"}},
    )
    assert result["state"] == "actionable"


def test_other_property_check_cannot_be_borrowed():
    result = reconcile_observation(
        _observation(check_ids=["rib-count"]),
        [{"id": "rib-count", "authoritative": True, "pass": True}],
        claim_bindings={"layout.rib_floor_contact": {"rib-floor-gap"}},
    )
    assert result["state"] == "protocol_error"


def test_unqualified_qualitative_residual_is_not_autonomous_blocker():
    result = reconcile_observation(_observation(kind="qualitative", property="silhouette_read"), [])
    assert result["state"] == "unverified_qualitative"


def test_gap_record_contains_hash_pinned_plan_defect(tmp_path):
    reconciled = reconcile_observations([_observation()], [])
    path = append_gap_record(
        tmp_path,
        layer="1",
        unit="architecture",
        candidate_hash="candidate-sha",
        settings_hash="settings-sha",
        rows=reconciled["observations"],
    )
    record = json.loads(path.read_text().splitlines()[0])
    assert record["classification"] == "plan_defect"
    assert record["candidate_hash"] == "candidate-sha"
    assert record["gaps"][0]["state"] == "contract_gap"


def test_gap_record_rejects_non_gap_rows(tmp_path):
    with pytest.raises(ValueError, match="contract_gap"):
        append_gap_record(
            tmp_path,
            layer="1",
            unit="architecture",
            candidate_hash="candidate-sha",
            settings_hash="settings-sha",
            rows=[],
        )
