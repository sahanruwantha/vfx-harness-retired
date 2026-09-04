"""One model session's turn accounting, owned by the harness rather than read off the CLI.

The budget the harness computes (HIR-0177) is denominated in turns, is handed to the SDK as
``max_turns``, and was reported at completion as the CLI's ``num_turns``: a different counter
that empirically exceeded its own cap (hansa run 20260904T143358Z-238376 reported 14 against
a cap of 12 and terminated ``success``). An operator reading the logs could therefore not
tell whether a budget bound anything or how much headroom remained. The harness counts the
assistant turns it observes in the stream it already reads and reports that beside the
budget; the CLI counter stays, labelled as its own (HIR-0199).

This is a dependency-free leaf so the logger and the transcript can share one counter.
"""

from __future__ import annotations

_STATE: dict[str, object] = {"observed": 0, "budget": None}


def begin(budget: int | None) -> None:
    """Declare the budget the next model session runs under and reset the counter."""

    _STATE["observed"] = 0
    _STATE["budget"] = int(budget) if isinstance(budget, int) and not isinstance(budget, bool) and budget > 0 else None


def reset_observed() -> None:
    """A new session started under the same declared budget; count its turns from zero."""

    _STATE["observed"] = 0


def observe_turn() -> None:
    """Count one assistant turn of the live session."""

    _STATE["observed"] = int(_STATE["observed"] or 0) + 1


def accounting() -> dict[str, object]:
    """Observed assistant turns and the declared budget for the live session."""

    return {"observed": int(_STATE["observed"] or 0), "budget": _STATE["budget"]}


__all__ = ["accounting", "begin", "observe_turn", "reset_observed"]
