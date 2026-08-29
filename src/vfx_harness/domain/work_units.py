"""Strict schema-4 layer, work-unit, claim, and evidence contracts.

The planner owns shot-specific decomposition.  This module owns only the generic shape,
validation, dependency semantics, claim authority, and protection resolution.  There is
deliberately no adapter for the former top-level array schema: execution authority must
not be guessed from an obsolete document.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
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
# The evidence domain a claim's proposition lives in. `image` and `human` are decided
# after a candidate exists, so they are build-time debts rather than materialization
# bindings; the rest must be covered by a bound metric of the same domain.
CLAIM_DOMAINS = {"scene", "image", "temporal", "projected_composition", "human"}
STRUCTURAL_CLAIM_DOMAINS = {"scene", "temporal", "projected_composition"}
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

    ``moments`` states WHICH of the claim's judged moments this binding settles. The
    vocabulary grew frame-scoped kinds after this contract was designed, so authors'
    real intent ("vis-f72 belongs to moment 72") was inexpressible and due-ness had to
    be inferred from the bound contract's frame (run 20260825: a [72, 150] claim was
    faulted at each frame for the other frame's row). Declared moments win; inference
    remains only a fallback for undeclared bindings.
    """

    kind: str
    id: str
    moments: tuple[int, ...] | None = None

    @classmethod
    def parse(cls, value: Any, where: str) -> EvidenceBinding:
        row = _mapping(value, where)
        kind = row.get("kind")
        if kind not in EVIDENCE_KINDS:
            raise ValueError(f"{where}.kind must be one of {sorted(EVIDENCE_KINDS)}")
        extra = sorted(set(row) - {"kind", "id", "moments"})
        if extra:
            raise ValueError(f"{where} has unknown fields: {', '.join(extra)}")
        moments = row.get("moments")
        if moments is not None:
            moments = _moments(moments, f"{where}.moments")
        return cls(kind, _id(row.get("id"), f"{where}.id"), moments)


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
    # The evidence domain this claim's proposition actually lives in. Declared, never
    # inferred from prose — and checked against the domains its bound metrics can
    # certify, so a temporal assertion cannot be closed by a static count.
    asserts: str | None = None

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
            # Walking this shape one missing-field error at a time cost run b603zc93p
            # its whole session (it guessed criteria/judge_frames). Name the complete
            # schema and the usual alternative in every failure.
            _QUALIFICATION_SHAPE = (
                f"{where}.qualification requires EXACTLY: suite, judge_model, prompt, "
                "evidence_shape, artifact (repo-relative rubric file), artifact_sha256 "
                "(64 lowercase hex of that file). This authority is rubric-file-backed "
                "acceptance machinery — for appearance judged at build time, the usual "
                "shape is authority: executable_required with asserts: image instead"
            )
            try:
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
            except ValueError as exc:
                raise ValueError(f"{exc}. {_QUALIFICATION_SHAPE}") from exc
            if not _SHA256.fullmatch(qualification["artifact_sha256"]):
                raise ValueError(
                    f"{where}.qualification.artifact_sha256 must be 64 lowercase hex "
                    f"characters. {_QUALIFICATION_SHAPE}"
                )
        elif qualification is not None:
            raise ValueError(f"{where}.qualification is only valid for qualified qualitative authority")

        repair_owner = _id(row.get("repair_owner"), f"{where}.repair_owner")
        evidence = _bindings(row.get("evidence"), f"{where}.evidence")
        for index, binding in enumerate(evidence):
            if binding.moments is not None:
                stray = sorted(set(binding.moments) - set(moments))
                if stray:
                    raise ValueError(
                        f"{where}.evidence[{index}].moments {sorted(binding.moments)} names "
                        f"moments outside this claim's judged set {sorted(moments)}: {stray}"
                    )
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

        asserts = row.get("asserts")
        if asserts is not None and asserts not in CLAIM_DOMAINS:
            raise ValueError(
                f"{where}.asserts must be one of {sorted(CLAIM_DOMAINS)} — the evidence "
                "domain this claim's proposition lives in"
            )

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
            asserts,
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
    # appearance-assignment authority over another layer's declared dressable roles
    # (material-slot writes only by convention; geometry protection stays with the
    # owner's contracts) — ADR-0007
    dresses: tuple[str, ...] = ()

    @classmethod
    def parse(cls, value: Any, where: str) -> MutationScope:
        row = _mapping(value, where)
        mode = row.get("mode", "scoped")
        if mode not in {"scoped", "none"}:
            raise ValueError(f"{where}.mode must be 'scoped' or 'none'")
        roles = _strings(row.get("roles", []), f"{where}.roles")
        controls = _strings(row.get("controls", []), f"{where}.controls")
        # Dressing: appearance-assignment authority over ANOTHER layer's declared
        # dressable roles. Lookdev's whole ontology is putting materials on geometry it
        # does not own; without a typed channel the composed judge frames stayed naked
        # proxies while every per-unit contract passed (run 20260825T133513Z-af3084,
        # composed 1.83/1.0 over sealed 5.0 units). ADR-0007.
        dresses = _strings(row.get("dresses", []), f"{where}.dresses")
        spans = tuple(_relative_path(v, f"{where}.script_spans") for v in row.get("script_spans", []))
        if len(set(spans)) != len(spans):
            raise ValueError(f"{where}.script_spans contains duplicates")
        if mode == "none" and any((roles, controls, spans, dresses)):
            raise ValueError(f"{where} mode 'none' cannot declare mutation targets")
        if mode == "scoped" and not any((roles, controls, spans, dresses)):
            raise ValueError(
                f"{where} scoped mutation needs roles, controls, script_spans, or dresses"
            )
        overlap = sorted(set(dresses) & set(roles))
        if overlap:
            raise ValueError(
                f"{where}.dresses overlaps mutation roles ({', '.join(overlap)}); a role "
                "the unit already owns needs no dressing declaration"
            )
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
        return cls(mode, roles, controls, spans, tuple(sorted(mapping)), dresses)


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


