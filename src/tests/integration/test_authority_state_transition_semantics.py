"""HIR-0171 semantic preservation and revocation transition fixtures."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

import tests.unit.test_layer_finalization_state as finalization_fixtures
from tests.integration.test_authority_receipt_lineage import (
    _commit_document_successor,
    _current_unit_completion_receipt,
    _prepare_same_capsule_successor,
    _published_finalized_root,
    _selected_documents,
)
from tests.integration.test_lifecycle_fixture import (
    _approve_hold_decision,
    _deferred_root,
    _root_materialization,
)
from tests.unit.test_authority_capsules import _global_documents, _materialize
from tests.unit.test_layer_finalization_state import (
    _complete_passed_layer,
    _finalized_binding,
    _layer_from_capsules,
    _pass_layer_units,
)
from tests.unit.test_layer_publication import _write_projections
from tests.unit.test_plan_records import _candidate
from tests.unit_attempt_fixtures import (
    ABSENT_SELECTION_TOKEN,
    freeze_unit,
    pass_unit,
    publish_passed_evaluation,
)
from vfx_harness.agents.builder.layer_finalization_guard import (
    LayerFinalizationAuthorityLost,
)
from vfx_harness.domain.authority_capsules import compile_authority_capsules
from vfx_harness.domain.authority_head_records import (
    AuthoritySelectionTokenProjection,
)
from vfx_harness.domain.layer_finalizations import (
    LayerFinalizationPredecessorInput,
    LayerFinalizationReceipt,
)
from vfx_harness.observability import run_artifacts
from vfx_harness.orchestration import revalidation, unit_state
from vfx_harness.orchestration.authority_capsule_resolution import (
    selected_layer_capsule_digest,
)
from vfx_harness.orchestration.authority_receipt_lineage import (
    require_preserved_unit_completion_authorization,
)
from vfx_harness.orchestration.authority_selection import resolve_selected_authority
from vfx_harness.orchestration.authority_state_context import (
    resolve_current_authority_state,
)
from vfx_harness.orchestration.authority_state_effects import (
    compile_authority_state_effects,
)
from vfx_harness.orchestration.authority_state_transaction import (
    commit_prepared_authority_state_transition,
)
from vfx_harness.orchestration.jit_materialization import (
    finalize_materialization_candidate,
    publish_materialization,
)
from vfx_harness.orchestration.layer_finalization_predecessors import (
    LayerFinalizationPredecessorConflict,
)
from vfx_harness.orchestration.layer_publication import (
    LayerPublicationConflict,
    require_current_layer_publication,
)
from vfx_harness.orchestration.layer_replay_receipts import (
    layer_replay_receipt_locator,
)
from vfx_harness.orchestration.ledger import load_layers_from_path
from vfx_harness.orchestration.plan_authority import (
    prepare_consumer_view,
    publish_current,
    selected_artifact_path,
)
from vfx_harness.orchestration.unit_completion_state import (
    authorize_completed_units_for_layer,
)
from vfx_harness.orchestration.unit_state import load as load_unit_state
from vfx_harness.orchestration.unit_state_claims import (
    claim_ready_unit_for_build,
    claim_ready_unit_for_planning,
    complete_unit_attempt,
)


def _dependent_unit(source: dict, unit_id: str, depends_on: list[str]) -> dict:
    unit = deepcopy(source)
    unit["id"] = unit_id
    unit["title"] = f"Build {unit_id}"
    unit["plan"] = f"plans/01_finish/{unit_id}.md"
    unit["depends_on"] = depends_on
    unit["mutates"]["script_spans"] = [f"build/units/01/{unit_id}.py"]
    for claim in unit["evaluation"]["claims"]:
        claim["id"] = f"{unit_id}-{claim['id']}"
        claim["repair_owner"] = unit_id
    unit["provides"] = []
    return unit


def _pass_current_unit(
    root: Path,
    layer,
    unit_id: str,
    *,
    plan_hash: str,
    selected,
    eligible_passed: set[str],
) -> None:
    """Exercise the claimed unit lifecycle with live coordinator authorization."""

    authorization = authorize_completed_units_for_layer(
        root,
        layer.id,
        layer.stages,
        expected_plan_hash=plan_hash,
        selected_authority=selected,
    )
    planning = claim_ready_unit_for_planning(
        root,
        layer.id,
        unit_id,
        layer.stages,
        expected_plan_hash=plan_hash,
        eligible_passed=eligible_passed,
        completion_authorization=authorization,
        run_id=f"fixture-{layer.id}-{unit_id}",
        selection_token=selected.selection_token,
        reason="fixture dependency closure proved ready",
    )
    building = claim_ready_unit_for_build(
        root,
        layer.id,
        unit_id,
        layer.stages,
        planning,
        expected_plan_hash=plan_hash,
        eligible_passed=eligible_passed,
        completion_authorization=authorization,
        run_id=planning.run_id,
        selection_token=selected.selection_token,
        reason="fixture plan passed its gate",
    )
    unit = next(row for row in layer.stages if row.id == unit_id)
    freeze_unit(
        root,
        layer.id,
        unit,
        building,
        selection_token=selected.selection_token,
    )
    unit_state.transition(
        root,
        layer.id,
        unit.id,
        "evaluating",
        reason="fixture executable evidence ready",
        attempt=building,
        selection_token=selected.selection_token,
    )
    publish_passed_evaluation(
        root,
        layer.id,
        unit,
        building,
        milestone_id=f"{layer.id}@{unit.id}",
    )
    complete_unit_attempt(
        root,
        layer.id,
        unit.id,
        layer.stages,
        building,
        expected_plan_hash=plan_hash,
        selection_token=selected.selection_token,
        reason="fixture evidence accepted",
        evidence=["fixture:canonical-pass", "fixture:completion-debt-clear"],
    )


def test_changed_unit_transition_preserves_only_exact_unchanged_completion(
    tmp_path: Path,
) -> None:
    """A changed sibling reopens its unit-DAG closure, not proven independent work."""

    initial_layer, _initial_terminal, _initial_authority = _published_finalized_root(
        tmp_path
    )
    unchanged_id = initial_layer.stages[0].id
    unchanged_receipt = _current_unit_completion_receipt(
        tmp_path,
        initial_layer.id,
        unchanged_id,
    )

    expanded = _selected_documents(tmp_path)
    layer_row = expanded["layers.json"]["layers"][0]
    original_unit = layer_row["stages"][0]
    changed_unit = _dependent_unit(original_unit, "changed", [unchanged_id])
    dependant_unit = _dependent_unit(changed_unit, "dependant", ["changed"])
    layer_row["stages"].extend([changed_unit, dependant_unit])
    _commit_document_successor(tmp_path, expanded)

    selected = resolve_selected_authority(tmp_path)
    layer = load_layers_from_path(selected_artifact_path(tmp_path, "layers.json"))["1"]
    plan_hash = selected_layer_capsule_digest(tmp_path, layer.id, selected)
    state = load_unit_state(tmp_path, layer.id)
    assert state["units"][unchanged_id]["completion_receipt"] == (
        unchanged_receipt.as_dict()
    )
    assert state["units"][unchanged_id]["status"] == "passed"

    passed = {unchanged_id}
    for unit_id in ("changed", "dependant"):
        _pass_current_unit(
            tmp_path,
            layer,
            unit_id,
            plan_hash=plan_hash,
            eligible_passed=set(passed),
            selected=selected,
        )
        passed.add(unit_id)

    _layer, _guard, _replay, _stored, terminal = _complete_passed_layer(
        tmp_path,
        layer,
        plan_hash=plan_hash,
        selection_token=selected.selection_token,
        revalidation_manifest_factory=lambda: revalidation.input_manifest(
            tmp_path,
            layer,
            blender_version="fixture",
            selected_authority=selected,
        ),
    )
    _write_projections(tmp_path, terminal)
    assert require_current_layer_publication(tmp_path, layer, selected).receipt == terminal

    replacement = _selected_documents(tmp_path)
    replacement_layer = replacement["layers.json"]["layers"][0]
    replacement_changed = next(
        row for row in replacement_layer["stages"] if row["id"] == "changed"
    )
    replacement_changed["title"] = "Changed authority generation"
    _commit_document_successor(tmp_path, replacement)

    successor = resolve_selected_authority(tmp_path)
    context = resolve_current_authority_state(tmp_path)
    assert context is not None
    effect = next(row for row in context.proposal.effects if row.layer_id == layer.id)
    assert effect.effect_kind == "changed"
    assert effect.preserved_finalization_receipt_digest is None
    assert tuple(row.unit_id for row in effect.preserved_units) == (unchanged_id,)
    assert effect.preserved_units[0].completion_receipt_digest == (
        unchanged_receipt.receipt_digest
    )
    assert set(effect.invalidated_unit_ids) == {"changed", "dependant"}

    successor_state = load_unit_state(tmp_path, layer.id)
    assert successor_state["units"][unchanged_id]["status"] == "passed"
    assert successor_state["units"][unchanged_id]["completion_receipt"] == (
        unchanged_receipt.as_dict()
    )
    assert successor_state["units"]["changed"]["status"] == "pending"
    assert successor_state["units"]["dependant"]["status"] == "pending"
    assert successor_state["layer_finalization"]["terminal_receipt"] is None
    assert successor_state["layer_finalization"]["receipt_history"][-1][
        "receipt"
    ] == terminal.as_dict()

    authorization = require_preserved_unit_completion_authorization(
        tmp_path,
        unchanged_receipt,
        successor,
    )
    assert authorization.receipt_digest == unchanged_receipt.receipt_digest
    assert len(authorization.traversed_head_digests) == 2
    successor_layer = load_layers_from_path(
        selected_artifact_path(tmp_path, "layers.json")
    )["1"]
    with pytest.raises(
        LayerPublicationConflict,
        match="no current terminal finalization receipt",
    ):
        require_current_layer_publication(tmp_path, successor_layer, successor)


def _three_finalized_layer_states(tmp_path: Path, capsules):
    layers = tuple(
        _layer_from_capsules(capsules, layer_id) for layer_id in ("3", "1", "2")
    )
    receipts: dict[str, LayerFinalizationReceipt] = {}
    states: dict[str, dict] = {}
    prefix: list[LayerFinalizationPredecessorInput] = []
    for layer in layers:
        plan_hash = capsules.layer(layer.id).capsule_digest
        _pass_layer_units(tmp_path, layer, plan_hash=plan_hash)
        _layer, _guard, _replay, _stored, receipt = _complete_passed_layer(
            tmp_path,
            layer,
            plan_hash=plan_hash,
            selection_token=ABSENT_SELECTION_TOKEN,
            predecessor_inputs=tuple(prefix),
        )
        receipts[layer.id] = receipt
        states[layer.id] = load_unit_state(tmp_path, layer.id)
        prefix.append(
            LayerFinalizationPredecessorInput.mint(
                layer_id=layer.id,
                finalization_receipt_digest=receipt.receipt_digest,
                script_path=receipt.layer_script_path,
                script_sha256=receipt.layer_script_sha256,
            )
        )
    bindings = {
        layer.id: _finalized_binding(capsules, layer, states[layer.id])
        for layer in layers
    }
    return layers, receipts, states, bindings


def test_upstream_change_invalidates_dependent_layer_and_preserves_earlier_branch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cross-layer closure follows dependency edges; an earlier branch stays exact."""

    # This fixture intentionally exercises the pure transition compiler below a
    # selected coordinator. Install the same source-verifying receipt adapter as
    # the layer-state unit suite; the other tests in this module retain live heads.
    finalization_fixtures._lower_boundary_receipt_authority.__wrapped__(monkeypatch)
    global_documents = _global_documents()
    by_layer = {
        row["id"]: row for row in global_documents["layers.json"]["layers"]
    }
    by_requirement = {
        row["id"]: row
        for row in global_documents["requirements.json"]["requirements"]
    }
    # Stable topological tie-breaking puts the independent branch before the
    # upstream/dependent chain, so its cumulative replay prefix does not consume
    # either changed layer.
    global_documents["layers.json"]["layers"] = [
        by_layer["3"],
        by_layer["1"],
        by_layer["2"],
    ]
    global_documents["requirements.json"]["requirements"] = [
        by_requirement["R-sibling"],
        by_requirement["R-camera"],
        by_requirement["R-form"],
    ]
    effective = deepcopy(global_documents)
    _materialize(
        effective,
        layer_id="3",
        unit_id="prop_unit",
        axis="prop",
        role="prop.mass",
        contract_id="prop-count",
        requirement_id="R-sibling",
    )
    _materialize(
        effective,
        layer_id="1",
        unit_id="camera_unit",
        axis="camera",
        role="camera.rig",
        contract_id="camera-count",
        requirement_id="R-camera",
    )
    _materialize(
        effective,
        layer_id="2",
        unit_id="form_unit",
        axis="form",
        role="hall.mass",
        contract_id="hall-count",
        requirement_id="R-form",
    )
    before = compile_authority_capsules(global_documents, effective)
    layers, receipts, states, bindings = _three_finalized_layer_states(
        tmp_path,
        before,
    )

    changed = deepcopy(effective)
    upstream = next(
        row for row in changed["layers.json"]["layers"] if row["id"] == "1"
    )
    upstream["stages"][0]["title"] = "Changed upstream camera authority"
    after = compile_authority_capsules(global_documents, changed)
    projection = compile_authority_state_effects(
        before_capsules=before,
        after_capsules=after,
        states=states,
        prior_bindings=bindings,
        predecessor_head_revision=1,
        before_selection_token=AuthoritySelectionTokenProjection(0, None, 0, None),
        at="2026-09-01T13:00:00Z",
    )
    effects = {row.layer_id: row for row in projection.layers}

    independent = effects["3"]
    assert independent.effect.effect_kind == "unchanged"
    assert independent.effect.preserved_finalization_receipt_digest == (
        receipts["3"].receipt_digest
    )
    assert independent.after_state == states["3"]
    assert independent.after_state["layer_finalization"]["terminal_receipt"] == (
        receipts["3"].as_dict()
    )

    changed_upstream = effects["1"]
    assert changed_upstream.effect.effect_kind == "changed"
    assert changed_upstream.effect.invalidated_downstream_layer_ids == ("2",)
    assert changed_upstream.effect.preserved_finalization_receipt_digest is None
    assert changed_upstream.after_state["layer_finalization"]["terminal_receipt"] is None
    assert changed_upstream.after_state["layer_finalization"]["receipt_history"][-1][
        "receipt"
    ] == receipts["1"].as_dict()

    dependent = effects["2"]
    assert dependent.effect.effect_kind == "changed"
    assert dependent.effect.preserved_units == ()
    assert dependent.effect.invalidated_unit_ids == (layers[2].stages[0].id,)
    assert dependent.after_state["units"][layers[2].stages[0].id]["status"] == (
        "pending"
    )
    assert dependent.after_state["layer_finalization"]["terminal_receipt"] is None
    assert dependent.after_state["layer_finalization"]["receipt_history"][-1][
        "receipt"
    ] == receipts["2"].as_dict()


