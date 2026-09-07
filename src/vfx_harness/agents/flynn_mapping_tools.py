"""Native global-plan drafting in a run-owned, revision-bound workspace."""

from __future__ import annotations

import json
from collections.abc import Callable

import flynn_agents_sdk as flynn
from jsonschema import Draft202012Validator

from vfx_harness.observability import prepared_publication
from vfx_harness.observability.run_artifacts import RunLayout
from vfx_harness.orchestration import authority_selection, plan_authoring, plan_inputs
from vfx_harness.orchestration.authority_selection_transaction import require_matching_authority_selection_token
from vfx_harness.orchestration.plan_bundle_integrity import digest, read_real_file, require_real_directory

MAX_MAPPING_BYTES = 64_000


def ownership_mapping_publication(
    *, layout: RunLayout, check_current: Callable[[], None],
) -> tuple[flynn.Tool, flynn.DispatchGuard]:
    """Bind the sole model-authored mapping to an already prepared workspace.

    The caller holds the run's owner fence and supplies its live-attempt check.
    Successful expansion is a draft observation, never gate or publication authority.
    A failed write remains unresolved in Flynn; the caller must discard or explicitly
    reconcile that workspace rather than repeat the operation automatically.
    """
    workspace = layout.scratch / "plan-workspace"
    marker = workspace / ".plan-workspace.json"
    names = ("ownership_mapping.json", *plan_authoring.MAPPING_ARTIFACTS)
    marker_bytes = read_real_file(layout.shot, marker, "native planning workspace marker")
    record = plan_inputs.read_workspace_marker(layout.shot, marker)
    if record["run_id"] != layout.run_id or record["shot"] != str(layout.shot):
        raise ValueError("native planning workspace must belong to the exact current shot and run")
    base = plan_inputs.workspace_base_selection(record)

    def outputs():
        identities = {}
        for name in names:
            path = workspace / name
            if path.parent.exists() or path.parent.is_symlink():
                require_real_directory(layout.shot, path.parent, "native mapping output parent")
            if path.exists() and path.stat().st_nlink != 1:
                raise ValueError("native mapping output must not share a hard-linked inode")
            identities[name] = (
                digest(read_real_file(layout.shot, path, "native mapping output"))
                if path.exists() or path.is_symlink() else None
            )
        return identities

    def check_inputs():
        check_current()
        if read_real_file(layout.shot, marker, "native planning workspace marker") != marker_bytes:
            raise ValueError("native planning workspace marker changed; start a new bound attempt")
        current = authority_selection.resolve_selected_authority(layout.shot)
        require_matching_authority_selection_token(base, current.selection_token)
        for root in (layout.shot, workspace):
            plan_inputs.require_exact_planning_input_identity(
                root, expected_authored_inputs=record["authored_inputs"],
                expected_decision_inputs=record["decision_inputs"], where="native planning workspace",
            )

    check_inputs()
    owned = outputs()
    registry = plan_authoring.clause_registry(workspace / "brief.md")
    schema = plan_authoring.ownership_mapping_authoring_schema()
    validator = Draft202012Validator(schema)

    def validate(arguments):
        errors = [error.message for error in validator.iter_errors(arguments)]
        if errors:
            raise ValueError("ownership mapping schema: " + "; ".join(errors))
        if len(json.dumps(arguments, ensure_ascii=False).encode("utf-8")) > MAX_MAPPING_BYTES:
            raise ValueError(f"ownership mapping exceeds {MAX_MAPPING_BYTES} bytes; reduce the global draft")
        errors = plan_authoring.validate_mapping(arguments, registry, workspace / "refs")
        if errors:
            raise ValueError("ownership mapping is invalid: " + "; ".join(errors))

    def check_bound():
        check_inputs()
        if outputs() != owned:
            raise ValueError("native planning output changed outside this attempt; preserve the newer draft")

    async def guard(_):
        check_bound()
        return flynn.GuardDecision(True, "exact VFX planning workspace and input generation remain current")

    async def publish(arguments):
        nonlocal owned
        check_bound()
        payload = (json.dumps(arguments, indent=1, ensure_ascii=False) + "\n").encode("utf-8")

        def replace_mapping(current):
            observed = digest(current) if current is not None else None
            if observed != owned["ownership_mapping.json"]:
                raise ValueError("ownership mapping changed before publication; preserve the newer draft")
            return payload, None

        prepared_publication.publish_file_update(
            layout.shot, workspace / "ownership_mapping.json", replace_mapping,
            authority_binding=f"native-mapping:{layout.run_id}:{digest(marker_bytes)}",
        )
        written = plan_authoring.expand_mapping(workspace, arguments)
        if set(written) != set(plan_authoring.MAPPING_ARTIFACTS):
            raise ValueError("mapping expansion did not produce its complete declared artifact set")
        # Independently reopen all generated bytes; retain exact identities across
        # any next inference so another writer's revision cannot be overwritten.
        owned = outputs()
        if any(value is None for value in owned.values()):
            raise ValueError("mapping expansion left missing artifacts; discard or reconcile the incomplete draft")
        check_inputs()
        return flynn.ToolResult(
            content=(flynn.TextContent(
                "Global draft expanded. Deterministic gate and authority publication remain due."
            ),),
            data_json=json.dumps({
                "schema": "vfx-harness.mapping-observation/v1",
                "workspace": str(workspace.relative_to(layout.root)),
                "base_selection": base.to_dict(), "workspace_marker_sha256": digest(marker_bytes),
                "artifacts": owned, "gate_attested": False,
            }, sort_keys=True),
        )

    return (
        flynn.Tool.structured(
            "publish_ownership_mapping",
            description=("Submit the complete ownership/DAG mapping, at most 64000 UTF-8 JSON bytes. "
                         "The harness validates and expands the draft in its bound workspace. "
                         "No path argument; this does not gate or publish selected authority."),
            parameters_json=json.dumps(schema), validate=validate, execute=publish, external_action=True,
        ),
        flynn.DispatchGuard("current-vfx-mapping-attempt", guard),
    )