VIS_REPAIR_OWNER_RULE = (
    "a required claim that binds visible_fraction is repaired by a unit that can "
    "change the rays: provides:[\"camera\"] or mutates/dresses every `roles` "
    "selector on that row. A volume-only unit cannot bind mesh vis as required repair."
)


def plan_selector_declared(selector: str, declarations: Iterable[str]) -> bool:
    """Whether a contract selector is covered by a declared role/dress pattern."""
    token = str(selector)
    return any(
        token == declared
        or token.startswith(f"{declared}.")
        or fnmatch.fnmatchcase(token, declared)
        or fnmatch.fnmatchcase(declared, token)
        for declared in declarations
    )


def vis_roles_unrepairable_by(
    *,
    provides: Iterable[str],
    mutation_roles: Iterable[str],
    vis_roles: Iterable[str],
) -> tuple[str, ...]:
    """Vis roles the named owner cannot change (HIR-0051). Camera may observe any."""
    if "camera" in {str(item) for item in provides}:
        return ()
    declared = {str(item) for item in mutation_roles}
    return tuple(
        sorted(
            str(role)
            for role in vis_roles
            if str(role) and not plan_selector_declared(str(role), declared)
        )
    )


def layer_active_visible_fraction_ids(
    rows: Sequence[Mapping[str, Any]] | Iterable[Mapping[str, Any]],
    layer_id: str | int,
    frame: int | None = None,
) -> tuple[str, ...]:
    """Lifecycle-active `visible_fraction` ids on this layer (optionally one frame)."""
    from vfx_harness.domain.contracts import active_for

    return tuple(
        sorted(
            {
                str(row["id"])
                for row in rows
                if isinstance(row, Mapping)
                and str(row.get("kind") or "") == "visible_fraction"
                and row.get("id")
                and active_for(dict(row), layer_id, frame)
            }
        )
    )


def geometry_vis_protection_ids(
    provides: Iterable[str],
    layer_active_vis_ids: Iterable[str],
) -> tuple[str, ...]:
    """Geometry units freeze-protect active-layer vis, including sibling rows."""
    if "geometry" not in {str(item) for item in provides}:
        return ()
    return tuple(sorted({str(item) for item in layer_active_vis_ids if str(item)}))


