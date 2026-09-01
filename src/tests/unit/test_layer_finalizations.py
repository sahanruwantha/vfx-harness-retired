from __future__ import annotations

import copy
import hashlib
import json

import pytest

from vfx_harness.domain.authority_head_records import (
    AUTHORITY_SELECTION_TOKEN_SCHEMA,
    AuthoritySelectionTokenProjection,
)
from vfx_harness.domain.judgment_debt_replay_receipts import (
    ReplayPrefixLayerReceipt,
    ReplayPrefixReceipt,
    ReplayPrefixUnitReceipt,
    replay_parent_chain_digest,
)
from vfx_harness.domain.judgment_debts import (
    JudgmentObservationRequest,
    JudgmentPaymentAttemptFailure,
    JudgmentPoint,
)
from vfx_harness.domain.layer_finalizations import (
    LAYER_FINALIZATION_CLAIM_ARCHIVE_SCHEMA,
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
    canonical_layer_finalization_receipt_bytes,
    canonical_layer_replay_receipt_bytes,
    validate_state_layer_finalization_contracts,
)
from vfx_harness.domain.layer_outcome_projections import LayerOutcomeProjection
from vfx_harness.domain.stop_envelope_primitives import canonical_digest
from vfx_harness.domain.unit_attempts import UnitAttemptClaim
from vfx_harness.domain.unit_completion_receipts import UnitCompletionReceipt
from vfx_harness.domain.unit_evaluation_receipts import ReplayInputBinding
from vfx_harness.domain.unit_outcomes import (
    hypothesis_falsification_render_settings_hash,
)


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _token() -> AuthoritySelectionTokenProjection:
    return AuthoritySelectionTokenProjection(
        plan_revision=4,
        plan_pointer_sha256=_digest("plan pointer"),
        jit_revision=2,
        jit_pointer_sha256=_digest("jit pointer"),
    )


def _token_dict() -> dict:
    token = _token()
    return {
        "schema": AUTHORITY_SELECTION_TOKEN_SCHEMA,
        "plan_revision": token.plan_revision,
        "plan_pointer_sha256": token.plan_pointer_sha256,
        "jit_revision": token.jit_revision,
        "jit_pointer_sha256": token.jit_pointer_sha256,
    }


def _unit_input(unit_id: str) -> LayerFinalizationUnitInput:
    return LayerFinalizationUnitInput.mint(
        unit_id=unit_id,
        unit_digest=_digest(f"unit {unit_id}"),
        completion_receipt_digest=_digest(f"completion {unit_id}"),
        script_path=f"build/units/3/{unit_id}.py",
        script_sha256=_digest(f"script {unit_id}"),
    )


def _predecessor(layer_id: str = "1") -> LayerFinalizationPredecessorInput:
    return LayerFinalizationPredecessorInput.mint(
        layer_id=layer_id,
        finalization_receipt_digest=_digest(f"finalization {layer_id}"),
        script_path=f"build/layer_{layer_id}.py",
        script_sha256=_digest(f"layer script {layer_id}"),
    )


def _claim(
    *,
    units: tuple[LayerFinalizationUnitInput, ...] | None = None,
    predecessors: tuple[LayerFinalizationPredecessorInput, ...] = (),
    claimed_at: str = "2026-09-01T10:00:00+00:00",
) -> LayerFinalizationClaim:
    resolved_units = units or (_unit_input("camera"),)
    return LayerFinalizationClaim.mint(
        attempt_revision=3,
        run_id="20260901T100000Z-finalize",
        layer_id="3",
        mode=(
            "singleton_passthrough"
            if len(resolved_units) == 1
            else "multi_unit_fan_in"
        ),
        selection_token=_token(),
        plan_hash=_digest("layers.json"),
        layer_script_path="build/layer_3.py",
        layer_script_sha256=_digest("composed layer"),
        unit_inputs=resolved_units,
        predecessor_inputs=predecessors,
        claimed_at=claimed_at,
    )


def _replay(
    claim: LayerFinalizationClaim | None = None,
    *,
    evidence_pass: bool = True,
) -> LayerReplayReceipt:
    resolved = claim or _claim()
    layer_sha = resolved.layer_script_sha256
    replay_inputs = [
        ReplayInputBinding.mint(
            script_path=row.script_path,
            script_sha256=row.script_sha256,
        )
        for row in resolved.predecessor_inputs
    ]
    replay_inputs.append(
        ReplayInputBinding.mint(
            script_path=resolved.layer_script_path,
            script_sha256=layer_sha,
        )
    )
    prefix_layers = [
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
                    checkpoint_unit_digest=_digest(
                        f"predecessor unit {row.layer_id}"
                    ),
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
        for row in resolved.predecessor_inputs
    ]
    prefix_layers.append(
        ReplayPrefixLayerReceipt(
            layer_id=resolved.layer_id,
            layer_generation_digest=resolved.plan_hash,
            predecessor_layer_digests=(),
            script_path=resolved.layer_script_path,
            script_sha256=layer_sha,
            dependencies=(),
            units=tuple(
                ReplayPrefixUnitReceipt(
                    layer_id=resolved.layer_id,
                    unit_id=row.unit_id,
                    unit_digest=row.unit_digest,
                    checkpoint_unit_digest=row.unit_digest,
                    script_path=row.script_path,
                    script_sha256=row.script_sha256,
                    checkpoint_script_sha256=row.script_sha256,
                    completion_receipt_digest=row.completion_receipt_digest,
                )
                for row in resolved.unit_inputs
            ),
            payer_claim_id=resolved.claim_id,
        )
    )
    plan = LayerReplayEvaluationGroupPlan(
        group_index=0,
        planned_group_count=1,
        requirement_ids=(),
        debt_id=None,
        definition_digest=None,
        activation_digest=None,
        payment_generation_digest=None,
        judge_points=((1, "refs/frame.png"),),
        axes=("fixture",),
        claims=(
            LayerReplayClaimRequirement(
                claim_id="fixture-claim",
                authority="executable_required",
                judge_frames=(1,),
                evidence_ids=("fixture-contract",),
            ),
        ),
        evidence_kind="executable_only",
        render_mode=None,
        render_scale=None,
    )
    point = LayerReplayPointObservation.mint(
        plan=plan,
        frame=1,
        ref="refs/frame.png",
        ref_sha256=_digest("fixture ref"),
        evidence=(
            {
                "id": "fixture-contract",
                "metric": "fixture",
                "value": 1,
                "target": "= 1",
                "pass": evidence_pass,
                "source": "scene_contract",
                "authoritative": True,
                "owner_layer": resolved.layer_id,
                "fault_owner": resolved.layer_id,
            },
        ),
    )
    return LayerReplayReceipt.mint(
        claim=resolved,
        layer_script_sha256=layer_sha,
        replay_inputs=replay_inputs,
        observation=LayerReplayObservation(
            replay_prefix=ReplayPrefixReceipt(tuple(prefix_layers)),
            plan=plan,
            points=(point,),
        ),
        created_at="2026-09-01T10:01:00+00:00",
    )


