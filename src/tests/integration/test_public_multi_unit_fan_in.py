"""Public multi-unit layer fan-in through terminal publication and replay."""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from tests.unit.test_layer_finalization_state import (
    _lower_boundary_receipt_authority as _lower_boundary_receipt_authority,
)
from tests.unit_attempt_fixtures import ABSENT_SELECTION_TOKEN, pass_unit
from vfx_harness.agents.builder.layer_artifact import (
    commit_layer_artifact,
    discard_layer_artifact,
    prepare_layer_artifact,
    proposed_layer_artifact_sha256,
)
from vfx_harness.agents.builder.layer_finalization_guard import (
    LayerFinalizationClaimGuard,
)
from vfx_harness.agents.builder.layer_finalization_reconcile import (
    reconcile_layer_finalization,
)
from vfx_harness.agents.builder.prior import _ARTIFACT_EVALUATION_BARRIER
from vfx_harness.domain.brief import load_shot
from vfx_harness.domain.judgment_debt_replay_receipts import (
    ReplayPrefixLayerReceipt,
    ReplayPrefixReceipt,
    ReplayPrefixUnitReceipt,
)
from vfx_harness.domain.layer_finalizations import (
    LAYER_FINALIZATION_PROJECTION_SCHEMA,
    LayerEvaluationReceipt,
    LayerFinalizationReceipt,
    LayerReplayClaimRequirement,
    LayerReplayEvaluationGroupPlan,
    LayerReplayObservation,
    LayerReplayPointObservation,
    LayerReplayReceipt,
    LayerReplayReceiptBinding,
)
from vfx_harness.domain.unit_evaluation_receipts import ReplayInputBinding
from vfx_harness.domain.work_units import dependency_ordered_units
from vfx_harness.evaluation import determinism
from vfx_harness.orchestration import unit_state
from vfx_harness.orchestration.layer_evaluation_receipts import (
    commit_layer_evaluation_receipt,
    discard_layer_evaluation_receipt,
    prepare_layer_evaluation_receipt,
)
from vfx_harness.orchestration.layer_finalization_state import (
    LayerFinalizationConflict,
    claim_layer_finalization,
    complete_layer_finalization,
)
from vfx_harness.orchestration.layer_plans import build_layer_outcome_projection
from vfx_harness.orchestration.layer_publication import (
    require_current_layer_publication,
)
from vfx_harness.orchestration.layer_replay_receipts import (
    commit_layer_replay_receipt,
    discard_layer_replay_receipt,
    prepare_layer_replay_receipt,
)
from vfx_harness.orchestration.ledger import Layer, load_layers_from_path

pytestmark = pytest.mark.usefixtures("_lower_boundary_receipt_authority")

_PLAN_HASH = "a" * 64


def _digest(value: bytes | str) -> str:
    payload = value.encode() if isinstance(value, str) else value
    return hashlib.sha256(payload).hexdigest()


def _selected_authority(root: Path):
    """Exact local selected-view facade for this lower coordinator fixture."""

    names = {
        "layers.json",
        "acceptance.json",
        "critic_axes.json",
        "checks.json",
        "scene_checks.json",
        "requirements.json",
        "obligations.json",
        "assumptions.json",
        "plan.provenance.json",
    }
    bundle = SimpleNamespace(
        root=root,
        content_hash=_digest("public fan-in selected bundle"),
        artifacts={},
    )
    return SimpleNamespace(
        selection_token=ABSENT_SELECTION_TOKEN,
        plan=SimpleNamespace(bundle=bundle),
        assertion=SimpleNamespace(effective_view=object()),
        artifact_paths={name: root / name for name in names},
    )


