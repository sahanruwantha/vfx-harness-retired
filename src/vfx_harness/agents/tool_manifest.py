"""The exact MCP tool names a session may call, stated before it guesses them.

Deferred MCP tools reach a session as names without schemas, so it must resolve them
through ``ToolSearch`` before its first call. The harness already knows the exact
qualified names -- it builds them and passes them as ``allowed_tools`` -- but never told
the session, so sessions guessed the bare form, received "No matching deferred tools
found", and re-queried with the prefix. Measured on one shot: two wasted calls and
several seconds at the start of a materialization session and of a builder session, on
the session type each was meant to be the control for (HIR-0209).

This is enumeration, not prompt tuning: the option space is knowable before the session
starts, so the harness states it instead of letting the model discover it by rejection.
"""

from __future__ import annotations

from collections.abc import Sequence

TOOL_MANIFEST_HEADING = "EXACT TOOL NAMES"


def tool_selection_card(names: Sequence[str]) -> str:
    """Render the qualified tool names and the one ``ToolSearch`` query that loads them."""
    qualified = [str(name) for name in names if str(name).startswith("mcp__")]
    if not qualified:
        return ""
    ordered = sorted(dict.fromkeys(qualified))
    return (
        f"{TOOL_MANIFEST_HEADING}. These MCP tools are deferred: their schemas load only "
        "after ToolSearch resolves them. The names below are exact and complete — a bare "
        "name without its `mcp__<server>__` prefix does not resolve and returns "
        '"No matching deferred tools found". Load every one in a single call:\n'
        f"  ToolSearch query: select:{','.join(ordered)}\n"
        "Do not shorten, guess, or re-derive these names.\n\n"
    )
