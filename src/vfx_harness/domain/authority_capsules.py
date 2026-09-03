"""Pure, schema-owned semantic authority capsules (HIR-0171).

The complete selected view remains provenance.  Per-layer and per-unit digests contain
only typed authority that can affect that subject, so an independent JIT publication
does not revoke unrelated proof.  Ownership is never inferred from prose, frames,
titles, axes, or role names.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar

from vfx_harness.domain.authority_head_records import OVERLAY_ARTIFACTS
from vfx_harness.domain.judgment_debt_models import JudgmentDebtActivation, JudgmentDebtDefinition
from vfx_harness.domain.work_units import WorkUnit, validate_unit_dag

AUTHORITY_CAPSULE_SET_SCHEMA = "vfx-harness.authority-capsule-set/v1"
LAYER_AUTHORITY_CAPSULE_SCHEMA = "vfx-harness.layer-authority-capsule/v1"
UNIT_AUTHORITY_CAPSULE_SCHEMA = "vfx-harness.unit-authority-capsule/v1"
WORK_UNIT_AUTHORITY_IDENTITY_SCHEMA = "vfx-harness.work-unit-authority-identity/v1"

_DOCS = frozenset(OVERLAY_ARTIFACTS)
_SCHEMAS: dict[str, object] = {
    "layers.json": 5,
    "scene_checks.json": 2,
    "checks.json": 2,
    "requirements.json": "vfx-harness.requirements/v2",
    "acceptance.json": "vfx-harness.acceptance-array/v1",
}
_LAYER_FIELDS = frozenset(
    {
        "id", "script", "title", "primary_judge", "judge", "owns",
        "evidence_domains", "reads", "execution", "stages", "jit", "dressable",
    }
)
_LAYER_CORE = frozenset(
    {"id", "script", "title", "primary_judge", "judge", "owns", "evidence_domains", "reads"}
)
_JIT_FIELDS = frozenset(
    {"depends_on_layers", "required_outcomes", "provides", "reserved_roles", "owned_requirements"}
)
_UNIT_FIELDS = frozenset(
    {
        "id", "title", "plan", "depends_on", "mutates", "protects", "evaluation",
        "completion", "look_capabilities", "provides", "publishes", "consumes", "construction",
    }
)
_REQUIREMENT_FIELDS = frozenset({"id", "statement", "citation", "resolution"})
_CITATION_FIELDS = frozenset({"source", "sha256", "line_start", "line_end"})
_ACCEPTANCE_FIELDS = frozenset({"id", "frame", "ref", "reads", "strip", "fingerprint"})
_BINDING_KINDS = frozenset(
    {"scene_contract", "image_contract", "semantic_diff", "qualification", "human_decision", "replay"}
)


class AuthorityCapsuleError(ValueError):
    """Authority has no total, current-schema capsule projection."""


def _bytes(value: Any, where: str) -> bytes:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise AuthorityCapsuleError(f"{where} must contain canonical JSON values: {exc}") from exc


def _digest(value: Any, where: str) -> str:
    return hashlib.sha256(_bytes(value, where)).hexdigest()


def _copy(value: Any, where: str) -> Any:
    return json.loads(_bytes(value, where))


def _map(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise AuthorityCapsuleError(f"{where} must be an object with string keys")
    return value


def _list(value: Any, where: str) -> list[Mapping[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(row, Mapping) for row in value):
        raise AuthorityCapsuleError(f"{where} must be a list of objects")
    return list(value)


def _text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise AuthorityCapsuleError(f"{where} must be a non-empty trimmed string")
    return value


def _texts(value: Any, where: str, *, nonempty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or (nonempty and not value):
        raise AuthorityCapsuleError(f"{where} must be a{' non-empty' if nonempty else ''} list")
    rows = tuple(_text(item, f"{where}[{index}]") for index, item in enumerate(value))
    if len(rows) != len(set(rows)):
        raise AuthorityCapsuleError(f"{where} contains duplicates")
    return rows


def _fields(
    row: Mapping[str, Any], allowed: frozenset[str], where: str, required: frozenset[str] = frozenset()
) -> None:
    missing, extra = sorted(required - set(row)), sorted(set(row) - allowed)
    if missing or extra:
        raise AuthorityCapsuleError(f"{where} fields unsupported; missing={missing}; unexpected={extra}")


def _documents(value: Any, where: str) -> dict[str, Any]:
    rows = _map(value, where)
    if set(rows) != _DOCS:
        raise AuthorityCapsuleError(f"{where} must contain exactly {sorted(_DOCS)}; found={sorted(rows)}")
    out = _copy(rows, where)
    assert isinstance(out, dict)
    for name, expected in _SCHEMAS.items():
        document = out[name]
        if name == "acceptance.json":
            if not isinstance(document, list):
                raise AuthorityCapsuleError(f"{where}.{name} must be a list")
        elif _map(document, f"{where}.{name}").get("schema") != expected:
            raise AuthorityCapsuleError(
                f"{where}.{name}.schema unsupported; expected {expected!r}, "
                f"found {_map(document, name).get('schema')!r}"
            )
    return out


def _index(rows: Sequence[Mapping[str, Any]], where: str) -> dict[str, Mapping[str, Any]]:
    out: dict[str, Mapping[str, Any]] = {}
    for index, row in enumerate(rows):
        row_id = _text(row.get("id"), f"{where}[{index}].id")
        if row_id in out:
            raise AuthorityCapsuleError(f"{where}[{index}].id duplicates {row_id!r}")
        out[row_id] = row
    return out


def _layers(
    documents: Mapping[str, Any], where: str
) -> tuple[list[Mapping[str, Any]], dict[str, Mapping[str, Any]]]:
    document = _map(documents["layers.json"], f"{where}.layers.json")
    _fields(document, frozenset({"schema", "layers"}), f"{where}.layers.json", frozenset({"schema", "layers"}))
    rows = _list(document.get("layers"), f"{where}.layers.json.layers")
    if not rows:
        raise AuthorityCapsuleError(f"{where}.layers.json.layers must be non-empty")
    return rows, _index(rows, f"{where}.layers.json.layers")


def _sparse_order(rows: Sequence[Mapping[str, Any]], where: str) -> tuple[str, ...]:
    order: list[str] = []
    for index, row in enumerate(rows):
        at = f"{where}.layers.json.layers[{index}]"
        _fields(row, _LAYER_FIELDS, at, _LAYER_CORE | frozenset({"execution", "stages", "jit"}))
        layer_id = _text(row.get("id"), f"{at}.id")
        if layer_id in order:
            raise AuthorityCapsuleError(f"{at}.id duplicates {layer_id!r}")
        if row.get("execution") != "jit_deferred" or row.get("stages") != []:
            raise AuthorityCapsuleError(f"{at} must be sparse jit_deferred authority with no stages")
        jit = _map(row.get("jit"), f"{at}.jit")
        _fields(jit, _JIT_FIELDS, f"{at}.jit", _JIT_FIELDS)
        dependencies = _texts(jit.get("depends_on_layers"), f"{at}.jit.depends_on_layers")
        unknown = sorted(set(dependencies) - set(order))
        if unknown:
            raise AuthorityCapsuleError(f"{at}.jit.depends_on_layers must name earlier layers: {unknown}")
        order.append(layer_id)
    return tuple(order)


def _effective_units(
    sparse: Mapping[str, Mapping[str, Any]], rows: Sequence[Mapping[str, Any]], where: str
) -> tuple[dict[str, tuple[WorkUnit, ...]], dict[str, dict[str, Mapping[str, Any]]]]:
    if [str(row.get("id") or "") for row in rows] != list(sparse):
        raise AuthorityCapsuleError(f"{where}.layers.json must preserve sparse layer order")
    parsed: dict[str, tuple[WorkUnit, ...]] = {}
    raw: dict[str, dict[str, Mapping[str, Any]]] = {}
    for index, row in enumerate(rows):
        at = f"{where}.layers.json.layers[{index}]"
        _fields(row, _LAYER_FIELDS, at, _LAYER_CORE | frozenset({"execution", "stages"}))
        layer_id = _text(row.get("id"), f"{at}.id")
        base = sparse[layer_id]
        changed = sorted(field for field in _LAYER_CORE if row.get(field) != base.get(field))
        if changed:
            raise AuthorityCapsuleError(f"{at} changes sparse layer fields: {changed}")
        if row.get("execution") == "jit_deferred":
            if row != base:
                raise AuthorityCapsuleError(f"{at} is deferred but differs from sparse authority")
            parsed[layer_id], raw[layer_id] = (), {}
            continue
        if row.get("execution") != "ready" or "jit" in row:
            raise AuthorityCapsuleError(f"{at} must be unchanged deferred or ready without jit")
        unit_rows = _list(row.get("stages"), f"{at}.stages")
        if not unit_rows:
            raise AuthorityCapsuleError(f"{at}.stages must be non-empty when ready")
        layer_units: list[WorkUnit] = []
        raw_by_id: dict[str, Mapping[str, Any]] = {}
        for unit_index, unit_row in enumerate(unit_rows):
            unit_at = f"{at}.stages[{unit_index}]"
            _fields(unit_row, _UNIT_FIELDS, unit_at)
            try:
                unit = WorkUnit.parse(unit_row, unit_at)
            except ValueError as exc:
                raise AuthorityCapsuleError(str(exc)) from exc
            if unit.id in raw_by_id:
                raise AuthorityCapsuleError(f"{unit_at}.id duplicates {unit.id!r}")
            layer_units.append(unit)
            raw_by_id[unit.id] = unit_row
        try:
            validate_unit_dag(tuple(layer_units), f"{at}.stages")
        except ValueError as exc:
            raise AuthorityCapsuleError(str(exc)) from exc
        parsed[layer_id], raw[layer_id] = tuple(layer_units), raw_by_id
    return parsed, raw


def _contracts(
    documents: Mapping[str, Any], where: str, layer_ids: frozenset[str]
) -> tuple[
    dict[tuple[str, str], Mapping[str, Any]],
    dict[str, tuple[str, str]],
    dict[str, list[tuple[str, Mapping[str, Any]]]],
]:
    by_key: dict[tuple[str, str], Mapping[str, Any]] = {}
    by_id: dict[str, tuple[str, str]] = {}
    by_owner = {layer_id: [] for layer_id in layer_ids}
    for name, rows_key, kind in (
        ("scene_checks.json", "contracts", "scene_contract"),
        ("checks.json", "checks", "image_contract"),
    ):
        document = _map(documents[name], f"{where}.{name}")
        _fields(document, frozenset({"schema", rows_key}), f"{where}.{name}", frozenset({"schema", rows_key}))
        for index, row in enumerate(_list(document.get(rows_key), f"{where}.{name}.{rows_key}")):
            at = f"{where}.{name}.{rows_key}[{index}]"
            row_id = _text(row.get("id"), f"{at}.id")
            owner = _text(row.get("owner_layer"), f"{at}.owner_layer")
            if owner not in layer_ids:
                raise AuthorityCapsuleError(f"{at}.owner_layer names unknown layer {owner!r}")
            if row_id in by_id:
                raise AuthorityCapsuleError(f"{at}.id {row_id!r} is ambiguous across contract catalogs")
            by_key[(kind, row_id)], by_id[row_id] = row, (kind, owner)
            by_owner[owner].append((kind, row))
    for owner_rows in by_owner.values():
        owner_rows.sort(key=lambda item: (item[0], str(item[1]["id"])))
    return by_key, by_id, by_owner


def _requirements(
    documents: Mapping[str, Any], where: str
) -> tuple[dict[str, Mapping[str, Any]], tuple[JudgmentDebtDefinition, ...], tuple[JudgmentDebtActivation, ...]]:
    document = _map(documents["requirements.json"], f"{where}.requirements.json")
    fields = frozenset({"schema", "requirements", "judgment_debt_definitions", "judgment_debt_activations"})
    _fields(document, fields, f"{where}.requirements.json", fields)
    rows = _list(document.get("requirements"), f"{where}.requirements.json.requirements")
    indexed = _index(rows, f"{where}.requirements.json.requirements")
    for index, row in enumerate(rows):
        at = f"{where}.requirements.json.requirements[{index}]"
        _fields(row, _REQUIREMENT_FIELDS, at, _REQUIREMENT_FIELDS)
        _text(row.get("statement"), f"{at}.statement")
        citation = _map(row.get("citation"), f"{at}.citation")
        _fields(citation, _CITATION_FIELDS, f"{at}.citation", _CITATION_FIELDS)
        if citation.get("source") != "brief.md":
            raise AuthorityCapsuleError(f"{at}.citation.source must be 'brief.md'")
        _map(row.get("resolution"), f"{at}.resolution")
    definitions: list[JudgmentDebtDefinition] = []
    for index, row in enumerate(
        _list(document.get("judgment_debt_definitions"), f"{where}.requirements.json.judgment_debt_definitions")
    ):
        at = f"{where}.requirements.json.judgment_debt_definitions[{index}]"
        try:
            definition = JudgmentDebtDefinition.from_dict(row, at)
        except ValueError as exc:
            raise AuthorityCapsuleError(str(exc)) from exc
        requirement = indexed.get(definition.seed.requirement_id)
        if requirement is None or requirement.get("statement") != definition.seed.statement:
            raise AuthorityCapsuleError(f"{at} does not exactly match its requirement")
        definitions.append(definition)
    if len({row.digest for row in definitions}) != len(definitions) or len(
        {row.debt_id for row in definitions}
    ) != len(definitions):
        raise AuthorityCapsuleError(f"{where}.requirements.json has duplicate debt definitions")
    by_digest = {row.digest: row for row in definitions}
    activations: list[JudgmentDebtActivation] = []
    for index, row in enumerate(
        _list(document.get("judgment_debt_activations"), f"{where}.requirements.json.judgment_debt_activations")
    ):
        at = f"{where}.requirements.json.judgment_debt_activations[{index}]"
        try:
            activation = JudgmentDebtActivation.from_dict(row, at)
            definition = by_digest.get(activation.definition_digest)
            if definition is None:
                raise AuthorityCapsuleError(f"{at}.definition_digest names no selected definition")
            activation.assert_matches(definition)
        except ValueError as exc:
            raise AuthorityCapsuleError(str(exc)) from exc
        activations.append(activation)
    if len({row.definition_digest for row in activations}) != len(activations):
        raise AuthorityCapsuleError(f"{where}.requirements.json has duplicate payer activations")
    return indexed, tuple(definitions), tuple(activations)


def _acceptance(documents: Mapping[str, Any], where: str) -> dict[str, Mapping[str, Any]]:
    rows = _list(documents["acceptance.json"], f"{where}.acceptance.json")
    indexed = _index(rows, f"{where}.acceptance.json")
    for index, row in enumerate(rows):
        at = f"{where}.acceptance.json[{index}]"
        _fields(row, _ACCEPTANCE_FIELDS, at, frozenset({"id", "frame", "ref"}))
        if isinstance(row.get("frame"), bool) or not isinstance(row.get("frame"), int) or row["frame"] < 1:
            raise AuthorityCapsuleError(f"{at}.frame must be a positive integer")
        _text(row.get("ref"), f"{at}.ref")
    return indexed


def _carried(before: Mapping[str, Any], after: Mapping[str, Any], where: str) -> None:
    missing = sorted(row_id for row_id, row in before.items() if after.get(row_id) != row)
    if missing:
        raise AuthorityCapsuleError(f"effective authority does not carry exact global {where}: {missing}")


def _bindings(unit: Mapping[str, Any], where: str) -> tuple[dict[str, Any], ...]:
    evaluation = _map(unit.get("evaluation"), f"{where}.evaluation")
    out: list[dict[str, Any]] = []
    for claim_index, claim in enumerate(_list(evaluation.get("claims"), f"{where}.evaluation.claims")):
        claim_id = _text(claim.get("id"), f"{where}.evaluation.claims[{claim_index}].id")
        for index, binding in enumerate(
            _list(claim.get("evidence"), f"{where}.evaluation.claims[{claim_index}].evidence")
        ):
            at = f"{where}.evaluation.claims[{claim_index}].evidence[{index}]"
            kind, binding_id = _text(binding.get("kind"), f"{at}.kind"), _text(binding.get("id"), f"{at}.id")
            if kind not in _BINDING_KINDS:
                raise AuthorityCapsuleError(f"{at}.kind {kind!r} has no capsule projection")
            out.append({"claim_id": claim_id, "binding": _copy(binding, at), "kind": kind, "id": binding_id})
    return tuple(out)


def _deferred_owner(global_row: Mapping[str, Any], requirement_id: str, layer_ids: frozenset[str]) -> str | None:
    """The layer the global bundle deferred this requirement to, if it deferred it at all."""
    resolution = _map(global_row.get("resolution"), f"global requirement {requirement_id!r}.resolution")
    if _text(resolution.get("kind"), f"global requirement {requirement_id!r}.resolution.kind") != "deferred_owner":
        return None
    owner = _text(resolution.get("owner_layer"), f"global requirement {requirement_id!r}.owner_layer")
    if owner not in layer_ids:
        raise AuthorityCapsuleError(f"global requirement {requirement_id!r} names unknown owner {owner!r}")
    return owner


def _requirement_owners(
    requirements: Mapping[str, Mapping[str, Any]],
    global_requirements: Mapping[str, Mapping[str, Any]],
    definitions: Sequence[JudgmentDebtDefinition],
    contract_owners: Mapping[str, tuple[str, str]],
    claim_owners: Mapping[tuple[str, str], frozenset[str]],
    layer_ids: frozenset[str],
) -> dict[str, frozenset[str]]:
    debt_by_requirement: dict[str, set[str]] = {}
    for definition in definitions:
        if definition.seed.owner_layer not in layer_ids:
            raise AuthorityCapsuleError(
                f"debt {definition.debt_id!r} names unknown owner {definition.seed.owner_layer!r}"
            )
        debt_by_requirement.setdefault(definition.seed.requirement_id, set()).add(definition.seed.owner_layer)
    out: dict[str, frozenset[str]] = {}
    for requirement_id, row in requirements.items():
        resolution = _map(row.get("resolution"), f"requirement {requirement_id!r}.resolution")
        kind = _text(resolution.get("kind"), f"requirement {requirement_id!r}.resolution.kind")
        if kind == "deferred_owner":
            _fields(
                resolution,
                frozenset({"kind", "ids", "owner_layer", "due", "evidence_domains"}),
                f"requirement {requirement_id!r}.resolution",
            )
            owner = _text(resolution.get("owner_layer"), f"requirement {requirement_id!r}.owner_layer")
            if owner not in layer_ids:
                raise AuthorityCapsuleError(f"requirement {requirement_id!r} names unknown owner {owner!r}")
            out[requirement_id] = frozenset({owner})
        elif kind == "contract":
            _fields(
                resolution,
                frozenset({"kind", "ids", "evidence_domains", "domain_bindings"}),
                f"requirement {requirement_id!r}.resolution",
            )
            owners: set[str] = set()
            for contract_id in _texts(resolution.get("ids"), f"requirement {requirement_id!r}.ids", nonempty=True):
                if contract_id in contract_owners:
                    owners.add(contract_owners[contract_id][1])
                    continue
                candidates = {
                    owner
                    for (_binding_kind, binding_id), binding_owners in claim_owners.items()
                    if binding_id == contract_id
                    for owner in binding_owners
                }
                if len(candidates) != 1:
                    raise AuthorityCapsuleError(
                        f"requirement {requirement_id!r} contract {contract_id!r} has no unique owner"
                    )
                owners.update(candidates)
            if len(owners) != 1:
                raise AuthorityCapsuleError(f"requirement {requirement_id!r} spans owners {sorted(owners)}")
            out[requirement_id] = frozenset(owners)
        elif kind == "decision":
            _fields(
                resolution,
                frozenset(
                    {"kind", "ids", "decision", "decision_strength", "evidence_domains", "domain_bindings"}
                ),
                f"requirement {requirement_id!r}.resolution",
            )
            debt_owners = debt_by_requirement.get(requirement_id, set())
            if len(debt_owners) > 1:
                raise AuthorityCapsuleError(f"requirement {requirement_id!r} has ambiguous debt owners")
            if debt_owners:
                out[requirement_id] = frozenset(debt_owners)
                continue
            # A decision a layer's materialization makes on a requirement the global
            # bundle deferred to it belongs to that layer. Run 20260903T053305Z-83f8e1
            # bound R51 as a pure human decision on layer 2 and, projected shot-wide, it
            # changed layer 1's capsule and superseded a terminal receipt earned minutes
            # earlier. Only a decision on a requirement the bundle never deferred is
            # genuinely shot-wide.
            deferred_owner = _deferred_owner(global_requirements[requirement_id], requirement_id, layer_ids)
            out[requirement_id] = frozenset({deferred_owner}) if deferred_owner else layer_ids
        else:
            raise AuthorityCapsuleError(
                f"requirement {requirement_id!r} kind {kind!r} has no capsule projection"
            )
    return out


def _interfaces(
    unit: Mapping[str, Any], units: Mapping[str, Mapping[str, Any]], where: str
) -> tuple[dict[str, Any], ...]:
    if unit.get("consumes") in (None, []):
        return ()
    out: list[dict[str, Any]] = []
    for index, consume in enumerate(_list(unit.get("consumes"), f"{where}.consumes")):
        at = f"{where}.consumes[{index}]"
        producer_id = _text(consume.get("producer"), f"{at}.producer")
        producer = units.get(producer_id)
        if producer is None:
            raise AuthorityCapsuleError(f"{at}.producer names unknown unit {producer_id!r}")
        matches = [
            publish
            for publish in _list(producer.get("publishes", []), f"{where}.producer.publishes")
            if publish.get("id") == consume.get("interface_id") and publish.get("kind") == consume.get("kind")
        ]
        if len(matches) != 1:
            raise AuthorityCapsuleError(f"{at} has {len(matches)} matching producer interfaces; expected one")
        out.append(
            {
                "consume": _copy(consume, at),
                "producer_unit_id": producer_id,
                "producer_interface": _copy(matches[0], at),
            }
        )
    return tuple(out)


def _qualified_payer_unit_id(layer_id: str, unit_id: str) -> str:
    """Return the canonical replay-prefix identity used by debt activations."""

    return f"{layer_id}:{unit_id}"


def _require_activation_payer_units(
    activation: JudgmentDebtActivation,
    units: Mapping[str, Mapping[str, Any]],
) -> None:
    """Close activation payer ids against exact layer-qualified unit authority.

    Replay-prefix state keys units globally as ``<layer>:<unit>``. Capsule
    compilation must join that same typed identity rather than treating the suffix as
    a local id or accepting an unqualified compatibility spelling.
    """

    expected = {
        _qualified_payer_unit_id(activation.payer_layer, unit_id)
        for unit_id in units
    }
    observed = {unit_id for unit_id, _digest in activation.payer_unit_digests}
    invalid = sorted(observed - expected)
    if invalid:
        raise AuthorityCapsuleError(
            f"activation payer layer {activation.payer_layer!r} has non-canonical "
            f"payer unit identities {invalid}; expected exact qualified ids from "
            f"{sorted(expected)}"
        )


@dataclass(frozen=True, slots=True)
class UnitAuthorityCapsule:
    SCHEMA: ClassVar[str] = UNIT_AUTHORITY_CAPSULE_SCHEMA
    layer_id: str
    unit_id: str
    work_unit_digest: str
    capsule_digest: str
    _projection_json: str

    @property
    def projection(self) -> dict[str, Any]:
        return json.loads(self._projection_json)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA, "layer_id": self.layer_id, "unit_id": self.unit_id,
            "work_unit_digest": self.work_unit_digest, "projection": self.projection,
            "capsule_digest": self.capsule_digest,
        }


@dataclass(frozen=True, slots=True)
class LayerAuthorityCapsule:
    SCHEMA: ClassVar[str] = LAYER_AUTHORITY_CAPSULE_SCHEMA
    layer_id: str
    unit_capsule_digests: tuple[tuple[str, str], ...]
    predecessor_layer_digests: tuple[tuple[str, str], ...]
    capsule_digest: str
    _projection_json: str

    @property
    def projection(self) -> dict[str, Any]:
        return json.loads(self._projection_json)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "layer_id": self.layer_id,
            "unit_capsules": [{"unit_id": key, "digest": value} for key, value in self.unit_capsule_digests],
            "predecessor_layers": [
                {"layer_id": key, "digest": value} for key, value in self.predecessor_layer_digests
            ],
            "projection": self.projection,
            "capsule_digest": self.capsule_digest,
        }


@dataclass(frozen=True, slots=True)
class AuthorityCapsuleSet:
    SCHEMA: ClassVar[str] = AUTHORITY_CAPSULE_SET_SCHEMA
    global_authority_digest: str
    effective_authority_digest: str
    document_schemas: tuple[tuple[str, str], ...]
    units: tuple[UnitAuthorityCapsule, ...]
    layers: tuple[LayerAuthorityCapsule, ...]
    capsule_set_digest: str

    def layer(self, layer_id: str) -> LayerAuthorityCapsule:
        return next(row for row in self.layers if row.layer_id == str(layer_id))

    def unit(self, layer_id: str, unit_id: str) -> UnitAuthorityCapsule:
        return next(
            row for row in self.units if row.layer_id == str(layer_id) and row.unit_id == str(unit_id)
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "provenance": {
                "global_authority_digest": self.global_authority_digest,
                "effective_authority_digest": self.effective_authority_digest,
                "document_schemas": dict(self.document_schemas),
            },
            "units": [row.as_dict() for row in self.units],
            "layers": [row.as_dict() for row in self.layers],
            "capsule_set_digest": self.capsule_set_digest,
        }


def _unit_capsule(
    layer_id: str,
    unit: Mapping[str, Any],
    layer_units: Mapping[str, Mapping[str, Any]],
    contracts: Mapping[tuple[str, str], Mapping[str, Any]],
    requirements: Mapping[str, Mapping[str, Any]],
    requirement_owners: Mapping[str, frozenset[str]],
    definitions: Sequence[JudgmentDebtDefinition],
    activations: Sequence[JudgmentDebtActivation],
) -> UnitAuthorityCapsule:
    unit_id, where = str(unit["id"]), f"layer {layer_id!r} unit {unit['id']!r}"
    bindings = _bindings(unit, where)
    keys = {(row["kind"], row["id"]) for row in bindings}
    missing_scene = sorted(
        row_id
        for kind, row_id in keys
        if kind == "scene_contract" and (kind, row_id) not in contracts
    )
    if missing_scene:
        raise AuthorityCapsuleError(f"{where} references missing scene contracts: {missing_scene}")
    selected_contracts = [
        {"kind": kind, "row": _copy(row, f"{where}.contract")}
        for (kind, contract_id), row in sorted(contracts.items())
        if (kind, contract_id) in keys
        or (row.get("owner_layer") == layer_id and row.get("fault_owner") == unit_id)
    ]
    unit_definitions = [
        row for row in definitions if row.seed.owner_layer == layer_id and row.seed.fault_owner == unit_id
    ]
    unit_activations = [
        row
        for row in activations
        if row.payer_layer == layer_id
        and _qualified_payer_unit_id(layer_id, unit_id)
        in {key for key, _value in row.payer_unit_digests}
    ]
    bound_ids = {row["id"] for row in bindings}
    definition_requirements = {row.seed.requirement_id for row in unit_definitions}
    unit_requirements = [
        row
        for requirement_id, row in requirements.items()
        if layer_id in requirement_owners[requirement_id]
        and (
            requirement_id in definition_requirements
            or bool(bound_ids & set(_map(row["resolution"], "resolution").get("ids", [])))
        )
    ]
    claims = _list(_map(unit["evaluation"], f"{where}.evaluation").get("claims"), f"{where}.claims")
    identity = {
        "schema": WORK_UNIT_AUTHORITY_IDENTITY_SCHEMA, "layer_id": layer_id,
        "unit_id": unit_id, "row": _copy(unit, where),
    }
    work_unit_digest = _digest(identity, f"{where}.identity")
    projection = {
        "work_unit": identity,
        "claim_bindings": list(bindings),
        "contracts": selected_contracts,
        "requirements": sorted((_copy(row, where) for row in unit_requirements), key=lambda row: row["id"]),
        "judgment_debt_definitions": [row.as_dict() for row in sorted(unit_definitions, key=lambda row: row.digest)],
        "judgment_debt_activations": [row.as_dict() for row in sorted(unit_activations, key=lambda row: row.digest)],
        "qualifications": [
            {"claim_id": claim["id"], "qualification": _copy(claim["qualification"], where)}
            for claim in claims
            if "qualification" in claim
        ],
        "construction": _copy(unit.get("construction"), where) if "construction" in unit else None,
        "interfaces": list(_interfaces(unit, layer_units, where)),
    }
    payload = {
        "schema": UNIT_AUTHORITY_CAPSULE_SCHEMA, "layer_id": layer_id, "unit_id": unit_id,
        "work_unit_digest": work_unit_digest, "projection": projection,
    }
    return UnitAuthorityCapsule(
        layer_id, unit_id, work_unit_digest, _digest(payload, f"{where}.capsule"),
        _bytes(projection, f"{where}.projection").decode(),
    )


def compile_authority_capsules(
    global_documents: Mapping[str, Any], effective_documents: Mapping[str, Any]
) -> AuthorityCapsuleSet:
    """Compile deterministic unit/layer capsules from one exact consumer view pair."""

    global_docs = _documents(global_documents, "global authority")
    effective_docs = _documents(effective_documents, "effective authority")
    global_rows, sparse = _layers(global_docs, "global authority")
    order = _sparse_order(global_rows, "global authority")
    effective_rows, _effective_index = _layers(effective_docs, "effective authority")
    parsed_units, raw_units = _effective_units(sparse, effective_rows, "effective authority")
    layer_ids = frozenset(order)

    global_contracts, _global_contract_owners, _global_contracts_by_owner = _contracts(
        global_docs, "global authority", layer_ids
    )
    contracts, contract_owners, contracts_by_owner = _contracts(
        effective_docs, "effective authority", layer_ids
    )
    _carried(global_contracts, contracts, "contracts")
    global_requirements, global_definitions, global_activations = _requirements(global_docs, "global authority")
    requirements, definitions, activations = _requirements(effective_docs, "effective authority")
    if set(global_requirements) != set(requirements):
        raise AuthorityCapsuleError("effective authority must preserve the global requirement id set")
    for requirement_id, before in global_requirements.items():
        after = requirements[requirement_id]
        if any(before.get(key) != after.get(key) for key in ("id", "statement", "citation")):
            raise AuthorityCapsuleError(f"effective requirement {requirement_id!r} changes authored authority")
    _carried(
        {row.digest: row.as_dict() for row in global_definitions},
        {row.digest: row.as_dict() for row in definitions},
        "debt definitions",
    )
    _carried(
        {row.digest: row.as_dict() for row in global_activations},
        {row.digest: row.as_dict() for row in activations},
        "debt activations",
    )
    global_acceptance, acceptance = _acceptance(global_docs, "global authority"), _acceptance(
        effective_docs, "effective authority"
    )
    _carried(global_acceptance, acceptance, "acceptance rows")

    claim_owner_sets: dict[tuple[str, str], set[str]] = {}
    for layer_id in order:
        for raw_unit in raw_units[layer_id].values():
            for binding in _bindings(raw_unit, f"layer {layer_id!r} unit {raw_unit['id']!r}"):
                claim_owner_sets.setdefault((binding["kind"], binding["id"]), set()).add(layer_id)
    claim_owners = {key: frozenset(value) for key, value in claim_owner_sets.items()}
    requirement_owners = _requirement_owners(
        requirements, global_requirements, definitions, contract_owners, claim_owners, layer_ids
    )
    definitions_by_owner = {layer_id: [] for layer_id in order}
    activations_by_payer = {layer_id: [] for layer_id in order}
    for definition in definitions:
        definitions_by_owner[definition.seed.owner_layer].append(definition)
    for activation in activations:
        payer = activation.payer_layer
        if payer not in layer_ids:
            raise AuthorityCapsuleError(f"activation names unknown payer layer {payer!r}")
        _require_activation_payer_units(activation, raw_units[payer])
        activations_by_payer[payer].append(activation)

    units: list[UnitAuthorityCapsule] = []
    units_by_layer: dict[str, tuple[UnitAuthorityCapsule, ...]] = {}
    for layer_id in order:
        compiled = tuple(
            _unit_capsule(
                layer_id, raw_units[layer_id][unit.id], raw_units[layer_id], contracts,
                requirements, requirement_owners, definitions_by_owner[layer_id], activations_by_payer[layer_id],
            )
            for unit in parsed_units[layer_id]
        )
        units_by_layer[layer_id] = compiled
        units.extend(compiled)

    layer_capsules: list[LayerAuthorityCapsule] = []
    layer_digests: dict[str, str] = {}
    effective_by_id = {str(row["id"]): row for row in effective_rows}
    global_acceptance_rows = [_copy(row, f"acceptance {row_id!r}") for row_id, row in sorted(acceptance.items())]
    for layer_id in order:
        dependencies = tuple(str(row) for row in sparse[layer_id]["jit"]["depends_on_layers"])
        unit_digests = tuple((row.unit_id, row.capsule_digest) for row in units_by_layer[layer_id])
        predecessor_digests = tuple((row, layer_digests[row]) for row in dependencies)
        projection = {
            "sparse_layer": _copy(sparse[layer_id], f"sparse layer {layer_id!r}"),
            "effective_layer": _copy(effective_by_id[layer_id], f"effective layer {layer_id!r}"),
            "unit_capsules": [{"unit_id": key, "digest": value} for key, value in unit_digests],
            "predecessor_layers": [{"layer_id": key, "digest": value} for key, value in predecessor_digests],
            "contracts": [
                {"kind": kind, "row": _copy(row, f"layer {layer_id!r} contract")}
                for kind, row in contracts_by_owner[layer_id]
            ],
            "requirements": [
                {
                    "global_row": _copy(global_requirements[key], f"global requirement {key!r}"),
                    "effective_row": _copy(requirements[key], f"effective requirement {key!r}"),
                }
                for key in sorted(requirements)
                if layer_id in requirement_owners[key]
            ],
            "judgment_debt_definitions": [
                row.as_dict() for row in sorted(definitions_by_owner[layer_id], key=lambda row: row.digest)
            ],
            # Deliberately payer-only: a later activation never changes the semantic owner.
            "judgment_debt_activations": [
                row.as_dict() for row in sorted(activations_by_payer[layer_id], key=lambda row: row.digest)
            ],
            # Current acceptance rows have no typed owner; never infer one from frame/title.
            "acceptance": global_acceptance_rows,
        }
        payload = {
            "schema": LAYER_AUTHORITY_CAPSULE_SCHEMA, "layer_id": layer_id,
            "unit_capsules": projection["unit_capsules"],
            "predecessor_layers": projection["predecessor_layers"], "projection": projection,
        }
        capsule_digest = _digest(payload, f"layer {layer_id!r}.capsule")
        layer_capsules.append(
            LayerAuthorityCapsule(
                layer_id, unit_digests, predecessor_digests, capsule_digest,
                _bytes(projection, f"layer {layer_id!r}.projection").decode(),
            )
        )
        layer_digests[layer_id] = capsule_digest

    schemas = tuple((name, str(_SCHEMAS[name])) for name in OVERLAY_ARTIFACTS)
    global_digest = _digest({"documents": global_docs}, "global authority")
    effective_digest = _digest({"documents": effective_docs}, "effective authority")
    set_payload = {
        "schema": AUTHORITY_CAPSULE_SET_SCHEMA,
        "provenance": {
            "global_authority_digest": global_digest,
            "effective_authority_digest": effective_digest,
            "document_schemas": dict(schemas),
        },
        "units": [row.as_dict() for row in units],
        "layers": [row.as_dict() for row in layer_capsules],
    }
    return AuthorityCapsuleSet(
        global_digest, effective_digest, schemas, tuple(units), tuple(layer_capsules),
        _digest(set_payload, "authority capsule set"),
    )


__all__ = [
    "AUTHORITY_CAPSULE_SET_SCHEMA",
    "LAYER_AUTHORITY_CAPSULE_SCHEMA",
    "UNIT_AUTHORITY_CAPSULE_SCHEMA",
    "AuthorityCapsuleError",
    "AuthorityCapsuleSet",
    "LayerAuthorityCapsule",
    "UnitAuthorityCapsule",
    "compile_authority_capsules",
]
