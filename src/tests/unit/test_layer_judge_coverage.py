"""A layer judge frame no unit judges is unsatisfiable by construction.

caesar_curia layer 1, run 20260906T002035Z-f1ad1d:

    LAYER 1 judge list                     [1, 121, 301, 541, 841, 1081]
    union of unit required-claim moments   [1, 121, 301]
    unit camera_rig judge frames           [1, 121, 301]      <- internally consistent

The unit is fine. HIR-0045 requires every *unit* judge frame to appear in a required
claim's moments, and it does. But the composed canonical takes its judge list from the
LAYER and its claims from the union of the units, so it evaluated six frames, found
three with no required claim, and emitted `lookless_requires_executable_claims` with
`contract_gap: True` -- correctly. Materialization published a layer whose composed
evaluation cannot be satisfied by construction, and the plan gate passed it.

That is HIR-0045's rule with the wrong quantifier. Reported by the caesar_curia driver.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from vfx_harness.agents.builder.provisional_judgment import _composition_judge_unit
from vfx_harness.domain.work_units import WorkUnit, uncovered_unit_judge_frames
from vfx_harness.evaluation.plan_gate import _check_evidence_coherence
from vfx_harness.orchestration.ledger import Layer

CHECK = "layer-judge-coverage"

CAESAR_LAYER_JUDGES = (1, 121, 301, 541, 841, 1081)
UNIT_MOMENTS = (1, 121, 301)


def _unit_document(
    unit_id: str = "camera_rig",
    moments: tuple[int, ...] = UNIT_MOMENTS,
    *,
    look: tuple[str, ...] = (),
    authority: str = "executable_required",
) -> dict:
    return {
        "id": unit_id,
        "title": unit_id,
        "plan": f"plans/01/{unit_id}.md",
        "depends_on": [],
        "mutates": {
            "mode": "scoped",
            "roles": ["cam_rig"],
            "controls": [],
            "script_spans": [f"build/units/1/{unit_id}.py"],
        },
        "protects": {
            "selector": "all_active_upstream_interfaces",
            "resolve_to_explicit_ids_at": "freeze",
        },
        "evaluation": {
            "primary_judge": moments[0],
            "judge": [{"frame": frame, "ref": f"refs/f{frame:04d}.png"} for frame in moments],
            "temporal_evidence": "none",
            "claims": [
                {
                    "id": f"claim.{unit_id}",
                    "proposition": "the rig is placed",
                    "axis": "camera_framing",
                    "property": "state.cam_rig",
                    "subject_roles": ["cam_rig"],
                    "subject_controls": [],
                    "moments": list(moments),
                    "kind": "atomic",
                    "required": True,
                    "authority": authority,
                    "repair_owner": unit_id,
                    "asserts": "scene",
                    "evidence": [{"kind": "scene_contract", "id": f"contract.{unit_id}"}],
                }
            ],
        },
        "completion": "all_required_claims_and_protected_contracts_pass",
        "provides": ["camera"],
        "look_capabilities": list(look),
    }


def _unit(
    unit_id: str = "camera_rig",
    moments: tuple[int, ...] = UNIT_MOMENTS,
    *,
    look: tuple[str, ...] = (),
    authority: str = "executable_required",
) -> WorkUnit:
    return WorkUnit.parse(
        _unit_document(unit_id, moments, look=look, authority=authority),
        f"unit.{unit_id}",
    )


def _layer(judge_frames: tuple[int, ...], units: tuple[WorkUnit, ...]) -> Layer:
    return Layer(
        id="1",
        script="build/01.py",
        title="Camera",
        judges=tuple((frame, f"refs/f{frame:04d}.png") for frame in judge_frames),
        reads="full layer",
        owns=("camera_framing",),
        primary_judge=judge_frames[0],
        stages=units,
    )


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _gate(
    tmp_path: Path,
    judge_frames: tuple[int, ...],
    units: list[dict],
) -> list:
    _write(
        tmp_path / "layers.json",
        {
            "schema": 5,
            "layers": [
                {
                    "id": "1",
                    "script": "build/01.py",
                    "title": "Camera",
                    "judge": [
                        {"frame": frame, "ref": f"refs/f{frame:04d}.png"}
                        for frame in judge_frames
                    ],
                    "reads": "full layer",
                    "owns": ["camera_framing"],
                    "primary_judge": judge_frames[0],
                    "evidence_domains": ["scene"],
                    "stages": units,
                }
            ],
        },
    )
    _write(tmp_path / "scene_checks.json", {"schema": 2, "contracts": []})
    _write(tmp_path / "checks.json", {"schema": 2, "checks": []})
    findings, _counts = _check_evidence_coherence(tmp_path)
    return findings


def test_the_composed_unit_the_builder_judges_cannot_pass_and_the_unit_is_clean() -> None:
    """The property the consumer depends on, stated in symbols that predate the fix.

    `_composition_judge_unit` takes its judge list from the LAYER and its claims from
    the union of the units, so this composed unit is unsatisfiable by construction --
    while `uncovered_unit_judge_frames` on the real unit is empty, which is why nothing
    caught it.
    """
    layer = _layer(CAESAR_LAYER_JUDGES, (_unit(),))
    composed = _composition_judge_unit(layer)
    assert composed is not None

    assert tuple(point.frame for point in composed.evaluation.judges) == CAESAR_LAYER_JUDGES
    assert uncovered_unit_judge_frames(composed) == (541, 841, 1081)
    assert uncovered_unit_judge_frames(layer.stages[0]) == ()


def test_the_gate_refuses_that_layer_and_names_it_in_the_typed_field(tmp_path: Path) -> None:
    findings = _gate(tmp_path, CAESAR_LAYER_JUDGES, [_unit_document()])

    matches = [finding for finding in findings if finding.check == CHECK]
    assert matches, [finding.check for finding in findings]
    finding = matches[0]
    assert finding.blocking
    # HIR-0187: ownership written in prose is ownership clean_for cannot read.
    assert finding.layer == "1"
    assert "f541" in finding.where and "f841" in finding.where and "f1081" in finding.where


def test_the_refusal_names_the_fix_and_not_only_the_violation(tmp_path: Path) -> None:
    findings = _gate(tmp_path, CAESAR_LAYER_JUDGES, [_unit_document()])
    fix = next(finding.fix for finding in findings if finding.check == CHECK)

    # The layer judge list is what materialization may not change; say so.
    assert "structural" in fix and "cannot shrink" in fix
    # And name what it may author instead.
    assert "executable claim" in fix
    assert "not a critic look vote" in fix


def test_a_layer_its_one_unit_covers_is_clean(tmp_path: Path) -> None:
    findings = _gate(tmp_path, UNIT_MOMENTS, [_unit_document()])

    assert not [finding for finding in findings if finding.check == CHECK]


def test_two_units_cover_the_layer_between_them(tmp_path: Path) -> None:
    """Coverage is the union across units -- neither unit judges all six frames."""
    findings = _gate(
        tmp_path,
        CAESAR_LAYER_JUDGES,
        [
            _unit_document("camera_rig", (1, 121, 301)),
            _unit_document("camera_move", (541, 841, 1081)),
        ],
    )

    assert not [finding for finding in findings if finding.check == CHECK]


def test_a_look_owning_layer_is_not_asked_for_executable_coverage(tmp_path: Path) -> None:
    """A critic decides there, so demanding coverage would refuse a legal layer."""
    layer = _layer(CAESAR_LAYER_JUDGES, (_unit(look=("lighting",)),))
    # The builder compiles no look-less composed judge at all for this layer.
    assert _composition_judge_unit(layer) is None

    findings = _gate(
        tmp_path, CAESAR_LAYER_JUDGES, [_unit_document(look=("lighting",))]
    )
    assert not [finding for finding in findings if finding.check == CHECK]


def test_a_layer_whose_required_claim_is_qualitative_is_exempt() -> None:
    """`required` and `advisory` are exclusive at parse, so this is the only shape.

    The gate and the builder must agree on the exemption, not only on the refusal.
    """
    unit = SimpleNamespace(
        id="cam_path_core",
        look_capabilities=(),
        evaluation=SimpleNamespace(
            claims=(
                SimpleNamespace(
                    required=True,
                    moments=UNIT_MOMENTS,
                    authority="qualified_qualitative_required",
                    evidence=(),
                ),
            )
        ),
        mutates=SimpleNamespace(roles=("cam_rig",), controls=()),
    )
    layer = SimpleNamespace(
        id="1",
        judges=tuple((frame, "refs/f.png") for frame in CAESAR_LAYER_JUDGES),
        stages=(unit,),
    )

    assert _composition_judge_unit(layer) is None
