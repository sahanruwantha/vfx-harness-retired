"""Native Flynn transport for one advisory approach recommendation.

The harness selects all context and interprets the recommendation. The SDK records
execution, guards and usage; neither a successful tool nor session accepts VFX work.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
from collections.abc import Callable
from dataclasses import asdict, replace
from pathlib import Path
from uuid import uuid4

import flynn_agents_sdk as flynn
from PIL import Image

from vfx_harness.observability import run_artifacts
from vfx_harness.orchestration.authority_selection_transaction import durably_ensure_real_directory
from vfx_harness.orchestration.plan_bundle_integrity import read_real_file

MAX_CONTEXT_CHARACTERS = 24000
MAX_IMAGE_BYTES = 8 * 1024 * 1024
LIMITS = flynn.RunLimits(1, 1, 0, 90, output_tokens=2048)


def image_input(folder: Path, relative: str) -> tuple[flynn.ImageInput, dict]:
    """Snapshot a selected local still, refusing links, escapes and unsupported bytes."""
    payload = read_real_file(folder, folder / relative, "approach review image")
    if len(payload) > MAX_IMAGE_BYTES:
        raise ValueError(f"approach image {relative!r} exceeds {MAX_IMAGE_BYTES} bytes")
    with Image.open(io.BytesIO(payload)) as image:
        mime = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}.get(image.format)
        if mime is None:
            raise ValueError(f"approach image {relative!r} must be PNG, JPEG or WEBP")
        image.verify()
    url = f"data:{mime};base64,{base64.b64encode(payload).decode('ascii')}"
    return flynn.ImageInput(url), {"path": relative, "sha256": hashlib.sha256(payload).hexdigest()}


def validate_recommendation(value: dict[str, object]) -> None:
    if set(value) != {"verdict", "why", "do"}:
        raise ValueError("approach recommendation requires exactly verdict, why and do")
    if value["verdict"] not in ("KEEP", "REPLACE"):
        raise ValueError("approach verdict must be KEEP or REPLACE")
    for field in ("why", "do"):
        text = value[field]
        if not isinstance(text, str) or not text.strip() or len(text) > 3000:
            raise ValueError(f"approach {field} must contain 1–3000 characters of nonempty text")


class _AdvisoryEvaluator:
    async def evaluate(self, candidate):
        result = flynn.ToolResult.from_json(candidate.output)
        validate_recommendation(json.loads(result.data_json))
        return flynn.Evaluation(
            candidate, "vfx-approach-advice/v1", "recommendation structure only",
            flynn.Verdict.SATISFIED, "advice cannot establish build acceptance",
        )


async def execute(
    *,
    folder: Path,
    layer_id: str,
    inference: flynn.InferenceAdapter,
    context: flynn.ContextPacket,
    images: tuple[flynn.ImageInput, ...],
    sources: tuple[dict, ...],
    check_current: Callable[[], None],
) -> dict:
    """Persist one bounded recommendation, returning only after the session seals."""
    check_current()
    layout = run_artifacts.active(folder)
    if layout is None:
        raise ValueError("approach review requires an active owning VFX run")
    invocation = f"approach-{uuid4().hex}"
    database = layout.checkpoints / "flynn" / f"{invocation}.sqlite"
    durably_ensure_real_directory(folder, database.parent.relative_to(folder))
    inputs = {
        "schema": "vfx-harness.approach-inputs/v1", "layer": layer_id,
        "sources": sources, "included_context": context.included_ids,
        "omitted_context": context.omitted_ids,
        "context_sha256": hashlib.sha256(context.text.encode()).hexdigest(),
    }

    async def submit(arguments):
        return flynn.ToolResult(data_json=json.dumps(arguments, sort_keys=True))

    async def guard(_):
        check_current()
        return flynn.GuardDecision(True, "owning VFX attempt remains current")

    tool = flynn.Tool.structured(
        "submit_review", description="Submit advisory technique review; this cannot accept a build.",
        parameters_json=json.dumps({
            "type": "object", "additionalProperties": False,
            "properties": {
                "verdict": {"type": "string", "enum": ["KEEP", "REPLACE"]},
                "why": {"type": "string", "minLength": 1, "maxLength": 3000},
                "do": {"type": "string", "minLength": 1, "maxLength": 3000},
            }, "required": ["verdict", "why", "do"],
        }), validate=validate_recommendation, execute=submit,
    )
    with flynn.SQLiteRun.create(
        database, run_id=invocation, initial_state=json.dumps(inputs, sort_keys=True), limits=LIMITS,
    ) as run:
        session = flynn.Session(
            inference=inference, tools=flynn.ToolBroker([tool]), evaluator=_AdvisoryEvaluator(), run=run,
            grants=("submit_review",),
            policy=lambda view: (
                flynn.SessionStop("advisory recommendation recorded; no build acceptance")
                if view.completed_steps else flynn.SessionStep(context.text, ("submit_review",))
            ),
            prepare_request=lambda request: replace(request, images=images),
            guards=(flynn.DispatchGuard("current-vfx-attempt", guard),),
        )
        try:
            await session.execute()
            result = flynn.ToolResult.from_json(run.latest_observation())
            recommendation = json.loads(result.data_json)
            validate_recommendation(recommendation)
            return {
                "replace": recommendation["verdict"] == "REPLACE",
                "text": (f"VERDICT: {recommendation['verdict']}\n"
                         f"WHY: {recommendation['why']}\nDO: {recommendation['do']}"),
            }
        finally:
            layout.write_report(invocation, {
                "schema": "vfx-harness.approach-review/v1", "layer": layer_id,
                "journal": str(database.relative_to(layout.root)),
                "inputs": inputs, "usage": run.usage_summary(),
                "output_budget": asdict(run.output_budget()),
                "termination": run.outcome(),
                "pricing_status": "unpriced",
            })
