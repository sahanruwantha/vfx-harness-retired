from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.architecture.test_staged_architecture import _unit
from vfx_harness.domain.unit_outcomes import (
    falsifying_decisions,
    load_hypothesis_falsification,
)
from vfx_harness.orchestration.unit_state import (
    initialize,
    load,
    record_hypothesis_falsification,
    transition,
)


def _record(tmp_path: Path, *, strength: str = "approved_start") -> dict:
    units = (_unit("proxy"), _unit("finish", depends_on=["proxy"]))
    initialize(tmp_path, "1", units, plan_hash="a" * 64)
    transition(tmp_path, "1", "proxy", "planning", reason="ready")
    transition(tmp_path, "1", "proxy", "building", reason="started")
    return record_hypothesis_falsification(
        tmp_path,
        "1",
        units[0],
        units,
        bundle_hash="b" * 64,
        unit_plan_hash="c" * 64,
        candidate_hash="d" * 64,
        settings_hash="e" * 64,
        contract_ids=["bbox-f36"],
        observations=[{
            "contract_id": "bbox-f36",
            "pass": False,
            "value": -10.0,
            "target": [0.95, 1.30],
        }],
        decisions=[{"id": "A-camera", "strength": strength}],
        conflict={
            "kind": "decision",
            "required_authority": "change the approved camera start",
            "roles": ["camera", "proxy"],
            "controls": ["camera_spine"],
        },
        evidence=["runs/run-1/evidence/bbox-f36.json"],
    )


def test_hypothesis_falsification_is_distinct_hash_pinned_state(tmp_path: Path) -> None:
    record = _record(tmp_path)

    state = load(tmp_path, "1")
    assert state["units"]["proxy"]["status"] == "hypothesis_falsified"
    assert state["units"]["finish"]["status"] == "blocked"
    assert record["affected"] == ["finish", "proxy"]
    assert record["identities"]["bundle_hash"] == "b" * 64
    artifact = (
        tmp_path
        / "state/work-units/hypothesis-falsifications"
        / f"{record['record_id']}.json"
    )
    parsed = load_hypothesis_falsification(artifact)
    assert parsed.record_id == record["record_id"]
    assert parsed.decisions[0].strength == "approved_start"


def test_falsified_hypothesis_cannot_be_retried_under_same_authority(tmp_path: Path) -> None:
    _record(tmp_path)

    with pytest.raises(ValueError, match="illegal work-unit transition"):
        transition(tmp_path, "1", "proxy", "retryable", reason="try again")


def test_falsifying_decisions_classify_by_declared_path_only() -> None:
    approved = SimpleNamespace(
        id="A2",
        decision_strength="approved_start",
        falsification_contract_ids=("SC-L1-16-housing-bbox-height-f36",),
    )
    planner = SimpleNamespace(
        id="A5",
        decision_strength="planner_start",
        falsification_contract_ids=("SC-L1-02-rim-light-count",),
    )
    hard = SimpleNamespace(
        id="A1",
        decision_strength="hard_constraint",
        falsification_contract_ids=("SC-L1-04-camera-lens",),
    )
    failing = ["SC-L1-16-housing-bbox-height-f36", "SC-L1-04-camera-lens", "unrelated-check"]

    picked = falsifying_decisions(failing, [approved, planner, hard])

    assert [record.id for record in picked] == ["A2", "A1"]
    assert falsifying_decisions(["unrelated-check"], [approved, planner, hard]) == ()