def _qualitative_replay(
    decision: dict,
    claim: LayerFinalizationClaim | None = None,
) -> tuple[LayerReplayReceipt, JudgmentObservationRequest, tuple[dict, ...]]:
    resolved_claim = claim or _claim()
    base = _replay(resolved_claim)
    capture_payload = {
        "schema": "vfx-harness.canonical-render-capture/v1",
        "frame": 1,
        "mode": "eevee",
        "scale": 0.5,
        "resolution": [32, 32, 50],
        "render_state": {"fixture": True},
        "warnings": [],
        "png_sha256": _digest("qualitative render"),
    }
    capture = {
        **capture_payload,
        "capture_digest": canonical_digest(capture_payload),
    }
    plan = LayerReplayEvaluationGroupPlan(
        group_index=0,
        planned_group_count=1,
        requirement_ids=(decision["id"],),
        debt_id=decision["debt_id"],
        definition_digest=decision["definition_digest"],
        activation_digest=decision["activation_digest"],
        payment_generation_digest=_digest("payment generation"),
        judge_points=((1, "refs/frame.png"),),
        axes=("composition",),
        claims=(
            LayerReplayClaimRequirement(
                claim_id=decision["id"],
                authority="qualified_qualitative_required",
                judge_frames=(1,),
                evidence_ids=(),
            ),
        ),
        evidence_kind="render",
        render_mode="eevee",
        render_scale=0.5,
    )
    point = LayerReplayPointObservation.mint(
        plan=plan,
        frame=1,
        ref="refs/frame.png",
        ref_sha256=_digest("reference"),
        evidence=(),
        render="runs/fixture/evidence/render.png",
        render_sha256=capture["png_sha256"],
        render_capture=capture,
    )
    replay = LayerReplayReceipt.mint(
        claim=resolved_claim,
        layer_script_sha256=resolved_claim.layer_script_sha256,
        replay_inputs=base.replay_inputs,
        observation=LayerReplayObservation(
            replay_prefix=base.observation.replay_prefix,
            plan=plan,
            points=(point,),
        ),
        created_at="2026-09-01T10:01:00+00:00",
    )
    request = JudgmentObservationRequest(
        definition_digest=decision["definition_digest"],
        activation_digest=decision["activation_digest"],
        payment_generation_digest=plan.payment_generation_digest,
        bundle_digest=_digest("payment bundle"),
        owner_view_digest=_digest("owner view"),
        payer_view_digest=_digest("payer view"),
        layer_replay_receipt_digest=replay.receipt_digest,
        replay_receipt_digest=replay.observation.replay_prefix.digest,
        parent_chain_digest=replay_parent_chain_digest(
            replay.observation.replay_prefix
        ),
        judge_point=JudgmentPoint(frame=1, ref="refs/frame.png"),
        observation_medium="eevee",
        render_mode="eevee",
        render_scale=0.5,
        reference_digest=point.ref_sha256,
        reference_marker=None,
        observation_environment_digest=_digest("observation environment"),
        external_asset_provenance_digest=_digest("external assets"),
        comparison_config_digest=_digest("comparison config"),
        judge_config_digest=_digest("judge config"),
    )
    canonical = (
        {
            "frame": 1,
            "ref": point.ref,
            "verdict": {
                "evidence_kind": "render",
                "pass": True,
                "issues": [],
                "evidence": [],
                "evidence_failures": [],
                "missing_evidence": [],
                "decided_by": "critic",
                "scores": {"composition": 4.5},
                "mean": 4.5,
                "render": point.render,
                "render_capture": point.render_capture,
                "layer_replay_receipt_digest": replay.receipt_digest,
                "judgment_observation": {
                    "request": request.as_dict(),
                    "candidate_capture": point.render_capture,
                    "reused_attempt": None,
                },
            },
        },
    )
    return replay, request, canonical


