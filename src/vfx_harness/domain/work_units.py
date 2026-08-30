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
# One evidence-domain vocabulary for layers, deferred_owner rows, and claims
# (ADR-0003). `image` and `human` are decided after a candidate exists, so they
# are build-time debts rather than materialization bindings; the rest must be
# covered by a bound metric of the same domain.
EVIDENCE_DOMAINS = frozenset(
    {"scene", "image", "temporal", "projected_composition", "human"}
)
CLAIM_DOMAINS = EVIDENCE_DOMAINS
STRUCTURAL_CLAIM_DOMAINS = frozenset({"scene", "temporal", "projected_composition"})
REQUIREMENT_DOMAIN_COVERAGE_FIX = (
    "assign an owner whose evidence_domains cover every declared domain, or split "
    "the row so each fragment's domains match one owner; do not infer domains from "
    "brief keywords and do not stamp a domain onto a layer that does not own it"
)
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


def parse_evidence_domains(value: Any, where: str) -> tuple[str, ...]:
    """Parse a non-empty unique subset of ``EVIDENCE_DOMAINS``, sorted for teaching."""
    accepted = sorted(EVIDENCE_DOMAINS)
    try:
        tokens = _strings(value, where, allow_empty=False)
    except ValueError as exc:
        raise ValueError(f"{exc}; accepted {accepted}") from None
    unknown = sorted(set(tokens) - EVIDENCE_DOMAINS)
    if unknown:
        raise ValueError(
            f"{where} unknown evidence domains: {', '.join(unknown)}; "
            f"accepted {accepted}"
        )
    return tuple(sorted(tokens))


def uncovered_evidence_domains(
    declared: Sequence[str], owner_domains: Sequence[str]
) -> tuple[str, ...]:
    return tuple(sorted(frozenset(declared) - frozenset(owner_domains)))


def layers_covering_evidence_domains(
    declared: Sequence[str],
    layer_domains: Mapping[str, Sequence[str]],
) -> tuple[str, ...]:
    """Return layer ids whose declared domains cover every required domain (AND)."""
    needed = frozenset(declared)
    if not needed:
        return ()
    return tuple(
        layer_id
        for layer_id, domains in layer_domains.items()
        if needed <= frozenset(domains)
    )


def requirement_domain_coverage_what(
    requirement_id: str,
    declared: Sequence[str],
    owner_layer: str,
    owner_domains: Sequence[str],
    covering_layers: Sequence[str],
) -> str:
    missing = uncovered_evidence_domains(declared, owner_domains)
    declared_s = ", ".join(declared) if declared else "none"
    owner_s = ", ".join(sorted(owner_domains)) if owner_domains else "none"
    covering_s = ", ".join(covering_layers) if covering_layers else "none"
    missing_s = ", ".join(missing) if missing else "none"
    return (
        f"requirement {requirement_id} declares evidence domains {declared_s}; "
        f"owner layer {owner_layer} covers {owner_s} (missing {missing_s}). "
        f"Layers whose domains cover every declared domain: {covering_s}"
    )


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


def canonical_unit_script_path(layer_id: str, unit_id: str) -> str:
    """Return the sole replay artifact path for one unit.

    Unit scripts are executable files, not addressable spans inside a composed layer
    script.  Keeping the path derivable from typed layer/unit identity prevents a
    planner from publishing fragment notation that the filesystem later interprets as
    a literal filename.
    """
    layer = _id(str(layer_id), "layer id")
    unit = _id(str(unit_id), "unit id")
    directory = layer.zfill(2) if layer.isdigit() else layer
    return f"build/units/{directory}/{unit}.py"


