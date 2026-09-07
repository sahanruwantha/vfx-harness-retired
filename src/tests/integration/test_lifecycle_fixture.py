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

from tests.unit.test_plan_records import _candidate, _declaring, _vis_rows, _write
from tests.unit_attempt_fixtures import pass_unit
from vfx_harness.evaluation import plan_gate
from vfx_harness.evaluation.plan_gate.types import Finding
from vfx_harness.observability import run_artifacts
from vfx_harness.orchestration.authority_capsule_resolution import (
    selected_layer_capsule_digest,
)
from vfx_harness.orchestration.authority_selection import resolve_selected_authority
from vfx_harness.orchestration.authority_state_context import (
    resolve_current_authority_state,
)
from vfx_harness.orchestration.authority_state_store import (
    read_current_bytes,
    read_pending_bytes,
)
from vfx_harness.orchestration.jit_materialization import (
    MATERIALIZATION_SCHEMA,
    MaterializationSelectionConflict,
    finalize_materialization_candidate,
    materialization_finalization_attested,
    materialization_finalization_path,
    publish_materialization,
    revert_materialization,
    stage_candidate_view,
)
from vfx_harness.orchestration.layer_outcome_paths import layer_outcome_path
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
from vfx_harness.orchestration.unit_state import (
    initialize,
    load,
    validate_current,
)
from vfx_harness.orchestration.unit_state_lock import unit_state_path


