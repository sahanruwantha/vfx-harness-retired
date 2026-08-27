"""Auditable administrative transactions for durable work-unit state."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

from vfx_harness.domain.brief import load_shot
from vfx_harness.domain.unit_outcomes import load_hypothesis_falsification
from vfx_harness.orchestration.ledger import load_layers, load_layers_from_path
from vfx_harness.orchestration.plan_authority import resolve_current, resolve_published_bundle
from vfx_harness.orchestration.unit_state import (
    apply_replan,
    invalidate_checkpoint,
    replan_effects,
    transition,
    unit_digest,
    validate_current,
)
from vfx_harness.orchestration.unit_state import load as load_unit_state


def _invalidate(args: argparse.Namespace) -> int:
    shot = load_shot(args.folder)
    layers = load_layers(shot)
    try:
        layer = layers[str(args.layer)]
    except KeyError as exc:
        raise SystemExit(f"unknown layer {args.layer!r}") from exc
    if args.unit not in {unit.id for unit in layer.stages}:
        raise SystemExit(f"unknown work unit {args.unit!r} in layer {args.layer}")
    record = invalidate_checkpoint(
        shot.folder,
        str(args.layer),
        args.unit,
        layer.stages,
        reason=args.reason,
        evidence=args.evidence,
    )
    affected = ", ".join(record["affected"])
    print(f"invalidated {args.unit}; affected: {affected}")
    return 0


def _replan(args: argparse.Namespace) -> int:
    """Move durable unit state from one proven bundle DAG to current authority."""
    shot = load_shot(args.folder)
    current = resolve_current(shot.folder)
    base = resolve_published_bundle(
        shot.folder,
        run_id=args.base_run,
        content_hash=args.base_bundle,
    )
    current_layers = load_layers(shot)
    base_layers = load_layers_from_path(base.root / "layers.json")
    layer_id = str(args.layer)
    try:
        old_layer = base_layers[layer_id]
    except KeyError as exc:
        raise SystemExit(f"base bundle has no layer {args.layer!r}") from exc
    try:
        new_layer = current_layers[layer_id]
    except KeyError as exc:
        raise SystemExit(f"current bundle has no layer {args.layer!r}") from exc
    old_plan_hash = hashlib.sha256((base.root / "layers.json").read_bytes()).hexdigest()
    # The target identity must be what build initialization will demand: the RESOLVED
    # view's layers.json, not the bundle's sparse document (they differ by design once
    # a layer materializes).
    from vfx_harness.orchestration.plan_authority import active_plan_hash

    new_plan_hash = active_plan_hash(shot.folder, fallback_root=current.root)
    evidence = list(getattr(args, "evidence", None) or [])
    falsification_id = None
    hard_approval = getattr(args, "hard_constraint_approval", None)
    falsification_path = getattr(args, "falsification", None)
    reopen: set[str] = set()
    # Under unit-first authority the base bundle carries no unit DAG — the layer's units
    # live only in its materialized view and durable state, and the state's recorded
    # plan hash (the materialized view, not the bundle file) is the truthful old
    # identity. Compare a falsification against that identity before the DAG diff
    # (HIR-0049). The base bundle stays in the audit record via trigger/evidence.
    state = load_unit_state(shot.folder, layer_id)
    state_unit_ids = set((state or {}).get("units") or {})
    deferred_base = not old_layer.stages and bool(state_unit_ids)
    if deferred_base:
        old_plan_hash = str((state or {}).get("plan_hash") or old_plan_hash)
    if falsification_path:
        target = Path(falsification_path)
        if not target.is_absolute():
            target = shot.folder / target
        target = target.resolve()
        try:
            relative = target.relative_to(Path(shot.folder).resolve()).as_posix()
        except ValueError as exc:
            raise SystemExit("falsification record must live inside the shot folder") from exc
        if not relative.startswith("state/work-units/hypothesis-falsifications/"):
            raise SystemExit(
                "falsification record must be a harness-authored state/work-units artifact"
            )
        finding = load_hypothesis_falsification(target)
        if finding.layer != layer_id:
            raise SystemExit(
                f"falsification belongs to layer {finding.layer}, not requested layer {layer_id}"
            )
        if finding.bundle_hash != base.content_hash or finding.plan_hash != old_plan_hash:
            raise SystemExit("falsification identities do not match the explicit base bundle")
        identity_units = old_layer.stages if old_layer.stages else new_layer.stages
        identity_by_id = {unit.id: unit for unit in identity_units}
        if (
            finding.unit not in identity_by_id
            or finding.unit_hash != unit_digest(identity_by_id[finding.unit])
        ):
            raise SystemExit("falsification unit identity does not match the base DAG")
        if finding.changes_hard_constraint and not hard_approval:
            raise SystemExit(
                "falsification involves a hard constraint; pass --hard-constraint-approval "
                "with the human approval evidence locator"
            )
        falsification_id = finding.record_id
        reopen = {finding.unit, *finding.affected}
        evidence.append(relative)
        if hard_approval:
            evidence.append(str(hard_approval))
    if not evidence:
        raise SystemExit("replan requires --evidence or --falsification")
    old_units = old_layer.stages
    if deferred_base and state is not None:
        try:
            validate_current(state, layer_id, new_layer.stages)
            old_units = new_layer.stages
        except ValueError:
            old_units = ()
    orphaned = sorted(
        state_unit_ids - {unit.id for unit in old_layer.stages} - {unit.id for unit in new_layer.stages}
        if deferred_base
        else set()
    )
    effects = replan_effects(old_units, new_layer.stages)
    if reopen:
        invalidated = set(effects["invalidated"]) | (reopen & {unit.id for unit in new_layer.stages})
        effects = {
            **effects,
            "invalidated": sorted(invalidated),
            "preserved": sorted(set(effects["preserved"]) - invalidated),
        }
    if getattr(args, "preview", False):
        print(
            f"replan preview layer {layer_id}: "
            f"added={','.join(effects['added']) or '-'}; "
            f"removed={','.join(effects['removed']) or '-'}; "
            f"changed={','.join(effects['changed']) or '-'}; "
            f"invalidated={','.join(effects['invalidated']) or '-'}; "
            f"preserved={','.join(effects['preserved']) or '-'}; "
            f"orphaned={','.join(orphaned) or '-'}"
        )
        return 0
    record = apply_replan(
        shot.folder,
        layer_id,
        old_units,
        new_layer.stages,
        old_plan_hash=old_plan_hash,
        new_plan_hash=new_plan_hash,
        owner=args.owner,
        trigger=args.trigger,
        evidence=evidence,
        falsification_id=falsification_id,
        hard_constraint_approval=hard_approval,
        discard_accepted=bool(getattr(args, "discard_accepted", False)),
        reopen=reopen,
    )
    print(
        f"replanned layer {layer_id} from {base.content_hash[:16]} to "
        f"{current.content_hash[:16]}; "
        f"added={','.join(record['added']) or '-'}; "
        f"removed={','.join(record['removed']) or '-'}; "
        f"changed={','.join(record['changed']) or '-'}; "
        f"preserved={','.join(record['preserved']) or '-'}; "
        f"orphaned={','.join(record.get('orphaned') or []) or '-'}"
    )
    return 0


def _retry(args: argparse.Namespace) -> int:
    """Reopen one failed/interrupted unit without erasing its history or dependency closure."""
    shot = load_shot(args.folder)
    layers = load_layers(shot)
    layer_id = str(args.layer)
    try:
        layer = layers[layer_id]
    except KeyError as exc:
        raise SystemExit(f"unknown layer {args.layer!r}") from exc
    units = {unit.id: unit for unit in layer.stages}
    if args.unit not in units:
        raise SystemExit(f"unknown work unit {args.unit!r} in layer {args.layer}")
    state = load_unit_state(shot.folder, layer_id)
    validate_current(state, layer_id, layer.stages)
    current = (state.get("units") or {}).get(args.unit, {}).get("status")
    retryable_from = {"failed", "planning", "building", "frozen", "repairing"}
    if current not in retryable_from:
        raise SystemExit(
            f"work unit {args.unit!r} is {current!r}; retry requires one of "
            f"{sorted(retryable_from)}"
        )
    transition(
        shot.folder,
        layer_id,
        args.unit,
        "retryable",
        reason=args.reason,
        metadata={"evidence": list(args.evidence)},
    )
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
    replan = commands.add_parser(
        "replan",
        help="transactionally move durable state from an explicit base bundle to current authority",
    )
    replan.add_argument("folder", help="shot folder")
    replan.add_argument("--layer", required=True, help="layer id")
    replan.add_argument("--base-run", required=True, help="run id that owns the previous bundle")
    replan.add_argument(
        "--base-bundle",
        required=True,
        help="full content hash of the previous immutable bundle",
    )
    replan.add_argument("--owner", required=True, help="authority applying the transaction")
    replan.add_argument("--trigger", required=True, help="reason the DAG changed")
    replan.add_argument(
        "--evidence",
        action="append",
        default=[],
        help="evidence locator; repeat for each item",
    )
    replan.add_argument(
        "--falsification",
        help="typed state/work-units hypothesis-falsification record to consume",
    )
    replan.add_argument(
        "--hard-constraint-approval",
        help="human approval evidence required when the finding names a hard constraint",
    )
    replan.add_argument(
        "--discard-accepted",
        action="store_true",
        help="retire accepted units too; discarding proven work is a deliberate decision "
        "and is recorded with the transaction",
    )
    replan.add_argument(
        "--preview",
        action="store_true",
        help="validate authority and print the invalidation closure without publishing state",
    )
    replan.set_defaults(handler=_replan)
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
