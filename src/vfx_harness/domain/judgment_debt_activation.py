"""DAG compilation and lifecycle transitions for qualitative judgment debt."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from vfx_harness.domain.judgment_debt_models import (
    RENDERED_CARRIER_FAMILIES,
    TERMINAL_JUDGMENT_DEBT_STATUSES,
    JudgmentDebtActivation,
    JudgmentDebtDefinition,
    JudgmentDebtSeed,
    JudgmentDebtState,
    JudgmentProvider,
    JudgmentProviderBinding,
    ProviderActivation,
    _input_text_sequence,
    _provider_rows,
    _require_digest,
    _require_roles,
    _require_text,
    _require_text_tuple,
    _subject_matches_provider,
    _validate_digest_pairs,
)


def _validated_layer_graph(
    layer_dependencies: Mapping[str, Sequence[str]],
    layer_order: Sequence[str],
) -> tuple[tuple[str, ...], dict[str, frozenset[str]]]:
    if not isinstance(layer_dependencies, Mapping) or not layer_dependencies:
        raise ValueError("layer_dependencies must be a non-empty mapping")
    graph: dict[str, tuple[str, ...]] = {}
    for layer_id, raw_dependencies in layer_dependencies.items():
        _require_text(layer_id, "layer_dependencies layer id")
        if isinstance(raw_dependencies, (str, bytes)) or not isinstance(raw_dependencies, Sequence):
            raise ValueError(f"layer_dependencies[{layer_id!r}] must be a sequence of layer ids")
        graph[layer_id] = _require_text_tuple(
            tuple(raw_dependencies),
            f"layer_dependencies[{layer_id!r}]",
            allow_empty=True,
        )
    unknown = sorted(
        {dependency for dependencies in graph.values() for dependency in dependencies if dependency not in graph}
    )
    if unknown:
        raise ValueError("layer dependency DAG names unknown layer(s): " + ", ".join(unknown))
    order = _input_text_sequence(layer_order, "layer_order")
    if set(order) != set(graph) or len(order) != len(graph):
        raise ValueError("layer_order must contain every layer id exactly once")
    position = {layer_id: index for index, layer_id in enumerate(order)}
    remaining = {layer_id: set(dependencies) for layer_id, dependencies in graph.items()}
    topological: list[str] = []
    while remaining:
        ready = sorted(
            (layer_id for layer_id, dependencies in remaining.items() if not dependencies),
            key=position.__getitem__,
        )
        if not ready:
            raise ValueError(
                "layer dependency DAG is cyclic: " + ", ".join(sorted(remaining, key=position.__getitem__))
            )
        topological.extend(ready)
        for layer_id in ready:
            remaining.pop(layer_id)
        for dependencies in remaining.values():
            dependencies.difference_update(ready)
    closures: dict[str, frozenset[str]] = {}
    for layer_id in topological:
        closure: set[str] = set()
        frontier = list(graph[layer_id])
        while frontier:
            dependency = frontier.pop()
            if dependency not in closure:
                closure.add(dependency)
                frontier.extend(graph[dependency])
        closures[layer_id] = frozenset(closure)
    return tuple(topological), closures


def compile_provider_activation(
    subject_roles: Sequence[str],
    carrier_families: Sequence[str],
    owner_layer: str,
    providers: Sequence[JudgmentProvider],
    *,
    layer_dependencies: Mapping[str, Sequence[str]],
    layer_order: Sequence[str],
) -> ProviderActivation:
    """Select the earliest owner-or-successor prefix covering every subject."""
    subjects = _input_text_sequence(subject_roles, "subject_roles")
    _require_roles(subjects, "subject_roles")
    families = _input_text_sequence(carrier_families, "carrier_families")
    unknown_families = sorted(set(families) - RENDERED_CARRIER_FAMILIES)
    if unknown_families:
        raise ValueError("carrier_families contains unsupported families: " + ", ".join(unknown_families))
    _require_text(owner_layer, "owner_layer")
    provider_rows = _provider_rows(providers, "providers")
    topological, closures = _validated_layer_graph(layer_dependencies, layer_order)
    if owner_layer not in closures:
        raise ValueError(f"provider activation owner layer {owner_layer!r} is absent from the selected DAG")
    unknown_layers = sorted({provider.layer_id for provider in provider_rows if provider.layer_id not in closures})
    if unknown_layers:
        raise ValueError("providers name layers absent from the selected DAG: " + ", ".join(unknown_layers))
    candidates = tuple(
        layer_id for layer_id in topological if layer_id == owner_layer or owner_layer in closures[layer_id]
    )
    chosen: tuple[str, tuple[tuple[str, tuple[str, ...]], ...], tuple[JudgmentProvider, ...]] | None = None
    for layer_id in candidates:
        prefix = set(closures[layer_id]) | {layer_id}
        bindings: list[tuple[str, tuple[str, ...]]] = []
        relevant_ids: set[str] = set()
        for subject in subjects:
            matching = tuple(
                sorted(
                    provider.id
                    for provider in provider_rows
                    if provider.layer_id in prefix
                    and provider.carrier_family in families
                    and any(_subject_matches_provider(subject, role) for role in provider.subject_roles)
                )
            )
            if not matching:
                break
            bindings.append((subject, matching))
            relevant_ids.update(matching)
        else:
            relevant = tuple(
                sorted(
                    (provider for provider in provider_rows if provider.id in relevant_ids),
                    key=lambda provider: provider.id,
                )
            )
            chosen = (layer_id, tuple(sorted(bindings)), relevant)
            break
    if chosen is None:
        payable_layers = set().union(*(set(closures[layer_id]) | {layer_id} for layer_id in candidates))
        uncovered: list[str] = []
        unreachable: dict[str, list[str]] = {}
        for subject in subjects:
            matching = [
                provider
                for provider in provider_rows
                if provider.carrier_family in families
                and any(_subject_matches_provider(subject, role) for role in provider.subject_roles)
            ]
            if not matching:
                uncovered.append(subject)
            else:
                outside = sorted(provider.id for provider in matching if provider.layer_id not in payable_layers)
                if outside:
                    unreachable[subject] = outside
        detail: list[str] = []
        if uncovered:
            detail.append("no matching provider promise exists for " + ", ".join(uncovered))
        if unreachable:
            detail.append(
                "matching provider promise(s) exist outside the owner's dependency predecessor/successor prefixes: "
                + ", ".join(f"{subject}={ids}" for subject, ids in sorted(unreachable.items()))
            )
        raise ValueError("no dependency-complete matching reachable rendered-carrier prefix; " + "; ".join(detail))
    activates_at, bindings, relevant = chosen
    return ProviderActivation(owner_layer, subjects, families, activates_at, bindings, relevant)


def compile_judgment_debt(
    seed: JudgmentDebtSeed,
    providers: Sequence[JudgmentProvider],
    *,
    layer_dependencies: Mapping[str, Sequence[str]],
    layer_order: Sequence[str],
) -> JudgmentDebtDefinition:
    if not isinstance(seed, JudgmentDebtSeed):
        raise ValueError("seed must be a JudgmentDebtSeed")
    activation = compile_provider_activation(
        seed.subject_roles,
        seed.carrier_families,
        seed.owner_layer,
        providers,
        layer_dependencies=layer_dependencies,
        layer_order=layer_order,
    )
    binding = JudgmentProviderBinding(
        seed.digest,
        activation.activates_at,
        activation.subject_provider_ids,
        tuple((provider.id, provider.digest) for provider in activation.providers),
    )
    return JudgmentDebtDefinition(seed, activation.providers, binding)


def validate_judgment_debt_replay_prefix(
    activation: JudgmentDebtActivation,
    replayed_unit_digests: Sequence[tuple[str, str]],
) -> None:
    """Prove the current cumulative replay includes every exact payer artifact."""
    if not isinstance(activation, JudgmentDebtActivation):
        raise ValueError("replay-prefix validation requires a JudgmentDebtActivation")
    if isinstance(replayed_unit_digests, (str, bytes)) or not isinstance(replayed_unit_digests, Sequence):
        raise ValueError("replayed_unit_digests must be a sequence of (unit_id, digest) pairs")
    replayed = tuple(replayed_unit_digests)
    _validate_digest_pairs(replayed, "replayed_unit_digests", allow_empty=True)
    expected = dict(activation.payer_unit_digests)
    observed = dict(replayed)
    missing = sorted(set(expected) - set(observed))
    stale = sorted(
        unit_id for unit_id, digest in expected.items() if unit_id in observed and observed[unit_id] != digest
    )
    if missing or stale:
        detail: list[str] = []
        if missing:
            detail.append("missing=" + ", ".join(missing))
        if stale:
            detail.append("stale=" + ", ".join(stale))
        raise ValueError("judgment debt replay prefix does not contain exact payer units: " + "; ".join(detail))


def activate_judgment_debt(
    definition: JudgmentDebtDefinition,
    state: JudgmentDebtState,
    *,
    activation: JudgmentDebtActivation,
    layer_id: str,
    replayed_unit_digests: Sequence[tuple[str, str]],
) -> JudgmentDebtState:
    if not isinstance(definition, JudgmentDebtDefinition) or not isinstance(state, JudgmentDebtState):
        raise ValueError("activation requires JudgmentDebtDefinition and JudgmentDebtState values")
    if state.definition_digest != definition.digest:
        raise ValueError("judgment debt state does not match the definition digest")
    if state.status != "pending_not_due":
        raise ValueError(f"judgment debt activation requires pending_not_due, found {state.status}")
    if not isinstance(activation, JudgmentDebtActivation):
        raise ValueError("judgment debt activation requires a JudgmentDebtActivation")
    activation.assert_matches(definition)
    _require_text(layer_id, "layer_id")
    if layer_id != definition.binding.activates_at:
        raise ValueError(f"judgment debt activates_at={definition.binding.activates_at!r}, not layer {layer_id!r}")
    if activation.payer_layer != layer_id:
        raise ValueError(f"judgment debt activation payer_layer={activation.payer_layer!r}, not layer {layer_id!r}")
    validate_judgment_debt_replay_prefix(activation, replayed_unit_digests)
    return JudgmentDebtState(definition.digest, "due", activation_digest=activation.digest)


def resolve_judgment_debt(
    definition: JudgmentDebtDefinition,
    state: JudgmentDebtState,
    *,
    outcome: str,
    evidence_digest: str,
) -> JudgmentDebtState:
    if not isinstance(definition, JudgmentDebtDefinition) or not isinstance(state, JudgmentDebtState):
        raise ValueError("resolution requires JudgmentDebtDefinition and JudgmentDebtState values")
    if state.definition_digest != definition.digest:
        raise ValueError("judgment debt state does not match the definition digest")
    if state.status != "due":
        raise ValueError(f"judgment debt resolution requires due, found {state.status}")
    if outcome not in TERMINAL_JUDGMENT_DEBT_STATUSES:
        raise ValueError("judgment debt outcome must be satisfied or falsified")
    _require_digest(evidence_digest, "evidence_digest")
    return JudgmentDebtState(
        definition.digest,
        outcome,
        evidence_digest,
        state.activation_digest,
    )
