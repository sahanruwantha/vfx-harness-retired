"""A stored spike success flag cannot override missing or failing measurements."""

from __future__ import annotations

import json

import pytest

from tests.unit.test_plan_improvements import _typed_spike_fixture
from vfx_harness.evaluation.plan_gate.coverage import _check_evidence


@pytest.mark.parametrize("value", [0.8, 1.2, None, True, "1.0", {}, [], float("nan"), float("inf")])
def test_spike_success_requires_an_admissible_passing_measurement(tmp_path, value):
    plan, record = _typed_spike_fixture(tmp_path)
    record["results"][0]["value"] = value
    (tmp_path / "plans/evidence/spikes/probe.json").write_text(json.dumps(record))

    findings, _ = _check_evidence(tmp_path, plan)

    assert any(item.blocking and "failing or missing" in item.what for item in findings)


@pytest.mark.parametrize("mutation", ["empty", "missing", "extra", "duplicate_result",
                                      "duplicate_contract", "wrong_id", "malformed", "error"])
def test_spike_success_requires_exact_complete_results(tmp_path, mutation):
    plan, record = _typed_spike_fixture(tmp_path)
    result = record["results"][0]
    if mutation == "empty":
        record["contracts"] = []
        record["results"] = []
    elif mutation == "missing":
        record["results"] = []
    elif mutation == "extra":
        record["results"].append({**result, "id": "unrequested"})
    elif mutation == "duplicate_result":
        record["results"].insert(0, {**result, "value": 0.1, "pass": False})
    elif mutation == "duplicate_contract":
        record["contracts"].append(record["contracts"][0])
    elif mutation == "wrong_id":
        result["id"] = "another-contract"
    elif mutation == "malformed":
        record["results"] = {"id": result["id"]}
    else:
        result["error"] = "probe failed"
    (tmp_path / "plans/evidence/spikes/probe.json").write_text(json.dumps(record))

    findings, _ = _check_evidence(tmp_path, plan)

    assert any(item.blocking for item in findings)


@pytest.mark.parametrize("record", [None, [], "passed", 1])
def test_non_object_spike_is_a_gate_rejection(tmp_path, record):
    plan, _ = _typed_spike_fixture(tmp_path)
    (tmp_path / "plans/evidence/spikes/probe.json").write_text(json.dumps(record))
    findings, _ = _check_evidence(tmp_path, plan)
    assert any(item.blocking and "unsupported schema" in item.what for item in findings)
