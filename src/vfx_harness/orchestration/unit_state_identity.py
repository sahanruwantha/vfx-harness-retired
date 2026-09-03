"""Pure WorkUnit identity, accepted-set, and replan-closure calculations."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict
from typing import Any

from vfx_harness.domain.unit_completion_receipts import UnitCompletionReceipt
from vfx_harness.domain.work_units import WorkUnit, validate_unit_dag
from vfx_harness.orchestration.unit_completion_authorizations import (
    AuthorizedUnitCompletionSet,
    CandidateAuthorizedUnitCompletionSet,
    UnitCompletionAuthorization,
    UnitCompletionAuthorizationError,
    require_completion_authorization_matches_state,
)

# Bump whenever WorkUnit gains or loses a field always present in unit_digest.
# Schema 4 added MutationScope.dresses (ADR-0007).
# 5: a layer capsule attributes a deferred requirement's decision to its owner layer
# (HIR-0181); every stored layer digest of generation 4 is incomparable and migrates
# through the authority-state transaction (HIR-0182).
DIGEST_SCHEMA = 5
PRIOR_DIGEST_SCHEMAS = frozenset({4})
DIGEST_GENERATION_RULE = (
    "durable work-unit state binds digests of a prior generation; run "
    "`vfx migrate-digest-schema <shot>` — it republishes the selected view through the "
    "authority-state transaction, supersedes every prior-generation unit and terminal "
    "receipt with a typed reason, and never edits state by hand"
)


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


def authorized_passed_unit_ids(
    state: Mapping[str, Any],
    units: tuple[WorkUnit, ...],
    *,
    completion_authorization: UnitCompletionAuthorization | None,
    allow_candidate: bool = False,
) -> set[str]:
    """Return passed ids named by an exact state-bound authorization.

    A matching layer hash is not current authorization: it cannot distinguish an
    execution receipt from an A-to-B-to-A semantic coincidence. ``None`` is an
    explicit deny-all value and never derives authority from lifecycle state.
    """

    if completion_authorization is None:
        return set()
    if not isinstance(completion_authorization, AuthorizedUnitCompletionSet) and not (
        allow_candidate
        and isinstance(
            completion_authorization,
            CandidateAuthorizedUnitCompletionSet,
        )
    ):
        kind = "current or candidate" if allow_candidate else "current coordinator"
        raise UnitCompletionAuthorizationError(
            f"digest-matched passed units require a typed {kind} authorization"
        )
    require_completion_authorization_matches_state(
        state,
        completion_authorization,
        allow_candidate=allow_candidate,
    )
    if int(state.get("digest_schema", 0)) != DIGEST_SCHEMA:
        raise UnitCompletionAuthorizationError(
            "work-unit completion authorization requires the current digest schema"
        )

    rows = state.get("units") or {}
    by_id = {unit.id: unit for unit in units}
    sealed: set[str] = set()
    for uid, authorized_digest in completion_authorization.receipts:
        unit = by_id.get(uid)
        if unit is None:
            raise UnitCompletionAuthorizationError(
                f"work-unit completion authorization names unknown unit {uid!r}"
            )
        row = rows.get(uid) or {}
        expected_unit_digest = unit_digest(unit)
        if row.get("status") != "passed" or row.get("unit_hash") != expected_unit_digest:
            raise UnitCompletionAuthorizationError(
                f"authorized completion {uid!r} is not an exact current passed unit"
            )
        try:
            receipt = UnitCompletionReceipt.parse(
                row.get("completion_receipt"),
                f"work-unit state {uid}.completion_receipt",
            )
        except ValueError as exc:
            raise UnitCompletionAuthorizationError(str(exc)) from exc
        if (
            receipt.claim.unit_id != uid
            or receipt.claim.layer_id != str(state.get("layer"))
            or receipt.claim.unit_digest != expected_unit_digest
            or receipt.receipt_digest != authorized_digest
        ):
            raise UnitCompletionAuthorizationError(
                f"authorized completion receipt identity changed for unit {uid!r}"
            )
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