def _deferred_root(root: Path) -> None:
    """Rewrite the fixture candidate as a schema-5 all-deferred single root layer."""
    (root / "brief.md").write_text(
        "---\nid: lifecycle-fixture\nframes: 240\nfps: 24\n---\n"
        "Final image must hold unchanged from frame 239 to 240.\n"
        "The closing set must be dressed before the hold.\n",
        encoding="utf-8",
    )
    (root / "refs" / "a.png").write_bytes(b"fixture-reference")
    document = json.loads((root / "layers.json").read_text(encoding="utf-8"))
    layer = document["layers"][0]
    layer["execution"] = "jit_deferred"
    layer["stages"] = []
    layer["evidence_domains"] = sorted(
        set(layer.get("evidence_domains") or []) | {"image"}
    )
    layer["jit"] = {
        "depends_on_layers": [],
        "required_outcomes": [],
        "reserved_roles": ["comp"],
        "owned_requirements": ["R-final-lock"],
        "provides": {"camera": ["comp"]},
    }
    # a later deferred layer reserves `set.*` — the namespace the root's persistent
    # clearance contract observes, exactly the shape a real multi-layer plan has
    document["layers"].append({
        "id": "2", "script": "build/02_set.py", "title": "Set dressing",
        "primary_judge": 240,
        "judge": [{"frame": 239, "ref": "refs/a.png"}, {"frame": 240, "ref": "refs/a.png"}],
        "owns": ["set_dressing"], "reads": "dressed ending",
        "evidence_domains": ["scene"],
        "execution": "jit_deferred", "stages": [],
        "jit": {
            "depends_on_layers": ["1"],
            "required_outcomes": [],
            "reserved_roles": ["set.*"],
            "owned_requirements": ["R-set-dressed"],
            "provides": {},
        },
    })
    document["schema"] = 5
    _write(root / "layers.json", document)
    _write(root / "critic_axes.json", [
        {"key": "final_lock", "desc": "ending is still"},
        {"key": "set_dressing", "desc": "set is dressed"},
    ])
    _write(root / "scene_checks.json", {"schema": 2, "contracts": []})
    requirements = json.loads((root / "requirements.json").read_text(encoding="utf-8"))
    requirements["requirements"][0]["resolution"] = {
        "kind": "deferred_owner", "ids": [], "owner_layer": "1",
        "due": {"kind": "before_layer", "layer": "1"},
        "evidence_domains": ["image"],
    }
    digest = hashlib.sha256((root / "brief.md").read_bytes()).hexdigest()
    requirements["requirements"][0]["citation"] = {
        "source": "brief.md", "sha256": digest, "line_start": 6, "line_end": 6,
    }
    requirements["requirements"].append({
        "id": "R-set-dressed",
        "statement": "the closing set is dressed before the hold",
        "citation": {"source": "brief.md", "sha256": digest, "line_start": 7, "line_end": 7},
        "resolution": {
            "kind": "deferred_owner", "ids": [], "owner_layer": "2",
            "due": {"kind": "before_layer", "layer": "2"},
            "evidence_domains": ["scene"],
        },
    })
    _write(root / "requirements.json", requirements)
    _write(root / "obligations.json", {"schema": "vfx-harness.obligations/v1", "obligations": []})
    (root / "plans" / "ownership_mapping.json").write_text(
        json.dumps({"schema": "vfx-harness.ownership-mapping/v1", "layers": ["1"], "axes": ["final_lock"],
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


def _approve_hold_decision(root: Path, bundle_hash: str) -> None:
    state = root / "state"
    state.mkdir(exist_ok=True)
    (state / "plan-resolutions.jsonl").write_text(
        json.dumps({
            "schema": "vfx-harness.plan-resolutions/v1",
            "bundle_hash": bundle_hash,
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
                    "script_spans": ["build/units/01/lock.py"]},
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
                       }, {
                           "id": "clear-claim",
                           "proposition": "comp keeps clearance from future set geometry",
                           "axis": "final_lock", "property": "path_clearance_min",
                           "subject_roles": ["comp"], "subject_controls": ["hold"],
                           "moments": [239, 240], "kind": "atomic", "required": True,
                           "authority": "executable_required", "repair_owner": "lock",
                           "asserts": "temporal",
                           "evidence": [{"kind": "scene_contract", "id": "comp-clearance"}],
                       }],
                       "composition_context": {
                           "frames": [239, 240],
                           "contract_ids": ["vis-f239", "vis-f240"],
                       }},
        "completion": "all_required_claims_and_protected_contracts_pass",
        "look_capabilities": [],
        "provides": ["camera"],
    }]
    payload = root / "root-jit.json"
    _write(payload, {
        "schema": MATERIALIZATION_SCHEMA,
        "bundle_hash": bundle_hash,
        "base_selection": resolve_selected_authority(root).selection_token.to_dict(),
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
            {
                # compare_roles name geometry OTHER generations' layers will own — the
                # measurement side of a two-sided kind is exempt from mutation-authority
                # closure (run 20260824T232758Z-c12e64: the first honest clearance
                # contract was blocked for selecting the namespaces it must observe)
                "id": "comp-clearance", "kind": "path_clearance_min", "owner_layer": "1",
                "fault_owner": "1", "activates_at": "1", "lifecycle": "persistent",
                "axis": "final_lock", "roles": ["comp"], "compare_roles": ["set.*"],
                "frames": [239, 240], "op": "min", "lo": 0.5,
            },
            *_vis_rows("1", (239, 240)),
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
    layout_a = run_artifacts.create(tmp_path, "gen-a")
    bundle_a = publish_current(tmp_path, layout_a, outcome="clean_with_deferred")
    _approve_hold_decision(tmp_path, bundle_a.content_hash)
    assert "plans/ownership_mapping.json" in bundle_a.artifacts  # membership seam
    assert resolve_current(tmp_path).content_hash == bundle_a.content_hash

    # ── 2 · the candidate preview shows the POST-publication world before publishing ──
    payload = _root_materialization(tmp_path, bundle_a.content_hash)
    preview = prepare_consumer_view(layout_a)
    stage_candidate_view(tmp_path, payload, preview)
    preview_state = preview / "state" / "work-units"
    assert preview_state.is_dir()
    assert not preview_state.is_symlink()
    previewed = plan_gate.run(preview)
    preview_families = {finding.check for finding in previewed.blocking}
    assert "global-preproduction" not in preview_families, plan_gate.report(previewed)
    assert "decision-adoption" not in preview_families, plan_gate.report(previewed)
    staged_contracts = json.loads((preview / "scene_checks.json").read_text(encoding="utf-8"))
    assert any(row.get("id") == "comp-clearance" for row in staged_contracts["contracts"])

    # ── 2b · materialize the dependency-ready root, adopting the approved decision ──
    finalized = finalize_materialization_candidate(
        tmp_path,
        payload,
        prepare_consumer_view(layout_a),
    )
    assert finalized.clean, plan_gate.report(finalized)
    pointer = publish_materialization(tmp_path, payload)
    assert json.loads(pointer.read_text(encoding="utf-8"))["materialized_layers"] == ["1"]
    with pytest.raises(MaterializationSelectionConflict, match="base selection is stale"):
        publish_materialization(tmp_path, payload)

    # ── 3 · the gate accepts the post-materialization view (lifecycle seam) ──
    view = prepare_consumer_view(layout_a)
    result = plan_gate.run(view)
    families = {finding.check for finding in result.blocking}
    assert "global-preproduction" not in families, plan_gate.report(result)
    assert "decision-adoption" not in families, plan_gate.report(result)
    assert "role-selector-closure" not in families, plan_gate.report(result)
    # The unit plan does not exist yet — for a PENDING unit that is the designed
    # state (the build flow generates and gate-attests it), named as an advisory,
    # never blocking: blocking here deadlocked the first unit plan of a fresh-id
    # layer on its sibling's equally-designed absence (run 0b6849).
    jit_plan_findings = [
        finding
        for finding in result.findings
        if finding.check == "hierarchy" and "no just-in-time plan" in finding.what
    ]
    assert jit_plan_findings, plan_gate.report(result)
    assert not any(finding.blocking for finding in jit_plan_findings), plan_gate.report(result)

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
    current_authority = resolve_selected_authority(tmp_path)
    view_plan_hash = selected_layer_capsule_digest(
        tmp_path,
        "1",
        current_authority,
    )
    assert load(tmp_path, "1")["plan_hash"] == view_plan_hash
    initialize(tmp_path, "1", stages, plan_hash=view_plan_hash)
    pass_unit(
        tmp_path,
        "1",
        stages[0],
        stages,
        plan_hash=view_plan_hash,
        selection_token=current_authority.selection_token,
    )
    live_ledger_path = tmp_path / "shot.json"
    live_ledger_bytes = live_ledger_path.read_bytes()
    live_outcome_path = layer_outcome_path(tmp_path, "1")
    live_outcome_path.parent.mkdir(parents=True, exist_ok=True)
    live_outcome_bytes = b'{"layer":"1","status":"passed"}\n'
    live_outcome_path.write_bytes(live_outcome_bytes)
    unrelated_outcome_path = layer_outcome_path(tmp_path, "2")
    unrelated_outcome_bytes = b'{"layer":"2","status":"failed"}\n'
    unrelated_outcome_path.write_bytes(unrelated_outcome_bytes)

    # ── 5b · replacement preview projects, but does not publish, the replan ──
    replacement = json.loads(payload.read_text(encoding="utf-8"))
    replacement_unit = replacement["layer"]["stages"][0]
    replacement_unit["id"] = "lock_v2"
    replacement_unit["title"] = "Replacement lock"
    replacement_unit["plan"] = "plans/01_finish/lock_v2.md"
    replacement_unit["mutates"]["script_spans"] = ["build/units/01/lock_v2.py"]
    for claim in replacement_unit["evaluation"]["claims"]:
        claim["repair_owner"] = "lock_v2"
    replacement["base_selection"] = resolve_selected_authority(
        tmp_path
    ).selection_token.to_dict()
    _write(payload, replacement)
    overlay = revert_materialization(tmp_path, "1", select=False)
    assert overlay is not None
    replacement_preview = prepare_consumer_view(layout_a)
    preview_ledger_path = replacement_preview / "shot.json"
    preview_outcome_path = layer_outcome_path(replacement_preview, "1")
    preview_unrelated_outcome_path = layer_outcome_path(replacement_preview, "2")
    assert not preview_ledger_path.is_symlink()
    assert preview_ledger_path.read_bytes() == live_ledger_bytes
    assert preview_outcome_path.read_bytes() == live_outcome_bytes
    assert preview_unrelated_outcome_path.read_bytes() == unrelated_outcome_bytes
    stage_candidate_view(
        tmp_path,
        payload,
        replacement_preview,
        overlay_root=overlay,
    )
    preview_ledger = json.loads(preview_ledger_path.read_text(encoding="utf-8"))
    assert preview_ledger["milestones"]["1"]["status"] == "pending"
    assert not preview_outcome_path.exists()
    assert preview_unrelated_outcome_path.read_bytes() == unrelated_outcome_bytes
    assert live_ledger_path.read_bytes() == live_ledger_bytes
    assert live_outcome_path.read_bytes() == live_outcome_bytes
    assert unrelated_outcome_path.read_bytes() == unrelated_outcome_bytes
    replacement_result = plan_gate.run(replacement_preview)
    assert not any(
        finding.check == "hierarchy"
        and "work-unit state IDs do not match" in finding.what
        for finding in replacement_result.blocking
    ), plan_gate.report(replacement_result)
    preview_layers = load_layers_from_path(replacement_preview / "layers.json")
    preview_state = load(replacement_preview, "1")
    validate_current(preview_state, "1", preview_layers["1"].stages)
    assert set(preview_state["units"]) == {"lock_v2"}
    assert preview_state["units"]["lock_v2"]["status"] == "pending"
    assert load(tmp_path, "1")["units"]["lock"]["status"] == "passed"
    terminal_result = finalize_materialization_candidate(
        tmp_path,
        payload,
        prepare_consumer_view(layout_a),
        overlay_root=overlay,
    )
    assert terminal_result.clean, plan_gate.report(terminal_result)
    assert materialization_finalization_attested(
        payload,
        bundle_hash=bundle_a.content_hash,
    )
    assert load(tmp_path, "1")["units"]["lock"]["status"] == "passed"
    assert live_ledger_path.read_bytes() == live_ledger_bytes
    assert live_outcome_path.read_bytes() == live_outcome_bytes
    assert unrelated_outcome_path.read_bytes() == unrelated_outcome_bytes

    # ── 6 · generation B supersedes A: stale view inert, sealed state retired with audit ──
    (tmp_path / "plans" / "global.md").write_text("# fixture plan, second generation\n", encoding="utf-8")
    layout_b = run_artifacts.create(tmp_path, "gen-b")
    bundle_b = publish_current(tmp_path, layout_b, outcome="clean_with_deferred")
    assert bundle_b.content_hash != bundle_a.content_hash
    # the A-generation materialized view must not block B's consumers (stale-view seam)
    assert selected_artifact_path(tmp_path, "layers.json") == bundle_b.root / "layers.json"
    assert load(tmp_path, "1") == {}
    coordinator = resolve_current_authority_state(tmp_path)
    assert coordinator is not None
    effect = next(
        row for row in coordinator.proposal.effects if row.layer_id == "1"
    )
    assert effect.effect_kind == "removed"
    assert effect.invalidated_unit_ids == ("lock",)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("candidate", "candidate changed"),
        ("artifact", "candidate view changed"),
        ("marker", "consumer authority changed"),
    ],
)
def test_terminal_gate_attests_no_mutated_candidate_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    message: str,
) -> None:
    """A clean verdict authorizes only the exact candidate snapshot the gate read."""

    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    _candidate(tmp_path)
    _deferred_root(tmp_path)
    layout = run_artifacts.create(tmp_path, f"gate-mutation-{mutation}")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    _approve_hold_decision(tmp_path, bundle.content_hash)
    candidate = _root_materialization(tmp_path, bundle.content_hash)
    consumer_view = prepare_consumer_view(layout)
    real_gate = plan_gate.run

    def mutate_after_gate(folder: Path, **kwargs):
        result = real_gate(folder, **kwargs)
        if mutation == "candidate":
            payload = json.loads(candidate.read_text(encoding="utf-8"))
            payload["post_gate_mutation"] = True
            _write(candidate, payload)
        elif mutation == "artifact":
            (folder / "layers.json").write_text("{}\n", encoding="utf-8")
        else:
            (folder / ".plan-consumer-view.json").write_text("{}\n", encoding="utf-8")
        return result

    monkeypatch.setattr(plan_gate, "run", mutate_after_gate)

    with pytest.raises(MaterializationSelectionConflict, match=message):
        finalize_materialization_candidate(tmp_path, candidate, consumer_view)

    assert not materialization_finalization_path(candidate).exists()


