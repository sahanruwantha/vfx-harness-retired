"""Native image-payment transport; VFX owns measurement and guarded publication."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

import flynn_agents_sdk as flynn

from vfx_harness.agents.builder.attempt_guard import UnitAttemptGuard
from vfx_harness.evidence import image_check_operation
from vfx_harness.observability import prepared_publication, run_artifacts
from vfx_harness.orchestration.builder_execution_fence import require_builder_execution_lease
from vfx_harness.orchestration.plan_bundle_integrity import digest, read_real_file


@dataclass(frozen=True)
class ImageCheckCapability:
    tool: flynn.Tool
    guard: flynn.DispatchGuard


def image_check_tool(
    *, attempt_guard: UnitAttemptGuard, fence_lease, comparison_state: dict,
    check_candidate: Callable[[], None],
) -> ImageCheckCapability:
    """Bind an image-check operation to a building attempt and its candidate guard.

    The caller owns prior/candidate capture and registers the returned dispatch guard.
    This capability alone does not enable a raster builder or authorize acceptance.
    """
    root = attempt_guard.folder
    binding = f"work-unit-attempt:{attempt_guard.claim.claim_id}"

    def check_current():
        require_builder_execution_lease(fence_lease, root)
        attempt_guard.check("native image-payment operation")
        if attempt_guard.claim.phase != "building":
            raise ValueError("native image payments require a building claim")
        layout = run_artifacts.active(root)
        if layout is None or layout.run_id != attempt_guard.claim.run_id:
            raise ValueError("native image payments require the exact attempt's active run")
        if (comparison_state.get("unit_id") != attempt_guard.claim.unit_id
                or comparison_state.get("unit_hash") != attempt_guard.claim.unit_digest):
            raise ValueError("image-payment state does not belong to the exact claimed unit")
        records = [*(comparison_state.get("image_artifacts") or {}).values(),
                   *(comparison_state.get("image_adversaries") or {}).values()]
        for record in records:
            if (record.get("run_id") != layout.run_id
                    or record.get("unit_id") != attempt_guard.claim.unit_id
                    or record.get("parent_chain_hash") != comparison_state.get("parent_chain_hash")):
                raise ValueError("image record must belong to the exact run, unit and accepted parent chain")
            path = root / record["path"]
            if not path.is_relative_to(run_artifacts.readable_renders_dir(root)):
                raise ValueError("image payment requires this run's evidence/renders artifact")
            if digest(read_real_file(root, path, "native image payment")) != record["sha256"]:
                raise ValueError("image payment artifact changed; capture new evidence")
        check_candidate()

    check_current()

    async def dispatch_guard(_context):
        check_current()
        return flynn.GuardDecision(True, "current VFX building claim and candidate")

    async def execute(arguments):
        check_current()
        with fence_lease.operation(root):
            def publish(operation, prepare):
                check_current()
                update = prepare(binding)
                publication = update.publication
                try:
                    check_current()
                    if publication is not None:
                        attempt_guard.publish(operation, lambda: prepared_publication.commit_prepared_file(
                            publication, authority_binding=binding,
                        ))
                except BaseException:
                    # Discard this staged update only; never undo a committed payment.
                    if publication is not None:
                        prepared_publication.discard_prepared_file(publication)
                    raise
                return update.result

            result = image_check_operation.propose_checks(
                arguments, shot_dir=root, layer_id=attempt_guard.layer_id,
                comparison_state=comparison_state, selected_authority=attempt_guard.selected_authority,
                prepare_and_publish=publish,
            )
            check_current()
            return flynn.ToolResult(
                status="refused" if result.is_error else "ok",
                content=(flynn.TextContent(result.message),),
                data_json=json.dumps({
                    "schema": "vfx-harness.image-check-observation/v1",
                    "attempt": attempt_guard.claim.as_dict(),
                    "kept_ids": result.kept_ids, "unpaid_ids": result.unpaid_ids,
                    "accepted": False,
                }, sort_keys=True),
            )

    return ImageCheckCapability(
        flynn.Tool.structured(
            "propose_checks", description=image_check_operation.DESCRIPTION,
            parameters_json=json.dumps(image_check_operation.SCHEMA),
            validate=image_check_operation.validate_arguments, execute=execute, external_action=True,
        ),
        flynn.DispatchGuard("current-vfx-image-payment", dispatch_guard),
    )
