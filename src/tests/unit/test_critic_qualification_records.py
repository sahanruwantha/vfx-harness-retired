"""Profile-set shape validation is pure and cannot itself grant qualification."""

from copy import deepcopy

import pytest

from vfx_harness.domain import critic_qualification as records


def record():
    source = {"path": "runs/current/reports/request.json", "sha256": "a" * 64}
    profile = {"judge_model": "model", "prompt_sha256": "b" * 64,
               "evidence_shape": "images/v2", "native_invocation_sha256": "c" * 64,
               "budgets": dict.fromkeys(records.RATES, 0.1), "metrics": dict.fromkeys(records.RATES, 0),
               "calibration_proof": {"request": source, "evaluation": deepcopy(source)}}
    return {"schema": records.SCHEMA, "suite": "reviewed-suite", "claim_id": "claim.form",
            "selection": deepcopy(source), "profiles": [profile]}


def test_distinct_measured_profiles_can_share_one_claim_without_a_single_prompt():
    value = record()
    second = deepcopy(value["profiles"][0])
    second.update(prompt_sha256="d" * 64, native_invocation_sha256="e" * 64)
    value["profiles"].append(second)
    records.validate_record(value)
    assert "passed" not in value
    assert "prompt" not in value


@pytest.mark.parametrize("failure", ["old_schema", "extra", "empty", "duplicate", "reordered",
                                     "unknown_rate", "bool_rate", "nan", "over_budget", "absolute",
                                     "traversal", "missing_proof", "bad_digest"])
def test_invalid_or_ambiguous_profile_sets_refuse(failure):
    value = record()
    profile = value["profiles"][0]
    if failure == "old_schema":
        value["schema"] = 1
    elif failure == "extra":
        value["passed"] = True
    elif failure == "empty":
        value["profiles"] = []
    elif failure == "duplicate":
        value["profiles"].append(deepcopy(profile))
    elif failure == "reordered":
        first = deepcopy(profile)
        first["native_invocation_sha256"] = "a" * 64
        value["profiles"].append(first)
    elif failure == "unknown_rate":
        profile["metrics"]["invented"] = 0
    elif failure in ("bool_rate", "nan", "over_budget"):
        profile["metrics"][records.RATES[0]] = {"bool_rate": False, "nan": float("nan"), "over_budget": 1}[failure]
    elif failure in ("absolute", "traversal"):
        value["selection"]["path"] = "/request.json" if failure == "absolute" else "runs/../request.json"
    elif failure == "missing_proof":
        profile["calibration_proof"].pop("evaluation")
    else:
        profile["native_invocation_sha256"] = "not-a-digest"
    with pytest.raises(ValueError):
        records.validate_record(value)
