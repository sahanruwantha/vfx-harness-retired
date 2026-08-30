from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest

from vfx_harness.agents import planner
from vfx_harness.observability import run_artifacts


def test_two_pass_seeds_canonical_gate_candidate_from_draft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    draft = tmp_path / "plans" / "global.draft.md"
    draft.parent.mkdir()
    draft.write_text("# exact draft\n", encoding="utf-8")
    final = tmp_path / "plans" / "global.md"
    final.write_text("# stale candidate\n", encoding="utf-8")
    shot = SimpleNamespace(folder=tmp_path)

    async def fake_generate(*args, **kwargs):
        if kwargs.get("verify_draft"):
            assert final.read_bytes() == draft.read_bytes()
            final.write_text("# verified\n", encoding="utf-8")
            return final
        return draft

    monkeypatch.setattr(planner, "load_shot", lambda folder: shot)
    monkeypatch.setattr(planner, "generate_plan", fake_generate)
    monkeypatch.setattr(
        planner.Settings,
        "from_environment",
        lambda **kwargs: SimpleNamespace(planner_model="model"),
    )
    monkeypatch.setattr(
        run_artifacts,
        "ensure",
        lambda *args, **kwargs: SimpleNamespace(scratch=tmp_path / "scratch"),
    )

    async def invoke():
        return await planner.generate_plan_two_pass(
            tmp_path, verify_only=True, workspace=tmp_path
        )

    result = anyio.run(invoke)

    assert result == final
    assert final.read_text(encoding="utf-8") == "# verified\n"
    assert draft.read_text(encoding="utf-8") == "# exact draft\n"


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
            "schema": "vfx-harness.requirements/v1",
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


