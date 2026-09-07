from __future__ import annotations

import copy
import hashlib
import inspect
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest

from tests.unit.test_plan_records import (
    _candidate as _plan_candidate,
)
from tests.unit.test_plan_records import (
    _materialize_fixture_ready_layer,
)
from tests.unit.test_plan_records import (
    _write as _write_plan_fixture,
)
from tests.unit.test_plan_records import (
    publish_current as _publish_fixture_current,
)
from tests.unit_attempt_fixtures import legacy_apply_replan, pass_unit
from vfx_harness.agents import global_planner, planner
from vfx_harness.agents.global_planning_session import PlanningSweepExhausted
from vfx_harness.agents.planner import kickoff as kickoff_runtime
from vfx_harness.domain.layer_outcomes import SealedLayerOutcome
from vfx_harness.observability import run_artifacts
from vfx_harness.orchestration import unit_state
from vfx_harness.orchestration.authority_selection import resolve_selected_authority
from vfx_harness.orchestration.authority_selection_transaction import (
    AuthoritySelectionToken,
)


def _native_two_pass_setup(tmp_path, monkeypatch):
    final = tmp_path / "plans/global.md"
    final.parent.mkdir(exist_ok=True)
    snapshot = tmp_path / "reports/draft.json"
    snapshot.parent.mkdir(exist_ok=True)
    monkeypatch.setattr(planner, "load_shot", lambda _: SimpleNamespace(folder=tmp_path))
    monkeypatch.setattr(planner.Settings, "from_environment", lambda **_: SimpleNamespace(
        global_planner_model="model", plan_verify_max_turns=6,
    ))
    monkeypatch.setattr(run_artifacts, "ensure", lambda *a, **kw: SimpleNamespace(scratch=tmp_path / "scratch"))

    def archive(_):
        snapshot.write_bytes(final.read_bytes())
        return snapshot

    monkeypatch.setattr(global_planner, "snapshot_candidate", archive)
    return final, snapshot


def test_two_pass_preserves_canonical_candidate_and_external_snapshot(tmp_path, monkeypatch):
    final, snapshot = _native_two_pass_setup(tmp_path, monkeypatch)

    async def generate(*args, **kwargs):
        if kwargs.get("role") == "verify":
            assert kwargs["phase_input"] == snapshot
            assert final.read_bytes() == snapshot.read_bytes() == b"# exact draft\n"
            final.write_text("# verified\n")
        else:
            final.write_text("# exact draft\n")
        return final

    monkeypatch.setattr(planner, "generate_plan", generate)
    result = anyio.run(lambda: planner.generate_plan_two_pass(tmp_path, workspace=tmp_path))
    assert result == final and final.read_text() == "# verified\n"
    assert snapshot.read_text() == "# exact draft\n"
    assert not (final.parent / "global.draft.md").exists()


def test_target_validation_feedback_closes_the_warm_loop(tmp_path: Path) -> None:
    """The materialization schema exists nowhere the session can read; the validator's
    field-precise errors are its only documentation, so they must arrive on every write
    of the target — and only the target."""
    from vfx_harness.agents.plan_guardrails import target_validation_feedback

    target = tmp_path / "jit-layer-1.json"
    errors: list[str] = ["layers[0].stages[0].plan must be a non-empty string"]
    matcher = target_validation_feedback(target, lambda: list(errors))
    hook = matcher.hooks[0]

    async def exercise():
        failing = await hook(
            {"tool_name": "Write", "tool_input": {"file_path": str(target)}}, "t1", None
        )
        errors.clear()
        passing = await hook(
            {"tool_name": "Write", "tool_input": {"file_path": str(target)}}, "t2", None
        )
        other = await hook(
            {"tool_name": "Write", "tool_input": {"file_path": str(tmp_path / "x.json")}},
            "t3", None,
        )
        return failing, passing, other

    failing, passing, other = anyio.run(exercise)

    assert "VALIDATION FAILED" in failing["hookSpecificOutput"]["additionalContext"]
    assert "stages[0].plan" in failing["hookSpecificOutput"]["additionalContext"]
    assert "VALIDATION PASSED" in passing["hookSpecificOutput"]["additionalContext"]
    assert other == {}