GEOMETRY_VIS_DEPENDENCY_RULE = (
    "a unit that provides geometry freeze-protects every lifecycle-active visible_fraction "
    "row on its layer. If one of those roles is produced by another same-layer unit, that "
    "producer must be in the geometry unit's dependency closure; a future producer makes "
    "the earlier geometry unit impossible to seal. Remove geometry from the earlier unit, "
    "use already-existing dressable geometry, or reorder/split the DAG."
)


@dataclass(frozen=True, slots=True)
class GeometryVisDependencyGap:
    unit_id: str
    contract_id: str
    role: str
    producer_ids: tuple[str, ...]


def geometry_vis_dependency_gaps(
    units: Sequence[WorkUnit],
    rows: Sequence[Mapping[str, Any]] | Iterable[Mapping[str, Any]],
    layer_id: str | int,
) -> tuple[GeometryVisDependencyGap, ...]:
    """Find geometry units that would protect visibility owned by a future sibling.

    HIR-0051 intentionally makes geometry preservation conservative. That protection
    becomes an unsealable cycle when an active visibility row selects geometry which a
    later same-layer unit is responsible for creating. Catch the cycle at authority
    publication instead of making the builder discover it from a missing role.
    """
    unit_rows = tuple(units)
    by_id = {unit.id: unit for unit in unit_rows}
    row_by_id = {
        str(row.get("id")): row
        for row in rows
        if isinstance(row, Mapping) and row.get("id")
    }
    active_ids = layer_active_visible_fraction_ids(row_by_id.values(), layer_id)

    def dependency_closure(unit: WorkUnit) -> set[str]:
        found: set[str] = set()
        frontier = list(unit.depends_on)
        while frontier:
            current = frontier.pop()
            if current in found:
                continue
            found.add(current)
            dependency = by_id.get(current)
            if dependency is not None:
                frontier.extend(dependency.depends_on)
        return found

    gaps: list[GeometryVisDependencyGap] = []
    for unit in unit_rows:
        if "geometry" not in unit.provides:
            continue
        dependencies = dependency_closure(unit)
        own_roles = (*unit.mutates.roles, *unit.mutates.dresses)
        for contract_id in active_ids:
            row = row_by_id[contract_id]
            unresolved = vis_roles_unrepairable_by(
                provides=(),
                mutation_roles=own_roles,
                vis_roles=row.get("roles") or (),
            )
            for role in unresolved:
                producers = tuple(
                    sorted(
                        other.id
                        for other in unit_rows
                        if other.id != unit.id
                        and plan_selector_declared(
                            role, (*other.mutates.roles, *other.mutates.dresses)
                        )
                    )
                )
                if producers and not any(producer in dependencies for producer in producers):
                    gaps.append(
                        GeometryVisDependencyGap(
                            unit.id, contract_id, role, producers
                        )
                    )
    return tuple(gaps)


# Judge lists are structural. Scene contracts may still measure other frames; the
# legal binding is contract ids, not extra frames on the judge lists (HIR-0029).
EXTRA_FRAME_BINDING_RULE = (
    "composition_context.frames and claim.moments must be a subset of this unit's "
    "judge frames, which must themselves be a subset of the layer judge list. "
    "The layer judge list is structural and cannot change in materialization. "
    "Scene contracts may still measure other frames; bind those contract ids "
    "through composition_context.contract_ids or a required claim whose moments "
    "stay in the unit judge — do not put the extra frames on composition_context.frames, "
    "claim.moments, unit evaluation.judge, or layer.judge."
)

UNIT_JUDGE_CLAIM_COVERAGE_RULE = (
    "every unit evaluation.judge frame must appear in at least one required "
    "claim.moments. A judge frame with no required claim is a contract_gap, "
    "not a critic look vote. Extra-frame scene contracts still bind through "
    "composition_context.contract_ids; do not add those frames to the unit judge."
)

LOOK_REQUIRES_IMAGE_DOMAIN_RULE = (
    "a unit that declares look_capabilities must cover every evaluation.judge "
    "frame with a required claim that asserts image and binds image-domain "
    "evidence (image_contract, qualification, or human_decision). Scene counts "
    "cannot certify appearance. That hole is a contract_gap, not a 5.0 "
    "executable seal and not a critic look vote."
)

