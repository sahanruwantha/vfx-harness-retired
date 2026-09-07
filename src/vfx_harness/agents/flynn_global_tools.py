"""Native global-planner capabilities sharing one exact VFX workspace binding."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import PurePosixPath
from uuid import uuid4

import flynn_agents_sdk as flynn

from vfx_harness.agents import flynn_mapping_tools, flynn_planning_inputs
from vfx_harness.agents.planning_workspace import PlanningWorkspace
from vfx_harness.domain.brief import load_shot
from vfx_harness.evaluation import plan_gate
from vfx_harness.evaluation.plan_gate.types import _CITE
from vfx_harness.observability.run_artifacts import RunLayout
from vfx_harness.orchestration.plan_bundle_integrity import digest, read_real_file

READ_CHARS = 4_000
FEEDBACK_CHARS = 8_000
GATE_CALLS = 4


def global_planning_tools(
    *, layout: RunLayout, check_current: Callable[[], None],
) -> tuple[tuple[flynn.Tool, ...], flynn.DispatchGuard]:
    """Register publication, selected text reads and bounded deterministic gate feedback.

    The caller owns session limits, context selection and the live run fence. Gate
    observations never authorize plan publication. Every gate writes a complete audit
    report, so it is charged as an external action even though it changes no plan bytes.
    """
    binding = PlanningWorkspace(layout, check_current)
    readable = tuple(sorted({
        "brief.md", *binding.record["decision_inputs"], *binding.owned,
    }))
    gate_calls = 0
    previous_signature = None
    plateau = False
    feedback_overflow = False

    def gate_scope():
        binding.require_closed_tree()
        text = read_real_file(layout.shot, binding.root / "plans/global.md", "native gate draft").decode("utf-8")
        for match in _CITE.finditer(text):
            path = PurePosixPath(match.group(1).replace("\\", "/"))
            if path.is_absolute() or ".." in path.parts:
                raise ValueError(f"native gate citation leaves its bound workspace: {path}")

    def gate_admission():
        if gate_calls >= GATE_CALLS:
            raise ValueError("run_gate call cap reached (4); finish the bounded planning sweep")
        if plateau:
            raise ValueError("run_gate plateau: unchanged findings; the outer loop owns further repair")
        if feedback_overflow:
            raise ValueError("run_gate feedback exceeds bounded context; inspect its report outside this sweep")
        if any(value is None for value in binding.owned.values()):
            raise ValueError("run_gate requires a complete published draft in the bound workspace")
        gate_scope()

    async def guard(context):
        binding.check()
        if context.call.name == "run_gate":
            gate_admission()
        return flynn.GuardDecision(True, "exact VFX planning workspace and tool admission remain current")

    def validate_read(arguments):
        if set(arguments) != {"name", "offset"} or arguments["name"] not in readable:
            raise ValueError("read_plan_input requires a name from its explicit document list and an offset")
        offset = arguments["offset"]
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("read_plan_input offset must be a non-negative character index")

    async def read(arguments):
        binding.check()
        name, offset = arguments["name"], arguments["offset"]
        path = binding.root / name
        if not path.exists():
            return flynn.ToolResult(
                status="refused", content=(flynn.TextContent("Draft document is not written yet."),),
                data_json=json.dumps({
                    "schema": "vfx-harness.plan-read/v1", **binding.identity(), "name": name, "available": False,
                }),
            )
        payload = read_real_file(layout.shot, path, "selected planning text")
        text = payload.decode("utf-8")
        binding.check()
        if offset > len(text):
            return flynn.ToolResult(
                status="refused", content=(flynn.TextContent(
                    f"Offset {offset} exceeds this document's length. Use an offset between 0 and {len(text)}."
                ),), data_json=json.dumps({
                    "schema": "vfx-harness.plan-read/v1", **binding.identity(), "name": name,
                    "available": True, "error": "offset_out_of_range", "sha256": digest(payload),
                    "offset": offset, "total_chars": len(text),
                }),
            )
        end = min(len(text), offset + READ_CHARS)
        return flynn.ToolResult(
            content=(flynn.TextContent(text[offset:end]),),
            data_json=json.dumps({
                "schema": "vfx-harness.plan-read/v1", **binding.identity(), "name": name, "available": True,
                "sha256": digest(payload), "offset": offset, "end": end,
                "total_chars": len(text), "next_offset": end if end < len(text) else None,
            }),
        )

    def validate_gate(arguments):
        if arguments:
            raise ValueError("run_gate takes no arguments; its draft is selected by the harness")

    async def gate(arguments):
        nonlocal gate_calls, previous_signature, plateau, feedback_overflow
        binding.check()
        gate_admission()
        gate_calls += 1
        identity = binding.identity()
        result = plan_gate.run(binding.root, require_scene_checks=True)
        result.shot = load_shot(binding.root).id
        binding.check()
        gate_scope()
        signature = result.signature()
        plateau = not result.clean and signature == previous_signature
        previous_signature = signature
        evaluation = result.to_dict(outcome=result.publishable_outcome)
        report = layout.write_report(f"native-plan-gate-{uuid4().hex}", {
            "schema": "vfx-harness.plan-gate-observation/v1", **identity,
            "evaluation": evaluation, "gate_attested": False,
        })
        report_bytes = read_real_file(layout.shot, report, "native gate audit report")
        binding.check()
        gate_scope()
        # Whole findings only, with blockers first. The full evaluation remains in
        # the hashed run report; omitted findings cannot turn a dirty gate clean.
        selected = []
        for finding in sorted(evaluation["findings"], key=lambda row: row["severity"] != "blocking"):
            if len(json.dumps([*selected, finding], ensure_ascii=False)) > FEEDBACK_CHARS:
                break
            selected.append(finding)
        feedback_overflow = bool(result.findings) and not selected
        halt = (
            "feedback_overflow" if feedback_overflow else
            "plateau" if plateau else "gate_call_cap" if gate_calls == GATE_CALLS else None
        )
        return flynn.ToolResult(
            content=(flynn.TextContent(
                f"Gate {'clean' if result.clean else 'dirty'}; {len(result.blocking)} blocking findings. "
                + (f"Stop this sweep: {halt}. " if halt else "")
                + "This observation is not plan publication authority."
            ),),
            data_json=json.dumps({
                "schema": "vfx-harness.plan-gate-feedback/v1", **identity,
                "clean": result.clean, "blocking_count": len(result.blocking),
                "signature": signature, "findings": selected,
                "omitted_findings": len(result.findings) - len(selected),
                "gate_calls": gate_calls, "remaining_gate_calls": GATE_CALLS - gate_calls,
                "halt_reason": halt, "report": str(report.relative_to(layout.root)),
                "report_sha256": digest(report_bytes), "gate_attested": False,
            }, sort_keys=True),
        )

    return (
        (
            flynn_mapping_tools.ownership_mapping_publication(binding),
            flynn.Tool.structured(
                "read_plan_input", description=(
                    "Read up to 4000 characters of an explicitly listed planning document. "
                    "Use next_offset to continue; no arbitrary paths, directories, or search."
                ), parameters_json=json.dumps({
                    "type": "object", "properties": {
                        "name": {"type": "string", "enum": readable},
                        "offset": {"type": "integer", "minimum": 0},
                    }, "required": ["name", "offset"], "additionalProperties": False,
                }), validate=validate_read, execute=read,
            ),
            flynn.Tool.structured(
                "run_gate", description=(
                    "Evaluate the current complete global draft and record its exact inputs and full findings. "
                    "At most four evaluations per sweep; unchanged dirty findings end the sweep."
                ), parameters_json=json.dumps({
                    "type": "object", "properties": {}, "additionalProperties": False,
                }), validate=validate_gate, execute=gate, external_action=True,
            ),
            *flynn_planning_inputs.planning_input_tools(binding),
        ),
        flynn.DispatchGuard("current-vfx-global-planning-attempt", guard),
    )