def validate_unit_script_path(layer_id: str, unit: WorkUnit, where: str) -> None:
    """Fail closed unless a unit owns its exact identity-derived Python artifact."""
    expected = canonical_unit_script_path(layer_id, unit.id)
    actual = list(unit.mutates.script_spans)
    if actual != [expected]:
        raise ValueError(
            f"{where}.mutates.script_spans must be exactly [{expected!r}]; got {actual}. "
            "A work unit owns one distinct replayable Python file; composed layer paths, "
            "#fragment notation, and alternate basenames are not script authority."
        )


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
            incomplete: list[str] = []
            if not isinstance(coordination_owner, str) or not coordination_owner.strip():
                incomplete.append("coordination_owner must be a same-layer work-unit id")
            if len(participants) < 2:
                incomplete.append(
                    "participants needs at least two same-layer work-unit ids"
                )
            if not controls:
                incomplete.append("controls must bound interaction balancing")
            if incomplete:
                raise ValueError(
                    f"{where} interaction claim requires the complete coordination shape: "
                    + "; ".join(incomplete)
                    + ". Participants are unit ids, not semantic roles or controls"
                )
            coordination_owner = _id(coordination_owner, f"{where}.coordination_owner")
        elif coordination_owner is not None or participants or controls:
            raise ValueError(
                f"{where} atomic claims cannot declare interaction coordination fields; "
                "omit coordination_owner, participants, and controls"
            )

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
    """Whether selector and declared role namespaces can address the same tag."""
    from vfx_harness.domain.semantic_roles import match_semantic

    token = str(selector)
    return any(
        match_semantic(token, (str(declared),))
        or match_semantic(str(declared), (token,))
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


def _dependency_closure(unit: WorkUnit, by_id: Mapping[str, WorkUnit]) -> set[str]:
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


def visible_fraction_repair_owners(
    units: Sequence[WorkUnit],
) -> dict[str, tuple[str, ...]]:
    """Required-claim repair owners that make each vis row due (HIR-0132)."""
    owners: dict[str, set[str]] = {}
    for unit in units:
        for claim in unit.evaluation.claims:
            if not claim.required:
                continue
            for binding in claim.evidence:
                if binding.kind != "scene_contract":
                    continue
                owners.setdefault(str(binding.id), set()).add(str(claim.repair_owner))
    return {contract_id: tuple(sorted(values)) for contract_id, values in owners.items()}


def geometry_vis_protection_ids_for_unit(
    units: Sequence[WorkUnit],
    unit: WorkUnit,
    rows: Sequence[Mapping[str, Any]] | Iterable[Mapping[str, Any]],
    layer_id: str | int,
    *,
    frame: int | None = None,
) -> tuple[str, ...]:
    """Vis rows due at this geometry unit after typed unit activation.

    A row bound to a required claim becomes due at that claim's repair owner.  It is
    then protected by every downstream geometry unit whose dependency closure contains
    that owner.  Rows without typed ownership retain HIR-0051's conservative layer-wide
    behavior instead of being silently dropped.
    """
    if "geometry" not in unit.provides:
        return ()
    unit_rows = tuple(units)
    by_id = {item.id: item for item in unit_rows}
    closure = _dependency_closure(unit, by_id) | {unit.id}
    owners = visible_fraction_repair_owners(unit_rows)
    active_ids = layer_active_visible_fraction_ids(rows, layer_id, frame=frame)
    return tuple(
        contract_id
        for contract_id in active_ids
        if not owners.get(contract_id) or set(owners[contract_id]).intersection(closure)
    )


GEOMETRY_VIS_DEPENDENCY_RULE = (
    "a required visible_fraction row becomes due at its typed repair-owner unit. Every "
    "later geometry unit freeze-protects that row and must include the owner in its "
    "dependency closure; a geometry unit before the owner does not pretend future surfaces "
    "already exist. Order the units with a real acyclic dependency, use already-existing "
    "owner-granted dressable geometry, or split the DAG. Rows without typed ownership "
    "remain conservatively layer-active."
)


GEOMETRY_VIS_CYCLE_RULE = (
    "mutually protecting geometry units cannot be made sealable by reordering, "
    "narrowing protects, or changing visibility lifecycle: every geometry provider "
    "freezes every lifecycle-active visible_fraction row on its layer. Retire the "
    "involved unpublished units in reverse dependency order, then either author all "
    "judged surfaces in one geometry unit whose roles truthfully form one derived "
    "write-cluster, or use already-existing owner-granted dressable geometry so only "
    "one unit provides geometry. Do not remove a real geometry capability, weaken "
    "visibility, or add cyclic depends_on edges."
)


@dataclass(frozen=True, slots=True)
class GeometryVisDependencyGap:
    unit_id: str
    contract_id: str
    role: str
    producer_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GeometryVisDependencyCycle:
    unit_ids: tuple[str, ...]
    contract_ids: tuple[str, ...]
    roles: tuple[str, ...]
    edges: tuple[tuple[str, str], ...]


def geometry_vis_dependency_gaps(
    units: Sequence[WorkUnit],
    rows: Sequence[Mapping[str, Any]] | Iterable[Mapping[str, Any]],
    layer_id: str | int,
) -> tuple[GeometryVisDependencyGap, ...]:
    """Find geometry/visibility order that lacks an authority edge.

    Required vis activates at its typed repair owner (HIR-0132). A later geometry unit
    must depend on that owner before it can protect the row; an earlier unit does not owe
    the future subject. Rows without typed ownership retain HIR-0051's conservative
    layer-wide producer check.
    """
    unit_rows = tuple(units)
    by_id = {unit.id: unit for unit in unit_rows}
    row_by_id = {
        str(row.get("id")): row
        for row in rows
        if isinstance(row, Mapping) and row.get("id")
    }
    active_ids = layer_active_visible_fraction_ids(row_by_id.values(), layer_id)

    closures = {unit.id: _dependency_closure(unit, by_id) for unit in unit_rows}
    order = {unit.id: index for index, unit in enumerate(unit_rows)}
    owners_by_contract = visible_fraction_repair_owners(unit_rows)

    gaps: list[GeometryVisDependencyGap] = []
    for unit in unit_rows:
        if "geometry" not in unit.provides:
            continue
        dependencies = closures[unit.id]
        own_roles = (*unit.mutates.roles, *unit.mutates.dresses)
        for contract_id in active_ids:
            row = row_by_id[contract_id]
            typed_owners = tuple(
                owner
                for owner in owners_by_contract.get(contract_id, ())
                if owner in by_id
            )
            if typed_owners:
                # A required vis row activates at its repair owner. For two unordered
                # geometry units, authored position is only a deterministic tie-break;
                # require the later row to publish the missing authority edge. This
                # produces one acyclic teaching finding instead of a false mutual cycle.
                earlier_unordered = tuple(
                    owner
                    for owner in typed_owners
                    if owner != unit.id
                    and owner not in dependencies
                    and unit.id not in closures[owner]
                    and order[owner] < order[unit.id]
                )
                if not earlier_unordered:
                    continue
            else:
                earlier_unordered = ()
            unresolved = vis_roles_unrepairable_by(
                provides=(),
                mutation_roles=own_roles,
                vis_roles=row.get("roles") or (),
            )
            for role in unresolved:
                producers = earlier_unordered or tuple(
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


def geometry_vis_dependency_cycles(
    units: Sequence[WorkUnit],
    rows: Sequence[Mapping[str, Any]] | Iterable[Mapping[str, Any]],
    layer_id: str | int,
) -> tuple[GeometryVisDependencyCycle, ...]:
    """Compile mutually unsealable geometry-protection gaps into cycle cards."""
    gaps = geometry_vis_dependency_gaps(units, rows, layer_id)
    graph: dict[str, set[str]] = {unit.id: set() for unit in units}
    for gap in gaps:
        graph.setdefault(gap.unit_id, set()).update(gap.producer_ids)

    index = 0
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: list[tuple[str, ...]] = []

    def visit(node: str) -> None:
        nonlocal index
        indices[node] = index
        lowlinks[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        for successor in sorted(graph.get(node, ())):
            if successor not in indices:
                visit(successor)
                lowlinks[node] = min(lowlinks[node], lowlinks[successor])
            elif successor in on_stack:
                lowlinks[node] = min(lowlinks[node], indices[successor])
        if lowlinks[node] != indices[node]:
            return
        component: list[str] = []
        while stack:
            member = stack.pop()
            on_stack.remove(member)
            component.append(member)
            if member == node:
                break
        if len(component) > 1:
            components.append(tuple(sorted(component)))

    for unit_id in sorted(graph):
        if unit_id not in indices:
            visit(unit_id)

    cycles: list[GeometryVisDependencyCycle] = []
    for component in sorted(components):
        members = set(component)
        relevant = tuple(
            gap
            for gap in gaps
            if gap.unit_id in members and any(pid in members for pid in gap.producer_ids)
        )
        edges = tuple(
            sorted(
                {
                    (gap.unit_id, producer)
                    for gap in relevant
                    for producer in gap.producer_ids
                    if producer in members
                }
            )
        )
        cycles.append(
            GeometryVisDependencyCycle(
                unit_ids=component,
                contract_ids=tuple(sorted({gap.contract_id for gap in relevant})),
                roles=tuple(sorted({gap.role for gap in relevant})),
                edges=edges,
            )
        )
    return tuple(cycles)


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
# Camera availability is only this typed declaration. Role names, including
# `camera.target` and `cam_rig`, never imply a capability (HIR-0098). Composition-bootstrap
# and later-layer propagation use the same set; a name heuristic is not a fallback.
UNIT_PROVIDES = {"camera", "geometry"}
# Capabilities whose availability changes what a later layer may legally plan. Geometry
# is exported through typed successor interfaces; the active camera is shot-wide
# bootstrap state and therefore belongs in the sparse global DAG.
GLOBAL_SCENE_CAPABILITIES = {"camera"}

CAMERA_LAYER_DEFERS_SUBJECT_FORM_RULE = (
    "a sparse layer that globally provides camera may stage camera/control units only; "
    "it must not add a unit with provides:[\"geometry\"] to manufacture framing. "
    "Author persistent bbox_* contracts over rendered-subject roles owned by the "
    "earliest downstream form layer, keep owner_layer and fault_owner on the camera "
    "layer, set activates_at to the compiled earliest_geometry_layer, and bind those ids "
    "through the camera unit's composition_context"
)
DEFERRED_SUBJECT_ACTIVATION_RULE = (
    "a camera layer that authors persistent bbox_* for a subject that does not exist "
    "yet must set activates_at to the compiled earliest_geometry_layer from the selected "
    "DAG. That occupancy is not a client question; do not ask_supervisor for it"
)
DEFERRED_SUBJECT_BBOX_KINDS = frozenset({
    "bbox_width",
    "bbox_height",
    "bbox_center_x",
    "bbox_center_y",
    "bbox_top_y",
    "bbox_bottom_y",
})
DEFERRED_CONTRACT_CONTEXT_RULE = (
    "a scene contract whose activates_at layer is later than its owner_layer is "
    "inactive at the owner's unit boundary and cannot be required claim evidence. "
    "Keep the contract out of claim.evidence and bind its id through "
    "evaluation.composition_context.contract_ids; the later mutator pays and "
    "freeze-protects it when it becomes active"
)
DEFERRED_CONTRACT_FRAME_AUTHORITY_RULE = (
    "a future-active scene contract keeps the authoring owner layer's judge-frame "
    "authority. The activation layer evaluates it as extra-frame evidence; do not add "
    "the owner's reference moments to the activation layer's judge list"
)


def allowed_unit_provides(global_layer_row: Mapping[str, Any]) -> frozenset[str]:
    """Compile unit capabilities from immutable sparse layer authority.

    Camera availability changes the global DAG, so a camera-providing sparse layer is
    the camera bootstrap boundary rather than a place to manufacture rendered subject
    form. Other layers may provide local geometry but cannot invent global camera
    authority (HIR-0086, HIR-0128).
    """
    jit = global_layer_row.get("jit")
    raw_global = jit.get("provides") if isinstance(jit, Mapping) else {}
    global_capabilities = {
        str(value) for value in raw_global
    } if isinstance(raw_global, Mapping) else set()
    if "camera" in global_capabilities:
        return frozenset({"camera"})
    return frozenset(UNIT_PROVIDES - GLOBAL_SCENE_CAPABILITIES)


def _sparse_depends_on(row: Mapping[str, Any]) -> tuple[str, ...]:
    jit = row.get("jit") if isinstance(row.get("jit"), Mapping) else {}
    raw = jit.get("depends_on_layers") if isinstance(jit, Mapping) else None
    if raw is None:
        raw = row.get("depends_on") or []
    if not isinstance(raw, list):
        return ()
    return tuple(str(item) for item in raw if str(item).strip())


def topological_sparse_layer_ids(layers: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    """Authored order is the tie-break among independent ready layers (HIR-0119)."""
    indexed: list[tuple[int, str, Mapping[str, Any]]] = []
    for index, row in enumerate(layers):
        if not isinstance(row, Mapping):
            continue
        layer_id = str(row.get("id") or "").strip()
        if layer_id:
            indexed.append((index, layer_id, row))
    by_id = {layer_id: row for _index, layer_id, row in indexed}
    authored = {layer_id: index for index, layer_id, _row in indexed}
    incoming: dict[str, set[str]] = {layer_id: set() for layer_id in by_id}
    children: dict[str, set[str]] = {layer_id: set() for layer_id in by_id}
    for layer_id, row in by_id.items():
        for dependency in _sparse_depends_on(row):
            if dependency in by_id:
                incoming[layer_id].add(dependency)
                children[dependency].add(layer_id)
    ready = sorted(
        (layer_id for layer_id, deps in incoming.items() if not deps),
        key=lambda layer_id: authored[layer_id],
    )
    remaining = {layer_id: set(deps) for layer_id, deps in incoming.items()}
    ordered: list[str] = []
    while ready:
        layer_id = ready.pop(0)
        ordered.append(layer_id)
        unlocked: list[str] = []
        for child in children[layer_id]:
            remaining[child].discard(layer_id)
            if not remaining[child] and child not in ordered and child not in ready:
                unlocked.append(child)
        ready.extend(sorted(unlocked, key=lambda item: authored[item]))
    if len(ordered) != len(by_id):
        return tuple(layer_id for _index, layer_id, _row in indexed)
    return tuple(ordered)


def compile_deferred_subject_activation(
    layers: Sequence[Mapping[str, Any]],
    owner_layer_id: str,
) -> dict[str, Any]:
    """Compile legal deferred-bbox activation from the selected sparse DAG (HIR-0158)."""
    owner = str(owner_layer_id)
    by_id = {
        str(row.get("id")): row
        for row in layers
        if isinstance(row, Mapping) and row.get("id")
    }
    owner_row = by_id.get(owner)

    def dependency_closure(layer_id: str) -> set[str]:
        seen: set[str] = set()
        stack = list(_sparse_depends_on(by_id[layer_id])) if layer_id in by_id else []
        while stack:
            dependency = stack.pop()
            if dependency in seen or dependency not in by_id:
                continue
            seen.add(dependency)
            stack.extend(_sparse_depends_on(by_id[dependency]))
        return seen

    successors: list[dict[str, Any]] = []
    for layer_id in topological_sparse_layer_ids(layers):
        row = by_id.get(layer_id)
        if row is None or layer_id == owner or owner not in dependency_closure(layer_id):
            continue
        jit = row.get("jit") if isinstance(row.get("jit"), Mapping) else {}
        reserved_raw = (
            (jit.get("reserved_roles") if isinstance(jit, Mapping) else None)
            or row.get("reserved_roles")
            or []
        )
        reserved = [str(item) for item in reserved_raw if str(item).strip()]
        successors.append({
            "id": layer_id,
            "title": row.get("title"),
            "reserved_roles": reserved,
            "allowed_provides": sorted(allowed_unit_provides(row)),
        })
    earliest = next(
        (row["id"] for row in successors if "geometry" in row["allowed_provides"]),
        None,
    )
    return {
        "owner_layer": owner,
        "owner_provides_camera": bool(
            owner_row is not None and "camera" in allowed_unit_provides(owner_row)
        ),
        "successors": successors,
        "earliest_geometry_layer": earliest,
    }


@dataclass(frozen=True, slots=True)
class DeferredSubjectActivationGap:
    contract_id: str
    index: int
    found: str
    expected: str | None


def deferred_subject_activation_gaps(
    card: Mapping[str, Any],
    scene_contracts: Sequence[Mapping[str, Any]],
) -> tuple[DeferredSubjectActivationGap, ...]:
    """Refuse deferred bbox activation that is not the compiled DAG successor."""
    if not card.get("owner_provides_camera"):
        return ()
    owner = str(card.get("owner_layer") or "")
    expected = card.get("earliest_geometry_layer")
    expected_id = str(expected) if expected not in (None, "") else None
    gaps: list[DeferredSubjectActivationGap] = []
    for index, row in enumerate(scene_contracts):
        if not isinstance(row, Mapping):
            continue
        kind = str(row.get("kind") or "")
        if kind not in DEFERRED_SUBJECT_BBOX_KINDS:
            continue
        if str(row.get("owner_layer") or "") != owner:
            continue
        found = str(row.get("activates_at") or owner)
        if found == owner:
            continue
        if expected_id is None or found != expected_id:
            gaps.append(
                DeferredSubjectActivationGap(
                    str(row.get("id") or f"<row {index}>"),
                    index,
                    found,
                    expected_id,
                )
            )
    return tuple(gaps)


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
    "with scene evidence; the downstream camera owner consumes that producer's exact "
    "typed placement interface and owns projection through the camera. The observed "
    "role/control is read-only. Do not fit the target after the camera."
)


@dataclass(frozen=True, slots=True)
class PointProjectionInterfaceGap:
    unit_id: str
    contract_id: str
    selector: str
    producer_ids: tuple[str, ...]
    reason: str


def point_projection_interface_gaps(
    units: Sequence[WorkUnit],
    rows: Sequence[Mapping[str, Any]] | Iterable[Mapping[str, Any]],
) -> tuple[PointProjectionInterfaceGap, ...]:
    """Require camera-owned point projection to read a typed producer interface.

    A same-layer target is a predecessor value, not camera mutation authority. If the
    selector is produced in this DAG, the camera owner must consume an interface that
    exports it. Roles owned only by an accepted upstream layer remain legal through the
    layer dependency/protected-interface mechanism.
    """
    from vfx_harness.domain.publish_interfaces import exported_selector_tokens_from_interface
    from vfx_harness.evidence.scene_checks import PROJECTED_ORIGIN_KINDS

    unit_rows = tuple(units)
    by_id = {unit.id: unit for unit in unit_rows}
    row_by_id = {
        str(row.get("id")): row
        for row in rows
        if isinstance(row, Mapping) and row.get("id")
    }

    def _matches(selector: str, declarations: Iterable[str]) -> bool:
        return plan_selector_declared(selector, tuple(str(value) for value in declarations))

    gaps: list[PointProjectionInterfaceGap] = []
    seen: set[tuple[str, str, str, str]] = set()
    for declaring_unit in unit_rows:
        for claim in declaring_unit.evaluation.claims:
            if not claim.required:
                continue
            owner = by_id.get(claim.repair_owner)
            if owner is None or "camera" not in owner.provides:
                continue  # the independent ownership rule reports this case
            for binding in claim.evidence:
                if binding.kind != "scene_contract":
                    continue
                row = row_by_id.get(binding.id)
                if row is None or str(row.get("kind") or "") not in PROJECTED_ORIGIN_KINDS:
                    continue
                for field, mutation_field in (("roles", "roles"), ("control_roles", "controls")):
                    for raw_selector in row.get(field) or ():
                        selector = str(raw_selector)
                        owner_values = getattr(owner.mutates, mutation_field)
                        if _matches(selector, owner_values):
                            key = (owner.id, binding.id, selector, "owner_mutation")
                            if key not in seen:
                                seen.add(key)
                                gaps.append(
                                    PointProjectionInterfaceGap(
                                        owner.id,
                                        binding.id,
                                        selector,
                                        (owner.id,),
                                        "owner_mutation",
                                    )
                                )
                            continue
                        producers = tuple(
                            producer
                            for producer in unit_rows
                            if producer.id != owner.id
                            and _matches(selector, getattr(producer.mutates, mutation_field))
                        )
                        if not producers:
                            continue
                        compatible: list[str] = []
                        for producer in producers:
                            for consume in owner.consumes:
                                if consume.producer != producer.id:
                                    continue
                                exported = exported_selector_tokens_from_interface(
                                    producer,
                                    interface_id=consume.interface_id,
                                    kind=consume.kind,
                                    selector_type=(
                                        "role" if field == "roles" else "control"
                                    ),
                                )
                                if _matches(selector, exported):
                                    compatible.append(producer.id)
                                    break
                        if compatible:
                            continue
                        producer_ids = tuple(sorted(producer.id for producer in producers))
                        key = (owner.id, binding.id, selector, "missing_consumption")
                        if key in seen:
                            continue
                        seen.add(key)
                        gaps.append(
                            PointProjectionInterfaceGap(
                                owner.id,
                                binding.id,
                                selector,
                                producer_ids,
                                "missing_consumption",
                            )
                        )
    return tuple(gaps)


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


def compile_clustered_mutation_roles(value: Mapping[str, Any]) -> dict[str, Any]:
    """Compile the staging-only one-namespace role shape into a WorkUnit row."""
    unit = dict(value)
    raw_mutates = unit.get("mutates")
    if not isinstance(raw_mutates, Mapping):
        raise ValueError("unit.mutates must be an object")
    mutates = dict(raw_mutates)
    if "roles" in mutates:
        raise ValueError(
            "unit.mutates.roles is not accepted by clustered staging; choose one "
            "role_namespace and relative role_members"
        )
    namespace = str(mutates.pop("role_namespace", "") or "").strip()
    members = mutates.pop("role_members", None)
    if not isinstance(members, list):
        raise ValueError("unit.mutates.role_members must be a list")
    if members and not namespace:
        raise ValueError("non-empty role_members requires one role_namespace")
    if not members and namespace:
        raise ValueError("role_namespace must be omitted when role_members is empty")
    roles = [
        namespace if str(member) == "$self" else f"{namespace}.{member!s}"
        for member in members
    ]
    if any(role != namespace and not role.startswith(f"{namespace}.") for role in roles):
        # Defense below the JSON schema for direct/non-SDK callers.
        raise ValueError(
            f"compiled mutation roles must stay in one namespace {namespace!r}: {roles}"
        )
    mutates["roles"] = roles
    unit["mutates"] = mutates
    return unit


def work_unit_authoring_schema(
    *,
    image_property_kinds: Iterable[str] | None = None,
    axis_ids: Iterable[str] | None = None,
    layer_id: str | None = None,
    allowed_provides: Iterable[str] | None = None,
    clustered_mutation_roles: bool = False,
) -> dict[str, Any]:
    """Closed JSON schema exposed by the materialization unit-ticket tool.

    This is an authoring instrument, not a second parser. ``WorkUnit.parse`` remains
    authoritative; the schema prevents the model from inventing field names before the
    typed parser and its cross-field validation run.
    """
    from vfx_harness.domain.publish_interfaces import PUBLISH_INTERFACE_KINDS
    from vfx_harness.domain.publish_interfaces import SCHEMA as PUB_SCHEMA

    text = {"type": "string", "minLength": 1}
    positive_int = {"type": "integer", "minimum": 1}
    claim_axis = dict(text)
    if axis_ids is not None:
        claim_axis = {
            "type": "string",
            "enum": sorted({str(value) for value in axis_ids}),
            "description": "Exact axis owned by the active layer; do not invent prefixes.",
        }

    def strings(*, nonempty: bool = False) -> dict[str, Any]:
        row: dict[str, Any] = {
            "type": "array",
            "items": dict(text),
            "uniqueItems": True,
        }
        if nonempty:
            row["minItems"] = 1
        return row

    script_item = dict(text)
    if layer_id is not None:
        directory = canonical_unit_script_path(str(layer_id), "unit-id").rsplit("/", 1)[0]
        script_item.update(
            {
                "pattern": rf"^{re.escape(directory)}/[a-zA-Z0-9][a-zA-Z0-9_.-]*\.py$",
                "description": (
                    f"Exact unit artifact under {directory}/; the basename must equal "
                    "the ticket id plus .py. Fragment notation is invalid."
                ),
            }
        )
    else:
        script_item.update(
            {
                "pattern": r"^build/units/[a-zA-Z0-9][a-zA-Z0-9_.-]*/"
                r"[a-zA-Z0-9][a-zA-Z0-9_.-]*\.py$",
                "description": (
                    "One identity-derived build/units/<layer>/<unit-id>.py artifact; "
                    "fragment notation is invalid."
                ),
            }
        )
    script_spans = {
        "type": "array",
        "items": script_item,
        "minItems": 1,
        "maxItems": 1,
        "uniqueItems": True,
    }

    judge = {
        "type": "object",
        "properties": {"frame": positive_int, "ref": text},
        "required": ["frame", "ref"],
        "additionalProperties": False,
    }
    evidence = {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": sorted(EVIDENCE_KINDS)},
            "id": text,
            "moments": {
                "type": "array", "items": positive_int, "minItems": 1, "uniqueItems": True
            },
        },
        "required": ["kind", "id"],
        "additionalProperties": False,
    }
    claim = {
        "type": "object",
        "properties": {
            "id": text,
            "proposition": text,
            "axis": claim_axis,
            "property": text,
            "subject_roles": strings(),
            "subject_controls": strings(),
            "moments": {
                "type": "array", "items": positive_int, "minItems": 1, "uniqueItems": True
            },
            "kind": {"type": "string", "enum": sorted(CLAIM_KINDS)},
            "required": {"type": "boolean"},
            "authority": {"type": "string", "enum": sorted(CLAIM_AUTHORITIES)},
            "repair_owner": text,
            "asserts": {"type": "string", "enum": sorted(CLAIM_DOMAINS)},
            "evidence": {"type": "array", "items": evidence, "minItems": 1},
            "qualification": {"type": "object"},
            "coordination_owner": {
                **text,
                "description": (
                    "Required only for interaction claims: exact same-layer work-unit id "
                    "that owns bounded balancing."
                ),
            },
            "participants": {
                **strings(),
                "description": (
                    "Required only for interaction claims: at least two exact same-layer "
                    "work-unit ids, never semantic roles or controls."
                ),
            },
            "controls": {
                **strings(),
                "description": (
                    "Required only for interaction claims: non-empty bounded control ids "
                    "available to the coordination owner."
                ),
            },
        },
        "required": [
            "id", "proposition", "axis", "property", "subject_roles",
            "subject_controls", "moments", "kind", "required", "authority",
            "repair_owner", "asserts", "evidence",
        ],
        "additionalProperties": False,
    }
    claim["allOf"] = [
        {
            "if": {
                "properties": {"kind": {"const": "interaction"}},
                "required": ["kind"],
            },
            "then": {
                "required": ["coordination_owner", "participants", "controls"],
                "properties": {
                    "participants": {"minItems": 2},
                    "controls": {"minItems": 1},
                },
            },
            "else": {
                "not": {
                    "anyOf": [
                        {"required": ["coordination_owner"]},
                        {"required": ["participants"]},
                        {"required": ["controls"]},
                    ]
                }
            },
        }
    ]
    if image_property_kinds is not None:
        payable = sorted({str(value) for value in image_property_kinds})
        claim["allOf"].append(
            {
                "if": {
                    "properties": {"asserts": {"const": "image"}},
                    "required": ["asserts"],
                },
                "then": {
                    "properties": {
                        "property": {
                            "type": "string",
                            "enum": payable,
                            "description": (
                                "Executable image property. Put free-form appearance "
                                "language in proposition."
                            ),
                        }
                    }
                },
            }
        )
    composition = {
        "type": "object",
        "description": (
            "Optional. Omit when required claims directly bind all evidence. When present, "
            "frames is non-empty and exactly one of source_unit or non-empty contract_ids is set."
        ),
        "properties": {
            "frames": {
                "type": "array", "items": positive_int, "minItems": 1, "uniqueItems": True
            },
            "source_unit": text,
            "contract_ids": strings(nonempty=True),
        },
        "required": ["frames"],
        "oneOf": [
            {"required": ["source_unit"], "not": {"required": ["contract_ids"]}},
            {"required": ["contract_ids"], "not": {"required": ["source_unit"]}},
        ],
        "additionalProperties": False,
    }
    publish = {
        "type": "object",
        "properties": {
            "id": text,
            "kind": {"type": "string", "enum": sorted(PUBLISH_INTERFACE_KINDS)},
            "schema": {"type": "string", "enum": [PUB_SCHEMA]},
            "exports": {
                "type": "object",
                "additionalProperties": dict(text),
                "minProperties": 1,
            },
        },
        "required": ["id", "kind", "exports"],
        "additionalProperties": False,
    }
    consume = {
        "type": "object",
        "description": "Exact read-only predecessor interface; producer must be in depends_on.",
        "properties": {
            "producer": text,
            "interface_id": text,
            "kind": {"type": "string", "enum": sorted(PUBLISH_INTERFACE_KINDS)},
        },
        "required": ["producer", "interface_id", "kind"],
        "additionalProperties": False,
    }
    provides_vocabulary = sorted(
        UNIT_PROVIDES
        if allowed_provides is None
        else {str(value) for value in allowed_provides}
    )

    mutation_properties = {
        "mode": {"type": "string", "enum": ["scoped", "none"]},
        "roles": strings(),
        "controls": strings(),
        "control_roles": {
            "type": "object", "additionalProperties": strings(nonempty=True)
        },
        "dresses": strings(),
        "script_spans": script_spans,
    }
    mutation_required = ["mode", "roles", "controls", "control_roles", "script_spans"]
    mutation_all_of: list[dict[str, Any]] = []
    if clustered_mutation_roles:
        # The model chooses one derived two-token namespace, then only relative members.
        # It cannot put building.mass and building.roof into one list because no field
        # accepts a second absolute namespace (HIR-0150).
        mutation_properties.pop("roles")
        mutation_properties.update({
            "role_namespace": {
                "type": "string",
                "pattern": r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$",
                "description": (
                    "The unit's one derived two-token write namespace, e.g. "
                    "building.mass. Every role member is relative to this namespace."
                ),
            },
            "role_members": {
                "type": "array",
                "items": {
                    "type": "string",
                    "pattern": r"^(?:\$self|[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*)$",
                },
                "uniqueItems": True,
                "description": (
                    "Relative role suffixes inside role_namespace; use $self for the "
                    "namespace tag itself. Absolute roles are not accepted."
                ),
            },
        })
        mutation_required = [
            "mode", "role_members", "controls", "control_roles", "script_spans"
        ]
        mutation_all_of = [{
            "if": {"properties": {"role_members": {"minItems": 1}}},
            "then": {"required": ["role_namespace"]},
            "else": {"not": {"required": ["role_namespace"]}},
        }]

    return {
        "type": "object",
        "properties": {
            "id": text,
            "title": text,
            "plan": text,
            "depends_on": strings(),
            "publishes": {"type": "array", "items": publish, "minItems": 1},
            "consumes": {"type": "array", "items": consume, "minItems": 1},
            "mutates": {
                "type": "object",
                "properties": mutation_properties,
                "required": mutation_required,
                **({"allOf": mutation_all_of} if mutation_all_of else {}),
                "additionalProperties": False,
            },
            "protects": {
                "type": "object",
                "properties": {
                    "selector": text,
                    "ids": strings(nonempty=True),
                    "resolve_to_explicit_ids_at": {"type": "string", "enum": ["freeze"]},
                },
                "required": ["resolve_to_explicit_ids_at"],
                "oneOf": [
                    {"required": ["selector"], "not": {"required": ["ids"]}},
                    {"required": ["ids"], "not": {"required": ["selector"]}},
                ],
                "additionalProperties": False,
            },
            "look_capabilities": {
                "type": "array",
                "items": {"type": "string", "enum": sorted(LOOK_CAPABILITIES)},
                "uniqueItems": True,
            },
            "provides": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": provides_vocabulary,
                    "description": CAMERA_LAYER_DEFERS_SUBJECT_FORM_RULE,
                },
                "uniqueItems": True,
            },
            "evaluation": {
                "type": "object",
                "properties": {
                    "primary_judge": positive_int,
                    "judge": {"type": "array", "items": judge, "minItems": 1},
                    "temporal_evidence": {
                        "type": "string", "enum": sorted(TEMPORAL_EVIDENCE)
                    },
                    "claims": {"type": "array", "items": claim, "minItems": 1},
                    "composition_context": composition,
                },
                "required": ["primary_judge", "judge", "temporal_evidence", "claims"],
                "additionalProperties": False,
            },
            "completion": text,
        },
        "required": [
            "id", "title", "plan", "depends_on", "mutates", "protects",
            "look_capabilities", "provides", "evaluation", "completion",
        ],
        "additionalProperties": False,
    }


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


