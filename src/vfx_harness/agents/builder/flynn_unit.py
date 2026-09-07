"""Opt-in executable-only Flynn unit engine, using the existing VFX publishers.

Import this module only with the private ``flynn`` extra installed. Bind ``inference``
and ``limits`` with functools.partial and pass it as build_layer's unit_builder.
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
    prior,
    revalidate,
    unit_evaluation,
    unit_runtime,
    verify,
)
from vfx_harness.agents.builder.attempt_guard import AttemptBoundBlenderSession, UnitAttemptGuard
from vfx_harness.agents.builder.models import BuildUnpassed
from vfx_harness.domain.semantic_roles import match_semantic
from vfx_harness.observability import run_artifacts
from vfx_harness.orchestration import unit_completion_state, unit_state
from vfx_harness.orchestration.authority_selection_transaction import durably_ensure_real_directory
from vfx_harness.orchestration.builder_execution_fence import builder_execution_fenced
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
        return flynn.Evaluation(
            candidate,
            "vfx-executable-operation/v1",
            "operation observation only",
            flynn.Verdict.FAILED if result.get("canonical") == "failed" else flynn.Verdict.SATISFIED,
            "VFX receipt readers, not this assessment, establish unit acceptance",
        )


def _selected_feedback(observation: str | None) -> str | None:
    """Select measured feedback; retain the full result in SQLite under its digest."""
    if observation is None:
        return None
    result = json.loads(observation)
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
    return json.dumps(result, sort_keys=True)


def _model_grants(*, inspected: bool, written: bool, observed: str | None, remaining: dict) -> tuple[str, ...]:
    """Grant only phases that leave room for observation, freeze and cold replay."""
    def fits(inference, external):
        return (
            remaining["inference"] >= inference
            and remaining["tool"] >= inference
            and remaining["external"] >= external
        )

    grants = ["abstain"]
    if not inspected and not written and fits(5, 4):
        grants.append("inspect_unit")
    if (not written or observed is not None) and fits(4, 3):
        grants.append("write_candidate")
    if written and observed is None and fits(3, 2):
        grants.append("probe_candidate")
    if observed is not None and fits(2, 1):
        grants.append("freeze_candidate")
    return tuple(grants)


@builder_execution_fenced
async def build_unit(
    shot,
    m,
    script_rel,
    prior_paths,
    session,
    *,
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
    if evidence._unit_requires_raster(shot, active_unit, selected_authority=selected_authority):
        raise ValueError("Flynn executable unit cannot pay raster or visual judgment debts")
    if active_unit.construction.route != "procedural":
        raise ValueError("Flynn executable unit currently requires procedural construction")
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
    written = False
    inspected = False
    canonical_verdicts = []
    replay_inputs = []

    def candidate_digest():
        return hashlib.sha256(read_real_file(shot.folder, candidate, "Flynn candidate")).hexdigest()

    def prepare(request):
        attempt_guard.check("prepare Flynn request")
        feedback = _selected_feedback(request.observation)
        phase = json.dumps({
            "initial_scene_inspected": inspected,
            "candidate_written": written,
            "observed_candidate_sha256": observed,
            "frozen_candidate_sha256": frozen,
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
        attempt_guard.check("inspect Flynn unit")
        session.run(prior._ARTIFACT_EVALUATION_BARRIER, journal=False)
        objects = revalidate._scene_object_manifest(session)
        inspected = True
        return json.dumps(
            {
                "objects": {
                    name: role for name, role in objects.items() if match_semantic(role, active_unit.mutates.roles)
                }
            },
            sort_keys=True,
        )

    async def write(arguments):
        nonlocal observed, written
        if frozen is not None:
            raise ValueError("frozen Flynn candidate cannot be edited")
        candidate_script.write_scratch_candidate(
            shot.folder,
            candidate,
            _arguments(arguments, "source")["source"],
            attempt_guard,
        )
        observed = None
        written = True
        return json.dumps({"candidate_sha256": candidate_digest()})

    async def replay(arguments):
        nonlocal observed
        attempt_guard.check("start Flynn candidate replay")
        before = candidate_digest()
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
        if candidate_digest() != before:
            raise ValueError("Flynn candidate changed during replay; freeze requires a fresh observation")
        observed = before
        return json.dumps(
            {"candidate_sha256": before, "canonical": result, "verdicts": canonical_verdicts,
             "replay_errors": replay_errors}, sort_keys=True
        )

    async def freeze(arguments):
        nonlocal frozen
        attempt_guard.check("freeze Flynn candidate")
        requested = _arguments(arguments, "sha256")["sha256"]
        if requested != observed or requested != candidate_digest():
            raise ValueError("freeze requires the exact observed candidate digest; probe the current candidate")
        frozen = requested
        return json.dumps({"frozen_sha256": frozen})

    async def abstain(arguments):
        attempt_guard.check("record Flynn unit abstention")
        return json.dumps({"abstention": _arguments(arguments, "reason")["reason"]})

    tools = flynn.ToolBroker(
        [
            flynn.Tool(
                "inspect_unit", _validate_arguments, inspect, observation=True, external_action=True,
                description=(
                    'Read initial scoped scene objects once, before writing a candidate. This '
                    'does not execute a candidate.'
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
        ledger = unit_runtime.start_unit_runtime(
            shot,
            m,
            script_rel,
            active_unit,
            selected_authority,
            attempt_guard,
        ).ledger
        runtime = flynn.Runtime(
            inference=inference,
            tools=tools,
            evaluator=_ObservationEvaluator(),
            run=run,
            grants=("inspect_unit", "write_candidate", "probe_candidate", "freeze_candidate", "abstain"),
            prepare_request=prepare,
        )
        while frozen is None:
            # Leave one deterministic invocation and external dispatch for cold replay.
            remaining = run.remaining()
            grants = _model_grants(
                inspected=inspected, written=written, observed=observed, remaining=remaining,
            )
            step = await runtime.step(
                "Inspect the unit, write and probe its candidate, then freeze the observed digest.",
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
        )
        result = await canonical_runtime.step(
            "Independently replay the frozen unit candidate from the accepted prefix."
        )
        canonical = json.loads(result.candidate.output)["canonical"]
        attempt_guard.check("publish Flynn evaluated candidate")
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
        # Executable-only checkpoints bind the replay script bytes, never an invented raster.
        ledger.mark(m, "passed", best={"round": 0, "mean": 0.0, "render": script_rel})
        receipt = unit_evaluation.publish_unit_evaluation_outcome(
            shot.folder,
            str(layer.id),
            active_unit,
            attempt_guard,
            result=canonical,
            canonical_verdicts=canonical_verdicts,
            ledger_slot=ledger._slot(m),
            replay_inputs=replay_inputs,
            candidate_path=script_rel,
        )
        ledger.snapshot_scripts(m)
        run.finish("unit_evaluation_receipt:" + receipt.receipt.receipt_digest)
        return ledger