LOOK_IMAGE_EVIDENCE_KINDS = frozenset(
    {"image_contract", "qualification", "human_decision"}
)


def _evidence_kind(item: Any) -> str:
    kind = getattr(item, "kind", None)
    if kind:
        return str(kind)
    if isinstance(item, dict):
        return str(item.get("kind") or "")
    return ""


def required_claim_certifies_look(claim: Any) -> bool:
    """True when a required claim can certify appearance (HIR-0046)."""
    if not getattr(claim, "required", False):
        return False
    if getattr(claim, "asserts", None) != "image":
        return False
    kinds = {_evidence_kind(item) for item in (getattr(claim, "evidence", ()) or ())}
    return bool(kinds & LOOK_IMAGE_EVIDENCE_KINDS)


def unearned_look_judge_frames(unit: Any) -> tuple[int, ...]:
    """Look-owning judge frames with no image-domain required claim (HIR-0046)."""
    if not tuple(getattr(unit, "look_capabilities", ()) or ()):
        return ()
    evaluation = getattr(unit, "evaluation", None)
    judged = {
        int(point.frame)
        for point in (getattr(evaluation, "judges", ()) or ())
    }
    certified = {
        int(moment)
        for claim in (getattr(evaluation, "claims", ()) or ())
        if required_claim_certifies_look(claim)
        for moment in (getattr(claim, "moments", ()) or ())
    }
    return tuple(sorted(judged - certified))


def uncovered_unit_judge_frames(unit: Any) -> tuple[int, ...]:
    """Judge frames that no required claim covers (HIR-0045)."""
    evaluation = getattr(unit, "evaluation", None)
    judges = tuple(getattr(evaluation, "judges", ()) or ())
    claims = tuple(getattr(evaluation, "claims", ()) or ())
    judged = {int(point.frame) for point in judges}
    claimed = {
        int(moment)
        for claim in claims
        if getattr(claim, "required", False)
        for moment in getattr(claim, "moments", ()) or ()
    }
    return tuple(sorted(judged - claimed))


def layer_judge_frames(global_row: dict[str, Any]) -> tuple[int, ...]:
    """Extract declared judge frames from a global layer row, preserving order."""
    frames: list[int] = []
    seen: set[int] = set()
    for point in global_row.get("judge") or []:
        if not isinstance(point, dict):
            continue
        frame = point.get("frame")
        if isinstance(frame, bool) or not isinstance(frame, int) or frame in seen:
            continue
        seen.add(frame)
        frames.append(frame)
    return tuple(frames)


def compile_frame_authority(global_row: dict[str, Any]) -> dict[str, Any]:
    """Compile the frame-subset card a materialization session must not rediscover."""
    return {
        "layer_judge_frames": list(layer_judge_frames(global_row)),
        "unit_evaluation_judge": (
            "subset of layer_judge_frames; copy those frames, do not add"
        ),
        "claim_moments": "subset of that unit's judge frames",
        "composition_context.frames": "subset of that unit's judge frames",
        "extra_frame_scene_contracts": (
            "Scene contracts may declare frames outside layer_judge_frames. "
            "Bind those ids through composition_context.contract_ids while keeping "
            "composition_context.frames on the unit judge, or through a required claim "
            "whose moments stay in the unit judge. Do not add those frames to "
            "layer.judge, unit evaluation.judge, composition_context.frames, or "
            "claim.moments."
        ),
        "required_claim_coverage": UNIT_JUDGE_CLAIM_COVERAGE_RULE,
        "look_image_domain": LOOK_REQUIRES_IMAGE_DOMAIN_RULE,
    }


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
                    f"{where}.composition_context frames are outside the judge set: "
                    f"{outside}. {EXTRA_FRAME_BINDING_RULE}"
                )
        return cls(primary, judges, temporal, claims, composition)


