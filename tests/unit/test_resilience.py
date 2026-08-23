from __future__ import annotations

import anyio
import pytest

from vfx_harness.agents.resilience import AgentSessionFailure, run_session


def test_max_turns_is_terminal_and_not_retried() -> None:
    attempts = 0

    async def exhausted() -> str:
        nonlocal attempts
        attempts += 1
        return "subtype=error_max_turns"

    async def exercise() -> None:
        with pytest.raises(AgentSessionFailure) as caught:
            await run_session(
                exhausted,
                succeeded=lambda: False,
                label="global plan",
                attempts=4,
                base_delay=0.001,
            )
        assert caught.value.terminal_cause == "max_turns_exhausted"

    anyio.run(exercise)
    assert attempts == 1


def test_unknown_empty_session_has_stable_stalled_cause() -> None:
    async def empty() -> str:
        return ""

    async def exercise() -> None:
        with pytest.raises(AgentSessionFailure) as caught:
            await run_session(
                empty,
                succeeded=lambda: False,
                label="global plan",
                attempts=2,
                base_delay=0.001,
            )
        assert caught.value.terminal_cause == "session_stalled"

    anyio.run(exercise)