def test_dirty_regate_clears_an_earlier_clean_finalization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A receipt belongs to one gate attempt; a later dirty attempt cannot inherit it."""

    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    _candidate(tmp_path)
    _deferred_root(tmp_path)
    layout = run_artifacts.create(tmp_path, "dirty-regate")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    _approve_hold_decision(tmp_path, bundle.content_hash)
    candidate = _root_materialization(tmp_path, bundle.content_hash)
    first = finalize_materialization_candidate(
        tmp_path,
        candidate,
        prepare_consumer_view(layout),
    )
    assert first.clean
    receipt = materialization_finalization_path(candidate)
    assert receipt.is_file()
    real_gate = plan_gate.run

    def dirty_gate(folder: Path, **kwargs):
        result = real_gate(folder, **kwargs)
        result.findings.append(
            Finding(
                "injected-dirty-gate",
                True,
                "fixture",
                "the later attempt is not clean",
            )
        )
        return result

    monkeypatch.setattr(plan_gate, "run", dirty_gate)
    second = finalize_materialization_candidate(
        tmp_path,
        candidate,
        prepare_consumer_view(layout),
    )

    assert not second.clean
    assert not receipt.exists()


def test_same_semantic_rematerialization_is_a_jit_pointer_noop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Revalidating the same cumulative view does not manufacture a head revision."""

    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    _candidate(tmp_path)
    _deferred_root(tmp_path)
    layout = run_artifacts.create(tmp_path, "jit-semantic-noop")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    _approve_hold_decision(tmp_path, bundle.content_hash)
    candidate = _root_materialization(tmp_path, bundle.content_hash)
    first = finalize_materialization_candidate(
        tmp_path,
        candidate,
        prepare_consumer_view(layout),
    )
    assert first.clean
    pointer = publish_materialization(tmp_path, candidate)
    selected_bytes = pointer.read_bytes()
    coordinator_bytes = read_current_bytes(tmp_path)
    state_bytes = unit_state_path(tmp_path, "1").read_bytes()
    assert coordinator_bytes is not None
    assert read_pending_bytes(tmp_path) is None

    payload = json.loads(candidate.read_text(encoding="utf-8"))
    payload["base_selection"] = resolve_selected_authority(
        tmp_path
    ).selection_token.to_dict()
    _write(candidate, payload)
    overlay = revert_materialization(tmp_path, "1", select=False)
    assert overlay is not None
    second = finalize_materialization_candidate(
        tmp_path,
        candidate,
        prepare_consumer_view(layout),
        overlay_root=overlay,
    )
    assert second.clean

    assert publish_materialization(
        tmp_path,
        candidate,
        overlay_root=overlay,
    ) == pointer
    assert pointer.read_bytes() == selected_bytes
    assert read_current_bytes(tmp_path) == coordinator_bytes
    assert unit_state_path(tmp_path, "1").read_bytes() == state_bytes
    assert read_pending_bytes(tmp_path) is None


