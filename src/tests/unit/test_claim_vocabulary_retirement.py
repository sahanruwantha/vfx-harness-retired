"""human_required claims are retired with a teaching rejection (HIR-0174).

Run 20260902T165518Z-004470: an executable-only unit carried a human_required claim that
no runtime producer could pay; it forced raster rounds and a revision session on a black
plate. The human domain is judgment debt on the owning requirement.
"""

from __future__ import annotations

import pytest

from vfx_harness.domain.work_units.claims import Claim
from vfx_harness.domain.work_units.parsing import CLAIM_AUTHORITIES, EVIDENCE_KINDS


def _row(**overrides) -> dict:
    row = {
        "id": "massing-isolation",
        "proposition": "the corner mass reads as one isolated block",
        "axis": "structural_scope",
        "property": "isolation",
        "subject_roles": ["exterior.corner"],
        "moments": [38],
        "kind": "atomic",
        "required": True,
        "authority": "executable_required",
        "repair_owner": "exterior_massing",
        "evidence": [{"kind": "scene_contract", "id": "corner-tower-vertex-count"}],
        "asserts": "scene",
    }
    row.update(overrides)
    return row


def test_vocabulary_no_longer_admits_human_claims() -> None:
    assert "human_required" not in CLAIM_AUTHORITIES
    assert "human_decision" not in EVIDENCE_KINDS
    assert Claim.parse(_row(), "claim").authority == "executable_required"


def test_human_required_authority_is_refused_with_the_judgment_debt_path() -> None:
    with pytest.raises(ValueError, match="retired") as exc:
        Claim.parse(_row(authority="human_required"), "claim")
    message = str(exc.value)
    assert "no runtime producer pays a human decision" in message
    assert "approved_start / planner_start judgment debt" in message
    assert "qualified_qualitative_required" in message


def test_human_decision_evidence_is_refused_with_the_judgment_debt_path() -> None:
    with pytest.raises(ValueError, match="retired") as exc:
        Claim.parse(
            _row(evidence=[{"kind": "human_decision", "id": "massing-isolation-decision"}]),
            "claim",
        )
    assert "judgment debt on the owning requirement" in str(exc.value)