def _terminal_failure_fixture(tmp_path: Path, monkeypatch, *, failing_id: str):
    units = (
        _unit("camera_iris_bootstrap"),
        _unit("iris_mechanism_detail", depends_on=["camera_iris_bootstrap"]),
    )
    initialize(tmp_path, "1", units, plan_hash="a" * 64)
    transition(tmp_path, "1", "camera_iris_bootstrap", "planning", reason="ready")
    transition(tmp_path, "1", "camera_iris_bootstrap", "building", reason="started")
    script = tmp_path / "build" / "unit.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("print('build')\n", encoding="utf-8")
    plan = tmp_path / "plans" / "unit.md"
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text("# unit plan\n", encoding="utf-8")

    from vfx_harness.domain import plan_records
    from vfx_harness.orchestration import layer_plans, plan_authority

    assumption = SimpleNamespace(
        id="A2",
        decision_strength="approved_start",
        falsification_contract_ids=("SC-L1-16-housing-bbox-height-f36",),
    )
    monkeypatch.setattr(
        plan_authority,
        "resolve_current",
        lambda folder: SimpleNamespace(root=tmp_path, content_hash="b" * 64),
    )
    monkeypatch.setattr(plan_records, "load_assumptions", lambda root: (assumption,))
    monkeypatch.setattr(layer_plans, "work_unit_plan_path", lambda folder, unit: plan)

    slot = {
        "script": "build/unit.py",
        "attempt": 1,
        "rounds": [
            {
                "kind": "canonical",
                "round": 2,
                "run_id": "run-1",
                "render": None,
                "evidence": [
                    {
                        "id": failing_id,
                        "pass": False,
                        "metric": "bbox_height",
                        "value": 2.61,
                        "target": [0.95, 1.3],
                    },
                    {
                        "id": "SC-L1-01-blade-count",
                        "pass": True,
                        "metric": "object_count",
                        "value": 12,
                        "target": 12,
                    },
                ],
            }
        ],
    }
    shot = SimpleNamespace(folder=tmp_path)
    layer = SimpleNamespace(id="1", stages=units)
    milestone = SimpleNamespace(frame=36, ref="refs/f036.png")
    ledger = SimpleNamespace(_slot=lambda m: slot)
    return shot, layer, units[0], milestone, ledger


def test_terminal_failing_falsification_contract_routes_to_typed_record(
    tmp_path: Path, monkeypatch
) -> None:
    from vfx_harness.agents.builder import _record_bound_contract_falsification

    shot, layer, unit, milestone, ledger = _terminal_failure_fixture(
        tmp_path, monkeypatch, failing_id="SC-L1-16-housing-bbox-height-f36"
    )

    record = _record_bound_contract_falsification(shot, layer, unit, milestone, ledger)

    assert record is not None
    state = load(tmp_path, "1")
    assert state["units"]["camera_iris_bootstrap"]["status"] == "hypothesis_falsified"
    assert state["units"]["iris_mechanism_detail"]["status"] == "blocked"
    assert record["contract_ids"] == ["SC-L1-16-housing-bbox-height-f36"]
    assert record["decisions"] == [{"id": "A2", "strength": "approved_start"}]
    assert record["observations"][0]["value"] == 2.61
    artifact = (
        tmp_path
        / "state/work-units/hypothesis-falsifications"
        / f"{record['record_id']}.json"
    )
    parsed = load_hypothesis_falsification(artifact)
    assert parsed.conflict.kind == "decision"
    assert "vfx units replan --falsification" in parsed.conflict.required_authority
    assert not parsed.changes_hard_constraint


def test_terminal_failure_without_declared_path_stays_ordinary(
    tmp_path: Path, monkeypatch
) -> None:
    from vfx_harness.agents.builder import _record_bound_contract_falsification

    shot, layer, unit, milestone, ledger = _terminal_failure_fixture(
        tmp_path, monkeypatch, failing_id="SC-L1-05-blade-bbox-height-f1"
    )

    assert _record_bound_contract_falsification(shot, layer, unit, milestone, ledger) is None
    state = load(tmp_path, "1")
    assert state["units"]["camera_iris_bootstrap"]["status"] == "building"
    assert (
        not (tmp_path / "state/work-units/hypothesis-falsifications").exists()
    )


def test_falsification_rejects_unpinned_candidate_identity(tmp_path: Path) -> None:
    units = (_unit("proxy"),)
    initialize(tmp_path, "1", units, plan_hash="a" * 64)
    transition(tmp_path, "1", "proxy", "planning", reason="ready")
    transition(tmp_path, "1", "proxy", "building", reason="started")

    with pytest.raises(ValueError, match="candidate_hash"):
        record_hypothesis_falsification(
            tmp_path,
            "1",
            units[0],
            units,
            bundle_hash="b" * 64,
            unit_plan_hash="c" * 64,
            candidate_hash="missing",
            settings_hash="e" * 64,
            contract_ids=[],
            observations=[{"pass": False}],
            decisions=[],
            conflict={
                "kind": "contract",
                "required_authority": "add coverage",
                "roles": [],
                "controls": [],
            },
            evidence=["state/contract-gaps.jsonl"],
        )
