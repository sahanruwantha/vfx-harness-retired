"""A mutated role without a required claim is refused at the stage call (HIR-0177).

Run 20260903T023810Z-8509f9 staged ``camera_targets`` cleanly and learned at its first
``finalize_materialization`` that ``camera.targets`` had no required claim. The predicate is
unit-local, so the staging transaction applies the same one claim closure uses.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.unit.test_plan_records import (
    _add_deferred_layer,
    _base_selection,
    _candidate,
    _jit_payload,
    publish_current,
)
from vfx_harness.domain.work_units.graph import MUTATION_CLAIM_COVERAGE_RULE, uncovered_mutation_roles
from vfx_harness.observability import run_artifacts
from vfx_harness.orchestration.jit_materialization import (
    seed_materialization_candidate,
    stage_materialization_unit,
)


def _unit(mutated: list[str], judged: list[str], *, required: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        mutates=SimpleNamespace(roles=tuple(mutated)),
        evaluation=SimpleNamespace(
            claims=(SimpleNamespace(required=required, subject_roles=tuple(judged)),)
        ),
    )


def test_predicate_matches_claim_closure_semantics() -> None:
    assert uncovered_mutation_roles(_unit(["camera.rig"], ["camera.rig"])) == ()
    assert uncovered_mutation_roles(_unit(["camera.rig"], ["camera.*"])) == ()
    assert uncovered_mutation_roles(_unit(["camera.*"], ["camera.rig"])) == ()
    assert uncovered_mutation_roles(_unit(["camera.targets"], ["camera.rig"])) == ("camera.targets",)
    # An advisory claim judges nothing a mutation can rely on.
    assert uncovered_mutation_roles(_unit(["camera.rig"], ["camera.rig"], required=False)) == ("camera.rig",)


def test_stage_call_refuses_an_uncovered_mutation_role(tmp_path: Path) -> None:
    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    layout = run_artifacts.create(tmp_path, "stage-coverage")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    full = json.loads(_jit_payload(tmp_path, bundle.content_hash).read_text(encoding="utf-8"))
    target = tmp_path / "stage-coverage.json"
    seed_materialization_candidate(
        bundle.root,
        target,
        layer_id="2",
        bundle_hash=bundle.content_hash,
        base_selection=_base_selection(tmp_path),
    )
    unit = json.loads(json.dumps(full["layer"]["stages"][0]))
    unit["mutates"]["roles"] = [*unit["mutates"]["roles"], "unjudged.role"]
    before = target.read_bytes()
    with pytest.raises(ValueError) as refused:
        stage_materialization_unit(
            target,
            unit=unit,
            scene_contracts=full["scene_contracts"],
            requirement_bindings=full["requirement_bindings"],
        )
    message = str(refused.value)
    assert message.startswith("staging refused before candidate write")
    assert "required-claim coverage:" in message
    assert f"unit {unit['id']} mutates ['unjudged.role'] without a required claim" in message
    assert "required claim subject_roles on this unit:" in message
    assert MUTATION_CLAIM_COVERAGE_RULE in message
    assert target.read_bytes() == before

    stage_materialization_unit(
        target,
        unit=full["layer"]["stages"][0],
        scene_contracts=full["scene_contracts"],
        requirement_bindings=full["requirement_bindings"],
    )
    assert target.read_bytes() != before


def _derivative(id: str, op: str, frames: list[int], **bound) -> dict:
    return {
        "id": id,
        "kind": "curve_derivative_max",
        "roles": ["polish.hero"],
        "property": "location",
        "op": op,
        "frames": frames,
        "owner_layer": "2",
        "fault_owner": "2",
        "activates_at": "2",
        "lifecycle": "layer",
        "axis": "polish",
        **bound,
    }


def test_stage_call_refuses_a_cross_row_contradiction(tmp_path: Path) -> None:
    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    layout = run_artifacts.create(tmp_path, "stage-row-set")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    full = json.loads(_jit_payload(tmp_path, bundle.content_hash).read_text(encoding="utf-8"))
    target = tmp_path / "stage-row-set.json"
    seed_materialization_candidate(
        bundle.root,
        target,
        layer_id="2",
        bundle_hash=bundle.content_hash,
        base_selection=_base_selection(tmp_path),
    )
    before = target.read_bytes()
    contradictory = [
        *full["scene_contracts"],
        _derivative("cap", "max", [1, 213], hi=0.06),
        _derivative("floor", "min", [113, 175], lo=0.12),
    ]
    with pytest.raises(ValueError) as refused:
        stage_materialization_unit(
            target,
            unit=full["layer"]["stages"][0],
            scene_contracts=contradictory,
            requirement_bindings=full["requirement_bindings"],
        )
    message = str(refused.value)
    assert message.startswith("staging refused before candidate write")
    assert "cross-row contradiction:" in message
    assert "floor: lo 0.12 over frames 113..175 can never satisfy cap: hi 0.06" in message
    assert target.read_bytes() == before


def test_a_refused_stage_reports_every_gate_and_the_transaction_outcome(tmp_path: Path) -> None:
    """Five gates over five turns for one camera unit is the cost of returning alone.

    Room run 20260904T143607Z-565c1e placed one unit in nine stage calls and eight refusals,
    each naming a different rule, while the collectable validator in the same session
    returned four and seven findings at once. The pre-write gates now do the same, and say
    that nothing was staged so the materializer stops probing with unstage (HIR-0201).
    """
    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    layout = run_artifacts.create(tmp_path, "stage-collected")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    full = json.loads(_jit_payload(tmp_path, bundle.content_hash).read_text(encoding="utf-8"))
    target = tmp_path / "stage-collected.json"
    seed_materialization_candidate(
        bundle.root,
        target,
        layer_id="2",
        bundle_hash=bundle.content_hash,
        base_selection=_base_selection(tmp_path),
    )
    before = target.read_bytes()
    unit = deepcopy(full["layer"]["stages"][0])
    unit["mutates"]["roles"] = [*unit["mutates"]["roles"], "unjudged.role"]
    contradictory = [
        *full["scene_contracts"],
        _derivative("cap", "max", [1, 213], hi=0.06),
        _derivative("floor", "min", [113, 175], lo=0.12),
    ]

    with pytest.raises(ValueError) as refused:
        stage_materialization_unit(
            target,
            unit=unit,
            scene_contracts=contradictory,
            requirement_bindings=full["requirement_bindings"],
        )

    message = str(refused.value)
    assert message.startswith("staging refused before candidate write; 3 unit-local finding(s)")
    assert "nothing was staged and the candidate is unchanged" in message, (
        "the materializer probed with unstage three times because no refusal said this"
    )
    # Three different gates, one call: coverage and the contradiction the candidate carries,
    # plus the atomicity consequence of the same edit that fail-fast would have billed on a
    # later turn.
    assert "  1. required-claim coverage:" in message
    assert "  2. cross-row contradiction:" in message
    assert "  3. unit atomicity:" in message
    assert MUTATION_CLAIM_COVERAGE_RULE in message
    assert "floor: lo 0.12 over frames 113..175 can never satisfy cap: hi 0.06" in message
    assert "mixed_clusters" in message
    assert target.read_bytes() == before
