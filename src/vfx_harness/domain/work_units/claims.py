"""Claim, judge, and evaluation-policy contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from vfx_harness.domain.work_units.evidence_domains import CLAIM_DOMAINS
from vfx_harness.domain.work_units.frames import EXTRA_FRAME_BINDING_RULE
from vfx_harness.domain.work_units.parsing import (
    _SHA256,
    CLAIM_AUTHORITIES,
    CLAIM_KINDS,
    EVIDENCE_KINDS,
    RETIRED_CLAIM_AUTHORITIES,
    RETIRED_EVIDENCE_KINDS,
    TEMPORAL_EVIDENCE,
    _id,
    _mapping,
    _relative_path,
    _strings,
    _text,
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
            raise ValueError(
                f"{where}.frame must be a positive integer: frames are 1-based, t=0.0s is "
                f"frame 1 and frame(t) = round(t*fps)+1 (found {frame!r})"
            )
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
        if kind in RETIRED_EVIDENCE_KINDS:
            raise ValueError(
                f"{where}.kind {kind!r} is retired: no runtime producer pays a human "
                "decision on a work-unit claim. Bind executable evidence or a "
                "qualification, and pay the human domain as approved_start / "
                "planner_start judgment debt on the owning requirement (HIR-0174)"
            )
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
        if authority in RETIRED_CLAIM_AUTHORITIES:
            raise ValueError(
                f"{where}.authority {authority!r} is retired: no runtime producer pays a "
                "human decision on a work-unit claim, and such a claim only forces an "
                "executable-only unit into raster rounds it cannot pay. Use "
                "executable_required or qualified_qualitative_required, and pay the human "
                "domain as approved_start / planner_start judgment debt on the owning "
                "requirement (HIR-0174)"
            )
        if authority not in CLAIM_AUTHORITIES:
            raise ValueError(f"{where}.authority must be one of {sorted(CLAIM_AUTHORITIES)}")
        if required and authority == "advisory":
            raise ValueError(f"{where} cannot be both required and advisory")
        if not required and authority != "advisory":
            raise ValueError(f"{where} non-required claims must be advisory")

        qualification = row.get("qualification")
        if authority == "qualified_qualitative_required":
            q = _mapping(qualification, f"{where}.qualification")
            expected = {"suite", "artifact", "artifact_sha256"}
            if set(q) != expected:
                raise ValueError(
                    f"{where}.qualification requires exactly {sorted(expected)}; "
                    "select a measured profile set instead of a single prompt/model credential"
                )
            qualification = {
                "suite": _text(q.get("suite"), f"{where}.qualification.suite"),
                "artifact": _relative_path(q.get("artifact"), f"{where}.qualification.artifact"),
                "artifact_sha256": _text(q.get("artifact_sha256"), f"{where}.qualification.artifact_sha256"),
            }
            if not _SHA256.fullmatch(qualification["artifact_sha256"]):
                raise ValueError(f"{where}.qualification.artifact_sha256 must be 64 lowercase hex characters")
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
        coordination_owner = row.get("coordination_owner")
        participants = _strings(row.get("participants", []), f"{where}.participants")
        controls = _strings(row.get("controls", []), f"{where}.controls")
        if kind == "interaction":
            incomplete: list[str] = []
            if not isinstance(coordination_owner, str) or not coordination_owner.strip():
                incomplete.append("coordination_owner must be a same-layer work-unit id")
            if len(participants) < 2:
                incomplete.append("participants needs at least two same-layer work-unit ids")
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

    #: Staging-only authoring keys. ``compile_clustered_mutation_roles`` pops both and
    #: emits absolute ``roles``; a durable record must never carry them. Ignoring them
    #: here let a ``patch_materialization`` written in the clustered dialect land as
    #: ``roles: ()`` -- a unit with a bare control, no write family, and no legal
    #: ``run_bpy`` at all (HIR-0217).
    STAGING_ONLY_KEYS = ("role_namespace", "role_members")

    @classmethod
    def parse(cls, value: Any, where: str) -> MutationScope:
        row = _mapping(value, where)
        uncompiled = [key for key in cls.STAGING_ONLY_KEYS if key in row]
        if uncompiled:
            raise ValueError(
                f"{where} carries staging-only authoring key(s) "
                f"{', '.join(uncompiled)}, which the durable shape does not hold. "
                "They are compiled into absolute mutates.roles when a unit is staged; "
                "a patch that writes them directly would silently mutate no roles. "
                "Write mutates.roles with the full selectors this unit mutates, or "
                "restage the unit."
            )
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
        # Name the field and its value, not just the rule. A materializer sent
        # mode 'none' with empty roles, empty controls and one script_span, read its own
        # empty roles in the refusal's terms, concluded it had declared no targets, and
        # retried the identical shape on the next unit. A script span IS a mutation
        # target -- it is a file this unit writes -- and nothing said so (HIR-0233).
        declared = {
            "roles": roles,
            "controls": controls,
            "script_spans": spans,
            "dresses": dresses,
        }
        if mode == "none":
            nonempty = {key: value for key, value in declared.items() if value}
            if nonempty:
                shown = "; ".join(
                    f"{key}={list(value)}" for key, value in sorted(nonempty.items())
                )
                raise ValueError(
                    f"{where} mode 'none' declares no mutation, but {shown} is set. "
                    "Every one of roles, controls, script_spans and dresses is a "
                    "mutation target -- a script span is a file this unit writes. "
                    "Either drop it and keep mode 'none' for a unit that mutates "
                    "nothing, or use mode 'scoped' and declare what it mutates."
                )
        elif not any(declared.values()):
            raise ValueError(
                f"{where} mode 'scoped' declares no mutation target: roles, controls, "
                "script_spans and dresses are all empty. Declare at least one, or use "
                "mode 'none' for a unit that mutates nothing."
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
                declared = ", ".join(controls) if controls else "(none)"
                raise ValueError(
                    f"{where}.control_roles maps undeclared control {key!r}; "
                    f"declared controls: {declared}"
                )
            unknown = sorted(set(targets) - set(roles))
            if unknown:
                # Name the accepted set, not only the offending token. Thirteen
                # materialization refusals across three shots each reported what was
                # wrong and never what would have been right (HIR-0217).
                if roles:
                    legal = f"this unit's mutation roles are: {', '.join(roles)}"
                else:
                    legal = (
                        "this unit mutates no roles, so no control can map one; declare "
                        "the roles this control steers, or drop the control -- a control "
                        "steering no mutated role derives no write family"
                    )
                raise ValueError(
                    f"{where}.control_roles.{key} maps roles outside mutation scope: "
                    f"{', '.join(unknown)}; {legal}"
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
