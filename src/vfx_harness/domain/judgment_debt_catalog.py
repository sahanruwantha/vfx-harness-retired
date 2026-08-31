"""Strict, dependency-free parsing for requirements judgment-debt catalogs."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from vfx_harness.domain.judgment_debts import (
    JudgmentDebtActivation,
    JudgmentDebtDefinition,
)

JUDGMENT_DEBT_DEFINITIONS_KEY = "judgment_debt_definitions"
JUDGMENT_DEBT_ACTIVATIONS_KEY = "judgment_debt_activations"


def _validate_selected_bundle_digest(value: str | None, where: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where} must be a non-empty string")
    digest = value.strip()
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError(f"{where} must be a lowercase SHA-256 digest")
    return digest


def selected_bundle_digest(value: str | None, where: str) -> str | None:
    """Validate an optional selected bundle digest for plan-record callers."""
    return _validate_selected_bundle_digest(value, where)


def _validate_bindings(
    document: Mapping[str, Any],
    definitions: tuple[JudgmentDebtDefinition, ...],
) -> None:
    """Require one exact image-domain provisional binding per definition."""
    by_digest = {definition.digest: definition for definition in definitions}
    referenced: set[str] = set()
    for requirement_index, requirement in enumerate(document["requirements"]):
        resolution = requirement.get("resolution") or {}
        for binding_index, binding in enumerate(resolution.get("domain_bindings") or ()):
            if (
                not isinstance(binding, Mapping)
                or binding.get("domain") != "image"
                or binding.get("kind") != "provisional_decision"
            ):
                continue
            where = f"requirements.json.requirements[{requirement_index}].resolution.domain_bindings[{binding_index}]"
            definition_digest = str(binding.get("definition_digest") or "")
            definition = by_digest.get(definition_digest)
            if definition is None:
                raise ValueError(
                    f"{where}.definition_digest names no selected judgment debt definition: {definition_digest!r}"
                )
            expected = (
                str(requirement.get("id") or ""),
                str(requirement.get("statement") or "").strip(),
                str(binding.get("statement") or "").strip(),
                str(binding.get("decision_strength") or ""),
                str(binding.get("debt_id") or ""),
                str(binding.get("activates_at") or ""),
            )
            found = (
                definition.seed.requirement_id,
                definition.seed.statement,
                definition.seed.statement,
                definition.seed.decision_strength,
                definition.debt_id,
                definition.binding.activates_at,
            )
            if expected != found:
                raise ValueError(f"{where} does not exactly match judgment debt definition {definition.digest}")
            if definition.digest in referenced:
                raise ValueError(f"{where} duplicates binding for judgment debt definition {definition.digest}")
            referenced.add(definition.digest)
    missing = sorted(set(by_digest) - referenced)
    if missing:
        raise ValueError(
            "judgment debt definitions have no exact image-domain provisional binding: " + ", ".join(missing)
        )


def load_definitions(
    document: Mapping[str, Any],
    *,
    statements: Mapping[str, str],
    selected_bundle_digest: str | None,
) -> tuple[JudgmentDebtDefinition, ...]:
    """Parse immutable definitions and bind each to the selected requirement catalog."""
    rows = document[JUDGMENT_DEBT_DEFINITIONS_KEY]
    bundle_digest = _validate_selected_bundle_digest(selected_bundle_digest, "selected_bundle_digest") if rows else None
    out: list[JudgmentDebtDefinition] = []
    debt_ids: set[str] = set()
    definition_digests: set[str] = set()
    for index, row in enumerate(rows):
        where = f"requirements.json.{JUDGMENT_DEBT_DEFINITIONS_KEY}[{index}]"
        definition = JudgmentDebtDefinition.from_dict(row, where)
        if definition.digest in definition_digests:
            raise ValueError(f"{where}.definition_digest duplicates exact definition {definition.digest!r}")
        if definition.debt_id in debt_ids:
            raise ValueError(f"{where}.debt_id duplicates {definition.debt_id!r}")
        debt_ids.add(definition.debt_id)
        definition_digests.add(definition.digest)
        requirement_statement = statements.get(definition.seed.requirement_id)
        if requirement_statement is None:
            raise ValueError(
                f"{where}.seed.requirement_id names unknown requirement {definition.seed.requirement_id!r}"
            )
        if definition.seed.statement != requirement_statement:
            raise ValueError(
                f"{where}.seed.statement must exactly equal requirement {definition.seed.requirement_id!r} statement"
            )
        if bundle_digest is not None and definition.seed.bundle_digest != bundle_digest:
            raise ValueError(
                f"{where}.seed.bundle_digest is stale for selected bundle; expected "
                f"{bundle_digest!r}, found {definition.seed.bundle_digest!r}"
            )
        out.append(definition)
    definitions = tuple(out)
    _validate_bindings(document, definitions)
    return definitions


def load_activations(
    document: Mapping[str, Any],
    definitions: tuple[JudgmentDebtDefinition, ...],
) -> tuple[JudgmentDebtActivation, ...]:
    """Parse at most one exact payer activation for every definition."""
    rows = document[JUDGMENT_DEBT_ACTIVATIONS_KEY]
    by_digest = {definition.digest: definition for definition in definitions}
    if len(by_digest) != len(definitions):
        raise ValueError("judgment debt definitions must have unique definition_digest values")
    out: list[JudgmentDebtActivation] = []
    activated_definitions: set[str] = set()
    for index, row in enumerate(rows):
        where = f"requirements.json.{JUDGMENT_DEBT_ACTIVATIONS_KEY}[{index}]"
        activation = JudgmentDebtActivation.from_dict(row, where)
        definition = by_digest.get(activation.definition_digest)
        if definition is None:
            raise ValueError(
                f"{where}.definition_digest names unknown judgment debt definition {activation.definition_digest!r}"
            )
        if activation.definition_digest in activated_definitions:
            raise ValueError(f"{where} duplicates activation for exact definition {activation.definition_digest!r}")
        activation.assert_matches(definition)
        activated_definitions.add(activation.definition_digest)
        out.append(activation)
    return tuple(out)
