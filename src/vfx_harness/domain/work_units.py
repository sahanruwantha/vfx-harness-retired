"""Strict schema-4 layer, work-unit, claim, and evidence contracts.

The planner owns shot-specific decomposition.  This module owns only the generic shape,
validation, dependency semantics, claim authority, and protection resolution.  There is
deliberately no adapter for the former top-level array schema: execution authority must
not be guessed from an obsolete document.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

SCHEMA = 4
TEMPORAL_EVIDENCE = {"none", "keyframes", "motion"}
CLAIM_AUTHORITIES = {
    "executable_required",
    "qualified_qualitative_required",
    "human_required",
    "advisory",
}
CLAIM_KINDS = {"atomic", "interaction"}
EVIDENCE_KINDS = {
    "scene_contract",
    "image_contract",
    "semantic_diff",
    "qualification",
    "human_decision",
}
UNIT_STATES = {
    "pending",
    "planning",
    "building",
    "frozen",
    "evaluating",
    "repairing",
    "passed",
    "failed",
    "hypothesis_falsified",
    "retryable",
    "blocked",
    "superseded",
}
_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _mapping(value: Any, where: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{where} must be an object")
    return value


def _text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where} must be a non-empty string")
    return value.strip()


def _id(value: Any, where: str) -> str:
    out = _text(value, where)
    if not _ID.fullmatch(out):
        raise ValueError(f"{where} has invalid id {out!r}")
    return out


def _strings(value: Any, where: str, *, allow_empty: bool = True) -> tuple[str, ...]:
    if not isinstance(value, list) or (not allow_empty and not value):
        suffix = "non-empty " if not allow_empty else ""
        raise ValueError(f"{where} must be a {suffix}list")
    out = tuple(_text(item, f"{where}[{index}]") for index, item in enumerate(value))
    if len(set(out)) != len(out):
        raise ValueError(f"{where} contains duplicates")
    return out


def _relative_path(value: Any, where: str) -> str:
    out = _text(value, where).replace("\\", "/")
    p = PurePosixPath(out)
    if p.is_absolute() or ".." in p.parts:
        raise ValueError(f"{where} must be a safe relative path")
    return out


@dataclass(frozen=True)
class JudgePoint:
    frame: int
    ref: str

    @classmethod
    def parse(cls, value: Any, where: str) -> JudgePoint:
        row = _mapping(value, where)
        frame = row.get("frame")
        if isinstance(frame, bool) or not isinstance(frame, int) or frame < 1:
            raise ValueError(f"{where}.frame must be a positive integer")
        return cls(frame, _relative_path(row.get("ref"), f"{where}.ref"))


def _judge_points(value: Any, where: str) -> tuple[JudgePoint, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{where} must be a non-empty list")
    out = tuple(JudgePoint.parse(row, f"{where}[{index}]") for index, row in enumerate(value))
    frames = [point.frame for point in out]
    if len(set(frames)) != len(frames):
        raise ValueError(f"{where} contains duplicate frames")
    return out


@dataclass(frozen=True)
class EvidenceBinding:
    """One explicit evidence path for one claim.

    Collection labels such as ``scene_contracts`` are deliberately invalid.  A required
    claim must say which exact contract, qualification, or human decision can settle it.
    """

    kind: str
    id: str

    @classmethod
    def parse(cls, value: Any, where: str) -> EvidenceBinding:
        row = _mapping(value, where)
        kind = row.get("kind")
        if kind not in EVIDENCE_KINDS:
            raise ValueError(f"{where}.kind must be one of {sorted(EVIDENCE_KINDS)}")
        extra = sorted(set(row) - {"kind", "id"})
        if extra:
            raise ValueError(f"{where} has unknown fields: {', '.join(extra)}")
        return cls(kind, _id(row.get("id"), f"{where}.id"))


def _bindings(value: Any, where: str) -> tuple[EvidenceBinding, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{where} must be a non-empty list of explicit evidence bindings")
    out = tuple(EvidenceBinding.parse(item, f"{where}[{index}]") for index, item in enumerate(value))
    keys = [(item.kind, item.id) for item in out]
    if len(set(keys)) != len(keys):
        raise ValueError(f"{where} contains duplicate bindings")
    return out


def _moments(value: Any, where: str) -> tuple[int, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{where} must be a non-empty list")
    out = []
    for index, item in enumerate(value):
        if isinstance(item, bool) or not isinstance(item, int) or item < 1:
            raise ValueError(f"{where}[{index}] must be a positive integer")
        out.append(item)
    if len(set(out)) != len(out):
        raise ValueError(f"{where} contains duplicates")
    return tuple(out)


@dataclass(frozen=True)
class Claim:
    id: str
    proposition: str
    axis: str
    property: str
    subject_roles: tuple[str, ...]
    subject_controls: tuple[str, ...]
    moments: tuple[int, ...]
    kind: str
    required: bool
    authority: str
    repair_owner: str
    evidence: tuple[EvidenceBinding, ...]
    qualification: dict[str, str] | None = None
    coordination_owner: str | None = None
    participants: tuple[str, ...] = ()
    controls: tuple[str, ...] = ()

    @classmethod
    def parse(cls, value: Any, where: str) -> Claim:
        row = _mapping(value, where)
        cid = _id(row.get("id"), f"{where}.id")
        proposition = _text(row.get("proposition"), f"{where}.proposition")
        axis = _id(row.get("axis"), f"{where}.axis")
        prop = _id(row.get("property"), f"{where}.property")
        subject_roles = _strings(row.get("subject_roles", []), f"{where}.subject_roles")
        subject_controls = _strings(row.get("subject_controls", []), f"{where}.subject_controls")
        if not subject_roles and not subject_controls:
            raise ValueError(f"{where} must declare subject_roles or subject_controls")
        moments = _moments(row.get("moments"), f"{where}.moments")
        kind = row.get("kind", "atomic")
        if kind not in CLAIM_KINDS:
            raise ValueError(f"{where}.kind must be one of {sorted(CLAIM_KINDS)}")
        required = row.get("required")
        if not isinstance(required, bool):
            raise ValueError(f"{where}.required must be boolean")
        authority = row.get("authority")
        if authority not in CLAIM_AUTHORITIES:
            raise ValueError(f"{where}.authority must be one of {sorted(CLAIM_AUTHORITIES)}")
        if required and authority == "advisory":
            raise ValueError(f"{where} cannot be both required and advisory")
        if not required and authority != "advisory":
            raise ValueError(f"{where} non-required claims must be advisory")

        qualification = row.get("qualification")
        if authority == "qualified_qualitative_required":
            q = _mapping(qualification, f"{where}.qualification")
            qualification = {
                "suite": _text(q.get("suite"), f"{where}.qualification.suite"),
                "judge_model": _text(q.get("judge_model"), f"{where}.qualification.judge_model"),
                "prompt": _text(q.get("prompt"), f"{where}.qualification.prompt"),
                "evidence_shape": _text(
                    q.get("evidence_shape"), f"{where}.qualification.evidence_shape"
                ),
                "artifact": _relative_path(q.get("artifact"), f"{where}.qualification.artifact"),
                "artifact_sha256": _text(
                    q.get("artifact_sha256"), f"{where}.qualification.artifact_sha256"
                ).lower(),
            }
            if not _SHA256.fullmatch(qualification["artifact_sha256"]):
                raise ValueError(f"{where}.qualification.artifact_sha256 must be 64 lowercase hex characters")
        elif qualification is not None:
            raise ValueError(f"{where}.qualification is only valid for qualified qualitative authority")

        repair_owner = _id(row.get("repair_owner"), f"{where}.repair_owner")
        evidence = _bindings(row.get("evidence"), f"{where}.evidence")
        evidence_kinds = {item.kind for item in evidence}
        if authority == "executable_required" and not evidence_kinds <= {
            "scene_contract",
            "image_contract",
            "semantic_diff",
        }:
            raise ValueError(f"{where} executable authority requires executable evidence bindings")
        if authority == "qualified_qualitative_required" and "qualification" not in evidence_kinds:
            raise ValueError(f"{where} qualified qualitative authority requires a qualification binding")
        if authority == "human_required" and "human_decision" not in evidence_kinds:
            raise ValueError(f"{where} human authority requires a human_decision binding")
        coordination_owner = row.get("coordination_owner")
        participants = _strings(row.get("participants", []), f"{where}.participants")
        controls = _strings(row.get("controls", []), f"{where}.controls")
        if kind == "interaction":
            coordination_owner = _id(coordination_owner, f"{where}.coordination_owner")
            if len(participants) < 2:
                raise ValueError(f"{where}.participants needs at least two units for an interaction claim")
            if not controls:
                raise ValueError(f"{where}.controls must bound interaction balancing")
        elif coordination_owner is not None or participants or controls:
            raise ValueError(f"{where} atomic claims cannot declare interaction coordination fields")

        return cls(
            cid,
            proposition,
            axis,
            prop,
            subject_roles,
            subject_controls,
            moments,
            kind,
            required,
            authority,
            repair_owner,
            evidence,
            qualification,
            coordination_owner,
            participants,
            controls,
        )

    @property
    def binding_ids(self) -> tuple[str, ...]:
        return tuple(item.id for item in self.evidence)


def validate_qualification(root: str | Path, claim: Claim, where: str) -> None:
    """Prove that a qualitative blocking claim references an exact passed qualification."""
    if claim.authority != "qualified_qualitative_required":
        return
    qualification = claim.qualification or {}
    artifact = Path(root) / qualification["artifact"]
    if not artifact.is_file():
        raise ValueError(f"{where}.qualification artifact is missing: {qualification['artifact']}")
    payload = artifact.read_bytes()
    actual_hash = hashlib.sha256(payload).hexdigest()
    if actual_hash != qualification["artifact_sha256"]:
        raise ValueError(
            f"{where}.qualification artifact hash mismatch: expected "
            f"{qualification['artifact_sha256']}, got {actual_hash}"
        )
    try:
        record = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{where}.qualification artifact is invalid JSON: {exc}") from exc
    if not isinstance(record, dict) or record.get("schema") != 1:
        raise ValueError(f"{where}.qualification artifact must be an object with schema=1")
    for key in ("suite", "judge_model", "prompt", "evidence_shape"):
        if record.get(key) != qualification[key]:
            raise ValueError(f"{where}.qualification artifact {key} does not match the claim")
    if record.get("passed") is not True:
        raise ValueError(f"{where}.qualification artifact did not pass")
    budgets = record.get("budgets")
    metrics = record.get("metrics")
    required_rates = (
        "false_pass_rate",
        "false_failure_rate",
        "repeatability_failure_rate",
        "scope_leakage_rate",
        "irrelevant_change_sensitivity_rate",
    )
    if not isinstance(budgets, dict) or not isinstance(metrics, dict):
        raise ValueError(f"{where}.qualification artifact requires budgets and metrics objects")
    for key in required_rates:
        budget = budgets.get(key)
        measured = metrics.get(key)
        if not isinstance(budget, (int, float)) or isinstance(budget, bool) or not 0 <= budget <= 1:
            raise ValueError(f"{where}.qualification budget {key} must be a rate in [0,1]")
        if not isinstance(measured, (int, float)) or isinstance(measured, bool) or not 0 <= measured <= 1:
            raise ValueError(f"{where}.qualification metric {key} must be a rate in [0,1]")
        if measured > budget:
            raise ValueError(
                f"{where}.qualification metric {key}={measured} exceeds budget {budget}"
            )


@dataclass(frozen=True)
class MutationScope:
    mode: str
    roles: tuple[str, ...]
    controls: tuple[str, ...]
    script_spans: tuple[str, ...]
    control_roles: tuple[tuple[str, tuple[str, ...]], ...] = ()

    @classmethod
    def parse(cls, value: Any, where: str) -> MutationScope:
        row = _mapping(value, where)
        mode = row.get("mode", "scoped")
        if mode not in {"scoped", "none"}:
            raise ValueError(f"{where}.mode must be 'scoped' or 'none'")
        roles = _strings(row.get("roles", []), f"{where}.roles")
        controls = _strings(row.get("controls", []), f"{where}.controls")
        spans = tuple(_relative_path(v, f"{where}.script_spans") for v in row.get("script_spans", []))
        if len(set(spans)) != len(spans):
            raise ValueError(f"{where}.script_spans contains duplicates")
        if mode == "none" and any((roles, controls, spans)):
            raise ValueError(f"{where} mode 'none' cannot declare mutation targets")
        if mode == "scoped" and not any((roles, controls, spans)):
            raise ValueError(f"{where} scoped mutation needs roles, controls, or script_spans")
        raw_mapping = row.get("control_roles", {})
        # Dataclass-to-JSON test/tooling paths serialize the empty tuple default as [].
        # Treat only that empty shape as the same legacy omission; non-empty mappings are
        # always objects in the authored schema.
        if raw_mapping == []:
            raw_mapping = {}
        if not isinstance(raw_mapping, dict):
            raise ValueError(f"{where}.control_roles must be an object")
        mapping = []
        for control, values in raw_mapping.items():
            key = _text(control, f"{where}.control_roles key")
            targets = _strings(values, f"{where}.control_roles.{key}", allow_empty=False)
            if key not in controls:
                raise ValueError(f"{where}.control_roles maps undeclared control {key!r}")
            unknown = sorted(set(targets) - set(roles))
            if unknown:
                raise ValueError(
                    f"{where}.control_roles.{key} maps roles outside mutation scope: {', '.join(unknown)}"
                )
            mapping.append((key, targets))
        if mapping and set(raw_mapping) != set(controls):
            missing = sorted(set(controls) - set(raw_mapping))
            raise ValueError(f"{where}.control_roles does not map controls: {', '.join(missing)}")
        return cls(mode, roles, controls, spans, tuple(sorted(mapping)))


@dataclass(frozen=True)
class ProtectionSpec:
    selector: str | None
    ids: tuple[str, ...]
    resolve_at: str

    @classmethod
    def parse(cls, value: Any, where: str) -> ProtectionSpec:
        row = _mapping(value, where)
        selector = row.get("selector")
        ids = _strings(row.get("ids", []), f"{where}.ids")
        if selector is not None:
            selector = _text(selector, f"{where}.selector")
        if bool(selector) == bool(ids):
            raise ValueError(f"{where} must declare exactly one of selector or ids")
        resolve_at = row.get("resolve_to_explicit_ids_at", "freeze")
        if resolve_at != "freeze":
            raise ValueError(f"{where}.resolve_to_explicit_ids_at must be 'freeze'")
        return cls(selector, ids, resolve_at)

    def resolve(self, active_ids: list[str] | tuple[str, ...] | set[str]) -> tuple[str, ...]:
        active = tuple(sorted({_text(value, "active protection id") for value in active_ids}))
        if self.ids:
            missing = sorted(set(self.ids) - set(active))
            if missing:
                raise ValueError(f"explicit protected contracts are not active: {', '.join(missing)}")
            return tuple(sorted(self.ids))
        if self.selector != "all_active_upstream_interfaces":
            raise ValueError(f"unknown protection selector {self.selector!r}")
        return active


@dataclass(frozen=True)
class CompositionContext:
    """Explicit visual context used to accept composition at declared moments."""

    frames: tuple[int, ...]
    source_unit: str | None
    contract_ids: tuple[str, ...]

    @classmethod
    def parse(cls, value: Any, where: str) -> CompositionContext:
        row = _mapping(value, where)
        frames = _moments(row.get("frames"), f"{where}.frames")
        source = row.get("source_unit")
        if source is not None:
            source = _id(source, f"{where}.source_unit")
        contract_ids = _strings(row.get("contract_ids", []), f"{where}.contract_ids")
        if bool(source) == bool(contract_ids):
            raise ValueError(f"{where} must declare exactly one of source_unit or contract_ids")
        return cls(frames, source, contract_ids)


@dataclass(frozen=True)
class EvaluationPolicy:
    primary_judge: int
    judges: tuple[JudgePoint, ...]
    temporal_evidence: str
    claims: tuple[Claim, ...]
    composition_context: CompositionContext | None = None

    @classmethod
    def parse(cls, value: Any, where: str) -> EvaluationPolicy:
        row = _mapping(value, where)
        judges = _judge_points(row.get("judge"), f"{where}.judge")
        primary = row.get("primary_judge")
        if isinstance(primary, bool) or not isinstance(primary, int):
            raise ValueError(f"{where}.primary_judge must be an integer")
        if primary not in {point.frame for point in judges}:
            raise ValueError(f"{where}.primary_judge must name one declared judge frame")
        temporal = row.get("temporal_evidence")
        if temporal not in TEMPORAL_EVIDENCE:
            raise ValueError(f"{where}.temporal_evidence must be one of {sorted(TEMPORAL_EVIDENCE)}")
        raw_claims = row.get("claims")
        if not isinstance(raw_claims, list) or not raw_claims:
            raise ValueError(f"{where}.claims must be a non-empty list")
        claims = tuple(Claim.parse(v, f"{where}.claims[{i}]") for i, v in enumerate(raw_claims))
        ids = [claim.id for claim in claims]
        if len(set(ids)) != len(ids):
            raise ValueError(f"{where}.claims contains duplicate ids")
        raw_composition = row.get("composition_context")
        composition = (
            CompositionContext.parse(raw_composition, f"{where}.composition_context")
            if raw_composition is not None
            else None
        )
        if composition:
            outside = sorted(set(composition.frames) - {point.frame for point in judges})
            if outside:
                raise ValueError(
                    f"{where}.composition_context frames are outside the judge set: {outside}"
                )
        return cls(primary, judges, temporal, claims, composition)


@dataclass(frozen=True)
class WorkUnit:
    id: str
    title: str
    plan: str
    depends_on: tuple[str, ...]
    mutates: MutationScope
    protects: ProtectionSpec
    evaluation: EvaluationPolicy
    completion: str

    @classmethod
    def parse(cls, value: Any, where: str) -> WorkUnit:
        row = _mapping(value, where)
        uid = _id(row.get("id"), f"{where}.id")
        unit = cls(
            uid,
            _text(row.get("title", uid), f"{where}.title"),
            _relative_path(row.get("plan"), f"{where}.plan"),
            _strings(row.get("depends_on", []), f"{where}.depends_on"),
            MutationScope.parse(row.get("mutates"), f"{where}.mutates"),
            ProtectionSpec.parse(row.get("protects"), f"{where}.protects"),
            EvaluationPolicy.parse(row.get("evaluation"), f"{where}.evaluation"),
            _text(row.get("completion"), f"{where}.completion"),
        )
        context = unit.evaluation.composition_context
        if context and context.source_unit and context.source_unit not in unit.depends_on:
            raise ValueError(
                f"{where}.evaluation.composition_context source_unit must be a declared dependency"
            )
        return unit


def validate_unit_dag(units: tuple[WorkUnit, ...], where: str) -> None:
    ids = [unit.id for unit in units]
    if len(set(ids)) != len(ids):
        raise ValueError(f"{where} contains duplicate unit ids")
    known = set(ids)
    for unit in units:
        missing = sorted(set(unit.depends_on) - known)
        if missing:
            raise ValueError(f"{where}.{unit.id} depends on unknown units: {', '.join(missing)}")
        if unit.id in unit.depends_on:
            raise ValueError(f"{where}.{unit.id} cannot depend on itself")

    visiting: set[str] = set()
    visited: set[str] = set()
    deps = {unit.id: unit.depends_on for unit in units}

    def visit(uid: str) -> None:
        if uid in visiting:
            raise ValueError(f"{where} contains a dependency cycle at {uid}")
        if uid in visited:
            return
        visiting.add(uid)
        for dep in deps[uid]:
            visit(dep)
        visiting.remove(uid)
        visited.add(uid)

    for uid in ids:
        visit(uid)


def ready_units(units: tuple[WorkUnit, ...], passed: set[str]) -> tuple[WorkUnit, ...]:
    """Return pending units whose declared dependency closure is currently accepted."""
    return tuple(unit for unit in units if unit.id not in passed and set(unit.depends_on) <= passed)


def read_document(path: str | Path) -> list[dict]:
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path.name} is invalid JSON: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("schema") != SCHEMA:
        raise ValueError(
            f"{path.name} must be an object with schema={SCHEMA}; schema 3 and legacy layer arrays are not supported"
        )
    rows = raw.get("layers")
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"{path.name}.layers must be a non-empty list")
    if not all(isinstance(row, dict) for row in rows):
        raise ValueError(f"{path.name}.layers entries must be objects")
    return rows
