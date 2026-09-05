"""A stage call refuses collectable findings addressed inside its own write (HIR-0180).

Run 20260903T053305Z-83f8e1 staged five clean-looking units and learned seven per-unit
findings from finalize_materialization; the session unstaged every unit to repair them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.unit.test_plan_records import (
    _add_deferred_layer,
    _base_selection,
    _candidate,
    _jit_payload,
    publish_current,
)
from vfx_harness.domain.work_units import LOOK_REQUIRES_IMAGE_DOMAIN_RULE
from vfx_harness.observability import run_artifacts
from vfx_harness.orchestration.jit_materialization import (
    MaterializationInspection,
    seed_materialization_candidate,
    stage_materialization_unit,
    staged_write_findings,
)
from vfx_harness.orchestration.jit_materialization.staging import STAGED_WRITE_FINDING_RULE


def test_only_findings_addressed_inside_the_write_are_refused() -> None:
    before = ["/requirement_bindings: requirement R1 is not bound"]
    after = [
        "/requirement_bindings: requirement R1 is not bound",
        "/layer/stages/1/look_capabilities: unit b declares look_capabilities but frame 1 has no image claim",
        "/scene_contracts/3/id: duplicate id",
        "/requirement_bindings: requirement R7 declares AND domains ['scene'] but does not pay ['scene']",
        "/scene_contracts: every judge frame needs a visible_fraction contract; missing at frame(s): 1",
        "/layer/stages/0/mutates/roles: unit a mutates a role nothing judges",
    ]
    refused, remaining = staged_write_findings(
        before,
        after,
        unit_index=1,
        contract_indices=range(3, 4),
        binding_indices=range(2, 3),
        supplied_ids=frozenset({"c3", "R7"}),
    )
    assert refused == [
        "/layer/stages/1/look_capabilities: unit b declares look_capabilities but frame 1 has no image claim",
        "/scene_contracts/3/id: duplicate id",
        "/requirement_bindings: requirement R7 declares AND domains ['scene'] but does not pay ['scene']",
    ]
    assert remaining == [
        "/requirement_bindings: requirement R1 is not bound",
        "/scene_contracts: every judge frame needs a visible_fraction contract; missing at frame(s): 1",
        "/layer/stages/0/mutates/roles: unit a mutates a role nothing judges",
    ]
    # An id is matched as a whole token: R7 must not match R70.
    refused, _remaining = staged_write_findings(
        [],
        ["/requirement_bindings: requirement R70 is unpaid"],
        unit_index=0,
        contract_indices=range(0),
        binding_indices=range(0),
        supplied_ids=frozenset({"R7"}),
    )
    assert refused == []


def test_stage_call_runs_the_terminal_validator_on_its_own_write(tmp_path: Path) -> None:
    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    layout = run_artifacts.create(tmp_path, "staged-write-findings")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    full = json.loads(_jit_payload(tmp_path, bundle.content_hash).read_text(encoding="utf-8"))
    target = tmp_path / "staged-write-findings.json"
    seed_materialization_candidate(
        bundle.root,
        target,
        layer_id="2",
        bundle_hash=bundle.content_hash,
        base_selection=_base_selection(tmp_path),
    )
    inspection = MaterializationInspection(
        global_root=bundle.root,
        expected_bundle_hash=bundle.content_hash,
        shot_folder=bundle.root,)
    unit = json.loads(json.dumps(full["layer"]["stages"][0]))
    unit["look_capabilities"] = ["material"]
    before = target.read_bytes()
    with pytest.raises(ValueError) as refused:
        stage_materialization_unit(
            target,
            unit=unit,
            scene_contracts=full["scene_contracts"],
            requirement_bindings=full["requirement_bindings"],
            inspection=inspection,
        )
    message = str(refused.value)
    assert message.startswith("staged write refused before candidate write: /layer/stages/0/look_capabilities:")
    assert LOOK_REQUIRES_IMAGE_DOMAIN_RULE in message
    assert STAGED_WRITE_FINDING_RULE in message
    assert target.read_bytes() == before

    staged = stage_materialization_unit(
        target,
        unit=full["layer"]["stages"][0],
        scene_contracts=full["scene_contracts"],
        requirement_bindings=full["requirement_bindings"],
        inspection=inspection,
    )
    assert staged.path == target
    assert target.read_bytes() != before
    assert all(not finding.startswith("/layer/stages/0") for finding in staged.remaining_findings)