def test_materialization_kickoff_carries_row_and_compiled_authority(tmp_path: Path) -> None:
    """Run 20260823T125746Z-9cd0b8: the kickoff named only the bundle hash, so the
    session probed six wrong bundle locations, was denied, reconstructed its layer row
    from prose, and failed structural validation on every field. The kickoff must carry
    the exact row and a bounded compiled authority card."""
    bundle_root = tmp_path / "runs" / "r1" / "checkpoints" / "plans" / "bundles" / "abc"
    bundle_root.mkdir(parents=True)
    row = {
        "id": "1", "script": "build/01_boot.py", "title": "Bootstrap",
        "primary_judge": 1, "judge": [{"frame": 1, "ref": "refs/a.png"}],
        "owns": ["camera_path_fidelity"], "evidence_domains": ["scene"],
        "reads": "authored brief", "execution": "jit_deferred", "stages": [],
    }
    (bundle_root / "layers.json").write_text(
        json.dumps({"schema": 5, "layers": [row]}), encoding="utf-8"
    )
    bundle = SimpleNamespace(root=bundle_root, content_hash="abc123")
    layer = SimpleNamespace(id="1", title="Bootstrap", jit=SimpleNamespace())

    kickoff = planner._materialization_kickoff(
        tmp_path, layer, bundle, "runs/r2/scratch/jit-layer-1.json"
    )

    assert "complete compiled authority card" in kickoff
    assert '"script": "build/01_boot.py"' in kickoff
    assert "state/plan-resolutions.jsonl" not in kickoff
    assert "This layer is a dependency root" in kickoff
    assert "Compiled requirements owned by this layer: none" in kickoff
    assert "Binding structured decisions" in kickoff
    assert "are inert" in kickoff
    assert '"layer_judge_frames": [\n  1\n ]' in kickoff
    assert "composition_context.contract_ids" in kickoff
    assert "path_clearance_min" in kickoff
    # The outer example covers universal fields. Optional composition context is
    # deliberately prose-only because showing it in every unit taught the model to
    # author invalid empty rows; the staging tool carries its closed union schema.
    for field in ('"plan"', '"protects"', '"control_roles"', '"proposition"',
                  '"requirement_bindings"', '"completion"'):
        assert field in kickoff, f"schema example missing {field}"
    assert "Omit `composition_context`" in kickoff
    assert '"producer":"target","interface_id":"target.publish"' in kickoff


def test_materialization_kickoff_lists_only_selected_bundle_decisions(tmp_path: Path) -> None:
    bundle_root = tmp_path / "runs" / "r1" / "checkpoints" / "plans" / "bundles" / "abc"
    bundle_root.mkdir(parents=True)
    row = {
        "id": "1", "script": "build/01_boot.py", "title": "Bootstrap",
        "primary_judge": 1, "judge": [{"frame": 1, "ref": "refs/a.png"}],
        "owns": ["camera_path_fidelity"], "evidence_domains": ["scene"],
        "reads": "authored brief", "execution": "jit_deferred", "stages": [],
        "jit": {"reserved_roles": ["cam_rig"]},
    }
    (bundle_root / "layers.json").write_text(
        json.dumps({"schema": 5, "layers": [row]}), encoding="utf-8"
    )
    (bundle_root / "requirements.json").write_text(
        json.dumps({
            "schema": "vfx-harness.requirements/v2",
            "judgment_debt_definitions": [],
            "judgment_debt_activations": [],
            "requirements": [{"id": "R-look", "statement": "author the look"}],
        }),
        encoding="utf-8",
    )
    state = tmp_path / "state"
    state.mkdir()
    contract = {
        "kind": "keyframe_schedule",
        "roles": ["cam_rig"],
        "samples": [{"frame": 1, "values": {"location": [0, 0, 0]}}],
        "op": "max",
        "hi": 0.001,
    }
    (state / "plan-resolutions.jsonl").write_text(
        json.dumps({
            "schema": "vfx-harness.plan-resolutions/v1",
            "bundle_hash": "other-generation",
            "kind": "assumption",
            "id": "A2",
            "status": "satisfied",
            "decision": "prior-generation falsified spine",
            "values": {"contract": contract},
        })
        + "\n"
        + json.dumps({
            "schema": "vfx-harness.plan-resolutions/v1",
            "bundle_hash": "abc123",
            "kind": "assumption",
            "id": "A-now",
            "status": "satisfied",
            "decision": "selected-bundle spine",
            "values": {"contract": contract},
        })
        + "\n",
        encoding="utf-8",
    )
    bundle = SimpleNamespace(root=bundle_root, content_hash="abc123")
    layer = SimpleNamespace(
        id="1", title="Bootstrap", jit=SimpleNamespace(reserved_roles=["cam_rig"])
    )

    kickoff = planner._materialization_kickoff(
        tmp_path, layer, bundle, "out.json", overlay_root=bundle_root
    )

    assert '"id": "A-now"' in kickoff
    assert "selected-bundle spine" in kickoff
    assert "prior-generation falsified spine" not in kickoff
    assert "A2" not in kickoff


