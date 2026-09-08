"""Typed provisional-debt loading and composed canonical judgment (HIR-0163)."""

from __future__ import annotations

from dataclasses import asdict
from functools import partial
from types import SimpleNamespace

from vfx_harness.domain.judgment_debt_models import unit_observation_medium
from vfx_harness.domain.work_units import MutationScope, composed_evaluation_is_lookless
from vfx_harness.domain.work_units.claims import Claim, EvidenceBinding
from vfx_harness.orchestration import judgment_debt_state


def _load_provisional_decisions(
    shot,
    layer_id: str,
    *,
    state_loader=None,
    selected_authority=None,
) -> tuple[dict, ...]:
    """Return exact current-generation debts due at one replay boundary."""
    rows: list[dict] = []
    if state_loader is not None:
        loader = state_loader
    elif selected_authority is None:
        loader = judgment_debt_state.current_judgment_debt_states
    else:
        loader = partial(
            judgment_debt_state.current_judgment_debt_states_for_authority,
            selected_authority=selected_authority,
        )
    for definition, activation, state in loader(shot.folder):
        if definition.binding.activates_at != str(layer_id):
            continue
        if activation is None:
            raise ValueError(
                f"judgment debt {definition.debt_id} is due at layer {layer_id} but "
                "selected authority has no exact payer activation"
            )
        activation.assert_matches(definition)
        if state.status == "satisfied":
            continue
        if state.status == "falsified":
            raise ValueError(
                f"judgment debt {definition.debt_id} is falsified under current "
                "authority; publish a validated authority amendment that covers its "
                "typed finding before rebuilding"
            )
        rows.append(
            {
                "id": definition.seed.requirement_id,
                "debt_id": definition.debt_id,
                "definition_digest": definition.digest,
                "activation_digest": activation.digest,
                "statement": definition.seed.statement,
                "decision_strength": definition.seed.decision_strength,
                "evidence_domains": ("image",),
                "claim_kind": definition.seed.claim_kind,
                "property": definition.seed.property,
                "fault_owner": definition.seed.fault_owner,
                "subject_roles": definition.seed.subject_roles,
                "axes": definition.seed.axes,
                "judge_points": tuple((point.frame, point.ref) for point in definition.seed.judge_points),
                "carrier_families": definition.seed.carrier_families,
                "observation_medium": definition.seed.observation_medium,
                "lifecycle": definition.seed.lifecycle,
                "state": state.status,
            }
        )
    return tuple(rows)



def _claim_is_image_bound(claim) -> bool:
    """A required claim bound to an image contract, so it owes a plate to be measured on."""
    if not getattr(claim, "required", False):
        return False
    return any(
        str(getattr(row, "kind", "")) == "image_contract"
        for row in (getattr(claim, "evidence", ()) or ())
    )


def _image_bound_claim_ids(unit) -> tuple[str, ...]:
    """Required claims of ``unit`` bound to an image contract, which owe a plate."""
    return tuple(
        str(claim.id)
        for claim in (getattr(getattr(unit, "evaluation", None), "claims", ()) or ())
        if _claim_is_image_bound(claim)
    )


def _layer_look_medium(stages) -> str:
    """The medium a layer's own plate is rendered in when no debt names one."""
    union = tuple(
        dict.fromkeys(
            capability for unit in stages for capability in (getattr(unit, "look_capabilities", ()) or ())
        )
    )
    return unit_observation_medium(SimpleNamespace(look_capabilities=union))


def _decision_medium(decision) -> str | None:
    """A typed debt names its medium; an untyped requirement decision does not."""
    if not decision.get("debt_id"):
        return None
    return str(decision["observation_medium"])


