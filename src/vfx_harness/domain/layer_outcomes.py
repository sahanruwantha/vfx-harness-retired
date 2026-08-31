"""Strict consumer contract for sealed layer outcomes.

Layer outcomes contain diagnostic and revalidation data in addition to the small
interface consumed by deferred materialization.  Consumers must validate the envelope
and read evidence only from the producer-owned ``interfaces`` and
``canonical[].authoritative`` collections; recursively scanning arbitrary JSON lets an
unrelated nested ``{id, pass}`` row impersonate a promised upstream result.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from vfx_harness.domain.stop_envelope_primitives import canonical_digest

OUTCOME_SCHEMA = 2

_EVIDENCE_FIELDS = (
    "id",
    "kind",
    "metric",
    "value",
    "target",
    "pass",
    "error",
    "note",
    "owner_layer",
    "fault_owner",
    "activates_at",
    "lifecycle",
)
_SOURCE_TO_REQUIRED_KIND = {
    "interface_contract": "scene_contract",
    "image_contract": "image_contract",
    "semantic_diff": "semantic_diff",
}


class LayerOutcomeContractError(ValueError):
    """A sealed outcome cannot be consumed under the current strict contract."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class SealedLayerOutcome:
    """The dependency-facing projection of one schema-2 layer outcome."""

    layer_id: str
    status: str
    script: str | None
    evidence: tuple[dict[str, Any], ...]

    @property
    def passed_bindings(self) -> frozenset[tuple[str, str]]:
        return frozenset(
            (str(row["kind"]), str(row["id"]))
            for row in self.evidence
            if row["pass"] is True
        )

    def required_evidence(
        self,
        required: frozenset[tuple[str, str]],
    ) -> tuple[dict[str, Any], ...]:
        matched = [
            row
            for row in self.evidence
            if (str(row["kind"]), str(row["id"])) in required
        ]
        unique = {canonical_digest(row): row for row in matched}
        return tuple(unique[digest] for digest in sorted(unique))


