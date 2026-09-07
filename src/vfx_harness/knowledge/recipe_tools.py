"""Legacy recipe registration for roles awaiting the Flynn cutover."""

import re
from collections.abc import Sequence

from claude_agent_sdk import create_sdk_mcp_server, tool

from vfx_harness.knowledge import recipes


def build_recipe_tools(on_use=None, mutation_roles: Sequence[str] | None = None):
    """An MCP server exposing find_recipe to the build agent."""
    roles = tuple(mutation_roles) if mutation_roles is not None else None
    context_budget = recipes.RecipeContextBudget()

    @tool(
        "find_recipe",
        "Search the vetted Blender recipe cookbook for a technique — volumetrics, "
        "materials, compositor, instancing, grade. Returns matching snippets + gotchas "
        "to ADAPT into your run_bpy. Fuzzy queries return summaries; an exact name "
        "returns a compact section index; use name#section for one body fragment and "
        "name#full only when multiple sections are jointly necessary. Call this BEFORE "
        "hand-rolling any hard effect. "
        "Recipes that mutate roles this unit does not own are refused (abstain), not ranked.",
        {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    )
    async def find_recipe(args):
        hits = recipes.search_recipes(args["query"], mutation_roles=roles)
        if not hits:
            blocked = recipes.out_of_scope_hits(args["query"], roles or ()) if roles is not None else []
            if blocked:
                names = ", ".join(rec["name"] for rec in blocked)
                present = ", ".join(roles) if roles else "(none)"
                return {"content": [{"type": "text",
                        "text": (
                            "ABSTAIN: no in-scope recipe for that query. Keyword hits "
                            f"{names} require mutation roles this unit does not own. "
                            f"Requested query {args['query']!r}; mutation roles present: "
                            f"{present}. Do not improvise those techniques here — they "
                            "belong to a unit that mutates the missing roles."
                        )}]}
            return {"content": [{"type": "text",
                    "text": "no matching recipe — improvise; a good solution may be harvested "
                            "into the cookbook if this milestone passes."}]}
        text, used = recipes.recipe_search_response(str(args["query"]), hits)
        if used:
            base, separator, selector = str(args["query"]).lower().partition("#")
            key = "-".join(re.findall(r"[a-z0-9]+", base))
            if separator:
                key += "#" + "-".join(re.findall(r"[a-z0-9]+", selector))
            refused = context_budget.admit(key, len(text))
            if refused:
                return {"content": [{"type": "text", "text": refused}]}
        if on_use and used:
            try:
                on_use(used)
            except Exception:
                # telemetry drives promote/prune; losing it silently means the cookbook
                # can never learn which recipes earn their place
                print("! recipe-use telemetry failed", flush=True)
        return {"content": [{"type": "text", "text": text}]}

    server = create_sdk_mcp_server(name="recipes", version="0.1.0", tools=[find_recipe])
    return server, ["mcp__recipes__find_recipe"]
