"""Auditable administrative transactions for durable work-unit state."""

from __future__ import annotations

import argparse

from vfx_harness.domain.brief import load_shot
from vfx_harness.domain.unit_attempts import UnitAttemptClaim
from vfx_harness.orchestration.authority_capsule_resolution import (
    selected_layer_capsule_digest,
)
from vfx_harness.orchestration.authority_selection import resolve_selected_authority
from vfx_harness.orchestration.authority_selection_transaction import (
    AuthoritySelectionConflict,
)
from vfx_harness.orchestration.builder_execution_fence import (
    BuilderExecutionFenceActive,
    BuilderExecutionFenceError,
    builder_execution_fence,
)
from vfx_harness.orchestration.ledger import load_layers
from vfx_harness.orchestration.selected_authority_guard import (
    commit_selected_authority,
)
from vfx_harness.orchestration.unit_state import (
    invalidate_checkpoint,
    validate_current,
)
from vfx_harness.orchestration.unit_state import load as load_unit_state
from vfx_harness.orchestration.unit_state_claims import (
    UnitAttemptConflict,
    release_unclaimed_unit_for_retry,
    release_unit_attempt,
)


def _invalidate(args: argparse.Namespace) -> int:
    shot = load_shot(args.folder)
    selected = resolve_selected_authority(shot.folder)
    layers = load_layers(shot, selected_authority=selected)
    try:
        layer = layers[str(args.layer)]
    except KeyError as exc:
        raise SystemExit(f"unknown layer {args.layer!r}") from exc
    if args.unit not in {unit.id for unit in layer.stages}:
        raise SystemExit(f"unknown work unit {args.unit!r} in layer {args.layer}")
    try:
        record = commit_selected_authority(
            shot.folder,
            selected,
            operation="work-unit checkpoint invalidation",
            mutation=lambda: invalidate_checkpoint(
                shot.folder,
                str(args.layer),
                args.unit,
                layer.stages,
                reason=args.reason,
                evidence=args.evidence,
            ),
        )
    except AuthoritySelectionConflict as exc:
        raise SystemExit(
            "selected authority changed before durable checkpoint invalidation"
        ) from exc
    affected = ", ".join(record["affected"])
    print(f"invalidated {args.unit}; affected: {affected}")
    return 0


def _retry(args: argparse.Namespace) -> int:
    """Reopen one failed/interrupted unit without erasing its history or dependency closure."""
    shot = load_shot(args.folder)
    layer_id = str(args.layer)
    try:
        with builder_execution_fence(shot.folder):
            selected = resolve_selected_authority(shot.folder)
            layers = load_layers(shot, selected_authority=selected)
            try:
                layer = layers[layer_id]
            except KeyError as exc:
                raise SystemExit(f"unknown layer {args.layer!r}") from exc
            units = {unit.id: unit for unit in layer.stages}
            if args.unit not in units:
                raise SystemExit(
                    f"unknown work unit {args.unit!r} in layer {args.layer}"
                )
            selected_plan_hash = selected_layer_capsule_digest(
                shot.folder,
                layer_id,
                selected,
            )
            state = load_unit_state(shot.folder, layer_id)
            validate_current(state, layer_id, layer.stages)
            slot = (state.get("units") or {}).get(args.unit, {})
            current = slot.get("status")
            retryable_from = {
                "failed",
                "planning",
                "building",
                "frozen",
                "evaluating",
                "repairing",
            }
            if current not in retryable_from:
                raise SystemExit(
                    f"work unit {args.unit!r} is {current!r}; retry requires one of "
                    f"{sorted(retryable_from)}"
                )
            raw_attempt = slot.get("active_attempt")
            attempt = (
                None
                if raw_attempt is None
                else UnitAttemptClaim.parse(raw_attempt, "reviewed retry active attempt")
            )
            # Both typed release operations acquire selection-SH then state-EX and
            # validate this exact token themselves.  A generic outer selection guard
            # would self-deadlock on a second descriptor for the same lock inode.
            if attempt is None:
                release_unclaimed_unit_for_retry(
                    shot.folder,
                    layer_id,
                    args.unit,
                    layer.stages,
                    expected_plan_hash=selected_plan_hash,
                    selection_token=selected.selection_token,
                    reason=args.reason,
                    evidence=list(args.evidence),
                )
            else:
                release_unit_attempt(
                    shot.folder,
                    layer_id,
                    args.unit,
                    layer.stages,
                    attempt,
                    expected_plan_hash=selected_plan_hash,
                    selection_token=selected.selection_token,
                    reason=args.reason,
                    evidence=list(args.evidence),
                )
    except BuilderExecutionFenceActive as exc:
        raise SystemExit(
            "work-unit retry refused because a live builder execution fence still owns "
            "the shot; stop the owning builder before reviewed recovery"
        ) from exc
    except BuilderExecutionFenceError as exc:
        raise SystemExit(f"work-unit retry could not prove an exclusive execution fence: {exc}") from exc
    except AuthoritySelectionConflict as exc:
        raise SystemExit(
            "selected authority changed before durable retry transition"
        ) from exc
    except UnitAttemptConflict as exc:
        raise SystemExit(f"work-unit attempt changed before reviewed retry: {exc}") from exc
    print(f"retryable: layer {layer_id} unit {args.unit}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    invalidate = commands.add_parser(
        "invalidate",
        help="revoke a checkpoint and block its downstream dependency closure",
    )
    invalidate.add_argument("folder", help="shot folder")
    invalidate.add_argument("--layer", required=True, help="layer id")
    invalidate.add_argument("--unit", required=True, help="work-unit id")
    invalidate.add_argument("--reason", required=True, help="authoritative failure reason")
    invalidate.add_argument(
        "--evidence",
        action="append",
        required=True,
        help="evidence locator; repeat for each item",
    )
    invalidate.set_defaults(handler=_invalidate)
    retry = commands.add_parser(
        "retry",
        help="reopen one failed or interrupted work unit while preserving its history",
    )
    retry.add_argument("folder", help="shot folder")
    retry.add_argument("--layer", required=True, help="layer id")
    retry.add_argument("--unit", required=True, help="failed work-unit id")
    retry.add_argument("--reason", required=True, help="why the failure is now retryable")
    retry.add_argument(
        "--evidence",
        action="append",
        required=True,
        help="evidence locator; repeat for each item",
    )
    retry.set_defaults(handler=_retry)
    args = parser.parse_args()
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