def composed_group_plans(layer, provisional_decisions=()):
    """The composed groups a layer owes: one per debt, plus one per uncovered medium.

    A group renders one plate, and a contract is re-measured in the medium it was paid
    in or not at all (HIR-0241).  So a layer whose debt is judged in Workbench solid and
    whose units paid image contracts in EEVEE owes two groups, not one -- and the second
    is not a second opinion, it is the only place those contracts can be evaluated.

    Returns ``((decisions, medium), ...)``; ``medium`` is ``None`` where the group takes
    the layer's own look-derived plate, which is the behaviour that predates this.
    """
    decisions = tuple(provisional_decisions or ())
    stages = tuple(getattr(layer, "stages", ()) or ())
    if not decisions:
        return (((), None),)
    plans: list[tuple[tuple, str | None]] = [((decision,), _decision_medium(decision)) for decision in decisions]
    look_medium = _layer_look_medium(stages)
    covered = {medium or look_medium for _decisions, medium in plans}
    owed = {unit_observation_medium(unit) for unit in stages if _image_bound_claim_ids(unit)}
    for medium in sorted(owed - covered):
        plans.append(((), medium))
    uncovered = owed - {medium or look_medium for _decisions, medium in plans}
    if uncovered:
        raise ValueError(
            "composed groups cover no plate for image contracts paid in "
            + ", ".join(sorted(uncovered))
        )
    return tuple(plans)


def _mixed_media_detail(stages, provisional_decisions) -> str:
    """Name every side of the mix, so the refusal says what to change."""
    parts: list[str] = []
    for decision in provisional_decisions:
        identity = decision.get("debt_id") or decision.get("id")
        medium = decision.get("observation_medium")
        if medium:
            parts.append(f"debt {identity} declares {medium}")
    for unit in stages:
        bound = _image_bound_claim_ids(unit)
        if bound:
            parts.append(
                f"unit {getattr(unit, 'id', '?')} pays {', '.join(bound)} in "
                f"{unit_observation_medium(unit)}"
            )
    return "; ".join(parts)

