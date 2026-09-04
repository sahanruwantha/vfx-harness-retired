"""One idle deadline for every model response stream.

AGENTS.md requires every model response stream to carry a positive event-idle deadline
(HIR-0138), but only the builder's drain loop enforced one: the planner, materialization and
rematerialization streams iterated ``query(...)`` with no deadline at all. A watcher reasoning
about "how much of the 360s deadline is spent" on a materialization session was therefore
reasoning about a deadline that did not exist (HIR-0200).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import anyio

from vfx_harness.agents.resilience import AgentSessionFailure
from vfx_harness.infrastructure.config import Settings
from vfx_harness.observability import transcript


def idle_deadline_seconds() -> int:
    """The configured event-idle deadline, read once per stream."""

    return Settings.from_environment(load_dotenv_file=False).model_event_idle_seconds


async def with_idle_deadline(
    stream: Any,
    *,
    label: str,
    idle_seconds: int | None = None,
) -> AsyncIterator[Any]:
    """Yield each SDK message, failing closed when none arrives before the deadline.

    Turn and spend caps cannot bound a stream that never emits a terminal result, so the
    deadline is measured on event arrival and its expiry is a typed session failure, not an
    operator interrupt.
    """
    deadline = int(idle_seconds if idle_seconds is not None else idle_deadline_seconds())
    if deadline < 1:
        raise ValueError("model event-idle deadline must be positive")
    iterator = stream.__aiter__()
    messages_seen = 0
    last_message_type = "response_start"
    while True:
        try:
            with anyio.fail_after(deadline):
                message = await anext(iterator)
        except StopAsyncIteration:
            return
        except TimeoutError as exc:
            transcript.event(
                "model_event_idle_timeout",
                idle_seconds=deadline,
                messages_seen=messages_seen,
                last_message_type=last_message_type,
                label=label,
            )
            raise AgentSessionFailure(
                f"{label} emitted no SDK event for {deadline}s after {last_message_type} "
                f"({messages_seen} message(s) seen); the model session is indeterminate",
                "model_session_idle_timeout",
            ) from exc
        messages_seen += 1
        last_message_type = type(message).__name__
        yield message


__all__ = ["idle_deadline_seconds", "with_idle_deadline"]
