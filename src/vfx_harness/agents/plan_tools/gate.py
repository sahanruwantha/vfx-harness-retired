"""Agent SDK tools for the PLAN harness — scene forensics + the spike lab."""

from __future__ import annotations

import json
from pathlib import Path

import anyio
from claude_agent_sdk import tool

from vfx_harness.agents import materialization_operations
from vfx_harness.agents.plan_tools.media import _text
from vfx_harness.domain.brief import load_shot
from vfx_harness.evaluation import plan_gate
from vfx_harness.knowledge import planning_vocabulary
from vfx_harness.observability.log import log
from vfx_harness.orchestration import authority_selection, vocabulary_gap_publication
from vfx_harness.orchestration.authority_selection_transaction import require_matching_authority_selection_token
from vfx_harness.orchestration.escalate import ask as _ask
from vfx_harness.orchestration.jit_materialization import stage_candidate_view
from vfx_harness.orchestration.plan_authority import prepare_consumer_view


def register_gate_tools(**closed):

    shot_folder = closed["shot_folder"]
    _resolve = closed["_resolve"]
    _keep = closed["_keep"]
    candidate_materialization = closed["candidate_materialization"]
    overlay_root = closed["overlay_root"]
    layout = closed["layout"]
    ns = closed["ns"]
    gate_calls = ns.gate_calls
    prior_gate_signature = ns.prior_gate_signature
    prior_preview_signature = ns.prior_preview_signature
    materialization_requirement_statements = ns.materialization_requirement_statements
    @tool(
        "ask_supervisor",
        "Raise a question ONLY the client can settle — an ambiguity in the brief, a "
        "contradiction between the brief and the stills, or a taste call that is theirs. "
        "Does not block: state the assumption you will plan on and continue. A human "
        "answers before an AFFECTED layer starts. Name the affected layer ids and/or "
        "owned axes; use global_decision only when every layer truly depends on it. "
        "Do NOT use it for anything measure_ref or a spike could answer. Do NOT use it "
        "to ask which selected DAG layer is the earliest geometry successor; that id is "
        "compiled as earliest_geometry_layer on the kickoff card.",
        {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "assumption": {"type": "string"},
                "why_it_matters": {"type": "string"},
                "affected_layers": {"type": "array", "items": {"type": "string"}},
                "affected_axes": {"type": "array", "items": {"type": "string"}},
                "global_decision": {"type": "boolean"},
            },
            "required": ["question", "assumption", "affected_layers"],
        },
    )
    async def ask_supervisor(args):

        qid = _ask(
            shot_folder,
            layer="PLAN",
            question=args["question"],
            assumption=args["assumption"],
            why_it_matters=args.get("why_it_matters", ""),
            affected_layers=args.get("affected_layers") or [],
            affected_axes=args.get("affected_axes") or [],
            global_decision=bool(args.get("global_decision")),
        )
        return _text(f"Recorded as Q{qid}. Continue planning on: {args['assumption']}")

    @tool(
        "escalate_vocabulary_gap",
        "Record that NO evidence kind can express a claim you must close. This is the "
        "honest alternative to padding: a typed durable record of the requirement, the "
        "kinds you attempted, and why each cannot certify the claim. Close the "
        "requirement with an explicit decision resolution that references the returned "
        "gap id — never with a trivially-satisfiable contract (those are rejected at "
        "validation). Gaps are visible to the operator and to future planning sessions.",
        vocabulary_gap_publication.argument_schema(materialization_requirement_statements),
    )
    async def escalate_vocabulary_gap(args):
        selected = authority_selection.resolve_selected_authority(shot_folder)

        def check_current():
            current = authority_selection.resolve_selected_authority(shot_folder)
            require_matching_authority_selection_token(selected.selection_token, current.selection_token)

        observed = vocabulary_gap_publication.record_gap(
            shot=shot_folder, run_id=layout.run_id, arguments=args,
            statements=materialization_requirement_statements, check_current=check_current,
            authority_binding=f"plan-gap:{layout.run_id}:{selected.selection_token}",
            commit_guard=lambda: materialization_operations._current_selection_guard(shot_folder, selected),
        )
        gap_id = observed["record"]["id"]
        log(f"plan-lab vocabulary gap {gap_id}: {args['requirement_id']} — {str(args['claim'])[:70]}", 1)
        authored = materialization_requirement_statements.get(
            str(args["requirement_id"]), ""
        )
        statement_rule = (
            f" Use the exact authored statement {authored!r}; materialization may not "
            "rewrite it."
            if authored
            else ""
        )
        return _text(
            f"Recorded {gap_id} for {args['requirement_id']}. Close the requirement with an "
            f"explicit approved_start or planner_start decision resolution ({gap_id} is "
            "durable audit state; the decision carries the authored statement + "
            "decision_strength), not a contract. The recorded gap is what makes that "
            "decision legal for this requirement whatever its declared domains, and binding "
            "same-domain contracts instead is the padding the gap says cannot measure it."
            + statement_rule
            + " The gap is durable state: the harness "
            "grows the vocabulary against it, and a later generation re-binds the "
            "requirement to a real metric."
        )

    @tool(
        "run_gate",
        "Run the free deterministic plan gate against the current working artifacts. "
        "Use during draft, verify, or repair after a coherent artifact sweep so cross-file "
        "defects are fixed while context is warm. Read-only and capped at four calls per "
        "planning session.",
        {"type": "object", "properties": {}},
    )
    async def run_gate(args):
        nonlocal gate_calls, prior_gate_signature
        gate_calls += 1
        if gate_calls > 4:
            return _text(
                "run_gate call cap reached (4); finish the bounded planning sweep",
                is_error=True,
            )

        result = plan_gate.run(shot_folder, require_scene_checks=True)
        # The filesystem leaf is deliberately named ``plan-workspace``; reports and
        # feedback must retain the authored shot identity from brief.md instead.
        result.shot = load_shot(shot_folder).id
        body = plan_gate.report(result)
        signature = result.signature()
        if not result.clean and signature == prior_gate_signature:
            return _text(
                body
                + "\n\nGATE PLATEAU: findings are unchanged from the previous call. "
                "Stop editing and end this session; the outer deterministic loop owns "
                "any further repair.",
                is_error=True,
            )
        prior_gate_signature = signature
        repair = plan_gate.feedback(result)
        return _text(body + (f"\n\nREPAIR BRIEF\n{repair}" if repair else ""))

    @tool(
        "evidence_vocabulary",
        "The complete registry of scene-contract evidence kinds: definition, evidence "
        "domain, and the structural fields each kind requires. Call this BEFORE "
        "authoring contracts, and whenever a validator error mentions a kind, a "
        "property, or a vacuous target — run 20260824T153427Z-91b7c1 burned 8 write "
        "rounds guessing at a vocabulary this call returns in one turn. If no kind can "
        "express a claim, say so via ask_supervisor instead of padding with a "
        "trivially-satisfiable contract; padding shapes are rejected at validation.",
        {"type": "object", "properties": {}},
    )
    async def evidence_vocabulary(args):
        return _text(json.dumps(planning_vocabulary.evidence_vocabulary(), indent=1))


    gate_preview_calls = 0
    prior_preview_signature: str | None = None

    @tool(
        "gate_preview",
        "Run the deterministic plan gate against the CURRENT consumer view (selected "
        "bundle + materialized layers + staged unit plans) — the exact evaluation "
        "terminal publication will apply. Use it before finishing so findings become "
        "fixes in this session instead of a retracted artifact. Read-only; capped at "
        "three calls per session.",
        {"type": "object", "properties": {}},
    )
    async def gate_preview(args):
        nonlocal gate_preview_calls, prior_preview_signature
        gate_preview_calls += 1
        if gate_preview_calls > 3:
            return _text(
                "gate_preview call cap reached (3); finish the artifact and let the "
                "terminal gate decide",
                is_error=True,
            )

        try:
            view = await anyio.to_thread.run_sync(prepare_consumer_view, layout)
            candidate = Path(candidate_materialization) if candidate_materialization else None
            if candidate is not None and candidate.is_file():
                # stage the session's own unpublished payload so the gate previews the
                # POST-publication world — two generations published on a false CLEAN
                # because the preview saw the pre-publication view

                try:
                    await anyio.to_thread.run_sync(
                        lambda: stage_candidate_view(
                            shot_folder, candidate, view, overlay_root=overlay_root
                        )
                    )
                except (ValueError, OSError) as exc:
                    return _text(
                        f"candidate materialization does not validate, so the gate has "
                        f"nothing to preview: {exc}",
                        is_error=True,
                    )
            result = await anyio.to_thread.run_sync(
                lambda: plan_gate.run(view, require_scene_checks=False)
            )
        except Exception as exc:
            log(f"plan-lab ✗ gate_preview: {str(exc)[:120]}", 1)
            return _text(f"gate preview failed: {exc}", is_error=True)
        result.shot = load_shot(shot_folder).id
        body = plan_gate.report(result)
        log(f"plan-lab gate_preview → {'CLEAN' if result.clean else f'{len(result.blocking)} blocking'}", 1)
        signature = result.signature()
        if not result.clean and signature == prior_preview_signature:
            return _text(
                body
                + "\n\nGATE PLATEAU: findings are unchanged from the previous preview. "
                "A finding on this session's own artifact is fixed by publishing it "
                "again (publish_unit_plan / patch_materialization); anything else "
                "(another layer's authority, a global DAG row) belongs to the outer "
                "flow — finish your artifact and report the residue.",
                is_error=True,
            )
        prior_preview_signature = signature
        repair = plan_gate.feedback(result)
        return _text(body + (f"\n\nREPAIR BRIEF\n{repair}" if repair else ""))

    return ask_supervisor, escalate_vocabulary_gap, run_gate, evidence_vocabulary, gate_preview
