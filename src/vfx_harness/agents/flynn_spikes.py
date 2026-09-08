"""Native VFX spike capability; scratch observations never select plan authority."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable

import anyio
import flynn_agents_sdk as flynn
from jsonschema import Draft202012Validator

from vfx_harness.agents import image_inputs, spike_policy
from vfx_harness.blender import artifact_execution, spike_execution
from vfx_harness.evidence import scene_checks
from vfx_harness.observability.run_artifacts import RunLayout
from vfx_harness.orchestration import authority_selection, plan_inputs
from vfx_harness.orchestration.authority_selection_transaction import require_matching_authority_selection_token
from vfx_harness.orchestration.plan_bundle_integrity import digest, read_real_file

SCHEMA = {
    "type": "object", "properties": {
        "script": {"type": "string", "minLength": 1, "maxLength": 16000},
        "contracts": {"type": "array", "maxItems": 8, "items": {"type": "object"}},
        "render_frame": {"type": ["integer", "null"]},
        "timeout": {"type": "integer", "minimum": 1, "maximum": 180},
    }, "required": ["script", "contracts", "render_frame", "timeout"], "additionalProperties": False,
}


def planning_spike_tools(
    *, layout: RunLayout, layer_id: str, check_current: Callable[[], None], blender: str = "blender",
) -> tuple[tuple[flynn.Tool, ...], flynn.DispatchGuard]:
    check_current()
    selected = authority_selection.resolve_selected_authority(layout.shot)
    layers = json.loads(read_real_file(layout.shot, selected.artifact_paths["layers.json"], "spike layers"))["layers"]
    if layer_id not in {str(row["id"]) for row in layers}:
        raise ValueError("spike planning requires an exact selected layer id")
    inputs_digest = plan_inputs.exact_planning_input_identity_digest(layout.shot)
    budget = spike_policy._SpikeBudget()
    validator = Draft202012Validator(SCHEMA)

    def check():
        check_current()
        current = authority_selection.resolve_selected_authority(layout.shot)
        require_matching_authority_selection_token(selected.selection_token, current.selection_token)
        if plan_inputs.exact_planning_input_identity_digest(layout.shot) != inputs_digest:
            raise ValueError("spike planning inputs changed; start a new bound attempt")

    def validate(arguments):
        errors = list(validator.iter_errors(arguments))
        if errors:
            raise ValueError("spike schema: " + "; ".join(error.message for error in errors[:3]))
        if len(json.dumps(arguments)) > 32000:
            raise ValueError("spike request exceeds the bounded script and contract payload")
        artifact_execution.validate_artifact_source(arguments["script"])
        rows = arguments["contracts"]
        if any(not isinstance(row.get("id"), str) or not row["id"].strip() for row in rows):
            raise ValueError("spike contract ids must be nonempty strings")
        if len({row["id"] for row in rows}) != len(rows):
            raise ValueError("spike contract ids must be distinct")
        for row in rows:
            error = scene_checks.validate_row(row)
            if error:
                raise ValueError(f"invalid spike contract: {error}")

    async def execute(arguments):
        validate(arguments)
        check()
        rows = arguments["contracts"]
        hypothesis = spike_policy._SpikeBudget.key(rows)
        refusal = spike_policy._spike_ineligibility(
            arguments["script"], arguments["render_frame"], rows, layout.shot,
        ) or budget.refusal(hypothesis)
        if refusal:
            return flynn.ToolResult(status="refused", content=(flynn.TextContent(refusal),))
        attempt = uuid.uuid4().hex
        directory = layout.scratch / "native-spikes" / attempt
        budget.record(hypothesis, ran_blender=True, failed=False)

        def run():
            return spike_execution.execute_spike(
                layout=layout, directory=directory, script=arguments["script"], contracts=rows,
                render_frame=arguments["render_frame"], timeout=arguments["timeout"],
                blender=blender, check_current=check,
            )

        try:
            result = await anyio.to_thread.run_sync(run)
        except BaseException:
            # Account for an uncertain attempt without inventing an execution result.
            budget.record(hypothesis, ran_blender=False, failed=True)
            raise
        failed = result["status"] != "measured" or (bool(rows) and not result["passed"])
        budget.record(hypothesis, ran_blender=False, failed=failed)
        check()
        files = {}
        for path in sorted(directory.rglob("*")):
            if path.is_file() or path.is_symlink():
                payload = read_real_file(layout.shot, path, "native spike artifact")
                files[str(path.relative_to(layout.root))] = digest(payload)
        report = {"schema": "vfx-harness.native-spike/v1", "layer": layer_id,
                  "base_selection": selected.selection_token.to_dict(), "planning_inputs_sha256": inputs_digest,
                  "contracts": rows, "hypothesis": hypothesis, "result": result, "artifacts": files,
                  "remaining_attempts": budget.session_cap - budget.total,
                  "plan_authority_changed": False, "planning_evidence_published": False}
        report_path = layout.write_report(f"native-spike-{attempt}", report)
        report_bytes = read_real_file(layout.shot, report_path, "native spike report")
        content = [flynn.TextContent(
            f"Spike {result['status']}; independent contract evaluation passed: {result['passed']}. "
            "This scratch observation is not a published planning-evidence receipt or scene acceptance."
        )]
        render = directory / "outputs/render.png"
        if result["status"] == "measured" and render.exists():
            snapshot, _ = image_inputs.snapshot_image(layout.shot, str(render.relative_to(layout.shot)))
            content.append(flynn.ImageContent(snapshot.url))
        check()
        return flynn.ToolResult(status="refused" if failed else "ok", content=tuple(content), data_json=json.dumps({
            "schema": report["schema"], "report": str(report_path.relative_to(layout.root)),
            "sha256": digest(report_bytes),
            "result": result if len(json.dumps(result)) <= 8000 else {
                "status": result["status"], "passed": result["passed"], "details_in_report": True,
            },
            "remaining_attempts": report["remaining_attempts"], "planning_evidence_published": False,
        }, sort_keys=True))

    async def guard(_):
        check()
        return flynn.GuardDecision(True, "VFX spike planning scope remains current")

    return ((flynn.Tool.structured("spike", description=(
        "Test a small construction technique in a confined empty Blender scene. Artifact Python policy applies. "
        "A fresh process evaluates the exact contracts; stdout is diagnostic only. Four attempts per session, "
        "two failures per hypothesis. Scratch results require separate VFX publication before plan citation."
    ), parameters_json=json.dumps(SCHEMA), validate=validate, execute=execute, external_action=True),),
            flynn.DispatchGuard("current-vfx-spike-scope", guard))
