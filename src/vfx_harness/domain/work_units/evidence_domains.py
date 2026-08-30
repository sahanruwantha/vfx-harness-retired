"""Evidence-domain vocabulary shared by layers, deferred owners, and claims."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from vfx_harness.domain.work_units.parsing import _strings

EVIDENCE_DOMAINS = frozenset({"scene", "image", "temporal", "projected_composition", "human"})
CLAIM_DOMAINS = EVIDENCE_DOMAINS
STRUCTURAL_CLAIM_DOMAINS = frozenset({"scene", "temporal", "projected_composition"})
REQUIREMENT_DOMAIN_COVERAGE_FIX = (
    "assign an owner whose evidence_domains cover every declared domain, or split "
    "the row so each fragment's domains match one owner; do not infer domains from "
    "brief keywords and do not stamp a domain onto a layer that does not own it"
)


def parse_evidence_domains(value: Any, where: str) -> tuple[str, ...]:
    """Parse a non-empty unique subset of ``EVIDENCE_DOMAINS``, sorted for teaching."""
    accepted = sorted(EVIDENCE_DOMAINS)
    try:
        tokens = _strings(value, where, allow_empty=False)
    except ValueError as exc:
        raise ValueError(f"{exc}; accepted {accepted}") from None
    unknown = sorted(set(tokens) - EVIDENCE_DOMAINS)
    if unknown:
        raise ValueError(f"{where} unknown evidence domains: {', '.join(unknown)}; accepted {accepted}")
    return tuple(sorted(tokens))


def uncovered_evidence_domains(declared: Sequence[str], owner_domains: Sequence[str]) -> tuple[str, ...]:
    return tuple(sorted(frozenset(declared) - frozenset(owner_domains)))


def layers_covering_evidence_domains(
    declared: Sequence[str],
    layer_domains: Mapping[str, Sequence[str]],
) -> tuple[str, ...]:
    """Return layer ids whose declared domains cover every required domain (AND)."""
    needed = frozenset(declared)
    if not needed:
        return ()
    return tuple(layer_id for layer_id, domains in layer_domains.items() if needed <= frozenset(domains))


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
