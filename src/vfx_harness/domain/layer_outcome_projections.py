"""Receipt-bound proposed projection for one sealed layer outcome.

The terminal finalization receipt is committed before mutable outcome bytes.  It
therefore carries this closed projection of every non-audit outcome field so a
pure reader can reject a record whose duplicated values drift while its embedded
receipt remains unchanged.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from vfx_harness.domain.layer_finalization_claims import (
    LayerFinalizationClaim,
    _text,
)
from vfx_harness.domain.layer_finalization_values import json_object
from vfx_harness.domain.stop_envelope_primitives import canonical_digest, require_digest

OUTCOME_SCHEMA = 3
LAYER_OUTCOME_PROJECTION_SCHEMA = "vfx-harness.layer-outcome-projection/v1"
CANONICAL_EVIDENCE_KINDS = frozenset({"render", "executable_only"})

# ``schema`` and ``at`` are validated independently by the outcome envelope.
# ``finalization_receipt`` is the source authority rather than its own projection.
OUTCOME_RECEIPT_BOUND_FIELDS = (
    "layer",
    "title",
    "script",
    "status",
    "run_id",
    "attempt",
    "best",
    "decided_by",
    "authoritative_total",
    "authoritative_passed",
    "failed_contracts",
    "interfaces",
    "revalidation_manifest",
    "canonical",
)
_PROJECTION_FIELDS = frozenset({"schema", *OUTCOME_RECEIPT_BOUND_FIELDS})
_INTERFACE_FIELDS = (
    "id",
    "metric",
    "value",
    "target",
    "pass",
    "owner_layer",
    "fault_owner",
    "activates_at",
    "lifecycle",
)
_AUTHORITATIVE_FIELDS = (
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
_CANONICAL_COMMON_FIELDS = frozenset(
    {
        "evidence_kind",
        "frame",
        "ref",
        "ref_sha256",
        "input_manifest_sha256",
        "authoritative",
        "authoritative_sha256",
        "qualitative_defects",
    }
)
_CANONICAL_RENDER_FIELDS = frozenset(
    {"render", "render_sha256", "render_capture"}
)


def _non_negative_integer(value: object, where: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{where} must be a non-negative integer")
    return value


def _object_rows(value: object, where: str) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list):
        raise ValueError(f"{where} must be a list of objects")
    return tuple(
        json_object(row, f"{where}[{index}]")
        for index, row in enumerate(value)
    )


def _source_summary(
    canonical: tuple[dict[str, Any], ...],
    *,
    layer_id: str,
    where: str,
) -> tuple[object, tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]]:
    decisions: list[str] = []
    authoritative: list[dict[str, Any]] = []
    for index, raw in enumerate(canonical):
        row_where = f"{where}[{index}]"
        if not isinstance(raw, Mapping):
            raise ValueError(f"{row_where} must be an object")
        verdict = raw.get("verdict")
        if not isinstance(verdict, Mapping):
            raise ValueError(f"{row_where}.verdict must be an object")
        decision = verdict.get("decided_by", "critic")
        decisions.append(_text(decision, f"{row_where}.verdict.decided_by"))
        evidence = verdict.get("evidence") or []
        if not isinstance(evidence, list):
            raise ValueError(f"{row_where}.verdict.evidence must be a list")
        for evidence_index, item in enumerate(evidence):
            item_where = f"{row_where}.verdict.evidence[{evidence_index}]"
            if not isinstance(item, Mapping):
                raise ValueError(f"{item_where} must be an object")
            marker = item.get("authoritative")
            if not isinstance(marker, bool):
                raise ValueError(f"{item_where}.authoritative must be a boolean")
            if not marker:
                continue
            passed = item.get("pass")
            if not isinstance(passed, bool):
                raise ValueError(f"{item_where}.pass must be a boolean")
            identifier = _text(item.get("id"), f"{item_where}.id")
            projected = {key: item.get(key) for key in _AUTHORITATIVE_FIELDS}
            projected["id"] = identifier
            projected["pass"] = passed
            authoritative.append(projected)
    decided_by: object = (
        decisions[0]
        if decisions and len(set(decisions)) == 1
        else list(decisions)
    )
    interfaces = tuple(
        {key: item.get(key) for key in _INTERFACE_FIELDS}
        for item in authoritative
        if item.get("source") == "interface_contract"
        and item.get("owner_layer") == layer_id
    )
    return decided_by, tuple(authoritative), interfaces


def _validate_canonical_projection(
    rows: tuple[dict[str, Any], ...],
    *,
    authoritative: tuple[dict[str, Any], ...],
    revalidation_manifest: Mapping[str, Any],
    where: str,
) -> None:
    manifest_digest = canonical_digest(dict(revalidation_manifest))
    projected_authoritative: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        row_where = f"{where}[{index}]"
        kind = row.get("evidence_kind")
        if kind not in CANONICAL_EVIDENCE_KINDS:
            raise ValueError(
                f"{row_where}.evidence_kind must be one of "
                f"{sorted(CANONICAL_EVIDENCE_KINDS)}"
            )
        expected_fields = (
            _CANONICAL_COMMON_FIELDS | _CANONICAL_RENDER_FIELDS
            if kind == "render"
            else _CANONICAL_COMMON_FIELDS
        )
        if set(row) != expected_fields:
            raise ValueError(
                f"{row_where} fields mismatch; "
                f"missing={sorted(expected_fields - set(row))}; "
                f"unexpected={sorted(set(row) - expected_fields)}"
            )
        frame = row.get("frame")
        if not isinstance(frame, int) or isinstance(frame, bool):
            raise ValueError(f"{row_where}.frame must be an integer")
        _text(row.get("ref"), f"{row_where}.ref")
        require_digest(row.get("ref_sha256"), f"{row_where}.ref_sha256")
        if require_digest(
            row.get("input_manifest_sha256"),
            f"{row_where}.input_manifest_sha256",
        ) != manifest_digest:
            raise ValueError(
                f"{row_where}.input_manifest_sha256 must match the exact "
                "revalidation manifest"
            )
        row_authoritative = _object_rows(
            row.get("authoritative"),
            f"{row_where}.authoritative",
        )
        expected_authoritative_sha256 = canonical_digest(
            {"authoritative": list(row_authoritative)}
        )
        if require_digest(
            row.get("authoritative_sha256"),
            f"{row_where}.authoritative_sha256",
        ) != expected_authoritative_sha256:
            raise ValueError(
                f"{row_where}.authoritative_sha256 does not match its exact evidence"
            )
        defects = row.get("qualitative_defects")
        if not isinstance(defects, list) or any(
            not isinstance(item, str) for item in defects
        ):
            raise ValueError(f"{row_where}.qualitative_defects must be strings")
        if kind == "render":
            _text(row.get("render"), f"{row_where}.render")
            require_digest(row.get("render_sha256"), f"{row_where}.render_sha256")
            json_object(row.get("render_capture"), f"{row_where}.render_capture")
        projected_authoritative.extend(row_authoritative)
    if tuple(projected_authoritative) != authoritative:
        raise ValueError(
            f"{where} authoritative evidence does not exactly project the "
            "terminal canonical verdicts"
        )


@dataclass(frozen=True, slots=True)
class LayerOutcomeProjection:
    """Exact non-audit outcome record sealed by a terminal receipt."""

    record: dict[str, Any]

    @classmethod
    def from_canonical(
        cls,
        *,
        claim: LayerFinalizationClaim,
        layer_title: object,
        layer_script_path: object,
        final_status: object,
        best: object,
        revalidation_manifest: object,
        canonical: object,
        receipt_canonical: tuple[dict[str, Any], ...],
        blender_version: str,
    ) -> LayerOutcomeProjection:
        """Derive every summary field from the exact terminal canonical rows."""

        decided_by, authoritative, interfaces = _source_summary(
            receipt_canonical,
            layer_id=claim.layer_id,
            where="layer finalization receipt canonical",
        )
        return cls.mint(
            claim=claim,
            layer_title=layer_title,
            layer_script_path=layer_script_path,
            final_status=final_status,
            best=best,
            decided_by=decided_by,
            authoritative_total=len(authoritative),
            authoritative_passed=sum(
                item["pass"] is True for item in authoritative
            ),
            failed_contracts=[
                str(item["id"])
                for item in authoritative
                if item["pass"] is False
            ],
            interfaces=list(interfaces),
            revalidation_manifest=revalidation_manifest,
            canonical=canonical,
            receipt_canonical=receipt_canonical,
            blender_version=blender_version,
        )

    @classmethod
    def mint(
        cls,
        *,
        claim: LayerFinalizationClaim,
        layer_title: object,
        layer_script_path: object,
        final_status: object,
        best: object,
        decided_by: object,
        authoritative_total: object,
        authoritative_passed: object,
        failed_contracts: object,
        interfaces: object,
        revalidation_manifest: object,
        canonical: object,
        receipt_canonical: tuple[dict[str, Any], ...],
        blender_version: str,
    ) -> LayerOutcomeProjection:
        return cls.parse(
            {
                "schema": LAYER_OUTCOME_PROJECTION_SCHEMA,
                "layer": claim.layer_id,
                "title": layer_title,
                "script": layer_script_path,
                "status": final_status,
                "run_id": claim.run_id,
                "attempt": claim.attempt_revision,
                "best": best,
                "decided_by": decided_by,
                "authoritative_total": authoritative_total,
                "authoritative_passed": authoritative_passed,
                "failed_contracts": failed_contracts,
                "interfaces": interfaces,
                "revalidation_manifest": revalidation_manifest,
                "canonical": canonical,
            },
            claim=claim,
            layer_script_path=str(layer_script_path),
            final_status=str(final_status),
            best=best,
            receipt_canonical=receipt_canonical,
            blender_version=blender_version,
        )

    @classmethod
    def parse(
        cls,
        value: object,
        *,
        claim: LayerFinalizationClaim,
        layer_script_path: str,
        final_status: str,
        best: object,
        receipt_canonical: tuple[dict[str, Any], ...],
        blender_version: str,
        where: str = "layer outcome projection",
    ) -> LayerOutcomeProjection:
        if not isinstance(claim, LayerFinalizationClaim):
            raise ValueError(f"{where} requires a typed finalization claim")
        row = json_object(value, where)
        found = set(row)
        if found != _PROJECTION_FIELDS:
            raise ValueError(
                f"{where} fields mismatch; missing={sorted(_PROJECTION_FIELDS - found)}; "
                f"unexpected={sorted(found - _PROJECTION_FIELDS)}"
            )
        if row["schema"] != LAYER_OUTCOME_PROJECTION_SCHEMA:
            raise ValueError(
                f"{where}.schema must be {LAYER_OUTCOME_PROJECTION_SCHEMA!r}"
            )
        expected_identity = {
            "layer": claim.layer_id,
            "script": layer_script_path,
            "status": final_status,
            "run_id": claim.run_id,
            "attempt": claim.attempt_revision,
        }
        for field, expected in expected_identity.items():
            if row[field] != expected:
                raise ValueError(f"{where}.{field} does not match terminal authority")
        _text(row["title"], f"{where}.title")
        normalized_best = json_object(best, f"{where} expected best")
        if row["best"] != normalized_best:
            raise ValueError(f"{where}.best does not match terminal best")
        decided_by, authoritative, expected_interfaces = _source_summary(
            receipt_canonical,
            layer_id=claim.layer_id,
            where="layer finalization receipt canonical",
        )
        if row["decided_by"] != decided_by:
            raise ValueError(
                f"{where}.decided_by does not match terminal canonical verdicts"
            )
        total = _non_negative_integer(
            row["authoritative_total"],
            f"{where}.authoritative_total",
        )
        passed = _non_negative_integer(
            row["authoritative_passed"],
            f"{where}.authoritative_passed",
        )
        expected_total = len(authoritative)
        expected_passed = sum(item["pass"] is True for item in authoritative)
        if total != expected_total or passed != expected_passed:
            raise ValueError(
                f"{where} authoritative counts do not match terminal canonical evidence"
            )
        failed_contracts = row["failed_contracts"]
        if not isinstance(failed_contracts, list):
            raise ValueError(f"{where}.failed_contracts must be a list")
        expected_failed = [
            str(item["id"]) for item in authoritative if item["pass"] is False
        ]
        if failed_contracts != expected_failed:
            raise ValueError(
                f"{where}.failed_contracts does not match terminal canonical evidence"
            )
        interfaces = _object_rows(row["interfaces"], f"{where}.interfaces")
        if interfaces != expected_interfaces:
            raise ValueError(
                f"{where}.interfaces does not exactly project terminal interfaces"
            )
        manifest = json_object(
            row["revalidation_manifest"],
            f"{where}.revalidation_manifest",
        )
        observed_blender_version = manifest.get("blender_version")
        if (
            observed_blender_version is not None
            and observed_blender_version != blender_version
        ):
            raise ValueError(
                f"{where}.revalidation_manifest.blender_version does not match "
                "terminal authority"
            )
        canonical_rows = _object_rows(row["canonical"], f"{where}.canonical")
        _validate_canonical_projection(
            canonical_rows,
            authoritative=authoritative,
            revalidation_manifest=manifest,
            where=f"{where}.canonical",
        )
        normalized = {key: row[key] for key in OUTCOME_RECEIPT_BOUND_FIELDS}
        return cls(record=normalized)

    def as_dict(self) -> dict[str, Any]:
        return {"schema": LAYER_OUTCOME_PROJECTION_SCHEMA, **self.record}

    def as_record(self) -> dict[str, Any]:
        return dict(self.record)


__all__ = [
    "CANONICAL_EVIDENCE_KINDS",
    "LAYER_OUTCOME_PROJECTION_SCHEMA",
    "OUTCOME_RECEIPT_BOUND_FIELDS",
    "OUTCOME_SCHEMA",
    "LayerOutcomeProjection",
]
