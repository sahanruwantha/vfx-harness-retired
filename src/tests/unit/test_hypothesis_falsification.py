from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.architecture.test_staged_architecture import _unit
from tests.unit_attempt_fixtures import (
    ABSENT_SELECTION_TOKEN,
    claim_for_build,
    pass_unit,
)
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
    plan_hash = "a" * 64
    initialize(tmp_path, "1", units, plan_hash=plan_hash)
    attempt = claim_for_build(
        tmp_path,
        "1",
        units,
        "proxy",
        plan_hash=plan_hash,
    )
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
        attempt=attempt,
        selection_token=ABSENT_SELECTION_TOKEN,
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

    with pytest.raises(ValueError, match="generic unclaimed transition refuses"):
        transition(tmp_path, "1", "proxy", "retryable", reason="try again")


def test_falsification_can_name_passed_upstream_fault_owner_for_replan(tmp_path: Path) -> None:
    units = (
        _unit("lighting"),
        _unit("detail", depends_on=["lighting"]),
        _unit("atmosphere", depends_on=["detail"]),
    )
    plan_hash = "a" * 64
    initialize(tmp_path, "1", units, plan_hash=plan_hash)
    pass_unit(tmp_path, "1", units[0], units, plan_hash=plan_hash)
    detail_attempt = claim_for_build(
        tmp_path,
        "1",
        units,
        "detail",
        plan_hash=plan_hash,
    )

    finding = record_hypothesis_falsification(
        tmp_path,
        "1",
        units[1],
        units,
        bundle_hash="b" * 64,
        unit_plan_hash="c" * 64,
        candidate_hash="d" * 64,
        settings_hash="e" * 64,
        contract_ids=["silhouette-bloom"],
        observations=[{"pass": False, "classification": "unsatisfiable_in_scope"}],
        decisions=[],
        conflict={
            "kind": "contract",
            "required_authority": "reopen the sealed lighting owner",
            "roles": ["detail.hero"],
            "controls": [],
        },
        evidence=["runs/run-1/evidence/f150.png"],
        affected_seed_ids={"detail", "lighting"},
        attempt=detail_attempt,
        selection_token=ABSENT_SELECTION_TOKEN,
    )

    state = load(tmp_path, "1")
    assert finding["affected"] == ["atmosphere", "detail", "lighting"]
    assert state["units"]["lighting"]["status"] == "passed"
    assert state["units"]["detail"]["status"] == "hypothesis_falsified"
    assert state["units"]["atmosphere"]["status"] == "blocked"


def test_composed_falsification_preserves_accepted_source_until_replan(
    tmp_path: Path,
) -> None:
    units = (_unit("mass"), _unit("roof", depends_on=["mass"]))
    plan_hash = "a" * 64
    initialize(tmp_path, "2", units, plan_hash=plan_hash)
    pass_unit(tmp_path, "2", units[0], units, plan_hash=plan_hash)
    pass_unit(tmp_path, "2", units[1], units, plan_hash=plan_hash)

    finding = record_hypothesis_falsification(
        tmp_path,
        "2",
        units[0],
        units,
        bundle_hash="b" * 64,
        unit_plan_hash="c" * 64,
        candidate_hash="d" * 64,
        settings_hash="e" * 64,
        contract_ids=["requirement:R51:form"],
        observations=[{"classification": "qualified_composed_failure"}],
        decisions=[{"id": "R51", "strength": "approved_start"}],
        conflict={
            "kind": "decision",
            "required_authority": "transactionally reopen producer closure",
            "roles": ["building.mass", "building.roof"],
            "controls": [],
        },
        evidence=["state/contract-gaps.jsonl"],
        affected_seed_ids={"mass", "roof"},
        preserve_accepted_source=True,
        selection_token=ABSENT_SELECTION_TOKEN,
    )

    state = load(tmp_path, "2")
    assert finding["affected"] == ["mass", "roof"]
    assert state["units"]["mass"]["status"] == "passed"
    assert state["units"]["roof"]["status"] == "passed"
    assert state["units"]["mass"]["falsification"]["record_id"] == finding["record_id"]
    assert state["units"]["mass"]["history"][-1]["to"] == "passed"


