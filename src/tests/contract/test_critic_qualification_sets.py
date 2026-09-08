"""Measured profile sets admit exact roles; they do not invent qualification or acceptance."""

import hashlib
import json
from copy import deepcopy
from dataclasses import replace

import pytest

from tests.contract.test_critic_qualification_admission import bound as bound
from tests.contract.test_critic_qualification_admission import claim_profile as claim_profile
from tests.contract.test_critic_qualification_admission import invoke
from tests.critic_calibration_fixtures import measured_artifact
from vfx_harness.evaluation import critic_calibration
from vfx_harness.orchestration import critic_qualification_publication as publication


def write_report(bound, name, value):
    path = bound.write_report(name, value)
    return {"path": path.relative_to(bound.shot).as_posix(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


@pytest.fixture
def measured_set(bound, claim_profile, monkeypatch):
    claim, expected = claim_profile
    observer = measured_artifact(bound, claim, expected, monkeypatch, invoke)
    first = observer["profiles"][0]["calibration_proof"]["request"]
    request = json.loads((bound.shot / first["path"]).read_text())
    focus_profile = deepcopy(request["profile"])
    focus_profile["scope"]["phase"] = "focus"
    focus_expected = expected | {"native_invocation_sha256": critic_calibration.fingerprint(focus_profile)}

    def focus_invoke(*args, **kwargs):
        return invoke(*args, **kwargs, phase="focus")

    focus = measured_artifact(bound, claim, focus_expected, monkeypatch, focus_invoke)
    second = focus["profiles"][0]["calibration_proof"]["request"]
    selection = write_report(bound, "selected-two-profile-set", {
        "schema": publication.SET_SCHEMA, "suite": expected["suite"], "claim_id": claim.id,
        "members": [second, first],
    })
    artifact = publication.publish(bound.shot, request=selection, check_current=lambda: None)
    selected_claim = publication.bind_claim(bound.shot, claim=replace(claim, qualification=None),
                                            artifact=artifact, check_current=lambda: None)
    return selected_claim, artifact, selection


def test_two_measured_roles_admit_and_unmeasured_role_refuses_before_inference(bound, measured_set):
    claim, artifact, _selection = measured_set
    record = publication.read_selected(bound.shot, claim, "fixture claim")
    assert len(record["profiles"]) == 2
    assert set(claim.qualification) == {"suite", "artifact", "artifact_sha256"}
    identities = [row["native_invocation_sha256"] for row in record["profiles"]]
    assert identities == sorted(set(identities))
    for role in ("observer", "focus"):
        observed = invoke(bound, claim, phase=role)
        assert observed["qualification_verified"] is True
        assert observed["acceptance_authorized"] is False
        report = json.loads((bound.shot / observed["report"]).read_text())
        source, = report["inputs"]["qualification_sources"]
        assert source["sha256"] == artifact["sha256"]
        assert source["native_invocation_sha256"] == report["qualification_check"]["native_invocation_sha256"]
    with pytest.raises(ValueError, match="no exact measured member"):
        invoke(bound, claim, lambda _: pytest.fail("unmeasured role reached inference"), phase="tie_breaker")


@pytest.mark.parametrize("failure", ["duplicate", "missing_profile", "wrong_member", "changed_trial"])
def test_profile_set_reopens_complete_selection_and_member_proofs(bound, measured_set, failure):
    claim, artifact, selection = measured_set
    record = json.loads((bound.shot / artifact["path"]).read_text())
    if failure == "duplicate":
        request = json.loads((bound.shot / selection["path"]).read_text())
        request["members"].append(request["members"][0])
        duplicate = write_report(bound, "duplicate-profile-set", request)
        with pytest.raises(ValueError, match="duplicate"):
            publication.publish(bound.shot, request=duplicate, check_current=lambda: None)
        return
    if failure == "missing_profile":
        record["profiles"].pop()
    elif failure == "wrong_member":
        record["profiles"][0]["calibration_proof"] = record["profiles"][1]["calibration_proof"]
    else:
        source = record["profiles"][0]["calibration_proof"]["request"]
        request = json.loads((bound.shot / source["path"]).read_text())
        (bound.shot / request["cases"][0]["trials"][0]["report"]).write_text("{}")
    tampered = write_report(bound, "tampered-profile-set", record)
    claim.qualification.update(artifact=tampered["path"], artifact_sha256=tampered["sha256"])
    with pytest.raises(ValueError):
        invoke(bound, claim, lambda _: pytest.fail("incomplete or changed proof reached inference"))