def test_semantic_noop_refuses_live_state_drift_after_finalization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A no-op attestation is still an exact state snapshot, not a bypass."""

    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    _candidate(tmp_path)
    _deferred_root(tmp_path)
    layout = run_artifacts.create(tmp_path, "jit-noop-state-drift")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    _approve_hold_decision(tmp_path, bundle.content_hash)
    candidate = _root_materialization(tmp_path, bundle.content_hash)
    first = finalize_materialization_candidate(
        tmp_path,
        candidate,
        prepare_consumer_view(layout),
    )
    assert first.clean
    publish_materialization(tmp_path, candidate)

    payload = json.loads(candidate.read_text(encoding="utf-8"))
    payload["base_selection"] = resolve_selected_authority(
        tmp_path
    ).selection_token.to_dict()
    _write(candidate, payload)
    overlay = revert_materialization(tmp_path, "1", select=False)
    assert overlay is not None
    second = finalize_materialization_candidate(
        tmp_path,
        candidate,
        prepare_consumer_view(layout),
        overlay_root=overlay,
    )
    assert second.clean

    state = load(tmp_path, "1")
    state["updated"] = "2099-01-01T00:00:00+00:00"
    _write(unit_state_path(tmp_path, "1"), state)

    with pytest.raises(
        MaterializationSelectionConflict,
        match="authority-state snapshot differs from terminal finalization",
    ):
        publish_materialization(
            tmp_path,
            candidate,
            overlay_root=overlay,
        )
    assert read_pending_bytes(tmp_path) is None
