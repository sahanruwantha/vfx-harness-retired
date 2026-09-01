"""Real selected-authority path for the public finalization release command."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

from tests.integration.test_lifecycle_fixture import (
    _approve_hold_decision,
    _deferred_root,
    _root_materialization,
)
from tests.unit.test_plan_records import _candidate
from tests.unit_attempt_fixtures import pass_unit
from vfx_harness.agents.builder.layer_artifact import (
    proposed_layer_artifact_sha256,
)
from vfx_harness.domain.brief import load_shot
from vfx_harness.observability import run_artifacts
from vfx_harness.orchestration import unit_state
from vfx_harness.orchestration.authority_capsule_resolution import (
    selected_layer_capsule_digest,
)
from vfx_harness.orchestration.authority_selection import resolve_selected_authority
from vfx_harness.orchestration.authority_state_store import read_current_bytes
from vfx_harness.orchestration.jit_materialization import (
    finalize_materialization_candidate,
    publish_materialization,
)
from vfx_harness.orchestration.layer_finalization_state import (
    claim_layer_finalization,
)
from vfx_harness.orchestration.ledger import load_layers
from vfx_harness.orchestration.plan_authority import (
    prepare_consumer_view,
    publish_current,
)

_EVALUATION_BARRIER = "# fixture evaluated-state barrier\npass"


def _pass_selected_layer(root: Path):
    shot = load_shot(root)
    selected = resolve_selected_authority(root)
    layer = load_layers(shot, selected_authority=selected)["1"]
    plan_hash = selected_layer_capsule_digest(root, layer.id, selected)
    unit_state.initialize(root, layer.id, layer.stages, plan_hash=plan_hash)
    eligible: set[str] = set()
    for unit in layer.stages:
        pass_unit(
            root,
            layer.id,
            unit,
            layer.stages,
            plan_hash=plan_hash,
            eligible_passed=eligible,
            selection_token=selected.selection_token,
        )
        eligible.add(unit.id)
    state = unit_state.load(root, layer.id)
    script_sha256 = proposed_layer_artifact_sha256(
        root,
        (
            (
                unit.id,
                str(state["units"][unit.id]["completion_receipt"]["script_path"]),
            )
            for unit in layer.stages
        ),
        evaluation_barrier=_EVALUATION_BARRIER,
    )
    claim = claim_layer_finalization(
        root,
        layer,
        expected_plan_hash=plan_hash,
        run_id="public-release-fixture",
        layer_script_sha256=script_sha256,
        predecessor_inputs=(),
        selection_token=selected.selection_token,
    )
    return selected, layer, plan_hash, script_sha256, claim


def test_public_cli_releases_real_selected_claim_and_preserves_units(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    _candidate(tmp_path)
    _deferred_root(tmp_path)
    layout = run_artifacts.create(tmp_path, "public-release-authority")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    _approve_hold_decision(tmp_path, bundle.content_hash)
    materialization = _root_materialization(tmp_path, bundle.content_hash)
    finalized = finalize_materialization_candidate(
        tmp_path,
        materialization,
        prepare_consumer_view(layout),
    )
    assert finalized.clean
    publish_materialization(tmp_path, materialization)
    selected, layer, plan_hash, script_sha256, claim = _pass_selected_layer(tmp_path)
    before_state = unit_state.load(tmp_path, layer.id)
    before_units = json.dumps(before_state["units"], sort_keys=True).encode()
    before_scripts = {
        row.script_path: (tmp_path / row.script_path).read_bytes()
        for row in claim.unit_inputs
    }
    before_head = read_current_bytes(tmp_path)
    before_token = selected.selection_token
    evidence = Path("runs/public-release-authority/evidence/process-death.json")
    (tmp_path / evidence).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / evidence).write_text(
        '{"process":"dead-before-terminal"}\n',
        encoding="utf-8",
    )
    command = Path(sys.executable).with_name("vfx")

    completed = subprocess.run(
        [
            str(command),
            "finalizations",
            "release",
            str(tmp_path),
            "--layer",
            layer.id,
            "--claim-id",
            claim.claim_id,
            "--reason",
            "reviewed real process death",
            "--evidence",
            evidence.as_posix(),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    output = json.loads(completed.stdout)
    assert output["release_receipt"]["request"]["claim"] == claim.as_dict()
    review = output["release_receipt"]["request"]["review_evidence"][0]
    assert review["source_locator"] == evidence.as_posix()
    assert (tmp_path / review["locator"]).read_bytes() == (tmp_path / evidence).read_bytes()
    assert hashlib.sha256((tmp_path / review["locator"]).read_bytes()).hexdigest() == review[
        "sha256"
    ]
    released = unit_state.load(tmp_path, layer.id)
    assert released["layer_finalization"]["active_claim"] is None
    assert released["layer_finalization"]["claim_history"][-1]["disposition"] == "released"
    assert json.dumps(released["units"], sort_keys=True).encode() == before_units
    assert all(
        (tmp_path / locator).read_bytes() == payload
        for locator, payload in before_scripts.items()
    )
    assert read_current_bytes(tmp_path) == before_head
    assert resolve_selected_authority(tmp_path).selection_token == before_token

    fresh = claim_layer_finalization(
        tmp_path,
        layer,
        expected_plan_hash=plan_hash,
        run_id="public-release-fresh-claim",
        layer_script_sha256=script_sha256,
        predecessor_inputs=(),
        selection_token=before_token,
    )
    assert fresh.attempt_revision == claim.attempt_revision + 1
    assert fresh.claim_id != claim.claim_id
    assert (
        json.dumps(unit_state.load(tmp_path, layer.id)["units"], sort_keys=True).encode()
        == before_units
    )
