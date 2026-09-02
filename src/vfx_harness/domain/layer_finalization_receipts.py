"""Immutable terminal receipt for one fully evaluated layer claim."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from vfx_harness.domain.layer_evaluation_receipts import (
    LayerEvaluationReceipt,
    canonical_layer_evaluation_receipt_bytes,
)
from vfx_harness.domain.layer_finalization_claims import (
    LayerFinalizationClaim,
    _relative_path,
    _text,
)
from vfx_harness.domain.layer_finalization_projection import finalization_projection
from vfx_harness.domain.layer_finalization_values import semantic_digest
from vfx_harness.domain.stop_envelope_primitives import canonical_digest, require_digest
from vfx_harness.domain.unit_outcomes import (
    hypothesis_falsification_render_settings_hash,
)

LAYER_FINALIZATION_RECEIPT_SCHEMA = "vfx-harness.layer-finalization-receipt/v2"
LAYER_FINAL_STATUSES = frozenset({"passed", "contract_gap", "judge_conflict", "failed"})

_FINALIZATION_RECEIPT_FIELDS = frozenset(
    {
        "schema",
        "receipt_digest",
        "claim",
        "evaluation_receipt_locator",
        "evaluation_receipt_sha256",
        "evaluation_receipt_digest",
        "evaluation_receipt",
        "final_status",
        "layer_script_path",
        "layer_script_sha256",
        "evaluation_groups",
        "canonical",
        "projection",
        "canonical_projection_digest",
        "completed_at",
    }
)
_OUTCOME_AUTHORITATIVE_FIELDS = (
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


def _authoritative_point_evidence(point) -> list[dict[str, Any]]:
    return [
        {key: row.get(key) for key in _OUTCOME_AUTHORITATIVE_FIELDS}
        for row in point.evidence
        if row.get("authoritative") is True
    ]


def _require_outcome_canonical_matches_evaluation(
    evaluation: LayerEvaluationReceipt,
    projection: Mapping[str, Any],
) -> None:
    outcome = projection["outcome"]
    observed = outcome["canonical"]
    expected: list[dict[str, Any]] = []
    canonical_index = 0
    for binding in evaluation.replay_receipts:
        replay = binding.receipt
        plan = replay.observation.plan
        for point in replay.observation.points:
            evaluated = evaluation.canonical[canonical_index]
            verdict = evaluated["verdict"]
            row: dict[str, Any] = {
                "evidence_kind": plan.evidence_kind,
                "frame": point.frame,
                "ref": point.ref,
                "ref_sha256": point.ref_sha256,
                "authoritative": _authoritative_point_evidence(point),
                "qualitative_defects": list(verdict["issues"]),
            }
            if plan.evidence_kind == "render":
                row.update(
                    render=point.render,
                    render_sha256=point.render_sha256,
                    render_capture=point.render_capture,
                )
            expected.append(row)
            canonical_index += 1
    if canonical_index != len(evaluation.canonical) or len(observed) != len(expected):
        raise ValueError(
            "layer finalization outcome canonical must exactly project every "
            "evaluated replay point"
        )
    for index, (actual, source) in enumerate(zip(observed, expected, strict=True)):
        represented = {
            key: actual.get(key)
            for key in source
        }
        if represented != source:
            raise ValueError(
                "layer finalization outcome canonical does not match its exact "
                f"evaluated replay point at index {index}"
            )


def _require_projection_matches_evaluation(
    evaluation: LayerEvaluationReceipt,
    projection: Mapping[str, Any],
) -> None:
    """Close the mutable debt projection over the groups that actually ran."""

    debt_groups = tuple(
        group
        for group, binding in zip(
            evaluation.evaluation_groups,
            evaluation.replay_receipts,
            strict=True,
        )
        if group["debt_id"] is not None
        and binding.receipt.observation.execution_status == "passed"
    )
    projected = projection["judgment_debts"]
    if len(projected) != len(debt_groups):
        raise ValueError(
            "layer finalization judgment projections must exactly match evaluated "
            "debt groups"
        )
    for index, (group, row) in enumerate(
        zip(debt_groups, projected, strict=True)
    ):
        decision = row["decision"]
        if (
            decision["id"] not in group["requirement_ids"]
            or decision["debt_id"] != group["debt_id"]
            or decision["definition_digest"] != group["definition_digest"]
            or decision["activation_digest"] != group["activation_digest"]
            or row["result"] != group["result"]
            or row["canonical_start"] != group["canonical_start"]
            or row["canonical_end"] != group["canonical_end"]
            or row["payment_failures"] != group["payment_failures"]
        ):
            raise ValueError(
                "layer finalization judgment projection does not match its exact "
                f"evaluated replay group at index {index}"
            )


def _finding_contract_ids(observations: list[dict[str, Any]]) -> list[str]:
    contract_ids: set[str] = set()
    for index, row in enumerate(observations):
        if not isinstance(row, Mapping):
            raise ValueError(
                "layer finalization finding observation "
                f"at index {index} must be an object"
            )
        check_ids = row.get("check_ids") or []
        if not isinstance(check_ids, list) or any(
            not isinstance(value, str) or not value.strip() for value in check_ids
        ):
            raise ValueError(
                "layer finalization finding observation check_ids must be a list "
                "of non-empty strings"
            )
        contract_ids.update(value.strip() for value in check_ids)
        observation = row.get("observation") or {}
        if not isinstance(observation, Mapping):
            raise ValueError(
                "layer finalization finding observation payload must be an object"
            )
        claim_id = observation.get("claim_id")
        if claim_id is not None:
            if not isinstance(claim_id, str) or not claim_id.strip():
                raise ValueError(
                    "layer finalization finding observation claim_id must be a "
                    "non-empty string"
                )
            contract_ids.add(claim_id.strip())
    return sorted(contract_ids)


def _require_finding_matches_evaluation(
    evaluation: LayerEvaluationReceipt,
    projection: Mapping[str, Any],
    *,
    completed_at: str,
) -> None:
    """Close a falsification finding over its one exact typed debt payment."""

    contract_gap_rows = [
        row
        for row in projection["judgment_debts"]
        if row["result"] == "contract_gap"
    ]
    finding = projection["finding"]
    if not contract_gap_rows:
        if finding is not None:
            raise ValueError(
                "layer finalization finding requires exactly one typed "
                "contract-gap evaluation group"
            )
        return
    if len(contract_gap_rows) != 1:
        raise ValueError(
            "layer finalization may bind a finding to exactly one typed "
            "contract-gap evaluation group"
        )
    if finding is None:
        raise ValueError(
            "layer finalization typed contract-gap evaluation requires its exact "
            "falsification finding"
        )

    debt_row = contract_gap_rows[0]
    decision = debt_row["decision"]
    expected_decisions = [
        {
            "id": decision["id"],
            "strength": decision["decision_strength"],
        }
    ]
    if finding["decisions"] != expected_decisions:
        raise ValueError(
            "layer finalization finding decisions do not match its exact typed "
            "contract-gap decision"
        )
    if finding["recorded_at"] != completed_at:
        raise ValueError(
            "layer finalization finding recorded_at must match terminal completion"
        )

    matching_groups = [
        (index, group)
        for index, group in enumerate(evaluation.evaluation_groups)
        if group["result"] == "contract_gap"
        and group["debt_id"] == decision["debt_id"]
        and group["definition_digest"] == decision["definition_digest"]
        and group["activation_digest"] == decision["activation_digest"]
        and group["canonical_start"] == debt_row["canonical_start"]
        and group["canonical_end"] == debt_row["canonical_end"]
    ]
    if len(matching_groups) != 1:
        raise ValueError(
            "layer finalization finding does not identify exactly one evaluated "
            "contract-gap group"
        )
    group_index, group = matching_groups[0]
    replay = evaluation.replay_receipts[group_index].receipt
    plan = replay.observation.plan
    identities = finding["identities"]
    point_matches: list[tuple[object, Mapping[str, Any]]] = []
    for offset, point in enumerate(replay.observation.points):
        if (
            point.render_sha256 is None
            or plan.render_mode is None
            or plan.render_scale is None
        ):
            continue
        settings_hash = hypothesis_falsification_render_settings_hash(
            mode=plan.render_mode,
            scale=plan.render_scale,
            frame=point.frame,
            reference=point.ref,
            reference_sha256=point.ref_sha256,
        )
        if (
            identities["candidate_hash"] == point.render_sha256
            and identities["settings_hash"] == settings_hash
        ):
            canonical_index = group["canonical_start"] + offset
            point_matches.append(
                (point, evaluation.canonical[canonical_index]["verdict"])
            )
    if len(point_matches) != 1:
        raise ValueError(
            "layer finalization finding candidate/settings do not identify exactly "
            "one sealed replay point in its contract-gap group"
        )

    _point, verdict = point_matches[0]
    expected_observations = verdict.get("contract_gaps")
    if (
        not isinstance(expected_observations, list)
        or not expected_observations
        or any(not isinstance(row, Mapping) for row in expected_observations)
    ):
        raise ValueError(
            "layer finalization contract-gap verdict must carry typed gap observations"
        )
    normalized_observations = [dict(row) for row in expected_observations]
    if finding["observations"] != normalized_observations:
        raise ValueError(
            "layer finalization finding observations do not match its exact "
            "contract-gap verdict"
        )
    if finding["contract_ids"] != _finding_contract_ids(normalized_observations):
        raise ValueError(
            "layer finalization finding contract_ids are not derived from its exact "
            "contract-gap observations"
        )


@dataclass(frozen=True, slots=True)
class LayerFinalizationReceipt:
    """Terminal authority derived from one immutable layer evaluation receipt."""

    receipt_digest: str
    claim: LayerFinalizationClaim
    evaluation_receipt_locator: str
    evaluation_receipt_sha256: str
    evaluation_receipt_digest: str
    evaluation_receipt: LayerEvaluationReceipt
    final_status: str
    layer_script_path: str
    layer_script_sha256: str
    evaluation_groups: tuple[dict[str, Any], ...]
    canonical: tuple[dict[str, Any], ...]
    projection: dict[str, Any]
    canonical_projection_digest: str
    completed_at: str

    @classmethod
    def mint(
        cls,
        *,
        evaluation_receipt: LayerEvaluationReceipt,
        evaluation_receipt_locator: object,
        evaluation_receipt_sha256: object,
        projection: object,
        completed_at: object,
    ) -> LayerFinalizationReceipt:
        if not isinstance(evaluation_receipt, LayerEvaluationReceipt):
            raise ValueError(
                "layer finalization receipt requires a typed evaluation receipt"
            )
        locator = _relative_path(
            evaluation_receipt_locator,
            "layer finalization receipt evaluation_receipt_locator",
        )
        file_sha256 = require_digest(
            evaluation_receipt_sha256,
            "layer finalization receipt evaluation_receipt_sha256",
        )
        expected_file_sha256 = hashlib.sha256(
            canonical_layer_evaluation_receipt_bytes(evaluation_receipt)
        ).hexdigest()
        if file_sha256 != expected_file_sha256:
            raise ValueError(
                "layer finalization evaluation receipt SHA-256 does not match "
                "embedded canonical bytes"
            )
        claim = evaluation_receipt.claim
        first_replay = evaluation_receipt.replay_receipts[0].receipt
        status = evaluation_receipt.final_status
        groups = evaluation_receipt.evaluation_groups
        canonical_rows = evaluation_receipt.canonical
        at = _text(completed_at, "layer finalization receipt completed_at")
        projection_row = finalization_projection(
            projection,
            "layer finalization receipt projection",
            claim=claim,
            final_status=status,
            layer_script_sha256=first_replay.layer_script_sha256,
            canonical=canonical_rows,
        )
        _require_projection_matches_evaluation(
            evaluation_receipt,
            projection_row,
        )
        _require_outcome_canonical_matches_evaluation(
            evaluation_receipt,
            projection_row,
        )
        _require_finding_matches_evaluation(
            evaluation_receipt,
            projection_row,
            completed_at=at,
        )
        projection_digest = canonical_digest(
            {
                "evaluation_receipt_digest": evaluation_receipt.receipt_digest,
                "evaluation_groups": list(groups),
                "canonical": list(canonical_rows),
                "projection": projection_row,
            }
        )
        identity = {
            "schema": LAYER_FINALIZATION_RECEIPT_SCHEMA,
            "claim": claim.as_dict(),
            "evaluation_receipt_locator": locator,
            "evaluation_receipt_sha256": file_sha256,
            "evaluation_receipt_digest": evaluation_receipt.receipt_digest,
            "evaluation_receipt": evaluation_receipt.as_dict(),
            "final_status": status,
            "layer_script_path": claim.layer_script_path,
            "layer_script_sha256": first_replay.layer_script_sha256,
            "evaluation_groups": list(groups),
            "canonical": list(canonical_rows),
            "projection": projection_row,
            "canonical_projection_digest": projection_digest,
        }
        return cls(
            receipt_digest=semantic_digest(identity),
            claim=claim,
            evaluation_receipt_locator=locator,
            evaluation_receipt_sha256=file_sha256,
            evaluation_receipt_digest=evaluation_receipt.receipt_digest,
            evaluation_receipt=evaluation_receipt,
            final_status=status,
            layer_script_path=claim.layer_script_path,
            layer_script_sha256=first_replay.layer_script_sha256,
            evaluation_groups=groups,
            canonical=canonical_rows,
            projection=projection_row,
            canonical_projection_digest=projection_digest,
            completed_at=at,
        )

    @classmethod
    def parse(
        cls,
        value: object,
        where: str = "layer finalization receipt",
    ) -> LayerFinalizationReceipt:
        if not isinstance(value, Mapping):
            raise ValueError(f"{where} must be an object")
        found = set(value)
        if found != _FINALIZATION_RECEIPT_FIELDS:
            raise ValueError(
                f"{where} fields mismatch; "
                f"missing={sorted(_FINALIZATION_RECEIPT_FIELDS - found)}; "
                f"unexpected={sorted(found - _FINALIZATION_RECEIPT_FIELDS)}"
            )
        if value.get("schema") != LAYER_FINALIZATION_RECEIPT_SCHEMA:
            raise ValueError(
                f"{where}.schema must be {LAYER_FINALIZATION_RECEIPT_SCHEMA!r}"
            )
        evaluation = LayerEvaluationReceipt.parse(
            value.get("evaluation_receipt"),
            f"{where}.evaluation_receipt",
        )
        receipt = cls.mint(
            evaluation_receipt=evaluation,
            evaluation_receipt_locator=value.get("evaluation_receipt_locator"),
            evaluation_receipt_sha256=value.get("evaluation_receipt_sha256"),
            projection=value.get("projection"),
            completed_at=value.get("completed_at"),
        )
        if LayerFinalizationClaim.parse(
            value.get("claim"),
            f"{where}.claim",
        ) != evaluation.claim:
            raise ValueError(f"{where}.claim does not match its evaluation receipt")
        exact = {
            "evaluation_receipt_digest": receipt.evaluation_receipt_digest,
            "final_status": receipt.final_status,
            "layer_script_path": receipt.layer_script_path,
            "layer_script_sha256": receipt.layer_script_sha256,
            "evaluation_groups": list(receipt.evaluation_groups),
            "canonical": list(receipt.canonical),
            "canonical_projection_digest": receipt.canonical_projection_digest,
        }
        if any(value[key] != expected for key, expected in exact.items()):
            raise ValueError(
                f"{where} duplicates do not exactly project its evaluation receipt"
            )
        if require_digest(
            value.get("receipt_digest"),
            f"{where}.receipt_digest",
        ) != receipt.receipt_digest:
            raise ValueError(f"{where}.receipt_digest does not match its exact payload")
        return receipt

    def assert_matches_evaluation(
        self,
        evaluation_receipt: LayerEvaluationReceipt,
    ) -> None:
        """Fail unless external evaluation bytes are this terminal's exact source."""

        if not isinstance(evaluation_receipt, LayerEvaluationReceipt):
            raise ValueError("evaluation_receipt must be a typed layer evaluation receipt")
        if (
            evaluation_receipt != self.evaluation_receipt
            or evaluation_receipt.receipt_digest != self.evaluation_receipt_digest
            or evaluation_receipt.claim != self.claim
        ):
            raise ValueError(
                "layer finalization receipt does not match its evaluation receipt"
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": LAYER_FINALIZATION_RECEIPT_SCHEMA,
            "receipt_digest": self.receipt_digest,
            "claim": self.claim.as_dict(),
            "evaluation_receipt_locator": self.evaluation_receipt_locator,
            "evaluation_receipt_sha256": self.evaluation_receipt_sha256,
            "evaluation_receipt_digest": self.evaluation_receipt_digest,
            "evaluation_receipt": self.evaluation_receipt.as_dict(),
            "final_status": self.final_status,
            "layer_script_path": self.layer_script_path,
            "layer_script_sha256": self.layer_script_sha256,
            "evaluation_groups": list(self.evaluation_groups),
            "canonical": list(self.canonical),
            "projection": dict(self.projection),
            "canonical_projection_digest": self.canonical_projection_digest,
            "completed_at": self.completed_at,
        }


def canonical_layer_finalization_receipt_bytes(
    receipt: LayerFinalizationReceipt,
) -> bytes:
    """Return the only accepted durable byte representation of a terminal receipt."""

    if not isinstance(receipt, LayerFinalizationReceipt):
        raise ValueError("receipt must be a typed layer finalization receipt")
    parsed = LayerFinalizationReceipt.parse(
        receipt.as_dict(),
        "layer finalization receipt serialization",
    )
    if parsed != receipt:
        raise ValueError(
            "layer finalization receipt serialization is not its strict parsed representation"
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
    "LAYER_FINALIZATION_RECEIPT_SCHEMA",
    "LAYER_FINAL_STATUSES",
    "LayerFinalizationReceipt",
    "canonical_layer_finalization_receipt_bytes",
]
