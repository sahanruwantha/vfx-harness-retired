"""Compile provisional qualitative debt into selected JIT authority (HIR-0163)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from vfx_harness.domain.atomicity import role_namespace, write_clusters
from vfx_harness.domain.judgment_debts import (
    JUDGMENT_DEBT_CLAIM_KINDS,
    JUDGMENT_DEBT_LIFECYCLES,
    JUDGMENT_DEBT_PROPERTIES,
    OBSERVATION_MEDIA,
    RENDERED_CARRIER_FAMILIES,
    JudgmentDebtActivation,
    JudgmentDebtDefinition,
    JudgmentDebtSeed,
    JudgmentPoint,
    JudgmentProvider,
    compile_judgment_debt,
    compile_provider_activation,
)
from vfx_harness.domain.work_units import (
    allowed_unit_provides,
    plan_selector_declared,
    sparse_layer_dependencies,
    topological_sparse_layer_ids,
)
from vfx_harness.orchestration.ledger import Layer
from vfx_harness.orchestration.unit_state import unit_digest


@dataclass(frozen=True, slots=True)
class ExactProvider:
    provider: JudgmentProvider
    payer_unit_id: str
    payer_unit_digest: str


def _strings(value: Any, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{where} must be a non-empty list")
    rows = tuple(str(item).strip() for item in value)
    if any(not row for row in rows) or len(rows) != len(set(rows)):
        raise ValueError(f"{where} must contain unique non-empty strings")
    return rows


def _moments(value: Any, where: str) -> tuple[int, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{where} must be a non-empty list")
    if any(isinstance(item, bool) or not isinstance(item, int) or item < 1 for item in value):
        raise ValueError(f"{where} must contain positive integers")
    rows = tuple(value)
    if len(rows) != len(set(rows)):
        raise ValueError(f"{where} contains duplicates")
    return rows


def _sparse_roles(row: Mapping[str, Any]) -> tuple[str, ...]:
    jit = row.get("jit") if isinstance(row.get("jit"), Mapping) else {}
    values = jit.get("reserved_roles") if isinstance(jit, Mapping) else None
    if not isinstance(values, list):
        return ()
    return tuple(str(value).strip() for value in values if str(value).strip())


def _family_roles(unit: Any, family: str, scene_rows: Sequence[Mapping[str, Any]], units) -> tuple[str, ...]:
    roles = tuple(str(value) for value in unit.mutates.roles if str(value))
    if family == "mesh" and "geometry" in unit.provides:
        return roles
    namespaces = {
        cluster.role_namespace
        for cluster in write_clusters(unit, scene_rows, units=units)
        if cluster.instrument_family == family
    }
    return tuple(role for role in roles if role_namespace(role) in namespaces)


def exact_judgment_providers(
    layers: Mapping[str, Layer],
    scene_rows: Sequence[Mapping[str, Any]],
) -> tuple[ExactProvider, ...]:
    """Exact carrier witnesses from every materialized layer in the consumer view."""
    rows: list[ExactProvider] = []
    for layer_id, layer in layers.items():
        if layer.execution != "ready":
            continue
        for unit in layer.stages:
            families = {
                cluster.instrument_family
                for cluster in write_clusters(unit, scene_rows, units=layer.stages)
                if cluster.instrument_family in RENDERED_CARRIER_FAMILIES
            }
            if "geometry" in unit.provides:
                families.add("mesh")
            for family in sorted(families):
                roles = _family_roles(unit, family, scene_rows, layer.stages)
                if not roles:
                    continue
                provider = JudgmentProvider(
                    id=f"unit:{layer_id}:{unit.id}:{family}",
                    layer_id=str(layer_id),
                    carrier_family=family,
                    subject_roles=roles,
                )
                rows.append(
                    ExactProvider(
                        provider=provider,
                        payer_unit_id=f"{layer_id}:{unit.id}",
                        payer_unit_digest=unit_digest(unit),
                    )
                )
    return tuple(rows)


def judgment_provider_promises(
    global_layers: Sequence[Mapping[str, Any]],
    parsed_layers: Mapping[str, Layer],
    scene_rows: Sequence[Mapping[str, Any]],
) -> tuple[JudgmentProvider, ...]:
    """Exact ready providers plus sparse future mesh promises from selected authority."""
    providers = [row.provider for row in exact_judgment_providers(parsed_layers, scene_rows)]
    parsed_by_id = {str(layer_id): layer for layer_id, layer in parsed_layers.items()}
    for row in global_layers:
        layer_id = str(row.get("id") or "")
        parsed = parsed_by_id.get(layer_id)
        if not layer_id or (parsed is not None and parsed.execution == "ready"):
            continue
        roles = _sparse_roles(row)
        if roles and "geometry" in allowed_unit_provides(row):
            providers.append(
                JudgmentProvider(
                    id=f"sparse-layer:{layer_id}:mesh",
                    layer_id=layer_id,
                    carrier_family="mesh",
                    subject_roles=roles,
                )
            )
    return tuple(providers)


def _enum(value: Any, allowed: frozenset[str], where: str) -> str:
    text = str(value or "").strip()
    if text not in allowed:
        raise ValueError(f"{where} must be one of {sorted(allowed)}")
    return text


def compile_materialized_judgment_definition(
    judgment: Any,
    *,
    requirement_id: str,
    statement: str,
    decision_strength: str,
    layer: Layer,
    global_layer: Mapping[str, Any],
    global_layers: Sequence[Mapping[str, Any]],
    parsed_layers: Mapping[str, Layer],
    scene_rows: Sequence[Mapping[str, Any]],
    bundle_digest: str,
) -> JudgmentDebtDefinition:
    """Validate model-authored semantics and compile harness-owned activation."""
    if not isinstance(judgment, Mapping):
        raise ValueError("decision.judgment must be an object")
    expected = {
        "claim_kind",
        "property",
        "fault_owner",
        "subject_roles",
        "axes",
        "moments",
        "carrier_families",
        "observation_medium",
        "lifecycle",
    }
    if set(judgment) != expected:
        raise ValueError(
            "decision.judgment fields mismatch; missing="
            f"{sorted(expected - set(judgment))}; unexpected={sorted(set(judgment) - expected)}"
        )
    claim_kind = _enum(judgment.get("claim_kind"), JUDGMENT_DEBT_CLAIM_KINDS, "decision.judgment.claim_kind")
    property_kind = _enum(judgment.get("property"), JUDGMENT_DEBT_PROPERTIES, "decision.judgment.property")
    fault_owner = str(judgment.get("fault_owner") or "").strip()
    by_unit = {unit.id: unit for unit in layer.stages}
    if fault_owner not in by_unit:
        raise ValueError(
            "decision.judgment.fault_owner must name an exact unit in the semantic owner "
            f"layer; found {fault_owner!r}, legal ids={sorted(by_unit)}"
        )
    subjects = _strings(judgment.get("subject_roles"), "decision.judgment.subject_roles")
    axes = _strings(judgment.get("axes"), "decision.judgment.axes")
    if len(axes) != 1:
        raise ValueError("decision.judgment.axes must contain exactly one independently payable axis")
    foreign_axes = sorted(set(axes) - set(layer.owns))
    if foreign_axes:
        raise ValueError("decision.judgment.axes escape the semantic owner's declared axes: " + ", ".join(foreign_axes))
    moments = _moments(judgment.get("moments"), "decision.judgment.moments")
    refs = dict(layer.judges)
    foreign_moments = sorted(set(moments) - set(refs))
    if foreign_moments:
        raise ValueError(
            "decision.judgment.moments escape the semantic owner's judge moments: "
            + ", ".join(map(str, foreign_moments))
        )
    families = _strings(judgment.get("carrier_families"), "decision.judgment.carrier_families")
    unknown_families = sorted(set(families) - RENDERED_CARRIER_FAMILIES)
    if unknown_families:
        raise ValueError(
            "decision.judgment.carrier_families contains unsupported values: " + ", ".join(unknown_families)
        )
    medium = _enum(
        judgment.get("observation_medium"),
        OBSERVATION_MEDIA,
        "decision.judgment.observation_medium",
    )
    lifecycle = _enum(
        judgment.get("lifecycle"),
        JUDGMENT_DEBT_LIFECYCLES,
        "decision.judgment.lifecycle",
    )

    raw_provides = (global_layer.get("jit") or {}).get("provides") or {}
    camera_owner = isinstance(raw_provides, Mapping) and "camera" in raw_provides
    fault_unit = by_unit[fault_owner]
    if property_kind == "camera_framing":
        if not camera_owner or "camera" not in fault_unit.provides:
            raise ValueError(
                "camera_framing judgment must be owned by typed camera authority and "
                "repaired by a camera-providing unit"
            )
    else:
        if property_kind == "subject_appearance" and camera_owner:
            raise ValueError(
                "subject_appearance cannot be owned by a camera-providing sparse layer; "
                "repair the requirement owner instead of hiding misownership with activation"
            )
        if property_kind == "reference_identity" and camera_owner:
            raise ValueError(
                "reference_identity is ambiguous on a camera-providing layer; classify the "
                "proposition as camera_framing or move subject_appearance to its form/look owner"
            )
        mutable_subjects = (*fault_unit.mutates.roles, *fault_unit.mutates.dresses)
        foreign_subjects = tuple(
            subject for subject in subjects if not plan_selector_declared(subject, mutable_subjects)
        )
        if foreign_subjects:
            raise ValueError(
                f"{property_kind} judgment subjects escape fault owner {fault_owner!r} "
                "mutation/dressing authority: " + ", ".join(foreign_subjects)
            )

    seed = JudgmentDebtSeed(
        requirement_id=requirement_id,
        statement=statement,
        decision_strength=decision_strength,
        claim_kind=claim_kind,
        property=property_kind,
        owner_layer=str(layer.id),
        fault_owner=fault_owner,
        subject_roles=subjects,
        axes=axes,
        judge_points=tuple(JudgmentPoint(frame, refs[frame]) for frame in moments),
        observation_medium=medium,
        lifecycle=lifecycle,
        bundle_digest=bundle_digest,
        carrier_families=families,
    )
    graph = sparse_layer_dependencies(global_layers)
    return compile_judgment_debt(
        seed,
        judgment_provider_promises(global_layers, parsed_layers, scene_rows),
        layer_dependencies=graph,
        layer_order=topological_sparse_layer_ids(global_layers),
    )


def compile_materialized_judgment_activations(
    definitions: Sequence[JudgmentDebtDefinition],
    existing: Sequence[JudgmentDebtActivation],
    *,
    layer_id: str,
    global_layers: Sequence[Mapping[str, Any]],
    parsed_layers: Mapping[str, Layer],
    scene_rows: Sequence[Mapping[str, Any]],
) -> tuple[JudgmentDebtActivation, ...]:
    """Pin exact unit identities for every debt becoming due on this materialization."""
    exact = exact_judgment_providers(parsed_layers, scene_rows)
    providers = tuple(row.provider for row in exact)
    exact_by_provider = {row.provider.id: row for row in exact}
    graph = sparse_layer_dependencies(global_layers)
    order = topological_sparse_layer_ids(global_layers)
    existing_by_definition: dict[str, JudgmentDebtActivation] = {}
    for activation in existing:
        prior = existing_by_definition.get(activation.definition_digest)
        if prior is not None and prior.digest != activation.digest:
            raise ValueError(
                "conflicting judgment-debt activations exist for definition " + activation.definition_digest
            )
        existing_by_definition[activation.definition_digest] = activation

    compiled: list[JudgmentDebtActivation] = []
    for definition in definitions:
        if definition.binding.activates_at != str(layer_id):
            continue
        exact_binding = compile_provider_activation(
            definition.seed.subject_roles,
            definition.seed.carrier_families,
            definition.seed.owner_layer,
            providers,
            layer_dependencies=graph,
            layer_order=order,
        )
        if exact_binding.activates_at != definition.binding.activates_at:
            raise ValueError(
                f"judgment debt {definition.debt_id} promised activation at layer "
                f"{definition.binding.activates_at}, but exact materialized providers "
                f"activate at {exact_binding.activates_at}; repair selected authority"
            )
        payer_rows = tuple(
            sorted(
                {
                    (
                        exact_by_provider[provider_id].payer_unit_id,
                        exact_by_provider[provider_id].payer_unit_digest,
                    )
                    for provider_id in exact_binding.provider_ids
                }
            )
        )
        activation = JudgmentDebtActivation.for_definition(
            definition,
            payer_unit_digests=payer_rows,
        )
        prior = existing_by_definition.get(definition.digest)
        if prior is not None:
            prior.assert_matches(definition)
            if prior.digest != activation.digest:
                raise ValueError(
                    f"judgment debt {definition.debt_id} exact payer units changed; "
                    "rematerialize through a lineage transaction instead of appending "
                    "a conflicting activation"
                )
            continue
        compiled.append(activation)
    return tuple(compiled)
