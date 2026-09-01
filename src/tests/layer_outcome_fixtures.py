"""Test-only construction of sealed-layer outcome fixtures."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from vfx_harness.domain.authority_head_records import AuthoritySelectionTokenProjection
from vfx_harness.domain.judgment_debt_replay_receipts import (
    ReplayPrefixLayerReceipt,
    ReplayPrefixReceipt,
    ReplayPrefixUnitReceipt,
)
from vfx_harness.domain.layer_finalizations import (
    LAYER_FINALIZATION_PROJECTION_SCHEMA,
    LayerEvaluationReceipt,
    LayerFinalizationClaim,
    LayerFinalizationPredecessorInput,
    LayerFinalizationReceipt,
    LayerFinalizationUnitInput,
    LayerReplayClaimRequirement,
    LayerReplayEvaluationGroupPlan,
    LayerReplayObservation,
    LayerReplayPointObservation,
    LayerReplayReceipt,
    LayerReplayReceiptBinding,
    canonical_layer_evaluation_receipt_bytes,
    canonical_layer_replay_receipt_bytes,
)
from vfx_harness.domain.layer_outcome_projections import LayerOutcomeProjection
from vfx_harness.domain.stop_envelope_primitives import canonical_digest
from vfx_harness.domain.unit_evaluation_receipts import ReplayInputBinding
from vfx_harness.orchestration.layer_outcome_paths import layer_outcome_path
from vfx_harness.orchestration.layer_plans import (
    build_layer_outcome_projection,
    build_layer_outcome_record,
    layer_finalization_canonical_rows,
)


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _selection_projection(selected_authority) -> AuthoritySelectionTokenProjection:
    if selected_authority is None:
        return AuthoritySelectionTokenProjection(0, None, 0, None)
    token = selected_authority.selection_token
    return AuthoritySelectionTokenProjection(
        token.plan_revision,
        token.plan_pointer_sha256,
        token.jit_revision,
        token.jit_pointer_sha256,
    )


def _fixture_source_digest(folder: Path, locator: str, label: str) -> str:
    path = folder / locator
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"fixture {label}".encode())
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _replay_prefix(
    claim: LayerFinalizationClaim,
) -> ReplayPrefixReceipt:
    predecessors = tuple(
        ReplayPrefixLayerReceipt(
            layer_id=row.layer_id,
            layer_generation_digest=_digest(f"fixture plan {row.layer_id}"),
            predecessor_layer_digests=(),
            script_path=row.script_path,
            script_sha256=row.script_sha256,
            units=(
                ReplayPrefixUnitReceipt(
                    layer_id=row.layer_id,
                    unit_id="fixture-unit",
                    unit_digest=_digest(f"fixture unit {row.layer_id}"),
                    checkpoint_unit_digest=_digest(f"fixture unit {row.layer_id}"),
                    script_path=f"build/units/{row.layer_id}/fixture-unit.py",
                    script_sha256=_digest(f"fixture unit script {row.layer_id}"),
                    checkpoint_script_sha256=_digest(
                        f"fixture unit script {row.layer_id}"
                    ),
                    completion_receipt_digest=_digest(
                        f"fixture completion {row.layer_id}"
                    ),
                ),
            ),
            dependencies=(),
            finalization_receipt_digest=row.finalization_receipt_digest,
        )
        for row in claim.predecessor_inputs
    )
    payer = ReplayPrefixLayerReceipt(
        layer_id=claim.layer_id,
        layer_generation_digest=claim.plan_hash,
        predecessor_layer_digests=(),
        script_path=claim.layer_script_path,
        script_sha256=claim.layer_script_sha256,
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
        dependencies=(),
        payer_claim_id=claim.claim_id,
    )
    return ReplayPrefixReceipt((*predecessors, payer))


def _normalized_replay_rows(
    folder: Path,
    layer_id: str,
    canonical: list,
    status: str,
) -> tuple[list[tuple[tuple[int, str], dict]], str]:
    rows = list(canonical)
    if not rows:
        rows = [
            (
                (1, "refs/fixture.png"),
                {
                    "evidence": [],
                    "decided_by": "unit_executable_evidence",
                },
            )
        ]
    kind = (
        "render"
        if any(
            isinstance(row[1], dict)
            and (row[1].get("evidence_kind") == "render" or row[1].get("render"))
            for row in rows
        )
        else "executable_only"
    )
    normalized: list[tuple[tuple[int, str], dict]] = []
    for index, ((frame, ref), raw_verdict) in enumerate(rows):
        verdict = dict(raw_verdict)
        _fixture_source_digest(folder, str(ref), f"reference {index}")
        evidence = [dict(item) for item in verdict.get("evidence", [])]
        if kind == "executable_only" and status != "passed":
            evidence = [
                {
                    **item,
                    "pass": (
                        False
                        if item.get("authoritative") is True
                        else item.get("pass")
                    ),
                }
                for item in evidence
            ]
        if kind == "executable_only" and not evidence:
            evidence = [
                {
                    "id": f"fixture-contract-{index}",
                    "metric": "fixture",
                    "value": 1 if status == "passed" else 0,
                    "target": "= 1",
                    "pass": status == "passed",
                    "source": "scene_contract",
                    "authoritative": True,
                    "owner_layer": layer_id,
                    "fault_owner": layer_id,
                }
            ]
        passed = status == "passed"
        verdict.update(
            {
                "evidence_kind": kind,
                "evidence": evidence,
                "evidence_failures": [
                    item
                    for item in evidence
                    if item.get("authoritative") is True
                    and item.get("pass") is False
                ],
                "missing_evidence": [],
                "pass": passed,
                "issues": (
                    []
                    if passed
                    else list(verdict.get("issues") or ["fixture failure"])
                ),
            }
        )
        if kind == "executable_only":
            verdict["decided_by"] = "unit_executable_evidence"
            verdict.pop("render", None)
            verdict.pop("render_capture", None)
        else:
            verdict.setdefault("decided_by", "critic")
            verdict.setdefault("scores", {"fixture": 5.0 if passed else 0.0})
            verdict.setdefault("mean", 5.0 if passed else 0.0)
        if status == "contract_gap":
            verdict.update(contract_gap=True, issues=[])
        elif status == "judge_conflict":
            verdict["judge_conflict"] = True
        normalized.append(((int(frame), str(ref)), verdict))
    return normalized, kind


def make_layer_finalization_receipt(
    folder: str | Path,
    layer,
    *,
    status: str,
    canonical: list,
    run_id: str,
    attempt: int | None = None,
    selected_authority=None,
    seal_outcome_sources: bool = False,
    best: dict | None = None,
    blender_version: str = "fixture",
    predecessor_inputs: tuple[LayerFinalizationPredecessorInput, ...] = (),
    runtime_checks_text: str | None = None,
) -> LayerFinalizationReceipt:
    """Mint a closed terminal receipt for outcome-focused fixtures."""

    requested_canonical = bool(canonical)
    script = Path(folder) / str(layer.script)
    script_sha256 = (
        hashlib.sha256(script.read_bytes()).hexdigest()
        if script.is_file()
        else _digest(f"fixture layer script {layer.id}")
    )
    unit = LayerFinalizationUnitInput.mint(
        unit_id="fixture-unit",
        unit_digest=_digest(f"fixture unit {layer.id}"),
        completion_receipt_digest=_digest(f"fixture completion {layer.id}"),
        script_path=f"build/units/{layer.id}/fixture-unit.py",
        script_sha256=_digest(f"fixture unit script {layer.id}"),
    )
    claim = LayerFinalizationClaim.mint(
        attempt_revision=attempt or 1,
        run_id=run_id,
        layer_id=str(layer.id),
        mode="singleton_passthrough",
        selection_token=_selection_projection(selected_authority),
        plan_hash=_digest(f"fixture plan {layer.id}"),
        layer_script_path=str(layer.script),
        layer_script_sha256=script_sha256,
        unit_inputs=(unit,),
        predecessor_inputs=predecessor_inputs,
        claimed_at="2026-09-01T00:00:00+00:00",
    )
    canonical, evidence_kind = _normalized_replay_rows(
        Path(folder),
        str(layer.id),
        canonical,
        status,
    )
    claims = tuple(
        LayerReplayClaimRequirement(
            claim_id=f"fixture-claim-{index}",
            authority=(
                "qualified_qualitative_required"
                if evidence_kind == "render"
                else "executable_required"
            ),
            judge_frames=(int(frame),),
            evidence_ids=(
                ()
                if evidence_kind == "render"
                else tuple(str(item["id"]) for item in verdict["evidence"])
            ),
        )
        for index, ((frame, _ref), verdict) in enumerate(canonical)
    )
    plan = LayerReplayEvaluationGroupPlan(
        group_index=0,
        planned_group_count=1,
        requirement_ids=(),
        debt_id=None,
        definition_digest=None,
        activation_digest=None,
        payment_generation_digest=None,
        judge_points=tuple((int(frame), str(ref)) for (frame, ref), _ in canonical),
        axes=("fixture",),
        claims=claims,
        evidence_kind=evidence_kind,
        render_mode=(
            None
            if evidence_kind == "executable_only"
            else str(canonical[0][1]["render_capture"]["mode"])
        ),
        render_scale=(
            None
            if evidence_kind == "executable_only"
            else float(canonical[0][1]["render_capture"]["scale"])
        ),
    )
    points = tuple(
        LayerReplayPointObservation.mint(
            plan=plan,
            frame=int(frame),
            ref=str(ref),
            ref_sha256=_fixture_source_digest(
                Path(folder),
                str(ref),
                f"reference {index}",
            ),
            evidence=verdict["evidence"],
            render=verdict.get("render"),
            render_sha256=(
                None
                if evidence_kind == "executable_only"
                else verdict["render_capture"]["png_sha256"]
            ),
            render_capture=verdict.get("render_capture"),
        )
        for index, ((frame, ref), verdict) in enumerate(canonical)
    )
    replay_inputs = (
        *(
            ReplayInputBinding.mint(
                script_path=row.script_path,
                script_sha256=row.script_sha256,
            )
            for row in predecessor_inputs
        ),
        ReplayInputBinding.mint(
            script_path=str(layer.script),
            script_sha256=script_sha256,
        ),
    )
    replay = LayerReplayReceipt.mint(
        claim=claim,
        layer_script_sha256=script_sha256,
        replay_inputs=replay_inputs,
        observation=LayerReplayObservation(
            replay_prefix=_replay_prefix(claim),
            plan=plan,
            points=points,
        ),
        created_at="2026-09-01T00:01:00+00:00",
    )
    replay_bytes = canonical_layer_replay_receipt_bytes(replay)
    canonical = [
        (
            frame_ref,
            {
                **verdict,
                "layer_replay_receipt_digest": replay.receipt_digest,
            },
        )
        for frame_ref, verdict in canonical
    ]
    best = (
        {
            "round": 0,
            "mean": min(
                (
                    float(verdict.get("mean", 0.0))
                    for _frame_ref, verdict in canonical
                ),
                default=0.0,
            ),
            "render": None,
        }
        if best is None
        else {key: best.get(key) for key in ("round", "mean", "render")}
    )
    receipt_canonical = layer_finalization_canonical_rows(canonical)
    revalidation_projection = {
        "schema": "vfx-harness.layer-image-check-revalidation/v1",
        "layer_id": str(layer.id),
        "source_sha256": (
            hashlib.sha256(runtime_checks_text.encode("utf-8")).hexdigest()
            if runtime_checks_text is not None
            else None
        ),
        "replacement_sha256": None,
        "replacement_text": None,
        "result": {"kept": 0, "dropped": []},
    }
    if seal_outcome_sources:
        outcome_projection = build_layer_outcome_projection(
            folder,
            layer,
            best=best,
            canonical=canonical,
            finalization_claim=claim,
            final_status=status,
            blender_version=blender_version,
            revalidation_projection=revalidation_projection,
            selected_authority=selected_authority,
        )
    else:
        if requested_canonical:
            raise ValueError(
                "non-empty fixture canonical requires seal_outcome_sources=True"
            )
        authoritative = [
            {
                key: item.get(key)
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
            for item in canonical[0][1]["evidence"]
            if item.get("authoritative") is True
        ]
        outcome_projection = LayerOutcomeProjection.from_canonical(
            claim=claim,
            layer_title=str(layer.title),
            layer_script_path=str(layer.script),
            final_status=status,
            best=best,
            revalidation_manifest={},
            canonical=[
                {
                    "evidence_kind": "executable_only",
                    "frame": canonical[0][0][0],
                    "ref": canonical[0][0][1],
                    "ref_sha256": points[0].ref_sha256,
                    "input_manifest_sha256": canonical_digest({}),
                    "authoritative": authoritative,
                    "authoritative_sha256": canonical_digest(
                        {"authoritative": authoritative}
                    ),
                    "qualitative_defects": list(
                        canonical[0][1].get("issues") or []
                    ),
                }
            ],
            receipt_canonical=receipt_canonical,
            blender_version=blender_version,
        )
    replay_locator = (
        f"runs/{run_id}/checkpoints/layer-finalizations/"
        f"{claim.claim_id}.group-0.replay.json"
    )
    evaluation = LayerEvaluationReceipt.mint(
        replay_receipts=(
            LayerReplayReceiptBinding.mint(
                locator=replay_locator,
                sha256=hashlib.sha256(replay_bytes).hexdigest(),
                receipt=replay,
            ),
        ),
        evaluation_groups=(
            {
                "group_index": 0,
                "result": status,
                "requirement_ids": [],
                "debt_id": None,
                "definition_digest": None,
                "activation_digest": None,
                "canonical_start": 0,
                "canonical_end": len(receipt_canonical),
                "payment_failures": [],
            },
        ),
        canonical=receipt_canonical,
        created_at="2026-09-01T00:01:30+00:00",
    )
    if evaluation.final_status != status:
        raise ValueError(
            "fixture status is not mechanically derived from its replay evidence"
        )
    evaluation_bytes = canonical_layer_evaluation_receipt_bytes(evaluation)
    return LayerFinalizationReceipt.mint(
        evaluation_receipt=evaluation,
        evaluation_receipt_locator=(
            f"runs/{run_id}/checkpoints/layer-finalizations/"
            f"{claim.claim_id}.evaluation.json"
        ),
        evaluation_receipt_sha256=hashlib.sha256(evaluation_bytes).hexdigest(),
        projection={
            "schema": LAYER_FINALIZATION_PROJECTION_SCHEMA,
            "best": best,
            "blender_version": blender_version,
            "ablation": {"ok": status == "passed", "note": "fixture"},
            "revalidation": revalidation_projection,
            "judgment_debts": [],
            "finding": None,
            "outcome": outcome_projection.as_dict(),
            "ledger": {
                "status": status,
                "script": str(layer.script),
                "script_sha256": script_sha256,
            },
        },
        completed_at="2026-09-01T00:02:00+00:00",
    )


def write_test_layer_outcome(
    folder: str | Path,
    layer,
    *,
    status: str,
    best: dict,
    canonical: list,
    run_id: str,
    attempt: int | None = None,
    blender_version: str,
    selected_authority=None,
    finalization_receipt: LayerFinalizationReceipt | None = None,
) -> Path:
    """Write fixture bytes without exposing an unguarded production publication API."""

    receipt = finalization_receipt or make_layer_finalization_receipt(
        folder,
        layer,
        status=status,
        canonical=canonical,
        run_id=run_id,
        attempt=attempt,
        selected_authority=selected_authority,
        seal_outcome_sources=True,
        best=best,
        blender_version=blender_version,
    )
    record = build_layer_outcome_record(
        folder,
        layer,
        best=best,
        canonical=[
            (
                (int(row["frame"]), str(row["ref"])),
                dict(row["verdict"]),
            )
            for row in receipt.canonical
        ],
        finalization_receipt=receipt,
        blender_version=blender_version,
        selected_authority=selected_authority,
    )
    path = layer_outcome_path(folder, str(layer.id))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return path
