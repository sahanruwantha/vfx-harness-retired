"""Typed provisional-debt loading and composed canonical judgment (HIR-0163)."""

from __future__ import annotations

from types import SimpleNamespace

from vfx_harness.domain.work_units import MutationScope
from vfx_harness.orchestration import judgment_debt_state


def _load_provisional_decisions(
    shot,
    layer_id: str,
    *,
    state_loader=None,
) -> tuple[dict, ...]:
    """Return exact current-generation debts due at one replay boundary."""
    rows: list[dict] = []
    loader = state_loader or judgment_debt_state.current_judgment_debt_states
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
                "authority; consume its typed finding through replan before rebuilding"
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


def _composition_judge_unit(layer, provisional_decisions=()):
    """Compile one local composed judge unit, optionally paying one typed debt."""
    stages = tuple(getattr(layer, "stages", ()) or ())
    if not stages:
        return None
    provisional_decisions = tuple(provisional_decisions or ())
    if any(tuple(getattr(unit, "look_capabilities", ()) or ()) for unit in stages) and not provisional_decisions:
        return None
    unit_claims = tuple(claim for unit in stages for claim in (unit.evaluation.claims or ()))
    required = [claim for claim in unit_claims if claim.required]
    if not required and not provisional_decisions:
        return None
    if any(claim.authority != "executable_required" for claim in required) and not provisional_decisions:
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
    if len(media) > 1:
        raise ValueError(
            "one composed judgment unit cannot mix observation media; schedule each typed debt independently"
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
