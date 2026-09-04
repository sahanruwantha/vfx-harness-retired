"""One constructor for SDK options, so a declared budget and tool set are always stated.

A budget handed to the SDK as ``max_turns`` is only meaningful if the harness can compare
it with the turns it actually observed. Constructing options in eight places meant the
budget was known at construction and nowhere afterwards, so completion reported the CLI's
own counter beside a budget it does not measure (HIR-0199). Every options construction that
carries a turn budget goes through here; an architecture test enforces it.

The same seam states the exact deferred MCP tool names. They are known here — they are the
``allowed_tools`` this very call passes — and were never told to the session, so sessions
guessed the bare form and spent two calls learning the prefix (HIR-0209). Stating them at
the one place every session is constructed means no kickoff can forget to.
"""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import ClaudeAgentOptions

from vfx_harness.agents.tool_manifest import tool_selection_card
from vfx_harness.observability import session_turns


def sdk_options(**kwargs: Any) -> ClaudeAgentOptions:
    """Build SDK options, declare the turn budget, and name the deferred tools."""

    session_turns.begin(kwargs.get("max_turns"))
    system_prompt = kwargs.get("system_prompt")
    if isinstance(system_prompt, str) and system_prompt:
        card = tool_selection_card(kwargs.get("allowed_tools") or ())
        if card:
            kwargs["system_prompt"] = card + system_prompt
    return ClaudeAgentOptions(**kwargs)


__all__ = ["sdk_options"]
