"""Legacy recipe registration for roles awaiting the Flynn cutover."""

from collections.abc import Sequence

from claude_agent_sdk import create_sdk_mcp_server, tool

from vfx_harness.knowledge import recipe_lookup, recipes


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
        result = recipe_lookup.lookup(args["query"], roles)
        text, used = result.text, list(result.used)
        if used:
            key = recipe_lookup.fragment_key(str(args["query"]), result.used)
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
