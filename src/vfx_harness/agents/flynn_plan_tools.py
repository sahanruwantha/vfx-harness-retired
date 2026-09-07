"""Native Flynn capabilities for planning, bound to VFX-owned publication targets."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path

import flynn_agents_sdk as flynn

from vfx_harness.orchestration import authority_selection, unit_plan_content
from vfx_harness.orchestration.authority_selection import ResolvedSelectedAuthority
from vfx_harness.orchestration.authority_selection_transaction import require_matching_authority_selection_token
from vfx_harness.orchestration.layer_plans import validate_work_unit_plan_authority, work_unit_plan_authority_path
from vfx_harness.orchestration.plan_bundle_integrity import read_real_file, require_real_directory
from vfx_harness.orchestration.work_unit_plan_transaction import WorkUnitPlanTransaction

UNIT_PLAN_SCHEMA = {
    "type": "object",
    "properties": {"content": {"type": "string", "minLength": 200, "maxLength": 24000}},
    "required": ["content"],
    "additionalProperties": False,
}


def _validate_unit_plan(arguments: dict[str, object]) -> None:
    if set(arguments) != {"content"} or not isinstance(arguments["content"], str):
        raise ValueError("publish_unit_plan requires exactly one string field: content")
    content = arguments["content"]
    if len(content.strip()) < 200 or len(content) > 24000:
        raise ValueError("unit plan requires at least 200 trimmed characters and at most 24000 total characters")
    if content.count("\n") + 1 > 160:
        raise ValueError("unit plan exceeds 160 lines; keep evidence in machine contracts")


def unit_plan_publication(
    *,
    shot_folder: Path,
    target: Path,
    selected_authority: ResolvedSelectedAuthority,
    check_current: Callable[[], None],
    transaction: WorkUnitPlanTransaction,
) -> tuple[flynn.Tool, flynn.DispatchGuard]:
    """Bind content-only publication and its pre-dispatch guard.

    The caller owns the planning transaction, its serialization fence and the target
    selection. It must claim the current plan/stamp pair before constructing this tool.
    ``check_current`` must prove that exact attempt is still permitted.
    Register the returned guard with Session/Runtime: it runs before tool/external
    reservations. The handler checks again immediately before the existing writer.
    No terminal gate or acceptance is synthesized from successful tool execution.
    """
    root = shot_folder.absolute()
    destination = target.absolute()
    if selected_authority.plan is None:
        raise ValueError("native unit planning requires selected global plan authority")
    try:
        relative = destination.relative_to(root)
    except ValueError as exc:
        raise ValueError("unit-plan target must belong to the active shot") from exc
    if relative.parts[:1] != ("plans",) or ".." in relative.parts or relative == Path("plans"):
        raise ValueError("unit-plan target must be a file inside the active shot's plans directory")

    try:
        selected_authority.plan.bundle.root.absolute().relative_to(root)
    except ValueError as exc:
        raise ValueError("selected plan authority belongs to another shot") from exc

    if transaction.target != destination or transaction.authority != work_unit_plan_authority_path(destination):
        raise ValueError("unit-plan transaction does not own the exact target and integrity stamp")
    transaction.require_owned_current()

    def check_bound():
        transaction.require_owned_current()
        check_current()
        current = authority_selection.resolve_selected_authority(root)
        require_matching_authority_selection_token(selected_authority.selection_token, current.selection_token)
        require_real_directory(root, destination.parent, "unit-plan target parent")
        if destination.exists() or destination.is_symlink():
            read_real_file(root, destination, "existing unit-plan target")

    async def guard(_):
        check_bound()
        return flynn.GuardDecision(True, "exact VFX unit-plan attempt remains current")

    async def publish(arguments):
        check_bound()
        try:
            path, lines = unit_plan_content.publish_unit_plan_content(
                root, destination, arguments["content"], selected_authority=selected_authority,
            )
        finally:
            # A partial pair remains an external effect in SQLite. Record the precise
            # bytes so the owning VFX transaction can explicitly roll them back.
            transaction.claim_current()
        validate_work_unit_plan_authority(root, path, require_gate=False, selected_authority=selected_authority)
        payload = read_real_file(root, path, "published unit-plan observation")
        sidecar = work_unit_plan_authority_path(path)
        stamp = read_real_file(root, sidecar, "published unit-plan integrity stamp")
        record = {
            "schema": "vfx-harness.unit-plan-observation/v1",
            "path": relative.as_posix(), "sha256": hashlib.sha256(payload).hexdigest(),
            "lines": lines, "bundle_hash": selected_authority.plan.bundle.content_hash,
            "integrity_stamp": sidecar.relative_to(root).as_posix(),
            "integrity_stamp_sha256": hashlib.sha256(stamp).hexdigest(),
            "selection": selected_authority.selection_token.to_dict(),
            "gate_attested": False,
        }
        return flynn.ToolResult(
            content=(flynn.TextContent(
                "Unit-plan content and integrity stamp written; terminal gate still required."
            ),),
            data_json=json.dumps(record, sort_keys=True),
        )

    return (
        flynn.Tool.structured(
            "publish_unit_plan",
            description=("Publish the complete bounded unit plan to the harness-selected target. "
                         "No path argument. This stamps integrity, not gate approval or build acceptance."),
            parameters_json=json.dumps(UNIT_PLAN_SCHEMA),
            validate=_validate_unit_plan, execute=publish, external_action=True,
        ),
        flynn.DispatchGuard("current-vfx-unit-plan-attempt", guard),
    )
