"""Auditable administrative transactions for durable work-unit state."""

from __future__ import annotations

import argparse
import hashlib
from datetime import datetime
from pathlib import Path

from vfx_harness.domain.brief import load_shot
from vfx_harness.domain.unit_outcomes import load_hypothesis_falsification
from vfx_harness.orchestration.authority_selection import resolve_selected_authority
from vfx_harness.orchestration.authority_selection_heads import (
    AuthoritySelectionHeadError,
    read_authority_selection_heads,
)
from vfx_harness.orchestration.authority_selection_transaction import (
    AuthoritySelectionConflict,
    authority_selection_lock,
    require_matching_authority_selection_token,
)
from vfx_harness.orchestration.ledger import load_layers, load_layers_from_path
from vfx_harness.orchestration.plan_authority import resolve_published_bundle
from vfx_harness.orchestration.selected_authority_guard import (
    commit_selected_authority,
)
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


def _unchanged_external_fault_owners(
    finding,
    *,
    folder: str | Path,
    requested_layer_id: str,
    base_layers,
    current_layers,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Fault owners a layer-local replan cannot prove were amended.

    HIR-0127 deliberately keeps an earlier-layer owner out of the affected seed set.
    Consuming that finding must therefore prove the selected authority changed the
    external owner; reopening an identical local DAG only repeats a known impossibility.
    """
    requested_ids = {
        unit.id for unit in current_layers[requested_layer_id].stages
    }
    external = sorted(set(finding.fault_owner_units) - requested_ids)
    if not external:
        return (), ()

    def _index(layers) -> dict[str, object]:
        return {
            unit.id: unit
            for layer in layers.values()
            for unit in layer.stages
        }

    old_by_id = _index(base_layers)
    new_by_id = _index(current_layers)
    current_layer_by_id = {
        unit.id: str(layer_id)
        for layer_id, layer in current_layers.items()
        for unit in layer.stages
    }
    unchanged: list[str] = []
    unresolved: list[str] = []
    for unit_id in external:
        old = old_by_id.get(unit_id)
        new = new_by_id.get(unit_id)
        if new is None:
            unresolved.append(unit_id)
        elif old is not None and unit_digest(old) == unit_digest(new):
            unchanged.append(unit_id)
        elif old is None:
            # Unit-first sparse bundles may omit the materialized earlier-layer DAG.
            # In that case, the durable replan audit is the exact old-identity bridge:
            # selected current state must match the new digest and carry a superseded
            # different digest recorded after this finding (HIR-0154).
            owner_layer = current_layer_by_id.get(unit_id)
            state = load_unit_state(folder, owner_layer) if owner_layer else None
            current_slot = ((state or {}).get("units") or {}).get(unit_id) or {}
            current_hash = unit_digest(new)
            try:
                finding_time = datetime.fromisoformat(str(finding.recorded_at))
            except ValueError:
                unresolved.append(unit_id)
                continue
            superseded_after_finding = False
            for row in ((state or {}).get("superseded") or []):
                if (
                    row.get("id") != unit_id
                    or row.get("unit_hash") == current_hash
                    or not isinstance(row.get("superseded_at"), str)
                ):
                    continue
                try:
                    superseded_time = datetime.fromisoformat(str(row["superseded_at"]))
                except ValueError:
                    continue
                if superseded_time > finding_time:
                    superseded_after_finding = True
                    break
            if (
                current_slot.get("unit_hash") != current_hash
                or not superseded_after_finding
            ):
                unresolved.append(unit_id)
    return tuple(unchanged), tuple(unresolved)


def _replan(args: argparse.Namespace) -> int:
    """Move durable unit state from one proven bundle DAG to current authority."""
    shot = load_shot(args.folder)
    selected = resolve_selected_authority(shot.folder)
    if selected.plan is None:
        raise SystemExit("replan requires selected global plan authority")
    current = selected.plan.bundle
    base = resolve_published_bundle(
        shot.folder,
        run_id=args.base_run,
        content_hash=args.base_bundle,
    )
    current_layers = load_layers_from_path(selected.artifact_paths["layers.json"])
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

    new_plan_hash = hashlib.sha256(
        selected.artifact_paths["layers.json"].read_bytes()
    ).hexdigest()
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
        unchanged_owners, unresolved_owners = _unchanged_external_fault_owners(
            finding,
            folder=shot.folder,
            requested_layer_id=layer_id,
            base_layers=base_layers,
            current_layers=current_layers,
        )
        if unchanged_owners or unresolved_owners:
            detail: list[str] = []
            if unchanged_owners:
                detail.append("unchanged=" + ",".join(unchanged_owners))
            if unresolved_owners:
                detail.append("unresolved=" + ",".join(unresolved_owners))
            raise SystemExit(
                "falsification assigns repair to out-of-layer fault owner units, but "
                "the selected authority does not prove those owners were amended ("
                + "; ".join(detail)
                + "). A layer-local replan cannot repair them: publish amended authority "
                "that changes the named owner units, then consume this finding; do not "
                "rerun the identical local DAG"
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
    try:
        with authority_selection_lock(shot.folder, exclusive=False):
            heads = read_authority_selection_heads(shot.folder)
            require_matching_authority_selection_token(
                selected.selection_token,
                heads.token,
            )
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
    except (AuthoritySelectionConflict, AuthoritySelectionHeadError) as exc:
        raise SystemExit(
            "selected authority changed before durable replan state mutation"
        ) from exc
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
    selected = resolve_selected_authority(shot.folder)
    layers = load_layers(shot, selected_authority=selected)
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
    try:
        commit_selected_authority(
            shot.folder,
            selected,
            operation="work-unit retry transition",
            mutation=lambda: transition(
                shot.folder,
                layer_id,
                args.unit,
                "retryable",
                reason=args.reason,
                metadata={"evidence": list(args.evidence)},
            ),
        )
    except AuthoritySelectionConflict as exc:
        raise SystemExit(
            "selected authority changed before durable retry transition"
        ) from exc
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
