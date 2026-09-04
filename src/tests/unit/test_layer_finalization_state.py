from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace

import pytest

import vfx_harness.agents.builder.layer_finalization_guard as finalization_guard_module
from tests.architecture.test_staged_architecture import _unit
from tests.unit.test_authority_capsules import _global_documents, _materialize
from tests.unit_attempt_fixtures import (
    ABSENT_SELECTION_TOKEN,
    FIXTURE_AUTHORITY_STATE_HEAD_REF,
    fixture_completion_authorization,
    legacy_apply_replan,
    pass_unit,
    synthetic_completion_authorization_for_receipts,
)
from vfx_harness.agents.builder.authority import AuthorityBoundLedger
from vfx_harness.agents.builder.layer_artifact import (
    commit_layer_artifact,
    discard_layer_artifact,
    prepare_layer_artifact,
    proposed_layer_artifact_sha256,
)
from vfx_harness.agents.builder.layer_finalization_guard import (
    LayerFinalizationAuthorityLost,
    LayerFinalizationClaimGuard,
    LayerFinalizationReceiptGuard,
)
from vfx_harness.domain.authority_capsules import compile_authority_capsules
from vfx_harness.domain.authority_head_records import AuthoritySelectionTokenProjection
from vfx_harness.domain.authority_state_records import (
    AuthorityUnitBinding,
    LayerAuthorityBinding,
)
from vfx_harness.domain.brief import Shot
from vfx_harness.domain.judgment_debt_replay_receipts import (
    ReplayPrefixLayerReceipt,
    ReplayPrefixReceipt,
    ReplayPrefixUnitReceipt,
)
from vfx_harness.domain.layer_finalizations import (
    LAYER_FINALIZATION_PROJECTION_SCHEMA,
    LayerEvaluationReceipt,
    LayerFinalizationPredecessorInput,
    LayerFinalizationReceipt,
    LayerReplayClaimRequirement,
    LayerReplayEvaluationGroupPlan,
    LayerReplayObservation,
    LayerReplayPointObservation,
    LayerReplayReceipt,
    LayerReplayReceiptBinding,
)
from vfx_harness.domain.layer_outcome_projections import LayerOutcomeProjection
from vfx_harness.domain.stop_envelope_primitives import canonical_digest
from vfx_harness.domain.unit_evaluation_receipts import ReplayInputBinding
from vfx_harness.domain.work_units import WorkUnit, dependency_ordered_units
from vfx_harness.orchestration import (
    layer_finalization_publication_authority,
    layer_finalization_release,
    layer_finalization_state,
    unit_state,
)
from vfx_harness.orchestration.authority_selection_transaction import (
    AuthoritySelectionToken,
)
from vfx_harness.orchestration.authority_state_effects import (
    AuthorityStateEffectsError,
    compile_authority_state_effects,
)
from vfx_harness.orchestration.builder_execution_fence import (
    builder_execution_fence,
)
from vfx_harness.orchestration.layer_evaluation_receipts import (
    commit_layer_evaluation_receipt,
    prepare_layer_evaluation_receipt,
)
from vfx_harness.orchestration.layer_finalization_predecessors import (
    require_exact_selected_finalization_predecessors,
)
from vfx_harness.orchestration.layer_finalization_release import (
    LayerFinalizationReleaseConflict,
    release_active_layer_finalization,
)
from vfx_harness.orchestration.layer_finalization_state import (
    LayerFinalizationConflict,
    claim_layer_finalization,
    complete_layer_finalization,
)
from vfx_harness.orchestration.layer_replay_receipts import (
    LayerReplayReceiptConflict,
    commit_layer_replay_receipt,
    layer_replay_receipt_locator,
    prepare_layer_replay_receipt,
)
from vfx_harness.orchestration.ledger import Layer
from vfx_harness.orchestration.unit_completion_authorizations import (
    completion_projection_digest,
)
from vfx_harness.orchestration.unit_completion_state import (
    source_verified_completion_receipt_digests,
)

_PLAN_HASH = "a" * 64
_EVALUATION_BARRIER = "# fixture evaluated-state barrier\npass"