# Scene capabilities a unit makes available to its dependents, and to the rules that
# judge it. `geometry` means "objects under my roles carry polygons", which is what a
# mesh metric needs and cannot otherwise learn: smooth_fraction over a camera rig reads
# None forever, and run 20260824T060927Z burned two repair rounds on that.
# Declared, never spelled:
# the composition-bootstrap rule detected camera ownership by substring-matching
# "camera" in mutated role names, so `cam_rig` — the harness's own default camera-rig
# role, and the role the approved camera decision keys against — was invisible, and a
# dependent could not be projected through a camera that demonstrably existed.
UNIT_PROVIDES = {"camera", "geometry"}
# Capabilities whose availability changes what a later layer may legally plan. Geometry
# is exported through typed successor interfaces; the active camera is shot-wide
# bootstrap state and therefore belongs in the sparse global DAG.
GLOBAL_SCENE_CAPABILITIES = {"camera"}

# Authored cluster labels. Publication derives write-clusters; these fields are
# HIR-0017 padding and are unrepresentable on a WorkUnit (HIR-0083).
ATOMICITY_PADDING_FIELDS = frozenset(
    {"family", "mutation_family", "coherent_family", "primary_subject"}
)

CONSUME_INTERFACE_RULE = (
    "a successor declares each consumed interface by producer unit id, interface id, "
    "and kind. Ready-set and publication require that digest-matched producer to "
    "publish that exact id and kind. depends_on without consumes is a legal status "
    "edge: it orders accepted work but grants no producer interface (HIR-0084)."
)
CONSUMED_ROLE_MUTATION_RULE = (
    "consumed publish interfaces are read-only inputs. Assembly mutates its own "
    "instances, relationships, and controls; it may not list a producer export role "
    "in mutates.roles. Adding a dependency cannot hide a mixed write-cluster or "
    "grant mutation authority over an accepted producer (HIR-0083)."
)


@dataclass(frozen=True)
class PublishSpec:
    """Authored successor interface. Identity participates in unit_digest (HIR-0084)."""

    id: str
    kind: str
    exports: tuple[tuple[str, str], ...]

    def as_authoring_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "exports": dict(self.exports),
        }


@dataclass(frozen=True)
class ConsumeSpec:
    """Declared consumption of a producer interface (HIR-0084)."""

    producer: str
    interface_id: str
    kind: str


def _parse_publish_specs(
    value: Any,
    where: str,
    *,
    legal_tokens: Iterable[str],
) -> tuple[PublishSpec, ...]:
    if value in (None, []):
        return ()
    if not isinstance(value, list) or not value:
        raise ValueError(f"{where} must be a non-empty list when declared")
    from vfx_harness.domain.publish_interfaces import parse_publish_interface

    specs: list[PublishSpec] = []
    seen: set[str] = set()
    for index, row in enumerate(value):
        payload = dict(_mapping(row, f"{where}[{index}]"))
        payload.pop("producer", None)
        payload.pop("digest", None)
        parsed = parse_publish_interface(
            payload,
            f"{where}[{index}]",
            legal_tokens=legal_tokens,
            layer_id="pending",
            unit_id="pending",
        )
        if parsed.id in seen:
            raise ValueError(f"{where}[{index}].id {parsed.id!r} is duplicated")
        seen.add(parsed.id)
        specs.append(PublishSpec(parsed.id, parsed.kind, tuple(sorted(parsed.exports))))
    return tuple(specs)


def _parse_consume_specs(
    value: Any,
    where: str,
    *,
    depends_on: Iterable[str],
) -> tuple[ConsumeSpec, ...]:
    if value in (None, []):
        return ()
    if not isinstance(value, list) or not value:
        raise ValueError(f"{where} must be a non-empty list when declared")
    from vfx_harness.domain.publish_interfaces import PUBLISH_INTERFACE_KINDS, UNKNOWN_KIND_RULE

    deps = {str(item) for item in depends_on}
    specs: list[ConsumeSpec] = []
    seen: set[tuple[str, str, str]] = set()
    for index, row in enumerate(value):
        mapping = _mapping(row, f"{where}[{index}]")
        extra = sorted(set(mapping) - {"producer", "interface_id", "kind"})
        if extra:
            raise ValueError(f"{where}[{index}] has unknown fields: {', '.join(extra)}")
        producer = _id(mapping.get("producer"), f"{where}[{index}].producer")
        if producer not in deps:
            raise ValueError(
                f"{where}[{index}].producer {producer!r} is not a declared dependency. "
                + CONSUME_INTERFACE_RULE
            )
        interface_id = _id(mapping.get("interface_id"), f"{where}[{index}].interface_id")
        kind = _text(mapping.get("kind"), f"{where}[{index}].kind")
        if kind not in PUBLISH_INTERFACE_KINDS:
            raise ValueError(f"{where}[{index}].kind {kind!r} is unknown. {UNKNOWN_KIND_RULE}")
        key = (producer, interface_id, kind)
        if key in seen:
            raise ValueError(f"{where}[{index}] duplicates {interface_id!r} from {producer}")
        seen.add(key)
        specs.append(ConsumeSpec(producer, interface_id, kind))
    return tuple(specs)


