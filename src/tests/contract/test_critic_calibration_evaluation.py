"""Measured qualification budgets come from pinned model trials, not report flags."""

import hashlib
import json
from dataclasses import replace

import flynn_agents_sdk as flynn
import pytest
from PIL import Image

from tests.contract.test_critic_qualification_admission import bound as bound
from tests.contract.test_critic_qualification_admission import claim_profile as claim_profile
from tests.contract.test_critic_qualification_admission import invoke
from tests.contract.test_flynn_critic_transport import response, verdict
from vfx_harness.evaluation import critic_calibration as calibration


@pytest.fixture
def suite(bound, claim_profile, request):
    claim, artifact = claim_profile
    cases = []
    labels = {"known_good": "pass", "identical": "pass", "defective": "fail",
              "near_threshold": "fail", "irrelevant": "pass", "unjudgeable": "unjudgeable"}
    for index, (control, expected) in enumerate(labels.items()):
        candidate = f"{control}.png"
        if control == "identical":
            (bound.shot / candidate).write_bytes((bound.shot / "reference.png").read_bytes())
        else:
            Image.new("RGB", (4, 4), (index * 30, 0, 200)).save(bound.shot / candidate)
        trials = []
        for repeat in range(2):
            value = verdict()
            value["scores"]["form"] = 4 if expected == "pass" else 2
            value["reference_usable"] = expected != "unjudgeable"
            if expected == "fail":
                value["observations"] = [{
                    "id": "form-miss", "kind": "qualitative", "axis": "form", "property": "shape",
                    "observation": "Form differs", "action": "Correct form", "moment": 7,
                    "roles": ["subject"], "claim_id": claim.id, "check_ids": ["form-v1"], "panel_ids": [],
                }]
            variant = getattr(request, "param", "clean")
            if variant == "errors":
                if control == "defective":
                    value["scores"]["form"] = 4
                if control == "known_good" and repeat == 0:
                    value["scores"]["form"] = 2
                    value["observations"] = [{
                        "id": "outside", "kind": "qualitative", "axis": "form", "property": "shape",
                        "observation": "Outside role", "action": "Change another subject", "moment": 7,
                        "roles": ["not-owned"], "claim_id": claim.id, "check_ids": [], "panel_ids": [],
                    }]
            if variant == "score_drift" and control == "irrelevant" and repeat == 0:
                value["scores"]["form"] = 5
            if variant == "reference_error" and control == "unjudgeable":
                value["reference_usable"] = True
            if variant == "missing_citation" and control == "defective":
                value["observations"] = []
            result = invoke(bound, claim, lambda _, value=value: response(value), qualification_claims=(),
                            calibration_claims=(replace(claim, qualification=None),),
                            images=(("reference", "reference.png"), ("candidate", candidate), ("focus", "focus.png")))
            report = json.loads((bound.shot / result["report"]).read_text())
            trials.append(calibration.Trial(
                bound.root.relative_to(bound.shot).as_posix(), result["report"], result["report_sha256"],
                calibration.fingerprint(flynn.SQLiteRun.inspect(bound.root / report["journal"])),
            ))
        cases.append(calibration.Case(control, control, expected, tuple(trials),
                                      baseline="known_good" if control == "irrelevant" else None))
    return {"root": bound.shot, "suite": "form-v1", "claim_id": claim.id,
            "invocation": artifact["native_invocation_sha256"], "cases": tuple(cases),
            "budgets": dict.fromkeys(calibration.RATES, 0)}


def test_reopens_trials_and_derives_all_five_rates_without_qualifying(suite):
    result = calibration.evaluate(**suite)
    assert result["within_budgets"] is True
    assert result["metrics"] == dict.fromkeys(calibration.RATES, 0)
    assert result["counts"]["false_pass_rate"] == {"errors": 0, "total": 6}
    assert result["counts"]["false_failure_rate"] == {"errors": 0, "total": 6}
    assert result["counts"]["repeatability_failure_rate"] == {"errors": 0, "total": 6}
    assert result["counts"]["scope_leakage_rate"] == {"errors": 0, "total": 12}
    assert result["counts"]["irrelevant_change_sensitivity_rate"] == {"errors": 0, "total": 4}
    assert result["qualification_verified"] is result["acceptance_authorized"] is False
    assert "passed" not in result


@pytest.mark.parametrize("failure", ["missing_control", "duplicate_case", "missing_repeat", "duplicate_trial",
                                     "wrong_label", "baseline", "budget", "invocation", "report", "journal", "image"])
