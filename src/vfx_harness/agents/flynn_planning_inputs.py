"""Native planning reference selection and durable supervisor questions."""

from __future__ import annotations

import json
from pathlib import PurePosixPath

import flynn_agents_sdk as flynn
from jsonschema import Draft202012Validator
from PIL import UnidentifiedImageError

from vfx_harness.agents import image_inputs
from vfx_harness.agents.planning_workspace import PlanningWorkspace
from vfx_harness.orchestration import escalate
from vfx_harness.orchestration.plan_bundle_integrity import digest, read_real_file

_TEXT = {"type": "string", "minLength": 1, "maxLength": 2000}
_IMPACT = {"type": "array", "maxItems": 64, "uniqueItems": True,
           "items": {"type": "string", "minLength": 1, "maxLength": 128}}
QUESTION_SCHEMA = {
    "type": "object", "properties": {
        "question": _TEXT, "assumption": _TEXT, "why_it_matters": _TEXT,
        "affected_layers": _IMPACT, "affected_axes": _IMPACT, "global_decision": {"type": "boolean"},
    },
    "required": ["question", "assumption", "why_it_matters", "affected_layers", "affected_axes", "global_decision"],
    "additionalProperties": False,
}


def planning_input_tools(binding: PlanningWorkspace) -> tuple[flynn.Tool, ...]:
    """Select references explicitly; record questions without inventing answers or authority."""
    validator = Draft202012Validator(QUESTION_SCHEMA)
    layout = binding.layout
    references = tuple(sorted(
        name for name in binding.record["authored_inputs"]
        if PurePosixPath(name).parts[:1] == ("refs",)
        and PurePosixPath(name).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
    ))

    def validate_question(arguments):
        errors = [error.message for error in validator.iter_errors(arguments)]
        if errors:
            raise ValueError("supervisor question schema: " + "; ".join(errors))
        if any(not arguments[key].strip() for key in ("question", "assumption", "why_it_matters")):
            raise ValueError("supervisor question, assumption and reason must be nonempty")
        if not arguments["global_decision"] and not arguments["affected_layers"] and not arguments["affected_axes"]:
            raise ValueError("supervisor question requires affected layers/axes or global_decision=true")
        binding.check()
        if arguments["affected_layers"] or arguments["affected_axes"]:
            if binding.owned["ownership_mapping.json"] is None:
                raise ValueError("scoped supervisor questions require a published ownership mapping")
            mapping = json.loads(read_real_file(layout.shot, binding.root / "ownership_mapping.json", "question scope"))
            layers = {row["id"] for row in mapping["layers"]}
            axes = {row["key"] for row in mapping["axes"]}
            if set(arguments["affected_layers"]) - layers or set(arguments["affected_axes"]) - axes:
                raise ValueError("supervisor question impact must name layers and axes from the current mapping")

    async def ask(arguments):
        binding.check()
        authority = f"native-global-question:{layout.run_id}:{digest(binding.marker_bytes)}"
        prepared = escalate.prepare_question(layout.shot, layer="PLAN", authority_binding=authority, **arguments)
        try:
            binding.check()
            qid = escalate.commit_prepared_question(prepared, authority_binding=authority)
        except BaseException:
            # Discard only this staged CAS publication; never erase a committed row.
            escalate.discard_prepared_question(prepared)
            raise
        path = layout.shot / escalate.QUESTIONS
        payload = read_real_file(layout.shot, path, "committed supervisor question stream")
        records = {row["id"]: row for row in escalate.parse_questions(payload, path)}
        if qid not in records:
            raise ValueError("committed supervisor question is absent from the verified event stream")
        record = records[qid]
        if not {"affected_layers", "affected_axes", "global_decision"} <= record.keys():
            raise ValueError("stored supervisor question lacks the required impact schema; migrate it explicitly")
        binding.check()
        data = json.dumps({
            "schema": "vfx-harness.plan-question-observation/v1", **binding.identity(),
            "created": prepared.update.result[1], "question": record,
            "ledger": escalate.QUESTIONS, "ledger_sha256": digest(payload),
            "plan_authority_changed": False,
        }, sort_keys=True)
        if len(data) > 32_000:
            raise ValueError("stored supervisor question exceeds bounded feedback; inspect the question ledger")
        return flynn.ToolResult(
            content=(flynn.TextContent(
                f"Question Q{qid} recorded or already present. Use its stored assumption and impact scope. "
                "No answer or plan approval was generated."
            ),), data_json=data,
        )

    def validate_reference(arguments):
        if set(arguments) != {"name"} or arguments["name"] not in references:
            raise ValueError("read_reference requires one name from the bound reference-image list")

    async def read_reference(arguments):
        binding.check()
        name = arguments["name"]
        try:
            snapshot, source = image_inputs.snapshot_image(binding.root, name)
        except (ValueError, UnidentifiedImageError) as exc:
            binding.check()
            return flynn.ToolResult(
                status="refused", content=(flynn.TextContent(str(exc)[:2000]),),
                data_json=json.dumps({
                    "schema": "vfx-harness.plan-reference/v1", **binding.identity(),
                    "path": name, "sha256": binding.record["authored_inputs"][name], "valid": False,
                }),
            )
        binding.check()
        if source["sha256"] != binding.record["authored_inputs"][name]:
            raise ValueError("reference snapshot differs from the exact planning input generation")
        return flynn.ToolResult(
            content=(flynn.TextContent(f"Selected planning reference: {name}"), flynn.ImageContent(snapshot.url)),
            data_json=json.dumps({
                "schema": "vfx-harness.plan-reference/v1", **binding.identity(), **source, "valid": True,
            }),
        )

    tools = [flynn.Tool.structured(
        "ask_supervisor", description=(
            "Record an ambiguity the human must settle and the assumption to proceed on. "
            "Declare affected mapping layers/axes, or global_decision only if the whole plan depends on it. "
            "A duplicate returns the existing question's actual assumption and scope."
        ), parameters_json=json.dumps(QUESTION_SCHEMA), validate=validate_question, execute=ask, external_action=True,
    )]
    if references:
        tools.append(flynn.Tool.structured(
            "read_reference", description=(
                "Read one explicitly selected bound PNG/JPEG/WEBP reference, at most 8 MiB. "
                "Returns verified image content and its hash; does not select images for later prompts automatically."
            ), parameters_json=json.dumps({
                "type": "object", "properties": {"name": {"type": "string", "enum": references}},
                "required": ["name"], "additionalProperties": False,
            }), validate=validate_reference, execute=read_reference,
        ))
    return tuple(tools)