def _unit_row(
    unit_id: str,
    *,
    frame: int,
    depends_on: tuple[str, ...] = (),
) -> dict:
    return {
        "id": unit_id,
        "title": f"Fixture {unit_id}",
        "plan": f"plans/01_fan_in/{unit_id}.md",
        "depends_on": list(depends_on),
        "mutates": {
            "mode": "scoped",
            "roles": [f"fixture.{unit_id}"],
            "controls": [],
            "control_roles": {},
            "script_spans": [f"build/units/01/{unit_id}.py"],
        },
        "protects": {
            "selector": "all_active_upstream_interfaces",
            "resolve_to_explicit_ids_at": "freeze",
        },
        "evaluation": {
            "primary_judge": frame,
            "judge": [{"frame": frame, "ref": f"refs/f{frame:03d}.png"}],
            "temporal_evidence": "none",
            "claims": [
                {
                    "id": f"claim.{unit_id}",
                    "proposition": f"{unit_id} has its declared scene state",
                    "axis": "form",
                    "property": "object_count",
                    "subject_roles": [f"fixture.{unit_id}"],
                    "subject_controls": [],
                    "moments": [frame],
                    "kind": "atomic",
                    "required": True,
                    "authority": "executable_required",
                    "repair_owner": unit_id,
                    "asserts": "scene",
                    "evidence": [
                        {"kind": "scene_contract", "id": f"contract.{unit_id}"}
                    ],
                }
            ],
        },
        "completion": "all_required_claims_and_protected_contracts_pass",
        "look_capabilities": [],
        "provides": ["geometry"],
    }


