"""Native Flynn observation transport for one VFX critic decision.

This records a structured opinion, not qualified judgment or acceptance. The owning
VFX caller supplies current authority checks and must establish qualification before
using an observation to decide production work. No legacy model transport is imported.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict, replace
from pathlib import Path
from uuid import uuid4

import flynn_agents_sdk as flynn
from jsonschema import Draft202012Validator

from vfx_harness.agents import image_inputs
from vfx_harness.domain.critic_verdict import critic_verdict_schema
from vfx_harness.observability import run_artifacts
from vfx_harness.orchestration.authority_selection_transaction import durably_ensure_real_directory
from vfx_harness.orchestration.plan_bundle_integrity import read_real_file

MAX_CONTEXT_CHARACTERS = 24000
LIMITS = flynn.RunLimits(1, 1, 0, 180, output_tokens=8192)
IMAGE_SHAPE = "vfx-harness.critic-images/v1"


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _snapshot_images(folder: Path, images: tuple[tuple[str, str], ...]):
    """Closed image slots preserve identity/order without an accumulated history."""
    roles = [role for role, _path in images]
    if roles[:2] != ["reference", "candidate"]:
        raise ValueError("critic images must begin with reference then candidate")
    limits = {"reference": 1, "candidate": 1, "focus": 2, "motion": 1, "prior": 1}
    if any(role not in limits or roles.count(role) > limits[role] for role in roles):
        raise ValueError("critic permits reference, candidate, at most two focus panels, one motion and one prior")
    snapshots, sources = [], []
    for index, (role, path) in enumerate(images):
        snapshot, identity = image_inputs.snapshot_image(folder, path)
        snapshots.append(snapshot)
        sources.append({**identity, "role": role, "image_index": index,
                        "input_sha256": _digest(snapshot.url.encode())})
    return tuple(snapshots), tuple(sources)


class _StructureEvaluator:
    async def evaluate(self, candidate):
        return flynn.Evaluation(
            candidate, "vfx-critic-response-structure/v1", "closed verdict structure only",
            flynn.Verdict.SATISFIED, "model opinion; qualification and VFX acceptance are not established",
        )


async def execute(
    *, folder: Path, scope_id: str, phase: str, requested_provider: str, requested_model: str,
    prompt: str, axes: tuple[tuple[str, str], ...], frames: tuple[int, ...],
    images: tuple[tuple[str, str], ...], allow_na: bool,
    inference: flynn.InferenceAdapter, check_current: Callable[[], None],
) -> dict:
    """Record one bounded opinion; refusals/errors propagate without retry or fallback.

    Model observations require matching requested and provider-reported identity.
    Scripted observations remain explicitly not applicable. Neither is a
    qualification credential or proof of the provider's actual model weights.
    """
    check_current()
    if any(not isinstance(value, str) or not value.strip()
           for value in (scope_id, phase, requested_provider, requested_model, prompt)):
        raise ValueError("critic requires nonempty scope, phase, requested provider/model and prompt")
    names = [name for name, _description in axes]
    if (not names or len(set(names)) != len(names)
            or any(not isinstance(name, str) or not name.strip() for name in names)):
        raise ValueError("critic requires nonempty distinct declared axes")
    if not frames or len(set(frames)) != len(frames) or any(type(frame) is not int or frame < 1 for frame in frames):
        raise ValueError("critic requires distinct positive declared judge frames")
    if type(allow_na) is not bool:
        raise ValueError("critic allow_na must be a boolean derived by the owning scope")
    layout = run_artifacts.active(folder)
    if layout is None:
        raise ValueError("native critic requires an active owning VFX run")
    snapshots, sources = _snapshot_images(folder, images)
    packet = flynn.ContextCompiler(max_characters=MAX_CONTEXT_CHARACTERS).compile((
        flynn.ContextItem("critic-prompt", prompt, required=True),
        flynn.ContextItem("image-order", json.dumps(sources, sort_keys=True), required=True),
    ))
    schema = critic_verdict_schema(list(axes), allow_na=allow_na, focus_frames=list(frames))
    validator = Draft202012Validator(schema)

    def validate(arguments):
        error = next(validator.iter_errors(arguments), None)
        if error is not None:
            raise ValueError(f"critic verdict {error.json_path}: {error.message}")

    invocation = f"critic-{uuid4().hex}"
    database = layout.checkpoints / "flynn" / f"{invocation}.sqlite"
    inputs = {
        "schema": "vfx-harness.critic-inputs/v1", "scope_id": scope_id, "phase": phase,
        "requested_provider": requested_provider, "requested_model": requested_model,
        "image_shape": IMAGE_SHAPE, "sources": sources,
        "prompt_sha256": _digest(prompt.encode()), "context_sha256": _digest(packet.text.encode()),
        "response_schema_sha256": _digest(json.dumps(schema, sort_keys=True).encode()),
        "axes": axes, "frames": frames, "allow_na": allow_na,
    }

    def current():
        check_current()
        active = run_artifacts.active(folder)
        if active is None or active.root != layout.root:
            raise ValueError("native critic owning run changed; observation cannot be consumed")
        for source in sources:
            payload = read_real_file(folder, folder / source["path"], "critic image source")
            if _digest(payload) != source["sha256"]:
                raise ValueError(f"critic {source['role']} bytes changed; prepare a new authoritative observation")

    def prepare(request):
        current()
        return replace(request, images=snapshots)

    model_identity = {"status": "unverified"}

    async def guard(request):
        current()
        rows = [row for row in run.records()["inference_usage"]
                if row["operation_id"] == request.operation_id]
        if len(rows) != 1:
            raise ValueError(
                "critic requires one durable inference identity; preserve this journal and repair the adapter"
            )
        usage = json.loads(rows[0]["payload"])
        model_identity.update(operation_id=request.operation_id, kind=usage["kind"],
                              provider=usage["provider"], requested_model=usage["model"],
                              response_model=usage.get("response_model"))
        if usage["kind"] == "scripted":
            model_identity["status"] = "not_applicable"
        else:
            expected = (requested_provider, requested_model, requested_model)
            observed = (usage["provider"], usage["model"], usage.get("response_model"))
            if observed != expected:
                raise ValueError(
                    f"critic provider/request/response identity mismatch: expected {expected!r}, got {observed!r}; "
                    "preserve this observation attempt and qualify the intended model before retrying"
                )
            model_identity["status"] = "matched"
        return flynn.GuardDecision(True, "current critic inputs and recorded inference identity checked")

    async def submit(arguments):
        current()
        return flynn.ToolResult(data_json=json.dumps(arguments, sort_keys=True))

    tool = flynn.Tool.structured(
        "submit_verdict", description="Submit a structured visual opinion; this does not accept VFX work.",
        parameters_json=json.dumps(schema), validate=validate, execute=submit,
    )
    current()
    durably_ensure_real_directory(folder, database.parent.relative_to(folder))
    with flynn.SQLiteRun.create(
        database, run_id=invocation, initial_state=json.dumps(inputs, sort_keys=True), limits=LIMITS,
    ) as run:
        session = flynn.Session(
            inference=inference, tools=flynn.ToolBroker([tool]), evaluator=_StructureEvaluator(), run=run,
            grants=("submit_verdict",), prepare_request=prepare,
            guards=(flynn.DispatchGuard("current-vfx-critic-inputs", guard),),
            policy=lambda view: (
                flynn.SessionStop("structured critic observation recorded; no qualification or acceptance")
                if view.completed_steps else flynn.SessionStep(packet.text, ("submit_verdict",))
            ),
        )
        validated = False
        try:
            await session.execute()
            current()
            result = flynn.ToolResult.from_json(run.latest_observation())
            verdict = json.loads(result.data_json)
            validate(verdict)
            validated = True
        finally:
            report = layout.write_report(invocation, {
                "schema": "vfx-harness.critic-observation/v1", "inputs": inputs,
                "journal": database.relative_to(layout.root).as_posix(),
                "usage": run.usage_summary(), "output_budget": asdict(run.output_budget()),
                "termination": run.outcome(), "inputs_validated_after_inference": validated,
                "model_identity": model_identity,
                "acceptance_authorized": False, "qualification_verified": False, "pricing_status": "unpriced",
            })
        current()
        return {"verdict": verdict, "report": report.relative_to(folder).as_posix(),
                "report_sha256": _digest(read_real_file(folder, report, "critic observation report")),
                "acceptance_authorized": False, "qualification_verified": False}
