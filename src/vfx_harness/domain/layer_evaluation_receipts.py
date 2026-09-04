"""Immutable, mechanically decided evaluation of ordered replay-group receipts."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from vfx_harness.domain.judgment_debt_observation import (
    JudgmentObservationRequest,
    JudgmentPaymentAttemptFailure,
)
from vfx_harness.domain.judgment_debt_replay_receipts import (
    replay_parent_chain_digest,
)
from vfx_harness.domain.layer_finalization_claims import (
    LayerFinalizationClaim,
    _relative_path,
    _text,
)
from vfx_harness.domain.layer_finalization_values import (
    json_object,
    json_object_rows,
    semantic_digest,
)
from vfx_harness.domain.layer_replay_contracts import (
    LayerReplayReceipt,
    canonical_layer_replay_receipt_bytes,
)
from vfx_harness.domain.stop_envelope_primitives import require_digest

LAYER_EVALUATION_RECEIPT_SCHEMA = "vfx-harness.layer-evaluation-receipt/v1"
LAYER_EVALUATION_RESULTS = frozenset(
    {"passed", "reproduced", "contract_gap", "judge_conflict", "failed"}
)

_RECEIPT_FIELDS = frozenset(
    {
        "schema",
        "receipt_digest",
        "claim",
        "replay_receipts",
        "evaluation_groups",
        "canonical",
        "final_status",
        "created_at",
    }
)
_REPLAY_BINDING_FIELDS = frozenset({"locator", "sha256", "receipt"})
_GROUP_FIELDS = frozenset(
    {
        "group_index",
        "result",
        "requirement_ids",
        "debt_id",
        "definition_digest",
        "activation_digest",
        "canonical_start",
        "canonical_end",
        "payment_failures",
    }
)
_CANONICAL_FIELDS = frozenset({"frame", "ref", "verdict"})


def _index(value: object, where: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{where} must be a non-negative integer")
    return value


@dataclass(frozen=True, slots=True)
class LayerReplayReceiptBinding:
    """Immutable external bytes and parsed content for one replay group."""

    locator: str
    sha256: str
    receipt: LayerReplayReceipt

    def __post_init__(self) -> None:
        _relative_path(self.locator, "layer replay receipt binding locator")
        require_digest(self.sha256, "layer replay receipt binding sha256")
        if not isinstance(self.receipt, LayerReplayReceipt):
            raise ValueError("layer replay receipt binding requires a typed receipt")
        expected_sha256 = hashlib.sha256(
            canonical_layer_replay_receipt_bytes(self.receipt)
        ).hexdigest()
        if self.sha256 != expected_sha256:
            raise ValueError(
                "layer replay receipt binding SHA-256 does not match embedded "
                "canonical bytes"
            )

    @classmethod
    def mint(
        cls,
        *,
        locator: object,
        sha256: object,
        receipt: LayerReplayReceipt,
    ) -> LayerReplayReceiptBinding:
        return cls(
            locator=_relative_path(locator, "layer replay receipt binding locator"),
            sha256=require_digest(sha256, "layer replay receipt binding sha256"),
            receipt=receipt,
        )

    @classmethod
    def from_dict(cls, value: object, where: str) -> LayerReplayReceiptBinding:
        if not isinstance(value, Mapping) or set(value) != _REPLAY_BINDING_FIELDS:
            raise ValueError(f"{where} has unsupported replay receipt binding shape")
        return cls.mint(
            locator=value["locator"],
            sha256=value["sha256"],
            receipt=LayerReplayReceipt.parse(value["receipt"], f"{where}.receipt"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "locator": self.locator,
            "sha256": self.sha256,
            "receipt": self.receipt.as_dict(),
        }


def _replay_bindings(
    value: Iterable[LayerReplayReceiptBinding],
) -> tuple[LayerReplayReceiptBinding, ...]:
    if isinstance(value, (str, bytes, Mapping, set, frozenset)):
        raise ValueError("layer evaluation replay receipts must be an ordered iterable")
    rows = tuple(value)
    if not rows or any(not isinstance(row, LayerReplayReceiptBinding) for row in rows):
        raise ValueError("layer evaluation requires typed replay receipt bindings")
    claim = rows[0].receipt.claim
    planned_count = rows[0].receipt.observation.plan.planned_group_count
    if any(row.receipt.claim != claim for row in rows):
        raise ValueError("layer evaluation replay receipts belong to different claims")
    if tuple(row.receipt.observation.group_index for row in rows) != tuple(
        range(len(rows))
    ):
        raise ValueError(
            "layer evaluation replay receipts must form an ordered contiguous prefix"
        )
    if any(
        row.receipt.observation.plan.planned_group_count != planned_count
        for row in rows
    ):
        raise ValueError("layer evaluation replay receipts disagree on planned group count")
    if len(rows) > planned_count:
        raise ValueError("layer evaluation replay receipts exceed planned group count")
    first = rows[0].receipt
    if any(
        row.receipt.replay_inputs != first.replay_inputs
        or row.receipt.observation.replay_prefix != first.observation.replay_prefix
        for row in rows[1:]
    ):
        raise ValueError(
            "layer evaluation replay groups do not share the exact replay prefix"
        )
    locators = [row.locator for row in rows]
    if len(locators) != len(set(locators)):
        raise ValueError("layer evaluation replay receipt locators contain duplicates")
    return rows


def _payment_failures(
    value: object,
    where: str,
    *,
    replay_receipt: LayerReplayReceipt,
) -> tuple[dict[str, Any], ...]:
    rows = json_object_rows(value, where)
    parsed: list[dict[str, Any]] = []
    request_digests: set[str] = set()
    request_points: set[tuple[int, str]] = set()
    plan = replay_receipt.observation.plan
    points = {
        (point.frame, point.ref): point for point in replay_receipt.observation.points
    }
    for index, row in enumerate(rows):
        row_where = f"{where}[{index}]"
        if set(row) != {"request", "failure"}:
            raise ValueError(f"{row_where} must contain exactly request and failure")
        request = JudgmentObservationRequest.from_dict(
            row["request"],
            f"{row_where}.request",
        )
        failure = JudgmentPaymentAttemptFailure.from_dict(
            row["failure"],
            f"{row_where}.failure",
        )
        failure.assert_matches_request(request)
        point_identity = (request.judge_point.frame, request.judge_point.ref)
        point = points.get(point_identity)
        if (
            point is None
            or request.layer_replay_receipt_digest
            != replay_receipt.receipt_digest
            or request.replay_receipt_digest
            != replay_receipt.observation.replay_prefix.digest
            or request.parent_chain_digest
            != replay_parent_chain_digest(replay_receipt.observation.replay_prefix)
            or request.payment_generation_digest != plan.payment_generation_digest
            or request.definition_digest != plan.definition_digest
            or request.activation_digest != plan.activation_digest
            or request.reference_digest != point.ref_sha256
            or request.render_mode != plan.render_mode
            or float(request.render_scale) != float(plan.render_scale)
        ):
            raise ValueError(f"{row_where}.request does not match its replay group")
        if request.digest in request_digests:
            raise ValueError(f"{where} contains duplicate request digests")
        if point_identity in request_points:
            raise ValueError(f"{where} contains duplicate requests for one replay point")
        request_digests.add(request.digest)
        request_points.add(point_identity)
        parsed.append({"request": request.as_dict(), "failure": failure.as_dict()})
    return tuple(parsed)


def _judgment_observation(
    verdict: Mapping[str, Any],
    where: str,
    *,
    replay_receipt: LayerReplayReceipt,
    point,
) -> None:
    expected = {"request", "candidate_capture", "reused_attempt"}
    raw = verdict.get("judgment_observation")
    if not isinstance(raw, Mapping):
        raise ValueError(
            f"{where}.judgment_observation is required at f{point.frame}, which this "
            f"group's judgment debt owns, but the verdict carries "
            f"{'nothing' if raw is None else type(raw).__name__}"
        )
    if set(raw) != expected:
        raise ValueError(
            f"{where}.judgment_observation must carry exactly {sorted(expected)}; "
            f"it carries {sorted(raw)}"
        )
    request = JudgmentObservationRequest.from_dict(
        raw["request"],
        f"{where}.judgment_observation.request",
    )
    plan = replay_receipt.observation.plan
    if (
        request.layer_replay_receipt_digest != replay_receipt.receipt_digest
        or request.replay_receipt_digest
        != replay_receipt.observation.replay_prefix.digest
        or request.payment_generation_digest != plan.payment_generation_digest
        or (request.judge_point.frame, request.judge_point.ref)
        != (point.frame, point.ref)
        or request.render_mode != plan.render_mode
        or float(request.render_scale) != float(plan.render_scale)
        or request.definition_digest != plan.definition_digest
        or request.activation_digest != plan.activation_digest
        or request.reference_digest != point.ref_sha256
        or request.parent_chain_digest
        != replay_parent_chain_digest(replay_receipt.observation.replay_prefix)
    ):
        raise ValueError(f"{where}.judgment_observation.request is stale")
    candidate = raw["candidate_capture"]
    reused = raw["reused_attempt"]
    if (candidate is None) == (reused is None):
        raise ValueError(
            f"{where}.judgment_observation must bind one candidate or reused attempt"
        )
    if candidate is not None:
        if candidate != point.render_capture:
            raise ValueError(
                f"{where}.judgment_observation candidate changed after replay receipt"
            )
    else:
        failure = JudgmentPaymentAttemptFailure.from_dict(
            reused,
            f"{where}.judgment_observation.reused_attempt",
        )
        failure.assert_matches_request(request)


def _canonical_row(
    raw: object,
    where: str,
    *,
    replay_receipt: LayerReplayReceipt,
    point,
) -> dict[str, Any]:
    row = json_object(raw, where)
    if set(row) != _CANONICAL_FIELDS:
        raise ValueError(f"{where} has unsupported canonical row shape")
    if row["frame"] != point.frame or row["ref"] != point.ref:
        raise ValueError(f"{where} does not match its sealed replay point")
    verdict = json_object(row["verdict"], f"{where}.verdict")
    plan = replay_receipt.observation.plan
    if verdict.get("layer_replay_receipt_digest") != replay_receipt.receipt_digest:
        raise ValueError(f"{where}.verdict did not consume its replay receipt")
    if verdict.get("evidence_kind") != plan.evidence_kind:
        raise ValueError(f"{where}.verdict.evidence_kind changed after replay")
    if verdict.get("evidence") != list(point.evidence):
        raise ValueError(f"{where}.verdict.evidence changed after replay receipt")
    if verdict.get("missing_evidence") != list(point.missing_evidence_ids):
        raise ValueError(f"{where}.verdict.missing_evidence is not derived")
    failures = [
        row
        for row in point.evidence
        if row["authoritative"] is True and row["pass"] is False
    ]
    if verdict.get("evidence_failures") != failures:
        raise ValueError(f"{where}.verdict.evidence_failures is not derived")
    if verdict.get("auxiliary_captures", []) != list(
        replay_receipt.observation.auxiliary_captures
    ):
        raise ValueError(
            f"{where}.verdict auxiliary captures changed after replay receipt"
        )
    passed = verdict.get("pass")
    if not isinstance(passed, bool):
        raise ValueError(f"{where}.verdict.pass must be a boolean")
    if passed and point.deterministic_status != "passed":
        raise ValueError(
            f"{where}.verdict cannot pass failed or missing deterministic evidence"
        )
    decided_by = _text(verdict.get("decided_by"), f"{where}.verdict.decided_by")
    issues = verdict.get("issues")
    if not isinstance(issues, list) or any(not isinstance(item, str) for item in issues):
        raise ValueError(f"{where}.verdict.issues must be a list of strings")
    qualitative = any(
        claim.authority == "qualified_qualitative_required"
        for claim in plan.claims
    )
    if plan.evidence_kind == "render" and (
        verdict.get("render") != point.render
        or verdict.get("render_capture") != point.render_capture
    ):
        raise ValueError(f"{where}.verdict render changed after replay receipt")
    if not qualitative:
        if decided_by != "unit_executable_evidence":
            raise ValueError(
                f"{where}.verdict executable claim result must be mechanically decided"
            )
        if passed != (point.deterministic_status == "passed"):
            raise ValueError(
                f"{where}.verdict executable pass is not mechanically derived"
            )
        if plan.evidence_kind == "executable_only" and (
            "render" in verdict or "render_capture" in verdict
        ):
            raise ValueError(f"{where}.verdict executable evidence forbids render")
    else:
        mean = verdict.get("mean")
        scores = verdict.get("scores")
        if (
            isinstance(mean, bool)
            or not isinstance(mean, (int, float))
            or not math.isfinite(float(mean))
            or not isinstance(scores, Mapping)
            or any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                for value in scores.values()
            )
        ):
            raise ValueError(f"{where}.verdict qualitative scorecard is invalid")
        owes_observation = (point.frame, point.ref) in plan.debt_points
        if owes_observation:
            _judgment_observation(
                verdict,
                where,
                replay_receipt=replay_receipt,
                point=point,
            )
        elif verdict.get("judgment_observation") is not None:
            # The payment compiler produces an observation only at a point the debt
            # owns, so one here means the two sides disagree about what was paid.
            raise ValueError(
                f"{where}.judgment_observation was produced at f{point.frame}, which "
                f"this group's judgment debt does not own"
            )
    return {"frame": point.frame, "ref": point.ref, "verdict": verdict}


def _derived_result(canonical: tuple[dict[str, Any], ...]) -> str:
    verdicts = tuple(row["verdict"] for row in canonical)
    if all(verdict["pass"] for verdict in verdicts):
        return (
            "reproduced"
            if len(verdicts) == 1
            and verdicts[0].get("decided_by") == "pixel_reproduction"
            else "passed"
        )
    failures = tuple(verdict for verdict in verdicts if not verdict["pass"])
    if failures and all(
        verdict.get("contract_gap") and not verdict.get("issues")
        for verdict in failures
    ):
        return "contract_gap"
    if failures and all(verdict.get("judge_conflict") for verdict in failures):
        return "judge_conflict"
    return "failed"


def _derived_final_status(groups: tuple[dict[str, Any], ...]) -> str:
    for group in groups:
        result = group["result"]
        if result not in {"passed", "reproduced"}:
            return result if result in {"contract_gap", "judge_conflict"} else "failed"
    return "passed"


def _evaluation_payload(
    replay_bindings: tuple[LayerReplayReceiptBinding, ...],
    evaluation_groups: object,
    canonical: object,
) -> tuple[tuple[dict[str, Any], ...], tuple[dict[str, Any], ...], str]:
    groups = json_object_rows(evaluation_groups, "layer evaluation groups")
    raw_canonical = json_object_rows(canonical, "layer evaluation canonical")
    if len(groups) != len(replay_bindings):
        raise ValueError(
            "layer evaluation groups must exactly match executed replay receipts"
        )
    normalized_groups: list[dict[str, Any]] = []
    normalized_canonical: list[dict[str, Any]] = []
    cursor = 0
    for index, raw in enumerate(groups):
        where = f"layer evaluation groups[{index}]"
        if set(raw) != _GROUP_FIELDS:
            raise ValueError(f"{where} has unsupported shape")
        replay = replay_bindings[index].receipt
        plan = replay.observation.plan
        start = _index(raw["canonical_start"], f"{where}.canonical_start")
        end = _index(raw["canonical_end"], f"{where}.canonical_end")
        if start != cursor or end - start != len(replay.observation.points):
            raise ValueError(f"{where} does not partition sealed replay points")
        if end > len(raw_canonical):
            raise ValueError(f"{where}.canonical_end exceeds canonical evidence")
        if (
            raw["group_index"] != plan.group_index
            or raw["requirement_ids"] != list(plan.requirement_ids)
            or raw["debt_id"] != plan.debt_id
            or raw["definition_digest"] != plan.definition_digest
            or raw["activation_digest"] != plan.activation_digest
        ):
            raise ValueError(f"{where} does not match its replay receipt plan")
        payment_failures = _payment_failures(
            raw["payment_failures"],
            f"{where}.payment_failures",
            replay_receipt=replay,
        )
        if replay.observation.execution_status == "failed":
            if start != end or payment_failures:
                raise ValueError(
                    f"{where} failed replay forbids judgment rows and payment attempts"
                )
            group_canonical = ()
            derived = "failed"
        else:
            group_canonical = tuple(
                _canonical_row(
                    raw_canonical[position],
                    f"layer evaluation canonical[{position}]",
                    replay_receipt=replay,
                    point=replay.observation.points[position - start],
                )
                for position in range(start, end)
            )
            derived = _derived_result(group_canonical)
        if raw["result"] != derived:
            raise ValueError(
                f"{where}.result {raw['result']!r} does not match {derived!r}"
            )
        normalized_groups.append(
            {
                "group_index": plan.group_index,
                "result": derived,
                "requirement_ids": list(plan.requirement_ids),
                "debt_id": plan.debt_id,
                "definition_digest": plan.definition_digest,
                "activation_digest": plan.activation_digest,
                "canonical_start": start,
                "canonical_end": end,
                "payment_failures": list(payment_failures),
            }
        )
        normalized_canonical.extend(group_canonical)
        cursor = end
        if derived not in {"passed", "reproduced"} and index != len(groups) - 1:
            raise ValueError("layer evaluation continued after a terminal group result")
    if cursor != len(raw_canonical):
        raise ValueError("layer evaluation canonical rows are not fully partitioned")
    status = _derived_final_status(tuple(normalized_groups))
    planned_count = replay_bindings[0].receipt.observation.plan.planned_group_count
    if status == "passed" and len(replay_bindings) != planned_count:
        raise ValueError("passing layer evaluation must execute every planned group")
    return tuple(normalized_groups), tuple(normalized_canonical), status


@dataclass(frozen=True, slots=True)
class LayerEvaluationReceipt:
    """Exact sealed observations and mechanically derived terminal disposition."""

    receipt_digest: str
    claim: LayerFinalizationClaim
    replay_receipts: tuple[LayerReplayReceiptBinding, ...]
    evaluation_groups: tuple[dict[str, Any], ...]
    canonical: tuple[dict[str, Any], ...]
    final_status: str
    created_at: str

    @classmethod
    def mint(
        cls,
        *,
        replay_receipts: Iterable[LayerReplayReceiptBinding],
        evaluation_groups: object,
        canonical: object,
        created_at: object,
    ) -> LayerEvaluationReceipt:
        bindings = _replay_bindings(replay_receipts)
        groups, canonical_rows, status = _evaluation_payload(
            bindings,
            evaluation_groups,
            canonical,
        )
        at = _text(created_at, "layer evaluation created_at")
        claim = bindings[0].receipt.claim
        identity = {
            "schema": LAYER_EVALUATION_RECEIPT_SCHEMA,
            "claim": claim.as_dict(),
            "replay_receipts": [row.as_dict() for row in bindings],
            "evaluation_groups": list(groups),
            "canonical": list(canonical_rows),
            "final_status": status,
        }
        return cls(
            receipt_digest=semantic_digest(identity),
            claim=claim,
            replay_receipts=bindings,
            evaluation_groups=groups,
            canonical=canonical_rows,
            final_status=status,
            created_at=at,
        )

    @classmethod
    def parse(
        cls,
        value: object,
        where: str = "layer evaluation receipt",
    ) -> LayerEvaluationReceipt:
        if not isinstance(value, Mapping) or set(value) != _RECEIPT_FIELDS:
            raise ValueError(f"{where} has unsupported shape")
        if value["schema"] != LAYER_EVALUATION_RECEIPT_SCHEMA:
            raise ValueError(
                f"{where}.schema must be {LAYER_EVALUATION_RECEIPT_SCHEMA!r}"
            )
        raw_replays = value["replay_receipts"]
        if not isinstance(raw_replays, list):
            raise ValueError(f"{where}.replay_receipts must be a list")
        receipt = cls.mint(
            replay_receipts=tuple(
                LayerReplayReceiptBinding.from_dict(
                    row,
                    f"{where}.replay_receipts[{index}]",
                )
                for index, row in enumerate(raw_replays)
            ),
            evaluation_groups=value["evaluation_groups"],
            canonical=value["canonical"],
            created_at=value["created_at"],
        )
        if LayerFinalizationClaim.parse(value["claim"], f"{where}.claim") != receipt.claim:
            raise ValueError(f"{where}.claim does not match replay receipts")
        if value["final_status"] != receipt.final_status:
            raise ValueError(f"{where}.final_status is not mechanically derived")
        if require_digest(
            value["receipt_digest"],
            f"{where}.receipt_digest",
        ) != receipt.receipt_digest:
            raise ValueError(f"{where}.receipt_digest does not match its payload")
        return receipt

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": LAYER_EVALUATION_RECEIPT_SCHEMA,
            "receipt_digest": self.receipt_digest,
            "claim": self.claim.as_dict(),
            "replay_receipts": [row.as_dict() for row in self.replay_receipts],
            "evaluation_groups": list(self.evaluation_groups),
            "canonical": list(self.canonical),
            "final_status": self.final_status,
            "created_at": self.created_at,
        }


def canonical_layer_evaluation_receipt_bytes(receipt: LayerEvaluationReceipt) -> bytes:
    if not isinstance(receipt, LayerEvaluationReceipt):
        raise ValueError("receipt must be a typed layer evaluation receipt")
    parsed = LayerEvaluationReceipt.parse(
        receipt.as_dict(),
        "layer evaluation receipt serialization",
    )
    if parsed != receipt:
        raise ValueError(
            "layer evaluation receipt serialization is not its strict parsed representation"
        )
    return (
        json.dumps(
            parsed.as_dict(),
            allow_nan=False,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


__all__ = [
    "LAYER_EVALUATION_RECEIPT_SCHEMA",
    "LAYER_EVALUATION_RESULTS",
    "LayerEvaluationReceipt",
    "LayerReplayReceiptBinding",
    "canonical_layer_evaluation_receipt_bytes",
]
