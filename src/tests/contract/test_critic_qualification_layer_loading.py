"""Layer loading reopens measured qualification rather than trusting asserted rates."""

import json
from dataclasses import asdict

import pytest

from tests.contract.test_critic_qualification_admission import bound as bound
from tests.contract.test_critic_qualification_admission import claim_profile as claim_profile
from tests.contract.test_critic_qualification_sets import measured_set as measured_set
from vfx_harness.orchestration import ledger


def test_layer_loader_reopens_all_selected_profile_proofs(bound, measured_set):
    claim, artifact, _selection = measured_set
    judge = [{"frame": 7, "ref": "reference.png"}]
    stage = {
        "id": "form", "title": "Form", "plan": "plans/form.md", "depends_on": [],
        "mutates": {"mode": "scoped", "roles": ["subject"], "controls": [],
                    "script_spans": ["build/units/01/form.py"]},
        "protects": {"selector": "all_active_upstream_interfaces", "resolve_to_explicit_ids_at": "freeze"},
        "evaluation": {"primary_judge": 7, "judge": judge, "temporal_evidence": "none", "claims": [asdict(claim)]},
        "completion": "all_required_claims_and_protected_contracts_pass",
    }
    path = bound.shot / "layers.json"
    path.write_text(json.dumps({"schema": 4, "layers": [{
        "id": "1", "script": "build/01_form.py", "title": "Form", "primary_judge": 7,
        "judge": judge, "owns": ["form"], "reads": "The declared form reads", "stages": [stage],
    }]}))
    layers = ledger.load_layers_from_path(path)
    assert layers["1"].stages[0].evaluation.claims[0] == claim
    record = json.loads((bound.shot / artifact["path"]).read_text())
    proof = record["profiles"][1]["calibration_proof"]
    (bound.shot / proof["evaluation"]["path"]).write_text("{}")
    with pytest.raises(ValueError, match="source digest differs"):
        ledger.load_layers_from_path(path)
