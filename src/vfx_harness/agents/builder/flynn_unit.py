"""Executable-only Flynn unit engine, using the existing VFX publishers.

The production dispatcher supplies the provider and bounded limits. Development
callers may bind those explicitly through build_layer's unit_builder injection.
This engine cannot resume an interrupted attempt or certify layer completion.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from dataclasses import asdict, replace
from functools import partial

import flynn_agents_sdk as flynn

from vfx_harness.agents import unit_scope
from vfx_harness.agents.builder import (
    candidate_script,
    evidence,
    flynn_construction,
    flynn_image_capture,
    prior,
    revalidate,
    unit_evaluation,
    unit_runtime,
    verdicts,
    verify,
)
from vfx_harness.agents.builder.attempt_guard import AttemptBoundBlenderSession, UnitAttemptGuard
from vfx_harness.agents.builder.models import _RESET, BuildUnpassed
from vfx_harness.domain.image_debts import image_contract_debt_cards, unpaid_image_contract_debts
from vfx_harness.domain.semantic_roles import match_semantic
from vfx_harness.evidence import checks, image_check_operation
from vfx_harness.observability import run_artifacts
from vfx_harness.orchestration import unit_completion_state, unit_state
from vfx_harness.orchestration.authority_selection_transaction import durably_ensure_real_directory
from vfx_harness.orchestration.builder_execution_fence import require_builder_execution_lease
from vfx_harness.orchestration.plan_bundle_integrity import read_real_file


@contextmanager
def _usage_report(run, layout, claim_id):
    """Publish an audit projection; SQLite remains the source after interruption."""
    try:
        yield
    finally:
        layout.write_report(f"flynn-usage-{claim_id}", {
            "schema": "vfx-harness.flynn-usage/v2",
            "claim_id": claim_id,
            "journal": str(run.path.relative_to(layout.root)),
            "usage": run.usage_summary(),
            "output_budget": asdict(run.output_budget()),
        })


def _arguments(arguments: str, field: str | None = None) -> dict:
    value = json.loads(arguments)
    expected = set() if field is None else {field}
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(f"expected exactly these argument fields: {sorted(expected)}")
    if field is not None and (not isinstance(value[field], str) or not value[field]):
        raise ValueError(f"{field} must be a nonempty string")
    return value


def _schema(field: str) -> str:
    return json.dumps(
        {
            "type": "object",
            "properties": {field: {"type": "string", "minLength": 1}},
            "required": [field],
            "additionalProperties": False,
        }
    )


def _validate_arguments(arguments: str, *, field: str | None = None) -> None:
    _arguments(arguments, field)


class _ObservationEvaluator:
    async def evaluate(self, candidate):
        result = json.loads(candidate.output)
        refused = False
        if result.get("schema", "").startswith("flynn.tool-result/"):
            refused = flynn.ToolResult.from_json(candidate.output).status == "refused"
        return flynn.Evaluation(
            candidate,
            "vfx-executable-operation/v1",
            "operation observation only",
            flynn.Verdict.FAILED if refused or result.get("canonical") == "failed" else flynn.Verdict.SATISFIED,
            "VFX receipt readers, not this assessment, establish unit acceptance",
        )


def _selected_feedback(observation: str | None) -> tuple[str | None, tuple[flynn.ImageInput, ...]]:
    """Select measured feedback; retain the full result in SQLite under its digest."""
    if observation is None:
        return None, ()
    result = json.loads(observation)
    images = []
    if result.get("schema", "").startswith("flynn.tool-result/"):
        structured = flynn.ToolResult.from_json(observation)
        content = []
        for block in structured.content:
            if isinstance(block, flynn.ImageContent):
                images.append(flynn.ImageInput(block.url, block.detail))
                content.append({"type": "image", "image_index": len(images) - 1})
            else:
                content.append({"type": "text", "text": block.text})
        result = {
            "status": structured.status,
            "content": content,
            "data": json.loads(structured.data_json) if structured.data_json is not None else None,
        }
    verdicts = result.pop("verdicts", None)
    if verdicts is not None:
        result["points"] = [
            {
                "frame": point[0],
                "pass": verdict["pass"],
                "issues": verdict.get("issues", []),
                "evidence": [
                    {key: row[key] for key in ("id", "pass", "value", "reason") if key in row}
                    for row in verdict.get("evidence", [])
                ],
                "missing_evidence": verdict.get("missing_evidence", []),
            }
            for point, verdict in verdicts
        ]
    result["observation_sha256"] = hashlib.sha256(observation.encode()).hexdigest()
    return json.dumps(result, sort_keys=True), tuple(images)


def _prepare_feedback(request: flynn.InferenceRequest) -> flynn.InferenceRequest:
    """Replace the previous observation's images; selection never accumulates history."""
    feedback, images = _selected_feedback(request.observation)
    return replace(request, observation=feedback, images=images)