def _terminal(replay: LayerReplayReceipt | None = None) -> LayerFinalizationReceipt:
    resolved = replay or _replay()
    replay_bytes = canonical_layer_replay_receipt_bytes(resolved)
    point_pass = resolved.observation.points[0].deterministic_status == "passed"
    status = "passed" if point_pass else "failed"
    best = {"round": 0, "mean": 1.0 if point_pass else 0.0, "render": None}
    receipt_canonical = (
        {
            "frame": 1,
            "ref": "refs/frame.png",
            "verdict": {
                "evidence_kind": "executable_only",
                "pass": point_pass,
                "issues": [] if point_pass else ["fixture failure"],
                "evidence": list(resolved.observation.points[0].evidence),
                "evidence_failures": [
                    row
                    for row in resolved.observation.points[0].evidence
                    if row["authoritative"] is True and row["pass"] is False
                ],
                "missing_evidence": [],
                "decided_by": "unit_executable_evidence",
                "layer_replay_receipt_digest": resolved.receipt_digest,
            },
        },
    )
    manifest: dict = {}
    authoritative = [
        {
            key: resolved.observation.points[0].evidence[0].get(key)
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
    outcome = LayerOutcomeProjection.from_canonical(
        claim=resolved.claim,
        layer_title="Fixture layer",
        layer_script_path=resolved.claim.layer_script_path,
        final_status=status,
        best=best,
        revalidation_manifest=manifest,
        canonical=[
            {
                "evidence_kind": "executable_only",
                "frame": 1,
                "ref": "refs/frame.png",
                "ref_sha256": _digest("fixture ref"),
                "input_manifest_sha256": canonical_digest(manifest),
                "authoritative": authoritative,
                "authoritative_sha256": canonical_digest(
                    {"authoritative": authoritative}
                ),
                "qualitative_defects": [] if point_pass else ["fixture failure"],
            }
        ],
        receipt_canonical=receipt_canonical,
        blender_version="fixture",
    )
    projection = {
        "schema": LAYER_FINALIZATION_PROJECTION_SCHEMA,
        "best": best,
        "blender_version": "fixture",
        "ablation": {"ok": True, "note": "fixture ablation"},
        "revalidation": {
            "schema": "vfx-harness.layer-image-check-revalidation/v1",
            "layer_id": resolved.claim.layer_id,
            "source_sha256": None,
            "replacement_sha256": None,
            "replacement_text": None,
            "result": {"kept": 0, "dropped": []},
        },
        "judgment_debts": [],
        "finding": None,
        "outcome": outcome.as_dict(),
        "ledger": {
            "status": status,
            "script": resolved.claim.layer_script_path,
            "script_sha256": resolved.layer_script_sha256,
        },
    }
    replay_locator = (
        f"runs/{resolved.claim.run_id}/checkpoints/layer-finalizations/"
        f"{resolved.claim.claim_id}.group-0.replay.json"
    )
    evaluation = LayerEvaluationReceipt.mint(
        replay_receipts=(
            LayerReplayReceiptBinding.mint(
                locator=replay_locator,
                sha256=hashlib.sha256(replay_bytes).hexdigest(),
                receipt=resolved,
            ),
        ),
        evaluation_groups=[
            {
                "group_index": 0,
                "result": status,
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
    evaluation_bytes = canonical_layer_evaluation_receipt_bytes(evaluation)
    return LayerFinalizationReceipt.mint(
        evaluation_receipt=evaluation,
        evaluation_receipt_locator=(
            f"runs/{resolved.claim.run_id}/checkpoints/layer-finalizations/"
            f"{resolved.claim.claim_id}.evaluation.json"
        ),
        evaluation_receipt_sha256=hashlib.sha256(evaluation_bytes).hexdigest(),
        projection=projection,
        completed_at="2026-09-01T10:02:00+00:00",
    )


def _mint_terminal(
    replay: LayerReplayReceipt,
    *,
    status: str,
    canonical: list[dict],
    projection: dict,
) -> LayerFinalizationReceipt:
    replay_bytes = canonical_layer_replay_receipt_bytes(replay)
    normalized_canonical = copy.deepcopy(canonical)
    if status != "passed":
        for row in normalized_canonical:
            verdict = row["verdict"]
            verdict["pass"] = False
            verdict["issues"] = [] if status == "contract_gap" else [status]
            verdict["contract_gap"] = status == "contract_gap"
            verdict["judge_conflict"] = status == "judge_conflict"
    result = (
        "passed"
        if status == "passed"
        else status
        if status in {"contract_gap", "judge_conflict"}
        else "failed"
    )
    replay_locator = (
        f"runs/{replay.claim.run_id}/checkpoints/layer-finalizations/"
        f"{replay.claim.claim_id}.group-0.replay.json"
    )
    plan = replay.observation.plan
    projected_debt = next(
        (
            row
            for row in projection.get("judgment_debts", [])
            if row["decision"]["debt_id"] == plan.debt_id
        ),
        None,
    )
    evaluation = LayerEvaluationReceipt.mint(
        replay_receipts=(
            LayerReplayReceiptBinding.mint(
                locator=replay_locator,
                sha256=hashlib.sha256(replay_bytes).hexdigest(),
                receipt=replay,
            ),
        ),
        evaluation_groups=[
            {
                "group_index": 0,
                "result": result,
                "requirement_ids": list(plan.requirement_ids),
                "debt_id": plan.debt_id,
                "definition_digest": plan.definition_digest,
                "activation_digest": plan.activation_digest,
                "canonical_start": 0,
                "canonical_end": len(normalized_canonical),
                "payment_failures": (
                    [] if projected_debt is None else projected_debt["payment_failures"]
                ),
            }
        ],
        canonical=normalized_canonical,
        created_at="2026-09-01T10:01:30+00:00",
    )
    evaluation_bytes = canonical_layer_evaluation_receipt_bytes(evaluation)
    return LayerFinalizationReceipt.mint(
        evaluation_receipt=evaluation,
        evaluation_receipt_locator=(
            f"runs/{replay.claim.run_id}/checkpoints/layer-finalizations/"
            f"{replay.claim.claim_id}.evaluation.json"
        ),
        evaluation_receipt_sha256=hashlib.sha256(evaluation_bytes).hexdigest(),
        projection=projection,
        completed_at="2026-09-01T10:02:00+00:00",
    )


def _rehash_finding(payload: dict) -> dict:
    payload["record_id"] = "pending"
    identity = dict(payload)
    identity.pop("record_id")
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    payload["record_id"] = f"hf-{digest[:20]}"
    return payload


def _finding(claim: LayerFinalizationClaim) -> dict:
    source = claim.unit_inputs[0]
    payload = {
        "schema": "vfx-harness.hypothesis-falsification/v1",
        "record_id": "pending",
        "recorded_at": "2026-09-01T10:02:00+00:00",
        "layer": claim.layer_id,
        "unit": source.unit_id,
        "identities": {
            "bundle_hash": _digest("bundle"),
            "plan_hash": claim.plan_hash,
            "unit_hash": source.unit_digest,
            "unit_plan_hash": _digest("unit plan"),
            "candidate_hash": _digest("candidate"),
            "settings_hash": _digest("settings"),
        },
        "contract_ids": ["contract-1"],
        "observations": [{"message": "composition gap"}],
        "decisions": [{"id": "decision-1", "strength": "approved_start"}],
        "conflict": {
            "kind": "decision",
            "required_authority": "amend the producer graph",
            "roles": ["hero"],
            "controls": [],
        },
        "evidence": ["state/contract-gaps.jsonl"],
        "affected": ["camera"],
        "fault_owner_units": [],
    }
    return _rehash_finding(payload)


def _judgment_decision() -> dict:
    return {
        "id": "requirement-1",
        "debt_id": "jd-" + _digest("debt"),
        "definition_digest": _digest("definition"),
        "activation_digest": _digest("activation"),
        "statement": "The hero remains framed.",
        "decision_strength": "approved_start",
        "evidence_domains": ["image"],
        "claim_kind": "atomic",
        "property": "camera_framing",
        "fault_owner": "3.camera",
        "subject_roles": ["hero"],
        "axes": ["composition"],
        "judge_points": [[1, "refs/frame.png"]],
        "carrier_families": ["mesh"],
        "observation_medium": "eevee",
        "lifecycle": "persistent",
        "state": "pending_not_due",
    }


def _typed_contract_gap_case(
    *,
    claim: LayerFinalizationClaim | None = None,
    decision: dict | None = None,
) -> tuple[LayerReplayReceipt, list[dict], dict]:
    resolved_decision = decision or _judgment_decision()
    replay, _request, canonical = _qualitative_replay(
        resolved_decision,
        claim,
    )
    gap_observation = {
        "state": "contract_gap",
        "check_ids": ["contract-1"],
        "observation": {
            "claim_id": resolved_decision["id"],
            "roles": ["hero"],
        },
        "reason": "the declared observation cannot be paid by this authority",
    }
    gap_canonical = copy.deepcopy(list(canonical))
    verdict = gap_canonical[0]["verdict"]
    verdict.update(
        {
            "pass": False,
            "issues": [],
            "contract_gap": True,
            "judge_conflict": False,
            "contract_gaps": [gap_observation],
            "observation_reconciliation": [gap_observation],
        }
    )
    point = replay.observation.points[0]
    source = replay.claim.unit_inputs[0]
    finding = _rehash_finding(
        {
            "schema": "vfx-harness.hypothesis-falsification/v1",
            "record_id": "pending",
            "recorded_at": "2026-09-01T10:02:00+00:00",
            "layer": replay.claim.layer_id,
            "unit": source.unit_id,
            "identities": {
                "bundle_hash": _digest("bundle"),
                "plan_hash": replay.claim.plan_hash,
                "unit_hash": source.unit_digest,
                "unit_plan_hash": _digest("unit plan"),
                "candidate_hash": point.render_sha256,
                "settings_hash": hypothesis_falsification_render_settings_hash(
                    mode=replay.observation.plan.render_mode,
                    scale=replay.observation.plan.render_scale,
                    frame=point.frame,
                    reference=point.ref,
                    reference_sha256=point.ref_sha256,
                ),
            },
            "contract_ids": sorted(["contract-1", resolved_decision["id"]]),
            "observations": [gap_observation],
            "decisions": [
                {
                    "id": resolved_decision["id"],
                    "strength": resolved_decision["decision_strength"],
                }
            ],
            "conflict": {
                "kind": "decision",
                "required_authority": "amend the producer graph",
                "roles": ["hero"],
                "controls": [],
            },
            "evidence": ["state/contract-gaps.jsonl"],
            "affected": [source.unit_id],
            "fault_owner_units": [],
        }
    )
    manifest: dict = {}
    best = {"round": 0, "mean": 0.0, "render": point.render}
    projected_canonical = [
        {
            "evidence_kind": "render",
            "frame": point.frame,
            "ref": point.ref,
            "ref_sha256": point.ref_sha256,
            "input_manifest_sha256": canonical_digest(manifest),
            "authoritative": [],
            "authoritative_sha256": canonical_digest({"authoritative": []}),
            "qualitative_defects": [],
            "render": point.render,
            "render_sha256": point.render_sha256,
            "render_capture": point.render_capture,
        }
    ]
    outcome = LayerOutcomeProjection.from_canonical(
        claim=replay.claim,
        layer_title="Fixture layer",
        layer_script_path=replay.claim.layer_script_path,
        final_status="contract_gap",
        best=best,
        revalidation_manifest=manifest,
        canonical=projected_canonical,
        receipt_canonical=gap_canonical,
        blender_version="fixture",
    )
    evidence_digest = canonical_digest(
        {
            "schema": "vfx-harness.judgment-debt-payment-evidence/v1",
            "debt_id": resolved_decision["debt_id"],
            "definition_digest": resolved_decision["definition_digest"],
            "activation_digest": resolved_decision["activation_digest"],
            "result": "contract_gap",
            "verdicts": [
                ((point.frame, point.ref), dict(gap_canonical[0]["verdict"])),
            ],
            "finding_record_id": finding["record_id"],
        }
    )
    projection = {
        "schema": LAYER_FINALIZATION_PROJECTION_SCHEMA,
        "best": best,
        "blender_version": "fixture",
        "ablation": {"ok": False, "note": "not run for contract gap"},
        "revalidation": {
            "schema": "vfx-harness.layer-image-check-revalidation/v1",
            "layer_id": replay.claim.layer_id,
            "source_sha256": None,
            "replacement_sha256": None,
            "replacement_text": None,
            "result": {"kept": 0, "dropped": []},
        },
        "judgment_debts": [
            {
                "decision": resolved_decision,
                "result": "contract_gap",
                "canonical_start": 0,
                "canonical_end": 1,
                "payment_failures": [],
                "resolution": {
                    "outcome": "falsified",
                    "evidence_digest": evidence_digest,
                },
            }
        ],
        "finding": finding,
        "outcome": outcome.as_dict(),
        "ledger": {
            "status": "contract_gap",
            "script": replay.claim.layer_script_path,
            "script_sha256": replay.layer_script_sha256,
        },
    }
    return replay, gap_canonical, projection


def _refresh_gap_resolution(projection: dict, canonical: list[dict]) -> None:
    debt = projection["judgment_debts"][0]
    decision = debt["decision"]
    debt["resolution"]["evidence_digest"] = canonical_digest(
        {
            "schema": "vfx-harness.judgment-debt-payment-evidence/v1",
            "debt_id": decision["debt_id"],
            "definition_digest": decision["definition_digest"],
            "activation_digest": decision["activation_digest"],
            "result": "contract_gap",
            "verdicts": [
                (
                    (canonical[0]["frame"], canonical[0]["ref"]),
                    dict(canonical[0]["verdict"]),
                )
            ],
            "finding_record_id": (
                None
                if projection["finding"] is None
                else projection["finding"]["record_id"]
            ),
        }
    )


def _payment_failure(decision: dict) -> dict:
    request = JudgmentObservationRequest(
        definition_digest=decision["definition_digest"],
        activation_digest=decision["activation_digest"],
        payment_generation_digest=_digest("payment generation"),
        bundle_digest=_digest("payment bundle"),
        owner_view_digest=_digest("owner view"),
        payer_view_digest=_digest("payer view"),
        replay_receipt_digest=_digest("replay prefix"),
        parent_chain_digest=_digest("parent chain"),
        judge_point=JudgmentPoint(frame=1, ref="refs/frame.png"),
        observation_medium="eevee",
        render_mode="eevee",
        render_scale=0.5,
        reference_digest=_digest("reference"),
        reference_marker=None,
        observation_environment_digest=_digest("observation environment"),
        external_asset_provenance_digest=_digest("external assets"),
        comparison_config_digest=_digest("comparison config"),
        judge_config_digest=_digest("judge config"),
    )
    failure = JudgmentPaymentAttemptFailure.for_request(
        request,
        reason="no_optical_signal",
        signal_metrics_digest=_digest("signal metrics"),
        candidate_capture_digest=_digest("candidate capture"),
    )
    return {"request": request.as_dict(), "failure": failure.as_dict()}


def test_finalization_claim_is_strict_and_timestamp_independent() -> None:
    first = _claim()
    later = _claim(claimed_at="2026-09-01T10:30:00+00:00")

    assert first.claim_id == later.claim_id
    assert LayerFinalizationClaim.parse(first.as_dict()) == first

    extra = {**first.as_dict(), "guess": True}
    with pytest.raises(ValueError, match=r"unexpected=.*guess"):
        LayerFinalizationClaim.parse(extra)

    tampered = first.as_dict()
    tampered["unit_inputs"][0]["script_sha256"] = _digest("different")
    with pytest.raises(ValueError, match="claim_id does not match"):
        LayerFinalizationClaim.parse(tampered)

    changed_script = first.as_dict()
    changed_script["layer_script_sha256"] = _digest("different composition")
    with pytest.raises(ValueError, match="claim_id does not match"):
        LayerFinalizationClaim.parse(changed_script)

    assert first.claim_id != LayerFinalizationClaim.mint(
        attempt_revision=first.attempt_revision,
        run_id=first.run_id,
        layer_id=first.layer_id,
        mode=first.mode,
        selection_token=first.selection_token,
        plan_hash=first.plan_hash,
        layer_script_path=first.layer_script_path,
        layer_script_sha256=_digest("different composition"),
        unit_inputs=first.unit_inputs,
        predecessor_inputs=first.predecessor_inputs,
        claimed_at=first.claimed_at,
    ).claim_id


def test_finalization_claim_enforces_mode_and_disjoint_paths() -> None:
    unit = _unit_input("camera")
    with pytest.raises(ValueError, match="at least two"):
        LayerFinalizationClaim.mint(
            attempt_revision=1,
            run_id="run-1",
            layer_id="3",
            mode="multi_unit_fan_in",
            selection_token=_token(),
            plan_hash=_digest("plan"),
            layer_script_path="build/layer_3.py",
            layer_script_sha256=_digest("composed layer"),
            unit_inputs=(unit,),
            predecessor_inputs=(),
            claimed_at="now",
        )

    same_path = LayerFinalizationPredecessorInput.mint(
        layer_id="1",
        finalization_receipt_digest=_digest("prior receipt"),
        script_path=unit.script_path,
        script_sha256=_digest("prior script"),
    )
    with pytest.raises(ValueError, match="must be disjoint"):
        _claim(predecessors=(same_path,))

    with pytest.raises(ValueError, match="canonical relative POSIX path"):
        LayerFinalizationUnitInput.mint(
            unit_id="camera",
            unit_digest=_digest("unit"),
            completion_receipt_digest=_digest("receipt"),
            script_path="build/../escape.py",
            script_sha256=_digest("script"),
        )


def test_multi_unit_claim_preserves_exact_authored_input_order() -> None:
    claim = _claim(
        units=(_unit_input("geometry"), _unit_input("look")),
        predecessors=(_predecessor("1"), _predecessor("2")),
    )

    parsed = LayerFinalizationClaim.parse(claim.as_dict())
    assert [row.unit_id for row in parsed.unit_inputs] == ["geometry", "look"]
    assert [row.layer_id for row in parsed.predecessor_inputs] == ["1", "2"]


def test_replay_receipt_is_pre_judgment_and_semantically_digested() -> None:
    replay = _replay()
    parsed = LayerReplayReceipt.parse(replay.as_dict())
    assert parsed == replay
    assert parsed.replay_status == "ready"
    assert "canonical" not in parsed.as_dict()
    assert "status" not in parsed.as_dict()

    with pytest.raises(ValueError, match="must match the proposed composed bytes"):
        LayerReplayReceipt.mint(
            claim=replay.claim,
            layer_script_sha256=_digest("unclaimed layer script"),
            replay_inputs=replay.replay_inputs,
            observation=replay.observation,
            created_at=replay.created_at,
        )

    later = LayerReplayReceipt.mint(
        claim=replay.claim,
        layer_script_sha256=replay.layer_script_sha256,
        replay_inputs=replay.replay_inputs,
        observation=replay.observation,
        created_at="2026-09-01T11:00:00+00:00",
    )
    assert later.receipt_digest == replay.receipt_digest
    assert canonical_layer_replay_receipt_bytes(replay).endswith(b"\n")


def test_replay_receipt_requires_the_exact_prefix_and_finite_observation() -> None:
    claim = _claim(predecessors=(_predecessor(),))
    with pytest.raises(ValueError, match="exact ordered predecessor prefix"):
        LayerReplayReceipt.mint(
            claim=claim,
            layer_script_sha256=claim.layer_script_sha256,
            replay_inputs=(
                ReplayInputBinding.mint(
                    script_path=claim.layer_script_path,
                    script_sha256=claim.layer_script_sha256,
                ),
            ),
            observation={},
            created_at="now",
        )

    replay = _replay()
    invalid = replay.as_dict()
    invalid["observation"]["points"][0]["evidence"][0]["value"] = float(
        "nan"
    )
    with pytest.raises(ValueError, match="finite JSON"):
        LayerReplayReceipt.parse(invalid)


def test_terminal_receipt_carries_reconciliation_payload_and_is_strict() -> None:
    replay = _replay()
    terminal = _terminal(replay)
    parsed = LayerFinalizationReceipt.parse(terminal.as_dict())

    parsed.assert_matches_evaluation(terminal.evaluation_receipt)
    assert parsed.projection["ledger"]["status"] == "passed"
    assert canonical_layer_finalization_receipt_bytes(parsed).endswith(b"\n")

    tampered = terminal.as_dict()
    tampered["projection"]["best"]["mean"] = 2.0
    with pytest.raises(ValueError, match=r"outcome\.best does not match"):
        LayerFinalizationReceipt.parse(tampered)

    extra = {**terminal.as_dict(), "legacy_projection": {}}
    with pytest.raises(ValueError, match=r"unexpected=.*legacy_projection"):
        LayerFinalizationReceipt.parse(extra)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ref", "refs/substituted.png"),
        ("ref_sha256", _digest("substituted reference")),
        ("qualitative_defects", ["invented defect"]),
    ],
)
def test_terminal_receipt_rejects_outcome_canonical_point_substitution(
    field: str,
    value: object,
) -> None:
    replay = _replay()
    baseline = _terminal(replay)
    projection = copy.deepcopy(baseline.projection)
    projection["outcome"]["canonical"][0][field] = value

    with pytest.raises(ValueError, match="exact evaluated replay point"):
        _mint_terminal(
            replay,
            status="passed",
            canonical=list(baseline.canonical),
            projection=projection,
        )


def test_terminal_receipt_digest_excludes_only_completion_time() -> None:
    replay = _replay()
    first = _terminal(replay)
    evaluation_bytes = canonical_layer_evaluation_receipt_bytes(
        first.evaluation_receipt
    )
    later = LayerFinalizationReceipt.mint(
        evaluation_receipt=first.evaluation_receipt,
        evaluation_receipt_locator=first.evaluation_receipt_locator,
        evaluation_receipt_sha256=hashlib.sha256(evaluation_bytes).hexdigest(),
        projection=first.projection,
        completed_at="2026-09-01T12:00:00+00:00",
    )

    assert first.receipt_digest == later.receipt_digest
    changed = copy.deepcopy(first.as_dict())
    changed["canonical"][0]["frame"] = 2
    with pytest.raises(ValueError, match="does not match its sealed replay point"):
        LayerFinalizationReceipt.parse(changed)


def test_terminal_projection_rejects_missing_unknown_and_mismatched_fields() -> None:
    replay = _replay()
    baseline = _terminal(replay)
    canonical = list(baseline.canonical)

    missing = copy.deepcopy(baseline.projection)
    missing.pop("best")
    with pytest.raises(ValueError, match=r"missing=.*best"):
        _mint_terminal(
            replay,
            status="passed",
            canonical=canonical,
            projection=missing,
        )

    unknown = copy.deepcopy(baseline.projection)
    unknown["best"]["legacy_score"] = 4
    with pytest.raises(ValueError, match=r"projection.best fields mismatch.*legacy_score"):
        _mint_terminal(
            replay,
            status="passed",
            canonical=canonical,
            projection=unknown,
        )

    stale_ledger = copy.deepcopy(baseline.projection)
    stale_ledger["ledger"]["script_sha256"] = _digest("other script")
    with pytest.raises(ValueError, match="must match the finalization receipt"):
        _mint_terminal(
            replay,
            status="passed",
            canonical=canonical,
            projection=stale_ledger,
        )

    stale_outcome = copy.deepcopy(baseline.projection)
    stale_outcome["outcome"]["status"] = "failed"
    with pytest.raises(ValueError, match=r"outcome\.status does not match"):
        _mint_terminal(
            replay,
            status="passed",
            canonical=canonical,
            projection=stale_outcome,
        )


def test_terminal_projection_strictly_rejects_schema_one() -> None:
    replay = _replay()
    baseline = _terminal(replay)
    legacy = copy.deepcopy(baseline.projection)
    legacy["schema"] = "vfx-harness.layer-finalization-projection/v1"

    with pytest.raises(
        ValueError,
        match=r"projection\.schema must be .*layer-finalization-projection/v3",
    ):
        _mint_terminal(
            replay,
            status="passed",
            canonical=list(baseline.canonical),
            projection=legacy,
        )


def test_terminal_projection_validates_ablation_and_exact_revalidation_bytes() -> None:
    replay = _replay()
    baseline = _terminal(replay)
    canonical = list(baseline.canonical)
    projection = copy.deepcopy(baseline.projection)
    projection["ablation"] = {
        "ok": True,
        "frames": [1, 40],
        "moved": {"f40:detail": 0.125},
        "note": "",
    }
    replacement = '{"checks":[]}\n'
    projection["revalidation"].update(
        replacement_text=replacement,
        replacement_sha256=hashlib.sha256(replacement.encode()).hexdigest(),
    )
    receipt = _mint_terminal(
        replay,
        status="passed",
        canonical=canonical,
        projection=projection,
    )
    assert receipt.projection == projection

    stale = copy.deepcopy(projection)
    stale["revalidation"]["replacement_sha256"] = _digest("stale replacement")
    with pytest.raises(ValueError, match="does not match exact text"):
        _mint_terminal(
            replay,
            status="passed",
            canonical=canonical,
            projection=stale,
        )

    extra = copy.deepcopy(projection)
    extra["ablation"]["legacy"] = True
    with pytest.raises(ValueError, match="ablation fields must be exactly"):
        _mint_terminal(
            replay,
            status="passed",
            canonical=canonical,
            projection=extra,
        )


def test_terminal_projection_binds_exact_finding_payload() -> None:
    replay, canonical, projection = _typed_contract_gap_case()
    receipt = _mint_terminal(
        replay,
        status="contract_gap",
        canonical=canonical,
        projection=projection,
    )
    assert receipt.projection["finding"] == projection["finding"]

    stale = copy.deepcopy(projection)
    stale["finding"]["observations"][0]["message"] = "different gap"
    with pytest.raises(ValueError, match="record_id does not match"):
        _mint_terminal(
            replay,
            status="contract_gap",
            canonical=canonical,
            projection=stale,
        )

    extra = copy.deepcopy(projection)
    extra["finding"]["legacy"] = True
    with pytest.raises(ValueError, match=r"finding fields mismatch.*legacy"):
        _mint_terminal(
            replay,
            status="contract_gap",
            canonical=canonical,
            projection=extra,
        )

    wrong_source = copy.deepcopy(projection)
    wrong_source["finding"]["identities"]["unit_hash"] = _digest(
        "other source unit"
    )
    with pytest.raises(ValueError, match="must match its claimed source unit"):
        _mint_terminal(
            replay,
            status="contract_gap",
            canonical=canonical,
            projection=wrong_source,
        )


def test_terminal_projection_rejects_finding_without_typed_contract_gap() -> None:
    replay = _replay()
    baseline = _terminal(replay)
    projection = copy.deepcopy(baseline.projection)
    projection["finding"] = _finding(replay.claim)

    with pytest.raises(
        ValueError,
        match="requires exactly one typed contract-gap evaluation group",
    ):
        _mint_terminal(
            replay,
            status="passed",
            canonical=list(baseline.canonical),
            projection=projection,
        )


def test_terminal_projection_rejects_finding_on_failed_evaluation() -> None:
    replay = _replay(evidence_pass=False)
    baseline = _terminal(replay)
    projection = copy.deepcopy(baseline.projection)
    projection["finding"] = _finding(replay.claim)
    projection["outcome"]["canonical"][0]["qualitative_defects"] = ["failed"]

    with pytest.raises(
        ValueError,
        match="requires exactly one typed contract-gap evaluation group",
    ):
        _mint_terminal(
            replay,
            status="failed",
            canonical=list(baseline.canonical),
            projection=projection,
        )


def test_terminal_projection_rejects_finding_on_judge_conflict() -> None:
    replay, canonical, projection = _typed_contract_gap_case()
    conflict_canonical = copy.deepcopy(canonical)
    conflict_verdict = conflict_canonical[0]["verdict"]
    conflict_verdict.update(
        {
            "pass": False,
            "issues": ["judge_conflict"],
            "contract_gap": False,
            "judge_conflict": True,
        }
    )
    projection["judgment_debts"][0].update(
        result="judge_conflict",
        resolution=None,
    )
    projection["ledger"]["status"] = "judge_conflict"
    projection["outcome"] = LayerOutcomeProjection.from_canonical(
        claim=replay.claim,
        layer_title="Fixture layer",
        layer_script_path=replay.claim.layer_script_path,
        final_status="judge_conflict",
        best=projection["best"],
        revalidation_manifest={},
        canonical=[
            {
                **projection["outcome"]["canonical"][0],
                "qualitative_defects": ["judge_conflict"],
            }
        ],
        receipt_canonical=conflict_canonical,
        blender_version="fixture",
    ).as_dict()

    with pytest.raises(
        ValueError,
        match="requires exactly one typed contract-gap evaluation group",
    ):
        _mint_terminal(
            replay,
            status="judge_conflict",
            canonical=conflict_canonical,
            projection=projection,
        )


def test_terminal_projection_requires_finding_for_typed_contract_gap() -> None:
    replay, canonical, projection = _typed_contract_gap_case()
    projection["finding"] = None
    _refresh_gap_resolution(projection, canonical)

    with pytest.raises(ValueError, match="requires the exact falsification finding"):
        _mint_terminal(
            replay,
            status="contract_gap",
            canonical=canonical,
            projection=projection,
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda finding: finding["decisions"][0].update(id="other-decision"),
            "decisions do not match",
        ),
        (
            lambda finding: finding["decisions"][0].update(
                strength="hard_constraint"
            ),
            "decisions do not match",
        ),
        (
            lambda finding: finding.update(
                recorded_at="2026-09-01T10:03:00+00:00"
            ),
            "recorded_at must match",
        ),
        (
            lambda finding: finding["identities"].update(
                candidate_hash=_digest("other candidate")
            ),
            "candidate/settings do not identify",
        ),
        (
            lambda finding: finding["identities"].update(
                settings_hash=_digest("other settings")
            ),
            "candidate/settings do not identify",
        ),
        (
            lambda finding: finding.update(
                observations=[{"state": "contract_gap", "message": "substituted"}]
            ),
            "observations do not match",
        ),
        (
            lambda finding: finding.update(contract_ids=["unrelated-contract"]),
            "contract_ids are not derived",
        ),
    ],
)
def test_terminal_projection_rejects_finding_not_earned_by_exact_gap_point(
    mutation,
    message: str,
) -> None:
    replay, canonical, projection = _typed_contract_gap_case()
    mutation(projection["finding"])
    _rehash_finding(projection["finding"])
    _refresh_gap_resolution(projection, canonical)

    with pytest.raises(ValueError, match=message):
        _mint_terminal(
            replay,
            status="contract_gap",
            canonical=canonical,
            projection=projection,
        )


def test_terminal_projection_binds_judgment_resolution_to_canonical_slice() -> None:
    decision = _judgment_decision()
    replay, _request, canonical = _qualitative_replay(decision)
    manifest: dict = {}
    best = {"round": 0, "mean": 4.5, "render": replay.observation.points[0].render}
    projected_canonical = [
        {
            "evidence_kind": "render",
            "frame": 1,
            "ref": replay.observation.points[0].ref,
            "ref_sha256": replay.observation.points[0].ref_sha256,
            "input_manifest_sha256": canonical_digest(manifest),
            "authoritative": [],
            "authoritative_sha256": canonical_digest({"authoritative": []}),
            "qualitative_defects": [],
            "render": replay.observation.points[0].render,
            "render_sha256": replay.observation.points[0].render_sha256,
            "render_capture": replay.observation.points[0].render_capture,
        }
    ]
    outcome = LayerOutcomeProjection.from_canonical(
        claim=replay.claim,
        layer_title="Fixture layer",
        layer_script_path=replay.claim.layer_script_path,
        final_status="passed",
        best=best,
        revalidation_manifest=manifest,
        canonical=projected_canonical,
        receipt_canonical=canonical,
        blender_version="fixture",
    )
    projection = {
        "schema": LAYER_FINALIZATION_PROJECTION_SCHEMA,
        "best": best,
        "blender_version": "fixture",
        "ablation": {"ok": True, "note": "fixture ablation"},
        "revalidation": {
            "schema": "vfx-harness.layer-image-check-revalidation/v1",
            "layer_id": replay.claim.layer_id,
            "source_sha256": None,
            "replacement_sha256": None,
            "replacement_text": None,
            "result": {"kept": 0, "dropped": []},
        },
        "judgment_debts": [],
        "finding": None,
        "outcome": outcome.as_dict(),
        "ledger": {
            "status": "passed",
            "script": replay.claim.layer_script_path,
            "script_sha256": replay.layer_script_sha256,
        },
    }
    evidence_digest = canonical_digest(
        {
            "schema": "vfx-harness.judgment-debt-payment-evidence/v1",
            "debt_id": decision["debt_id"],
            "definition_digest": decision["definition_digest"],
            "activation_digest": decision["activation_digest"],
            "result": "passed",
            "verdicts": [
                ((1, "refs/frame.png"), dict(canonical[0]["verdict"])),
            ],
            "finding_record_id": None,
        }
    )
    projection["judgment_debts"] = [
        {
            "decision": decision,
            "result": "passed",
            "canonical_start": 0,
            "canonical_end": 1,
            "payment_failures": [],
            "resolution": {
                "outcome": "satisfied",
                "evidence_digest": evidence_digest,
            },
        }
    ]
    receipt = _mint_terminal(
        replay,
        status="passed",
        canonical=list(canonical),
        projection=projection,
    )
    assert (
        receipt.projection["judgment_debts"][0]["resolution"]["evidence_digest"]
        == evidence_digest
    )

    for field, value in (
        ("render", "runs/fixture/evidence/substituted.png"),
        ("render_sha256", _digest("substituted render")),
        (
            "render_capture",
            {
                **replay.observation.points[0].render_capture,
                "mode": "solid",
            },
        ),
    ):
        substituted = copy.deepcopy(projection)
        substituted["outcome"]["canonical"][0][field] = value
        with pytest.raises(ValueError, match="exact evaluated replay point"):
            _mint_terminal(
                replay,
                status="passed",
                canonical=list(canonical),
                projection=substituted,
            )

    stale = copy.deepcopy(projection)
    stale["judgment_debts"][0]["resolution"]["evidence_digest"] = _digest(
        "unrelated judgment"
    )
    with pytest.raises(ValueError, match="exact canonical judgment"):
        _mint_terminal(
            replay,
            status="passed",
            canonical=list(canonical),
            projection=stale,
        )

    unknown = copy.deepcopy(projection)
    unknown["judgment_debts"][0]["retry"] = True
    with pytest.raises(ValueError, match=r"judgment_debts\[0\] fields mismatch"):
        _mint_terminal(
            replay,
            status="passed",
            canonical=list(canonical),
            projection=unknown,
        )

    malformed_failure = copy.deepcopy(projection)
    malformed_failure["judgment_debts"][0]["payment_failures"] = [
        {"legacy": True}
    ]
    with pytest.raises(ValueError, match=r"payment_failures\[0\].*request and failure"):
        _mint_terminal(
            replay,
            status="passed",
            canonical=list(canonical),
            projection=malformed_failure,
        )

    legacy_prefix = copy.deepcopy(projection)
    legacy_prefix["judgment_debts"][0]["replayed_unit_digests"] = [
        [
            f"{replay.claim.layer_id}:{replay.claim.unit_inputs[0].unit_id}",
            replay.claim.unit_inputs[0].unit_digest,
        ]
    ]
    with pytest.raises(ValueError, match=r"unexpected=.*replayed_unit_digests"):
        _mint_terminal(
            replay,
            status="passed",
            canonical=list(canonical),
            projection=legacy_prefix,
        )

    unresolved = copy.deepcopy(projection)
    unresolved["judgment_debts"][0]["resolution"] = None
    with pytest.raises(ValueError, match="resolution must be an object"):
        _mint_terminal(
            replay,
            status="passed",
            canonical=list(canonical),
            projection=unresolved,
        )


def _completion_for_state(
    unit_id: str,
    *,
    plan_hash: str,
) -> UnitCompletionReceipt:
    unit_digest = _digest(f"unit {unit_id}")
    attempt = UnitAttemptClaim.mint(
        attempt_revision=1,
        run_id="20260901T090000Z-builder",
        layer_id="3",
        unit_id=unit_id,
        unit_digest=unit_digest,
        plan_hash=plan_hash,
        selection_token=_token_dict(),
        phase="building",
        at="2026-09-01T09:00:00+00:00",
    )
    return UnitCompletionReceipt.mint(
        claim=attempt,
        checkpoint={"candidate": _digest(f"candidate {unit_id}")},
        script_path=f"build/units/3/{unit_id}.py",
        script_hash=_digest(f"script {unit_id}"),
        evaluation_receipt_locator=f"runs/builder/{unit_id}.json",
        evaluation_receipt_sha256=_digest(f"evaluation file {unit_id}"),
        evaluation_receipt_digest=_digest(f"evaluation {unit_id}"),
        passed_evidence=(("scene_contract", f"check-{unit_id}"),),
        completed_at="2026-09-01T09:05:00+00:00",
    )


def _state_with_terminal() -> dict:
    plan_hash = _digest("state plan")
    completion = _completion_for_state("camera", plan_hash=plan_hash)
    unit_input = LayerFinalizationUnitInput.mint(
        unit_id="camera",
        unit_digest=completion.claim.unit_digest,
        completion_receipt_digest=completion.receipt_digest,
        script_path=completion.script_path,
        script_sha256=completion.script_hash,
    )
    claim = LayerFinalizationClaim.mint(
        attempt_revision=1,
        run_id="20260901T100000Z-finalize",
        layer_id="3",
        mode="singleton_passthrough",
        selection_token=_token(),
        plan_hash=plan_hash,
        layer_script_path="build/layer_3.py",
        layer_script_sha256=_digest("layer"),
        unit_inputs=(unit_input,),
        predecessor_inputs=(),
        claimed_at="2026-09-01T10:00:00+00:00",
    )
    terminal = _terminal(_replay(claim))
    return {
        "schema": 1,
        "layer": "3",
        "plan_hash": plan_hash,
        "units": {
            "camera": {
                "status": "passed",
                "unit_hash": completion.claim.unit_digest,
                "completion_receipt": completion.as_dict(),
            }
        },
        "layer_finalization": {
            "attempt_revision": 1,
            "active_claim": None,
            "terminal_receipt": terminal.as_dict(),
            "claim_history": [
                {
                    "schema": LAYER_FINALIZATION_CLAIM_ARCHIVE_SCHEMA,
                    "claim": claim.as_dict(),
                    "disposition": "completed",
                    "reason": "terminal receipt committed",
                    "evidence": [terminal.receipt_digest],
                    "at": "2026-09-01T10:02:00+00:00",
                }
            ],
            "receipt_history": [],
        },
    }


def test_state_finalization_contract_allows_absence_and_validates_current_source() -> None:
    validate_state_layer_finalization_contracts({})
    state = _state_with_terminal()
    validate_state_layer_finalization_contracts(state)

    stale = copy.deepcopy(state)
    stale["plan_hash"] = _digest("new plan")
    with pytest.raises(ValueError, match="plan_hash does not match"):
        validate_state_layer_finalization_contracts(stale)

    ambiguous = copy.deepcopy(state)
    ambiguous["layer_finalization"]["active_claim"] = (
        ambiguous["layer_finalization"]["terminal_receipt"]["claim"]
    )
    with pytest.raises(ValueError, match="mutually exclusive"):
        validate_state_layer_finalization_contracts(ambiguous)


def test_state_finalization_contract_rejects_bad_archives_and_revisions() -> None:
    state = _state_with_terminal()
    state["layer_finalization"]["claim_history"][0]["legacy"] = True
    with pytest.raises(ValueError, match="must contain exactly"):
        validate_state_layer_finalization_contracts(state)

    state = _state_with_terminal()
    state["layer_finalization"]["attempt_revision"] = 2
    with pytest.raises(ValueError, match="current authority revision"):
        validate_state_layer_finalization_contracts(state)
