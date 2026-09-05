"""Subject-framing coverage is one predicate, refused at the camera unit's stage call (HIR-0177).

Layer-1 rematerializations 20260903T023810Z-8509f9 and 20260903T035326Z-290f3c both staged
the camera unit without the persistent subject bbox rows and learned the rule from the
terminal gate; the plan gate and the staging transaction now share the predicate.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.unit.test_plan_records import (
    _add_deferred_layer,
    _base_selection,
    _candidate,
    _deferred_owner,
    _jit_payload,
    _write,
    publish_current,
)
from vfx_harness.domain.work_units.subject_framing import (
    SUBJECT_FRAMING_COVERAGE_RULE,
    is_subject_framing_row,
    uncovered_subject_framing_frames,
)
from vfx_harness.observability import run_artifacts
from vfx_harness.orchestration.jit_materialization import (
    seed_materialization_candidate,
    stage_materialization_unit,
)


def _bbox(id: str, frame: int, roles: list[str], *, owner: str = "1", activates_at: str | None = None) -> dict:
    return {
        "id": id,
        "kind": "bbox_height",
        "roles": roles,
        "frame": frame,
        "op": "band",
        "lo": 0.3,
        "hi": 0.6,
        "owner_layer": owner,
        "fault_owner": owner,
        "activates_at": activates_at or owner,
        "lifecycle": "persistent" if activates_at else "layer",
        "axis": "framing",
    }


def _camera_unit(*, claims=(), context=None) -> dict:
    unit = {
        "id": "camera_rig",
        "provides": ["camera"],
        "mutates": {"roles": ["camera.rig"], "controls": []},
        "evaluation": {"claims": list(claims)},
    }
    if context is not None:
        unit["evaluation"]["composition_context"] = context
    return unit


def test_predicate_matches_the_gate_semantics() -> None:
    camera_only = frozenset({"camera.rig"})
    assert not is_subject_framing_row(_bbox("cam", 1, ["camera.rig"]), camera_only)
    assert is_subject_framing_row(_bbox("hero", 1, ["hero"]), camera_only)
    assert not is_subject_framing_row({"kind": "projected_origin_x", "roles": ["hero"], "frame": 1}, camera_only)

    stages = {"camera_rig": _camera_unit()}
    rows = [_bbox("cam-only", 1, ["camera.rig"])]
    assert uncovered_subject_framing_frames("1", [1, 38], stages, rows) == (1, 38)

    claim = {
        "required": True,
        "moments": [1],
        "evidence": [{"kind": "scene_contract", "id": "hero-f1"}],
    }
    stages = {"camera_rig": _camera_unit(claims=[claim])}
    assert uncovered_subject_framing_frames("1", [1, 38], stages, [_bbox("hero-f1", 1, ["hero"])]) == (38,)

    deferred = _bbox("exterior-f38", 38, ["exterior.*"], activates_at="2")
    context = {"frames": [38], "contract_ids": ["exterior-f38"]}
    stages = {"camera_rig": _camera_unit(claims=[claim], context=context)}
    assert uncovered_subject_framing_frames("1", [1, 38], stages, [_bbox("hero-f1", 1, ["hero"]), deferred]) == ()
    # The persistent row this layer authored covers its frame even without a context binding.
    stages = {"camera_rig": _camera_unit(claims=[claim])}
    assert uncovered_subject_framing_frames("1", [1, 38], stages, [_bbox("hero-f1", 1, ["hero"]), deferred]) == ()


def test_camera_unit_stage_call_refuses_uncovered_judge_frames(tmp_path: Path) -> None:
    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    layers = json.loads((tmp_path / "layers.json").read_text(encoding="utf-8"))
    layers["layers"][1]["jit"]["reserved_roles"].append("camera.*")
    layers["layers"][1]["jit"]["provides"] = {"camera": ["camera.*"]}
    _write(tmp_path / "layers.json", layers)
    layout = run_artifacts.create(tmp_path, "stage-subject-framing")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    full = json.loads(_jit_payload(tmp_path, bundle.content_hash).read_text(encoding="utf-8"))
    target = tmp_path / "stage-subject-framing.json"
    seed_materialization_candidate(
        bundle.root,
        target,
        layer_id="2",
        bundle_hash=bundle.content_hash,
        base_selection=_base_selection(tmp_path),
    )
    unit = json.loads(json.dumps(full["layer"]["stages"][0]))
    unit["provides"] = ["camera"]
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
    assert "subject-framing coverage:" in message
    assert "judge frame(s) [239, 240]" in message
    assert SUBJECT_FRAMING_COVERAGE_RULE in message
    assert target.read_bytes() == before

    deferred = [
        _bbox("exterior-f239", 239, ["exterior.*"], owner="2", activates_at="3"),
        _bbox("exterior-f240", 240, ["exterior.*"], owner="2", activates_at="3"),
    ]
    unit["evaluation"]["composition_context"] = {
        "frames": [239, 240],
        "contract_ids": ["exterior-f239", "exterior-f240"],
    }
    stage_materialization_unit(
        target,
        unit=unit,
        scene_contracts=[*full["scene_contracts"], *deferred],
        requirement_bindings=full["requirement_bindings"],
    )
    assert target.read_bytes() != before


def test_stage_call_lists_the_composition_obligation_for_a_form_layer(tmp_path: Path) -> None:
    """A composition-owning geometry layer sees uncovered judge frames before finalize."""
    from vfx_harness.orchestration.jit_materialization import MaterializationInspection

    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    layout = run_artifacts.create(tmp_path, "form-layer-composition")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    full = json.loads(_jit_payload(tmp_path, bundle.content_hash).read_text(encoding="utf-8"))
    target = tmp_path / "form-layer-composition.json"
    seed_materialization_candidate(
        bundle.root,
        target,
        layer_id="2",
        bundle_hash=bundle.content_hash,
        base_selection=_base_selection(tmp_path),
    )
    staged = stage_materialization_unit(
        target,
        unit=full["layer"]["stages"][0],
        scene_contracts=full["scene_contracts"],
        requirement_bindings=full["requirement_bindings"],
        inspection=MaterializationInspection(
            global_root=bundle.root,
            expected_bundle_hash=bundle.content_hash,
            shot_folder=bundle.root,
        ),
    )
    open_findings = [row for row in staged.remaining_findings if "composition-coverage" in row]
    assert open_findings, staged.remaining_findings
    assert "judge frame(s) [239, 240]" in open_findings[0]


def test_camera_layer_candidate_reports_uncovered_downstream_subjects(tmp_path: Path) -> None:
    """HIR-0184: the terminal validator names the later subject the camera never framed."""
    from vfx_harness.domain.work_units.subject_framing import DOWNSTREAM_SUBJECT_COVERAGE_RULE
    from vfx_harness.orchestration.jit_materialization import inspect_materialization

    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    layers = json.loads((tmp_path / "layers.json").read_text(encoding="utf-8"))
    layers["layers"][1]["jit"]["reserved_roles"].append("camera.*")
    layers["layers"][1]["jit"]["provides"] = {"camera": ["camera.*"]}
    layers["layers"].append(
        {
            "id": "3",
            "script": "build/03_future.py",
            "title": "Future subject",
            "primary_judge": 239,
            "judge": [{"frame": 239, "ref": "refs/a.png"}],
            "owns": ["future_axis"],
            "reads": "a later subject",
            "evidence_domains": ["scene"],
            "execution": "jit_deferred",
            "stages": [],
            "jit": {
                "depends_on_layers": ["2"],
                "required_outcomes": [],
                "provides": {},
                "reserved_roles": ["future.*"],
                "owned_requirements": ["R-future"],
            },
        }
    )
    _write(tmp_path / "layers.json", layers)
    requirements = json.loads((tmp_path / "requirements.json").read_text(encoding="utf-8"))
    future = json.loads(json.dumps(requirements["requirements"][0]))
    future["id"] = "R-future"
    future["statement"] = "A later subject fills the frame."
    future["resolution"] = _deferred_owner("3", "scene")
    requirements["requirements"].append(future)
    _write(tmp_path / "requirements.json", requirements)
    layout = run_artifacts.create(tmp_path, "downstream-coverage")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    full = json.loads(_jit_payload(tmp_path, bundle.content_hash).read_text(encoding="utf-8"))
    target = tmp_path / "downstream-coverage.json"
    seed_materialization_candidate(
        bundle.root,
        target,
        layer_id="2",
        bundle_hash=bundle.content_hash,
        base_selection=_base_selection(tmp_path),
    )
    unit = json.loads(json.dumps(full["layer"]["stages"][0]))
    unit["provides"] = ["camera"]
    covering = [
        _bbox("polish-f239", 239, ["polish.hero"], owner="2", activates_at="3"),
        _bbox("polish-f240", 240, ["polish.hero"], owner="2", activates_at="3"),
    ]
    unit["evaluation"]["composition_context"] = {
        "frames": [239, 240],
        "contract_ids": ["polish-f239", "polish-f240"],
    }
    stage_materialization_unit(
        target,
        unit=unit,
        scene_contracts=[*full["scene_contracts"], *covering],
        requirement_bindings=full["requirement_bindings"],
    )
    findings, _materialized = inspect_materialization(
        bundle.root, target, expected_bundle_hash=bundle.content_hash, shot_folder=bundle.root
    )
    downstream = [finding for finding in findings if "downstream subject coverage" in finding]
    assert len(downstream) == 1, findings
    assert "judge f239 is shared with layer 3 (future.*)" in downstream[0]
    assert "refs/a.png" in downstream[0]
    assert DOWNSTREAM_SUBJECT_COVERAGE_RULE in downstream[0]

    covered = json.loads(target.read_text(encoding="utf-8"))
    covered["scene_contracts"].append(_bbox("future-f239", 239, ["future.subject"], owner="2", activates_at="3"))
    covered["layer"]["stages"][0]["evaluation"]["composition_context"]["contract_ids"].append("future-f239")
    _write(target, covered)
    findings, _materialized = inspect_materialization(
        bundle.root, target, expected_bundle_hash=bundle.content_hash, shot_folder=bundle.root
    )
    assert not [finding for finding in findings if "downstream subject coverage" in finding], findings