def native_execution_refusal(unit) -> str | None:
    """Shared domain eligibility for routing and explicit native execution.

    This governs one unit, not the composed layer's independent look judgment.
    """
    for point in unit.evaluation.judges:
        required = verdicts._required_claims_at(unit, point.frame)
        if not required or any(claim.authority != "executable_required" for claim in required):
            return "Flynn requires executable claims covering every judge frame; visual judgment is unsupported"
    if getattr(unit, "provisional_requirement_ids", ()):
        return "Flynn executable unit cannot decide provisional visual requirements"
    if unit.construction.route not in {"procedural", "generate"}:
        return "Flynn executable unit requires procedural or generated construction"
    return None


def _model_grants(*, inspected: bool, written: bool, observed: str | None, remaining: dict,
                  image_tools=False, captures=0, payments=0, can_pay=False, unpaid=False,
                  write_captures=None) -> tuple[str, ...]:
    """Grant only phases that leave room for observation, freeze and cold replay."""
    def fits(inference, external):
        return (
            remaining["inference"] >= inference
            and remaining["tool"] >= inference
            and remaining["external"] >= external
        )

    grants = ["abstain"]
    extra = captures + payments
    write_extra = (captures if write_captures is None else write_captures) + payments
    if not inspected and not written and fits(5 + extra, 4 + extra):
        grants.append("inspect_unit")
    if (not written or observed is not None) and fits(4 + write_extra, 3 + write_extra):
        grants.append("write_candidate")
    if written and observed is None and fits(3, 2):
        grants.append("probe_candidate")
    if image_tools and written:
        if fits(3 + max(1, captures) + payments, 2 + max(1, captures) + payments):
            grants.append("capture_unit_frame")
        if can_pay and fits(3 + captures + max(1, payments), 2 + captures + max(1, payments)):
            grants.append("propose_checks")
    if observed is not None and not unpaid and not captures and fits(2, 1):
        grants.append("freeze_candidate")
    return tuple(grants)


async def build_unit(shot, m, script_rel, prior_paths, session, *, fence_lease=None, **kwargs):
    """Retain the live lease through native tools and the completion transaction."""
    require_builder_execution_lease(fence_lease, shot.folder)
    with fence_lease.operation(shot.folder):
        return await _build_unit(shot, m, script_rel, prior_paths, session,
                                 fence_lease=fence_lease, **kwargs)


