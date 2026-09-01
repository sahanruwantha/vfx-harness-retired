"""Strict terminal-projection validation for layer-finalization receipts."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from typing import Any

from vfx_harness.domain.judgment_debt_models import (
    JUDGMENT_DEBT_CLAIM_KINDS,
    JUDGMENT_DEBT_LIFECYCLES,
    JUDGMENT_DEBT_PROPERTIES,
    OBSERVATION_MEDIA,
    PROVISIONAL_STRENGTHS,
    RENDERED_CARRIER_FAMILIES,
)
from vfx_harness.domain.judgment_debt_observation import (
    JudgmentObservationRequest,
    JudgmentPaymentAttemptFailure,
)
from vfx_harness.domain.layer_finalization_claims import (
    LayerFinalizationClaim,
    _relative_path,
    _text,
)
from vfx_harness.domain.layer_finalization_values import json_object
from vfx_harness.domain.layer_outcome_projections import LayerOutcomeProjection
from vfx_harness.domain.stop_envelope_primitives import canonical_digest, require_digest
from vfx_harness.domain.unit_outcomes import (
    HYPOTHESIS_FALSIFICATION_SCHEMA,
    HypothesisFalsification,
)

LAYER_FINALIZATION_PROJECTION_SCHEMA = "vfx-harness.layer-finalization-projection/v3"

_FINALIZATION_PROJECTION_FIELDS = frozenset(
    {
        "schema",
        "best",
        "blender_version",
        "ablation",
        "revalidation",
        "judgment_debts",
        "finding",
        "outcome",
        "ledger",
    }
)
_BEST_PROJECTION_FIELDS = frozenset({"round", "mean", "render"})
_ABLATION_PROJECTION_FIELDS = frozenset({"ok", "note"})
_ABLATION_MEASUREMENT_FIELDS = frozenset({"ok", "note", "frames", "moved"})
_REVALIDATION_PROJECTION_SCHEMA = "vfx-harness.layer-image-check-revalidation/v1"
_REVALIDATION_PROJECTION_FIELDS = frozenset(
    {
        "schema",
        "layer_id",
        "source_sha256",
        "replacement_sha256",
        "replacement_text",
        "result",
    }
)
_REVALIDATION_RESULT_FIELDS = frozenset({"kept", "dropped"})
_REVALIDATION_DROP_FIELDS = frozenset({"id", "reason"})
_JUDGMENT_DEBT_PROJECTION_FIELDS = frozenset(
    {
        "decision",
        "result",
        "canonical_start",
        "canonical_end",
        "payment_failures",
        "resolution",
    }
)
_JUDGMENT_DECISION_FIELDS = frozenset(
    {
        "id",
        "debt_id",
        "definition_digest",
        "activation_digest",
        "statement",
        "decision_strength",
        "evidence_domains",
        "claim_kind",
        "property",
        "fault_owner",
        "subject_roles",
        "axes",
        "judge_points",
        "carrier_families",
        "observation_medium",
        "lifecycle",
        "state",
    }
)
_PAYMENT_FAILURE_PROJECTION_FIELDS = frozenset({"request", "failure"})
_JUDGMENT_RESOLUTION_FIELDS = frozenset({"outcome", "evidence_digest"})
_JUDGMENT_RESULTS = frozenset({"passed", "reproduced", "contract_gap", "judge_conflict", "failed"})
_FINDING_FIELDS = frozenset(
    {
        "schema",
        "record_id",
        "recorded_at",
        "layer",
        "unit",
        "identities",
        "contract_ids",
        "observations",
        "decisions",
        "conflict",
        "evidence",
        "affected",
        "fault_owner_units",
    }
)
_FINDING_IDENTITY_FIELDS = frozenset(
    {
        "bundle_hash",
        "plan_hash",
        "unit_hash",
        "unit_plan_hash",
        "candidate_hash",
        "settings_hash",
    }
)
_FINDING_DECISION_FIELDS = frozenset({"id", "strength"})
_FINDING_CONFLICT_FIELDS = frozenset({"kind", "required_authority", "roles", "controls"})
_LEDGER_PROJECTION_FIELDS = frozenset({"status", "script", "script_sha256"})


def _closed_object(
    value: object,
    where: str,
    fields: frozenset[str],
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{where} must be an object")
    found = set(value)
    if found != fields:
        raise ValueError(
            f"{where} fields mismatch; missing={sorted(fields - found)}; unexpected={sorted(found - fields)}"
        )
    return value


def _finite_number(value: object, where: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{where} must be a finite number")
    if not math.isfinite(value):
        raise ValueError(f"{where} must be a finite number")
    return value


def _non_negative_integer(value: object, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{where} must be a non-negative integer")
    return value


def _positive_integer(value: object, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{where} must be a positive integer")
    return value


def _text_list(
    value: object,
    where: str,
    *,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    if not isinstance(value, list) or (not allow_empty and not value):
        qualifier = "" if allow_empty else " non-empty"
        raise ValueError(f"{where} must be a{qualifier} list of strings")
    rows = tuple(_text(item, f"{where}[{index}]") for index, item in enumerate(value))
    if len(rows) != len(set(rows)):
        raise ValueError(f"{where} contains duplicates")
    return rows


def _validate_best_projection(value: object, where: str) -> None:
    row = _closed_object(value, where, _BEST_PROJECTION_FIELDS)
    _non_negative_integer(row["round"], f"{where}.round")
    _finite_number(row["mean"], f"{where}.mean")
    render = row["render"]
    if render is not None:
        _relative_path(render, f"{where}.render")


def _validate_ablation_projection(value: object, where: str) -> None:
    if not isinstance(value, Mapping):
        raise ValueError(f"{where} must be an object")
    fields = set(value)
    if fields not in {
        _ABLATION_PROJECTION_FIELDS,
        _ABLATION_MEASUREMENT_FIELDS,
    }:
        allowed = [
            sorted(_ABLATION_PROJECTION_FIELDS),
            sorted(_ABLATION_MEASUREMENT_FIELDS),
        ]
        raise ValueError(f"{where} fields must be exactly one of {allowed}")
    if not isinstance(value["ok"], bool):
        raise ValueError(f"{where}.ok must be a boolean")
    if not isinstance(value["note"], str):
        raise ValueError(f"{where}.note must be a string")
    if fields == _ABLATION_PROJECTION_FIELDS:
        return
    frames = value["frames"]
    if not isinstance(frames, list) or not frames:
        raise ValueError(f"{where}.frames must be a non-empty list")
    normalized_frames = tuple(
        _positive_integer(frame, f"{where}.frames[{index}]") for index, frame in enumerate(frames)
    )
    if len(normalized_frames) != len(set(normalized_frames)):
        raise ValueError(f"{where}.frames contains duplicates")
    moved = value["moved"]
    if not isinstance(moved, Mapping):
        raise ValueError(f"{where}.moved must be an object")
    for key, change in moved.items():
        _text(key, f"{where}.moved key")
        _finite_number(change, f"{where}.moved[{key!r}]")


def _validate_revalidation_projection(
    value: object,
    where: str,
    *,
    layer_id: str,
) -> None:
    row = _closed_object(value, where, _REVALIDATION_PROJECTION_FIELDS)
    if row["schema"] != _REVALIDATION_PROJECTION_SCHEMA:
        raise ValueError(f"{where}.schema must be {_REVALIDATION_PROJECTION_SCHEMA!r}")
    if row["layer_id"] != layer_id:
        raise ValueError(f"{where}.layer_id must match the finalization claim")
    source_sha256 = row["source_sha256"]
    if source_sha256 is not None:
        require_digest(source_sha256, f"{where}.source_sha256")
    replacement_sha256 = row["replacement_sha256"]
    replacement_text = row["replacement_text"]
    if (replacement_sha256 is None) != (replacement_text is None):
        raise ValueError(f"{where}.replacement_sha256 and replacement_text must both be null or both be present")
    if replacement_text is not None:
        if not isinstance(replacement_text, str):
            raise ValueError(f"{where}.replacement_text must be a string or null")
        expected = hashlib.sha256(replacement_text.encode("utf-8")).hexdigest()
        if (
            require_digest(
                replacement_sha256,
                f"{where}.replacement_sha256",
            )
            != expected
        ):
            raise ValueError(f"{where}.replacement_sha256 does not match exact text")
    result = _closed_object(
        row["result"],
        f"{where}.result",
        _REVALIDATION_RESULT_FIELDS,
    )
    _non_negative_integer(result["kept"], f"{where}.result.kept")
    dropped = result["dropped"]
    if not isinstance(dropped, list):
        raise ValueError(f"{where}.result.dropped must be a list")
    identifiers: list[str] = []
    for index, value in enumerate(dropped):
        item_where = f"{where}.result.dropped[{index}]"
        item = _closed_object(value, item_where, _REVALIDATION_DROP_FIELDS)
        identifiers.append(_text(item["id"], f"{item_where}.id"))
        _text(item["reason"], f"{item_where}.reason")
    if len(identifiers) != len(set(identifiers)):
        raise ValueError(f"{where}.result.dropped contains duplicate ids")


def _validate_judgment_decision(value: object, where: str) -> Mapping[str, Any]:
    row = _closed_object(value, where, _JUDGMENT_DECISION_FIELDS)
    _text(row["id"], f"{where}.id")
    _text(row["debt_id"], f"{where}.debt_id")
    require_digest(row["definition_digest"], f"{where}.definition_digest")
    require_digest(row["activation_digest"], f"{where}.activation_digest")
    _text(row["statement"], f"{where}.statement")
    if row["decision_strength"] not in PROVISIONAL_STRENGTHS:
        raise ValueError(f"{where}.decision_strength must be one of {sorted(PROVISIONAL_STRENGTHS)}")
    domains = _text_list(row["evidence_domains"], f"{where}.evidence_domains")
    if domains != ("image",):
        raise ValueError(f"{where}.evidence_domains must be exactly ['image']")
    if row["claim_kind"] not in JUDGMENT_DEBT_CLAIM_KINDS:
        raise ValueError(f"{where}.claim_kind must be one of {sorted(JUDGMENT_DEBT_CLAIM_KINDS)}")
    if row["property"] not in JUDGMENT_DEBT_PROPERTIES:
        raise ValueError(f"{where}.property must be one of {sorted(JUDGMENT_DEBT_PROPERTIES)}")
    _text(row["fault_owner"], f"{where}.fault_owner")
    _text_list(row["subject_roles"], f"{where}.subject_roles")
    _text_list(row["axes"], f"{where}.axes")
    points = row["judge_points"]
    if not isinstance(points, list) or not points:
        raise ValueError(f"{where}.judge_points must be a non-empty list")
    normalized_points: list[tuple[int, str]] = []
    for index, point in enumerate(points):
        point_where = f"{where}.judge_points[{index}]"
        if not isinstance(point, list) or len(point) != 2:
            raise ValueError(f"{point_where} must be [frame, ref]")
        normalized_points.append(
            (
                _positive_integer(point[0], f"{point_where}[0]"),
                _text(point[1], f"{point_where}[1]"),
            )
        )
    if len(normalized_points) != len(set(normalized_points)):
        raise ValueError(f"{where}.judge_points contains duplicates")
    families = _text_list(row["carrier_families"], f"{where}.carrier_families")
    if set(families) - RENDERED_CARRIER_FAMILIES:
        raise ValueError(f"{where}.carrier_families contains unsupported families")
    if row["observation_medium"] not in OBSERVATION_MEDIA:
        raise ValueError(f"{where}.observation_medium must be one of {sorted(OBSERVATION_MEDIA)}")
    if row["lifecycle"] not in JUDGMENT_DEBT_LIFECYCLES:
        raise ValueError(f"{where}.lifecycle must be one of {sorted(JUDGMENT_DEBT_LIFECYCLES)}")
    if row["state"] not in {"pending_not_due", "due"}:
        raise ValueError(f"{where}.state must be pending_not_due or due")
    return row


def _validate_finding_projection(
    value: object,
    where: str,
    *,
    claim: LayerFinalizationClaim,
) -> Mapping[str, Any]:
    row = _closed_object(value, where, _FINDING_FIELDS)
    if row["schema"] != HYPOTHESIS_FALSIFICATION_SCHEMA:
        raise ValueError(f"{where}.schema must be {HYPOTHESIS_FALSIFICATION_SCHEMA!r}")
    _closed_object(
        row["identities"],
        f"{where}.identities",
        _FINDING_IDENTITY_FIELDS,
    )
    decisions = row["decisions"]
    if not isinstance(decisions, list):
        raise ValueError(f"{where}.decisions must be a list")
    for index, decision in enumerate(decisions):
        _closed_object(
            decision,
            f"{where}.decisions[{index}]",
            _FINDING_DECISION_FIELDS,
        )
    _closed_object(
        row["conflict"],
        f"{where}.conflict",
        _FINDING_CONFLICT_FIELDS,
    )
    parsed = HypothesisFalsification.parse(dict(row), where)
    if parsed.layer != claim.layer_id:
        raise ValueError(f"{where}.layer must match the finalization claim")
    source_inputs = {unit_input.unit_id: unit_input for unit_input in claim.unit_inputs}
    source = source_inputs.get(parsed.unit)
    if source is None:
        raise ValueError(f"{where}.unit is not a unit input of the finalization claim")
    if parsed.plan_hash != claim.plan_hash:
        raise ValueError(f"{where}.identities.plan_hash must match the finalization claim")
    if parsed.unit_hash != source.unit_digest:
        raise ValueError(f"{where}.identities.unit_hash must match its claimed source unit")
    identity = dict(row)
    identity.pop("record_id")
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    if parsed.record_id != f"hf-{digest[:20]}":
        raise ValueError(f"{where}.record_id does not match its exact payload")
    return row


def _canonical_verdict_slice(
    canonical: tuple[dict[str, Any], ...],
    start: int,
    end: int,
    where: str,
) -> list[tuple[tuple[int, str], dict[str, Any]]]:
    if start >= end or end > len(canonical):
        raise ValueError(f"{where} must name a non-empty range within {len(canonical)} canonical rows")
    verdicts: list[tuple[tuple[int, str], dict[str, Any]]] = []
    for index, raw in enumerate(canonical[start:end], start):
        row_where = f"layer finalization receipt canonical[{index}]"
        if not isinstance(raw, Mapping):
            raise ValueError(f"{row_where} must be an object")
        frame = _positive_integer(raw.get("frame"), f"{row_where}.frame")
        ref = _text(raw.get("ref"), f"{row_where}.ref")
        verdict = raw.get("verdict")
        if not isinstance(verdict, Mapping):
            raise ValueError(f"{row_where}.verdict must be an object")
        verdicts.append(((frame, ref), dict(verdict)))
    return verdicts


def _validate_judgment_debt_projections(
    value: object,
    where: str,
    *,
    canonical: tuple[dict[str, Any], ...],
    finding: Mapping[str, Any] | None,
) -> None:
    if not isinstance(value, list):
        raise ValueError(f"{where} must be a list")
    debt_ids: list[str] = []
    definition_digests: list[str] = []
    previous_end = 0
    for index, raw_debt in enumerate(value):
        row_where = f"{where}[{index}]"
        row = _closed_object(
            raw_debt,
            row_where,
            _JUDGMENT_DEBT_PROJECTION_FIELDS,
        )
        decision = _validate_judgment_decision(
            row["decision"],
            f"{row_where}.decision",
        )
        debt_ids.append(str(decision["debt_id"]))
        definition_digests.append(str(decision["definition_digest"]))
        result = row["result"]
        if result not in _JUDGMENT_RESULTS:
            raise ValueError(f"{row_where}.result must be one of {sorted(_JUDGMENT_RESULTS)}")
        start = _non_negative_integer(
            row["canonical_start"],
            f"{row_where}.canonical_start",
        )
        end = _non_negative_integer(
            row["canonical_end"],
            f"{row_where}.canonical_end",
        )
        if start < previous_end:
            raise ValueError(f"{where} canonical ranges must not overlap or reorder")
        verdicts = _canonical_verdict_slice(
            canonical,
            start,
            end,
            f"{row_where} canonical range",
        )
        previous_end = end
        payment_failures = row["payment_failures"]
        if not isinstance(payment_failures, list):
            raise ValueError(f"{row_where}.payment_failures must be a list")
        request_digests: list[str] = []
        for failure_index, raw_failure in enumerate(payment_failures):
            failure_where = f"{row_where}.payment_failures[{failure_index}]"
            failure_row = _closed_object(
                raw_failure,
                failure_where,
                _PAYMENT_FAILURE_PROJECTION_FIELDS,
            )
            request = JudgmentObservationRequest.from_dict(
                failure_row["request"],
                f"{failure_where}.request",
            )
            failure = JudgmentPaymentAttemptFailure.from_dict(
                failure_row["failure"],
                f"{failure_where}.failure",
            )
            failure.assert_matches_request(request)
            if (
                request.definition_digest != decision["definition_digest"]
                or request.activation_digest != decision["activation_digest"]
            ):
                raise ValueError(f"{failure_where}.request does not match its judgment decision")
            request_digests.append(request.digest)
        if len(request_digests) != len(set(request_digests)):
            raise ValueError(f"{row_where}.payment_failures contains duplicates")
        resolution = row["resolution"]
        expected_outcome = (
            "falsified" if result == "contract_gap" else "satisfied" if result in {"passed", "reproduced"} else None
        )
        if expected_outcome is None:
            if resolution is not None:
                raise ValueError(f"{row_where}.resolution must be null for result {result!r}")
            continue
        resolution_row = _closed_object(
            resolution,
            f"{row_where}.resolution",
            _JUDGMENT_RESOLUTION_FIELDS,
        )
        if resolution_row["outcome"] != expected_outcome:
            raise ValueError(f"{row_where}.resolution.outcome must be {expected_outcome!r}")
        if result == "contract_gap" and finding is None:
            raise ValueError(f"{row_where}.resolution requires the exact falsification finding")
        finding_record_id = None if finding is None else finding["record_id"]
        expected_evidence_digest = canonical_digest(
            {
                "schema": "vfx-harness.judgment-debt-payment-evidence/v1",
                "debt_id": decision["debt_id"],
                "definition_digest": decision["definition_digest"],
                "activation_digest": decision["activation_digest"],
                "result": result,
                "verdicts": verdicts,
                "finding_record_id": finding_record_id,
            }
        )
        observed_evidence_digest = require_digest(
            resolution_row["evidence_digest"],
            f"{row_where}.resolution.evidence_digest",
        )
        if observed_evidence_digest != expected_evidence_digest:
            raise ValueError(f"{row_where}.resolution.evidence_digest does not match its exact canonical judgment")
    if len(debt_ids) != len(set(debt_ids)):
        raise ValueError(f"{where} contains duplicate debt ids")
    if len(definition_digests) != len(set(definition_digests)):
        raise ValueError(f"{where} contains duplicate definition digests")


def finalization_projection(
    value: object,
    where: str,
    *,
    claim: LayerFinalizationClaim,
    final_status: str,
    layer_script_sha256: str,
    canonical: tuple[dict[str, Any], ...],
) -> dict[str, Any]:
    """Validate and detach the exact mutable projections sealed by a receipt."""

    row = json_object(value, where)
    _closed_object(row, where, _FINALIZATION_PROJECTION_FIELDS)
    if row["schema"] != LAYER_FINALIZATION_PROJECTION_SCHEMA:
        raise ValueError(f"{where}.schema must be {LAYER_FINALIZATION_PROJECTION_SCHEMA!r}")
    _validate_best_projection(row["best"], f"{where}.best")
    blender_version = _text(
        row["blender_version"],
        f"{where}.blender_version",
    )
    _validate_ablation_projection(row["ablation"], f"{where}.ablation")
    _validate_revalidation_projection(
        row["revalidation"],
        f"{where}.revalidation",
        layer_id=claim.layer_id,
    )
    finding = (
        None
        if row["finding"] is None
        else _validate_finding_projection(
            row["finding"],
            f"{where}.finding",
            claim=claim,
        )
    )
    _validate_judgment_debt_projections(
        row["judgment_debts"],
        f"{where}.judgment_debts",
        canonical=canonical,
        finding=finding,
    )
    outcome = LayerOutcomeProjection.parse(
        row["outcome"],
        claim=claim,
        layer_script_path=claim.layer_script_path,
        final_status=final_status,
        best=row["best"],
        receipt_canonical=canonical,
        blender_version=blender_version,
        where=f"{where}.outcome",
    )
    row["outcome"] = outcome.as_dict()
    ledger = _closed_object(
        row["ledger"],
        f"{where}.ledger",
        _LEDGER_PROJECTION_FIELDS,
    )
    if ledger["status"] != final_status:
        raise ValueError(f"{where}.ledger.status must match final_status")
    if ledger["script"] != claim.layer_script_path:
        raise ValueError(f"{where}.ledger.script must match the finalization claim")
    if (
        require_digest(
            ledger["script_sha256"],
            f"{where}.ledger.script_sha256",
        )
        != layer_script_sha256
    ):
        raise ValueError(f"{where}.ledger.script_sha256 must match the finalization receipt")
    return row


__all__ = ["LAYER_FINALIZATION_PROJECTION_SCHEMA", "finalization_projection"]