def test_two_pass_verify_budget_scales_with_drafted_layers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fixed six-turn verify cap audited three layers and exhausted before the last
    layers of six-layer drafts (OBS-55). The budget follows the ownership mapping."""
    from vfx_harness.agents.planner import budget

    assert budget.plan_verify_turn_budget(6, 0) == 6
    assert budget.plan_verify_turn_budget(6, 3) == 12
    assert budget.plan_verify_turn_budget(6, 6) == 18
    assert budget.plan_verify_turn_budget(20, 3) == 20
    assert budget.plan_verify_turn_budget(6, 40) == budget.PLAN_VERIFY_TURN_CEILING
    with pytest.raises(ValueError):
        budget.plan_verify_turn_budget(0, 3)

    final, _snapshot = _native_two_pass_setup(tmp_path, monkeypatch)
    (tmp_path / "ownership_mapping.json").write_text(json.dumps({"layers": [{"id": i} for i in range(6)]}))
    seen = []

    async def generate(*args, **kwargs):
        if kwargs.get("role") == "verify":
            seen.append(kwargs["max_turns"])
        final.write_text("# draft\n")
        return final

    monkeypatch.setattr(planner, "generate_plan", generate)
    anyio.run(lambda: planner.generate_plan_two_pass(tmp_path, workspace=tmp_path, max_turns=100))
    assert seen == [18]
    (tmp_path / "ownership_mapping.json").write_text("not json")
    anyio.run(lambda: planner.generate_plan_two_pass(tmp_path, workspace=tmp_path, max_turns=100))
    assert seen == [18, 6]


def test_two_pass_only_verified_budget_exhaustion_retains_candidate(tmp_path, monkeypatch):
    final, _ = _native_two_pass_setup(tmp_path, monkeypatch)
    failure = PlanningSweepExhausted("spent, no pending operation, current workspace")

    async def generate(*args, **kwargs):
        if kwargs.get("role") == "verify":
            raise failure
        final.write_text("# exact draft\n")
        return final

    monkeypatch.setattr(planner, "generate_plan", generate)
    assert anyio.run(lambda: planner.generate_plan_two_pass(tmp_path, workspace=tmp_path)) == final
    assert final.read_text() == "# exact draft\n"
    failure = RuntimeError("unresolved provider failure")
    with pytest.raises(RuntimeError, match="unresolved"):
        anyio.run(lambda: planner.generate_plan_two_pass(tmp_path, workspace=tmp_path))


def test_until_clean_main_exits_three_and_preserves_dirty_plan(tmp_path, monkeypatch) -> None:
    plan = tmp_path / "plans" / "global.md"
    plan.parent.mkdir()
    plan.write_text("# dirty but useful\n", encoding="utf-8")
    shot = SimpleNamespace(folder=tmp_path, id="dirty-plan")

    async def dirty_result(*args, **kwargs):
        return planner.PlanLoopResult(plan, "budget", 2)

    monkeypatch.setattr(planner, "load_shot", lambda folder: shot)
    monkeypatch.setattr(planner, "generate_plan_until_clean", dirty_result)
    monkeypatch.setattr(sys, "argv", ["vfx plan", str(tmp_path), "--until-clean"])
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    monkeypatch.setenv("VFXH_RUN_ID", "planner-dirty")

    with pytest.raises(planner.PlanGateFailure) as raised:
        planner.main()

    assert raised.value.code == 3
    assert plan.read_text(encoding="utf-8") == "# dirty but useful\n"
    layout = run_artifacts.select(tmp_path, "planner-dirty")
    assert layout is not None
    status = json.loads(layout.status.read_text(encoding="utf-8"))
    assert status["state"] == "failed"
    assert status["exit_code"] == 3
    assert not (tmp_path / "plan.provenance.json").exists()


def test_standalone_unit_plan_refuses_before_shot_or_model_spend(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(
        planner.Settings,
        "from_environment",
        lambda **_kwargs: SimpleNamespace(
            planner_model="model",
            global_planner_model="model",
            blender_bin="blender",
        ),
    )
    monkeypatch.setattr(
        planner,
        "load_shot",
        lambda *_args, **_kwargs: pytest.fail("unit refusal reached shot loading"),
    )
    monkeypatch.setattr(
        planner,
        "generate_layer_plan",
        lambda *_args, **_kwargs: pytest.fail("unit refusal reached paid planning"),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["vfx plan", str(tmp_path), "--layer", "1", "--unit", "form"],
    )

    with pytest.raises(SystemExit) as raised:
        planner.main()

    captured = capsys.readouterr()
    assert raised.value.code == 2
    assert "--unit no longer starts paid planning" in captured.err
    assert "vfx build <shot> --layer <id>" in captured.err


def _mark_passed(folder, layer_id: str, units, *unit_ids: str) -> None:
    plan_hash = unit_state.load(folder, layer_id)["plan_hash"]
    by_id = {unit.id: unit for unit in units}
    eligible_passed: set[str] = set()
    for unit_id in unit_ids:
        pass_unit(
            folder,
            layer_id,
            by_id[unit_id],
            units,
            plan_hash=plan_hash,
            eligible_passed=eligible_passed,
        )
        eligible_passed.add(unit_id)


def _patch_remat_design(
    monkeypatch,
    tmp_path,
    *,
    base_units,
    new_units,
    overlay_name="overlay",
):
    """Skip model design; publish `new_units` as the replacement DAG."""
    overlay = tmp_path / overlay_name
    overlay.mkdir(exist_ok=True)
    called = {"materialize": False}

    base_layers = tmp_path / "base-selected-layers.json"
    published_layers = tmp_path / "published-selected-layers.json"
    base_layers.write_text('{"generation":"base"}\n', encoding="utf-8")
    published_layers.write_text('{"generation":"published"}\n', encoding="utf-8")
    base_token = AuthoritySelectionToken(
        plan_revision=1,
        plan_pointer_sha256=hashlib.sha256(b"plan-head").hexdigest(),
        jit_revision=1,
        jit_pointer_sha256=hashlib.sha256(b"base-jit-head").hexdigest(),
    )
    published_token = AuthoritySelectionToken(
        plan_revision=1,
        plan_pointer_sha256=base_token.plan_pointer_sha256,
        jit_revision=2,
        jit_pointer_sha256=hashlib.sha256(b"published-jit-head").hexdigest(),
    )
    bundle = SimpleNamespace(root=tmp_path, content_hash="b" * 64)
    base_authority = SimpleNamespace(
        plan=SimpleNamespace(bundle=bundle),
        artifact_paths={"layers.json": base_layers},
        selection_token=base_token,
    )
    published_authority = SimpleNamespace(
        plan=SimpleNamespace(bundle=bundle),
        artifact_paths={"layers.json": published_layers},
        selection_token=published_token,
    )
    selections = [base_authority, published_authority]
    monkeypatch.setattr(
        "vfx_harness.agents.planner.rematerialize.resolve_selected_authority",
        lambda _folder: selections.pop(0),
    )
    monkeypatch.setattr(
        "vfx_harness.agents.planner.rematerialize.read_authority_selection_heads",
        lambda _folder: SimpleNamespace(token=published_token),
    )
    monkeypatch.setattr(
        planner,
        "load_layers_from_path",
        lambda path: {"2": SimpleNamespace(id="2", execution="jit_deferred", stages=())},
    )
    monkeypatch.setattr(
        "vfx_harness.agents.planner.rematerialize.revert_materialization",
        lambda *a, **k: overlay,
    )

    new_plan_hash = hashlib.sha256(b"published semantic layer capsule").hexdigest()

    async def fake_materialize(*args, **kwargs):
        called["materialize"] = True
        before = unit_state.load(tmp_path, "2")
        legacy_apply_replan(
            tmp_path,
            "2",
            tuple(base_units),
            tuple(new_units),
            old_plan_hash=str(before["plan_hash"]),
            new_plan_hash=new_plan_hash,
            owner="fixture authority-state publisher",
            trigger="fixture materialization publication",
            evidence=["atomic publication fixture"],
        )

    monkeypatch.setattr(planner, "_materialize_deferred_layer", fake_materialize)
    monkeypatch.setattr(
        "vfx_harness.agents.planner.rematerialize.selected_layer_capsule_digest",
        lambda *_args: new_plan_hash,
    )
    monkeypatch.setattr(
        planner,
        "load_layers",
        lambda shot, *, replacing_layer_id=None, selected_authority=None: {
            "2": SimpleNamespace(
                id="2",
                execution="ready",
                stages=(
                    base_units
                    if selected_authority is base_authority
                    else new_units
                ),
            )
        },
    )
    called["new_plan_hash"] = new_plan_hash
    return called


def _coordinator_remat_unit(
    template: dict,
    unit_id: str,
    *,
    depends_on: list[str],
) -> dict:
    """Return one strict materialization row with an identity-derived script."""

    row = copy.deepcopy(template)
    role = f"comp.{unit_id}"
    control = f"control.{unit_id}"
    contract_id = f"contract.{unit_id}"
    row.update(
        {
            "id": unit_id,
            "title": unit_id.title(),
            "plan": f"plans/01_finish/{unit_id}.md",
            "depends_on": depends_on,
        }
    )
    row["mutates"] = {
        "mode": "scoped",
        "roles": [role],
        "controls": [control],
        "control_roles": {control: [role]},
        "script_spans": [f"build/units/01/{unit_id}.py"],
    }
    claim = row["evaluation"]["claims"][0]
    claim.update(
        {
            "id": f"claim.{unit_id}",
            "proposition": f"{unit_id} preserves the final lock",
            "subject_roles": [role],
            "subject_controls": [control],
            "repair_owner": unit_id,
            "evidence": [{"kind": "scene_contract", "id": contract_id}],
        }
    )
    return row


def _coordinator_remat_contract(template: dict, unit_id: str) -> dict:
    row = copy.deepcopy(template)
    row["id"] = f"contract.{unit_id}"
    return row


def _install_coordinator_remat_fixture(tmp_path: Path):
    """Select and complete a three-unit layer through the real coordinator."""

    _plan_candidate(tmp_path)
    layers_document = json.loads(
        (tmp_path / "layers.json").read_text(encoding="utf-8")
    )
    layer_document = layers_document["layers"][0]
    template_unit = layer_document["stages"][0]
    layer_document["stages"] = [
        _coordinator_remat_unit(template_unit, "materials", depends_on=[]),
        _coordinator_remat_unit(
            template_unit,
            "atmosphere",
            depends_on=["materials"],
        ),
        _coordinator_remat_unit(
            template_unit,
            "lighting",
            depends_on=["atmosphere"],
        ),
    ]
    _write_plan_fixture(tmp_path / "layers.json", layers_document)

    scene_document = json.loads(
        (tmp_path / "scene_checks.json").read_text(encoding="utf-8")
    )
    template_contract = scene_document["contracts"][0]
    scene_contracts = [
        _coordinator_remat_contract(template_contract, unit_id)
        for unit_id in ("materials", "atmosphere", "lighting")
    ]
    scene_document["contracts"] = scene_contracts
    _write_plan_fixture(tmp_path / "scene_checks.json", scene_document)

    requirements = json.loads(
        (tmp_path / "requirements.json").read_text(encoding="utf-8")
    )
    requirements["requirements"][0]["resolution"] = {
        "kind": "deferred_owner",
        "ids": [],
        "owner_layer": "1",
        "due": {"kind": "before_layer", "layer": "1"},
        "evidence_domains": ["image"],
    }
    _write_plan_fixture(tmp_path / "requirements.json", requirements)
    _write_plan_fixture(
        tmp_path / "obligations.json",
        {"schema": "vfx-harness.obligations/v1", "obligations": []},
    )

    layout = run_artifacts.create(tmp_path, "coordinator-rematerialization")
    bundle = _publish_fixture_current(
        tmp_path,
        layout,
        outcome="clean_with_deferred",
    )
    shot = SimpleNamespace(folder=tmp_path, id="shot")
    selected = resolve_selected_authority(tmp_path)
    layer = planner.load_layers(shot, selected_authority=selected)["1"]
    plan_hash = unit_state.load(tmp_path, layer.id)["plan_hash"]
    eligible_passed: set[str] = set()
    for unit in layer.stages:
        pass_unit(
            tmp_path,
            layer.id,
            unit,
            layer.stages,
            plan_hash=plan_hash,
            eligible_passed=eligible_passed,
            selection_token=selected.selection_token,
        )
        eligible_passed.add(unit.id)
    return shot, bundle, layer, layer_document, scene_contracts


def test_rematerialize_preserves_accepted_units_whose_digests_match(
    tmp_path, monkeypatch
) -> None:
    """The real coordinator preserves an unchanged unit's exact historical receipt
    while reopening a changed sibling and that sibling's downstream closure."""
    from vfx_harness.domain.unit_completion_receipts import UnitCompletionReceipt
    from vfx_harness.orchestration.authority_receipt_lineage import (
        require_preserved_unit_completion_authorization,
    )
    from vfx_harness.orchestration.authority_state_context import (
        resolve_current_authority_state,
    )

    shot, bundle, layer, layer_document, scene_contracts = (
        _install_coordinator_remat_fixture(tmp_path)
    )
    before_state = unit_state.load(tmp_path, layer.id)
    before_plan_hash = before_state["plan_hash"]
    materials_receipt = copy.deepcopy(
        before_state["units"]["materials"]["completion_receipt"]
    )
    replacement = copy.deepcopy(layer_document)
    atmosphere = next(
        row for row in replacement["stages"] if row["id"] == "atmosphere"
    )
    atmosphere["evaluation"]["claims"][0]["proposition"] += " after repair"
    called = {"materialize": False}

    async def publish_replacement(*args, overlay_root=None, **kwargs):
        called["materialize"] = True
        assert overlay_root is not None
        _materialize_fixture_ready_layer(
            tmp_path,
            bundle.content_hash,
            replacement,
            scene_contracts,
            [],
            overlay_root=Path(overlay_root),
        )

    monkeypatch.setattr(planner, "_materialize_deferred_layer", publish_replacement)

    async def invoke():
        return await planner._rematerialize_layer(
            shot,
            layer,
            ("operator", "vis-repair-owner", ["evals/plan"], False),
            model="m",
            blender="blender",
            max_turns=4,
        )

    refreshed = anyio.run(invoke)
    assert called["materialize"] is True
    assert [unit.id for unit in refreshed.stages] == [
        "materials",
        "atmosphere",
        "lighting",
    ]
    state = unit_state.load(tmp_path, layer.id)
    assert state["plan_hash"] != before_plan_hash
    assert state["units"]["materials"]["status"] == "passed"
    assert state["units"]["materials"]["completion_receipt"] == materials_receipt
    assert state["units"]["atmosphere"]["status"] == "pending"
    assert state["units"]["lighting"]["status"] == "pending"
    context = resolve_current_authority_state(tmp_path)
    assert context is not None
    effect = next(row for row in context.proposal.effects if row.layer_id == layer.id)
    assert [row.unit_id for row in effect.preserved_units] == ["materials"]
    assert effect.invalidation_seed_unit_ids == ("atmosphere",)
    assert effect.invalidated_unit_ids == ("atmosphere", "lighting")
    selected = resolve_selected_authority(tmp_path)
    receipt = UnitCompletionReceipt.parse(materials_receipt)
    authorization = require_preserved_unit_completion_authorization(
        tmp_path,
        receipt,
        selected,
    )
    assert authorization.receipt_digest == receipt.receipt_digest
    assert authorization.execution_layer_generation_digest == before_plan_hash
    assert authorization.current_layer_generation_digest == state["plan_hash"]
    source = inspect.getsource(planner._rematerialize_layer)
    assert "re-materialization would discard proven work" not in source
    assert "selected_layer_capsule_digest" in source


