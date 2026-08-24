"""The composed authority lifecycle, hermetic — no model, no Blender.

Every defect of 2026-08-22..24 lived at a SEAM between individually-tested mechanisms,
and the composed path ran only inside paid production runs: unit-plan leak (HIR-0016),
bundle members resolution refused, a superseded view blocking its successor generation,
generation supersession inexpressible for materialization-era state, and gate rules blind
to the materialization lifecycle — materialize→gate→publish had NEVER passed end-to-end.
This fixture drives one synthetic generation through publish → materialize → gate →
unit-plan attestation → unit seal → republication → supersession, asserting each seam.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from tests.unit.test_plan_records import _candidate, _declaring, _write
from vfx_harness.evaluation import plan_gate
from vfx_harness.observability import run_artifacts
from vfx_harness.orchestration.jit_materialization import publish_materialization
from vfx_harness.orchestration.layer_plans import (
    read_work_unit_plan,
    stamp_work_unit_plan,
    validate_work_unit_plan_authority,
)
from vfx_harness.orchestration.plan_authority import (
    prepare_consumer_view,
    publish_current,
    resolve_current,
    selected_artifact_path,
)
from vfx_harness.orchestration.unit_state import apply_replan, initialize, load, transition


def _deferred_root(root: Path) -> None:
    """Rewrite the fixture candidate as a schema-5 all-deferred single root layer."""
    (root / "brief.md").write_text(
        "---\nid: lifecycle-fixture\nframes: 240\nfps: 24\n---\n"
        "Final image must hold unchanged from frame 239 to 240.\n",
        encoding="utf-8",
    )
    (root / "refs" / "a.png").write_bytes(b"fixture-reference")
    document = json.loads((root / "layers.json").read_text(encoding="utf-8"))
    layer = document["layers"][0]
    layer["execution"] = "jit_deferred"
    layer["stages"] = []
    layer["jit"] = {
        "depends_on_layers": [],
        "required_outcomes": [],
        "reserved_roles": ["comp"],
        "owned_requirements": ["R-final-lock"],
    }
    document["schema"] = 5
    _write(root / "layers.json", document)
    _write(root / "scene_checks.json", {"schema": 2, "contracts": []})
    requirements = json.loads((root / "requirements.json").read_text(encoding="utf-8"))
    requirements["requirements"][0]["resolution"] = {
        "kind": "deferred_owner", "ids": [], "owner_layer": "1",
        "due": {"kind": "before_layer", "layer": "1"},
    }
    requirements["requirements"][0]["citation"] = {
        "source": "brief.md",
        "sha256": hashlib.sha256((root / "brief.md").read_bytes()).hexdigest(),
        "line_start": 6,
        "line_end": 6,
    }
    _write(root / "requirements.json", requirements)
    _write(root / "obligations.json", {"schema": "vfx-harness.obligations/v1", "obligations": []})
    (root / "plans" / "ownership_mapping.json").write_text(
        json.dumps({"schema": 1, "layers": ["1"], "axes": ["final_lock"],
                    "resolutions": {}, "blockers": []}) + "\n",
        encoding="utf-8",
    )


_HOLD_CONTRACT = {
    "kind": "keyframe_schedule",
    "roles": ["comp"],
    "samples": [
        {"frame": 239, "values": {"location": [0, 0, 0]}},
        {"frame": 240, "values": {"location": [0, 0, 0]}},
    ],
    "op": "max",
    "hi": 0.001,
}


def _approve_hold_decision(root: Path) -> None:
    state = root / "state"
    state.mkdir(exist_ok=True)
    (state / "plan-resolutions.jsonl").write_text(
        json.dumps({
            "schema": "vfx-harness.plan-resolutions/v1",
            "bundle_hash": "prior-bundle",
            "kind": "assumption",
            "id": "A-hold",
            "status": "satisfied",
            "evidence": [{"kind": "human_decision", "id": "user-approved-hold"}],
            "decision": "approved exact final hold",
            "values": {"contract": _HOLD_CONTRACT},
        }) + "\n",
        encoding="utf-8",
    )


def _root_materialization(root: Path, bundle_hash: str) -> Path:
    base = json.loads((root / "layers.json").read_text(encoding="utf-8"))["layers"][0]
    layer = {key: value for key, value in base.items() if key != "jit"}
    layer["execution"] = "ready"
    layer["stages"] = [{
        "id": "lock", "title": "Lock", "plan": "plans/01_finish/lock.md",
        "depends_on": [],
        "mutates": {"mode": "scoped", "roles": ["comp"], "controls": ["hold"],
                    "control_roles": {"hold": ["comp"]},
                    "script_spans": ["build/units/01_finish/lock.py"]},
        "protects": {"selector": "all_active_upstream_interfaces",
                     "resolve_to_explicit_ids_at": "freeze"},
        "evaluation": {"primary_judge": 240,
                       "judge": [{"frame": 239, "ref": "refs/a.png"},
                                 {"frame": 240, "ref": "refs/a.png"}],
                       "temporal_evidence": "keyframes",
                       "claims": [{
                           "id": "lock-claim", "proposition": "ending locks",
                           "axis": "final_lock", "property": "frame_delta",
                           "subject_roles": ["comp"], "subject_controls": ["hold"],
                           "moments": [239, 240], "kind": "atomic", "required": True,
                           "authority": "executable_required", "repair_owner": "lock",
                           "asserts": "image",
                           "evidence": [{"kind": "scene_contract", "id": "final-lock"}],
                       }, {
                           "id": "hold-claim", "proposition": "approved hold is keyed exactly",
                           "axis": "final_lock", "property": "keyframe_schedule",
                           "subject_roles": ["comp"], "subject_controls": ["hold"],
                           "moments": [239, 240], "kind": "atomic", "required": True,
                           "authority": "executable_required", "repair_owner": "lock",
                           "asserts": "temporal",
                           "evidence": [{"kind": "scene_contract", "id": "hold-schedule"}],
                       }]},
        "completion": "all_required_claims_and_protected_contracts_pass",
        "look_capabilities": [],
    }]
    payload = root / "root-jit.json"
    _write(payload, {
        "schema": "vfx-harness.jit-layer-materialization/v1",
        "bundle_hash": bundle_hash,
        "layer": _declaring(layer),
        "scene_contracts": [
            {
                "id": "final-lock", "kind": "frame_delta", "owner_layer": "1",
                "fault_owner": "1", "activates_at": "1", "lifecycle": "layer",
                "axis": "final_lock", "frames": [239, 240], "op": "max", "hi": 0.01,
            },
            {
                **_HOLD_CONTRACT,
                "id": "hold-schedule", "decision_id": "A-hold", "owner_layer": "1",
                "fault_owner": "1", "activates_at": "1", "lifecycle": "layer",
                "axis": "final_lock",
            },
        ],
        "image_contracts": [],
        "requirement_bindings": [{
            "requirement_id": "R-final-lock", "contract_ids": ["final-lock"],
        }],
        "acceptance": [],
    })
    return payload


def test_generation_lifecycle_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)

    # ── 1 · publish generation A: sparse all-deferred authority, mapping sealed ──
    _candidate(tmp_path)
    _deferred_root(tmp_path)
    _approve_hold_decision(tmp_path)
    layout_a = run_artifacts.create(tmp_path, "gen-a")
    bundle_a = publish_current(tmp_path, layout_a, outcome="clean_with_deferred")
    assert "plans/ownership_mapping.json" in bundle_a.artifacts  # membership seam
    assert resolve_current(tmp_path).content_hash == bundle_a.content_hash

    # ── 2 · materialize the dependency-ready root, adopting the approved decision ──
    pointer = publish_materialization(tmp_path, _root_materialization(tmp_path, bundle_a.content_hash))
    assert json.loads(pointer.read_text(encoding="utf-8"))["materialized_layers"] == ["1"]

    # ── 3 · the gate accepts the post-materialization view (lifecycle seam) ──
    view = prepare_consumer_view(layout_a)
    result = plan_gate.run(view)
    families = {finding.check for finding in result.blocking}
    assert "global-preproduction" not in families, plan_gate.report(result)
    assert "decision-adoption" not in families, plan_gate.report(result)
    assert any(  # the unit plan does not exist yet — the gate must still demand it
        finding.check == "hierarchy" and "no just-in-time plan" in finding.what
        for finding in result.blocking
    )

    # ── 4 · unit-plan publication: existence is not authority (leak seam) ──
    plan_path = tmp_path / "plans" / "01_finish" / "lock.md"
    plan_path.parent.mkdir(parents=True)
    plan_path.write_text(
        "# Unit lock\n\n" + "Scope, contracts, and stop rule for the lock unit.\n" * 12,
        encoding="utf-8",
    )
    stamp_work_unit_plan(tmp_path, plan_path)  # integrity only — publication unfinished
    with pytest.raises(ValueError, match="no clean-gate attestation"):
        validate_work_unit_plan_authority(tmp_path, plan_path)
    view = prepare_consumer_view(layout_a)
    result = plan_gate.run(view)  # integrity-stamped plan is staged for the gate
    assert not any(
        finding.check == "hierarchy" and "no just-in-time plan" in finding.what
        for finding in result.blocking
    ), plan_gate.report(result)
    stamp_work_unit_plan(
        tmp_path, plan_path, gate={"clean": True, "blocking": 0, "run_id": layout_a.run_id}
    )
    validate_work_unit_plan_authority(tmp_path, plan_path)

    class _Unit:
        plan = "plans/01_finish/lock.md"

    class _Layer:
        id = "1"

    assert "lock unit" in read_work_unit_plan(tmp_path, _Layer(), _Unit())

    # ── 5 · the unit seals under generation A ──
    from vfx_harness.orchestration.ledger import load_layers_from_path

    selected_layers = selected_artifact_path(tmp_path, "layers.json")
    parsed = load_layers_from_path(selected_layers)
    stages = parsed["1"].stages
    assert [unit.id for unit in stages] == ["lock"]
    view_plan_hash = hashlib.sha256(selected_layers.read_bytes()).hexdigest()
    initialize(tmp_path, "1", stages, plan_hash=view_plan_hash)
    for status in ("planning", "building", "frozen", "evaluating", "passed"):
        transition(tmp_path, "1", "lock", status, reason="fixture build")

    # ── 6 · generation B supersedes A: stale view inert, sealed state retired with audit ──
    (tmp_path / "plans" / "global.md").write_text("# fixture plan, second generation\n", encoding="utf-8")
    layout_b = run_artifacts.create(tmp_path, "gen-b")
    bundle_b = publish_current(tmp_path, layout_b, outcome="clean_with_deferred")
    assert bundle_b.content_hash != bundle_a.content_hash
    # the A-generation materialized view must not block B's consumers (stale-view seam)
    assert selected_artifact_path(tmp_path, "layers.json") == bundle_b.root / "layers.json"
    record = apply_replan(
        tmp_path, "1", (), (),
        old_plan_hash=view_plan_hash,
        new_plan_hash=hashlib.sha256((bundle_b.root / "layers.json").read_bytes()).hexdigest(),
        owner="fixture-operator",
        trigger="generation B supersedes A",
        evidence=["gate:gen-b-clean"],
        discard_accepted=True,
    )
    assert record["orphaned"] == ["lock"]
    state = load(tmp_path, "1")
    assert state["units"] == {}
    assert any(row["id"] == "lock" and row["status"] == "superseded" for row in state["superseded"])
