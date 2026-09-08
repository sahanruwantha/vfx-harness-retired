"""Native Flynn observation transport for one VFX critic decision.

This records a structured opinion and can admit explicitly selected artifact-backed
qualification. The owning VFX caller derives those claims and current authority checks;
domain reconciliation and acceptance remain separate. No legacy transport is imported.
"""

from __future__ import annotations

import hashlib
import io
import json
from collections.abc import Callable
from dataclasses import asdict, replace
from pathlib import Path
from uuid import uuid4

import flynn_agents_sdk as flynn
from jsonschema import Draft202012Validator
from PIL import Image

from vfx_harness.agents import critic_images, critic_qualification, image_inputs
from vfx_harness.domain.critic_prompt import CriticPrompt, protocol_schema_digest
from vfx_harness.domain.critic_verdict import critic_verdict_schema
from vfx_harness.domain.work_units.claims import Claim
from vfx_harness.observability import run_artifacts
from vfx_harness.orchestration.authority_selection_transaction import durably_ensure_real_directory
from vfx_harness.orchestration.plan_bundle_integrity import read_real_file

MAX_CONTEXT_CHARACTERS = 24000
LIMITS = flynn.RunLimits(1, 1, 0, 180, output_tokens=8192)
IMAGE_SHAPE = "vfx-harness.critic-images/v2"
EMPTY_STATE = "Observation-only critic; no accepted VFX state."
TOOL_DESCRIPTION = "Submit a structured visual opinion; this does not accept VFX work."


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _snapshot_images(folder: Path, images: tuple[tuple[str, str], ...]):
    """Closed image slots preserve identity/order without an accumulated history."""
    snapshots, sources = [], []
    for index, slot in enumerate(critic_images.compile_images(images)):
        payload = read_real_file(folder, folder / slot.path, "critic image source")
        snapshot, identity = image_inputs.snapshot_image_payload(payload, slot.path)
        with Image.open(io.BytesIO(payload)) as source_image:
            shape = {"size": list(source_image.size), "mode": source_image.mode,
                     "frame_count": getattr(source_image, "n_frames", 1)}
        snapshots.append(snapshot)
        sources.append({**identity, **shape, "role": slot.role, "label": slot.label, "image_index": index,
                        "input_sha256": _digest(snapshot.url.encode())})
    return tuple(snapshots), tuple(sources)


class _StructureEvaluator:
    async def evaluate(self, candidate):
        return flynn.Evaluation(
            candidate, "vfx-critic-response-structure/v1", "closed verdict structure only",
            flynn.Verdict.SATISFIED, "structure evaluation grants no qualification or VFX acceptance",
        )


