"""One constructor for SDK options, so a declared turn budget is always observable.

A budget handed to the SDK as ``max_turns`` is only meaningful if the harness can compare
it with the turns it actually observed. Constructing options in eight places meant the
budget was known at construction and nowhere afterwards, so completion reported the CLI's
own counter beside a budget it does not measure (HIR-0199). Every options construction that
carries a turn budget goes through here; an architecture test enforces it.
"""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import ClaudeAgentOptions

from vfx_harness.observability import session_turns


def sdk_options(**kwargs: Any) -> ClaudeAgentOptions:
    """Build SDK options and declare their turn budget to the harness counter."""

    session_turns.begin(kwargs.get("max_turns"))
    return ClaudeAgentOptions(**kwargs)


__all__ = ["sdk_options"]
