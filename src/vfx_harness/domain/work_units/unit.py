"""WorkUnit identity, mutation scope, publish/consume, and document load."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vfx_harness.domain.claim_bindings import bound_claim_contract_ids
from vfx_harness.domain.publish_interfaces import (
    PUBLISH_INTERFACE_KINDS,
    UNKNOWN_KIND_RULE,
    parse_publish_interface,
)
from vfx_harness.domain.work_units.capabilities import UNIT_PROVIDES
from vfx_harness.domain.work_units.claims import EvaluationPolicy, MutationScope, ProtectionSpec
from vfx_harness.domain.work_units.parsing import (
    SCHEMA,
    _id,
    _mapping,
    _relative_path,
    _strings,
    _text,
    canonical_unit_script_path,
)


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


# Authored cluster labels. Publication derives write-clusters; these fields are
# HIR-0017 padding and are unrepresentable on a WorkUnit (HIR-0083).
ATOMICITY_PADDING_FIELDS = frozenset({"family", "mutation_family", "coherent_family", "primary_subject"})

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
                f"{where}[{index}].producer {producer!r} is not a declared dependency. " + CONSUME_INTERFACE_RULE
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


def parse_look_capabilities(value: Any, where: str) -> tuple[str, ...]:
    names = _strings(value, where) if value else ()
    unknown = sorted(set(names) - set(LOOK_CAPABILITIES))
    if unknown:
        raise ValueError(
            f"{where} has unknown capability {', '.join(unknown)}; declare a subset of "
            + ", ".join(sorted(LOOK_CAPABILITIES))
        )
    return tuple(dict.fromkeys(names))


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
            parse_look_capabilities(row.get("look_capabilities", []), f"{where}.look_capabilities"),
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
            _parse_consume_specs(row.get("consumes"), f"{where}.consumes", depends_on=draft.depends_on),
        )
        context = unit.evaluation.composition_context
        if context and context.source_unit and context.source_unit not in unit.depends_on:
            raise ValueError(f"{where}.evaluation.composition_context source_unit must be a declared dependency")
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
