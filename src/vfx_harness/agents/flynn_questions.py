"""Native question publication uses the VFX prepared append transaction."""

import json
from collections.abc import Callable

import flynn_agents_sdk as flynn

from vfx_harness.observability.run_artifacts import RunLayout
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


async def record_question(
    *, layout: RunLayout, arguments: dict, check_current: Callable[[], None],
    authority_binding: str, identity: Callable[[], dict],
) -> flynn.ToolResult:
    check_current()
    prepared = escalate.prepare_question(layout.shot, layer="PLAN", authority_binding=authority_binding, **arguments)
    try:
        check_current()
        qid = escalate.commit_prepared_question(prepared, authority_binding=authority_binding)
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
    check_current()
    data = json.dumps({
        "schema": "vfx-harness.plan-question-observation/v1", **identity(),
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