def test_rematerialize_accepts_atomic_publisher_state_for_a_replaced_dag(
    tmp_path, monkeypatch
) -> None:
    """Rematerialization verifies the state installed by publication for a wholly
    replaced DAG; it does not perform a second post-selection state move."""
    from tests.architecture.test_staged_architecture import _unit
    from vfx_harness.orchestration.unit_state import initialize, load

    old_units = (_unit("old_camera"), _unit("old_proxies", depends_on=["old_camera"]))
    new_units = (_unit("aim_target"), _unit("camera_rig", depends_on=["aim_target"]))
    initialize(tmp_path, "2", old_units, plan_hash="2" * 64)
    _mark_passed(tmp_path, "2", old_units, "old_camera", "old_proxies")
    called = _patch_remat_design(
        monkeypatch,
        tmp_path,
        base_units=old_units,
        new_units=new_units,
    )
    shot = SimpleNamespace(folder=tmp_path, id="shot")
    layer = SimpleNamespace(id="2", execution="ready", stages=old_units)

    async def invoke():
        return await planner._rematerialize_layer(
            shot,
            layer,
            ("operator", "global camera DAG changed", ["runs/plan/plan_gate.json"], False),
            model="m",
            blender="blender",
            max_turns=4,
        )

    anyio.run(invoke)
    state = load(tmp_path, "2")
    assert state["plan_hash"] == called["new_plan_hash"]
    assert set(state["units"]) == {"aim_target", "camera_rig"}
    assert {row["status"] for row in state["units"].values()} == {"pending"}
    record = state["replans"][-1]
    assert record["old_plan_hash"] == "2" * 64
    assert record["removed"] == ["old_camera", "old_proxies"]
    assert record["added"] == ["aim_target", "camera_rig"]
    assert record.get("orphaned") is None
    assert {row["id"] for row in state["superseded"][-2:]} == {
        "old_camera",
        "old_proxies",
    }