def parse_provides(value: Any, where: str) -> tuple[str, ...]:
    names = _strings(value, where) if value else ()
    unknown = sorted(set(names) - UNIT_PROVIDES)
    if unknown:
        raise ValueError(
            f"{where} has unknown capability {', '.join(unknown)}; declare a subset of "
            + ", ".join(sorted(UNIT_PROVIDES))
        )
    return tuple(dict.fromkeys(names))


# Which image-feedback families a unit is answerable for. Declared by the unit that
# owns the work, never inferred: run 20260823T154920Z scanned axis IDENTIFIERS for look
# words, so `iris_seal_readability` — whose description demanded layered machined metal,
# seams and fasteners — was classified as owning no appearance, and the builder was told
# surface quality was out of scope while its unit plan said the opposite.
LOOK_CAPABILITIES: dict[str, tuple[str, ...]] = {
    "detail": ("detail",),
    "material": ("detail", "color"),
    "color": ("color",),
    "exposure": ("exposure", "detail"),
    "lighting": ("exposure", "detail", "emitters", "color"),
    "emission": ("exposure", "emitters"),
    "atmosphere": ("exposure", "detail", "emitters"),
    "motion": ("motion",),
    "grade": ("exposure", "detail", "emitters", "halation", "color", "motion"),
}

PROJECTED_ORIGIN_REPAIR_RULE = (
    "projected_origin_x/y is camera-alignment evidence: its required claim's "
    "repair_owner provides camera. A fixed Empty/control producer proves world state "
    "with scene evidence; the downstream camera owner depends on that producer and "
    "owns projection through the camera. Do not fit the target after the camera."
)


def parse_look_capabilities(value: Any, where: str) -> tuple[str, ...]:
    names = _strings(value, where) if value else ()
    unknown = sorted(set(names) - set(LOOK_CAPABILITIES))
    if unknown:
        raise ValueError(
            f"{where} has unknown capability {', '.join(unknown)}; declare a subset of "
            + ", ".join(sorted(LOOK_CAPABILITIES))
        )
    return tuple(dict.fromkeys(names))


