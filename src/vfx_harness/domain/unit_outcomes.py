"""Typed model-free outcomes produced when executable reality invalidates plan authority."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from vfx_harness.domain.plan_records import DECISION_STRENGTHS

HYPOTHESIS_FALSIFICATION_SCHEMA = "vfx-harness.hypothesis-falsification/v1"
AUTHORITY_CONFLICTS = {
    "decision",
    "dependency",
    "ownership",
    "mutation_scope",
    "contract",
    "sealed_outcome",
}
_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where} must be a non-empty string")
    return value.strip()


def _id(value: Any, where: str) -> str:
    value = _text(value, where)
    if not _ID.fullmatch(value):
        raise ValueError(f"{where} has invalid id {value!r}")
    return value


def _hash(value: Any, where: str) -> str:
    value = _text(value, where)
    if not _SHA256.fullmatch(value):
        raise ValueError(f"{where} must be a lowercase SHA-256 digest")
    return value


def _strings(value: Any, where: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or (not allow_empty and not value):
        qualifier = "non-empty " if not allow_empty else ""
        raise ValueError(f"{where} must be a {qualifier}list")
    out = tuple(_text(item, f"{where}[{index}]") for index, item in enumerate(value))
    if len(out) != len(set(out)):
        raise ValueError(f"{where} contains duplicates")
    return out


@dataclass(frozen=True, slots=True)
class DecisionAuthority:
    id: str
    strength: str

    @classmethod
    def parse(cls, value: Any, where: str) -> DecisionAuthority:
        if not isinstance(value, dict):
            raise ValueError(f"{where} must be an object")
        strength = value.get("strength")
        if strength not in DECISION_STRENGTHS:
            raise ValueError(f"{where}.strength must be one of {sorted(DECISION_STRENGTHS)}")
        return cls(_id(value.get("id"), f"{where}.id"), str(strength))


@dataclass(frozen=True, slots=True)
class AuthorityConflict:
    kind: str
    required_authority: str
    roles: tuple[str, ...]
    controls: tuple[str, ...]

    @classmethod
    def parse(cls, value: Any, where: str) -> AuthorityConflict:
        if not isinstance(value, dict):
            raise ValueError(f"{where} must be an object")
        kind = value.get("kind")
        if kind not in AUTHORITY_CONFLICTS:
            raise ValueError(f"{where}.kind must be one of {sorted(AUTHORITY_CONFLICTS)}")
        return cls(
            str(kind),
            _text(value.get("required_authority"), f"{where}.required_authority"),
            _strings(value.get("roles", []), f"{where}.roles", allow_empty=True),
            _strings(value.get("controls", []), f"{where}.controls", allow_empty=True),
        )


@dataclass(frozen=True, slots=True)
class HypothesisFalsification:
    record_id: str
    recorded_at: str
    layer: str
    unit: str
    bundle_hash: str
    plan_hash: str
    unit_hash: str
    unit_plan_hash: str
    candidate_hash: str
    settings_hash: str
    contract_ids: tuple[str, ...]
    observations: tuple[dict[str, Any], ...]
    decisions: tuple[DecisionAuthority, ...]
    conflict: AuthorityConflict
    evidence: tuple[str, ...]
    affected: tuple[str, ...]

    @classmethod
    def parse(cls, value: Any, where: str = "hypothesis falsification") -> HypothesisFalsification:
        if not isinstance(value, dict) or value.get("schema") != HYPOTHESIS_FALSIFICATION_SCHEMA:
            raise ValueError(
                f"{where} must be an object with schema={HYPOTHESIS_FALSIFICATION_SCHEMA!r}"
            )
        identities = value.get("identities")
        if not isinstance(identities, dict):
            raise ValueError(f"{where}.identities must be an object")
        raw_observations = value.get("observations")
        if not isinstance(raw_observations, list) or not raw_observations or not all(
            isinstance(item, dict) for item in raw_observations
        ):
            raise ValueError(f"{where}.observations must be a non-empty list of objects")
        raw_decisions = value.get("decisions", [])
        if not isinstance(raw_decisions, list):
            raise ValueError(f"{where}.decisions must be a list")
        decisions = tuple(
            DecisionAuthority.parse(item, f"{where}.decisions[{index}]")
            for index, item in enumerate(raw_decisions)
        )
        if len({item.id for item in decisions}) != len(decisions):
            raise ValueError(f"{where}.decisions contains duplicate ids")
        return cls(
            _id(value.get("record_id"), f"{where}.record_id"),
            _text(value.get("recorded_at"), f"{where}.recorded_at"),
            _text(value.get("layer"), f"{where}.layer"),
            _id(value.get("unit"), f"{where}.unit"),
            _hash(identities.get("bundle_hash"), f"{where}.identities.bundle_hash"),
            _hash(identities.get("plan_hash"), f"{where}.identities.plan_hash"),
            _hash(identities.get("unit_hash"), f"{where}.identities.unit_hash"),
            _hash(identities.get("unit_plan_hash"), f"{where}.identities.unit_plan_hash"),
            _hash(identities.get("candidate_hash"), f"{where}.identities.candidate_hash"),
            _hash(identities.get("settings_hash"), f"{where}.identities.settings_hash"),
            _strings(value.get("contract_ids", []), f"{where}.contract_ids", allow_empty=True),
            tuple(dict(item) for item in raw_observations),
            decisions,
            AuthorityConflict.parse(value.get("conflict"), f"{where}.conflict"),
            _strings(value.get("evidence"), f"{where}.evidence"),
            _strings(value.get("affected"), f"{where}.affected"),
        )

    @property
    def changes_hard_constraint(self) -> bool:
        return any(item.strength == "hard_constraint" for item in self.decisions)


def load_hypothesis_falsification(path: str | Any) -> HypothesisFalsification:
    import json
    from pathlib import Path

    target = Path(path)
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{target} is invalid JSON: {exc}") from exc
    return HypothesisFalsification.parse(value, str(target))