def _composition_judge_unit(layer, provisional_decisions=(), *, medium=None, qualified_debt_claims=()):
    """Compile one local composed judge unit, optionally paying one typed debt.

    ``medium`` names the plate this group renders when no debt does -- the second group a
    layer owes when its debt is judged in one medium and its units paid image contracts in
    another (HIR-0241).  Such a group takes no look vote: it exists to measure executable
    contracts on the plate they were paid on.
    """
    if qualified_debt_claims and not provisional_decisions:
        raise ValueError("selected debt qualification requires an owning debt group")
    if qualified_debt_claims and any(not decision.get("debt_id") for decision in provisional_decisions):
        raise ValueError("selected debt qualification requires typed judgment-debt identity")
    stages = tuple(getattr(layer, "stages", ()) or ())
    if not stages:
        if qualified_debt_claims:
            raise ValueError("selected debt qualification requires the owning layer's units")
        return None
    provisional_decisions = tuple(provisional_decisions or ())
    # No look capabilities, some required claim, all of them executable_required: the
    # same predicate the materialization validator and the plan gate demand layer judge
    # coverage under, so a layer cannot be refused for a coverage a critic would supply
    # (HIR-0238). A group compiled for an explicit medium is exempt: it carries no
    # qualitative claim and cannot reach a critic.
    if not provisional_decisions and medium is None and not composed_evaluation_is_lookless(stages):
        return None

    roles = tuple(dict.fromkeys(role for unit in stages for role in unit.mutates.roles))
    controls = tuple(dict.fromkeys(control for unit in stages for control in unit.mutates.controls))
    layer_points = tuple(getattr(layer, "judges", ()) or ())
    qualitative = []
    debt_points: list[tuple[int, str]] = []
    media: set[str] = set()
    for decision in provisional_decisions:
        typed = bool(decision.get("debt_id"))
        decision_axes = tuple(decision.get("axes") or getattr(layer, "owns", ()) or ("reference_match",))
        decision_roles = tuple(decision.get("subject_roles") or roles)
        decision_points = tuple(decision.get("judge_points") or layer_points)
        decision_moments = tuple(int(frame) for frame, _ref in decision_points)
        debt_points.extend((int(frame), str(ref)) for frame, ref in decision_points)
        if typed:
            media.add(str(decision["observation_medium"]))
        for axis in decision_axes:
            binding_id = (
                f"judgment-debt:{decision['debt_id']}:{axis}" if typed else f"requirement:{decision['id']}:{axis}"
            )
            qualitative.append(
                Claim(
                    id=binding_id,
                    proposition=decision["statement"],
                    axis=str(axis),
                    property=str(decision.get("property") or "reference_identity"),
                    subject_roles=decision_roles,
                    subject_controls=controls,
                    moments=decision_moments,
                    kind=str(decision.get("claim_kind") or "atomic"),
                    required=True,
                    authority="qualified_qualitative_required",
                    repair_owner=str(decision.get("fault_owner") or f"{getattr(layer, 'id', 'layer')}._composition"),
                    asserts="image",
                    evidence=(EvidenceBinding(kind="qualification", id=binding_id),),
                )
            )
    if qualified_debt_claims:
        if any(not isinstance(claim, Claim) or claim.qualification is None for claim in qualified_debt_claims):
            raise ValueError("selected debt qualification requires explicit artifact-bound claims")
        selected = {claim.id: claim for claim in qualified_debt_claims}
        if len(selected) != len(qualified_debt_claims) or set(selected) != {claim.id for claim in qualitative}:
            raise ValueError("selected debt qualification must cover exactly the group's debt claims")
        for claim in qualitative:
            candidate = asdict(selected[claim.id])
            candidate["qualification"] = None
            if candidate != asdict(claim):
                raise ValueError(
                    f"selected debt qualification changes owning claim {claim.id}; requalify its exact scope"
                )
        qualitative = [selected[claim.id] for claim in qualitative]
    # The plate this group renders. A contract is re-measured in the medium it was paid
    # in or not at all, so an image-bound claim from a unit judged in another medium
    # belongs to that medium's group, not to this one (HIR-0241).
    effective = medium or next(iter(media), None) or _layer_look_medium(stages)
    unit_claims = tuple(
        claim
        for unit in stages
        for claim in (unit.evaluation.claims or ())
        if not _claim_is_image_bound(claim) or unit_observation_medium(unit) == effective
    )
    claims = (*unit_claims, *qualitative)
    if len(media) > 1:
        raise ValueError(
            "one composed judgment unit cannot mix observation media "
            f"({', '.join(sorted(media))}); "
            + _mixed_media_detail(stages, provisional_decisions)
            + ". Schedule each typed debt independently"
        )
    judges = tuple(
        SimpleNamespace(frame=int(frame), ref=ref) for frame, ref in dict.fromkeys((*layer_points, *debt_points))
    )
    return SimpleNamespace(
        id=f"{getattr(layer, 'id', 'layer')}._composition",
        # A medium-only group measures executable contracts on the plate they were paid
        # on and takes no look vote, so it declares no look capability (HIR-0241).
        look_capabilities=(
            ()
            if medium is not None and not provisional_decisions
            else tuple(
                dict.fromkeys(
                    capability for unit in stages for capability in (getattr(unit, "look_capabilities", ()) or ())
                )
            )
        ),
        evaluation=SimpleNamespace(
            claims=claims,
            judges=judges,
            composition_context=None,
        ),
        provisional_requirement_ids=tuple(str(decision["id"]) for decision in provisional_decisions),
        provisional_decisions=provisional_decisions,
        provisional_debt_ids=tuple(
            str(decision.get("debt_id") or "") for decision in provisional_decisions if decision.get("debt_id")
        ),
        judgment_observation_medium=medium or next(iter(media), None),
        worklist_units=stages,
        mutates=MutationScope(
            mode="scoped",
            roles=roles,
            controls=controls,
            script_spans=(),
        ),
    )