def test_direct_materialization_delegates_state_movement_to_atomic_publication() -> None:
    """There is no post-selection reconciliation seam: the coordinator commit is the
    sole owner of selected authority and durable work-unit state."""
    from vfx_harness.orchestration.jit_materialization import publish as jit_publish

    materialize = inspect.getsource(planner._materialize_deferred_layer)
    publication = inspect.getsource(jit_publish.publish_materialization)

    assert "publish_materialization(" in materialize
    assert "_reconcile_materialized_layer_state" not in materialize
    assert not hasattr(planner, "_reconcile_materialized_layer_state")
    assert "commit_prepared_authority_state_transition_locked" in publication


def test_rematerialize_refuses_an_unusable_publication_state_without_wiping_proof(
    tmp_path, monkeypatch
) -> None:
    """A publisher that cannot project the predecessor state fails before selection;
    rematerialization has no post-publication fallback that can wipe accepted proof."""
    from tests.architecture.test_staged_architecture import _unit
    from vfx_harness.orchestration.unit_state import initialize, load

    units = (_unit("materials"),)
    initialize(tmp_path, "2", units, plan_hash="1" * 64)
    _mark_passed(tmp_path, "2", units, "materials")
    _patch_remat_design(
        monkeypatch,
        tmp_path,
        base_units=units,
        new_units=units,
    )
    superseded = {"called": False}

    def capture_supersede(*args, **kwargs):
        superseded["called"] = True
        return {}

    def reject_projection(*args, **kwargs):
        raise ValueError("replan base layer/plan hash does not match active state")

    monkeypatch.setattr(
        sys.modules[__name__],
        "legacy_apply_replan",
        reject_projection,
    )
    monkeypatch.setattr(
        "vfx_harness.orchestration.unit_state.supersede_layer_units",
        capture_supersede,
    )
    shot = SimpleNamespace(folder=tmp_path, id="shot")
    layer = SimpleNamespace(id="2", execution="ready", stages=units)

    async def invoke():
        return await planner._rematerialize_layer(
            shot,
            layer,
            ("operator", "vis-repair-owner", ["evals/plan"], False),
            model="m",
            blender="blender",
            max_turns=4,
        )

    with pytest.raises(ValueError, match="replan base"):
        anyio.run(invoke)
    assert superseded["called"] is False
    assert load(tmp_path, "2")["units"]["materials"]["status"] == "passed"


