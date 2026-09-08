"""A measured credential may bind only the exact debt claim that was calibrated."""

import hashlib
import json
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from tests.contract.test_critic_qualification_admission import bound as bound
from tests.contract.test_critic_qualification_admission import claim_profile as claim_profile
from tests.contract.test_critic_qualification_admission import invoke
from tests.contract.test_critic_qualification_admission import qualified as qualified
from tests.critic_calibration_fixtures import measured_artifact
from vfx_harness.agents import critic_qualification
from vfx_harness.agents.builder import provisional_judgment
from vfx_harness.domain.work_units.claims import Claim, EvidenceBinding
from vfx_harness.orchestration import critic_qualification_publication as publication


@pytest.fixture
def debt():
    layer = SimpleNamespace(
        id="surface", judges=((7, "reference.png"), (11, "later.png")), owns=("form",),
        stages=(SimpleNamespace(id="subject", look_capabilities=(),
                                evaluation=SimpleNamespace(claims=()),
                                mutates=SimpleNamespace(roles=("subject",), controls=())),),
    )
    decision = {"id": "requirement-appearance", "debt_id": "jd-appearance", "statement": "The subject reads clearly",
                "observation_medium": "eevee", "judge_points": ((7, "reference.png"),), "axes": ("form",),
                "subject_roles": ("subject",), "claim_kind": "atomic", "property": "subject_appearance",
                "fault_owner": "subject"}
    unit = provisional_judgment._composition_judge_unit(layer, (decision,))
    return layer, decision, unit.evaluation.claims[0]


def reference(root, path):
    return {"path": path.relative_to(root).as_posix(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def test_composed_debt_is_typed_and_calibratable_without_claiming_qualification(debt):
    layer, decision, claim = debt
    assert isinstance(claim, Claim)
    assert claim.qualification is None
    assert claim.evidence == (EvidenceBinding("qualification", "judgment-debt:jd-appearance:form"),)
    assert claim.moments == (7,)
    assert claim.repair_owner == decision["fault_owner"]
    assert claim.subject_roles == decision["subject_roles"]
    semantics = critic_qualification.selected_semantics((claim,), axes=("form",), frames=(7,))
    assert semantics[0]["proposition"] == decision["statement"]
    unit = provisional_judgment._composition_judge_unit(layer, (decision,))
    assert [point.frame for point in unit.evaluation.judges] == [7, 11]
    with pytest.raises(ValueError, match="outside invocation scope"):
        critic_qualification.selected_semantics((claim,), axes=("form",), frames=(11,))


def test_measured_debt_binds_and_reenters_its_exact_composed_group(bound, debt, monkeypatch):
    layer, decision, claim = debt
    trial = invoke(bound, claim, qualification_claims=(), calibration_claims=(claim,))
    report = json.loads((bound.shot / trial["report"]).read_text())
    expected = {"suite": claim.binding_ids[0], "budgets": dict.fromkeys((
        "false_pass_rate", "false_failure_rate", "repeatability_failure_rate", "scope_leakage_rate",
        "irrelevant_change_sensitivity_rate"), 0.1),
        "native_invocation_sha256": report["calibration_check"]["native_invocation_sha256"]}
    record = measured_artifact(bound, claim, expected, monkeypatch, invoke)
    artifact = reference(bound.shot, bound.write_report("selected-debt-qualification", record))
    selected = publication.bind_claim(bound.shot, claim=claim, artifact=artifact, check_current=lambda: None)
    assert claim.qualification is None
    assert {key: value for key, value in asdict(selected).items() if key != "qualification"} == {
        key: value for key, value in asdict(claim).items() if key != "qualification"
    }
    composed = provisional_judgment._composition_judge_unit(layer, (decision,), qualified_debt_claims=(selected,))
    assert composed.evaluation.claims == (selected,)
    result = invoke(bound, selected)
    assert result["qualification_verified"] is True
    assert result["acceptance_authorized"] is False
    assert composed.provisional_debt_ids == (decision["debt_id"],)


@pytest.mark.parametrize("field,value", [
    ("id", "different"), ("proposition", "Different proposition"), ("repair_owner", "other-owner"),
    ("subject_roles", ("other",)), ("moments", (11,)), ("property", "different_property"),
])
def test_binding_cannot_repurpose_measured_qualification(bound, qualified, field, value):
    claim, _record = qualified
    artifact = {"path": claim.qualification["artifact"], "sha256": claim.qualification["artifact_sha256"]}
    changed = replace(claim, qualification=None, **{field: value})
    message = "must measure the selected claim" if field == "id" else "semantics or owner"
    with pytest.raises(ValueError, match=message):
        publication.bind_claim(bound.shot, claim=changed, artifact=artifact, check_current=lambda: None)


@pytest.mark.parametrize("failure", ["duplicate", "missing", "owner", "point", "unbound", "untyped"])
def test_composed_group_refuses_ambiguous_or_changed_debt_selection(debt, failure):
    layer, decision, claim = debt
    # A structural selection fixture; it grants no native credential without independent admission.
    selected = replace(claim, qualification={"artifact": "not-a-credential"})
    candidates = (selected,)
    if failure == "duplicate":
        candidates *= 2
    elif failure == "missing":
        decision = {**decision, "axes": ("form", "another_axis")}
    elif failure == "owner":
        candidates = (replace(selected, repair_owner="other-owner"),)
    elif failure == "point":
        candidates = (replace(selected, moments=(11,)),)
    elif failure == "unbound":
        candidates = (claim,)
    else:
        decision = {key: value for key, value in decision.items() if key != "debt_id"}
    with pytest.raises(ValueError, match="selected debt qualification"):
        provisional_judgment._composition_judge_unit(layer, (decision,), qualified_debt_claims=candidates)


@pytest.mark.parametrize("failure", ["selection", "source", "owner"])
def test_binding_refuses_changed_authority_at_return(bound, qualified, failure):
    claim, _record = qualified
    artifact = {"path": claim.qualification["artifact"], "sha256": claim.qualification["artifact_sha256"]}
    calls = 0

    def check():
        nonlocal calls
        calls += 1
        if calls == 3:
            if failure == "selection":
                artifact["sha256"] = "0" * 64
            elif failure == "source":
                (bound.shot / artifact["path"]).write_text("{}")
            else:
                raise ValueError("owning authority changed")

    with pytest.raises(ValueError, match=r"selection changed|digest differs|authority changed"):
        publication.bind_claim(bound.shot, claim=replace(claim, qualification=None), artifact=artifact,
                               check_current=check)


@pytest.mark.parametrize("failure", ["no_debt", "no_units"])
def test_composed_group_cannot_silently_discard_selected_qualification(debt, failure):
    layer, decision, claim = debt
    if failure == "no_units":
        layer.stages = ()
    with pytest.raises(ValueError, match="selected debt qualification requires"):
        provisional_judgment._composition_judge_unit(
            layer, () if failure == "no_debt" else (decision,), qualified_debt_claims=(claim,),
        )