@pytest.fixture(autouse=True)
def _lower_boundary_receipt_authority(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep this state-machine suite below the coordinator integration boundary."""

    def authorize(
        folder,
        layer_id: str,
        _units,
        *,
        expected_plan_hash: str,
        selection_token: AuthoritySelectionToken,
    ):
        with source_verified_completion_receipt_digests(folder) as sourced:
            receipts = {
                unit_id: digest
                for (candidate_layer, unit_id), digest in sourced.items()
                if candidate_layer == str(layer_id)
            }
            authorization = fixture_completion_authorization(
                folder,
                str(layer_id),
                selection_token=selection_token,
            )
            if authorization is None:
                empty = synthetic_completion_authorization_for_receipts(
                    str(layer_id),
                    expected_plan_hash,
                    {},
                    selection_token=selection_token,
                )
                return replace(
                    empty,
                    completion_projection_digest=completion_projection_digest(
                        unit_state.load(folder, str(layer_id))
                    ),
                )
            assert dict(authorization.receipts) == receipts
            return authorization

    monkeypatch.setattr(
        "vfx_harness.orchestration.layer_finalization_state._authorized_unit_receipts",
        authorize,
    )
    monkeypatch.setattr(
        "vfx_harness.orchestration.layer_finalization_state."
        "authorize_completed_units_for_layer",
        lambda folder, layer_id, units, *, expected_plan_hash, selected_authority: (
            authorize(
                folder,
                layer_id,
                units,
                expected_plan_hash=expected_plan_hash,
                selection_token=selected_authority.selection_token,
            )
        ),
    )
    monkeypatch.setattr(
        "vfx_harness.orchestration.layer_finalization_state."
        "resolve_current_authority_state",
        lambda *_args, **_kwargs: SimpleNamespace(
            head_ref=FIXTURE_AUTHORITY_STATE_HEAD_REF,
        ),
    )
    monkeypatch.setattr(
        "vfx_harness.orchestration.layer_finalization_state."
        "require_exact_selected_finalization_predecessors",
        lambda *_args, **_kwargs: (),
    )


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _layer(*, multi: bool = False) -> Layer:
    first = _unit(
        "form",
        script_span="build/units/1/form.py",
    )
    units = (first,)
    if multi:
        second = _unit(
            "look",
            depends_on=[first.id],
            script_span="build/units/1/look.py",
        )
        units = (first, second)
    return Layer(
        id="1",
        script="build/layer_1.py",
        title="Fixture layer",
        judges=((40, "refs/f040.png"),),
        reads="fixture",
        owns=("form",),
        primary_judge=40,
        stages=units,
    )


def _independent_two_layer_capsules():
    global_documents = _global_documents()
    global_documents["layers.json"]["layers"] = [
        global_documents["layers.json"]["layers"][0],
        global_documents["layers.json"]["layers"][2],
    ]
    global_documents["requirements.json"]["requirements"] = [
        global_documents["requirements.json"]["requirements"][0],
        global_documents["requirements.json"]["requirements"][2],
    ]
    effective = deepcopy(global_documents)
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
        layer_id="3",
        unit_id="prop_unit",
        axis="prop",
        role="prop.mass",
        contract_id="prop-count",
        requirement_id="R-sibling",
    )
    return (
        global_documents,
        effective,
        compile_authority_capsules(global_documents, effective),
    )


def _layer_from_capsules(capsules, layer_id: str) -> Layer:
    capsule = capsules.layer(layer_id)
    row = capsule.projection["effective_layer"]
    return Layer(
        id=layer_id,
        script=str(row["script"]),
        title=str(row["title"]),
        judges=tuple(
            (int(judge["frame"]), str(judge["ref"]))
            for judge in row["judge"]
        ),
        reads=str(row["reads"]),
        owns=tuple(str(axis) for axis in row["owns"]),
        primary_judge=int(row["primary_judge"]),
        stages=tuple(
            WorkUnit.parse(
                capsules.unit(layer_id, unit_id).projection["work_unit"]["row"],
                f"independent prefix fixture {layer_id}.{unit_id}",
            )
            for unit_id, _digest_value in capsule.unit_capsule_digests
        ),
    )


def _finalized_binding(capsules, layer: Layer, state: dict) -> LayerAuthorityBinding:
    terminal = state["layer_finalization"]["terminal_receipt"]
    return LayerAuthorityBinding.mint(
        transition_revision=1,
        transition_proposal_digest=_digest(f"fixture binding {layer.id}"),
        selection_token=AuthoritySelectionTokenProjection(0, None, 0, None),
        layer_id=layer.id,
        layer_generation_digest=capsules.layer(layer.id).capsule_digest,
        units=(
            AuthorityUnitBinding.mint(
                unit_id=unit.id,
                unit_generation_digest=capsules.unit(
                    layer.id,
                    unit.id,
                ).capsule_digest,
                completion_receipt_digest=state["units"][unit.id][
                    "completion_receipt"
                ]["receipt_digest"],
            )
            for unit in layer.stages
        ),
        finalization_receipt_digest=terminal["receipt_digest"],
    )


def _finalized_independent_states(folder, capsules, *, omit_prefix: bool = False):
    first = _layer_from_capsules(capsules, "1")
    later = _layer_from_capsules(capsules, "3")
    _pass_layer_units(
        folder,
        first,
        plan_hash=capsules.layer(first.id).capsule_digest,
    )
    _first_layer, _guard, _replay, _stored, first_receipt = (
        _complete_passed_layer(
            folder,
            first,
            plan_hash=capsules.layer(first.id).capsule_digest,
            selection_token=ABSENT_SELECTION_TOKEN,
        )
    )
    _pass_layer_units(
        folder,
        later,
        plan_hash=capsules.layer(later.id).capsule_digest,
    )
    predecessor_inputs = ()
    if not omit_prefix:
        predecessor_inputs = (
            LayerFinalizationPredecessorInput.mint(
                layer_id=first.id,
                finalization_receipt_digest=first_receipt.receipt_digest,
                script_path=first_receipt.layer_script_path,
                script_sha256=first_receipt.layer_script_sha256,
            ),
        )
    _complete_passed_layer(
        folder,
        later,
        plan_hash=capsules.layer(later.id).capsule_digest,
        selection_token=ABSENT_SELECTION_TOKEN,
        predecessor_inputs=predecessor_inputs,
    )
    states = {
        first.id: unit_state.load(folder, first.id),
        later.id: unit_state.load(folder, later.id),
    }
    bindings = {
        first.id: _finalized_binding(capsules, first, states[first.id]),
        later.id: _finalized_binding(capsules, later, states[later.id]),
    }
    return first, later, states, bindings


def _pass_layer_units(
    folder,
    layer: Layer,
    *,
    plan_hash: str = _PLAN_HASH,
    selection_token: AuthoritySelectionToken = ABSENT_SELECTION_TOKEN,
) -> None:
    unit_state.initialize(
        folder,
        layer.id,
        layer.stages,
        plan_hash=plan_hash,
    )
    eligible: set[str] = set()
    for unit in layer.stages:
        pass_unit(
            folder,
            layer.id,
            unit,
            layer.stages,
            plan_hash=plan_hash,
            eligible_passed=eligible,
            selection_token=selection_token,
        )
        eligible.add(unit.id)


@pytest.mark.parametrize("variant", ["omitted", "reordered", "extra"])
def test_claim_rejects_inexact_selected_prefix_before_state_mutation(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    variant: str,
) -> None:
    def selected_layer(layer_id: str) -> Layer:
        return Layer(
            id=layer_id,
            script=f"build/layer_{layer_id}.py",
            title=f"Layer {layer_id}",
            judges=((40, "refs/f040.png"),),
            reads="fixture",
            owns=("form",),
            primary_judge=40,
            stages=(
                _unit(
                    f"unit_{layer_id}",
                    script_span=f"build/units/{layer_id}/unit_{layer_id}.py",
                ),
            ),
        )

    first, second, target, extra = (
        selected_layer(layer_id) for layer_id in ("1", "2", "3", "4")
    )
    _pass_layer_units(tmp_path, target)
    monkeypatch.setattr(
        "vfx_harness.orchestration.layer_finalization_predecessors."
        "selected_layer_chain",
        lambda *_args, **_kwargs: (first, second, target),
    )

    def row(candidate: Layer) -> LayerFinalizationPredecessorInput:
        return LayerFinalizationPredecessorInput.mint(
            layer_id=candidate.id,
            finalization_receipt_digest=_digest(f"receipt {candidate.id}"),
            script_path=candidate.script,
            script_sha256=_digest(f"script {candidate.id}"),
        )

    inputs = {
        "omitted": (row(second),),
        "reordered": (row(second), row(first)),
        "extra": (row(first), row(second), row(extra)),
    }[variant]
    selected = SimpleNamespace(selection_token=ABSENT_SELECTION_TOKEN)
    monkeypatch.setattr(
        layer_finalization_state,
        "require_exact_selected_finalization_predecessors",
        lambda folder, layer_id, rows, *, selection_token: (
            require_exact_selected_finalization_predecessors(
                folder,
                layer_id,
                rows,
                selection_token=selection_token,
                selected_authority=selected,
            )
        ),
    )

    with pytest.raises(ValueError, match="differs from the selected stable DAG"):
        _claim(tmp_path, target, predecessor_inputs=inputs)
    assert (
        unit_state.load(tmp_path, target.id)
        .get("layer_finalization", {})
        .get("active_claim")
        is None
    )


def _claim(
    folder,
    layer: Layer,
    *,
    layer_script_sha256: str | None = None,
    plan_hash: str = _PLAN_HASH,
    selection_token: AuthoritySelectionToken = ABSENT_SELECTION_TOKEN,
    predecessor_inputs: tuple[LayerFinalizationPredecessorInput, ...] = (),
):
    state = unit_state.load(folder, layer.id)
    complete = all(
        isinstance(state["units"][unit.id].get("completion_receipt"), dict)
        for unit in layer.stages
    )
    if complete:
        proposed_sha256 = proposed_layer_artifact_sha256(
            folder,
            (
                (
                    unit.id,
                    str(state["units"][unit.id]["completion_receipt"]["script_path"]),
                )
                for unit in dependency_ordered_units(layer.stages)
            ),
            evaluation_barrier=_EVALUATION_BARRIER,
        )
    else:
        proposed_sha256 = _digest("unavailable fixture composition")
    return claim_layer_finalization(
        folder,
        layer,
        expected_plan_hash=plan_hash,
        run_id="fixture-finalization",
        layer_script_sha256=(
            layer_script_sha256 or proposed_sha256
        ),
        predecessor_inputs=predecessor_inputs,
        selection_token=selection_token,
    )


def _claim_guard(
    folder,
    layer: Layer,
    claim,
    *,
    selection_token: AuthoritySelectionToken = ABSENT_SELECTION_TOKEN,
) -> LayerFinalizationClaimGuard:
    return LayerFinalizationClaimGuard.bind(
        folder,
        claim,
        layer.stages,
        SimpleNamespace(selection_token=selection_token),
    )


def _publish_artifact(folder, guard: LayerFinalizationClaimGuard):
    prepared = prepare_layer_artifact(
        folder,
        guard,
        evaluation_barrier=_EVALUATION_BARRIER,
    )
    sha256 = prepared.sha256
    return commit_layer_artifact(folder, prepared, guard), sha256


def _publish_replay(
    folder,
    guard: LayerFinalizationClaimGuard,
    script_sha256: str,
    *,
    group_index: int = 0,
    planned_group_count: int = 1,
):
    refs = Path(folder) / "refs"
    existing_references = tuple(
        path for path in sorted(refs.glob("*")) if path.is_file()
    )
    if existing_references:
        reference = existing_references[0]
    else:
        reference = refs / "f040.png"
        reference.parent.mkdir(parents=True, exist_ok=True)
        reference.write_bytes(b"fixture reference")
    reference_rel = reference.relative_to(Path(folder)).as_posix()
    payer_units = tuple(
        ReplayPrefixUnitReceipt(
            layer_id=guard.claim.layer_id,
            unit_id=row.unit_id,
            unit_digest=row.unit_digest,
            checkpoint_unit_digest=row.unit_digest,
            script_path=row.script_path,
            script_sha256=row.script_sha256,
            checkpoint_script_sha256=row.script_sha256,
            completion_receipt_digest=row.completion_receipt_digest,
        )
        for row in guard.claim.unit_inputs
    )
    predecessor_layers = tuple(
        ReplayPrefixLayerReceipt(
            layer_id=row.layer_id,
            layer_generation_digest=_digest(f"predecessor plan {row.layer_id}"),
            predecessor_layer_digests=(),
            script_path=row.script_path,
            script_sha256=row.script_sha256,
            dependencies=(),
            units=(
                ReplayPrefixUnitReceipt(
                    layer_id=row.layer_id,
                    unit_id="fixture-unit",
                    unit_digest=_digest(f"predecessor unit {row.layer_id}"),
                    checkpoint_unit_digest=_digest(f"predecessor unit {row.layer_id}"),
                    script_path=f"build/units/{row.layer_id}/fixture-unit.py",
                    script_sha256=_digest(f"predecessor script {row.layer_id}"),
                    checkpoint_script_sha256=_digest(
                        f"predecessor script {row.layer_id}"
                    ),
                    completion_receipt_digest=_digest(
                        f"predecessor completion {row.layer_id}"
                    ),
                ),
            ),
            finalization_receipt_digest=row.finalization_receipt_digest,
        )
        for row in guard.claim.predecessor_inputs
    )
    prefix = ReplayPrefixReceipt(
        (
            *predecessor_layers,
            ReplayPrefixLayerReceipt(
                layer_id=guard.claim.layer_id,
                layer_generation_digest=guard.claim.plan_hash,
                predecessor_layer_digests=(),
                script_path=guard.claim.layer_script_path,
                script_sha256=script_sha256,
                dependencies=(),
                units=payer_units,
                payer_claim_id=guard.claim.claim_id,
            ),
        )
    )
    plan = LayerReplayEvaluationGroupPlan(
        group_index=group_index,
        planned_group_count=planned_group_count,
        requirement_ids=(),
        debt_id=None,
        debt_points=(),
        definition_digest=None,
        activation_digest=None,
        payment_generation_digest=None,
        judge_points=((40, reference_rel),),
        axes=("form",),
        claims=(
            LayerReplayClaimRequirement(
                claim_id="fixture-claim",
                authority="executable_required",
                judge_frames=(40,),
                evidence_ids=("fixture-contract",),
            ),
        ),
        evidence_kind="executable_only",
        render_mode=None,
        render_scale=None,
    )
    evidence = (
        {
            "id": "fixture-contract",
            "metric": "fixture",
            "value": 1,
            "target": "= 1",
            "pass": True,
            "source": "scene_contract",
            "authoritative": True,
            "owner_layer": guard.claim.layer_id,
            "fault_owner": guard.claim.layer_id,
        },
    )
    point = LayerReplayPointObservation.mint(
        plan=plan,
        frame=40,
        ref=reference_rel,
        ref_sha256=hashlib.sha256(reference.read_bytes()).hexdigest(),
        evidence=evidence,
    )
    replay = LayerReplayReceipt.mint(
        claim=guard.claim,
        layer_script_sha256=script_sha256,
        replay_inputs=(
            *(
                ReplayInputBinding.mint(
                    script_path=row.script_path,
                    script_sha256=row.script_sha256,
                )
                for row in guard.claim.predecessor_inputs
            ),
            ReplayInputBinding.mint(
                script_path=guard.claim.layer_script_path,
                script_sha256=script_sha256,
            ),
        ),
        observation=LayerReplayObservation(
            replay_prefix=prefix,
            plan=plan,
            points=(point,),
        ),
        created_at="2026-09-01T10:01:00+00:00",
    )
    prepared = prepare_layer_replay_receipt(folder, replay, guard)
    return replay, commit_layer_replay_receipt(folder, prepared, guard)


def _complete_passed_layer(
    folder,
    layer: Layer,
    *,
    plan_hash: str,
    selection_token: AuthoritySelectionToken,
    predecessor_inputs: tuple[LayerFinalizationPredecessorInput, ...] = (),
    revalidation_manifest_factory=None,
):
    claim = _claim(
        folder,
        layer,
        plan_hash=plan_hash,
        selection_token=selection_token,
        predecessor_inputs=predecessor_inputs,
    )
    claim_guard = _claim_guard(
        folder,
        layer,
        claim,
        selection_token=selection_token,
    )
    _path, script_sha256 = _publish_artifact(folder, claim_guard)
    replay, stored = _publish_replay(folder, claim_guard, script_sha256)
    best = {"round": 0, "mean": 1.0, "render": None}
    replay_point = replay.observation.points[0]
    receipt_canonical = (
        {
            "frame": replay_point.frame,
            "ref": replay_point.ref,
            "verdict": {
                "evidence_kind": "executable_only",
                "pass": True,
                "issues": [],
                "evidence": list(replay_point.evidence),
                "evidence_failures": [],
                "missing_evidence": [],
                "decided_by": "unit_executable_evidence",
                "layer_replay_receipt_digest": replay.receipt_digest,
            },
        },
    )
    manifest: dict = (
        {}
        if revalidation_manifest_factory is None
        else revalidation_manifest_factory()
    )
    if not isinstance(manifest, dict):
        raise ValueError("fixture revalidation manifest factory must return an object")
    authoritative = [
        {
            key: replay_point.evidence[0].get(key)
            for key in (
                "id",
                "metric",
                "value",
                "target",
                "pass",
                "source",
                "owner_layer",
                "fault_owner",
                "activates_at",
                "lifecycle",
            )
        }
    ]
    outcome_projection = LayerOutcomeProjection.from_canonical(
        claim=claim,
        layer_title=layer.title,
        layer_script_path=layer.script,
        final_status="passed",
        best=best,
        revalidation_manifest=manifest,
        canonical=[
            {
                "evidence_kind": "executable_only",
                "frame": replay_point.frame,
                "ref": replay_point.ref,
                "ref_sha256": replay_point.ref_sha256,
                "input_manifest_sha256": canonical_digest(manifest),
                "authoritative": authoritative,
                "authoritative_sha256": canonical_digest(
                    {"authoritative": authoritative}
                ),
                "qualitative_defects": [],
            }
        ],
        receipt_canonical=receipt_canonical,
        blender_version="fixture",
    )
    evaluation = LayerEvaluationReceipt.mint(
        replay_receipts=(
            LayerReplayReceiptBinding.mint(
                locator=stored.locator,
                sha256=stored.sha256,
                receipt=replay,
            ),
        ),
        evaluation_groups=[
            {
                "group_index": 0,
                "result": "passed",
                "requirement_ids": [],
                "debt_id": None,
                "definition_digest": None,
                "activation_digest": None,
                "canonical_start": 0,
                "canonical_end": 1,
                "payment_failures": [],
            }
        ],
        canonical=receipt_canonical,
        created_at="2026-09-01T10:01:30+00:00",
    )
    prepared_evaluation = prepare_layer_evaluation_receipt(
        folder,
        evaluation,
        claim_guard,
    )
    stored_evaluation = commit_layer_evaluation_receipt(
        folder,
        prepared_evaluation,
        claim_guard,
    )
    receipt = LayerFinalizationReceipt.mint(
        evaluation_receipt=evaluation,
        evaluation_receipt_locator=stored_evaluation.locator,
        evaluation_receipt_sha256=stored_evaluation.sha256,
        projection={
            "schema": LAYER_FINALIZATION_PROJECTION_SCHEMA,
            "best": best,
            "blender_version": "fixture",
            "ablation": {"ok": True, "note": "fixture ablation"},
            "revalidation": {
                "schema": "vfx-harness.layer-image-check-revalidation/v1",
                "layer_id": layer.id,
                "source_sha256": None,
                "replacement_sha256": None,
                "replacement_text": None,
                "result": {"kept": 0, "dropped": []},
            },
            "judgment_debts": [],
            "finding": None,
            "outcome": outcome_projection.as_dict(),
            "ledger": {
                "status": "passed",
                "script": replay.claim.layer_script_path,
                "script_sha256": replay.layer_script_sha256,
            },
        },
        completed_at="2026-09-01T10:02:00+00:00",
    )
    complete_layer_finalization(
        folder,
        receipt,
        evaluation,
        layer.stages,
        selection_token=selection_token,
    )
    return layer, claim_guard, replay, stored, receipt


def _finalize(folder, *, multi: bool = False):
    layer = _layer(multi=multi)
    _pass_layer_units(folder, layer)
    return _complete_passed_layer(
        folder,
        layer,
        plan_hash=_PLAN_HASH,
        selection_token=ABSENT_SELECTION_TOKEN,
    )


def test_claim_requires_every_exact_source_verified_unit_receipt(tmp_path) -> None:
    layer = _layer(multi=True)
    unit_state.initialize(
        tmp_path,
        layer.id,
        layer.stages,
        plan_hash=_PLAN_HASH,
    )

    with pytest.raises(LayerFinalizationConflict, match="not a completed executable checkpoint"):
        _claim(tmp_path, layer)

    pass_unit(
        tmp_path,
        layer.id,
        layer.stages[0],
        layer.stages,
        plan_hash=_PLAN_HASH,
        eligible_passed=set(),
    )
    with pytest.raises(LayerFinalizationConflict, match="not a completed executable checkpoint"):
        _claim(tmp_path, layer)

    pass_unit(
        tmp_path,
        layer.id,
        layer.stages[1],
        layer.stages,
        plan_hash=_PLAN_HASH,
        eligible_passed={layer.stages[0].id},
    )
    first_script = (
        tmp_path
        / unit_state.load(tmp_path, layer.id)["units"][layer.stages[0].id][
            "completion_receipt"
        ]["script_path"]
    )
    first_script.write_text("# changed after unit acceptance\n", encoding="utf-8")
    with pytest.raises(ValueError, match="changed after acceptance"):
        _claim(tmp_path, layer)


@pytest.mark.parametrize(
    ("multi", "expected_mode"),
    [
        (False, "singleton_passthrough"),
        (True, "multi_unit_fan_in"),
    ],
)
def test_claim_records_distinct_singleton_and_multi_unit_modes(
    tmp_path,
    multi: bool,
    expected_mode: str,
) -> None:
    layer = _layer(multi=multi)
    _pass_layer_units(tmp_path, layer)

    claim = _claim(tmp_path, layer)

    assert claim.mode == expected_mode
    assert [row.unit_id for row in claim.unit_inputs] == [
        unit.id for unit in layer.stages
    ]


def test_layer_finalization_normalizes_non_topological_authored_unit_order(
    tmp_path,
) -> None:
    form = _unit("form", script_span="build/units/1/form.py")
    look = _unit(
        "look",
        depends_on=[form.id],
        script_span="build/units/1/look.py",
    )
    layer = Layer(
        id="1",
        script="build/layer_1.py",
        title="Reverse-authored fixture layer",
        judges=((40, "refs/f040.png"),),
        reads="fixture",
        owns=("form",),
        primary_judge=40,
        stages=(look, form),
    )
    unit_state.initialize(tmp_path, layer.id, layer.stages, plan_hash=_PLAN_HASH)
    pass_unit(
        tmp_path,
        layer.id,
        form,
        layer.stages,
        plan_hash=_PLAN_HASH,
        eligible_passed=set(),
    )
    pass_unit(
        tmp_path,
        layer.id,
        look,
        layer.stages,
        plan_hash=_PLAN_HASH,
        eligible_passed={form.id},
    )

    _layer_row, _guard, replay, _stored, receipt = _complete_passed_layer(
        tmp_path,
        layer,
        plan_hash=_PLAN_HASH,
        selection_token=ABSENT_SELECTION_TOKEN,
    )

    assert [row.unit_id for row in receipt.claim.unit_inputs] == ["form", "look"]
    assert replay.claim.unit_inputs == receipt.claim.unit_inputs
    assert unit_state.load(tmp_path, layer.id)["layer_finalization"][
        "terminal_receipt"
    ] == receipt.as_dict()


def test_independent_earlier_prefix_preserves_later_terminal_until_prefix_changes(
    tmp_path,
) -> None:
    global_documents, effective, before = _independent_two_layer_capsules()
    first, later, states, bindings = _finalized_independent_states(
        tmp_path,
        before,
    )
    unchanged = compile_authority_state_effects(
        before_capsules=before,
        after_capsules=before,
        states=states,
        prior_bindings=bindings,
        predecessor_head_revision=1,
        before_selection_token=AuthoritySelectionTokenProjection(0, None, 0, None),
        at="2026-09-01T12:00:00Z",
    )
    unchanged_by_layer = {row.layer_id: row for row in unchanged.layers}
    assert (
        unchanged_by_layer[later.id].effect.preserved_finalization_receipt_digest
        == bindings[later.id].finalization_receipt_digest
    )

    changed_documents = deepcopy(effective)
    changed_documents["layers.json"]["layers"][0]["stages"][0][
        "title"
    ] = "Changed independent earlier unit"
    after = compile_authority_capsules(global_documents, changed_documents)
    assert after.layer(later.id).capsule_digest == before.layer(later.id).capsule_digest

    changed = compile_authority_state_effects(
        before_capsules=before,
        after_capsules=after,
        states=states,
        prior_bindings=bindings,
        predecessor_head_revision=1,
        before_selection_token=AuthoritySelectionTokenProjection(0, None, 0, None),
        at="2026-09-01T13:00:00Z",
    )
    changed_by_layer = {row.layer_id: row for row in changed.layers}
    assert changed_by_layer[first.id].effect.effect_kind == "changed"
    assert changed_by_layer[later.id].effect.effect_kind == "unchanged"
    assert changed_by_layer[later.id].effect.preserved_units
    assert changed_by_layer[later.id].effect.preserved_finalization_receipt_digest is None
    assert changed_by_layer[later.id].after_state["units"][
        later.stages[0].id
    ]["status"] == "passed"
    assert changed_by_layer[later.id].after_state["layer_finalization"][
        "terminal_receipt"
    ] is None


def test_independent_later_terminal_cannot_omit_earlier_replay_prefix(
    tmp_path,
) -> None:
    _global_documents_row, _effective, capsules = _independent_two_layer_capsules()
    _first, _later, states, bindings = _finalized_independent_states(
        tmp_path,
        capsules,
        omit_prefix=True,
    )

    with pytest.raises(
        AuthorityStateEffectsError,
        match="stale predecessor inputs",
    ):
        compile_authority_state_effects(
            before_capsules=capsules,
            after_capsules=capsules,
            states=states,
            prior_bindings=bindings,
            predecessor_head_revision=1,
            before_selection_token=AuthoritySelectionTokenProjection(
                0,
                None,
                0,
                None,
            ),
            at="2026-09-01T12:00:00Z",
        )


def test_layer_artifact_can_publish_only_after_the_exact_claim(tmp_path) -> None:
    layer = _layer(multi=True)
    _pass_layer_units(tmp_path, layer)
    destination = tmp_path / layer.script
    assert not destination.exists()

    claim = _claim(tmp_path, layer)
    state = unit_state.load(tmp_path, layer.id)
    assert state["layer_finalization"]["active_claim"]["claim_id"] == claim.claim_id
    guard = _claim_guard(tmp_path, layer, claim)
    published, _sha256 = _publish_artifact(tmp_path, guard)

    assert published == destination
    source = destination.read_text(encoding="utf-8")
    assert "work unit form" in source
    assert "work unit look" in source
    assert source.count("fixture evaluated-state barrier") == 2


def test_layer_artifact_noop_preserves_canonical_originating_shot(tmp_path) -> None:
    layer = _layer(multi=True)
    _pass_layer_units(tmp_path, layer)
    guard = _claim_guard(tmp_path, layer, _claim(tmp_path, layer))
    aliased_folder = tmp_path / "unused-component" / ".."
    initial = prepare_layer_artifact(
        aliased_folder,
        guard,
        evaluation_barrier=_EVALUATION_BARRIER,
    )
    commit_layer_artifact(aliased_folder, initial, guard)

    reused = prepare_layer_artifact(
        aliased_folder,
        guard,
        evaluation_barrier=_EVALUATION_BARRIER,
    )

    assert reused.destination == tmp_path / layer.script
    with pytest.raises(AttributeError):
        _ = reused.publication
    with pytest.raises(AttributeError):
        _ = reused.shot
    assert (
        commit_layer_artifact(aliased_folder, reused, guard)
        == tmp_path / layer.script
    )


def test_layer_artifact_noop_uses_publish_prepared_exactly_once(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layer = _layer()
    _pass_layer_units(tmp_path, layer)
    guard = _claim_guard(tmp_path, layer, _claim(tmp_path, layer))
    _publish_artifact(tmp_path, guard)
    prepared = prepare_layer_artifact(
        tmp_path,
        guard,
        evaluation_barrier=_EVALUATION_BARRIER,
    )
    with pytest.raises(AttributeError):
        _ = prepared.publication
    publish_prepared = guard.publish_prepared
    calls = 0

    def counted_publish(_self, operation, transaction_binding, mutation):
        nonlocal calls
        calls += 1
        assert transaction_binding is prepared
        return publish_prepared(operation, transaction_binding, mutation)

    monkeypatch.setattr(
        LayerFinalizationClaimGuard,
        "publish_prepared",
        counted_publish,
    )

    assert commit_layer_artifact(tmp_path, prepared, guard) == tmp_path / layer.script
    assert calls == 1


def test_layer_artifact_replacement_refuses_guard_that_skips_mutation(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layer = _layer()
    _pass_layer_units(tmp_path, layer)
    guard = _claim_guard(tmp_path, layer, _claim(tmp_path, layer))
    prepared = prepare_layer_artifact(
        tmp_path,
        guard,
        evaluation_barrier=_EVALUATION_BARRIER,
    )
    destination = prepared.destination

    def skip_mutation(_self, _operation, transaction_binding, _mutation):
        assert transaction_binding is prepared
        return None

    monkeypatch.setattr(
        LayerFinalizationClaimGuard,
        "publish_prepared",
        skip_mutation,
    )

    with pytest.raises(
        ValueError,
        match="returned without completing its exact prepared mutation",
    ):
        commit_layer_artifact(tmp_path, prepared, guard)

    assert not destination.exists()
    discard_layer_artifact(prepared)
    assert not tuple(
        destination.parent.glob(f".{destination.name}.prepared.*")
    )


def test_layer_artifact_replacement_refuses_guard_invoking_mutation_twice(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layer = _layer()
    _pass_layer_units(tmp_path, layer)
    guard = _claim_guard(tmp_path, layer, _claim(tmp_path, layer))
    prepared = prepare_layer_artifact(
        tmp_path,
        guard,
        evaluation_barrier=_EVALUATION_BARRIER,
    )
    destination = prepared.destination
    publish_prepared = guard.publish_prepared

    def invoke_twice(_self, operation, transaction_binding, mutation):
        assert transaction_binding is prepared

        def duplicate(authorization):
            mutation(authorization)
            return mutation(authorization)

        return publish_prepared(
            operation,
            transaction_binding,
            duplicate,
        )

    monkeypatch.setattr(
        LayerFinalizationClaimGuard,
        "publish_prepared",
        invoke_twice,
    )

    with pytest.raises(
        ValueError,
        match="invoked its prepared mutation more than once",
    ):
        commit_layer_artifact(tmp_path, prepared, guard)

    assert destination.is_file()
    discard_layer_artifact(prepared)


def test_layer_artifact_success_retires_typed_preparation(tmp_path) -> None:
    layer = _layer()
    _pass_layer_units(tmp_path, layer)
    guard = _claim_guard(tmp_path, layer, _claim(tmp_path, layer))
    prepared = prepare_layer_artifact(
        tmp_path,
        guard,
        evaluation_barrier=_EVALUATION_BARRIER,
    )

    assert commit_layer_artifact(tmp_path, prepared, guard) == tmp_path / layer.script
    with pytest.raises(ValueError, match="unregistered, expired, consumed"):
        _ = prepared.sha256
    with pytest.raises(ValueError, match="unregistered, expired, consumed"):
        commit_layer_artifact(tmp_path, prepared, guard)
    with pytest.raises(ValueError, match="unregistered, expired, consumed"):
        discard_layer_artifact(prepared)


def test_layer_artifact_refuses_foreign_thread_access(tmp_path) -> None:
    layer = _layer()
    _pass_layer_units(tmp_path, layer)
    guard = _claim_guard(tmp_path, layer, _claim(tmp_path, layer))
    prepared = prepare_layer_artifact(
        tmp_path,
        guard,
        evaluation_barrier=_EVALUATION_BARRIER,
    )
    failures: list[BaseException] = []

    def read_from_foreign_thread() -> None:
        try:
            _ = prepared.sha256
        except BaseException as exc:  # asserted below
            failures.append(exc)

    worker = Thread(target=read_from_foreign_thread)
    worker.start()
    worker.join(5)

    assert not worker.is_alive()
    assert len(failures) == 1
    assert "another process or thread" in str(failures[0])
    discard_layer_artifact(prepared)


def test_layer_artifact_noop_refuses_destination_changed_before_publish(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layer = _layer()
    _pass_layer_units(tmp_path, layer)
    guard = _claim_guard(tmp_path, layer, _claim(tmp_path, layer))
    destination, _sha256 = _publish_artifact(tmp_path, guard)
    prepared = prepare_layer_artifact(
        tmp_path,
        guard,
        evaluation_barrier=_EVALUATION_BARRIER,
    )
    payload = destination.read_bytes()
    replacement = tmp_path / "same-bytes-replacement.py"
    replacement.write_bytes(payload)
    replacement.replace(destination)

    publish_called = False

    def unexpected_publish(_self, *_args, **_kwargs):
        nonlocal publish_called
        publish_called = True
        raise AssertionError("stale no-op reached guard.publish_prepared")

    monkeypatch.setattr(
        LayerFinalizationClaimGuard,
        "publish_prepared",
        unexpected_publish,
    )

    with pytest.raises(
        ValueError,
        match=(
            r"trusted (?:path|file).*changed|rebound|destination changed|"
            r"audit does not match"
        ),
    ):
        commit_layer_artifact(tmp_path, prepared, guard)

    assert publish_called is False
    assert destination.read_bytes() == payload


def test_layer_artifact_noop_refuses_destination_changed_during_publish(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layer = _layer()
    _pass_layer_units(tmp_path, layer)
    guard = _claim_guard(tmp_path, layer, _claim(tmp_path, layer))
    destination, _sha256 = _publish_artifact(tmp_path, guard)
    prepared = prepare_layer_artifact(
        tmp_path,
        guard,
        evaluation_barrier=_EVALUATION_BARRIER,
    )
    payload = destination.read_bytes()
    replacement = tmp_path / "same-bytes-during-publish.py"
    replacement.write_bytes(payload)

    publish_prepared = guard.publish_prepared

    def publish_then_rebind(_self, operation, transaction_binding, mutation):
        assert transaction_binding is prepared

        def rebind_then_mutate(capability):
            replacement.replace(destination)
            return mutation(capability)

        return publish_prepared(
            operation,
            transaction_binding,
            rebind_then_mutate,
        )

    monkeypatch.setattr(
        LayerFinalizationClaimGuard,
        "publish_prepared",
        publish_then_rebind,
    )

    with pytest.raises(
        ValueError,
        match=r"trusted (?:path|file).*changed|rebound|destination changed",
    ):
        commit_layer_artifact(tmp_path, prepared, guard)

    assert destination.read_bytes() == payload


def test_layer_artifact_noop_destination_binding_is_not_replaceable(tmp_path) -> None:
    layer = _layer()
    _pass_layer_units(tmp_path, layer)
    guard = _claim_guard(tmp_path, layer, _claim(tmp_path, layer))
    destination, _sha256 = _publish_artifact(tmp_path, guard)
    prepared = prepare_layer_artifact(
        tmp_path,
        guard,
        evaluation_barrier=_EVALUATION_BARRIER,
    )
    with pytest.raises(TypeError, match="dataclass instances"):
        replace(prepared, existing_destination=None)
    with pytest.raises(AttributeError):
        _ = prepared.existing_destination
    discard_layer_artifact(prepared)
    assert destination.is_file()


def test_layer_artifact_noop_rehashes_existing_destination_from_claim(
    tmp_path,
) -> None:
    layer = _layer()
    _pass_layer_units(tmp_path, layer)
    guard = _claim_guard(tmp_path, layer, _claim(tmp_path, layer))
    destination = tmp_path / layer.script
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(b"unrelated existing artifact\n")
    prepared = prepare_layer_artifact(
        tmp_path,
        guard,
        evaluation_barrier=_EVALUATION_BARRIER,
    )
    with pytest.raises(AttributeError):
        _ = prepared.publication
    with pytest.raises(TypeError, match="dataclass instances"):
        replace(prepared, publication=None)
    commit_layer_artifact(tmp_path, prepared, guard)
    assert hashlib.sha256(destination.read_bytes()).hexdigest() == guard.claim.layer_script_sha256


def test_layer_artifact_preparation_refuses_another_guard_shot(tmp_path) -> None:
    layer = _layer()
    _pass_layer_units(tmp_path, layer)
    guard = _claim_guard(tmp_path, layer, _claim(tmp_path, layer))
    foreign = tmp_path / "foreign-shot"
    foreign.mkdir()

    with pytest.raises(
        ValueError,
        match="preparation folder does not match its finalization guard shot",
    ):
        prepare_layer_artifact(
            foreign,
            guard,
            evaluation_barrier=_EVALUATION_BARRIER,
        )

    assert not (foreign / layer.script).exists()


def test_layer_artifact_commit_refuses_foreign_or_malformed_shot_binding(
    tmp_path,
) -> None:
    layer = _layer()
    _pass_layer_units(tmp_path, layer)
    guard = _claim_guard(tmp_path, layer, _claim(tmp_path, layer))
    prepared = prepare_layer_artifact(
        tmp_path,
        guard,
        evaluation_barrier=_EVALUATION_BARRIER,
    )
    foreign = tmp_path / "foreign-shot"
    foreign.mkdir()

    try:
        with pytest.raises(ValueError, match="commit folder does not match"):
            commit_layer_artifact(foreign, prepared, guard)
        with pytest.raises(ValueError, match="belongs to another shot"):
            commit_layer_artifact(
                tmp_path,
                prepared,
                replace(guard, folder=foreign),
            )
        with pytest.raises(AttributeError):
            _ = prepared.shot
        with pytest.raises(TypeError, match="dataclass instances"):
            replace(prepared, shot=str(tmp_path))
    finally:
        discard_layer_artifact(prepared)

    assert not (tmp_path / layer.script).exists()


@pytest.mark.parametrize("source_variant", ["mutated", "deleted"])
def test_layer_artifact_commit_refuses_claim_source_drift(
    tmp_path,
    source_variant: str,
) -> None:
    layer = _layer(multi=True)
    _pass_layer_units(tmp_path, layer)
    guard = _claim_guard(tmp_path, layer, _claim(tmp_path, layer))
    prepared = prepare_layer_artifact(
        tmp_path,
        guard,
        evaluation_barrier=_EVALUATION_BARRIER,
    )
    source = tmp_path / guard.claim.unit_inputs[-1].script_path
    if source_variant == "mutated":
        source.write_text("# changed after artifact preparation\n", encoding="utf-8")
    else:
        source.unlink()

    try:
        with pytest.raises(ValueError, match=r"changed|missing|not found"):
            commit_layer_artifact(tmp_path, prepared, guard)
    finally:
        discard_layer_artifact(prepared)

    assert not (tmp_path / layer.script).exists()


def test_layer_artifact_commit_refuses_forged_staged_payload_fields(
    tmp_path,
) -> None:
    layer = _layer()
    _pass_layer_units(tmp_path, layer)
    guard = _claim_guard(tmp_path, layer, _claim(tmp_path, layer))
    prepared = prepare_layer_artifact(
        tmp_path,
        guard,
        evaluation_barrier=_EVALUATION_BARRIER,
    )
    with pytest.raises(AttributeError):
        _ = prepared.publication
    staged = list(prepared.destination.parent.glob(f".{prepared.destination.name}.prepared.*"))
    assert len(staged) == 1
    staged[0].write_bytes(b"forged staged artifact\n")

    try:
        with pytest.raises(ValueError, match="prepared side-file inode changed"):
            commit_layer_artifact(tmp_path, prepared, guard)
    finally:
        discard_layer_artifact(prepared)

    assert not (tmp_path / layer.script).exists()


def test_layer_artifact_raw_sink_refuses_foreign_writer_capability(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layer = _layer()
    _pass_layer_units(tmp_path, layer)
    guard = _claim_guard(tmp_path, layer, _claim(tmp_path, layer))
    prepared = prepare_layer_artifact(
        tmp_path,
        guard,
        evaluation_barrier=_EVALUATION_BARRIER,
    )
    foreign = tmp_path / "foreign-writer-shot"
    foreign.mkdir()

    try:
        with finalization_guard_module.shot_authority_capture.shot_authority_writer_fence(
            foreign
        ) as foreign_capability:

            def inject_foreign_capability(
                _self,
                _operation,
                transaction_binding,
                mutation,
            ):
                assert transaction_binding is prepared
                return mutation(foreign_capability)

            monkeypatch.setattr(
                LayerFinalizationClaimGuard,
                "publish_prepared",
                inject_foreign_capability,
            )
            with pytest.raises(
                ValueError,
                match="exact typed finalization authorization",
            ):
                commit_layer_artifact(tmp_path, prepared, guard)
    finally:
        discard_layer_artifact(prepared)

    assert not (tmp_path / layer.script).exists()


def test_layer_artifact_proposal_refuses_symlinked_unit_source(tmp_path) -> None:
    layer = _layer()
    _pass_layer_units(tmp_path, layer)
    state = unit_state.load(tmp_path, layer.id)
    source = tmp_path / state["units"]["form"]["completion_receipt"]["script_path"]
    outside = tmp_path / "outside.py"
    outside.write_text("# substituted source\n", encoding="utf-8")
    source.unlink()
    source.symlink_to(outside)

    with pytest.raises(ValueError, match=r"symlink|real regular file"):
        proposed_layer_artifact_sha256(
            tmp_path,
            (("form", source.relative_to(tmp_path).as_posix()),),
            evaluation_barrier=_EVALUATION_BARRIER,
        )


def test_layer_artifact_refuses_bytes_not_bound_by_claim(tmp_path) -> None:
    layer = _layer(multi=True)
    _pass_layer_units(tmp_path, layer)
    claim = _claim(
        tmp_path,
        layer,
        layer_script_sha256=_digest("different proposed composition"),
    )
    guard = _claim_guard(tmp_path, layer, claim)

    with pytest.raises(
        ValueError,
        match="SHA-256 does not match the proposed bytes",
    ):
        prepare_layer_artifact(
            tmp_path,
            guard,
            evaluation_barrier=_EVALUATION_BARRIER,
        )

    assert not (tmp_path / layer.script).exists()


def test_layer_artifact_commit_rechecks_claimed_digest(tmp_path) -> None:
    layer = _layer(multi=True)
    _pass_layer_units(tmp_path, layer)
    guard = _claim_guard(tmp_path, layer, _claim(tmp_path, layer))
    prepared = prepare_layer_artifact(
        tmp_path,
        guard,
        evaluation_barrier=_EVALUATION_BARRIER,
    )
    try:
        with pytest.raises(TypeError, match="dataclass instances"):
            replace(prepared, sha256=_digest("different staged artifact"))
        with pytest.raises(ValueError, match="cannot be copied"):
            copy.copy(prepared)
    finally:
        discard_layer_artifact(prepared)

    assert not (tmp_path / layer.script).exists()


def test_layer_replay_receipt_is_create_only_for_one_claim(tmp_path) -> None:
    layer = _layer()
    _pass_layer_units(tmp_path, layer)
    guard = _claim_guard(tmp_path, layer, _claim(tmp_path, layer))
    _path, script_sha256 = _publish_artifact(tmp_path, guard)
    replay, stored = _publish_replay(tmp_path, guard, script_sha256)

    identical = prepare_layer_replay_receipt(tmp_path, replay, guard)
    with pytest.raises(AttributeError):
        _ = identical.publication
    assert commit_layer_replay_receipt(tmp_path, identical, guard).receipt == replay

    conflicting = LayerReplayReceipt.mint(
        claim=replay.claim,
        layer_script_sha256=replay.layer_script_sha256,
        replay_inputs=replay.replay_inputs,
        observation=replace(
            replay.observation,
            plan=replace(replay.observation.plan, axes=("different",)),
        ),
        created_at="2026-09-01T11:00:00+00:00",
    )
    with pytest.raises(
        LayerReplayReceiptConflict,
        match="immutable layer replay receipt conflicts",
    ):
        prepare_layer_replay_receipt(tmp_path, conflicting, guard)

    assert json.loads((tmp_path / stored.locator).read_text()) == replay.as_dict()


def test_reviewed_release_archives_and_reopens_the_complete_replay_prefix(
    tmp_path,
) -> None:
    layer = _layer(multi=True)
    _pass_layer_units(tmp_path, layer)
    claim = _claim(tmp_path, layer)
    guard = _claim_guard(tmp_path, layer, claim)
    _path, script_sha256 = _publish_artifact(tmp_path, guard)
    published = tuple(
        _publish_replay(
            tmp_path,
            guard,
            script_sha256,
            group_index=group_index,
            planned_group_count=2,
        )
        for group_index in range(2)
    )
    review = tmp_path / "runs/release/evidence/process.json"
    review.parent.mkdir(parents=True, exist_ok=True)
    review.write_text('{"process":"dead"}\n', encoding="utf-8")
    selected = SimpleNamespace(selection_token=ABSENT_SELECTION_TOKEN)

    with builder_execution_fence(tmp_path) as lease:
        released = release_active_layer_finalization(
            tmp_path,
            layer,
            claim_id=claim.claim_id,
            selected_authority=selected,
            reason="reviewed multi-group process death",
            evidence=(review.relative_to(tmp_path).as_posix(),),
            fence_lease=lease,
        )

    replay_evidence = released.receipt.request.replay_receipts
    assert tuple(row.group_index for row in replay_evidence) == (0, 1)
    assert tuple(row.planned_group_count for row in replay_evidence) == (2, 2)
    assert tuple(row.record_digest for row in replay_evidence) == tuple(
        replay.receipt_digest for replay, _stored in published
    )
    _replay, stored_group_one = published[1]
    (tmp_path / stored_group_one.locator).write_text("{}\n", encoding="utf-8")
    with builder_execution_fence(tmp_path) as lease, pytest.raises(
        LayerFinalizationReleaseConflict,
        match="file digest changed",
    ):
        release_active_layer_finalization(
            tmp_path,
            layer,
            claim_id=claim.claim_id,
            selected_authority=selected,
            reason="reviewed multi-group process death",
            evidence=(review.relative_to(tmp_path).as_posix(),),
            fence_lease=lease,
        )


@pytest.mark.parametrize("published_indices", [(1,), (0, 2)])
def test_reviewed_release_refuses_a_gapped_replay_prefix(
    tmp_path,
    published_indices: tuple[int, ...],
) -> None:
    layer = _layer(multi=True)
    _pass_layer_units(tmp_path, layer)
    claim = _claim(tmp_path, layer)
    guard = _claim_guard(tmp_path, layer, claim)
    _path, script_sha256 = _publish_artifact(tmp_path, guard)
    for group_index in published_indices:
        _publish_replay(
            tmp_path,
            guard,
            script_sha256,
            group_index=group_index,
            planned_group_count=3,
        )
    review = tmp_path / "runs/release/evidence/process.json"
    review.parent.mkdir(parents=True, exist_ok=True)
    review.write_text('{"process":"dead"}\n', encoding="utf-8")

    with builder_execution_fence(tmp_path) as lease, pytest.raises(
        LayerFinalizationReleaseConflict,
        match="contiguous prefix",
    ):
        release_active_layer_finalization(
            tmp_path,
            layer,
            claim_id=claim.claim_id,
            selected_authority=SimpleNamespace(
                selection_token=ABSENT_SELECTION_TOKEN
            ),
            reason="reviewed gapped process death",
            evidence=(review.relative_to(tmp_path).as_posix(),),
            fence_lease=lease,
        )


def test_reviewed_release_refuses_an_out_of_range_replay_group(tmp_path) -> None:
    layer = _layer(multi=True)
    _pass_layer_units(tmp_path, layer)
    claim = _claim(tmp_path, layer)
    guard = _claim_guard(tmp_path, layer, claim)
    _path, script_sha256 = _publish_artifact(tmp_path, guard)
    stored = tuple(
        _publish_replay(
            tmp_path,
            guard,
            script_sha256,
            group_index=group_index,
            planned_group_count=2,
        )[1]
        for group_index in range(2)
    )
    extra = tmp_path / layer_replay_receipt_locator(claim, 2)
    extra.write_bytes((tmp_path / stored[1].locator).read_bytes())
    review = tmp_path / "runs/release/evidence/process.json"
    review.parent.mkdir(parents=True, exist_ok=True)
    review.write_text('{"process":"dead"}\n', encoding="utf-8")

    with builder_execution_fence(tmp_path) as lease, pytest.raises(
        LayerFinalizationReleaseConflict,
        match="out-of-range group",
    ):
        release_active_layer_finalization(
            tmp_path,
            layer,
            claim_id=claim.claim_id,
            selected_authority=SimpleNamespace(
                selection_token=ABSENT_SELECTION_TOKEN
            ),
            reason="reviewed extra-group process death",
            evidence=(review.relative_to(tmp_path).as_posix(),),
            fence_lease=lease,
        )


@pytest.mark.parametrize("crash_phase", ["after_replay", "after_critic"])
def test_reviewed_release_recovers_preterminal_process_death_without_touching_units(
    tmp_path,
    crash_phase: str,
) -> None:
    """A dead pre-terminal owner needs an exact release, never an inferred retry."""

    layer = _layer(multi=True)
    _pass_layer_units(tmp_path, layer)
    claim = _claim(tmp_path, layer)
    stale_guard = _claim_guard(tmp_path, layer, claim)
    _path, script_sha256 = _publish_artifact(tmp_path, stale_guard)
    replay, stored_replay = _publish_replay(
        tmp_path,
        stale_guard,
        script_sha256,
    )
    before_state = unit_state.load(tmp_path, layer.id)
    before_units = json.dumps(before_state["units"], sort_keys=True)
    unit_source_bytes = {
        row.script_path: (tmp_path / row.script_path).read_bytes()
        for row in claim.unit_inputs
    }

    process_evidence = Path("runs/fixture-finalization/evidence/process-death.json")
    critic_evidence = Path("runs/fixture-finalization/evidence/critic-output.json")
    script = """
import json
import os
import sys
from pathlib import Path
from vfx_harness.orchestration.builder_execution_fence import builder_execution_fence

root = Path(sys.argv[1])
phase = sys.argv[2]
process_path = root / sys.argv[3]
critic_path = root / sys.argv[4]
with builder_execution_fence(root):
    process_path.parent.mkdir(parents=True, exist_ok=True)
    process_path.write_text(
        json.dumps({"phase": phase, "process": "exited-before-terminal"}) + "\\n",
        encoding="utf-8",
    )
    if phase == "after_critic":
        critic_path.write_text(
            json.dumps({"ordinary_critic_output": True, "sealed_receipt": None}) + "\\n",
            encoding="utf-8",
        )
    os._exit(73)
"""
    crashed = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(tmp_path),
            crash_phase,
            process_evidence.as_posix(),
            critic_evidence.as_posix(),
        ],
        check=False,
    )
    assert crashed.returncode == 73

    # Restart fails closed while the dead process's exact claim remains active.
    with pytest.raises(LayerFinalizationConflict, match="active finalization claim"):
        _claim(tmp_path, layer)

    evidence = [process_evidence.as_posix()]
    if crash_phase == "after_critic":
        evidence.append(critic_evidence.as_posix())
    selected = SimpleNamespace(selection_token=ABSENT_SELECTION_TOKEN)
    with builder_execution_fence(tmp_path) as lease:
        released = release_active_layer_finalization(
            tmp_path,
            layer,
            claim_id=claim.claim_id,
            selected_authority=selected,
            reason=f"reviewed {crash_phase} process death",
            evidence=tuple(evidence),
            fence_lease=lease,
        )

    request = released.receipt.request
    assert request.claim == claim
    assert len(request.replay_receipts) == 1
    released_replay = request.replay_receipts[0]
    assert released_replay.locator == stored_replay.locator
    assert released_replay.record_digest == replay.receipt_digest
    assert released_replay.group_index == 0
    assert released_replay.planned_group_count == 1
    assert request.judgment_disposition == "unsealed_not_reusable"
    assert {row.source_locator for row in request.review_evidence} == set(evidence)
    assert all(
        (tmp_path / row.locator).read_bytes()
        == (tmp_path / row.source_locator).read_bytes()
        for row in request.review_evidence
    )

    released_state = unit_state.load(tmp_path, layer.id)
    finalization = released_state["layer_finalization"]
    assert finalization["active_claim"] is None
    assert finalization["terminal_receipt"] is None
    assert finalization["attempt_revision"] == claim.attempt_revision
    assert finalization["claim_history"][-1]["disposition"] == "released"
    assert json.dumps(released_state["units"], sort_keys=True) == before_units
    assert all(
        (tmp_path / locator).read_bytes() == payload
        for locator, payload in unit_source_bytes.items()
    )

    # Neither the stale claim nor its replay can publish after release.
    with pytest.raises(LayerFinalizationAuthorityLost, match="lost authority"):
        stale_guard.check("publish stale critic result")
    with pytest.raises(LayerFinalizationAuthorityLost, match="lost authority"):
        prepare_layer_replay_receipt(tmp_path, replay, stale_guard)

    # Exact repeat reconciles the committed receipt without rereading mutable
    # historical evidence; changed review authority still fails closed.
    (tmp_path / evidence[0]).write_text("changed after commit\n", encoding="utf-8")
    if len(evidence) > 1:
        (tmp_path / evidence[1]).unlink()
    with builder_execution_fence(tmp_path) as lease:
        repeated = release_active_layer_finalization(
            tmp_path,
            layer,
            claim_id=claim.claim_id,
            selected_authority=selected,
            reason=f"reviewed {crash_phase} process death",
            evidence=tuple(reversed(evidence)),
            fence_lease=lease,
        )
    assert repeated.receipt == released.receipt
    with builder_execution_fence(tmp_path) as lease, pytest.raises(
        LayerFinalizationReleaseConflict,
        match="conflicts with the archived transaction",
    ):
        release_active_layer_finalization(
            tmp_path,
            layer,
            claim_id=claim.claim_id,
            selected_authority=selected,
            reason="different reviewed conclusion",
            evidence=tuple(evidence),
            fence_lease=lease,
        )

    fresh = _claim(tmp_path, layer)
    assert fresh.attempt_revision == claim.attempt_revision + 1
    assert fresh.claim_id != claim.claim_id
    assert json.dumps(unit_state.load(tmp_path, layer.id)["units"], sort_keys=True) == before_units
    with builder_execution_fence(tmp_path) as lease, pytest.raises(
        LayerFinalizationReleaseConflict,
        match="another active finalization claim",
    ):
        release_active_layer_finalization(
            tmp_path,
            layer,
            claim_id=claim.claim_id,
            selected_authority=selected,
            reason=f"reviewed {crash_phase} process death",
            evidence=tuple(evidence),
            fence_lease=lease,
        )


def test_reviewed_release_reconciles_an_exact_receipt_orphan(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layer = _layer()
    _pass_layer_units(tmp_path, layer)
    claim = _claim(tmp_path, layer)
    evidence = Path("runs/release/evidence/review.json")
    (tmp_path / evidence).parent.mkdir(parents=True)
    (tmp_path / evidence).write_text("reviewed process death\n", encoding="utf-8")
    selected = SimpleNamespace(selection_token=ABSENT_SELECTION_TOKEN)
    before_units = json.dumps(
        unit_state.load(tmp_path, layer.id)["units"],
        sort_keys=True,
    )
    before_scripts = {
        row.script_path: (tmp_path / row.script_path).read_bytes()
        for row in claim.unit_inputs
    }
    actual_release = layer_finalization_state.release_layer_finalization_claim

    def fail_release(*_args, **_kwargs):
        raise LayerFinalizationConflict("injected death before state write")

    monkeypatch.setattr(
        layer_finalization_state,
        "release_layer_finalization_claim",
        fail_release,
    )

    with builder_execution_fence(tmp_path) as lease, pytest.raises(
        LayerFinalizationReleaseConflict,
        match="injected death before state write",
    ):
        release_active_layer_finalization(
            tmp_path,
            layer,
            claim_id=claim.claim_id,
            selected_authority=selected,
            reason="reviewed process death",
            evidence=(evidence.as_posix(),),
            fence_lease=lease,
        )

    assert (
        unit_state.load(tmp_path, layer.id)["layer_finalization"]["active_claim"]
        == claim.as_dict()
    )
    release_files = tuple(
        (tmp_path / "state/layer-finalization-releases").glob("*.json")
    )
    assert len(release_files) == 1
    orphan_bytes = release_files[0].read_bytes()

    monkeypatch.setattr(
        layer_finalization_state,
        "release_layer_finalization_claim",
        actual_release,
    )
    with builder_execution_fence(tmp_path) as lease:
        stored = release_active_layer_finalization(
            tmp_path,
            layer,
            claim_id=claim.claim_id,
            selected_authority=selected,
            reason="reviewed process death",
            evidence=(evidence.as_posix(),),
            fence_lease=lease,
        )

    assert release_files[0].read_bytes() == orphan_bytes
    assert stored.locator == release_files[0].relative_to(tmp_path).as_posix()
    with builder_execution_fence(tmp_path) as lease:
        repeated = release_active_layer_finalization(
            tmp_path,
            layer,
            claim_id=claim.claim_id,
            selected_authority=selected,
            reason="reviewed process death",
            evidence=(evidence.as_posix(),),
            fence_lease=lease,
        )
    assert repeated.receipt == stored.receipt
    state = unit_state.load(tmp_path, layer.id)
    assert state["layer_finalization"]["active_claim"] is None
    assert len(state["layer_finalization"]["claim_history"]) == 1
    assert json.dumps(state["units"], sort_keys=True) == before_units
    assert all(
        (tmp_path / locator).read_bytes() == payload
        for locator, payload in before_scripts.items()
    )


def test_reviewed_release_reconciles_after_state_write_before_return(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layer = _layer(multi=True)
    _pass_layer_units(tmp_path, layer)
    claim = _claim(tmp_path, layer)
    evidence = Path("runs/release/evidence/review.json")
    (tmp_path / evidence).parent.mkdir(parents=True)
    (tmp_path / evidence).write_text("reviewed process death\n", encoding="utf-8")
    selected = SimpleNamespace(selection_token=ABSENT_SELECTION_TOKEN)
    before_units = json.dumps(
        unit_state.load(tmp_path, layer.id)["units"],
        sort_keys=True,
    )
    before_scripts = {
        row.script_path: (tmp_path / row.script_path).read_bytes()
        for row in claim.unit_inputs
    }
    actual_discard = layer_finalization_release.discard_prepared_file

    def crash_after_state_write(publication) -> None:
        actual_discard(publication)
        if publication is not None and "evidence" not in publication.relative_path.parts:
            raise RuntimeError("injected death after state write")

    monkeypatch.setattr(
        layer_finalization_release,
        "discard_prepared_file",
        crash_after_state_write,
    )
    with builder_execution_fence(tmp_path) as lease, pytest.raises(
        RuntimeError,
        match="injected death after state write",
    ):
        release_active_layer_finalization(
            tmp_path,
            layer,
            claim_id=claim.claim_id,
            selected_authority=selected,
            reason="reviewed process death",
            evidence=(evidence.as_posix(),),
            fence_lease=lease,
        )

    committed = unit_state.load(tmp_path, layer.id)
    assert committed["layer_finalization"]["active_claim"] is None
    assert len(committed["layer_finalization"]["claim_history"]) == 1
    monkeypatch.setattr(
        layer_finalization_release,
        "discard_prepared_file",
        actual_discard,
    )
    with builder_execution_fence(tmp_path) as lease:
        repeated = release_active_layer_finalization(
            tmp_path,
            layer,
            claim_id=claim.claim_id,
            selected_authority=selected,
            reason="reviewed process death",
            evidence=(evidence.as_posix(),),
            fence_lease=lease,
        )

    state = unit_state.load(tmp_path, layer.id)
    assert repeated.receipt.request.claim == claim
    assert len(state["layer_finalization"]["claim_history"]) == 1
    assert len(tuple((tmp_path / "state/layer-finalization-releases").glob("*.json"))) == 1
    assert json.dumps(state["units"], sort_keys=True) == before_units
    assert all(
        (tmp_path / locator).read_bytes() == payload
        for locator, payload in before_scripts.items()
    )


@pytest.mark.parametrize("damage", ["delete", "tamper"])
def test_fresh_claim_requires_the_exact_release_receipt_source(
    tmp_path,
    damage: str,
) -> None:
    layer = _layer()
    _pass_layer_units(tmp_path, layer)
    claim = _claim(tmp_path, layer)
    evidence = Path("runs/release/evidence/review.json")
    (tmp_path / evidence).parent.mkdir(parents=True)
    (tmp_path / evidence).write_text("reviewed process death\n", encoding="utf-8")
    selected = SimpleNamespace(selection_token=ABSENT_SELECTION_TOKEN)
    before_units = json.dumps(
        unit_state.load(tmp_path, layer.id)["units"],
        sort_keys=True,
    )
    with builder_execution_fence(tmp_path) as lease:
        stored = release_active_layer_finalization(
            tmp_path,
            layer,
            claim_id=claim.claim_id,
            selected_authority=selected,
            reason="reviewed process death",
            evidence=(evidence.as_posix(),),
            fence_lease=lease,
        )

    release_path = tmp_path / stored.locator
    if damage == "delete":
        release_path.unlink()
    else:
        release_path.write_text("{}\n", encoding="utf-8")

    with pytest.raises(LayerFinalizationConflict, match="release receipt"):
        _claim(tmp_path, layer)
    state = unit_state.load(tmp_path, layer.id)
    assert state["layer_finalization"]["active_claim"] is None
    assert state["layer_finalization"]["attempt_revision"] == claim.attempt_revision
    assert json.dumps(state["units"], sort_keys=True) == before_units


@pytest.mark.parametrize("damage", ["delete", "tamper"])
def test_fresh_claim_requires_immutable_review_evidence_snapshots(
    tmp_path,
    damage: str,
) -> None:
    layer = _layer()
    _pass_layer_units(tmp_path, layer)
    claim = _claim(tmp_path, layer)
    evidence = Path("runs/release/evidence/review.json")
    (tmp_path / evidence).parent.mkdir(parents=True)
    (tmp_path / evidence).write_text("reviewed process death\n", encoding="utf-8")
    selected = SimpleNamespace(selection_token=ABSENT_SELECTION_TOKEN)
    before_units = json.dumps(
        unit_state.load(tmp_path, layer.id)["units"],
        sort_keys=True,
    )
    with builder_execution_fence(tmp_path) as lease:
        stored = release_active_layer_finalization(
            tmp_path,
            layer,
            claim_id=claim.claim_id,
            selected_authority=selected,
            reason="reviewed process death",
            evidence=(evidence.as_posix(),),
            fence_lease=lease,
        )

    snapshot_path = tmp_path / stored.receipt.request.review_evidence[0].locator
    if damage == "delete":
        snapshot_path.unlink()
    else:
        snapshot_path.write_text("changed immutable evidence\n", encoding="utf-8")

    with builder_execution_fence(tmp_path) as lease, pytest.raises(
        LayerFinalizationReleaseConflict,
        match="review evidence snapshot",
    ):
        release_active_layer_finalization(
            tmp_path,
            layer,
            claim_id=claim.claim_id,
            selected_authority=selected,
            reason="reviewed process death",
            evidence=(evidence.as_posix(),),
            fence_lease=lease,
        )
    with pytest.raises(
        LayerFinalizationConflict,
        match="review evidence snapshot",
    ):
        _claim(tmp_path, layer)
    state = unit_state.load(tmp_path, layer.id)
    assert state["layer_finalization"]["active_claim"] is None
    assert state["layer_finalization"]["attempt_revision"] == claim.attempt_revision
    assert json.dumps(state["units"], sort_keys=True) == before_units


def test_terminal_commit_clears_claim_before_any_projection(tmp_path) -> None:
    layer, claim_guard, _replay, _stored, receipt = _finalize(
        tmp_path,
        multi=True,
    )
    state = unit_state.load(tmp_path, layer.id)
    finalization = state["layer_finalization"]

    assert finalization["active_claim"] is None
    assert finalization["terminal_receipt"] == receipt.as_dict()
    assert finalization["claim_history"][-1]["disposition"] == "completed"
    assert not (tmp_path / "plans/outcomes/layer-1.json").exists()
    ledger = json.loads((tmp_path / "shot.json").read_text(encoding="utf-8"))
    assert layer.id not in ledger["milestones"]
    with pytest.raises(LayerFinalizationAuthorityLost, match="lost authority"):
        claim_guard.check("project before terminal handoff")

    receipt_guard = LayerFinalizationReceiptGuard.bind(
        tmp_path,
        receipt,
        layer.stages,
        SimpleNamespace(selection_token=ABSENT_SELECTION_TOKEN),
    )
    observed: list[str] = []

    def project() -> None:
        current = unit_state.load(tmp_path, layer.id)
        observed.append(
            current["layer_finalization"]["terminal_receipt"]["receipt_digest"]
        )

    receipt_guard.publish("fixture derived projection", project)
    assert observed == [receipt.receipt_digest]


@pytest.mark.parametrize(
    "guard_type",
    [LayerFinalizationClaimGuard, LayerFinalizationReceiptGuard],
)
def test_publish_prepared_acquires_writer_before_finalization_hold(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    guard_type: type[LayerFinalizationClaimGuard]
    | type[LayerFinalizationReceiptGuard],
) -> None:
    capability = object()
    authorization = object()
    transaction_binding = object()
    events: list[str] = []

    @contextmanager
    def writer_fence(folder: str | Path) -> Iterator[object]:
        assert Path(folder) == tmp_path
        events.append("writer-enter")
        try:
            yield capability
        finally:
            events.append("writer-exit")

    @contextmanager
    def hold(_self: object, operation: str) -> Iterator[object]:
        assert operation == "publish prepared fixture"
        events.append("hold-enter")
        try:
            yield object()
        finally:
            events.append("hold-exit")

    @contextmanager
    def issue(**kwargs: object) -> Iterator[object]:
        assert kwargs["guard"] is guard
        assert kwargs["writer_capability"] is capability
        assert kwargs["transaction_binding"] is transaction_binding
        events.append("authorization-enter")
        try:
            yield authorization
        finally:
            events.append("authorization-exit")

    def mutation(observed_authorization: object) -> str:
        assert observed_authorization is authorization
        events.append("mutation")
        return "published"

    monkeypatch.setattr(
        finalization_guard_module.shot_authority_capture,
        "shot_authority_writer_fence",
        writer_fence,
    )
    monkeypatch.setattr(guard_type, "hold", hold)
    claim = SimpleNamespace()
    guard = (
        guard_type(tmp_path, claim, (), SimpleNamespace())
        if guard_type is LayerFinalizationClaimGuard
        else guard_type(
            tmp_path,
            SimpleNamespace(claim=claim),
            (),
            SimpleNamespace(),
        )
    )
    monkeypatch.setattr(
        layer_finalization_publication_authority,
        "_issue_layer_finalization_prepared_mutation_authorization",
        issue,
    )

    assert (
        guard.publish_prepared(
            "publish prepared fixture",
            transaction_binding,
            mutation,
        )
        == "published"
    )
    assert events == [
        "writer-enter",
        "hold-enter",
        "authorization-enter",
        "mutation",
        "authorization-exit",
        "hold-exit",
        "writer-exit",
    ]


@pytest.mark.parametrize(
    "guard_type",
    [LayerFinalizationClaimGuard, LayerFinalizationReceiptGuard],
)
def test_publish_prepared_delivers_the_exact_prepared_authorization(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    guard_type: type[LayerFinalizationClaimGuard]
    | type[LayerFinalizationReceiptGuard],
) -> None:
    capability = object()
    authorization = object()
    transaction_binding = object()
    delivered: list[object] = []

    @contextmanager
    def writer_fence(_folder: str | Path) -> Iterator[object]:
        yield capability

    @contextmanager
    def hold(_self: object, _operation: str) -> Iterator[object]:
        yield object()

    @contextmanager
    def issue(**kwargs: object) -> Iterator[object]:
        assert kwargs["guard"] is guard
        assert kwargs["writer_capability"] is capability
        assert kwargs["transaction_binding"] is transaction_binding
        yield authorization

    def mutation(observed_authorization: object) -> object:
        delivered.append(observed_authorization)
        return observed_authorization

    monkeypatch.setattr(
        finalization_guard_module.shot_authority_capture,
        "shot_authority_writer_fence",
        writer_fence,
    )
    monkeypatch.setattr(guard_type, "hold", hold)
    claim = SimpleNamespace()
    guard = (
        guard_type(tmp_path, claim, (), SimpleNamespace())
        if guard_type is LayerFinalizationClaimGuard
        else guard_type(
            tmp_path,
            SimpleNamespace(claim=claim),
            (),
            SimpleNamespace(),
        )
    )
    monkeypatch.setattr(
        layer_finalization_publication_authority,
        "_issue_layer_finalization_prepared_mutation_authorization",
        issue,
    )

    result = guard.publish_prepared(
        "publish prepared fixture",
        transaction_binding,
        mutation,
    )

    assert result is authorization
    assert delivered == [authorization]
    assert delivered[0] is authorization


@pytest.mark.parametrize(
    "guard_type",
    [LayerFinalizationClaimGuard, LayerFinalizationReceiptGuard],
)
def test_publish_prepared_releases_both_guards_when_mutation_raises(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    guard_type: type[LayerFinalizationClaimGuard]
    | type[LayerFinalizationReceiptGuard],
) -> None:
    events: list[str] = []
    authorization = object()
    transaction_binding = object()

    @contextmanager
    def writer_fence(_folder: str | Path) -> Iterator[object]:
        events.append("writer-enter")
        try:
            yield object()
        finally:
            events.append("writer-exit")

    @contextmanager
    def hold(_self: object, _operation: str) -> Iterator[object]:
        events.append("hold-enter")
        try:
            yield object()
        finally:
            events.append("hold-exit")

    @contextmanager
    def issue(**_kwargs: object) -> Iterator[object]:
        events.append("authorization-enter")
        try:
            yield authorization
        finally:
            events.append("authorization-exit")

    def mutation(observed_authorization: object) -> None:
        assert observed_authorization is authorization
        events.append("mutation")
        raise RuntimeError("fixture publication failed")

    monkeypatch.setattr(
        finalization_guard_module.shot_authority_capture,
        "shot_authority_writer_fence",
        writer_fence,
    )
    monkeypatch.setattr(guard_type, "hold", hold)
    claim = SimpleNamespace()
    guard = (
        guard_type(tmp_path, claim, (), SimpleNamespace())
        if guard_type is LayerFinalizationClaimGuard
        else guard_type(
            tmp_path,
            SimpleNamespace(claim=claim),
            (),
            SimpleNamespace(),
        )
    )
    monkeypatch.setattr(
        layer_finalization_publication_authority,
        "_issue_layer_finalization_prepared_mutation_authorization",
        issue,
    )

    with pytest.raises(RuntimeError, match="fixture publication failed"):
        guard.publish_prepared(
            "publish prepared fixture",
            transaction_binding,
            mutation,
        )

    assert events == [
        "writer-enter",
        "hold-enter",
        "authorization-enter",
        "mutation",
        "authorization-exit",
        "hold-exit",
        "writer-exit",
    ]


def test_claim_guard_does_not_reclassify_publication_value_error(tmp_path) -> None:
    layer = _layer()
    _pass_layer_units(tmp_path, layer)
    guard = _claim_guard(tmp_path, layer, _claim(tmp_path, layer))
    failure = ValueError("fixture publication conflict")
    transaction_binding = object()

    def refuse(_authorization: object) -> None:
        raise failure

    with pytest.raises(ValueError, match="fixture publication conflict") as observed:
        guard.publish_prepared(
            "refuse prepared fixture",
            transaction_binding,
            refuse,
        )

    assert observed.value is failure


@pytest.mark.parametrize(
    "guard_type",
    [LayerFinalizationClaimGuard, LayerFinalizationReceiptGuard],
)
def test_publish_prepared_noop_result_uses_one_writer_acquisition(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    guard_type: type[LayerFinalizationClaimGuard]
    | type[LayerFinalizationReceiptGuard],
) -> None:
    existing = object()
    authorization = object()
    transaction_binding = object()
    acquisitions = 0
    holds = 0

    @contextmanager
    def writer_fence(_folder: str | Path) -> Iterator[object]:
        nonlocal acquisitions
        acquisitions += 1
        yield object()

    @contextmanager
    def hold(_self: object, _operation: str) -> Iterator[object]:
        nonlocal holds
        holds += 1
        yield object()

    @contextmanager
    def issue(**_kwargs: object) -> Iterator[object]:
        yield authorization

    monkeypatch.setattr(
        finalization_guard_module.shot_authority_capture,
        "shot_authority_writer_fence",
        writer_fence,
    )
    monkeypatch.setattr(guard_type, "hold", hold)
    claim = SimpleNamespace()
    guard = (
        guard_type(tmp_path, claim, (), SimpleNamespace())
        if guard_type is LayerFinalizationClaimGuard
        else guard_type(
            tmp_path,
            SimpleNamespace(claim=claim),
            (),
            SimpleNamespace(),
        )
    )
    monkeypatch.setattr(
        layer_finalization_publication_authority,
        "_issue_layer_finalization_prepared_mutation_authorization",
        issue,
    )

    observed = guard.publish_prepared(
        "reuse existing prepared publication",
        transaction_binding,
        lambda observed_authorization: (
            existing
            if observed_authorization is authorization
            else pytest.fail("guard delivered another authorization")
        ),
    )

    assert observed is existing
    assert acquisitions == 1
    assert holds == 1


def test_prepared_mutation_authorization_is_exact_and_one_shot(
    tmp_path,
) -> None:
    layer = _layer()
    _pass_layer_units(tmp_path, layer)
    guard = _claim_guard(tmp_path, layer, _claim(tmp_path, layer))
    observed: list[object] = []
    transaction_binding = object()

    def mutation(authorization: object) -> str:
        observed.append(authorization)
        with pytest.raises(ValueError, match="cannot be copied"):
            copy.copy(authorization)
        with pytest.raises(ValueError, match="cannot be copied"):
            copy.deepcopy(authorization)
        with pytest.raises(
            ValueError,
            match="exact guard and prepared transaction authority",
        ):
            layer_finalization_publication_authority.consume_layer_finalization_prepared_mutation_authorization(
                authorization,
                expected_guard=guard,
                expected_shot=tmp_path,
                expected_claim=guard.claim,
                expected_transaction_binding=object(),
            )
        consumed = (
            layer_finalization_publication_authority.consume_layer_finalization_prepared_mutation_authorization(
                authorization,
                expected_guard=guard,
                expected_shot=tmp_path,
                expected_claim=guard.claim,
                expected_transaction_binding=transaction_binding,
            )
        )
        assert consumed is None
        assert (
            finalization_guard_module.shot_authority_capture.current_shot_authority_writer(
                tmp_path
            )
            is not None
        )
        with pytest.raises(
            ValueError,
            match="expired, or already consumed",
        ):
            layer_finalization_publication_authority.consume_layer_finalization_prepared_mutation_authorization(
                authorization,
                expected_guard=guard,
                expected_shot=tmp_path,
                expected_claim=guard.claim,
                expected_transaction_binding=transaction_binding,
            )
        return "published"

    assert (
        guard.publish_prepared(
            "publish one exact fixture",
            transaction_binding,
            mutation,
        )
        == "published"
    )
    assert len(observed) == 1
    with pytest.raises(ValueError, match="expired, or already consumed"):
        layer_finalization_publication_authority.consume_layer_finalization_prepared_mutation_authorization(
            observed[0],
            expected_guard=guard,
            expected_shot=tmp_path,
            expected_claim=guard.claim,
            expected_transaction_binding=transaction_binding,
        )


def test_unconsumed_prepared_mutation_authorization_expires_before_guard_exit(
    tmp_path,
) -> None:
    layer = _layer()
    _pass_layer_units(tmp_path, layer)
    guard = _claim_guard(tmp_path, layer, _claim(tmp_path, layer))
    observed: list[object] = []
    transaction_binding = object()

    guard.publish_prepared(
        "do not consume prepared fixture",
        transaction_binding,
        lambda authorization: observed.append(authorization),
    )

    assert len(observed) == 1
    with pytest.raises(ValueError, match="unregistered, expired"):
        layer_finalization_publication_authority.consume_layer_finalization_prepared_mutation_authorization(
            observed[0],
            expected_guard=guard,
            expected_shot=tmp_path,
            expected_claim=guard.claim,
            expected_transaction_binding=transaction_binding,
        )


def test_prepared_mutation_authorization_refuses_equal_guard_substitution(
    tmp_path,
) -> None:
    layer = _layer()
    _pass_layer_units(tmp_path, layer)
    guard = _claim_guard(tmp_path, layer, _claim(tmp_path, layer))
    substituted_guard = replace(guard)
    transaction_binding = object()

    def mutation(authorization: object) -> None:
        with pytest.raises(
            ValueError,
            match="exact guard and prepared transaction authority",
        ):
            layer_finalization_publication_authority.consume_layer_finalization_prepared_mutation_authorization(
                authorization,
                expected_guard=substituted_guard,
                expected_shot=tmp_path,
                expected_claim=guard.claim,
                expected_transaction_binding=transaction_binding,
            )
        layer_finalization_publication_authority.consume_layer_finalization_prepared_mutation_authorization(
            authorization,
            expected_guard=guard,
            expected_shot=tmp_path,
            expected_claim=guard.claim,
            expected_transaction_binding=transaction_binding,
        )

    guard.publish_prepared(
        "refuse substituted guard fixture",
        transaction_binding,
        mutation,
    )


def test_prepared_mutation_authorization_refuses_thread_transfer(
    tmp_path,
) -> None:
    layer = _layer()
    _pass_layer_units(tmp_path, layer)
    guard = _claim_guard(tmp_path, layer, _claim(tmp_path, layer))
    transaction_binding = object()

    def mutation(authorization: object) -> None:
        failures: list[BaseException] = []

        def consume_from_other_thread() -> None:
            try:
                layer_finalization_publication_authority.consume_layer_finalization_prepared_mutation_authorization(
                    authorization,
                    expected_guard=guard,
                    expected_shot=tmp_path,
                    expected_claim=guard.claim,
                    expected_transaction_binding=transaction_binding,
                )
            except BaseException as exc:  # asserted below
                failures.append(exc)

        thread = Thread(target=consume_from_other_thread)
        thread.start()
        thread.join(5)
        assert not thread.is_alive()
        assert len(failures) == 1
        assert "another process or thread" in str(failures[0])
        layer_finalization_publication_authority.consume_layer_finalization_prepared_mutation_authorization(
            authorization,
            expected_guard=guard,
            expected_shot=tmp_path,
            expected_claim=guard.claim,
            expected_transaction_binding=transaction_binding,
        )

    guard.publish_prepared(
        "refuse transferred fixture",
        transaction_binding,
        mutation,
    )


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires os.fork")
def test_prepared_mutation_issuer_lock_is_reinitialized_in_fork_child() -> None:
    lock_held = Event()
    release_lock = Event()
    fork_started = Event()
    fork_completed = Event()
    failures: list[BaseException] = []
    results: list[tuple[int, bytes]] = []

    def hold_issuer_lock() -> None:
        try:
            with layer_finalization_publication_authority._issuer_locked():
                lock_held.set()
                if not release_lock.wait(5):
                    raise AssertionError("issuer-lock release timed out")
        except BaseException as exc:  # asserted in the owning thread
            failures.append(exc)

    owner = Thread(target=hold_issuer_lock)
    owner.start()
    assert lock_held.wait(5)

    read_descriptor, write_descriptor = os.pipe()
    def fork_after_barrier() -> None:
        try:
            fork_started.set()
            child = os.fork()
            if child == 0:  # pragma: no branch - parent asserts the child result
                os.close(read_descriptor)
                acquired = (
                    layer_finalization_publication_authority._ISSUER_LOCK.acquire(
                        blocking=False
                    )
                )
                if acquired:
                    layer_finalization_publication_authority._ISSUER_LOCK.release()
                os.write(
                    write_descriptor,
                    b"reset" if acquired else b"inherited-locked",
                )
                os.close(write_descriptor)
                os._exit(0)
            os.close(write_descriptor)
            observed = os.read(read_descriptor, 64)
            os.close(read_descriptor)
            waited, status = os.waitpid(child, 0)
            if waited != child:
                raise AssertionError("fork child wait returned another process")
            results.append((os.waitstatus_to_exitcode(status), observed))
        except BaseException as exc:  # asserted in the owning thread
            failures.append(exc)
        finally:
            fork_completed.set()

    forker = Thread(target=fork_after_barrier)
    forker.start()
    assert fork_started.wait(5)
    assert not fork_completed.wait(0.1)
    release_lock.set()
    owner.join(5)
    forker.join(5)

    assert not owner.is_alive()
    assert not forker.is_alive()
    assert failures == []
    assert results == [(0, b"reset")]


def test_prepared_mutation_authorization_cannot_be_constructed() -> None:
    authorization_type = (
        layer_finalization_publication_authority.LayerFinalizationPreparedMutationAuthorization
    )
    with pytest.raises(ValueError, match="issued only"):
        authorization_type()


def test_duck_guard_cannot_mint_prepared_mutation_authorization(tmp_path) -> None:
    layer = _layer()
    _pass_layer_units(tmp_path, layer)
    guard = _claim_guard(tmp_path, layer, _claim(tmp_path, layer))
    duck_guard = SimpleNamespace(
        folder=guard.folder,
        claim=guard.claim,
        selected_authority=guard.selected_authority,
    )

    with (
        finalization_guard_module.shot_authority_capture.shot_authority_writer_fence(
            tmp_path
        ) as capability,
        pytest.raises(ValueError, match="exact concrete claim or receipt guard"),
        layer_finalization_publication_authority._issue_layer_finalization_prepared_mutation_authorization(
            issuer=finalization_guard_module._PREPARED_MUTATION_ISSUER,
            guard=duck_guard,
            shot_folder=tmp_path,
            claim=guard.claim,
            receipt=None,
            writer_capability=capability,
            transaction_binding=object(),
        ),
    ):
        pytest.fail("a duck guard minted prepared-mutation authority")


def test_exact_guard_cannot_mint_prepared_authority_without_active_hold(
    tmp_path,
) -> None:
    layer = _layer()
    _pass_layer_units(tmp_path, layer)
    guard = _claim_guard(tmp_path, layer, _claim(tmp_path, layer))

    with (
        finalization_guard_module.shot_authority_capture.shot_authority_writer_fence(
            tmp_path
        ) as capability,
        pytest.raises(ValueError, match="exact finalization guard to be actively held"),
        layer_finalization_publication_authority._issue_layer_finalization_prepared_mutation_authorization(
            issuer=finalization_guard_module._PREPARED_MUTATION_ISSUER,
            guard=guard,
            shot_folder=tmp_path,
            claim=guard.claim,
            receipt=None,
            writer_capability=capability,
            transaction_binding=object(),
        ),
    ):
        pytest.fail("an unheld exact guard minted prepared-mutation authority")


def test_active_claim_cannot_publish_terminal_layer_ledger_status(tmp_path) -> None:
    layer = _layer()
    _pass_layer_units(tmp_path, layer)
    claim = _claim(tmp_path, layer)
    guard = _claim_guard(tmp_path, layer, claim)
    shot = Shot(
        folder=tmp_path,
        frontmatter={"id": "claim-ledger-fixture", "frames": 1, "fps": 24},
        body="fixture",
    )
    milestone = layer.as_milestone({})
    ledger = AuthorityBoundLedger(
        shot,
        guard.selected_authority,
        execution_guard=guard,
    )
    ledger._slot(milestone)["script"] = layer.script
    ledger._slot(milestone)["layer_finalization_claim"] = claim.claim_id
    ledger.begin(milestone)

    with pytest.raises(ValueError, match="only its exact in-progress ledger row"):
        ledger.mark(milestone, "passed")

    stored = json.loads((tmp_path / "shot.json").read_text(encoding="utf-8"))
    assert stored["milestones"][layer.id]["status"] == "in_progress"
    assert "finalization_receipt_digest" not in stored["milestones"][layer.id]


def test_new_finalization_attempt_starts_from_a_bare_in_progress_row(tmp_path) -> None:
    """Run 20260903T040612Z-0b3fe5: layer 1 re-finalized after a rematerialization.

    The ledger row still projected run 1b6807's passed receipt, and the fresh claim's scope
    check refused to publish an in-progress row that carried a receipt digest.
    """
    layer = _layer()
    _pass_layer_units(tmp_path, layer)
    claim = _claim(tmp_path, layer)
    guard = _claim_guard(tmp_path, layer, claim)
    shot = Shot(
        folder=tmp_path,
        frontmatter={"id": "refinalize-fixture", "frames": 1, "fps": 24},
        body="fixture",
    )
    milestone = layer.as_milestone({})
    stale = {
        "status": "passed",
        "script": layer.script,
        "script_sha256": "a" * 64,
        "script_sha": "a" * 16,
        "layer_finalization_claim": "lfc-" + "b" * 64,
        "finalization_receipt_digest": "c" * 64,
        "run_id": "20260903T002758Z-1b6807",
        "attempt": 1,
        "best": {"round": 0, "mean": 5.0, "render": None},
    }
    ledger = AuthorityBoundLedger(shot, guard.selected_authority, execution_guard=guard)
    ledger._slot(milestone).update(stale)
    ledger._slot(milestone)["layer_finalization_claim"] = claim.claim_id
    ledger.begin(milestone)

    stored = json.loads((tmp_path / "shot.json").read_text(encoding="utf-8"))
    row = stored["milestones"][layer.id]
    assert row["status"] == "in_progress"
    assert row["attempt"] == 2
    assert row["layer_finalization_claim"] == claim.claim_id
    for field in ("finalization_receipt_digest", "script_sha256", "script_sha"):
        assert field not in row, field
    previous = row["history"][0]
    assert previous["attempt"] == 1
    assert previous["run_id"] == "20260903T002758Z-1b6807"
    assert previous["finalization_receipt_digest"] == "c" * 64
    assert previous["script_sha256"] == "a" * 64
    # The claim that earned the previous attempt is durable in the work-unit claim history.


def test_terminal_layer_ledger_projection_requires_exact_receipt(tmp_path) -> None:
    layer, _claim_guard_value, _replay, _stored, receipt = _finalize(tmp_path)
    selected = SimpleNamespace(selection_token=ABSENT_SELECTION_TOKEN)
    guard = LayerFinalizationReceiptGuard.bind(
        tmp_path,
        receipt,
        layer.stages,
        selected,
    )
    shot = Shot(
        folder=tmp_path,
        frontmatter={"id": "receipt-ledger-fixture", "frames": 1, "fps": 24},
        body="fixture",
    )
    milestone = layer.as_milestone({})
    ledger = AuthorityBoundLedger(shot, selected, execution_guard=guard)
    ledger._slot(milestone).update(
        {
            "status": receipt.final_status,
            "script": receipt.layer_script_path,
            "script_sha256": receipt.layer_script_sha256,
            "script_sha": receipt.layer_script_sha256[:16],
            "layer_finalization_claim": receipt.claim.claim_id,
            "finalization_receipt_digest": receipt.receipt_digest,
        }
    )
    ledger.save()

    stored = json.loads((tmp_path / "shot.json").read_text(encoding="utf-8"))
    assert stored["milestones"][layer.id]["status"] == "passed"
    assert (
        stored["milestones"][layer.id]["finalization_receipt_digest"]
        == receipt.receipt_digest
    )

    conflicting = AuthorityBoundLedger(shot, selected, execution_guard=guard)
    conflicting._slot(milestone).update(
        {
            "status": "failed",
            "script": receipt.layer_script_path,
            "script_sha256": receipt.layer_script_sha256,
            "script_sha": receipt.layer_script_sha256[:16],
            "layer_finalization_claim": receipt.claim.claim_id,
            "finalization_receipt_digest": receipt.receipt_digest,
        }
    )
    with pytest.raises(ValueError, match="must exactly name its finalization receipt"):
        conflicting.save()


@pytest.mark.parametrize("tamper", ["selection", "unit", "script", "replay"])
def test_terminal_guard_rejects_selection_and_source_tamper(
    tmp_path,
    tamper: str,
) -> None:
    layer, _claim_guard_value, _replay, stored, receipt = _finalize(tmp_path)
    selected = SimpleNamespace(selection_token=ABSENT_SELECTION_TOKEN)
    if tamper == "selection":
        selected = SimpleNamespace(
            selection_token=AuthoritySelectionToken(
                plan_revision=1,
                plan_pointer_sha256=_digest("different plan pointer"),
                jit_revision=0,
                jit_pointer_sha256=None,
            )
        )
    elif tamper == "unit":
        unit_script = (
            tmp_path
            / unit_state.load(tmp_path, layer.id)["units"][layer.stages[0].id][
                "completion_receipt"
            ]["script_path"]
        )
        unit_script.write_text("# tampered accepted unit\n", encoding="utf-8")
    elif tamper == "script":
        (tmp_path / receipt.layer_script_path).write_text(
            "# tampered composed layer\n",
            encoding="utf-8",
        )
    else:
        (tmp_path / stored.locator).write_text(
            '{"tampered":true}\n',
            encoding="utf-8",
        )

    with pytest.raises(LayerFinalizationAuthorityLost, match="lost authority"):
        LayerFinalizationReceiptGuard.bind(
            tmp_path,
            receipt,
            layer.stages,
            selected,
        )


def test_checkpoint_invalidation_archives_active_and_terminal_authority(tmp_path) -> None:
    active_root = tmp_path / "active"
    active_root.mkdir()
    active_layer = _layer()
    _pass_layer_units(active_root, active_layer)
    active_claim = _claim(active_root, active_layer)

    unit_state.invalidate_checkpoint(
        active_root,
        active_layer.id,
        active_layer.stages[0].id,
        active_layer.stages,
        reason="fixture invalidation",
        evidence=["fixture:unit-tamper"],
    )
    active_state = unit_state.load(active_root, active_layer.id)["layer_finalization"]
    assert active_state["active_claim"] is None
    assert active_state["claim_history"][-1]["claim"] == active_claim.as_dict()
    assert active_state["claim_history"][-1]["disposition"] == "revoked"

    terminal_root = tmp_path / "terminal"
    terminal_root.mkdir()
    terminal_layer, _guard, _replay, _stored, terminal = _finalize(terminal_root)
    unit_state.invalidate_checkpoint(
        terminal_root,
        terminal_layer.id,
        terminal_layer.stages[0].id,
        terminal_layer.stages,
        reason="fixture invalidation",
        evidence=["fixture:terminal-tamper"],
    )
    terminal_state = unit_state.load(terminal_root, terminal_layer.id)["layer_finalization"]
    assert terminal_state["terminal_receipt"] is None
    assert terminal_state["receipt_history"][-1]["receipt"] == terminal.as_dict()
    assert terminal_state["receipt_history"][-1]["disposition"] == "revoked"


def test_apply_replan_supersedes_terminal_layer_authority(tmp_path) -> None:
    layer, _guard, _replay, _stored, receipt = _finalize(tmp_path)

    legacy_apply_replan(
        tmp_path,
        layer.id,
        layer.stages,
        layer.stages,
        old_plan_hash=_PLAN_HASH,
        new_plan_hash=_PLAN_HASH,
        owner="fixture",
        trigger="fixture authority republication",
        evidence=["fixture:replan"],
    )

    finalization = unit_state.load(tmp_path, layer.id)["layer_finalization"]
    assert finalization["active_claim"] is None
    assert finalization["terminal_receipt"] is None
    assert finalization["receipt_history"][-1]["receipt"] == receipt.as_dict()
    assert finalization["receipt_history"][-1]["disposition"] == "superseded"