def test_discard_accepted_does_not_bypass_atomic_state_projection(
    tmp_path, monkeypatch
) -> None:
    """The legacy flag cannot turn an unprojectable predecessor into a selected
    authority generation; the existing accepted state remains byte-for-byte live."""
    from tests.architecture.test_staged_architecture import _unit
    from vfx_harness.orchestration.unit_state import initialize, load

    units = (_unit("materials"),)
    initialize(tmp_path, "2", units, plan_hash="1" * 64)
    _mark_passed(tmp_path, "2", units, "materials")
    _patch_remat_design(
        monkeypatch,
        tmp_path,
        base_units=units,
        new_units=units,
    )

    def reject_projection(*args, **kwargs):
        raise ValueError("replan base layer/plan hash does not match active state")

    monkeypatch.setattr(
        sys.modules[__name__],
        "legacy_apply_replan",
        reject_projection,
    )

    shot = SimpleNamespace(folder=tmp_path, id="shot")
    layer = SimpleNamespace(id="2", execution="ready", stages=units)

    async def invoke():
        return await planner._rematerialize_layer(
            shot,
            layer,
            ("operator", "vis-repair-owner", ["evals/plan"], True),
            model="m",
            blender="blender",
            max_turns=4,
        )

    with pytest.raises(ValueError, match="replan base"):
        anyio.run(invoke)
    state = load(tmp_path, "2")
    assert state["units"]["materials"]["status"] == "passed"
    assert state["superseded"] == []


