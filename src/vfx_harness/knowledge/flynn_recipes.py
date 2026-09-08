"""Shared bounded Flynn recipe retrieval; VFX owns scope and context admission."""

from __future__ import annotations

import json

import flynn_agents_sdk as flynn

from vfx_harness.knowledge import recipe_lookup
from vfx_harness.orchestration.plan_bundle_integrity import digest

MAX_READS = 6
MAX_CHARACTERS = 12_000
MAX_FRAGMENTS = 3


def recipe_tool(*, roles, check, result, result_fits=None, max_response_characters=MAX_CHARACTERS):
    """Charge rereads without retaining a growing prompt or granting mutation rights.

    The caller binds observation identity and may refuse a whole result that cannot
    fit its required active context. SQLite retains per-read fragment use and digest.
    Fresh counters never authorize resuming a spent journal.
    """
    remaining_reads, remaining_characters = MAX_READS, MAX_CHARACTERS
    fragments: set[str] = set()

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
        if fragment is not None and fragment not in fragments and len(fragments) == MAX_FRAGMENTS:
            return result("Distinct recipe fragment budget exhausted; use a selected fragment or instrument.", {
                "schema": "vfx-harness.recipe-observation/v1", "status": "fragment_refused",
                "remaining_reads": remaining_reads, "remaining_characters": remaining_characters, "used": [],
            }, refused=True)
        if len(found.text) > min(max_response_characters, remaining_characters):
            return result("Recipe response exceeds the remaining budget; request a smaller named section.", {
                "schema": "vfx-harness.recipe-observation/v1", "status": "size_refused",
                "remaining_reads": remaining_reads, "remaining_characters": remaining_characters, "used": [],
            }, refused=True)
        response = result(found.text, {
            "schema": "vfx-harness.recipe-observation/v1", "status": found.status,
            "query": arguments["query"], "used": list(found.used), "mutation_roles": roles,
            "response_sha256": digest(found.text.encode()), "remaining_reads": remaining_reads,
            "remaining_characters": remaining_characters - len(found.text),
            "selected_fragments": sorted(fragments | ({fragment} if fragment is not None else set())),
        }, refused=found.status == "out_of_scope")

        if result_fits is not None and not result_fits(response):
            return result("Recipe does not fit the active required context; request a smaller named section.", {
                "schema": "vfx-harness.recipe-observation/v1", "status": "context_refused",
                "remaining_reads": remaining_reads, "remaining_characters": remaining_characters, "used": [],
            }, refused=True)
        remaining_characters -= len(found.text)
        if fragment is not None:
            fragments.add(fragment)
        return response

    return flynn.Tool.structured("find_recipe", description=(
        "Search vetted recipes. Query an exact name for its section index, then name#section for one "
        "fragment. Six reads, three distinct fragments and 12000 response characters per session; "
        "rereads consume read and character budgets. "
        "Retrieval grants no mutation authority. Unit scope filters recipes by mutation roles."
    ), parameters_json=json.dumps({
        "type": "object", "properties": {"query": {"type": "string", "minLength": 1, "maxLength": 256}},
        "required": ["query"], "additionalProperties": False,
    }), validate=validate_query, execute=find_recipe)