def unit_requires_surface_visibility(unit: WorkUnit) -> bool:
    """Whether a unit owns or consumes a rendered subject at its judge frames.

    Camera rigs, lights, volumes, and Empty-style control hosts are scene interfaces,
    not rendered subjects. Forcing visible_fraction on them makes a builder invent mesh
    solely to pay evidence debt. Geometry, dressing, look work, and consumed asset/
    instance sources do own rendered surfaces and keep the occlusion-true requirement.
    """
    return bool(
        "geometry" in unit.provides
        or unit.mutates.dresses
        or unit.look_capabilities
        or any(claim.required and claim.asserts == "image" for claim in unit.evaluation.claims)
        or any(item.kind in {"asset_source", "instance_source"} for item in unit.consumes)
    )


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
    look_capabilities: tuple[str, ...] = ()
    provides: tuple[str, ...] = ()
    publishes: tuple[PublishSpec, ...] = ()
    consumes: tuple[ConsumeSpec, ...] = ()

    @classmethod
    def parse(cls, value: Any, where: str) -> WorkUnit:
        row = _mapping(value, where)
        uid = _id(row.get("id"), f"{where}.id")
        depends_on = _strings(row.get("depends_on", []), f"{where}.depends_on")
        mutates = MutationScope.parse(row.get("mutates"), f"{where}.mutates")
        evaluation = EvaluationPolicy.parse(row.get("evaluation"), f"{where}.evaluation")
        draft = cls(
            uid,
            _text(row.get("title", uid), f"{where}.title"),
            _relative_path(row.get("plan"), f"{where}.plan"),
            depends_on,
            mutates,
            ProtectionSpec.parse(row.get("protects"), f"{where}.protects"),
            evaluation,
            _text(row.get("completion"), f"{where}.completion"),
            parse_look_capabilities(
                row.get("look_capabilities", []), f"{where}.look_capabilities"
            ),
            parse_provides(row.get("provides", []), f"{where}.provides"),
        )
        legal = {
            *draft.mutates.roles,
            *draft.mutates.dresses,
            *draft.mutates.controls,
            *bound_claim_contract_ids(draft),
        }
        unit = cls(
            draft.id,
            draft.title,
            draft.plan,
            draft.depends_on,
            draft.mutates,
            draft.protects,
            draft.evaluation,
            draft.completion,
            draft.look_capabilities,
            draft.provides,
            _parse_publish_specs(row.get("publishes"), f"{where}.publishes", legal_tokens=legal),
            _parse_consume_specs(
                row.get("consumes"), f"{where}.consumes", depends_on=draft.depends_on
            ),
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


def bound_claim_contract_ids(unit: WorkUnit) -> tuple[str, ...]:
    """Claim and composition contract ids this unit is answerable for."""
    ids: list[str] = []
    seen: set[str] = set()

    def add(cid: str) -> None:
        token = str(cid)
        if token and token not in seen:
            seen.add(token)
            ids.append(token)

    for claim in unit.evaluation.claims:
        for binding in claim.evidence:
            if binding.kind in {"scene_contract", "image_contract"}:
                add(binding.id)
    context = unit.evaluation.composition_context
    if context:
        for cid in context.contract_ids:
            add(cid)
    return tuple(ids)


def offered_interface_keys(unit: WorkUnit) -> tuple[tuple[str, str], ...]:
    """Interface id/kind pairs this unit currently publishes.

    Authored ``publishes`` is the exact set. When omitted, the derived
    ``{unit.id}.publish`` row is the only offered interface.
    """
    if unit.publishes:
        return tuple((spec.id, spec.kind) for spec in unit.publishes)
    from vfx_harness.domain.publish_interfaces import derived_interface_key

    derived = derived_interface_key(unit)
    return (derived,) if derived is not None else ()


def consumption_is_satisfied(
    unit: WorkUnit,
    units: Sequence[WorkUnit],
    sealed_producers: set[str],
) -> bool:
    """True when every declared consume matches a sealed producer's offered interface."""
    if not unit.consumes:
        return True
    by_id = {item.id: item for item in units}
    for consume in unit.consumes:
        if consume.producer not in sealed_producers:
            return False
        producer = by_id.get(consume.producer)
        if producer is None:
            return False
        if (consume.interface_id, consume.kind) not in offered_interface_keys(producer):
            return False
    return True


def ready_units(
    units: tuple[WorkUnit, ...],
    passed: set[str],
    *,
    sealed_producers: set[str] | None = None,
) -> tuple[WorkUnit, ...]:
    """Return pending units whose declared dependency closure is currently accepted.

    ``sealed_producers`` is the digest-matched passed set. When omitted, readiness
    is the declared ``depends_on`` subset of ``passed``. A successor is ready only
    when every ``depends_on`` producer is sealed **and** each consumed interface
    id/kind is actually offered by that producer (HIR-0084).
    """
    producers = passed if sealed_producers is None else sealed_producers
    return tuple(
        unit
        for unit in units
        if unit.id not in passed
        and set(unit.depends_on) <= producers
        and consumption_is_satisfied(unit, units, producers)
    )


def read_document(path: str | Path) -> list[dict]:
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path.name} is invalid JSON: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("schema") not in {SCHEMA, 5}:
        raise ValueError(
            f"{path.name} must be an object with schema={SCHEMA} or schema=5; "
            "schema 3 and legacy layer arrays are not supported"
        )
    rows = raw.get("layers")
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"{path.name}.layers must be a non-empty list")
    if not all(isinstance(row, dict) for row in rows):
        raise ValueError(f"{path.name}.layers entries must be objects")
    return rows