def _write_shot_authority(root: Path) -> Layer:
    """Write one exact reverse-authored, dependency-ordered two-unit layer."""

    root.joinpath("brief.md").write_text(
        "---\n"
        "id: public-fan-in-fixture\n"
        "frames: 41\n"
        "fps: 24\n"
        "resolution: [64, 64]\n"
        "engine: BLENDER_EEVEE\n"
        "---\n"
        "Two independently accepted scene constituents compose one public layer.\n",
        encoding="utf-8",
    )
    refs = root / "refs"
    refs.mkdir(parents=True)
    for frame, color in ((40, (24, 32, 48)), (41, (32, 48, 24))):
        Image.new("RGB", (16, 16), color).save(refs / f"f{frame:03d}.png")

    form = _unit_row("form", frame=40)
    look = _unit_row("look", frame=41, depends_on=("form",))
    # Deliberately authored in reverse order. Execution and claim identity must use
    # stable topological order, with authored position only breaking ready-set ties.
    layer_row = {
        "id": "1",
        "script": "build/layer_1.py",
        "title": "Public fan-in fixture",
        "primary_judge": 40,
        "judge": [
            {"frame": 40, "ref": "refs/f040.png"},
            {"frame": 41, "ref": "refs/f041.png"},
        ],
        "owns": ["form"],
        "reads": "two dependency-ordered constituents",
        "evidence_domains": ["scene"],
        "execution": "ready",
        "stages": [look, form],
    }
    root.joinpath("layers.json").write_text(
        json.dumps({"schema": 4, "layers": [layer_row]}) + "\n",
        encoding="utf-8",
    )
    root.joinpath("acceptance.json").write_text("[]\n", encoding="utf-8")
    root.joinpath("critic_axes.json").write_text(
        json.dumps([{"key": "form", "desc": "both scene constituents exist"}])
        + "\n",
        encoding="utf-8",
    )
    root.joinpath("checks.json").write_text(
        '{"schema":2,"checks":[]}\n',
        encoding="utf-8",
    )
    root.joinpath("scene_checks.json").write_text(
        json.dumps(
            {
                "schema": 2,
                "contracts": [
                    {
                        "id": f"contract.{unit_id}",
                        "kind": "object_count",
                        "owner_layer": "1",
                        "fault_owner": "1",
                        "activates_at": "1",
                        "lifecycle": "layer",
                        "axis": "form",
                        "roles": [f"fixture.{unit_id}"],
                        "op": "min",
                        "lo": 1,
                    }
                    for unit_id in ("form", "look")
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    plans = root / "plans" / "01_fan_in"
    plans.mkdir(parents=True)
    root.joinpath("plans/global.md").write_text(
        "# Public fan-in fixture plan\n",
        encoding="utf-8",
    )
    root.joinpath("global.md").write_text(
        "# Selected public fan-in bundle\n",
        encoding="utf-8",
    )
    for unit_id in ("form", "look"):
        plans.joinpath(f"{unit_id}.md").write_text(
            f"# {unit_id}\nProduce the declared fixture scene constituent.\n",
            encoding="utf-8",
        )

    layer = load_layers_from_path(root / "layers.json")["1"]
    scripts = {
        "form": (
            "import bpy\n"
            "from mathutils import Vector\n"
            "bpy.ops.mesh.primitive_cube_add(location=(-0.9, 0.0, 0.0))\n"
            "form = bpy.data.objects.get('Cube')\n"
            "assert form is not None\n"
            "form.name = 'FanInForm'\n"
            "form['semantic_role'] = 'fixture.form'\n"
            "camera_data = bpy.data.cameras.new('FanInCameraData')\n"
            "camera = bpy.data.objects.new('FanInCamera', camera_data)\n"
            "bpy.context.scene.collection.objects.link(camera)\n"
            "camera.location = (4.5, -7.0, 3.5)\n"
            "camera.rotation_euler = ((Vector((0.0, 0.0, 0.0)) - camera.location)"
            ".to_track_quat('-Z', 'Y').to_euler())\n"
            "bpy.context.scene.camera = camera\n"
            "light_data = bpy.data.lights.new('FanInKeyData', type='AREA')\n"
            "light_data.energy = 950.0\n"
            "light_data.shape = 'DISK'\n"
            "light_data.size = 4.0\n"
            "light = bpy.data.objects.new('FanInKey', light_data)\n"
            "bpy.context.scene.collection.objects.link(light)\n"
            "light.location = (3.0, -4.0, 6.0)\n"
        ),
        "look": (
            "import bpy\n"
            "assert bpy.data.objects.get('FanInForm') is not None\n"
            "bpy.ops.mesh.primitive_uv_sphere_add(segments=16, ring_count=8, "
            "location=(1.1, 0.0, 0.0))\n"
            "look = bpy.data.objects.get('Sphere')\n"
            "assert look is not None\n"
            "look.name = 'FanInLook'\n"
            "look['semantic_role'] = 'fixture.look'\n"
            "material = bpy.data.materials.new('FanInLookMaterial')\n"
            "material.diffuse_color = (0.08, 0.3, 0.7, 1.0)\n"
            "look.data.materials.append(material)\n"
        ),
    }
    for unit in layer.stages:
        script = root / unit.mutates.script_spans[0]
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(scripts[unit.id], encoding="utf-8")
    return layer


def _pass_exact_units(root: Path, layer: Layer, *, count: int | None = None) -> None:
    unit_state.initialize(root, layer.id, layer.stages, plan_hash=_PLAN_HASH)
    ordered = dependency_ordered_units(layer.stages)
    eligible: set[str] = set()
    for unit in ordered[:count]:
        pass_unit(
            root,
            layer.id,
            unit,
            layer.stages,
            plan_hash=_PLAN_HASH,
            eligible_passed=eligible,
        )
        eligible.add(unit.id)


def _claim_and_publish_artifact(
    root: Path,
    layer: Layer,
) -> tuple[LayerFinalizationClaimGuard, str]:
    state = unit_state.load(root, layer.id)
    proposed = proposed_layer_artifact_sha256(
        root,
        (
            (
                unit.id,
                str(state["units"][unit.id]["completion_receipt"]["script_path"]),
            )
            for unit in dependency_ordered_units(layer.stages)
        ),
        evaluation_barrier=_ARTIFACT_EVALUATION_BARRIER,
    )
    claim = claim_layer_finalization(
        root,
        layer,
        expected_plan_hash=_PLAN_HASH,
        run_id="public-fanin",
        layer_script_sha256=proposed,
        predecessor_inputs=(),
        selection_token=ABSENT_SELECTION_TOKEN,
    )
    selected = _selected_authority(root)
    guard = LayerFinalizationClaimGuard.bind(
        root,
        claim,
        layer.stages,
        selected,
    )
    prepared = prepare_layer_artifact(
        root,
        guard,
        evaluation_barrier=_ARTIFACT_EVALUATION_BARRIER,
    )
    try:
        artifact = commit_layer_artifact(prepared, guard)
    finally:
        discard_layer_artifact(prepared)
    return guard, _digest(artifact.read_bytes())


def _replay_prefix(guard: LayerFinalizationClaimGuard) -> ReplayPrefixReceipt:
    claim = guard.claim
    return ReplayPrefixReceipt(
        (
            ReplayPrefixLayerReceipt(
                layer_id=claim.layer_id,
                layer_generation_digest=claim.plan_hash,
                predecessor_layer_digests=(),
                script_path=claim.layer_script_path,
                script_sha256=claim.layer_script_sha256,
                dependencies=(),
                units=tuple(
                    ReplayPrefixUnitReceipt(
                        layer_id=claim.layer_id,
                        unit_id=row.unit_id,
                        unit_digest=row.unit_digest,
                        checkpoint_unit_digest=row.unit_digest,
                        script_path=row.script_path,
                        script_sha256=row.script_sha256,
                        checkpoint_script_sha256=row.script_sha256,
                        completion_receipt_digest=row.completion_receipt_digest,
                    )
                    for row in claim.unit_inputs
                ),
                payer_claim_id=claim.claim_id,
            ),
        )
    )


def _publish_group(
    root: Path,
    guard: LayerFinalizationClaimGuard,
    prefix: ReplayPrefixReceipt,
    *,
    group_index: int,
    unit_id: str,
    frame: int,
) -> tuple[LayerReplayReceiptBinding, dict]:
    reference = f"refs/f{frame:03d}.png"
    evidence_id = f"contract.{unit_id}"
    plan = LayerReplayEvaluationGroupPlan(
        group_index=group_index,
        planned_group_count=2,
        requirement_ids=(),
        debt_id=None,
        definition_digest=None,
        activation_digest=None,
        payment_generation_digest=None,
        judge_points=((frame, reference),),
        axes=("form",),
        claims=(
            LayerReplayClaimRequirement(
                claim_id=f"claim.{unit_id}",
                authority="executable_required",
                judge_frames=(frame,),
                evidence_ids=(evidence_id,),
            ),
        ),
        evidence_kind="executable_only",
        render_mode=None,
        render_scale=None,
    )
    evidence = (
        {
            "id": evidence_id,
            "metric": "object_count",
            "value": 1,
            "target": ">= 1",
            "pass": True,
            "source": "scene_contract",
            "authoritative": True,
            "owner_layer": guard.claim.layer_id,
            "fault_owner": guard.claim.layer_id,
            "activates_at": guard.claim.layer_id,
            "lifecycle": "layer",
        },
    )
    point = LayerReplayPointObservation.mint(
        plan=plan,
        frame=frame,
        ref=reference,
        ref_sha256=_digest((root / reference).read_bytes()),
        evidence=evidence,
    )
    receipt = LayerReplayReceipt.mint(
        claim=guard.claim,
        layer_script_sha256=guard.claim.layer_script_sha256,
        replay_inputs=(
            ReplayInputBinding.mint(
                script_path=guard.claim.layer_script_path,
                script_sha256=guard.claim.layer_script_sha256,
            ),
        ),
        observation=LayerReplayObservation(
            replay_prefix=prefix,
            plan=plan,
            points=(point,),
        ),
        created_at=f"2026-09-01T10:0{group_index + 1}:00+00:00",
    )
    prepared = prepare_layer_replay_receipt(root, receipt)
    try:
        stored = commit_layer_replay_receipt(prepared, guard)
    finally:
        discard_layer_replay_receipt(prepared)
    verdict = {
        "evidence_kind": "executable_only",
        "pass": True,
        "issues": [],
        "evidence": list(point.evidence),
        "evidence_failures": [],
        "missing_evidence": [],
        "decided_by": "unit_executable_evidence",
        "layer_replay_receipt_digest": receipt.receipt_digest,
    }
    return (
        LayerReplayReceiptBinding.mint(
            locator=stored.locator,
            sha256=stored.sha256,
            receipt=stored.receipt,
        ),
        {"frame": frame, "ref": reference, "verdict": verdict},
    )


def _publish_fan_in(root: Path):
    layer = _write_shot_authority(root)
    _pass_exact_units(root, layer)
    guard, artifact_sha256 = _claim_and_publish_artifact(root, layer)
    assert guard.claim.mode == "multi_unit_fan_in"
    assert [row.unit_id for row in guard.claim.unit_inputs] == ["form", "look"]
    assert len({row.completion_receipt_digest for row in guard.claim.unit_inputs}) == 2
    assert len({row.script_sha256 for row in guard.claim.unit_inputs}) == 2
    assert artifact_sha256 == guard.claim.layer_script_sha256

    artifact_text = (root / layer.script).read_text(encoding="utf-8")
    form_marker = artifact_text.index("# --- work unit form:")
    look_marker = artifact_text.index("# --- work unit look:")
    assert form_marker < look_marker
    assert artifact_text.count("publish evaluated unit interface (HIR-0117)") == 2

    prefix = _replay_prefix(guard)
    bindings: list[LayerReplayReceiptBinding] = []
    canonical: list[dict] = []
    for group_index, (unit_id, frame) in enumerate((("form", 40), ("look", 41))):
        binding, row = _publish_group(
            root,
            guard,
            prefix,
            group_index=group_index,
            unit_id=unit_id,
            frame=frame,
        )
        bindings.append(binding)
        canonical.append(row)
    assert [
        row.receipt.observation.group_index for row in bindings
    ] == [0, 1]
    assert len({row.locator for row in bindings}) == 2

    evaluation = LayerEvaluationReceipt.mint(
        replay_receipts=tuple(bindings),
        evaluation_groups=[
            {
                "group_index": index,
                "result": "passed",
                "requirement_ids": [],
                "debt_id": None,
                "definition_digest": None,
                "activation_digest": None,
                "canonical_start": index,
                "canonical_end": index + 1,
                "payment_failures": [],
            }
            for index in range(2)
        ],
        canonical=canonical,
        created_at="2026-09-01T10:03:00+00:00",
    )
    prepared_evaluation = prepare_layer_evaluation_receipt(root, evaluation)
    try:
        stored_evaluation = commit_layer_evaluation_receipt(
            prepared_evaluation,
            guard,
        )
    finally:
        discard_layer_evaluation_receipt(prepared_evaluation)

    selected = _selected_authority(root)
    revalidation_projection = {
        "schema": "vfx-harness.layer-image-check-revalidation/v1",
        "layer_id": layer.id,
        "source_sha256": None,
        "replacement_sha256": None,
        "replacement_text": None,
        "result": {"kept": 0, "dropped": []},
    }
    best = {"round": 0, "mean": 1.0, "render": None}
    canonical_pairs = [
        ((int(row["frame"]), str(row["ref"])), dict(row["verdict"]))
        for row in canonical
    ]
    outcome = build_layer_outcome_projection(
        root,
        layer,
        best=best,
        canonical=canonical_pairs,
        finalization_claim=guard.claim,
        final_status=evaluation.final_status,
        blender_version="fixture-blender",
        revalidation_projection=revalidation_projection,
        selected_authority=selected,
    )
    receipt = LayerFinalizationReceipt.mint(
        evaluation_receipt=evaluation,
        evaluation_receipt_locator=stored_evaluation.locator,
        evaluation_receipt_sha256=stored_evaluation.sha256,
        projection={
            "schema": LAYER_FINALIZATION_PROJECTION_SCHEMA,
            "best": best,
            "blender_version": "fixture-blender",
            "ablation": {"ok": True, "note": "two-unit fan-in replayed"},
            "revalidation": revalidation_projection,
            "judgment_debts": [],
            "finding": None,
            "outcome": outcome.as_dict(),
            "ledger": {
                "status": "passed",
                "script": layer.script,
                "script_sha256": guard.claim.layer_script_sha256,
            },
        },
        completed_at="2026-09-01T10:04:00+00:00",
    )
    complete_layer_finalization(
        root,
        receipt,
        evaluation,
        layer.stages,
        selection_token=ABSENT_SELECTION_TOKEN,
    )
    reconciled = reconcile_layer_finalization(
        load_shot(root),
        layer,
        receipt,
        selected_authority=selected,
        strips={},
    )
    publication = require_current_layer_publication(root, layer, selected)
    return layer, receipt, selected, reconciled, publication


def test_public_multi_unit_fan_in_reaches_current_publication(tmp_path: Path) -> None:
    layer, receipt, _selected, reconciled, publication = _publish_fan_in(tmp_path)

    assert receipt.final_status == "passed"
    assert len(receipt.evaluation_receipt.replay_receipts) == 2
    assert tuple(
        row["group_index"] for row in receipt.evaluation_groups
    ) == (0, 1)
    assert reconciled.outcome.is_file()
    assert reconciled.ledger.data["milestones"][layer.id][
        "finalization_receipt_digest"
    ] == receipt.receipt_digest
    assert publication.receipt == receipt
    assert publication.outcome.status == "passed"
    assert publication.ledger_script_sha256 == receipt.layer_script_sha256


@pytest.mark.skipif(shutil.which("blender") is None, reason="Blender is unavailable")
def test_public_multi_unit_artifact_replays_from_empty_in_real_blender(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _layer, _receipt, _selected, _reconciled, _publication = _publish_fan_in(
        tmp_path
    )
    replay_selected = _selected_authority(tmp_path)
    monkeypatch.setattr(
        determinism,
        "resolve_selected_authority",
        lambda _folder: replay_selected,
    )

    result = determinism.replay_equivalence(
        load_shot(tmp_path),
        passes=2,
        frame=41,
        scale=0.5,
        mode="eevee",
    )

    assert result.ok is True, result.detail
    assert result.data["layers_replayed"] == 1
    assert result.data["passes"] == 2


@pytest.mark.parametrize(
    ("damage", "match"),
    [
        ("missing", "completed executable checkpoint|completion"),
        ("stale", "changed after acceptance|source"),
        ("duplicate", "unique unit ids"),
    ],
)
def test_public_claim_refuses_inexact_constituent_closure_before_artifact(
    tmp_path: Path,
    damage: str,
    match: str,
) -> None:
    layer = _write_shot_authority(tmp_path)
    _pass_exact_units(tmp_path, layer, count=1 if damage == "missing" else None)
    state = unit_state.load(tmp_path, layer.id)
    proposed = proposed_layer_artifact_sha256(
        tmp_path,
        (
            (
                unit.id,
                unit.mutates.script_spans[0],
            )
            for unit in dependency_ordered_units(layer.stages)
        ),
        evaluation_barrier=_ARTIFACT_EVALUATION_BARRIER,
    )
    candidate_layer = layer
    if damage == "stale":
        accepted = dependency_ordered_units(layer.stages)[0]
        (tmp_path / accepted.mutates.script_spans[0]).write_text(
            "# changed after independent unit acceptance\n",
            encoding="utf-8",
        )
    elif damage == "duplicate":
        accepted = dependency_ordered_units(layer.stages)[0]
        candidate_layer = replace(layer, stages=(accepted, accepted))

    with pytest.raises((LayerFinalizationConflict, ValueError), match=match):
        claim_layer_finalization(
            tmp_path,
            candidate_layer,
            expected_plan_hash=_PLAN_HASH,
            run_id=f"public-fanin-{damage}",
            layer_script_sha256=proposed,
            predecessor_inputs=(),
            selection_token=ABSENT_SELECTION_TOKEN,
        )

    current = unit_state.load(tmp_path, layer.id)
    assert (
        (current.get("layer_finalization") or {}).get("active_claim") is None
    )
    assert not (tmp_path / layer.script).exists()
    if damage != "missing":
        assert state["units"].keys() == current["units"].keys()
