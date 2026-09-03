"""Mutation fence for repeated bbox failures: measure feasibility before guessing again.

HIR-0082 closes a density search after a measured sweep; this module closes a bbox search
the same way. After ``BBOX_FEASIBILITY_STREAK`` consecutive mutations leave one ``bbox_*``
row failing, ``run_bpy`` is refused until ``check_scene(kind='bbox_feasibility')`` has run
for that row; an infeasible verdict fails closed into ``cannot_express_in_scope``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from vfx_harness.blender.bbox_feasibility import BBOX_METRIC_READINGS

BBOX_FEASIBILITY_STREAK = 6
STREAK_KEY = "bbox_failure_streaks"
RESULT_KEY = "bbox_feasibility"


def record_bbox_failures(comparison_state: dict, failed_rows: Iterable[Mapping]) -> None:
    """Count consecutive mutations after which the same bbox row still fails."""

    streaks: dict[str, dict] = comparison_state.setdefault(STREAK_KEY, {})
    failing: dict[str, list[str]] = {}
    for row in failed_rows:
        if str(row.get("metric") or "") in BBOX_METRIC_READINGS and row.get("id"):
            failing[str(row["id"])] = [str(role) for role in row.get("selector_roles") or []]
    for row_id, roles in failing.items():
        entry = streaks.get(row_id) or {"count": 0, "roles": roles}
        entry["count"] = int(entry.get("count", 0)) + 1
        entry["roles"] = roles or entry.get("roles") or []
        streaks[row_id] = entry
    for row_id in list(streaks):
        if row_id not in failing:
            streaks.pop(row_id, None)


def record_bbox_feasibility(
    comparison_state: dict,
    *,
    row_ids: Iterable[str],
    feasible: bool,
    binding: Iterable[str],
    bounds_source: str = "hosts",
) -> None:
    """Keep the strongest verdict: feasibility inside any bounds proves feasibility.

    Run 20260903T100335Z-fa5dbb found a satisfying box under wide bounds, then narrowed
    its own bounds to a tower and read INFEASIBLE; that later verdict must not license
    abstention, so a feasible record for the same rows is never downgraded by a
    supplied-bounds infeasibility.
    """
    ids = sorted(str(item) for item in row_ids)
    previous = comparison_state.get(RESULT_KEY) or {}
    if (
        previous.get("feasible")
        and set(ids) <= set(previous.get("row_ids") or [])
        and not feasible
        and bounds_source == "supplied"
    ):
        return
    comparison_state[RESULT_KEY] = {
        "row_ids": ids,
        "feasible": bool(feasible),
        "binding": sorted(str(item) for item in binding),
        "bounds_source": str(bounds_source),
    }


def bbox_feasibility_block(comparison_state: Mapping) -> str | None:
    """The refusal a mutation earns while a hot bbox row has no feasibility verdict."""

    streaks = comparison_state.get(STREAK_KEY) or {}
    hot = {row_id: entry for row_id, entry in streaks.items() if int(entry.get("count", 0)) >= BBOX_FEASIBILITY_STREAK}
    if not hot:
        return None
    result = comparison_state.get(RESULT_KEY) or {}
    covered = set(result.get("row_ids") or [])
    uncovered = sorted(row_id for row_id in hot if row_id not in covered)
    if uncovered:
        roles = sorted({role for row_id in uncovered for role in hot[row_id].get("roles") or []})
        return (
            "BLOCKED: bbox row(s) "
            + ", ".join(uncovered)
            + f" still fail after {BBOX_FEASIBILITY_STREAK}+ consecutive mutations. Run "
            f"check_scene(kind='bbox_feasibility', roles={roles}) before another mutation: "
            "it searches every axis-aligned proxy box under the sealed camera and returns the "
            "satisfying box or proves none exists. Guessing again is not measurement."
        )
    if not result.get("feasible", True):
        binding = ", ".join(result.get("binding") or sorted(covered))
        if result.get("bounds_source") == "supplied":
            return (
                "BLOCKED: check_scene(kind='bbox_feasibility') found no proxy box for "
                + binding
                + " within the bounds you supplied. That is not proof against the sealed "
                "camera: re-run it without bounds= (harness-derived bounds) or widen them to "
                "the region the hosts may legally occupy before mutating again."
            )
        return (
            "BLOCKED: check_scene(kind='bbox_feasibility') proved that no proxy box satisfies "
            + binding
            + " under the sealed camera. Further mutation cannot satisfy them; call "
            "cannot_express_in_scope naming those contract ids and the camera provider from "
            "your fault_owner_options."
        )
    return None
