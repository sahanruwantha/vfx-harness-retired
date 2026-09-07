"""Bounded native planning knowledge and questions for one selected layer or unit."""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import contextmanager

import flynn_agents_sdk as flynn
from jsonschema import Draft202012Validator

from vfx_harness.agents import flynn_questions
from vfx_harness.knowledge import planning_vocabulary, recipe_lookup
from vfx_harness.observability.run_artifacts import RunLayout
from vfx_harness.orchestration import authority_selection, plan_inputs, vocabulary_gap_publication
from vfx_harness.orchestration.authority_selection_heads import read_authority_selection_heads
from vfx_harness.orchestration.authority_selection_transaction import (
    authority_selection_lock,
    require_matching_authority_selection_token,
)
from vfx_harness.orchestration.plan_bundle_integrity import digest, read_real_file

MAX_RESPONSE_CHARACTERS = 12_000
RECIPE_READS = 6
RECIPE_CHARACTERS = 12_000
RECIPE_FRAGMENTS = 3


def planning_knowledge_tools(
    *, layout: RunLayout, layer_id: str, check_current: Callable[[], None], unit_id: str | None = None,
) -> tuple[tuple[flynn.Tool, ...], flynn.DispatchGuard]:
    """Bind knowledge to a current VFX scope; callers own session/context policy.

    Layer planning discovers techniques without mutation permission. A selected unit
    additionally filters recipes through its actual mutation roles. Recipe reads are
    charged each time, so a bounded context policy may legally reread evicted material.
    These are fresh-session counters; they do not authorize resuming a spent journal.
    """
    check_current()
    selected = authority_selection.resolve_selected_authority(layout.shot)
    if selected.plan is None:
        raise ValueError("native JIT knowledge requires selected global authority")
    inputs_digest = plan_inputs.exact_planning_input_identity_digest(layout.shot)
    layers = json.loads(read_real_file(
        layout.shot, selected.artifact_paths["layers.json"], "selected planning layers",
    ))["layers"]
    layer = next((row for row in layers if str(row["id"]) == layer_id), None)
    if layer is None:
        raise ValueError(f"unknown planning layer {layer_id!r}; use an exact selected layer id")
    global_layers = json.loads(read_real_file(
        layout.shot, selected.plan.bundle.root / "layers.json", "global planning layers",
    ))["layers"]
    global_layer = next(row for row in global_layers if str(row["id"]) == layer_id)
    owned_ids = set(global_layer["jit"]["owned_requirements"])
    requirements = json.loads(read_real_file(
        layout.shot, selected.plan.bundle.root / "requirements.json", "global planning requirements",
    ))["requirements"]
    statements = {row["id"]: row["statement"].strip() for row in requirements if row["id"] in owned_ids}
    roles = None
    if unit_id is not None:
        unit = next((row for row in layer["stages"] if row["id"] == unit_id), None)
        if unit is None:
            raise ValueError(f"unknown planning unit {unit_id!r} in layer {layer_id}; use its selected unit id")
        roles = tuple(unit["mutates"]["roles"])
    layer_ids = {str(row["id"]) for row in layers}
    axes = {axis for row in layers for axis in row["owns"]}
    vocabulary = planning_vocabulary.evidence_vocabulary()
    vocabulary_digest = digest(json.dumps(vocabulary, sort_keys=True).encode())
    remaining_reads, remaining_characters = RECIPE_READS, RECIPE_CHARACTERS
    fragments: set[str] = set()

    def identity():
        return {"layer": layer_id, "unit": unit_id, "base_selection": selected.selection_token.to_dict(),
                "planning_inputs_sha256": inputs_digest}

    def check():
        check_current()
        current = authority_selection.resolve_selected_authority(layout.shot)
        require_matching_authority_selection_token(selected.selection_token, current.selection_token)
        if plan_inputs.exact_planning_input_identity_digest(layout.shot) != inputs_digest:
            raise ValueError("native planning knowledge inputs changed; start a new bound attempt")

    def result(text, data, *, refused=False):
        return flynn.ToolResult(
            status="refused" if refused else "ok", content=(flynn.TextContent(text),),
            data_json=json.dumps({**identity(), **data, "mutation_authorized": False}, sort_keys=True),
        )

    def validate_kind(arguments):
        if (set(arguments) != {"kind"} or not isinstance(arguments["kind"], str)
                or arguments["kind"] not in {"", *vocabulary["kinds"]}):
            raise ValueError("evidence_vocabulary requires an exact registered kind, or empty string for the index")

    async def evidence(arguments):
        check()
        kind = arguments["kind"]
        data = ({"kinds": {name: row["domain"] for name, row in vocabulary["kinds"].items()}}
                if not kind else {"kind": kind, **vocabulary["kinds"][kind],
                                  "operators": vocabulary["operators"], "note": vocabulary["note"]})
        text = json.dumps(data, ensure_ascii=False, sort_keys=True)
        if len(text) > MAX_RESPONSE_CHARACTERS:
            raise ValueError("registered evidence entry exceeds bounded response; refine the vocabulary instrument")
        check()
        return result(f"Evidence vocabulary: {kind or 'kind index'}. See the structured selection.",
                      {"schema": "vfx-harness.evidence-vocabulary-observation/v1",
                             "vocabulary_sha256": vocabulary_digest, "selection": data})

    def validate_query(arguments):
        if (set(arguments) != {"query"} or not isinstance(arguments["query"], str)
                or not arguments["query"].strip() or len(arguments["query"]) > 256):
            raise ValueError("find_recipe requires one nonempty query of at most 256 characters")

    async def find_recipe(arguments):
        nonlocal remaining_reads, remaining_characters
        check()
        if remaining_reads == 0:
            return result("Recipe retrieval budget exhausted; stop retrieving in this session.", {
                "schema": "vfx-harness.recipe-observation/v1", "status": "budget_refused",
                "remaining_reads": 0, "remaining_characters": remaining_characters, "used": [],
            }, refused=True)
        remaining_reads -= 1
        found = recipe_lookup.lookup(arguments["query"], roles)
        check()
        fragment = recipe_lookup.fragment_key(arguments["query"], found.used) if found.used else None
        if fragment is not None and fragment not in fragments and len(fragments) == RECIPE_FRAGMENTS:
            return result("Distinct recipe fragment budget exhausted; use a selected fragment or instrument.", {
                "schema": "vfx-harness.recipe-observation/v1", "status": "fragment_refused",
                "remaining_reads": remaining_reads, "remaining_characters": remaining_characters, "used": [],
            }, refused=True)
        if len(found.text) > min(MAX_RESPONSE_CHARACTERS, remaining_characters):
            return result("Recipe response exceeds the remaining budget; request a smaller named section.", {
                "schema": "vfx-harness.recipe-observation/v1", "status": "size_refused",
                "remaining_reads": remaining_reads, "remaining_characters": remaining_characters, "used": [],
            }, refused=True)
        remaining_characters -= len(found.text)
        if fragment is not None:
            fragments.add(fragment)
        return result(found.text, {
            "schema": "vfx-harness.recipe-observation/v1", "status": found.status,
            "query": arguments["query"], "used": list(found.used), "mutation_roles": roles,
            "response_sha256": digest(found.text.encode()), "remaining_reads": remaining_reads,
            "remaining_characters": remaining_characters,
            "selected_fragments": sorted(fragments),
        }, refused=found.status == "out_of_scope")

    question_validator = Draft202012Validator(flynn_questions.QUESTION_SCHEMA)

    def validate_question(arguments):
        errors = list(question_validator.iter_errors(arguments))
        if errors:
            raise ValueError("supervisor question schema: " + "; ".join(error.message for error in errors[:5]))
        if any(not arguments[key].strip() for key in ("question", "assumption", "why_it_matters")):
            raise ValueError("supervisor question, assumption and reason must be nonempty")
        if set(arguments["affected_layers"]) - layer_ids or set(arguments["affected_axes"]) - axes:
            raise ValueError("question impact must name selected layers and axes")
        if not arguments["global_decision"] and not arguments["affected_layers"] and not arguments["affected_axes"]:
            raise ValueError("question requires affected layers/axes or global_decision=true")

    async def ask(arguments):
        scope_digest = digest(json.dumps(identity(), sort_keys=True).encode())
        return await flynn_questions.record_question(
            layout=layout, arguments=arguments, check_current=check, identity=identity,
            authority_binding=f"native-jit-question:{layout.run_id}:{scope_digest}",
        )

    def validate_gap(arguments):
        if unit_id is not None:
            raise ValueError("vocabulary gap recording belongs to layer planning, not a unit session")
        vocabulary_gap_publication.validate_arguments(arguments, statements)

    @contextmanager
    def gap_commit_guard():
        with authority_selection_lock(layout.shot, exclusive=False):
            check_current()
            require_matching_authority_selection_token(
                selected.selection_token, read_authority_selection_heads(layout.shot).token,
            )
            if plan_inputs.exact_planning_input_identity_digest(layout.shot) != inputs_digest:
                raise ValueError("native planning inputs changed before gap publication")
            yield

    async def record_gap(arguments):
        binding_digest = digest(json.dumps(identity(), sort_keys=True).encode())
        data = vocabulary_gap_publication.record_gap(
            shot=layout.shot, run_id=layout.run_id, arguments=arguments, statements=statements,
            check_current=check, commit_guard=gap_commit_guard,
            authority_binding=f"native-jit-gap:{layout.run_id}:{binding_digest}",
        )
        return result(
            f"Recorded {data['record']['id']}. Propose an explicit decision with the exact authored statement; "
            "the VFX gate still evaluates that decision. The gap itself accepts no plan or scene.",
            {"schema": "vfx-harness.vocabulary-gap-observation/v1", **data},
        )

    async def guard(_):
        check()
        return flynn.GuardDecision(True, "selected VFX planning knowledge scope remains current")

    check()
    return (
        (
            flynn.Tool.structured("evidence_vocabulary", description=(
                "Read the evidence kind index with kind='', then select one exact kind for its definition, "
                "domain, fields and operators. Never guess a kind or substitute a vacuous contract."
            ), parameters_json=json.dumps({
                "type": "object", "properties": {"kind": {"type": "string", "enum": ["", *vocabulary["kinds"]]}},
                "required": ["kind"], "additionalProperties": False,
            }), validate=validate_kind, execute=evidence),
            flynn.Tool.structured("find_recipe", description=(
                "Search vetted recipes. Query an exact name for its section index, then name#section for one "
                "fragment. Six reads, three distinct fragments and 12000 response characters per session; "
                "rereads consume read and character budgets. "
                "Planning discovery grants no mutation authority. Unit scope filters recipes by mutation roles."
            ), parameters_json=json.dumps({
                "type": "object", "properties": {"query": {"type": "string", "minLength": 1, "maxLength": 256}},
                "required": ["query"], "additionalProperties": False,
            }), validate=validate_query, execute=find_recipe),
            flynn.Tool.structured("ask_supervisor", description=(
                "Record a client ambiguity and its working assumption, with exact selected layer/axis impact. "
                "A duplicate returns its stored assumption; this neither answers the question nor approves a plan."
            ), parameters_json=json.dumps(flynn_questions.QUESTION_SCHEMA),
                validate=validate_question, execute=ask, external_action=True),
            *((flynn.Tool.structured("escalate_vocabulary_gap", description=(
                "Record why registered evidence kinds cannot certify one requirement owned by this layer. "
                "Use its exact authored statement as claim. This enables an explicit provisional decision; "
                "it does not approve that decision or prove the model's diagnosis."
            ), parameters_json=json.dumps(vocabulary_gap_publication.argument_schema(statements)),
                validate=validate_gap, execute=record_gap, external_action=True),)
               if unit_id is None and statements else ()),
        ), flynn.DispatchGuard("current-vfx-planning-knowledge", guard),
    )
