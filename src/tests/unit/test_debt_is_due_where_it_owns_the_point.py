"""A judgment debt owes an observation only at the points it owns (HIR-0206).

caesar run 20260904T143311Z-c0f282, layer 2 composed finalization. Three judge frames,
one group, one judgment debt owning frame 1. The ledger shows all three frames judged:

    round 0  f1    provisional_requirement_contract_gap  2.0  fail   critic ran
    round 1  f121  unit_executable_evidence              5.0  pass   no vision call
    round 2  f301  unit_executable_evidence              5.0  pass   no vision call

``JudgmentDebtPayment.prepare`` returns None at a point the debt does not own, so no
observation is compiled at f121 or f301 -- correctly, per HIR-0163. The evaluation
receipt demanded one at every qualitative row from the layer-level ``debt_id`` and died
with "judgment_observation has unsupported shape", killing the process before the
critic's finding could reach a receipt.
"""

from __future__ import annotations

import pytest

from vfx_harness.domain.layer_replay_observations import (
    LayerReplayClaimRequirement,
    LayerReplayEvaluationGroupPlan,
)

REF = "refs/frame_63s.jpg"
POINTS = ((1, REF), (121, "refs/frame_67s.jpg"), (301, "refs/frame_73s.jpg"))
DEBT = "jd-718f8516b4f69a122a5602d478635067ce986eb981dedbe525e6fc852de2412d"


def _digest(seed: str) -> str:
    import hashlib

    return hashlib.sha256(seed.encode()).hexdigest()


def _plan(*, debt_points, debt_id: str | None = DEBT) -> LayerReplayEvaluationGroupPlan:
    paid = debt_id is not None
    return LayerReplayEvaluationGroupPlan(
        group_index=0,
        planned_group_count=1,
        requirement_ids=("R24",),
        debt_id=debt_id,
        debt_points=debt_points,
        definition_digest=_digest("definition") if paid else None,
        activation_digest=_digest("activation") if paid else None,
        payment_generation_digest=_digest("generation") if paid else None,
        judge_points=POINTS,
        axes=("chamber_readability_and_containment",),
        claims=(
            LayerReplayClaimRequirement(
                claim_id="composed-look",
                authority="qualified_qualitative_required",
                judge_frames=(1, 121, 301),
                evidence_ids=(),
            ),
        ),
        evidence_kind="render",
        render_mode="eevee",
        render_scale=1.0,
    )


def test_a_debt_may_own_fewer_points_than_the_group_judges() -> None:
    """The exact shipped shape: three judge points, one owned by the debt."""
    plan = _plan(debt_points=((1, REF),))

    assert plan.debt_points == ((1, REF),)
    assert plan.judge_points == POINTS
    assert plan.as_dict()["debt_points"] == [[1, REF]]
    assert LayerReplayEvaluationGroupPlan.from_dict(plan.as_dict(), "plan") == plan


def test_a_debt_point_must_be_a_judge_point_of_this_group() -> None:
    with pytest.raises(ValueError, match="must be judge points of this group"):
        _plan(debt_points=((999, "refs/elsewhere.png"),))


def test_a_debt_owns_at_least_one_point_and_a_debtless_group_owns_none() -> None:
    """Absence fails closed: a debt that owns nothing could never be demanded."""
    with pytest.raises(ValueError, match="must own at least one judge point"):
        _plan(debt_points=())
    with pytest.raises(ValueError, match="require a judgment debt on this group"):
        _plan(debt_points=((1, REF),), debt_id=None)
    assert _plan(debt_points=(), debt_id=None).debt_points == ()


def test_duplicate_and_malformed_debt_points_are_refused() -> None:
    with pytest.raises(ValueError, match="debt_points contains duplicates"):
        _plan(debt_points=((1, REF), (1, REF)))
    with pytest.raises(ValueError, match=r"debt_points\[\] must each be"):
        _plan(debt_points=((1,),))