def test_two_pass_verify_exhaustion_falls_back_to_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Runs 1c18c2/7040c2/270652 each died when verify hit its ceiling, discarding a
    converging candidate the deterministic gate and repair rounds never saw. Exhaustion
    hands the on-disk candidate forward; any other terminal cause still propagates."""
    from vfx_harness.agents.resilience import AgentSessionFailure

    draft = tmp_path / "plans" / "global.draft.md"
    draft.parent.mkdir()
    draft.write_text("# exact draft\n", encoding="utf-8")
    shot = SimpleNamespace(folder=tmp_path)

    async def exhausted_verify(*args, **kwargs):
        if kwargs.get("verify_draft"):
            raise AgentSessionFailure("verify died", "max_turns_exhausted")
        return draft

    monkeypatch.setattr(planner, "load_shot", lambda folder: shot)
    monkeypatch.setattr(planner, "generate_plan", exhausted_verify)
    monkeypatch.setattr(
        planner.Settings,
        "from_environment",
        lambda **kwargs: SimpleNamespace(planner_model="model", plan_verify_max_turns=6),
    )
    monkeypatch.setattr(
        run_artifacts,
        "ensure",
        lambda *args, **kwargs: SimpleNamespace(scratch=tmp_path / "scratch"),
    )

    async def invoke():
        return await planner.generate_plan_two_pass(
            tmp_path, verify_only=True, workspace=tmp_path
        )

    result = anyio.run(invoke)

    assert result == tmp_path / "plans" / "global.md"
    assert result.read_bytes() == draft.read_bytes()

    async def terminally_failing_verify(*args, **kwargs):
        if kwargs.get("verify_draft"):
            raise AgentSessionFailure("billing", "usage_limit")
        return draft

    monkeypatch.setattr(planner, "generate_plan", terminally_failing_verify)

    async def invoke_terminal():
        return await planner.generate_plan_two_pass(
            tmp_path, verify_only=True, workspace=tmp_path
        )

    with pytest.raises(AgentSessionFailure):
        anyio.run(invoke_terminal)


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


def _mark_passed(folder, layer_id: str, *unit_ids: str) -> None:
    import json as _json

    path = folder / "state" / "work-units" / f"layer_{layer_id}.json"
    value = _json.loads(path.read_text(encoding="utf-8"))
    for uid in unit_ids:
        value["units"][uid]["status"] = "passed"
    path.write_text(_json.dumps(value), encoding="utf-8")


def _patch_remat_design(monkeypatch, tmp_path, *, new_units, overlay_name="overlay"):
    """Skip model design; publish `new_units` as the replacement DAG."""
    overlay = tmp_path / overlay_name
    overlay.mkdir(exist_ok=True)
    called = {"materialize": False}

    monkeypatch.setattr(
        "vfx_harness.orchestration.plan_authority.resolve_current",
        lambda folder: SimpleNamespace(root=tmp_path, content_hash="b" * 64),
    )
    monkeypatch.setattr(
        planner,
        "load_layers_from_path",
        lambda path: {"2": SimpleNamespace(id="2", execution="jit_deferred", stages=())},
    )
    monkeypatch.setattr(
        "vfx_harness.orchestration.jit_materialization.revert_materialization",
        lambda *a, **k: overlay,
    )

    async def fake_materialize(*args, **kwargs):
        called["materialize"] = True

    monkeypatch.setattr(planner, "_materialize_deferred_layer", fake_materialize)
    monkeypatch.setattr(
        planner,
        "load_layers",
        lambda shot: {"2": SimpleNamespace(id="2", stages=new_units)},
    )
    hashes = ["old-hash"]

    def fake_plan_hash(folder, **kwargs):
        if hashes:
            return hashes.pop(0)
        return "new-hash"

    monkeypatch.setattr(
        "vfx_harness.orchestration.plan_authority.active_plan_hash",
        fake_plan_hash,
    )
    return called


def test_rematerialize_preserves_accepted_units_whose_digests_match(
    tmp_path, monkeypatch
) -> None:
    """HIR-0052: remat is apply_replan. A passed sibling whose digest still matches
    stays passed; a changed unit and its dependant reopen. The door must not refuse
    because accepted units exist."""
    from tests.architecture.test_staged_architecture import _unit
    from vfx_harness.orchestration.unit_state import initialize, load

    materials = _unit("materials")
    atmosphere = _unit("atmosphere", depends_on=["materials"])
    lighting = _unit("lighting", depends_on=["atmosphere"])
    old_units = (materials, atmosphere, lighting)
    new_atmosphere = _unit(
        "atmosphere", depends_on=["materials"], proposition_suffix=" vis moved"
    )
    new_units = (materials, new_atmosphere, lighting)
    initialize(tmp_path, "2", old_units, plan_hash="old-hash")
    _mark_passed(tmp_path, "2", "materials", "atmosphere", "lighting")
    called = _patch_remat_design(monkeypatch, tmp_path, new_units=new_units)
    shot = SimpleNamespace(folder=tmp_path, id="shot")
    layer = SimpleNamespace(id="2", execution="ready", stages=old_units)

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
    assert refreshed.stages == new_units
    state = load(tmp_path, "2")
    assert state["units"]["materials"]["status"] == "passed"
    assert state["units"]["atmosphere"]["status"] == "pending"
    assert state["units"]["lighting"]["status"] == "pending"
    record = state["replans"][-1]
    assert record["preserved"] == ["materials"]
    assert "atmosphere" in record["changed"]
    assert "lighting" in record["invalidated"]
    assert record.get("discard_accepted") is None
    source = inspect.getsource(planner._rematerialize_layer)
    assert "re-materialization would discard proven work" not in source
    assert "stay unless the replacement" in source


def test_rematerialize_uses_digest_bound_state_after_global_republication(
    tmp_path, monkeypatch
) -> None:
    """A new global bundle makes the prior JIT view inert before remat starts.

    The selected layer can therefore already be deferred or can name the replacement
    units while durable state still names the accepted predecessor DAG. The state hash
    and stored unit digests are the exact old identity; treating the selected row/hash as
    the replan base made run 20260829T083336Z-91fc7b publish then fail.
    """
    from tests.architecture.test_staged_architecture import _unit
    from vfx_harness.orchestration.unit_state import initialize, load

    old_units = (_unit("old_camera"), _unit("old_proxies", depends_on=["old_camera"]))
    new_units = (_unit("aim_target"), _unit("camera_rig", depends_on=["aim_target"]))
    initialize(tmp_path, "2", old_units, plan_hash="durable-old-hash")
    _mark_passed(tmp_path, "2", "old_camera", "old_proxies")
    _patch_remat_design(monkeypatch, tmp_path, new_units=new_units)
    monkeypatch.setattr(
        "vfx_harness.orchestration.plan_authority.active_plan_hash",
        lambda folder, **kwargs: "selected-new-hash",
    )
    shot = SimpleNamespace(folder=tmp_path, id="shot")
    layer = SimpleNamespace(id="2", execution="ready", stages=new_units)

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
    assert state["plan_hash"] == "selected-new-hash"
    assert set(state["units"]) == {"aim_target", "camera_rig"}
    assert {row["status"] for row in state["units"].values()} == {"pending"}
    record = state["replans"][-1]
    assert record["old_plan_hash"] == "durable-old-hash"
    assert record["removed"] == ["old_camera", "old_proxies"]
    assert record["added"] == ["aim_target", "camera_rig"]
    assert record.get("orphaned") is None
    assert {row["id"] for row in state["superseded"][-2:]} == {
        "old_camera",
        "old_proxies",
    }


def test_direct_materialization_reconciles_prior_generation_state(
    tmp_path, monkeypatch
) -> None:
    """HIR-0133: plain plan --layer is the legal first materialization command."""
    from tests.architecture.test_staged_architecture import _unit
    from vfx_harness.orchestration.unit_state import initialize, load

    old_units = (_unit("facade"), _unit("windows", depends_on=["facade"]))
    new_units = (_unit("massing"), _unit("roof", depends_on=["massing"]))
    initialize(tmp_path, "2", old_units, plan_hash="old-generation")
    _mark_passed(tmp_path, "2", "facade", "windows")
    monkeypatch.setattr(
        "vfx_harness.orchestration.plan_authority.active_plan_hash",
        lambda folder, **kwargs: "selected-materialized-view",
    )
    shot = SimpleNamespace(folder=tmp_path, id="shot")
    layer = SimpleNamespace(id="2", stages=new_units)

    changed = planner._reconcile_materialized_layer_state(shot, layer)

    assert changed is True
    state = load(tmp_path, "2")
    assert state["plan_hash"] == "selected-materialized-view"
    assert set(state["units"]) == {"massing", "roof"}
    assert {row["status"] for row in state["units"].values()} == {"pending"}
    record = state["replans"][-1]
    assert record["old_plan_hash"] == "old-generation"
    assert record["removed"] == ["facade", "windows"]
    assert record["added"] == ["massing", "roof"]
    assert record.get("discard_accepted") is None
    assert {row["id"] for row in state["superseded"][-2:]} == {
        "facade",
        "windows",
    }


def test_direct_materialization_reconciliation_is_noop_for_matching_digests(
    tmp_path, monkeypatch
) -> None:
    from tests.architecture.test_staged_architecture import _unit
    from vfx_harness.orchestration.unit_state import initialize, load

    units = (_unit("massing"),)
    initialize(tmp_path, "2", units, plan_hash="old-view-hash")
    before = load(tmp_path, "2")
    monkeypatch.setattr(
        "vfx_harness.orchestration.plan_authority.active_plan_hash",
        lambda folder, **kwargs: "new-combined-view-hash",
    )

    changed = planner._reconcile_materialized_layer_state(
        SimpleNamespace(folder=tmp_path, id="shot"),
        SimpleNamespace(id="2", stages=units),
    )

    assert changed is False
    assert load(tmp_path, "2") == before


def test_rematerialize_unusable_base_does_not_wipe_accepted_units(
    tmp_path, monkeypatch
) -> None:
    """Publication then a failed apply_replan must not supersede accepted seals
    unless the operator passed --discard-accepted."""
    from tests.architecture.test_staged_architecture import _unit
    from vfx_harness.orchestration.unit_state import initialize, load

    units = (_unit("materials"),)
    initialize(tmp_path, "2", units, plan_hash="old-hash")
    _mark_passed(tmp_path, "2", "materials")
    _patch_remat_design(monkeypatch, tmp_path, new_units=units)
    superseded = {"called": False}

    def boom(*args, **kwargs):
        raise ValueError("replan base layer/plan hash does not match active state")

    def capture_supersede(*args, **kwargs):
        superseded["called"] = True
        return {}

    monkeypatch.setattr("vfx_harness.orchestration.unit_state.apply_replan", boom)
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


def test_rematerialize_unusable_base_wipes_only_with_discard_accepted(
    tmp_path, monkeypatch
) -> None:
    """--discard-accepted remains the unusable-base wipe, not the door on remat."""
    from tests.architecture.test_staged_architecture import _unit
    from vfx_harness.orchestration.unit_state import initialize, load

    units = (_unit("materials"),)
    initialize(tmp_path, "2", units, plan_hash="old-hash")
    _mark_passed(tmp_path, "2", "materials")
    _patch_remat_design(monkeypatch, tmp_path, new_units=units)

    def boom(*args, **kwargs):
        raise ValueError("replan base layer/plan hash does not match active state")

    monkeypatch.setattr("vfx_harness.orchestration.unit_state.apply_replan", boom)
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

    anyio.run(invoke)
    state = load(tmp_path, "2")
    assert state["units"] == {}
    assert state["superseded"][-1]["id"] == "materials"
    assert state["superseded"][-1]["status"] == "superseded"


def test_already_deferred_rematerialize_still_runs_the_transaction() -> None:
    """Run 3af3b7 selected a hole; layer 1 is jit_deferred. The next --rematerialize
    must still take _rematerialize_layer (discard, unpublished overlay, apply_replan),
    not unpack a 4-tuple as 3 and skip unit-state movement."""
    import inspect

    from vfx_harness.agents import planner as _planner

    source = inspect.getsource(_planner.generate_layer_plan)
    assert "if rematerialize is not None and layer.execution == \"ready\":" not in source
    assert "if rematerialize is not None:" in source
    assert "_owner, replacing, _evidence = rematerialize" not in source
    remat = inspect.getsource(_planner._rematerialize_layer)
    assert "select=False" in remat
    assert "overlay_root=overlay" in remat
    assert "discard_accepted=discard_accepted" in remat


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
) -> None:
    """Remat6 Read plans/outcomes as a directory, then put extra-frame vis on
    composition_context.frames until max-turns. Kickoff must compile the subset
    rule from the global row and name outcome files, not a directory."""
    bundle_root = tmp_path / "runs" / "r1" / "checkpoints" / "plans" / "bundles" / "abc"
    bundle_root.mkdir(parents=True)
    row = {
        "id": "2", "script": "build/02.py", "title": "Look",
        "primary_judge": 1, "judge": [{"frame": 1, "ref": "refs/a.png"},
                                      {"frame": 240, "ref": "refs/a.png"}],
        "owns": ["look"], "evidence_domains": ["scene"],
        "reads": "authored brief", "execution": "jit_deferred", "stages": [],
        "jit": {
            "depends_on_layers": ["1"],
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
            "schema": "vfx-harness.requirements/v1",
            "requirements": [{"id": "R-look", "statement": "author the look"}],
        }),
        encoding="utf-8",
    )
    outcomes = tmp_path / "plans" / "outcomes"
    outcomes.mkdir(parents=True)
    (outcomes / "01.json").write_text('{"layer": "1", "status": "passed"}\n', encoding="utf-8")
    (outcomes / "02.json").write_text('{"layer": "2", "status": "passed"}\n', encoding="utf-8")
    bundle = SimpleNamespace(root=bundle_root, content_hash="abc123")
    layer = SimpleNamespace(
        id="2",
        title="Look",
        jit=SimpleNamespace(
            depends_on_layers=["1"],
            required_outcomes=(("scene_contract", "upstream-lock"),),
        ),
    )

    kickoff = planner._materialization_kickoff(
        tmp_path, layer, bundle, "out.json", overlay_root=bundle_root
    )

    assert '"layer_judge_frames": [\n  1,\n  240\n ]' in kickoff
    assert "Do not add those frames to" in kickoff
    assert '"layer": "1"' in kickoff
    assert '"status": "passed"' in kickoff
    assert '"layer": "2"' not in kickoff
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