def test_falsification_records_earlier_layer_camera_without_local_affected(
    tmp_path: Path,
) -> None:
    units = (_unit("facade"),)
    plan_hash = "a" * 64
    initialize(tmp_path, "2", units, plan_hash=plan_hash)
    attempt = claim_for_build(
        tmp_path,
        "2",
        units,
        "facade",
        plan_hash=plan_hash,
    )

    finding = record_hypothesis_falsification(
        tmp_path,
        "2",
        units[0],
        units,
        bundle_hash="b" * 64,
        unit_plan_hash="c" * 64,
        candidate_hash="d" * 64,
        settings_hash="e" * 64,
        contract_ids=["subject-bbox-1"],
        observations=[{"pass": False, "classification": "unsatisfiable_in_scope"}],
        decisions=[],
        conflict={
            "kind": "ownership",
            "required_authority": "reopen the earlier camera owner",
            "roles": ["atrium.shell"],
            "controls": [],
        },
        evidence=["runs/run-1/evidence/f038.png"],
        affected_seed_ids={"facade", "camera_path"},
        attempt=attempt,
        selection_token=ABSENT_SELECTION_TOKEN,
    )

    state = load(tmp_path, "2")
    assert finding["affected"] == ["facade"]
    assert finding["fault_owner_units"] == ["camera_path"]
    assert "camera_path" not in finding["affected"]
    assert state["units"]["facade"]["status"] == "hypothesis_falsified"
    parsed = load_hypothesis_falsification(
        tmp_path
        / "state/work-units/hypothesis-falsifications"
        / f"{finding['record_id']}.json"
    )
    assert parsed.fault_owner_units == ("camera_path",)


def test_hypothesis_falsification_parses_without_fault_owner_units() -> None:
    from vfx_harness.domain.unit_outcomes import (
        HYPOTHESIS_FALSIFICATION_SCHEMA,
        HypothesisFalsification,
    )

    digest = "b" * 64
    parsed = HypothesisFalsification.parse(
        {
            "schema": HYPOTHESIS_FALSIFICATION_SCHEMA,
            "record_id": "hf-legacy",
            "recorded_at": "2026-08-30T00:00:00+00:00",
            "layer": "2",
            "unit": "facade",
            "identities": {
                "bundle_hash": digest,
                "plan_hash": digest,
                "unit_hash": digest,
                "unit_plan_hash": digest,
                "candidate_hash": digest,
                "settings_hash": digest,
            },
            "contract_ids": ["subject-bbox-1"],
            "observations": [{"pass": False}],
            "decisions": [],
            "conflict": {
                "kind": "contract",
                "required_authority": "amend",
                "roles": [],
                "controls": [],
            },
            "evidence": ["runs/run-1/evidence/f038.png"],
            "affected": ["facade"],
        }
    )
    assert parsed.fault_owner_units == ()


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
    plan_hash = "a" * 64
    initialize(tmp_path, "1", units, plan_hash=plan_hash)
    attempt = claim_for_build(
        tmp_path,
        "1",
        units,
        "camera_iris_bootstrap",
        plan_hash=plan_hash,
    )
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
    bundle = SimpleNamespace(root=tmp_path, content_hash="b" * 64)
    monkeypatch.setattr(plan_authority, "resolve_current", lambda folder: bundle)
    monkeypatch.setattr(plan_records, "load_assumptions", lambda root: (assumption,))
    monkeypatch.setattr(
        layer_plans,
        "work_unit_plan_path",
        lambda folder, unit, **_kwargs: plan,
    )

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
    selected = SimpleNamespace(
        plan=SimpleNamespace(bundle=bundle),
        selection_token=ABSENT_SELECTION_TOKEN,
    )
    return shot, layer, units[0], milestone, ledger, attempt, selected


def test_terminal_failing_falsification_contract_routes_to_typed_record(
    tmp_path: Path, monkeypatch
) -> None:
    from vfx_harness.agents.builder import _record_bound_contract_falsification

    shot, layer, unit, milestone, ledger, attempt, selected = _terminal_failure_fixture(
        tmp_path, monkeypatch, failing_id="SC-L1-16-housing-bbox-height-f36"
    )

    record = _record_bound_contract_falsification(
        shot,
        layer,
        unit,
        milestone,
        ledger,
        attempt=attempt,
        selected_authority=selected,
    )

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

    shot, layer, unit, milestone, ledger, attempt, selected = _terminal_failure_fixture(
        tmp_path, monkeypatch, failing_id="SC-L1-05-blade-bbox-height-f1"
    )

    assert (
        _record_bound_contract_falsification(
            shot,
            layer,
            unit,
            milestone,
            ledger,
            attempt=attempt,
            selected_authority=selected,
        )
        is None
    )
    state = load(tmp_path, "1")
    assert state["units"]["camera_iris_bootstrap"]["status"] == "building"
    assert (
        not (tmp_path / "state/work-units/hypothesis-falsifications").exists()
    )


def test_falsification_rejects_unpinned_candidate_identity(tmp_path: Path) -> None:
    units = (_unit("proxy"),)
    plan_hash = "a" * 64
    initialize(tmp_path, "1", units, plan_hash=plan_hash)
    attempt = claim_for_build(
        tmp_path,
        "1",
        units,
        "proxy",
        plan_hash=plan_hash,
    )

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
            attempt=attempt,
            selection_token=ABSENT_SELECTION_TOKEN,
        )