def test_already_deferred_rematerialize_still_runs_the_transaction() -> None:
    """Run 3af3b7 selected a hole; layer 1 is jit_deferred. The next --rematerialize
    must still take _rematerialize_layer and publish from an unpublished overlay;
    the atomic publisher, not this wrapper, owns unit-state movement."""
    import inspect

    from vfx_harness.agents import planner as _planner
    from vfx_harness.agents.planner import generate as _generate

    source = inspect.getsource(_generate._generate_layer_plan)
    assert "if rematerialize is not None and layer.execution == \"ready\":" not in source
    assert "if rematerialize is not None:" in source
    assert "_owner, replacing, _evidence = rematerialize" not in source
    remat = inspect.getsource(_planner._rematerialize_layer)
    assert "select=False" in remat
    assert "overlay_root=overlay" in remat
    assert "selected_layer_capsule_digest" in remat
    assert "apply_replan(" not in remat


def test_rematerialization_kickoff_carries_the_replacement_reason(tmp_path: Path) -> None:
    """A replacement designed in ignorance of why its predecessor was discarded repeats
    the predecessor's mistakes: the first re-materialization of layer 1 put the camera
    last and left the faceted housing unowned — both defects being replaced."""
    bundle_root = tmp_path / "b"
    bundle_root.mkdir()
    row = {
        "id": "1", "script": "build/01.py", "title": "T", "primary_judge": 1,
        "judge": [{"frame": 1, "ref": "refs/a.png"}], "owns": ["axis"],
        "evidence_domains": ["scene"], "reads": "brief",
        "execution": "jit_deferred", "stages": [],
    }
    (bundle_root / "layers.json").write_text(
        json.dumps({"schema": 5, "layers": [row]}), encoding="utf-8"
    )
    bundle = SimpleNamespace(root=bundle_root, content_hash="h")
    layer = SimpleNamespace(id="1", title="T", jit=SimpleNamespace())

    plain = planner._materialization_kickoff(tmp_path, layer, bundle, "out.json")
    assert "REPLACING A DISCARDED MATERIALIZATION" not in plain

    replacing = planner._materialization_kickoff(
        tmp_path, layer, bundle, "out.json", "camera must be the dependency root"
    )
    assert replacing.startswith("REPLACING A DISCARDED MATERIALIZATION")
    assert "camera must be the dependency root" in replacing
    assert "do not reproduce the structure being replaced" in replacing


