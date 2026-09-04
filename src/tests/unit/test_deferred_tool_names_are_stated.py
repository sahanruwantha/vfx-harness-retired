"""A session is told its exact deferred tool names before it guesses them (HIR-0209).

caesar run 20260904T191549Z-e048a6, rematerialization session, verbatim:

    ToolSearch {"query": "select:evidence_vocabulary,stage_materialization_unit,..."}
      -> "No matching deferred tools found"
    ToolSearch {"query": "select:mcp__plan__evidence_vocabulary,..."}
      -> 8 tool_references

Two calls and several seconds to learn a prefix the harness passed as ``allowed_tools`` in
the same construction. First reported as a builder-only defect with materialization as the
control; the control did it too, so it is model variance on every session type.
"""

from __future__ import annotations

from vfx_harness.agents import sdk_options as sdk_options_module
from vfx_harness.agents.tool_manifest import tool_selection_card

NAMES = (
    "mcp__plan__stage_materialization_unit",
    "mcp__plan__evidence_vocabulary",
    "mcp__recipes__find_recipe",
    "Read",
    "Glob",
)


def test_the_card_names_every_qualified_tool_in_one_query() -> None:
    card = tool_selection_card(NAMES)

    assert (
        "select:mcp__plan__evidence_vocabulary,"
        "mcp__plan__stage_materialization_unit,"
        "mcp__recipes__find_recipe" in card
    )
    assert card.count("ToolSearch query:") == 1, "one call loads them all"
    assert "Read" not in card and "Glob" not in card, "built-in tools are not deferred"
    assert "No matching deferred tools found" in card, "the exact rejection it prevents"


def test_a_session_without_mcp_tools_gets_no_card() -> None:
    assert tool_selection_card(("Read", "Glob", "WebFetch")) == ""
    assert tool_selection_card(()) == ""


def test_the_card_reaches_every_session_through_the_one_seam(monkeypatch) -> None:
    """No kickoff can forget it: it is added where every session is constructed."""
    captured: dict = {}
    monkeypatch.setattr(
        sdk_options_module, "ClaudeAgentOptions", lambda **kw: captured.update(kw) or object()
    )

    sdk_options_module.sdk_options(
        system_prompt="ORIGINAL SYSTEM PROMPT", allowed_tools=list(NAMES), max_turns=12
    )

    assert captured["system_prompt"].endswith("ORIGINAL SYSTEM PROMPT")
    assert captured["system_prompt"].startswith("EXACT TOOL NAMES")
    assert "mcp__plan__stage_materialization_unit" in captured["system_prompt"]
    assert captured["allowed_tools"] == list(NAMES), "the tool list itself is unchanged"


def test_a_session_with_no_system_prompt_is_left_alone(monkeypatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(
        sdk_options_module, "ClaudeAgentOptions", lambda **kw: captured.update(kw) or object()
    )

    sdk_options_module.sdk_options(allowed_tools=list(NAMES))
    assert "system_prompt" not in captured
