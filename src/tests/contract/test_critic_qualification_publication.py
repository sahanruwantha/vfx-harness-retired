"""Qualification publication re-derives measurements and grants no plan-selection authority."""

import hashlib
import json

import pytest

from tests.contract.test_critic_qualification_admission import bound as bound
from tests.contract.test_critic_qualification_admission import claim_profile as claim_profile
from tests.contract.test_critic_qualification_admission import invoke, publish
from tests.contract.test_critic_qualification_admission import qualified as qualified
from tests.contract.test_flynn_critic_transport import response
from vfx_harness.observability import run_artifacts
from vfx_harness.orchestration import critic_qualification_publication as publication


def rewrite(root, reference, value):
    payload = json.dumps(value).encode()
    (root / reference["path"]).write_bytes(payload)
    reference["sha256"] = hashlib.sha256(payload).hexdigest()


def test_publishes_recomputed_artifact_without_mutating_selected_claim(bound, qualified):
    claim, record = qualified
    original = dict(claim.qualification)
    result = publication.publish(bound.shot, request=record["calibration_proof"]["request"], check_current=lambda: None)
    path = bound.shot / result["path"]
    assert path.parent == bound.reports
    assert hashlib.sha256(path.read_bytes()).hexdigest() == result["sha256"]
    published = json.loads(path.read_text())
    publication.verify(bound.shot, published)
    assert published["metrics"] == record["metrics"]
    assert published["claim_id"] == claim.id
    assert claim.qualification == original
    assert not (bound.shot / "shot.json").exists()
    assert not list((bound.checkpoints / "flynn").glob("*.sqlite"))


@pytest.mark.parametrize("failure", ["missing_proof", "claim", "metrics", "evaluation", "request", "trial"])
def test_native_admission_reopens_proof_before_spending(bound, qualified, failure):
    claim, record = qualified
    if failure == "missing_proof":
        record.pop("calibration_proof")
    elif failure == "claim":
        record["claim_id"] = "different-claim"
    elif failure == "metrics":
        record["metrics"]["false_pass_rate"] = 0.05
    else:
        proof = record["calibration_proof"]
        reference = proof["request" if failure == "trial" else failure]
        selected = json.loads((bound.shot / reference["path"]).read_text())
        if failure == "trial":
            (bound.shot / selected["cases"][0]["trials"][0]["report"]).write_text("{}")
        elif failure == "evaluation":
            selected["counts"]["false_pass_rate"]["total"] += 1
            rewrite(bound.shot, reference, selected)
        else:
            selected["budgets"]["false_pass_rate"] = 0.5
            rewrite(bound.shot, reference, selected)
    publish(bound, claim, record)
    with pytest.raises(ValueError, match=r"qualification|calibration"):
        invoke(bound, claim, lambda _: pytest.fail("invalid proof reached the provider"))
    assert not list((bound.checkpoints / "flynn").glob("*.sqlite"))


def test_changed_proof_during_inference_refuses_observation(bound, qualified):
    claim, record = qualified

    def handler(_):
        (bound.shot / record["calibration_proof"]["evaluation"]["path"]).write_text("{}")
        return response()

    with pytest.raises(ValueError, match="source digest differs"):
        invoke(bound, claim, handler)
    report_path, = bound.reports.glob("critic-*.json")
    report = json.loads(report_path.read_text())
    assert report["usage"]["known_output_tokens"] == 31
    assert report["qualification_verified"] is False


@pytest.mark.parametrize("failure", ["owner", "run", "request", "selection"])
def test_publication_refuses_changed_ownership_or_sources(bound, qualified, monkeypatch, failure):
    _claim, record = qualified
    selected = record["calibration_proof"]["request"]
    checks = 0

    def check():
        nonlocal checks
        checks += 1
        if checks == 3:
            if failure == "owner":
                raise ValueError("owning authority changed")
            if failure == "run":
                monkeypatch.delenv(run_artifacts.ENV)
            elif failure == "request":
                (bound.shot / selected["path"]).write_text("{}")
            else:
                selected["sha256"] = "0" * 64

    with pytest.raises(ValueError, match=r"changed|digest differs"):
        publication.publish(bound.shot, request=selected, check_current=check)
    assert not [path for path in bound.reports.glob("qualification-*.json") if not path.stem.endswith("-evaluation")]


def test_failed_measured_suite_cannot_publish_a_passed_artifact(bound, qualified):
    _claim, record = qualified
    selected = dict(record["calibration_proof"]["request"])
    request = json.loads((bound.shot / selected["path"]).read_text())
    # Both near-threshold votes failed; an authored passing label exposes false failures.
    request["cases"][3]["expected"] = "pass"
    rewrite(bound.shot, selected, request)
    with pytest.raises(ValueError, match="failed measured budgets"):
        publication.publish(bound.shot, request=selected, check_current=lambda: None)
    assert not list(bound.reports.glob("qualification-*.json"))


def test_suite_has_a_closed_schema(bound, qualified):
    _claim, record = qualified
    selected = dict(record["calibration_proof"]["request"])
    request = json.loads((bound.shot / selected["path"]).read_text())
    request["passed"] = True
    rewrite(bound.shot, selected, request)
    with pytest.raises(ValueError, match="requires exactly"):
        publication.publish(bound.shot, request=selected, check_current=lambda: None)