def _rows(value: object, where: str) -> list[Mapping[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(row, Mapping) for row in value):
        raise LayerOutcomeContractError("evidence_shape", f"{where} must be a list of objects")
    return list(value)


def _evidence(
    value: Mapping[str, Any],
    *,
    where: str,
    default_source: str | None,
) -> dict[str, Any] | None:
    identifier = value.get("id")
    if not isinstance(identifier, str) or not identifier or identifier != identifier.strip():
        raise LayerOutcomeContractError("evidence_id", f"{where}.id must be a non-empty trimmed string")
    passed = value.get("pass")
    if not isinstance(passed, bool):
        raise LayerOutcomeContractError("evidence_pass", f"{where}.pass must be Boolean")
    observed_source = value.get("source")
    if default_source is not None and observed_source not in (None, default_source):
        raise LayerOutcomeContractError(
            "evidence_kind",
            f"{where}.source contradicts its producer-owned evidence collection",
        )
    source = default_source or observed_source
    required_kind = _SOURCE_TO_REQUIRED_KIND.get(str(source or ""))
    if required_kind is None:
        return None
    normalized = {
        key: item
        for key in _EVIDENCE_FIELDS
        if (item := value.get(key)) not in (None, "")
    }
    normalized["id"] = identifier
    normalized["kind"] = required_kind
    normalized["pass"] = passed
    canonical_digest(normalized)
    return normalized


def parse_sealed_layer_outcome(
    value: object,
    *,
    expected_layer_id: str,
) -> SealedLayerOutcome:
    """Validate one outcome and return only its materialization-facing evidence."""

    if not isinstance(value, Mapping):
        raise LayerOutcomeContractError("shape", "sealed layer outcome must be an object")
    if value.get("schema") != OUTCOME_SCHEMA:
        raise LayerOutcomeContractError(
            "schema",
            f"sealed layer outcome schema must be {OUTCOME_SCHEMA}",
        )
    layer_id = value.get("layer")
    if not isinstance(layer_id, str) or layer_id != expected_layer_id:
        raise LayerOutcomeContractError(
            "layer",
            "sealed layer outcome does not match its dependency layer",
        )
    status = value.get("status")
    if not isinstance(status, str) or not status or status != status.strip():
        raise LayerOutcomeContractError(
            "status",
            "sealed layer outcome status must be a non-empty trimmed string",
        )
    script = value.get("script")
    if script is not None and (
        not isinstance(script, str) or not script or script != script.strip()
    ):
        raise LayerOutcomeContractError(
            "script",
            "sealed layer outcome script must be a non-empty trimmed string when present",
        )

    interface_evidence: list[dict[str, Any]] = []
    for index, row in enumerate(_rows(value.get("interfaces"), "outcome.interfaces")):
        if row.get("source") is not None:
            raise LayerOutcomeContractError(
                "interface_projection",
                "outcome.interfaces rows must not override their producer-owned source",
            )
        parsed = _evidence(
            row,
            where=f"outcome.interfaces[{index}]",
            default_source="interface_contract",
        )
        if parsed is None or parsed.get("owner_layer") != layer_id:
            raise LayerOutcomeContractError(
                "interface_owner",
                "outcome.interfaces may export only contracts owned by its layer",
            )
        interface_evidence.append(parsed)

    canonical_rows = _rows(value.get("canonical"), "outcome.canonical")
    if status == "passed" and not canonical_rows:
        raise LayerOutcomeContractError(
            "canonical_missing",
            "a passed sealed layer outcome must contain canonical records",
        )
    canonical_interfaces: list[dict[str, Any]] = []
    canonical_only_evidence: list[dict[str, Any]] = []
    for canonical_index, canonical in enumerate(canonical_rows):
        for evidence_index, row in enumerate(
            _rows(
                canonical.get("authoritative"),
                f"outcome.canonical[{canonical_index}].authoritative",
            )
        ):
            source = row.get("source")
            if source in _SOURCE_TO_REQUIRED_KIND:
                owner_layer = row.get("owner_layer")
                if not isinstance(owner_layer, str) or not owner_layer.strip():
                    raise LayerOutcomeContractError(
                        "evidence_owner",
                        "canonical exported evidence must name its semantic owner",
                    )
                if owner_layer != layer_id:
                    # Cumulative replay also evaluates protected contracts owned by
                    # predecessors.  Those rows remain internal canonical evidence and
                    # cannot be re-exported by this layer.
                    continue
            parsed = _evidence(
                row,
                where=(
                    f"outcome.canonical[{canonical_index}]."
                    f"authoritative[{evidence_index}]"
                ),
                default_source=None,
            )
            if parsed is None:
                continue
            if source == "interface_contract":
                canonical_interfaces.append(parsed)
            else:
                canonical_only_evidence.append(parsed)

    projected = Counter(canonical_digest(row) for row in interface_evidence)
    canonical_projection = Counter(canonical_digest(row) for row in canonical_interfaces)
    if projected != canonical_projection:
        raise LayerOutcomeContractError(
            "interface_projection",
            "outcome.interfaces do not exactly project owner-layer canonical interfaces",
        )

    evidence = [*interface_evidence, *canonical_only_evidence]
    verdicts: dict[tuple[str, str], set[bool]] = {}
    for row in evidence:
        binding = (str(row["kind"]), str(row["id"]))
        verdicts.setdefault(binding, set()).add(bool(row["pass"]))
    conflicts = sorted(binding for binding, values in verdicts.items() if len(values) != 1)
    if conflicts:
        raise LayerOutcomeContractError(
            "evidence_conflict",
            "sealed layer outcome contains conflicting evidence verdicts for "
            + ", ".join(f"{kind}:{identifier}" for kind, identifier in conflicts),
        )
    unique = {canonical_digest(row): row for row in evidence}
    return SealedLayerOutcome(
        layer_id=layer_id,
        status=status,
        script=script,
        evidence=tuple(unique[digest] for digest in sorted(unique)),
    )
