"""Authoritative Blender-state and cross-layer interface contracts.

``scene_checks.json`` is a strict schema-2 document. Contracts address objects,
materials, shader controls and compositor nodes by semantic custom properties, never by
datablock names. Object selectors distinguish ``bvfx_role`` from ``bvfx_control`` so a
planner cannot put control ids in a role field and publish an unresolvable contract. Their
lifecycle decides which prior-layer guarantees remain active for the layer currently being
built.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from vfx_harness.domain.contracts import active_for, load_document
from vfx_harness.domain.evidence_kinds import PROJECTED_ORIGIN_KINDS as PROJECTED_ORIGIN_KINDS
from vfx_harness.domain.work_units import plan_selector_declared
from vfx_harness.evidence.scene_checks.kinds import BBOX_KINDS
from vfx_harness.orchestration.plan_authority import selected_artifact_path

if TYPE_CHECKING:
    from vfx_harness.orchestration.authority_selection import ResolvedSelectedAuthority


def _scene_checks_path(
    shot_folder: str | Path,
    selected_authority: ResolvedSelectedAuthority | None,
) -> Path:
    shot = Path(shot_folder)
    if selected_authority is None:
        return selected_artifact_path(shot, "scene_checks.json")
    if selected_authority.plan is None:
        return shot / "scene_checks.json"
    try:
        return selected_authority.artifact_paths["scene_checks.json"]
    except KeyError as exc:
        raise ValueError("selected authority omits scene_checks.json") from exc


def load_rows(
    shot_folder: str | Path,
    selected_authority: ResolvedSelectedAuthority | None = None,
) -> list[dict]:
    return load_document(
        _scene_checks_path(shot_folder, selected_authority),
        "contracts",
    )


def scene_contract_declared_frames(
    shot_folder,
    *,
    selected_authority=None,
) -> dict[str, tuple[int, ...]]:
    """Frames each selected scene contract declares, by contract id.

    A row with ``frame`` or ``frames`` is due only there; a row with neither is unframed and
    falls back to the active judge. Composition needs this to require a bound row at the
    frame it measures rather than at every moment of the claim that binds it (HIR-0204).
    """
    declared: dict[str, tuple[int, ...]] = {}
    try:
        rows = load_rows(shot_folder, selected_authority)
    except (OSError, ValueError):
        # No selected scene-contract artifact means no scene contracts, hence no declared
        # frames. A malformed one is the artifact reader's failure, not this map's.
        return declared
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        contract_id = str(row.get("id") or "")
        if not contract_id:
            continue
        frames: list[int] = []
        value = row.get("frame")
        if isinstance(value, int) and not isinstance(value, bool):
            frames.append(int(value))
        for item in row.get("frames") or ():
            if isinstance(item, int) and not isinstance(item, bool):
                frames.append(int(item))
        if frames:
            declared[contract_id] = tuple(sorted(dict.fromkeys(frames)))
    return declared


def deferred_subject_composition_ids(
    rows: Sequence[Mapping[str, object]],
    layer_id: str | int,
    frame: int | None = None,
) -> tuple[str, ...]:
    """Camera-owned bbox rows that become testable on a later geometry layer (HIR-0127)."""

    try:
        current = int(layer_id)
    except (TypeError, ValueError):
        return ()
    ids: list[str] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        if str(row.get("kind") or "") not in BBOX_KINDS:
            continue
        cid = str(row.get("id") or "")
        if not cid:
            continue
        try:
            owner = int(row.get("owner_layer"))
        except (TypeError, ValueError):
            continue
        if owner >= current:
            continue
        if not active_for(dict(row), layer_id, frame):
            continue
        ids.append(cid)
    return tuple(sorted(ids))


def deferred_subject_composition_activation_ids(
    rows: Sequence[Mapping[str, object]],
    layer_id: str | int,
    frame: int | None = None,
) -> tuple[str, ...]:
    """Deferred bbox rows whose subject first becomes due on this layer (HIR-0134)."""
    try:
        current = int(layer_id)
    except (TypeError, ValueError):
        return ()
    active = set(deferred_subject_composition_ids(rows, layer_id, frame))
    return tuple(
        sorted(
            str(row.get("id"))
            for row in rows
            if isinstance(row, Mapping)
            and str(row.get("id") or "") in active
            and int(row.get("activates_at")) == current
        )
    )


def _selector_overlap(left: str, right: str) -> bool:

    return plan_selector_declared(left, (right,)) or plan_selector_declared(
        right, (left,)
    )


def deferred_subject_composition_ids_for_unit(
    rows: Sequence[Mapping[str, object]],
    units,
    unit,
    layer_id: str | int,
    frame: int | None = None,
) -> tuple[str, ...]:
    """Deferred bbox debt payable by this geometry unit (HIR-0134).

    On the activation layer, a parent selector such as ``building`` may be produced by
    several truthful write clusters. The first unit whose dependency closure contains
    every overlapping producer pays the camera-owned row. On later layers the subject
    already exists, so every overlapping geometry mutation protects the persistent row.
    """
    if unit is None or "geometry" not in tuple(getattr(unit, "provides", ()) or ()):
        return ()
    try:
        current = int(layer_id)
    except (TypeError, ValueError):
        return ()
    unit_rows = tuple(units or ())
    by_id = {str(getattr(item, "id", "")): item for item in unit_rows}
    closure = {str(getattr(unit, "id", ""))}
    frontier = list(getattr(unit, "depends_on", ()) or ())
    while frontier:
        current_id = str(frontier.pop())
        if current_id in closure:
            continue
        closure.add(current_id)
        dependency = by_id.get(current_id)
        if dependency is not None:
            frontier.extend(getattr(dependency, "depends_on", ()) or ())

    by_contract = {
        str(row.get("id")): row
        for row in rows
        if isinstance(row, Mapping) and row.get("id")
    }
    payable: list[str] = []
    for contract_id in deferred_subject_composition_ids(rows, layer_id, frame):
        row = by_contract[contract_id]
        roles = tuple(str(role) for role in (row.get("roles") or ()) if str(role))
        if not roles:
            continue
        producers = {
            str(getattr(candidate, "id", ""))
            for candidate in unit_rows
            if "geometry" in tuple(getattr(candidate, "provides", ()) or ())
            and any(
                _selector_overlap(role, mutation)
                for role in roles
                for mutation in (
                    *(getattr(getattr(candidate, "mutates", None), "roles", ()) or ()),
                    *(getattr(getattr(candidate, "mutates", None), "dresses", ()) or ()),
                )
            )
        }
        unit_id = str(getattr(unit, "id", ""))
        overlaps = unit_id in producers
        try:
            activates_at = int(row.get("activates_at"))
        except (TypeError, ValueError):
            continue
        if activates_at == current:
            if overlaps and producers and producers.issubset(closure):
                payable.append(contract_id)
        elif activates_at < current and overlaps:
            payable.append(contract_id)
    return tuple(sorted(payable))


def deferred_subject_composition_forecast_ids_for_unit(
    rows: Sequence[Mapping[str, object]],
    units,
    unit,
    layer_id: str | int,
    frame: int | None = None,
) -> tuple[str, ...]:
    """Activation-layer bbox rows an overlapping producer may inspect early.

    HIR-0134 deliberately makes only the first dependency-complete producer pay a
    deferred subject bbox.  Earlier producers still need the exact camera-owned rows
    while their geometry is mutable, otherwise the payer discovers a bad aggregate
    after those producers have frozen.  Forecasts are diagnostic only; callers must
    not add these ids to required evidence or checkpoint protection.
    """
    if unit is None or "geometry" not in tuple(getattr(unit, "provides", ()) or ()):
        return ()
    unit_rows = tuple(units or ())
    unit_id = str(getattr(unit, "id", ""))
    payable = set(
        deferred_subject_composition_ids_for_unit(
            rows, unit_rows, unit, layer_id, frame
        )
    )
    by_contract = {
        str(row.get("id")): row
        for row in rows
        if isinstance(row, Mapping) and row.get("id")
    }
    forecasts: list[str] = []
    for contract_id in deferred_subject_composition_activation_ids(
        rows, layer_id, frame
    ):
        if contract_id in payable:
            continue
        roles = tuple(
            str(role)
            for role in (by_contract[contract_id].get("roles") or ())
            if str(role)
        )
        if any(
            _selector_overlap(role, mutation)
            for role in roles
            for candidate in unit_rows
            if str(getattr(candidate, "id", "")) == unit_id
            for mutation in (
                *(getattr(getattr(candidate, "mutates", None), "roles", ()) or ()),
                *(getattr(getattr(candidate, "mutates", None), "dresses", ()) or ()),
            )
        ):
            forecasts.append(contract_id)
    return tuple(sorted(forecasts))


def irreversible_deferred_subject_forecast_failures(
    contract_rows: Sequence[Mapping[str, object]],
    evidence_rows: Sequence[Mapping[str, object]],
) -> tuple[dict, ...]:
    """Forecast misses that adding successor geometry cannot repair.

    Projected union width, height, and bottom are monotone nondecreasing as more
    subject geometry arrives; union top is monotone nonincreasing.  Only violations
    on the already-impossible side become blockers.  Other misses remain diagnostic
    because a successor can still extend the union into the target.
    """
    by_id = {
        str(row.get("id")): row
        for row in contract_rows
        if isinstance(row, Mapping) and row.get("id")
    }
    out: list[dict] = []
    for evidence in evidence_rows:
        if not isinstance(evidence, Mapping) or evidence.get("pass"):
            continue
        cid = str(evidence.get("id") or "")
        source = by_id.get(cid)
        value = evidence.get("value")
        if source is None or not isinstance(value, (int, float)):
            continue
        kind = str(source.get("kind") or "")
        op = str(source.get("op") or "band")
        increasing = kind in {"bbox_width", "bbox_height", "bbox_bottom_y"}
        decreasing = kind == "bbox_top_y"
        if not increasing and not decreasing:
            continue
        lower: float | None = None
        upper: float | None = None
        if op == "band":
            lower = float(source["lo"])
            upper = float(source["hi"])
        elif op == "min":
            lower = float(source["lo"])
        elif op == "max":
            upper = float(source["hi"])
        elif op == "eq":
            target = float(source["value"])
            tolerance = float(source.get("tol") or 0)
            lower, upper = target - tolerance, target + tolerance
        irreversible = (
            increasing and upper is not None and float(value) > upper
        ) or (
            decreasing and lower is not None and float(value) < lower
        )
        if not irreversible:
            continue
        direction = "increase" if increasing else "decrease"
        out.append({
            **dict(evidence),
            "pass": False,
            "source": "deferred_subject_forecast_blocker",
            "diagnostic_only": False,
            "acceptance_evidence": True,
            "note": (
                f"irreversible partial-subject union violation: {kind} can only "
                f"{direction} as successor geometry is added; repair this producer "
                "before freeze or call cannot_express_in_scope"
            ),
        })
    return tuple(out)


INCREASING_UNION_KINDS = frozenset({"bbox_width", "bbox_height", "bbox_bottom_y"})
DECREASING_UNION_KINDS = frozenset({"bbox_top_y"})


def _dependency_closure(unit, by_id: Mapping[str, object]) -> set[str]:
    closure = {str(getattr(unit, "id", ""))}
    frontier = list(getattr(unit, "depends_on", ()) or ())
    while frontier:
        current_id = str(frontier.pop())
        if current_id in closure:
            continue
        closure.add(current_id)
        dependency = by_id.get(current_id)
        if dependency is not None:
            frontier.extend(getattr(dependency, "depends_on", ()) or ())
    return closure


def deferred_subject_union_producers(
    rows: Sequence[Mapping[str, object]],
    units,
    layer_id: str | int,
    frame: int | None = None,
) -> dict[str, tuple[str, ...]]:
    """Every geometry unit whose mutation overlaps each deferred row, in dependency order.

    The projected union of a deferred subject row is the union of *all* of these
    producers' geometry. Room run 20260904T105849Z-0c9a45: ``exterior_facade`` froze at
    height 0.349 of a 0.35 ceiling over ``exterior.*`` and the last producer,
    ``exterior_ground``, could only grow that union; nothing had told either unit who
    else shared the band (HIR-0197). Order is the stable topological order over
    ``depends_on`` with authored position as the tie-break (HIR-0119).
    """
    unit_rows = tuple(units or ())
    by_id = {str(getattr(item, "id", "")): item for item in unit_rows}
    ordered: list[str] = []
    remaining = [str(getattr(item, "id", "")) for item in unit_rows]
    while remaining:
        progressed = False
        for candidate in list(remaining):
            deps = {
                str(dep) for dep in (getattr(by_id[candidate], "depends_on", ()) or ())
            } & set(by_id)
            if deps <= set(ordered):
                ordered.append(candidate)
                remaining.remove(candidate)
                progressed = True
                break
        if not progressed:
            ordered.extend(remaining)
            break
    by_contract = {
        str(row.get("id")): row
        for row in rows
        if isinstance(row, Mapping) and row.get("id")
    }
    producers: dict[str, tuple[str, ...]] = {}
    for contract_id in deferred_subject_composition_activation_ids(rows, layer_id, frame):
        row = by_contract[contract_id]
        roles = tuple(str(role) for role in (row.get("roles") or ()) if str(role))
        if not roles:
            continue
        producers[contract_id] = tuple(
            unit_id
            for unit_id in ordered
            if "geometry" in tuple(getattr(by_id[unit_id], "provides", ()) or ())
            and any(
                _selector_overlap(role, mutation)
                for role in roles
                for mutation in (
                    *(getattr(getattr(by_id[unit_id], "mutates", None), "roles", ()) or ()),
                    *(getattr(getattr(by_id[unit_id], "mutates", None), "dresses", ()) or ()),
                )
            )
        )
    return producers


def deferred_subject_sharing_for_unit(
    rows: Sequence[Mapping[str, object]],
    units,
    unit,
    layer_id: str | int,
    frame: int | None = None,
) -> dict[str, dict[str, list[str]]]:
    """Per deferred row this unit touches: who shares the union and who still comes.

    ``pending`` producers are the sharers outside this unit's dependency closure: their
    geometry is not in the scene yet and can only extend the union on its irreversible
    sides, so whatever slack this unit leaves them is all they will ever get.
    """
    if unit is None:
        return {}
    unit_rows = tuple(units or ())
    by_id = {str(getattr(item, "id", "")): item for item in unit_rows}
    closure = _dependency_closure(unit, by_id)
    unit_id = str(getattr(unit, "id", ""))
    sharing: dict[str, dict[str, list[str]]] = {}
    for contract_id, producers in deferred_subject_union_producers(rows, unit_rows, layer_id, frame).items():
        if unit_id not in producers:
            continue
        sharing[contract_id] = {
            "producers": list(producers),
            "pending": [producer for producer in producers if producer not in closure],
        }
    return sharing


def deferred_subject_union_slack(
    contract_rows: Sequence[Mapping[str, object]],
    evidence_rows: Sequence[Mapping[str, object]],
) -> dict[str, dict[str, object]]:
    """Remaining room on each evidence row's irreversible side, measured, per row id.

    Width, height, and bottom can only grow as later producers add geometry; top can
    only fall. ``slack`` is how far the current union still is from the bound on that
    side (negative once the bound is already crossed); it is the budget every pending
    producer must share.
    """
    by_id = {
        str(row.get("id")): row
        for row in contract_rows
        if isinstance(row, Mapping) and row.get("id")
    }
    slack: dict[str, dict[str, object]] = {}
    for evidence in evidence_rows:
        if not isinstance(evidence, Mapping):
            continue
        cid = str(evidence.get("id") or "")
        source = by_id.get(cid)
        value = evidence.get("value")
        if source is None or not isinstance(value, (int, float)):
            continue
        kind = str(source.get("kind") or "")
        op = str(source.get("op") or "band")
        lower: float | None = None
        upper: float | None = None
        if op == "band":
            lower, upper = float(source["lo"]), float(source["hi"])
        elif op == "min":
            lower = float(source["lo"])
        elif op == "max":
            upper = float(source["hi"])
        elif op == "eq":
            target = float(source["value"])
            tolerance = float(source.get("tol") or 0)
            lower, upper = target - tolerance, target + tolerance
        if kind in INCREASING_UNION_KINDS and upper is not None:
            slack[cid] = {"kind": kind, "side": "grows", "bound": upper, "slack": upper - float(value)}
        elif kind in DECREASING_UNION_KINDS and lower is not None:
            slack[cid] = {"kind": kind, "side": "falls", "bound": lower, "slack": float(value) - lower}
    return slack


def deferred_subject_irreversible_bound(
    contract_row: Mapping[str, object],
) -> dict[str, object] | None:
    """The side and bound a union can never come back from, without a measurement.

    ``deferred_subject_union_slack`` answers the same question once a reading exists.
    The compiled unit card is built before any mutation, so it has no reading and must
    state the CONDITION instead of a verdict (HIR-0212).
    """
    kind = str(contract_row.get("kind") or "")
    op = str(contract_row.get("op") or "band")
    lower = upper = None
    if op == "band":
        lower, upper = float(contract_row["lo"]), float(contract_row["hi"])
    elif op == "min":
        lower = float(contract_row["lo"])
    elif op == "max":
        upper = float(contract_row["hi"])
    elif op == "eq":
        target = float(contract_row["value"])
        tolerance = float(contract_row.get("tol") or 0)
        lower, upper = target - tolerance, target + tolerance
    if kind in INCREASING_UNION_KINDS and upper is not None:
        return {"kind": kind, "side": "grows", "bound": upper, "crosses_when": "above"}
    if kind in DECREASING_UNION_KINDS and lower is not None:
        return {"kind": kind, "side": "falls", "bound": lower, "crosses_when": "below"}
    return None


@dataclass(frozen=True, slots=True)
class DeferredSubjectCompositionPaymentGap:
    contract_id: str
    roles: tuple[str, ...]
    producer_ids: tuple[str, ...]


def deferred_subject_composition_payment_gaps(
    rows: Sequence[Mapping[str, object]], units, layer_id: str | int
) -> tuple[DeferredSubjectCompositionPaymentGap, ...]:
    """Activation-layer bbox rows with no dependency-complete geometry payer."""
    unit_rows = tuple(units or ())
    by_contract = {
        str(row.get("id")): row
        for row in rows
        if isinstance(row, Mapping) and row.get("id")
    }
    gaps: list[DeferredSubjectCompositionPaymentGap] = []
    for contract_id in deferred_subject_composition_activation_ids(rows, layer_id):
        if any(
            contract_id
            in deferred_subject_composition_ids_for_unit(
                rows, unit_rows, unit, layer_id
            )
            for unit in unit_rows
        ):
            continue
        row = by_contract[contract_id]
        roles = tuple(str(role) for role in (row.get("roles") or ()) if str(role))
        producers = tuple(
            sorted(
                str(getattr(unit, "id", ""))
                for unit in unit_rows
                if "geometry" in tuple(getattr(unit, "provides", ()) or ())
                and any(
                    _selector_overlap(role, mutation)
                    for role in roles
                    for mutation in (
                        *(getattr(getattr(unit, "mutates", None), "roles", ()) or ()),
                        *(getattr(getattr(unit, "mutates", None), "dresses", ()) or ()),
                    )
                )
            )
        )
        gaps.append(
            DeferredSubjectCompositionPaymentGap(contract_id, roles, producers)
        )
    return tuple(gaps)