def test_incomplete_or_substituted_suite_refuses(suite, failure):
    cases = list(suite["cases"])
    if failure == "missing_control":
        cases.pop()
    elif failure == "duplicate_case":
        cases.append(cases[0])
    elif failure == "missing_repeat":
        cases[0] = replace(cases[0], trials=cases[0].trials[:1])
    elif failure == "duplicate_trial":
        cases[0] = replace(cases[0], trials=(cases[0].trials[0],) * 2)
    elif failure == "wrong_label":
        cases[0] = replace(cases[0], expected="fail")
    elif failure == "baseline":
        cases[4] = replace(cases[4], baseline="defective")
    elif failure == "budget":
        suite["budgets"]["false_pass_rate"] = True
    elif failure == "invocation":
        suite["invocation"] = "0" * 64
    elif failure == "report":
        (suite["root"] / cases[0].trials[0].report).write_text("{}")
    elif failure == "journal":
        cases[0] = replace(cases[0], trials=(replace(cases[0].trials[0], journal_records_sha256="0" * 64),
                                          cases[0].trials[1]))
    else:
        Image.new("RGB", (4, 4), "black").save(suite["root"] / "known_good.png")
    suite["cases"] = tuple(cases)
    with pytest.raises(ValueError, match="critic calibration"):
        calibration.evaluate(**suite)


@pytest.mark.parametrize("suite", ["errors"], indirect=True)
def test_metric_denominators_and_pairwise_disagreement_are_derived(suite):
    result = calibration.evaluate(**suite)
    assert result["within_budgets"] is False
    assert result["counts"]["false_pass_rate"] == {"errors": 2, "total": 6}
    assert result["counts"]["false_failure_rate"] == {"errors": 1, "total": 6}
    assert result["counts"]["repeatability_failure_rate"] == {"errors": 1, "total": 6}
    assert result["counts"]["scope_leakage_rate"] == {"errors": 1, "total": 12}
    assert result["counts"]["irrelevant_change_sensitivity_rate"] == {"errors": 2, "total": 4}


@pytest.mark.parametrize("suite", ["score_drift"], indirect=True)
def test_same_pass_band_does_not_hide_score_instability(suite):
    result = calibration.evaluate(**suite)
    assert result["counts"]["false_failure_rate"]["errors"] == 0
    assert result["counts"]["repeatability_failure_rate"]["errors"] == 1
    assert result["counts"]["irrelevant_change_sensitivity_rate"]["errors"] == 2
    assert result["within_budgets"] is False


@pytest.mark.parametrize("suite", ["reference_error"], indirect=True)
def test_rejecting_candidate_is_not_recognizing_unusable_reference(suite):
    result = calibration.evaluate(**suite)
    assert result["metrics"] == dict.fromkeys(calibration.RATES, 0)
    assert result["unjudgeable_reference_errors"] == 2
    assert result["within_budgets"] is False


@pytest.mark.parametrize("suite", ["missing_citation"], indirect=True)
def test_low_score_without_an_owned_defect_citation_cannot_pass(suite):
    result = calibration.evaluate(**suite)
    assert result["metrics"] == dict.fromkeys(calibration.RATES, 0)
    assert result["citation_errors"] == 2
    assert result["within_budgets"] is False


def test_report_status_cannot_replace_journal_identity(suite):
    case = suite["cases"][0]
    trial = case.trials[0]
    path = suite["root"] / trial.report
    report = json.loads(path.read_text())
    report["calibration_check"]["configuration_sha256"] = "0" * 64
    payload = json.dumps(report).encode()
    path.write_bytes(payload)
    altered = replace(trial, report_sha256=hashlib.sha256(payload).hexdigest())
    suite["cases"] = (replace(case, trials=(altered, case.trials[1])), *suite["cases"][1:])
    with pytest.raises(ValueError, match="dispatched configuration"):
        calibration.evaluate(**suite)


@pytest.mark.parametrize("change", ["scope", "shape"])
def test_even_rehashed_report_profile_must_match_recorded_request(suite, change):
    case = suite["cases"][0]
    trial = case.trials[0]
    path = suite["root"] / trial.report
    report = json.loads(path.read_text())
    profile = report["calibration_check"]["profile"]
    if change == "scope":
        profile["scope"]["phase"] = "different-review"
    else:
        profile["images"][0]["size"] = [8, 8]
    suite["invocation"] = calibration.fingerprint(profile)
    report["calibration_check"]["native_invocation_sha256"] = suite["invocation"]
    payload = json.dumps(report).encode()
    path.write_bytes(payload)
    trial = replace(trial, report_sha256=hashlib.sha256(payload).hexdigest())
    suite["cases"] = (replace(case, trials=(trial, case.trials[1])), *suite["cases"][1:])
    with pytest.raises(ValueError, match=r"recorded context|image representation"):
        calibration.evaluate(**suite)