def _passed_unfinalized_root(root: Path):
    _candidate(root)
    _deferred_root(root)
    reference = root / "refs/f040.png"
    reference.parent.mkdir(parents=True, exist_ok=True)
    reference.write_bytes(b"fixture reference")
    layout = run_artifacts.create(root, "stale-finalization-post-check")
    bundle = publish_current(root, layout, outcome="clean_with_deferred")
    _approve_hold_decision(root, bundle.content_hash)
    candidate = _root_materialization(root, bundle.content_hash)
    result = finalize_materialization_candidate(
        root,
        candidate,
        prepare_consumer_view(layout),
    )
    assert result.clean
    publish_materialization(root, candidate)
    selected = resolve_selected_authority(root)
    layer = load_layers_from_path(selected_artifact_path(root, "layers.json"))["1"]
    plan_hash = selected_layer_capsule_digest(root, layer.id, selected)
    pass_unit(
        root,
        layer.id,
        layer.stages[0],
        layer.stages,
        plan_hash=plan_hash,
        selection_token=selected.selection_token,
    )
    return layout, layer, plan_hash, selected


@pytest.mark.parametrize("boundary", ["replay", "terminal"])
def test_selection_transition_revokes_active_claim_before_stale_postcheck(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    """Prepared replay/judgment output cannot cross a selection transition."""

    _layout, layer, plan_hash, selected = _passed_unfinalized_root(tmp_path)
    observed: dict[str, object] = {}

    if boundary == "replay":
        real_commit = finalization_fixtures.commit_layer_replay_receipt

        def transition_before_replay_postcheck(prepared, guard):
            observed["claim"] = prepared.receipt.claim
            observed["replay_locator"] = layer_replay_receipt_locator(
                prepared.receipt.claim
            )
            commit_prepared_authority_state_transition(
                tmp_path,
                _prepare_same_capsule_successor(tmp_path),
            )
            return real_commit(prepared, guard)

        monkeypatch.setattr(
            finalization_fixtures,
            "commit_layer_replay_receipt",
            transition_before_replay_postcheck,
        )
        expected_error = LayerFinalizationAuthorityLost
    else:
        real_complete = finalization_fixtures.complete_layer_finalization

        def transition_before_terminal_postcheck(*args, **kwargs):
            observed["claim"] = args[1].claim
            commit_prepared_authority_state_transition(
                tmp_path,
                _prepare_same_capsule_successor(tmp_path),
            )
            return real_complete(*args, **kwargs)

        monkeypatch.setattr(
            finalization_fixtures,
            "complete_layer_finalization",
            transition_before_terminal_postcheck,
        )
        expected_error = LayerFinalizationPredecessorConflict

    with pytest.raises(expected_error):
        _complete_passed_layer(
            tmp_path,
            layer,
            plan_hash=plan_hash,
            selection_token=selected.selection_token,
            revalidation_manifest_factory=lambda: revalidation.input_manifest(
                tmp_path,
                layer,
                blender_version="fixture",
                selected_authority=selected,
            ),
        )

    claim = observed["claim"]
    context = resolve_current_authority_state(tmp_path)
    assert context is not None
    effect = next(row for row in context.proposal.effects if row.layer_id == layer.id)
    assert effect.revoked_layer_finalization_claim_id == claim.claim_id
    state = load_unit_state(tmp_path, layer.id)
    assert state["units"][layer.stages[0].id]["status"] == "passed"
    assert state["layer_finalization"]["active_claim"] is None
    assert state["layer_finalization"]["terminal_receipt"] is None
    assert state["layer_finalization"]["claim_history"][-1]["claim"] == (
        claim.as_dict()
    )
    assert state["layer_finalization"]["claim_history"][-1]["disposition"] == "revoked"
    if boundary == "replay":
        assert not (tmp_path / str(observed["replay_locator"])).exists()