async def _build_unit(
    shot,
    m,
    script_rel,
    prior_paths,
    session,
    *,
    fence_lease,
    inference: flynn.InferenceAdapter,
    limits: flynn.RunLimits,
    max_context_characters: int = 12000,
    rounds=2,
    verbose=True,
    plan_excerpt="",
    scope=None,
    layer=None,
    active_unit=None,
    report_layer=None,
    resume_ok=False,
    layer_units=None,
    selected_authority=None,
    attempt_guard: UnitAttemptGuard | None = None,
):
    """Propose, observe, freeze and independently replay one exact claimed unit.

    ``limits`` includes one reserved scripted invocation for canonical replay. There is
    no model-visible accept/finish tool. A returned ledger is consumed by the layer's
    existing checkpoint/completion transaction. SQLite remains an execution record.
    """
    if resume_ok:
        raise ValueError("Flynn unit resume requires a complete VFX resume receipt; start a reviewed new attempt")
    if active_unit is None or attempt_guard is None or selected_authority is None or layer is None:
        raise ValueError("Flynn requires an exact unit, layer, selected authority and attempt guard")
    attempt_guard.require_unit_boundary(m, active_unit, layer=layer, script_rel=script_rel)
    attempt_guard.check("start Flynn executable unit")
    if selected_authority != attempt_guard.selected_authority:
        raise ValueError("Flynn selected authority must be the exact attempt's snapshot")
    raster = evidence._unit_requires_raster(shot, active_unit, selected_authority=selected_authority)
    refusal = native_execution_refusal(active_unit)
    if refusal is not None:
        raise ValueError(refusal)
    completion_authorization = unit_completion_state.authorize_completed_units_for_layer(
        shot.folder, str(layer.id), attempt_guard.units,
        expected_plan_hash=attempt_guard.expected_plan_hash, selected_authority=selected_authority,
    )
    card = unit_scope.compile_unit_scope_for_shot(
        shot,
        active_unit,
        str(layer.id),
        units=attempt_guard.units,
        durable_state=unit_state.load(shot.folder, str(layer.id)),
        completion_authorization=completion_authorization,
        selected_authority=selected_authority,
    )
    # Authority and selected context must fit before any ledger write or reservation.
    packet = flynn.ContextCompiler(max_characters=max_context_characters).compile(
        (
            flynn.ContextItem("unit-scope", json.dumps(card, sort_keys=True), required=True),
            flynn.ContextItem("unit-plan", plan_excerpt, required=True),
        )
    )
    layout = run_artifacts.ensure(shot.folder, command="build")
    if layout.run_id != attempt_guard.claim.run_id:
        raise ValueError("Flynn execution journal must belong to the exact attempt run")
    database = layout.checkpoints / "flynn" / f"{attempt_guard.claim.claim_id}.sqlite"
    durably_ensure_real_directory(shot.folder, database.parent.relative_to(shot.folder))
    candidate = candidate_script.exact_candidate_script_path(shot.folder, attempt_guard)
    candidate_rel = candidate.relative_to(shot.folder).as_posix()
    session = AttemptBoundBlenderSession(session, attempt_guard)
    frozen = None
    observed = None
    observed_payments = None
    written = False
    inspected = False
    owned_candidate = None
    canonical_verdicts = []
    replay_inputs = []
    construction = (
        flynn_construction.UnitConstruction(shot, attempt_guard)
        if active_unit.construction.route == "generate" else None
    )

    def candidate_digest():
        return hashlib.sha256(read_real_file(shot.folder, candidate, "Flynn candidate")).hexdigest()

    def check_current():
        attempt_guard.check("check Flynn unit dispatch authority")
        if construction is not None:
            construction.check()
        if owned_candidate is None:
            if candidate.exists() or candidate.is_symlink():
                raise ValueError("Flynn candidate exists outside this attempt's writes; preserve it and stop")
        elif candidate_digest() != owned_candidate:
            raise ValueError("Flynn candidate changed outside this attempt; preserve the newer bytes and stop")

    debts = image_contract_debt_cards(active_unit)
    payment_batch_size = image_check_operation.SCHEMA["properties"]["checks"]["maxItems"]

    def payment_state():
        rows = checks.load_image_contract_payment_rows(
            shot.folder, selected_authority=selected_authority,
        ) if debts else []
        return unpaid_image_contract_debts(debts, rows), hashlib.sha256(
            json.dumps(rows, sort_keys=True).encode()
        ).hexdigest()

    def require_freeze_evidence():
        unpaid, fingerprint = payment_state()
        if unpaid:
            raise ValueError("freeze requires paid image-contract debts: " + ", ".join(
                f"{debt.id}@{debt.frame}" for debt in unpaid
            ))
        if fingerprint != observed_payments:
            raise ValueError("image payments changed after observation; probe the current evidence before freeze")
        if images is not None:
            images.require_current_images()
            for record in images.state["image_artifacts"].values():
                images.reopen(record)

    async def guard_dispatch(context):
        check_current()
        if context.call.name in {"freeze_candidate", "canonical_replay"}:
            require_freeze_evidence()
        return flynn.GuardDecision(True, "exact VFX unit claim and owned candidate remain current")

    images = flynn_image_capture.UnitImageCapture(
        shot=shot, attempt_guard=attempt_guard, fence_lease=fence_lease, session=session,
        prior_paths=prior_paths, check_candidate=check_current,
    ) if raster else None
    guards = (flynn.DispatchGuard("current-vfx-builder-attempt", guard_dispatch),)
    if images is not None:
        guards += (images.guard,)

    def prepare(request):
        check_current()
        request = _prepare_feedback(request)
        feedback = request.observation
        phase = json.dumps({
            "initial_scene_inspected": inspected,
            "candidate_written": written,
            "observed_candidate_sha256": observed,
            "frozen_candidate_sha256": frozen,
            "unpaid_image_debts": [debt.as_dict() for debt in payment_state()[0]],
            "construction": construction.context() if construction is not None else None,
            "current_image_handles": [
                {key: record[key] for key in ("handle", "frame", "candidate_sha256", "sha256")}
                for record in images.state["image_artifacts"].values()
                if record["role"] == "live_candidate" and written
                and record["candidate_sha256"] == candidate_digest()
            ] if images is not None else [],
        }, sort_keys=True)
        objective = packet.text + "\n\n" + request.objective + "\n\nExecution phase: " + phase
        if written:
            source = read_real_file(shot.folder, candidate, "Flynn current candidate context")
            objective += "\n\nCurrent candidate: " + json.dumps({
                "source": source.decode("utf-8"), "sha256": hashlib.sha256(source).hexdigest(),
            }, sort_keys=True)
        flynn.ContextCompiler(max_characters=max_context_characters).compile(
            (
                flynn.ContextItem("objective", objective, required=True),
                flynn.ContextItem("selected-observation", feedback or "", required=True),
            )
        )
        return replace(request, objective=objective, observation=feedback)

    async def inspect(arguments):
        nonlocal inspected
        check_current()
        root = shot.folder.expanduser().absolute()
        prepared = prior._prepare_artifact_replay_inputs(root, [
            (path.expanduser().absolute().relative_to(root).as_posix(), path)
            for path in prior_paths
        ])
        session.run(_RESET)
        session.run(prior._preamble(shot))
        prior._run_prior_paths(session, prior_paths, prepared)
        session.run(f"bpy.context.scene.frame_set({int(m.frame)})", journal=False)
        session.run(prior._ARTIFACT_EVALUATION_BARRIER, journal=False)
        objects = revalidate._scene_object_manifest(session)
        for item in prepared:
            prior.require_prepared_artifact_replay_input_unchanged(item)
        check_current()
        own_objects = {name: role for name, role in objects.items()
                       if match_semantic(role, active_unit.mutates.roles)}
        predecessor_roles = tuple(role for row in card["predecessor_interfaces"]
                                  for role in row["semantic_roles"])
        predecessor_objects = {name: role for name, role in objects.items()
                               if match_semantic(role, predecessor_roles)}
        inputs = [{"script_path": item.executed.script_path, "sha256": item.executed.script_sha256,
                   "dependencies": [{"kind": dep.kind, "path": dep.path, "sha256": dep.sha256}
                                    for dep in item.executed.dependencies]} for item in prepared]
        record = {
            "schema": "vfx-harness.unit-inspection/v1", "claim": attempt_guard.claim.as_dict(),
            "frame": int(m.frame), "replay_inputs": inputs, "objects": own_objects,
            "predecessor_objects": predecessor_objects, "acceptance_authorized": False,
        }
        report = attempt_guard.publish("record Flynn unit inspection", lambda: layout.write_report(
            f"flynn-unit-inspection-{attempt_guard.claim.claim_id}", record,
        ))
        for item in prepared:
            prior.require_prepared_artifact_replay_input_unchanged(item)
        check_current()
        inspected = True
        return json.dumps({
            "frame": int(m.frame), "objects": own_objects, "predecessor_objects": predecessor_objects,
            "prior_count": len(prepared), "report": str(report.relative_to(layout.root)),
            "report_sha256": hashlib.sha256(report.read_bytes()).hexdigest(),
            "acceptance_authorized": False,
        }, sort_keys=True)

    async def write(arguments):
        nonlocal observed, written, owned_candidate
        check_current()
        if frozen is not None:
            raise ValueError("frozen Flynn candidate cannot be edited")
        source = _arguments(arguments, "source")["source"]
        expected = hashlib.sha256(source.encode("utf-8")).hexdigest()
        candidate_script.write_scratch_candidate(
            shot.folder,
            candidate,
            source,
            attempt_guard,
        )
        owned_candidate = expected
        check_current()
        observed = None
        written = True
        return json.dumps({"candidate_sha256": expected})

    async def replay(arguments):
        nonlocal observed, observed_payments
        check_current()
        before = candidate_digest()
        payments_before = payment_state()[1]
        if frozen is not None and before != frozen:
            raise ValueError("frozen Flynn candidate bytes changed; no replay or publication authorized")
        canonical_verdicts.clear()
        replay_inputs.clear()
        replay_errors = []

        def record_replay_failure(inputs, stage, message, context):
            replay_errors.append({"stage": stage, "message": message})

        result = await verify._verify_script(
            shot,
            m,
            candidate_rel,
            prior_paths,
            session,
            [],
            ledger,
            verbose,
            scope=scope,
            layer=layer,
            active_unit=active_unit,
            out_verdicts=canonical_verdicts,
            out_replay_inputs=replay_inputs,
            on_replay_failed=record_replay_failure,
            authority_script_rel=script_rel,
            selected_authority=selected_authority,
            execution_guard=attempt_guard,
        )
        check_current()
        if payment_state()[1] != payments_before:
            raise ValueError("image payments changed during replay; no current observation can be recorded")
        observed = before
        observed_payments = payments_before
        return json.dumps(
            {"candidate_sha256": before, "canonical": result, "verdicts": canonical_verdicts,
             "replay_errors": replay_errors}, sort_keys=True
        )

    async def freeze(arguments):
        nonlocal frozen
        check_current()
        requested = _arguments(arguments, "sha256")["sha256"]
        if requested != observed or requested != candidate_digest():
            raise ValueError("freeze requires the exact observed candidate digest; probe the current candidate")
        require_freeze_evidence()
        frozen = requested
        return json.dumps({"frozen_sha256": frozen})

    async def abstain(arguments):
        attempt_guard.check("record Flynn unit abstention")
        return json.dumps({"abstention": _arguments(arguments, "reason")["reason"]})

    async def image_operation(arguments, *, tool):
        nonlocal observed, observed_payments
        if frozen is not None:
            raise ValueError("frozen image evidence cannot be changed")
        observed = observed_payments = None
        return await tool.execute(arguments)

    image_tools = [replace(tool, execute=partial(image_operation, tool=tool))
                   for tool in images.tools] if images is not None else []

    tools = flynn.ToolBroker(
        [
            *image_tools,
            flynn.Tool(
                "inspect_unit", _validate_arguments, inspect, observation=True, external_action=True,
                description=(
                    'Reset to an empty scene, replay the exact accepted prefix, and inspect owned '
                    'and read-only predecessor objects at the unit frame. Once, before writing; '
                    'never executes a candidate or grants acceptance.'
                ),
            ),
            flynn.Tool(
                "write_candidate",
                partial(_validate_arguments, field="source"),
                write,
                observation=True,
                external_action=True,
                parameters_json=_schema("source"),
                description=(
                    "Write the complete self-contained Blender Python candidate to this unit's "
                    'scratch file. Does not execute it; probe next.'
                ),
            ),
            flynn.Tool(
                "probe_candidate", _validate_arguments, replay, observation=True, external_action=True,
                description=(
                    'Cold-replay accepted priors and the written candidate, returning measured '
                    'verdicts and the candidate SHA-256.'
                ),
            ),
            flynn.Tool(
                "freeze_candidate",
                partial(_validate_arguments, field="sha256"),
                freeze,
                observation=True,
                parameters_json=_schema("sha256"),
                description=(
                    'Freeze the exact probed SHA-256 for independent canonical replay. This '
                    'does not accept the unit.'
                ),
            ),
            flynn.Tool("canonical_replay", _validate_arguments, replay, observation=True, external_action=True),
            flynn.Tool(
                "abstain",
                partial(_validate_arguments, field="reason"),
                abstain,
                observation=True,
                parameters_json=_schema("reason"),
                description="Stop with insufficient evidence, without accepting or declaring a plan defect.",
            ),
        ]
    )
    with flynn.SQLiteRun.create(
        database,
        run_id=attempt_guard.claim.claim_id,
        initial_state=json.dumps(attempt_guard.claim.as_dict(), sort_keys=True),
        limits=limits,
    ) as run, _usage_report(run, layout, attempt_guard.claim.claim_id):
        check_current()
        ledger = unit_runtime.start_unit_runtime(
            shot,
            m,
            script_rel,
            active_unit,
            selected_authority,
            attempt_guard,
        ).ledger
        if construction is not None:
            remaining = run.remaining()
            unpaid, _ = payment_state()
            image_operations = len({debt.frame for debt in unpaid}) + (
                len(unpaid) + payment_batch_size - 1
            ) // payment_batch_size
            if any(remaining[kind] < minimum + image_operations for kind, minimum in (
                ("inference", 5), ("tool", 5), ("external", 4),
            )):
                raise flynn.BudgetExhausted(
                    "generated construction requires capacity for preparation, candidate write, "
                    "required image captures/payments, probe, freeze and independent replay"
                )
            preparation = flynn.Runtime(
                inference=flynn.ScriptedAdapter([flynn.ToolCall("prepare_construction", "{}")]),
                tools=flynn.ToolBroker([construction.tool]), evaluator=_ObservationEvaluator(),
                run=run, grants=("prepare_construction",), guards=guards,
            )
            await preparation.step("Prepare the exact construction route selected by the unit plan.")
            check_current()
        runtime = flynn.Runtime(
            inference=inference,
            tools=tools,
            evaluator=_ObservationEvaluator(),
            run=run,
            grants=("inspect_unit", "write_candidate", "probe_candidate", "freeze_candidate", "abstain",
                    *(tool.name for tool in image_tools)),
            prepare_request=prepare,
            guards=guards,
        )
        while frozen is None:
            # Leave one deterministic invocation and external dispatch for cold replay.
            remaining = run.remaining()
            unpaid, fingerprint = payment_state()
            if observed is not None and fingerprint != observed_payments:
                observed = None
            current_images = [record for record in images.state["image_artifacts"].values()
                              if record["role"] == "live_candidate" and written
                              and record["candidate_sha256"] == candidate_digest()] if images is not None else []
            captured_frames = {record["frame"] for record in images.state["image_artifacts"].values()
                               if record["role"] == "live_candidate"} if images is not None else set()
            needed_frames = {debt.frame for debt in unpaid} | captured_frames
            missing_frames = needed_frames - {record["frame"] for record in current_images}
            grants = _model_grants(
                inspected=inspected, written=written, observed=observed, remaining=remaining,
                image_tools=images is not None, captures=len(missing_frames),
                payments=(len(unpaid) + payment_batch_size - 1) // payment_batch_size,
                can_pay=bool(current_images), unpaid=bool(unpaid),
                write_captures=len(needed_frames),
            )
            step = await runtime.step(
                "Inspect the unit, write and probe its candidate, then freeze the observed digest."
                + (" Capture declared frames and pay required image debts with propose_checks. "
                   "Probe again after captures or payment changes before freezing." if raster else ""),
                grants=tuple(grants),
            )
            if step.candidate.call.name == "abstain":
                reason = json.loads(step.candidate.output)["abstention"]
                run.finish("unit_abstained")
                raise BuildUnpassed(f"unit {m.id} has insufficient evidence: {reason}")
        canonical_runtime = flynn.Runtime(
            inference=flynn.ScriptedAdapter([flynn.ToolCall("canonical_replay", "{}")]),
            tools=tools,
            evaluator=_ObservationEvaluator(),
            run=run,
            grants=("canonical_replay",),
            prepare_request=prepare,
            guards=guards,
        )
        result = await canonical_runtime.step(
            "Independently replay the frozen unit candidate from the accepted prefix."
        )
        canonical = json.loads(result.candidate.output)["canonical"]
        check_current()
        require_freeze_evidence()
        if candidate_digest() != frozen:
            raise ValueError("Flynn frozen candidate changed before publication")
        unit_runtime.publish_candidate_script(
            shot.folder,
            candidate,
            script_rel,
            attempt_guard,
            expected_sha256=frozen,
        )
        if canonical != "passed":
            # Match the existing failure writer: the ledger must name the script
            # actually evaluated, not inherit a previous attempt's artifact hash.
            # Publishing failed script bytes supplies no completion receipt.
            ledger.mark(m, canonical)
            run.finish("unit_evaluation_failed")
            return ledger
        # The existing replay verifier owns the canonical render, not the diagnostic capture.
        primary = next(verdict for point, verdict in canonical_verdicts if point[0] == m.frame) if raster else None
        ledger.mark(m, "passed", best={"round": 0, "mean": primary["mean"] if primary else 0.0,
                                      "render": primary["render"] if primary else script_rel})
        receipt = unit_evaluation.publish_unit_evaluation_outcome(
            shot.folder,
            str(layer.id),
            active_unit,
            attempt_guard,
            result=canonical,
            canonical_verdicts=canonical_verdicts,
            ledger_slot=ledger._slot(m),
            replay_inputs=replay_inputs,
            candidate_path=primary["render"] if primary else script_rel,
        )
        ledger.snapshot_scripts(m)
        run.finish("unit_evaluation_receipt:" + receipt.receipt.receipt_digest)
        return ledger
