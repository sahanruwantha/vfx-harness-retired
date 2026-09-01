"""Exact selected-authority snapshots at materialization consumer boundaries."""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest

from tests.unit.test_plan_authority import _write_plan
from vfx_harness.agents.plan_tools import materialize_mcp
from vfx_harness.agents.planner import kickoff
from vfx_harness.observability import run_artifacts
from vfx_harness.orchestration import authority_selection, plan_authority
from vfx_harness.orchestration.authority_selection import resolve_selected_authority
from vfx_harness.orchestration.jit_materialization import (
    OVERLAY_ARTIFACTS,
    apply_materialization_patches,
    seed_materialization_candidate,
)
from vfx_harness.orchestration.jit_materialization.overlay_base import (
    write_overlay_base,
)
from vfx_harness.orchestration.layer_plans import (
    stamp_work_unit_plan,
    validate_work_unit_plan_authority,
)


def _deferred_row() -> dict[str, object]:
    return {
        "id": "1",
        "title": "Camera",
        "script": "build/01_camera.py",
        "primary_judge": 1,
        "judge": [{"frame": 1, "ref": "refs/a.png"}],
        "owns": ["camera"],
        "evidence_domains": ["scene"],
        "reads": "camera",
        "execution": "jit_deferred",
        "stages": [],
        "jit": {
            "depends_on_layers": [],
            "required_outcomes": [],
            "reserved_roles": ["camera.rig"],
            "owned_requirements": [],
            "provides": {"camera": ["camera.rig"]},
        },
    }


def _author_deferred_plan(root: Path, *, marker: str) -> None:
    _write_plan(root, marker=marker)
    (root / "refs" / "a.png").write_bytes(b"reference")
    (root / "layers.json").write_text(
        json.dumps({"schema": 5, "layers": [_deferred_row()]}) + "\n",
        encoding="utf-8",
    )
    state = root / "state"
    state.mkdir(exist_ok=True)
    (state / "plan-resolutions.jsonl").write_text("", encoding="utf-8")


def _publish_deferred_plan(root: Path, *, marker: str, run_id: str):
    _author_deferred_plan(root, marker=marker)
    layout = run_artifacts.create(root, run_id)
    bundle = plan_authority.publish_current(
        root,
        layout,
        outcome="clean_with_deferred",
    )
    return layout, bundle, resolve_selected_authority(root)


def _seed_candidate(root: Path, bundle, selected, name: str = "candidate.json") -> Path:
    candidate = root / "scratch" / name
    candidate.parent.mkdir(exist_ok=True)
    seed_materialization_candidate(
        bundle.root,
        candidate,
        layer_id="1",
        bundle_hash=bundle.content_hash,
        base_selection=selected.selection_token,
    )
    return candidate


