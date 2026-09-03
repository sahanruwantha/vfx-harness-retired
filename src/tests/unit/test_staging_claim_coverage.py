"""A mutated role without a required claim is refused at the stage call (HIR-0177).

Run 20260903T023810Z-8509f9 staged ``camera_targets`` cleanly and learned at its first
``finalize_materialization`` that ``camera.targets`` had no required claim. The predicate is
unit-local, so the staging transaction applies the same one claim closure uses.
"""

from __future__ import annotations

import json
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
    assert message.startswith("required-claim coverage refused before candidate write")
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
