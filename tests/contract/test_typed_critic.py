from __future__ import annotations

from vfx_harness.agents.builder import (
    _aggregate_critic_panel,
    _audit_panel_citations,
    _critic_schema,
    _filter_critic_issues,
    _needs_critic_panel,
)


def _observation(**updates):
    row = {
        "id": "rib-floor-contact",
        "kind": "measurable",
        "axis": "layout",
        "property": "floor_contact",
        "observation": "The rib feet terminate above the floor.",
        "action": "Extend the rib feet to the floor plane.",
        "moment": 1,
        "roles": ["architecture.rib.*"],
        "claim_id": "layout.rib_floor_contact",
        "check_ids": [],
        "panel_ids": [],
    }
    row.update(updates)
    return row


def test_critic_schema_requires_typed_observations_only():
    schema = _critic_schema([("layout", "layout")], allow_na=False, focus_frames=[1])
    assert "observations" in schema["required"]
    assert "issues" not in schema["properties"]
    assert "issue_evidence" not in schema["properties"]
    required = set(schema["properties"]["observations"]["items"]["required"])
    assert {"property", "moment", "roles", "claim_id", "check_ids", "panel_ids"} <= required


def test_uncovered_measurable_observation_is_contract_gap():
    verdict = _filter_critic_issues(
        {"pass": False, "observations": [_observation(claim_id=None)]},
        [],
        claim_bindings={"layout.rib_floor_contact": frozenset({"rib-floor-gap"})},
    )
    assert verdict["contract_gap"]
    assert not verdict["issues"]
    assert not verdict.get("judge_conflict")
    assert not _needs_critic_panel({**verdict, "scores": {"layout": 2}, "mean": 2.0})


def test_only_directly_bound_passing_evidence_contradicts():
    verdict = _filter_critic_issues(
        {
            "pass": False,
            "observations": [_observation(check_ids=["rib-floor-gap"])],
        },
        [{"id": "rib-floor-gap", "authoritative": True, "pass": True}],
        claim_bindings={"layout.rib_floor_contact": frozenset({"rib-floor-gap"})},
    )
    assert verdict["judge_conflict"]
    assert len(verdict["contradicted_issues"]) == 1
    assert not verdict["contract_gap"]


def test_unknown_claim_is_protocol_error_not_repair_authority():
    verdict = _filter_critic_issues(
        {"pass": False, "observations": [_observation(claim_id="invented.claim")]},
        [],
        claim_bindings={"layout.rib_floor_contact": frozenset()},
    )
    assert verdict["judge_conflict"]
    assert verdict["protocol_errors"][0]["reason"] == "names unknown claim invented.claim"
    assert not verdict["issues"]


def test_panel_audit_removes_unseen_panel_ids_from_observation():
    verdict = _audit_panel_citations(
        {"observations": [_observation(panel_ids=["shown", "invented"])]},
        [{"id": "shown"}],
    )
    assert verdict["observations"][0]["panel_ids"] == ["shown"]
    assert verdict["invalid_panel_citations"][0]["panel_ids"] == ["invented"]


def test_bare_panel_majority_cannot_erase_actionable_dissent():
    observation = _observation(kind="qualitative", check_ids=[])
    dissent = _filter_critic_issues(
        {
            "pass": False,
            "mean": 2.5,
            "scores": {"form": 2, "site": 3},
            "observations": [observation],
        },
        [],
        claim_bindings={"layout.rib_floor_contact": frozenset()},
        qualified_claims={"layout.rib_floor_contact"},
    )
    panel = [
        dissent,
        {"pass": True, "mean": 3.0, "scores": {"form": 3, "site": 3}},
        {"pass": True, "mean": 3.0, "scores": {"form": 3, "site": 3}},
    ]

    verdict = _aggregate_critic_panel(panel)

    assert verdict["pass"] is False
    assert verdict["decided_by"] == "actionable_panel_dissent"
    assert verdict["issues"] == [observation["action"]]
    assert verdict["observation_reconciliation"][0]["state"] == "actionable"
    assert verdict["panel"] == [
        {"mean": 2.5, "pass": False},
        {"mean": 3.0, "pass": True},
        {"mean": 3.0, "pass": True},
    ]


def test_contradicted_dissent_does_not_block_panel_majority():
    dissent = {
        "pass": False,
        "mean": 2.0,
        "scores": {"form": 2},
        "issues": [],
        "judge_conflict": True,
        "contradicted_issues": [{"issue": "already disproved"}],
    }
    verdict = _aggregate_critic_panel(
        [
            dissent,
            {"pass": True, "mean": 3.0, "scores": {"form": 3}},
            {"pass": True, "mean": 3.0, "scores": {"form": 3}},
        ]
    )

    assert verdict["pass"] is True
    assert "actionable_dissent" not in verdict