def test_a_receipt_sealed_before_this_field_still_reads(  # HIR-0207
) -> None:
    """A stored plan predating debt_points is read as what it actually proved.

    Such a receipt minted under a reader that demanded an observation at every judge
    point, so a debt there owned all of them. Deriving that is not interpretation; it is
    the only reading consistent with the receipt having minted at all.
    """
    payload = _plan(debt_points=((1, REF),)).as_dict()
    del payload["debt_points"]
    assert LayerReplayEvaluationGroupPlan.from_dict(payload, "plan").debt_points == POINTS

    debtless = _plan(debt_points=(), debt_id=None).as_dict()
    del debtless["debt_points"]
    assert LayerReplayEvaluationGroupPlan.from_dict(debtless, "plan").debt_points == ()

    # A four-key claim row is the shape every receipt sealed before HIR-0204 carries.
    claim = {
        "claim_id": "composed-look",
        "authority": "qualified_qualitative_required",
        "judge_frames": [1, 121, 301],
        "evidence_ids": [],
    }
    parsed = LayerReplayClaimRequirement.from_dict(claim, "claims[0]")
    assert parsed.evidence_frames == ()

    # A missing REQUIRED key is still refused, and the refusal names what it needs.
    with pytest.raises(ValueError, match="are required"):
        LayerReplayClaimRequirement.from_dict(
            {k: v for k, v in claim.items() if k != "judge_frames"}, "claims[0]"
        )
    with pytest.raises(ValueError, match="are required"):
        LayerReplayEvaluationGroupPlan.from_dict(
            {k: v for k, v in payload.items() if k != "judge_points"}, "plan"
        )


def test_a_point_the_debt_does_not_own_mints_without_an_observation() -> None:
    """The exact crash: f121 is judged, the debt owns f1, mint demanded an observation."""
    from types import SimpleNamespace

    from vfx_harness.domain import layer_evaluation_receipts as receipts

    plan = _plan(debt_points=((1, REF),))
    unowned = SimpleNamespace(
        frame=121,
        ref="refs/frame_67s.jpg",
        deterministic_status="passed",
        render="renders/f121.png",
        render_capture={"png_sha256": _digest("f121")},
        evidence=(),
        missing_evidence_ids=(),
    )
    verdict = {
        "evidence_kind": "render",
        "pass": True,
        "issues": [],
        "evidence": [],
        "evidence_failures": [],
        "missing_evidence": [],
        "decided_by": "unit_executable_evidence",
        "scores": {"chamber_readability_and_containment": 5.0},
        "mean": 5.0,
        "render": unowned.render,
        "render_capture": unowned.render_capture,
        "layer_replay_receipt_digest": _digest("replay"),
    }
    def _row(plan_, point_, verdict_):
        replay = SimpleNamespace(
            observation=SimpleNamespace(plan=plan_, auxiliary_captures=()),
            receipt_digest=_digest("replay"),
        )
        return receipts._canonical_row(
            {"frame": point_.frame, "ref": point_.ref, "verdict": verdict_},
            "canonical[1]",
            replay_receipt=replay,
            point=point_,
        )

    assert _row(plan, unowned, dict(verdict))["verdict"]["mean"] == 5.0

    # An observation where the debt owns nothing means the two sides disagree.
    stray = {**verdict, "judgment_observation": {"request": {}, "candidate_capture": None, "reused_attempt": None}}
    with pytest.raises(ValueError, match="does not own"):
        _row(plan, unowned, stray)

    # At a point the debt DOES own, a missing observation still fails closed, and the
    # refusal says what it expected instead of "unsupported shape".
    owned = SimpleNamespace(
        frame=1,
        ref=REF,
        deterministic_status="passed",
        render="renders/f1.png",
        render_capture={"png_sha256": _digest("f1")},
        evidence=(),
        missing_evidence_ids=(),
    )
    with pytest.raises(ValueError, match="is required at f1, which this group"):
        _row(plan, owned, {**verdict, "render": owned.render, "render_capture": owned.render_capture})
