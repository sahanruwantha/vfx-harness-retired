"""Typed provisional-debt loading and composed canonical judgment (HIR-0163)."""

from __future__ import annotations

from functools import partial
from types import SimpleNamespace

from vfx_harness.domain.judgment_debt_models import unit_observation_medium
from vfx_harness.domain.work_units import MutationScope, composed_evaluation_is_lookless
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



def _image_bound_claim_ids(unit) -> tuple[str, ...]:
    """Required claims of ``unit`` bound to an image contract, which owe a plate."""
    return tuple(
        str(claim.id)
        for claim in (getattr(getattr(unit, "evaluation", None), "claims", ()) or ())
        if getattr(claim, "required", False)
        and any(
            str(getattr(row, "kind", "")) == "image_contract"
            for row in (getattr(claim, "evidence", ()) or ())
        )
    )


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

def _composition_judge_unit(layer, provisional_decisions=()):
    """Compile one local composed judge unit, optionally paying one typed debt."""
    stages = tuple(getattr(layer, "stages", ()) or ())
    if not stages:
        return None
    provisional_decisions = tuple(provisional_decisions or ())
    # No look capabilities, some required claim, all of them executable_required: the
    # same predicate the materialization validator and the plan gate demand layer judge
    # coverage under, so a layer cannot be refused for a coverage a critic would supply
    # (HIR-0238).
    if not provisional_decisions and not composed_evaluation_is_lookless(stages):
        return None
    unit_claims = tuple(claim for unit in stages for claim in (unit.evaluation.claims or ()))

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
                SimpleNamespace(
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
                    evidence=(SimpleNamespace(kind="qualification", id=binding_id),),
                    binding_ids=(binding_id,),
                )
            )
    claims = (*unit_claims, *qualitative)
    # A unit's bound image contracts were paid on that unit's own plate, so re-measuring
    # them on this group's plate compares a threshold calibrated in one medium against a
    # measurement in another. That medium requirement is implicit -- it is whatever
    # _unit_raster_mode gave the unit -- so it never entered `media` and the mix below
    # could not see it. hansa_silk_road layer 2 failed a frame_detail debt at 1.826 on a
    # Workbench solid plate that its unit had paid at 5.266 on EEVEE, because the group's
    # medium came from an unrelated workbench_solid debt (HIR-0241).
    for unit in stages:
        if not _image_bound_claim_ids(unit):
            continue
        media.add(unit_observation_medium(unit))
    if len(media) > 1:
        raise ValueError(
            "one composed judgment unit cannot mix observation media "
            f"({', '.join(sorted(media))}); "
            + _mixed_media_detail(stages, provisional_decisions)
            + ". Schedule each typed debt independently, or give the debt the medium its "
            "layer's image contracts were paid in -- a contract is re-measured in the "
            "medium it was paid in or not at all"
        )
    judges = tuple(
        SimpleNamespace(frame=int(frame), ref=ref) for frame, ref in dict.fromkeys((*layer_points, *debt_points))
    )
    return SimpleNamespace(
        id=f"{getattr(layer, 'id', 'layer')}._composition",
        look_capabilities=tuple(
            dict.fromkeys(
                capability for unit in stages for capability in (getattr(unit, "look_capabilities", ()) or ())
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
        judgment_observation_medium=next(iter(media), None),
        worklist_units=stages,
        mutates=MutationScope(
            mode="scoped",
            roles=roles,
            controls=controls,
            script_spans=(),
        ),
    )