def test_materialization_validation_resolves_once_and_keeps_one_view(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _layout, bundle, selected = _publish_deferred_plan(
        tmp_path,
        marker="a",
        run_id="plan-a",
    )
    candidate = _seed_candidate(tmp_path, bundle, selected)
    calls = 0

    def resolve_once(_root: Path):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise AssertionError("materialization validation resolved a second generation")
        return selected

    monkeypatch.setattr(materialize_mcp, "resolve_selected_authority", resolve_once)

    inputs = materialize_mcp._materialization_authority_inputs(
        tmp_path,
        candidate,
        overlay_root=None,
    )

    assert calls == 1
    assert inputs.bundle_root == selected.plan.bundle.root
    assert inputs.base_layers == selected.artifact_paths["layers.json"]
    assert inputs.base_scene_checks == selected.artifact_paths["scene_checks.json"]
    assert inputs.base_requirements == selected.artifact_paths["requirements.json"]


def test_candidate_base_rejects_semantic_a_to_b_to_a_selection(
    tmp_path: Path,
) -> None:
    _layout, bundle_a1, selected_a1 = _publish_deferred_plan(
        tmp_path,
        marker="a",
        run_id="plan-a1",
    )
    candidate = _seed_candidate(tmp_path, bundle_a1, selected_a1)
    _publish_deferred_plan(tmp_path, marker="b", run_id="plan-b")
    _layout, bundle_a2, selected_a2 = _publish_deferred_plan(
        tmp_path,
        marker="a",
        run_id="plan-a2",
    )

    assert bundle_a2.content_hash == bundle_a1.content_hash
    assert selected_a2.assertion == selected_a1.assertion
    assert selected_a2.selection_token != selected_a1.selection_token
    with pytest.raises(ValueError, match="candidate base selection is stale"):
        materialize_mcp._materialization_authority_inputs(
            tmp_path,
            candidate,
            overlay_root=None,
        )


def test_overlay_base_rejects_semantic_a_to_b_to_a_selection(
    tmp_path: Path,
) -> None:
    _layout, bundle_a1, selected_a1 = _publish_deferred_plan(
        tmp_path,
        marker="a",
        run_id="plan-a1",
    )
    overlay = tmp_path / "overlay-a1"
    overlay.mkdir()
    for name in OVERLAY_ARTIFACTS:
        (overlay / name).write_bytes(selected_a1.artifact_paths[name].read_bytes())
    write_overlay_base(
        overlay,
        bundle_hash=bundle_a1.content_hash,
        base_selection=selected_a1.selection_token,
    )
    _publish_deferred_plan(tmp_path, marker="b", run_id="plan-b")
    _layout, bundle_a2, selected_a2 = _publish_deferred_plan(
        tmp_path,
        marker="a",
        run_id="plan-a2",
    )
    candidate = _seed_candidate(tmp_path, bundle_a2, selected_a2, "candidate-a2.json")

    assert bundle_a2.content_hash == bundle_a1.content_hash
    with pytest.raises(ValueError, match="overlay base selection is stale"):
        materialize_mcp._materialization_authority_inputs(
            tmp_path,
            candidate,
            overlay_root=overlay,
        )


def test_patch_write_guard_rejects_head_aba_without_mutating_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _layout, bundle_a1, selected_a1 = _publish_deferred_plan(
        tmp_path,
        marker="a",
        run_id="plan-a1",
    )
    candidate = _seed_candidate(tmp_path, bundle_a1, selected_a1)
    inputs = materialize_mcp._materialization_authority_inputs(
        tmp_path,
        candidate,
        overlay_root=None,
    )
    before = candidate.read_bytes()
    _publish_deferred_plan(tmp_path, marker="b", run_id="plan-b")
    _publish_deferred_plan(tmp_path, marker="a", run_id="plan-a2")
    monkeypatch.setattr(
        "vfx_harness.orchestration.jit_materialization.inspect_materialization",
        lambda *_args, **_kwargs: ([], None),
    )

    with pytest.raises(ValueError, match="selection changed before candidate mutation"):
        apply_materialization_patches(
            inputs.bundle_root,
            candidate,
            [("/acceptance", [])],
            expected_bundle_hash=inputs.bundle_hash,
            base_layers_path=inputs.base_layers,
            base_scene_checks_path=inputs.base_scene_checks,
            base_requirements_path=inputs.base_requirements,
            candidate_write_guard=lambda: materialize_mcp._current_selection_guard(
                tmp_path,
                inputs.selected,
            ),
        )

    assert candidate.read_bytes() == before


@pytest.mark.parametrize(
    ("tool_index", "operation_name"),
    ((0, "stage"), (1, "unstage")),
)
def test_stage_and_unstage_tools_guard_candidate_write_with_exact_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool_index: int,
    operation_name: str,
) -> None:
    candidate = tmp_path / "candidate.json"
    candidate.write_text(
        json.dumps(
            {
                "layer": {"stages": []},
                "scene_contracts": [],
                "requirement_bindings": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    selected = SimpleNamespace(selection_token=object())
    authority = SimpleNamespace(selected=selected)
    entered_guard = False

    @contextmanager
    def guard(_folder, observed_selected):
        nonlocal entered_guard
        assert observed_selected is selected
        entered_guard = True
        yield

    def stage_stub(*_args, candidate_write_guard=None, **_kwargs):
        assert candidate_write_guard is not None
        with candidate_write_guard():
            return candidate

    def unstage_stub(*_args, candidate_write_guard=None, **_kwargs):
        assert candidate_write_guard is not None
        with candidate_write_guard():
            return SimpleNamespace(
                unit_id="unit",
                removed_contract_ids=(),
                removed_requirement_ids=(),
            )

    monkeypatch.setattr(
        materialize_mcp,
        "_materialization_authority_inputs",
        lambda *_args, **_kwargs: authority,
    )
    monkeypatch.setattr(materialize_mcp, "_current_selection_guard", guard)
    monkeypatch.setattr(
        materialize_mcp,
        "compile_clustered_mutation_roles",
        lambda unit: unit,
    )
    monkeypatch.setattr(materialize_mcp, "stage_materialization_unit", stage_stub)
    monkeypatch.setattr(materialize_mcp, "unstage_materialization_unit", unstage_stub)

    async def run_sync_immediately(function):
        return function()

    monkeypatch.setattr(
        materialize_mcp.anyio.to_thread,
        "run_sync",
        run_sync_immediately,
    )
    ns = SimpleNamespace(
        materialization_revision_token=None,
        materialization_axis_ids=(),
        materialization_layer_id="1",
        materialization_allowed_provides=frozenset(),
        materialization_requirement_statements={},
    )
    args = (
        {"unit": {"id": "unit"}, "scene_contracts": [], "requirement_bindings": []}
        if operation_name == "stage"
        else {"unit_id": "unit"}
    )

    async def invoke():
        tools = materialize_mcp.register_materialize_tools(
            shot_folder=tmp_path,
            _resolve=lambda *_args: None,
            _keep=lambda *_args: None,
            candidate_materialization=candidate,
            overlay_root=None,
            materialization_write_lock=anyio.Lock(),
            layout=SimpleNamespace(shot=tmp_path),
            ns=ns,
        )
        return await tools[tool_index].handler(args)

    result = anyio.run(invoke)

    assert entered_guard is True
    assert result.get("is_error") is not True


def test_kickoff_resolves_once_and_threads_that_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _layout, bundle, selected = _publish_deferred_plan(
        tmp_path,
        marker="a",
        run_id="plan-a",
    )
    candidate = _seed_candidate(tmp_path, bundle, selected)
    calls = 0

    def resolve_once(_root: Path):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise AssertionError("kickoff resolved a second authority generation")
        return selected

    monkeypatch.setattr(kickoff, "resolve_selected_authority", resolve_once)
    layer = SimpleNamespace(
        id="1",
        title="Camera",
        jit=SimpleNamespace(),
        judges=((1, "refs/a.png"),),
    )

    card = kickoff._materialization_kickoff(
        tmp_path,
        layer,
        bundle,
        candidate.relative_to(tmp_path).as_posix(),
    )

    assert calls == 1
    assert f"Selected bundle hash: {bundle.content_hash}" in card


def test_consumer_view_snapshot_avoids_nested_live_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _author_deferred_plan(tmp_path, marker="ready")
    rel = Path("plans/01_camera/unit.md")
    ready_layers = tmp_path / "selected-ready-layers.json"
    ready_layers.write_text(
        json.dumps(
            {
                "schema": 5,
                "layers": [
                    {
                        **{
                            key: value
                            for key, value in _deferred_row().items()
                            if key != "jit"
                        },
                        "execution": "ready",
                        "stages": [{"id": "unit", "plan": rel.as_posix()}],
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    plan = tmp_path / rel
    layout = run_artifacts.create(tmp_path, "ready-plan")
    plan_authority.publish_current(tmp_path, layout, outcome="clean_with_deferred")
    plan.parent.mkdir(parents=True)
    plan.write_text("# unit\n" + "bounded execution\n" * 20, encoding="utf-8")
    stamp_work_unit_plan(
        tmp_path,
        plan,
        gate={"clean": True, "blocking": 0, "run_id": "ready-plan"},
    )
    selected = resolve_selected_authority(tmp_path)
    # A verified snapshot is the input contract under test. Represent the already-
    # materialized effective layer without mutating the sparse global publication;
    # publication semantics are covered by the JIT transaction suites.
    selected = type(selected)(
        assertion=selected.assertion,
        pointer_observation=selected.pointer_observation,
        selection_token=selected.selection_token,
        plan=selected.plan,
        artifact_paths={**selected.artifact_paths, "layers.json": ready_layers},
    )
    monkeypatch.setattr(
        authority_selection,
        "resolve_selected_authority",
        lambda _root: (_ for _ in ()).throw(AssertionError("consumer view resolved after receiving its snapshot")),
    )
    monkeypatch.setattr(
        plan_authority,
        "resolve_current",
        lambda _root: (_ for _ in ()).throw(AssertionError("unit-plan validation resolved the live pointer")),
    )

    view = plan_authority.prepare_consumer_view(
        layout,
        selected_authority=selected,
    )

    assert (view / rel).resolve() == plan.resolve()
    (tmp_path / "plans" / "current.json").unlink()
    plan.write_text(plan.read_text(encoding="utf-8") + "tampered\n", encoding="utf-8")
    with pytest.raises(ValueError, match="stale or edited"):
        validate_work_unit_plan_authority(
            tmp_path,
            plan,
            selected_authority=selected,
        )
