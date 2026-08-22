from __future__ import annotations

from pathlib import Path

import pytest

from tests.architecture.test_staged_architecture import _unit
from vfx_harness.domain.unit_outcomes import load_hypothesis_falsification
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
