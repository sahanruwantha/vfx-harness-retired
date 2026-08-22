"""Typed planning meta-records and dependency-outcome due gates.

These records close the gap between normative brief text and executable work.  They are
immutable members of a published global-plan bundle; later resolutions are append-only
shot state keyed to that bundle's content hash.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REQUIREMENTS_SCHEMA = "vfx-harness.requirements/v1"
OBLIGATIONS_SCHEMA = "vfx-harness.obligations/v1"
ASSUMPTIONS_SCHEMA = "vfx-harness.assumptions/v1"
RESOLUTIONS_SCHEMA = "vfx-harness.plan-resolutions/v1"

RESOLUTION_KINDS = {"contract", "obligation", "decision"}
DUE_KINDS = {"before_unit", "unit_completion", "before_layer", "before_acceptance"}
EVIDENCE_KINDS = {"scene_contract", "image_contract", "human_decision", "replay"}
DECISION_STRENGTHS = {
    "hard_constraint",
    "approved_start",
    "planner_start",
    "confirmed_outcome",
}

_LIST_ITEM = re.compile(r"^\s*(?:[-+*]|\d+[.)])\s+")
_TABLE_SEPARATOR = re.compile(r"^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*$")


def sha256(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def brief_clause_spans(path: str | Path) -> tuple[tuple[int, int, str], ...]:
    """Return every substantive authored brief block with stable line bounds.

    Register completeness cannot depend on a model deciding which prose is normative.
    Instead, every body paragraph, list item, and table data row must be represented by
    at least one requirement citation. Headings and table headers are structure; YAML
    front matter is handled by the shot schema.
    """
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    out: list[tuple[int, int, str]] = []
    index = 0
    if lines and lines[0].strip() == "---":
        index = 1
        while index < len(lines) and lines[index].strip() != "---":
            index += 1
        index = min(index + 1, len(lines))

    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "```")):
            index += 1
            continue
        if stripped.startswith("|"):
            is_separator = bool(_TABLE_SEPARATOR.fullmatch(stripped))
            next_is_separator = (
                index + 1 < len(lines)
                and bool(_TABLE_SEPARATOR.fullmatch(lines[index + 1].strip()))
            )
            if not is_separator and not next_is_separator:
                out.append((index + 1, index + 1, stripped))
            index += 1
            continue

        start = index
        list_item = bool(_LIST_ITEM.match(line))
        index += 1
        while index < len(lines):
            candidate = lines[index]
            value = candidate.strip()
            if (
                not value
                or value.startswith(("#", "|", "```"))
                or (list_item and _LIST_ITEM.match(candidate))
            ):
                break
            index += 1
        text = " ".join(part.strip() for part in lines[start:index])
        out.append((start + 1, index, text))
    return tuple(out)


def _document(path: Path, schema: str, key: str) -> list[dict[str, Any]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path.name} is invalid JSON: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("schema") != schema:
        raise ValueError(f"{path.name} must be an object with schema={schema!r}")
    rows = raw.get(key)
    if not isinstance(rows, list):
        raise ValueError(f"{path.name}.{key} must be a list")
    return rows


def _text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where} must be a non-empty string")
    return value.strip()


def _ids(value: Any, where: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or (not allow_empty and not value):
        raise ValueError(f"{where} must be a {'non-empty ' if not allow_empty else ''}list")
    values = tuple(_text(item, f"{where}[{index}]") for index, item in enumerate(value))
    if len(values) != len(set(values)):
        raise ValueError(f"{where} contains duplicates")
    return values


@dataclass(frozen=True, slots=True)
class DueGate:
    kind: str
    layer: str | None = None
    unit: str | None = None

    @classmethod
    def parse(cls, value: Any, where: str) -> DueGate:
        if not isinstance(value, dict):
            raise ValueError(f"{where} must be an object")
        kind = value.get("kind")
        if kind not in DUE_KINDS:
            raise ValueError(f"{where}.kind must be one of {sorted(DUE_KINDS)}")
        layer = value.get("layer")
        unit = value.get("unit")
        if kind == "before_acceptance":
            if layer is not None or unit is not None:
                raise ValueError(f"{where} before_acceptance must omit layer and unit")
            return cls(kind)
        layer = _text(layer, f"{where}.layer")
        if kind == "before_layer":
            if unit is not None:
                raise ValueError(f"{where} before_layer must omit unit")
            return cls(kind, layer)
        return cls(kind, layer, _text(unit, f"{where}.unit"))

    def due_for(self, *, layer: str | None = None, unit: str | None = None,
                acceptance: bool = False, completion: bool = False) -> bool:
        if self.kind == "before_acceptance":
            return acceptance
        if str(layer) != self.layer:
            return False
        if self.kind == "unit_completion":
            return completion and str(unit) == self.unit
        if completion:
            return False
        return self.kind == "before_layer" or str(unit) == self.unit


@dataclass(frozen=True, slots=True)
class Requirement:
    id: str
    statement: str
    brief_sha256: str
    line_start: int
    line_end: int
    resolution_kind: str
    resolution_ids: tuple[str, ...]
    decision: str | None = None


@dataclass(frozen=True, slots=True)
class Obligation:
    id: str
    statement: str
    requirement_ids: tuple[str, ...]
    owner: str
    due: DueGate
    evidence: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class Assumption:
    id: str
    statement: str
    requirement_ids: tuple[str, ...]
    owner: str
    due: DueGate
    affected_layers: tuple[str, ...]
    affected_axes: tuple[str, ...]
    global_decision: bool
    decision_strength: str
    falsification_owner: str | None
    falsification_contract_ids: tuple[str, ...]


def decision_strength(value: Any, where: str, *, legacy_default: bool = False) -> str:
    """Parse decision authority without silently weakening legacy approvals.

    Old resolution records predate explicit strength.  Their safe compatibility meaning is
    ``hard_constraint``: callers may continue to read them, but automation may not tune them.
    New authored starts must state their weaker authority explicitly.
    """
    if value is None and legacy_default:
        return "hard_constraint"
    if value not in DECISION_STRENGTHS:
        raise ValueError(f"{where} must be one of {sorted(DECISION_STRENGTHS)}")
    return str(value)


def resolution_decision_strength(row: dict[str, Any], where: str) -> str:
    """Return the strength of an append-only decision/resolution record.

    A confirmed outcome is meaningful only when it names the accepted checkpoint that
    produced it.  This prevents prose or plan-time evidence from sealing future scene truth.
    """
    strength = decision_strength(row.get("decision_strength"), f"{where}.decision_strength", legacy_default=True)
    if strength == "confirmed_outcome":
        checkpoint = row.get("checkpoint_hash")
        if not isinstance(checkpoint, str) or len(checkpoint) != 64 or any(
            char not in "0123456789abcdef" for char in checkpoint
        ):
            raise ValueError(
                f"{where}.checkpoint_hash must be a lowercase SHA-256 digest for confirmed_outcome"
            )
    return strength


def load_requirements(root: str | Path, *, verify_brief: bool = True) -> tuple[Requirement, ...]:
    root = Path(root)
    rows = _document(root / "requirements.json", REQUIREMENTS_SCHEMA, "requirements")
    brief = root / "brief.md"
    brief_hash = sha256(brief) if verify_brief else None
    line_count = len(brief.read_text(encoding="utf-8").splitlines()) if verify_brief else None
    out: list[Requirement] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        where = f"requirements.json.requirements[{index}]"
        if not isinstance(row, dict):
            raise ValueError(f"{where} must be an object")
        rid = _text(row.get("id"), f"{where}.id")
        if rid in seen:
            raise ValueError(f"{where}.id duplicates {rid!r}")
        seen.add(rid)
        citation = row.get("citation")
        if not isinstance(citation, dict) or citation.get("source") != "brief.md":
            raise ValueError(f"{where}.citation must cite source 'brief.md'")
        cited_hash = _text(citation.get("sha256"), f"{where}.citation.sha256")
        if len(cited_hash) != 64 or any(c not in "0123456789abcdef" for c in cited_hash):
            raise ValueError(f"{where}.citation.sha256 must be a lowercase SHA-256 digest")
        start, end = citation.get("line_start"), citation.get("line_end")
        if any(isinstance(v, bool) or not isinstance(v, int) or v < 1 for v in (start, end)):
            raise ValueError(f"{where}.citation line bounds must be positive integers")
        if end < start:
            raise ValueError(f"{where}.citation.line_end cannot precede line_start")
        if verify_brief and (cited_hash != brief_hash or end > int(line_count)):
            detail = "hash differs from brief.md" if cited_hash != brief_hash else "line span exceeds brief.md"
            raise ValueError(f"{where}.citation {detail}")
        resolution = row.get("resolution")
        if not isinstance(resolution, dict) or resolution.get("kind") not in RESOLUTION_KINDS:
            raise ValueError(f"{where}.resolution.kind must be one of {sorted(RESOLUTION_KINDS)}")
        kind = str(resolution["kind"])
        ids = _ids(resolution.get("ids", []), f"{where}.resolution.ids", allow_empty=kind == "decision")
        decision = resolution.get("decision")
        if kind == "decision":
            decision = _text(decision, f"{where}.resolution.decision")
            if ids:
                raise ValueError(f"{where}.resolution decision must not carry ids")
        elif decision is not None:
            raise ValueError(f"{where}.resolution.{kind} must omit decision")
        out.append(Requirement(rid, _text(row.get("statement"), f"{where}.statement"), cited_hash,
                               start, end, kind, ids, decision))
    if not out:
        raise ValueError("requirements.json.requirements must not be empty")
    return tuple(out)


def _evidence(value: Any, where: str) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{where} must be a non-empty list")
    out = []
    for index, row in enumerate(value):
        at = f"{where}[{index}]"
        if not isinstance(row, dict) or row.get("kind") not in EVIDENCE_KINDS:
            raise ValueError(f"{at}.kind must be one of {sorted(EVIDENCE_KINDS)}")
        out.append((str(row["kind"]), _text(row.get("id"), f"{at}.id")))
    if len(out) != len(set(out)):
        raise ValueError(f"{where} contains duplicate evidence bindings")
    return tuple(out)


def load_obligations(root: str | Path) -> tuple[Obligation, ...]:
    root = Path(root)
    rows = _document(root / "obligations.json", OBLIGATIONS_SCHEMA, "obligations")
    out: list[Obligation] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        where = f"obligations.json.obligations[{index}]"
        if not isinstance(row, dict):
            raise ValueError(f"{where} must be an object")
        oid = _text(row.get("id"), f"{where}.id")
        if oid in seen:
            raise ValueError(f"{where}.id duplicates {oid!r}")
        seen.add(oid)
        out.append(Obligation(
            oid,
            _text(row.get("statement"), f"{where}.statement"),
            _ids(row.get("requirement_ids"), f"{where}.requirement_ids"),
            _text(row.get("owner"), f"{where}.owner"),
            DueGate.parse(row.get("due"), f"{where}.due"),
            _evidence(row.get("evidence"), f"{where}.evidence"),
        ))
    return tuple(out)


def load_assumptions(root: str | Path) -> tuple[Assumption, ...]:
    root = Path(root)
    rows = _document(root / "assumptions.json", ASSUMPTIONS_SCHEMA, "assumptions")
    out: list[Assumption] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        where = f"assumptions.json.assumptions[{index}]"
        if not isinstance(row, dict):
            raise ValueError(f"{where} must be an object")
        aid = _text(row.get("id"), f"{where}.id")
        if aid in seen:
            raise ValueError(f"{where}.id duplicates {aid!r}")
        seen.add(aid)
        impact = row.get("impact")
        if not isinstance(impact, dict):
            raise ValueError(f"{where}.impact must be an object")
        layers = _ids(impact.get("layers", []), f"{where}.impact.layers", allow_empty=True)
        axes = _ids(impact.get("axes", []), f"{where}.impact.axes", allow_empty=True)
        global_decision = impact.get("global_decision", False)
        if not isinstance(global_decision, bool):
            raise ValueError(f"{where}.impact.global_decision must be boolean")
        if not global_decision and not layers and not axes:
            raise ValueError(f"{where}.impact must declare layers, axes, or global_decision")
        strength = decision_strength(
            row.get("decision_strength"),
            f"{where}.decision_strength",
            legacy_default=True,
        )
        if strength == "confirmed_outcome":
            raise ValueError(
                f"{where}.decision_strength cannot be confirmed_outcome; assumptions are unresolved"
            )
        raw_falsification = row.get("falsification")
        falsification_owner = None
        falsification_contract_ids: tuple[str, ...] = ()
        if raw_falsification is not None:
            if not isinstance(raw_falsification, dict):
                raise ValueError(f"{where}.falsification must be an object")
            falsification_owner = _text(
                raw_falsification.get("owner"), f"{where}.falsification.owner"
            )
            falsification_contract_ids = _ids(
                raw_falsification.get("contract_ids"),
                f"{where}.falsification.contract_ids",
            )
        if strength in {"approved_start", "planner_start"} and not falsification_owner:
            raise ValueError(
                f"{where} {strength} requires falsification.owner and contract_ids"
            )
        out.append(Assumption(
            aid,
            _text(row.get("statement"), f"{where}.statement"),
            _ids(row.get("requirement_ids", []), f"{where}.requirement_ids", allow_empty=True),
            _text(row.get("owner"), f"{where}.owner"),
            DueGate.parse(row.get("due"), f"{where}.due"),
            layers,
            axes,
            global_decision,
            strength,
            falsification_owner,
            falsification_contract_ids,
        ))
    return tuple(out)


def load_resolutions(
    path: str | Path, *, bundle_hash: str
) -> dict[tuple[str, str], tuple[tuple[str, str], ...]]:
    """Return satisfied records and evidence for exactly one published bundle."""
    path = Path(path)
    if not path.is_file():
        return {}
    out: dict[tuple[str, str], tuple[tuple[str, str], ...]] = {}
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path.name}:{line_no} is invalid JSON: {exc}") from exc
        if row.get("schema") != RESOLUTIONS_SCHEMA:
            raise ValueError(f"{path.name}:{line_no} has unsupported schema")
        if row.get("bundle_hash") != bundle_hash:
            continue
        kind = row.get("kind")
        if kind not in {"obligation", "assumption"}:
            raise ValueError(f"{path.name}:{line_no}.kind must be obligation or assumption")
        rid = _text(row.get("id"), f"{path.name}:{line_no}.id")
        if row.get("status") == "satisfied":
            evidence = _evidence(row.get("evidence"), f"{path.name}:{line_no}.evidence")
            out[(str(kind), rid)] = evidence
    return out
