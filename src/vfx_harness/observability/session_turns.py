"""One model session's turn accounting, stated so the two counters cannot be confused.

``max_turns`` is handed to the SDK, which passes it to the CLI as ``--max-turns``; the
CLI enforces it and reports its own ``num_turns``. Across 168 sessions in this
repository's artifacts that is demonstrably the bounded quantity: the one session where
the cap engaged reported ``num_turns`` 39 against a cap of 38 -- exactly ``cap + 1`` --
while the harness's own stream count stood at 75.

HIR-0199 concluded the opposite, from a real observation inverted. It saw ``num_turns``
report 14 against a cap of 12 and read the overshoot as proof the counter was unbounded.
Across the corpus ``num_turns`` overshoots its cap by at most 3, while the replacement
counter reached 2.03x its budget on a session that terminated ``success``. The fix
substituted a counter that overshoots by 38 for one that overshoots by 3, and stated the
substitution as a rule (HIR-0228).

The stream count is still worth having -- it is available live, where ``num_turns`` only
arrives with the result -- but it is not turns. It counts ``AssistantMessage`` values, and
the SDK emits a median of 1.55 of those per CLI turn. So it is named for what it counts.
"""

from __future__ import annotations

_STATE: dict[str, object] = {"assistant_messages": 0, "budget": None}


def begin(budget: int | None) -> None:
    """Declare the budget the next model session runs under and reset the counter."""

    _STATE["assistant_messages"] = 0
    _STATE["budget"] = (
        int(budget)
        if isinstance(budget, int) and not isinstance(budget, bool) and budget > 0
        else None
    )


def reset_observed() -> None:
    """A new session started under the same declared budget; count from zero."""

    _STATE["assistant_messages"] = 0


def observe_assistant_message() -> None:
    """Count one assistant message of the live session.

    Not a turn: the SDK emits several of these per CLI turn, so this number is a
    liveness and volume signal, never a measure of the ``max_turns`` budget.
    """

    _STATE["assistant_messages"] = int(_STATE["assistant_messages"] or 0) + 1


def accounting() -> dict[str, object]:
    """Assistant messages observed live, and the declared budget.

    The budget's own counter is the CLI's ``num_turns``, which is not available until
    the result message; consumers that have it should report it against ``budget`` and
    this count as its own quantity.
    """

    return {
        "assistant_messages": int(_STATE["assistant_messages"] or 0),
        "budget": _STATE["budget"],
    }


__all__ = ["accounting", "begin", "observe_assistant_message", "reset_observed"]