def test_materialization_kickoff_compiles_frame_authority_and_named_outcomes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Remat6 Read plans/outcomes as a directory, then put extra-frame vis on
    composition_context.frames until max-turns. Kickoff must compile the subset
    rule from the global row and name outcome files, not a directory."""
    bundle_root = tmp_path / "runs" / "r1" / "checkpoints" / "plans" / "bundles" / "abc"
    bundle_root.mkdir(parents=True)
    dependency_id = "camera.hero"
    layer_id = "look.final"
    row = {
        "id": layer_id, "script": "build/look.py", "title": "Look",
        "primary_judge": 1, "judge": [{"frame": 1, "ref": "refs/a.png"},
                                      {"frame": 240, "ref": "refs/a.png"}],
        "owns": ["look"], "evidence_domains": ["scene"],
        "reads": "authored brief", "execution": "jit_deferred", "stages": [],
        "jit": {
            "depends_on_layers": [dependency_id],
            "required_outcomes": [{"kind": "scene_contract", "id": "upstream-lock"}],
            "reserved_roles": ["look.*"],
            "owned_requirements": ["R-look"],
        },
    }
    (bundle_root / "layers.json").write_text(
        json.dumps({"schema": 5, "layers": [row]}), encoding="utf-8"
    )
    (bundle_root / "requirements.json").write_text(
        json.dumps({
            "schema": "vfx-harness.requirements/v2",
            "judgment_debt_definitions": [],
            "judgment_debt_activations": [],
            "requirements": [{"id": "R-look", "statement": "author the look"}],
        }),
        encoding="utf-8",
    )
    (tmp_path / "refs").mkdir()
    (tmp_path / "refs" / "a.png").write_bytes(b"named-outcome-reference")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "01.py").write_text(
        "# named predecessor outcome\n",
        encoding="utf-8",
    )
    predecessor = SimpleNamespace(
        id=dependency_id,
        title="Upstream",
        script="build/01.py",
        judges=((1, "refs/a.png"),),
        stages=(),
    )
    monkeypatch.setattr(
        kickoff_runtime,
        "load_layers_from_path",
        lambda _path: {dependency_id: predecessor},
    )
    monkeypatch.setattr(
        kickoff_runtime.layer_publication,
        "require_current_layer_publication",
        lambda *_args, **_kwargs: SimpleNamespace(
            outcome=SealedLayerOutcome(
                layer_id=dependency_id,
                status="passed",
                script="build/01.py",
                receipt_digest="a" * 64,
                evidence=(
                    {
                        "id": "upstream-lock",
                        "kind": "scene_contract",
                        "pass": True,
                    },
                ),
            )
        ),
    )
    bundle = SimpleNamespace(root=bundle_root, content_hash="abc123")
    layer = SimpleNamespace(
        id=layer_id,
        title="Look",
        jit=SimpleNamespace(
            depends_on_layers=[dependency_id],
            required_outcomes=(("scene_contract", "upstream-lock"),),
        ),
    )

    kickoff = planner._materialization_kickoff(
        tmp_path, layer, bundle, "out.json", overlay_root=bundle_root
    )

    assert '"layer_judge_frames": [\n  1,\n  240\n ]' in kickoff
    assert "Do not add those frames to" in kickoff
    assert f'"layer": "{dependency_id}"' in kickoff
    assert '"status": "passed"' in kickoff
    assert f'"layer": "{layer_id}"' not in kickoff
    assert "readable_files" not in kickoff


def test_materialize_deferred_layer_binds_a_transcript() -> None:
    """Remat6's failure loop was unreconstructable: log_message only journals when
    transcript is bound, and materialization never bound."""
    import inspect

    source = inspect.getsource(planner._materialize_deferred_layer)
    assert "transcript.bind" in source
    assert "materialize-layer-" in source
    assert "transcript.unbind" in source
    assert "costlog.bind" in source
