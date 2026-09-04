"""A second unit declaring a capability must reach the one that originates it.

`provides` is additive mutation authority, not a label (HIR-0112), and camera
availability is typed `provides` authority rather than anything a role name implies
(HIR-0098).  Neither rule says what happens when two units in one layer declare the same
capability: replay order is topological over `depends_on` with authored position as the
only tie-break (HIR-0119), so two independent declarers are ordered by accident.

Run 20260904T020917Z-7afeae ordered `cam_lens` (`provides: ["camera"]`, `depends_on: []`,
mutating `camera.lens`) before `cam_path` (`provides: ["camera"]`, mutating `camera.rig`,
the unit that actually creates the camera).  `cam_lens.py` then replayed onto a scene with
no camera and the accepted chain broke mid-layer: `AttributeError: 'NoneType' object has
no attribute 'data'`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from vfx_harness.domain.work_units.unit import WorkUnit

CAPABILITY_ORIGIN_RULE = (
    "a capability is originated once per layer: the first unit declaring it in dependency "
    "order creates the host, and every other unit declaring the same capability only "
    "modifies that host, so it must contain the originator in its dependency closure. "
    "Two independent declarers are ordered by authored position alone, and the later one "
    "replays onto a scene the earlier one has not built yet. Add the originator to "
    "depends_on, or drop provides from the unit that does not create the host"
)


@dataclass(frozen=True, slots=True)
class CapabilityOriginGap:
    capability: str
    unordered_ids: tuple[str, ...]

    @property
    def code(self) -> str:
        return "capability_origin_unordered"


def _closure(units: Sequence[WorkUnit], start: str) -> set[str]:
    by_id = {unit.id: unit for unit in units}
    seen: set[str] = set()
    frontier = [start]
    while frontier:
        current = frontier.pop()
        if current in seen:
            continue
        seen.add(current)
        unit = by_id.get(current)
        if unit is not None:
            frontier.extend(unit.depends_on)
    return seen


def capability_origin_gaps(units: Sequence[WorkUnit]) -> tuple[CapabilityOriginGap, ...]:
    """Capabilities whose declarers are not ordered against one another.

    Exactly one declarer may reach no other: the originator that creates the host. Any
    second such declarer is unordered against it, so which script replays first is decided
    by authored position rather than by an edge.
    """
    rows = tuple(units)
    gaps: list[CapabilityOriginGap] = []
    for capability in sorted({item for unit in rows for item in unit.provides}):
        declarers = [unit for unit in rows if capability in unit.provides]
        if len(declarers) < 2:
            continue
        ids = {unit.id for unit in declarers}
        roots = sorted(
            unit.id
            for unit in declarers
            if not ((_closure(rows, unit.id) & ids) - {unit.id})
        )
        if len(roots) > 1:
            gaps.append(CapabilityOriginGap(capability, tuple(roots)))
    return tuple(gaps)


def describe_gap(gap: CapabilityOriginGap) -> str:
    names = ", ".join(gap.unordered_ids)
    return (
        f"units {names} each declare provides {gap.capability!r} and none reaches another "
        "in its dependency closure, so their replay order is decided by authored position "
        "alone; exactly one unit originates a capability and the rest must depend on it"
    )
