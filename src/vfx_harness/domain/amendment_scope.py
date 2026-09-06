"""A finding-driven amendment is bounded by the finding that drove it.

room_1046_opening layer 1 carried two camera-owned bands that the built geometry
falsified together, and the controller dispatched an amendment. The finding said one
thing: ``cam-bbox-f1-building`` and ``cam-bbox-f38-building`` are jointly unsatisfiable.
The amendment resolved that, and while it was there it also moved bounds the conflict
never implicated:

    f1   0.15-0.35 -> 0.28-0.46     conflict was the UPPER bound, over by 0.0009
    f38  0.55-0.80 -> 0.40-0.58     conflict was the LOWER bound, under by 0.0924

The reference still measures 0.216 at f1. The original band contained it; the amended
one excludes it. Only f38 was ever wrong, and the amendment fixed the broken row and
broke the correct one -- at the establishing frame whose authored beat is a small pool
of light. Nothing defended f1, because the finding had not named it as wrong, only as
party to a contradiction.

The rule needs no measurement and no reference instrument. **Resolving an
unsatisfiability requires enlarging an admissible set; it can never require shrinking
one.** So a bound that shrinks on a row the finding names was not asked for by the
finding, and the amendment is spending authority nothing put in question.

This binds only amendments driven by a finding. An operator-directed rematerialization
is bounded by the operator, who may narrow a band deliberately and say so in the
trigger.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

#: Threshold shapes and the admissible interval each denotes (HIR-0125 field shapes).
_LOW_KEYS = ("lo", "min")
_HIGH_KEYS = ("hi", "max")


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def admissible_interval(row: Mapping[str, object]) -> tuple[float, float] | None:
    """The closed interval a threshold row admits, or None when it denotes none.

    ``eq`` carries ``value`` with optional ``tol``; ``min``/``max``/``band`` carry the
    bounds directly. A row whose numbers cannot be read denotes no interval and is not
    compared -- an unreadable row is a different finding's business.
    """

    op = str(row.get("op") or "").strip()
    if op == "eq":
        value = _number(row.get("value"))
        if value is None:
            return None
        tol = _number(row.get("tol")) or 0.0
        return (value - abs(tol), value + abs(tol))
    low = next((_number(row.get(k)) for k in _LOW_KEYS if _number(row.get(k)) is not None), None)
    high = next((_number(row.get(k)) for k in _HIGH_KEYS if _number(row.get(k)) is not None), None)
    if low is None and high is None:
        return None
    return (-math.inf if low is None else low, math.inf if high is None else high)


@dataclass(frozen=True, slots=True)
class TightenedBound:
    """One row whose admissible set shrank under a finding-driven amendment."""

    contract_id: str
    before: tuple[float, float]
    after: tuple[float, float]
    named: bool = True

    @property
    def lost_below(self) -> float:
        """How much of the previously admissible range was cut from the low side."""
        return max(0.0, self.after[0] - self.before[0])

    @property
    def lost_above(self) -> float:
        """How much of the previously admissible range was cut from the high side."""
        return max(0.0, self.before[1] - self.after[1])

    @property
    def gained_below(self) -> float:
        """Room the amendment added at the low side."""
        return max(0.0, self.before[0] - self.after[0])

    @property
    def gained_above(self) -> float:
        """Room the amendment added at the high side."""
        return max(0.0, self.after[1] - self.before[1])

    def describe(self) -> str:
        """Name the refused move, and where the resolution actually took its room.

        The finding records its residuals in prose, so the binding bound is not readable
        from it -- but the amendment shows it: the bound that was *enlarged* is where the
        resolution took room. Saying both turns "you shrank this" into "the conflict
        needed room at the ceiling; you also moved the floor", which is what a
        materializer needs to author a compliant amendment rather than guess (HIR-0232).
        """
        refused: list[str] = []
        if self.lost_below > 0:
            refused.append(f"raised its floor {self.before[0]:g} -> {self.after[0]:g}")
        if self.lost_above > 0:
            refused.append(f"lowered its ceiling {self.before[1]:g} -> {self.after[1]:g}")
        scope = "" if self.named else " (a row the finding does not name at all)"
        text = f"{self.contract_id}{scope} {' and '.join(refused)}"
        if self.gained_above > 0:
            text += (
                f" — the resolution took its room at the ceiling "
                f"({self.before[1]:g} -> {self.after[1]:g}), which is permitted; keep that "
                f"and restore the other bound"
            )
        elif self.gained_below > 0:
            text += (
                f" — the resolution took its room at the floor "
                f"({self.before[0]:g} -> {self.after[0]:g}), which is permitted; keep that "
                f"and restore the other bound"
            )
        return text


def tightened_conflict_rows(
    conflict_contract_ids: Iterable[str],
    base_rows: Sequence[Mapping[str, object]],
    candidate_rows: Sequence[Mapping[str, object]],
) -> tuple[TightenedBound, ...]:
    """Rows the finding named whose admissible set shrank in the amendment.

    A row absent from either side is not compared: removal and introduction are
    different transactions with their own rules.
    """

    named = {str(cid) for cid in conflict_contract_ids if str(cid)}
    if not named:
        return ()
    before = {
        str(r.get("id")): r for r in base_rows if isinstance(r, Mapping) and r.get("id")
    }
    after = {
        str(r.get("id")): r for r in candidate_rows if isinstance(r, Mapping) and r.get("id")
    }
    found: list[TightenedBound] = []
    # Every row present on both sides, not only the ones the finding named. caesar_curia's
    # amendment was asked about ONE row and pulled the ceiling on three: 0.9105 and 1.0,
    # measured passing minutes earlier, failed against bands nobody asked to move. Scoping
    # to the named ids would have reported the one row that was at least in question and
    # stayed silent on the two that were not -- and an unnamed row is further outside the
    # finding's bound, not nearer it (HIR-0245).
    for contract_id in sorted(set(before) & set(after)):
        old = admissible_interval(before[contract_id])
        new = admissible_interval(after[contract_id])
        if old is None or new is None:
            continue
        if new[0] > old[0] or new[1] < old[1]:
            found.append(TightenedBound(contract_id, old, new, contract_id in named))
    return tuple(found)


AMENDMENT_RELAXATION_RULE = (
    "an amendment driven by a joint-unsatisfiability finding may enlarge an admissible "
    "set but never shrink one: resolving a contradiction requires giving some row more "
    "room, never less. A bound that shrank was not asked for by the finding -- and a row "
    "the finding does not name at all is further outside its bound, not nearer it. "
    "Restore it, or -- if the narrower bound is genuinely intended -- raise it as its own "
    "reviewed change rather than folding it into this repair"
)

__all__ = [
    "AMENDMENT_RELAXATION_RULE",
    "TightenedBound",
    "admissible_interval",
    "tightened_conflict_rows",
]