@dataclass(frozen=True, slots=True)
class DeferredClaimBindingGap:
    unit_id: str
    claim_id: str
    contract_id: str
    owner_layer: str
    activates_at: str


def deferred_claim_binding_gaps(
    units: Iterable[WorkUnit],
    scene_contracts: Iterable[Mapping[str, Any]],
) -> tuple[DeferredClaimBindingGap, ...]:
    """Find future-active contracts incorrectly authored as unit claim evidence."""
    rows = {
        str(row.get("id")): row
        for row in scene_contracts
        if isinstance(row, Mapping) and row.get("id")
    }
    gaps: list[DeferredClaimBindingGap] = []
    for unit in units:
        for claim in unit.evaluation.claims:
            for binding in claim.evidence:
                if binding.kind != "scene_contract":
                    continue
                row = rows.get(str(binding.id))
                if row is None:
                    continue
                owner = str(row.get("owner_layer") or "")
                activates = str(row.get("activates_at") or owner)
                if not owner or not activates or activates == owner:
                    continue
                gaps.append(
                    DeferredClaimBindingGap(
                        unit.id,
                        claim.id,
                        str(binding.id),
                        owner,
                        activates,
                    )
                )
    return tuple(gaps)


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


def dependency_ordered_units(units: Sequence[WorkUnit]) -> tuple[WorkUnit, ...]:
    """Return one deterministic topological order for a work-unit DAG.

    ``stages`` is a collection of authored units, not replay order.  A materializer may
    legally place a consumer before its producer in that array because ``depends_on`` is
    the authority.  Runtime replay and composed publication must therefore derive order
    from the DAG.  Authored position is only the stable tie-break for simultaneously
    ready independent units (HIR-0119).
    """
    ordered_input = tuple(units)
    by_id = {unit.id: unit for unit in ordered_input}
    if len(by_id) != len(ordered_input):
        raise ValueError("work-unit dependency order requires unique unit ids")
    position = {unit.id: index for index, unit in enumerate(ordered_input)}
    missing = {
        dependency
        for unit in ordered_input
        for dependency in unit.depends_on
        if dependency not in by_id
    }
    if missing:
        raise ValueError(
            "work-unit dependency order names missing producer(s): "
            + ", ".join(sorted(missing))
        )

    remaining = {unit.id: set(unit.depends_on) for unit in ordered_input}
    result: list[WorkUnit] = []
    while remaining:
        ready = sorted(
            (uid for uid, dependencies in remaining.items() if not dependencies),
            key=position.__getitem__,
        )
        if not ready:
            involved = sorted(remaining, key=position.__getitem__)
            raise ValueError(
                "work-unit dependency graph is cyclic: " + ", ".join(involved)
            )
        for uid in ready:
            result.append(by_id[uid])
            remaining.pop(uid)
        ready_set = set(ready)
        for dependencies in remaining.values():
            dependencies.difference_update(ready_set)
    return tuple(result)


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