async def execute(
    *, folder: Path, scope_id: str, phase: str, requested_provider: str, requested_model: str,
    prompt: CriticPrompt, axes: tuple[tuple[str, str], ...], frames: tuple[int, ...],
    images: tuple[tuple[str, str], ...], allow_na: bool,
    inference: flynn.InferenceAdapter, check_current: Callable[[], None],
    qualification_claims: tuple[Claim, ...] = (),
    calibration_claims: tuple[Claim, ...] = (),
) -> dict:
    """Record one bounded opinion; refusals/errors propagate without retry or fallback.

    Model observations require matching requested and provider-reported identity.
    Scripted observations remain explicitly not applicable. Neither is a
    qualification credential or proof of the provider's actual model weights.
    Calibration supplies the same claim semantics without admitting an artifact;
    it records a trial, never a passed qualification or an accepted claim.
    """
    check_current()
    if not isinstance(prompt, CriticPrompt):
        raise ValueError("critic requires a typed rubric and observation record; rebuild the invocation")
    if qualification_claims and calibration_claims:
        raise ValueError("critic calibration and qualification admission are mutually exclusive")
    if any(not isinstance(value, str) or not value.strip()
           for value in (scope_id, phase, requested_provider, requested_model)):
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
    prompt.validate_images(images)
    snapshots, sources = _snapshot_images(folder, images)
    admission = critic_qualification.Admission(
        folder, qualification_claims,
        axes=tuple(names), frames=frames,
    ) if qualification_claims else None
    calibration_semantics = critic_qualification.selected_semantics(
        calibration_claims, axes=tuple(names), frames=frames,
    ) if calibration_claims else []
    calibration_snapshot = critic_qualification.digest([asdict(claim) for claim in calibration_claims])
    scope = {"scope_id": scope_id, "phase": phase, "axes": axes, "frames": frames, "allow_na": allow_na,
             "claims": admission.semantics if admission else calibration_semantics,
             "authority": json.loads(prompt.authority_json)}
    packet = flynn.ContextCompiler(max_characters=MAX_CONTEXT_CHARACTERS).compile((
        flynn.ContextItem("critic-rubric", prompt.rubric, required=True),
        flynn.ContextItem("critic-observation", prompt.observation_json, required=True),
        flynn.ContextItem("critic-scope", json.dumps(scope, sort_keys=True), required=True),
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
        "schema": "vfx-harness.critic-inputs/v2", "scope_id": scope_id, "phase": phase,
        "requested_provider": requested_provider, "requested_model": requested_model,
        "image_shape": IMAGE_SHAPE, "sources": sources,
        "prompt_sha256": _digest(prompt.rubric.encode()),
        "observation_sha256": _digest(prompt.observation_json.encode()),
        "context_schema_sha256": protocol_schema_digest(), "context_sha256": _digest(packet.text.encode()),
        "response_schema_sha256": _digest(json.dumps(schema, sort_keys=True).encode()),
        "axes": axes, "frames": frames, "allow_na": allow_na,
        "qualification_sources": admission.references if admission else [],
    }
    invocation_contract = {
        "schema": "vfx-harness.critic-invocation/v2", "scope": scope,
        "context_schema_sha256": inputs["context_schema_sha256"],
        "prompt_sha256": inputs["prompt_sha256"], "response_schema_sha256": inputs["response_schema_sha256"],
        "image_shape": IMAGE_SHAPE, "accepted_state": EMPTY_STATE,
        "tool": {"name": "submit_verdict", "description": TOOL_DESCRIPTION},
        "images": [{"role": source["role"], "label": source["label"], "detail": snapshot.detail,
                    "mime": snapshot.url.split(";", 1)[0], "size": source["size"], "mode": source["mode"],
                    "frame_count": source["frame_count"]}
                   for source, snapshot in zip(sources, snapshots, strict=True)],
    }
    qualification_check = {"status": "not_requested" if admission is None else "pending"}
    calibration_check = {"status": "pending" if calibration_claims else "not_requested"}
    configuration_check = qualification_check if admission else calibration_check
    requires_configuration = admission is not None or bool(calibration_claims)

    def current():
        check_current()
        active = run_artifacts.active(folder)
        if active is None or active.root != layout.root:
            raise ValueError("native critic owning run changed; observation cannot be consumed")
        if admission is not None:
            admission.check()
        if critic_qualification.digest([asdict(claim) for claim in calibration_claims]) != calibration_snapshot:
            raise ValueError("selected calibration claims changed; prepare a new calibration trial")
        for source in sources:
            payload = read_real_file(folder, folder / source["path"], "critic image source")
            if _digest(payload) != source["sha256"]:
                raise ValueError(f"critic {source['role']} bytes changed; prepare a new authoritative observation")

    def prepare(request):
        current()
        prepared = replace(request, images=snapshots, max_output_tokens=LIMITS.output_tokens)
        if requires_configuration:
            if not isinstance(inference, flynn.ConfiguredInference):
                raise ValueError(
                    "critic qualification/calibration requires a configuration-reporting model adapter; "
                    "scripted cannot qualify"
                )
            configuration = inference.configuration(prepared)
            if not isinstance(configuration, flynn.InferenceConfiguration):
                raise ValueError("critic adapter must return an InferenceConfiguration")
            if len(configuration.settings_json) > MAX_CONTEXT_CHARACTERS:
                raise ValueError(
                    "critic provider settings exceed bounded context; shorten and requalify the configuration"
                )
            if (configuration.provider, configuration.model) != (requested_provider, requested_model):
                raise ValueError("critic configured provider/model differs from selected qualification")
            profile = invocation_contract | {"configuration": asdict(configuration)}
            configuration_check.update(profile=profile, configuration_sha256=configuration.sha256,
                                       native_invocation_sha256=critic_qualification.digest(profile))
            if admission is not None:
                admission.admit(profile)
            configuration_check["status"] = "admitted_before_inference" if admission else "prepared_trial"
        return prepared

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
            if requires_configuration:
                raise ValueError("scripted critic observations cannot satisfy model qualification or calibration")
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
        if requires_configuration:
            if usage.get("configuration_sha256") != configuration_check["configuration_sha256"]:
                raise ValueError(
                    "critic dispatched configuration differs from qualification; preserve spending and requalify"
                )
            configuration_check["status"] = "matched_dispatched_configuration"
        return flynn.GuardDecision(True, "current critic inputs and recorded inference identity checked")

    async def submit(arguments):
        current()
        return flynn.ToolResult(data_json=json.dumps(arguments, sort_keys=True))

    tool = flynn.Tool.structured(
        "submit_verdict", description=TOOL_DESCRIPTION,
        parameters_json=json.dumps(schema), validate=validate, execute=submit,
    )
    current()
    durably_ensure_real_directory(folder, database.parent.relative_to(folder))
    with flynn.SQLiteRun.create(
        database, run_id=invocation, initial_state=EMPTY_STATE, limits=LIMITS,
    ) as run:
        session = flynn.Session(
            inference=inference, tools=flynn.ToolBroker([tool]), evaluator=_StructureEvaluator(), run=run,
            grants=("submit_verdict",), prepare_request=prepare,
            guards=(flynn.DispatchGuard("current-vfx-critic-inputs", guard),),
            policy=lambda view: (
                flynn.SessionStop("structured critic observation recorded; no VFX acceptance")
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
                "schema": "vfx-harness.critic-observation/v2", "inputs": inputs,
                "journal": database.relative_to(layout.root).as_posix(),
                "usage": run.usage_summary(), "output_budget": asdict(run.output_budget()),
                "termination": run.outcome(), "inputs_validated_after_inference": validated,
                "model_identity": model_identity,
                "qualification_check": qualification_check,
                "calibration_check": calibration_check,
                "acceptance_authorized": False, "qualification_verified": bool(admission) and validated,
                "pricing_status": "unpriced",
            })
        current()
        return {"verdict": verdict, "report": report.relative_to(folder).as_posix(),
                "report_sha256": _digest(read_real_file(folder, report, "critic observation report")),
                "acceptance_authorized": False, "qualification_verified": bool(admission) and validated,
                "qualified_claim_ids": [claim.id for claim in qualification_claims]}
