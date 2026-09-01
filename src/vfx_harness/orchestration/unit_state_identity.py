"""Pure WorkUnit identity, accepted-set, and replan-closure calculations."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict
from typing import Any

from vfx_harness.domain.unit_completion_receipts import UnitCompletionReceipt
from vfx_harness.domain.work_units import WorkUnit, validate_unit_dag

# Bump whenever WorkUnit gains or loses a field always present in unit_digest.
# Schema 4 added MutationScope.dresses (ADR-0007).
DIGEST_SCHEMA = 4


def unit_digest(unit: WorkUnit) -> str:
    """Hash the exact durable identity-bearing shape of one WorkUnit."""

    payload = asdict(unit)
    if not payload.get("publishes"):
        payload.pop("publishes", None)
    if not payload.get("consumes"):
        payload.pop("consumes", None)
    construction = payload.get("construction")
    if isinstance(construction, dict) and (
        construction.get("route", "procedural") == "procedural"
        and not construction.get("witnesses")
        and not construction.get("reason")
    ):
        payload.pop("construction", None)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def digest_matched_passed(
    state: Mapping[str, Any] | dict,
    units: tuple[WorkUnit, ...],
) -> set[str]:
    """Return passed ids whose receipt and digest match current authority."""

    rows = (state or {}).get("units") or {}
    passed = {
        uid
        for uid, row in rows.items()
        if isinstance(row, dict) and row.get("status") == "passed"
    }
    if not state or int(state.get("digest_schema", 1)) != DIGEST_SCHEMA:
        # Digests from another schema are not comparable, and a bare lifecycle bit
        # must never become acceptance authority.
        return set()
    by_id = {unit.id: unit for unit in units}
    sealed: set[str] = set()
    for uid in passed:
        row = rows.get(uid) or {}
        if uid not in by_id or row.get("unit_hash") != unit_digest(by_id[uid]):
            continue
        try:
            receipt = UnitCompletionReceipt.parse(
                row.get("completion_receipt"),
                f"work-unit state {uid}.completion_receipt",
            )
        except ValueError:
            continue
        if (
            receipt.claim.unit_id == uid
            and receipt.claim.layer_id == str(state.get("layer"))
            and receipt.claim.unit_digest == row.get("unit_hash")
            and receipt.claim.plan_hash == state.get("plan_hash")
        ):
            sealed.add(uid)
    return sealed


def downstream(seeds: set[str], units: tuple[WorkUnit, ...]) -> set[str]:
    reverse: dict[str, set[str]] = {unit.id: set() for unit in units}
    for unit in units:
        for dependency in unit.depends_on:
            reverse.setdefault(dependency, set()).add(unit.id)
    out = set(seeds)
    frontier = list(seeds)
    while frontier:
        current = frontier.pop()
        for child in reverse.get(current, set()):
            if child not in out:
                out.add(child)
                frontier.append(child)
    return out


def replan_effects(
    old_units: tuple[WorkUnit, ...],
    new_units: tuple[WorkUnit, ...],
) -> dict[str, list[str]]:
    """Return the deterministic supersession closure without durable I/O."""

    validate_unit_dag(old_units, "old work-unit DAG")
    validate_unit_dag(new_units, "new work-unit DAG")
    old = {unit.id: unit for unit in old_units}
    new = {unit.id: unit for unit in new_units}
    added = set(new) - set(old)
    removed = set(old) - set(new)
    changed = {
        uid
        for uid in set(old) & set(new)
        if unit_digest(old[uid]) != unit_digest(new[uid])
    }
    invalidated = downstream(added | changed, new_units)
    preserved = set(old) & set(new) - invalidated
    return {
        "added": sorted(added),
        "removed": sorted(removed),
        "changed": sorted(changed),
        "invalidated": sorted(invalidated),
        "preserved": sorted(preserved),
    }
