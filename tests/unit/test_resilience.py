from __future__ import annotations

import anyio
import pytest

from vfx_harness.agents.resilience import AgentSessionFailure, result_signal, run_session


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


def test_max_turns_does_not_publish_just_because_a_candidate_exists() -> None:
    """Remat5 (975cb6): the session wrote jit-layer-1.json, hit error_max_turns, and
    run_session treated the mtime bump as success. publish_materialization then raised
    on the dirty document, so the run was process_error instead of a failed transaction.
    A written candidate is not a select."""
    attempts = 0

    async def wrote_then_exhausted() -> str:
        nonlocal attempts
        attempts += 1
        return "VALIDATION FAILED for jit-layer-1.json\nsubtype=error_max_turns"

    async def exercise() -> None:
        with pytest.raises(AgentSessionFailure) as caught:
            await run_session(
                wrote_then_exhausted,
                succeeded=lambda: True,
                label="materialize layer 1",
                attempts=4,
                base_delay=0.001,
            )
        assert caught.value.terminal_cause == "max_turns_exhausted"

    anyio.run(exercise)
    assert attempts == 1


def test_max_turns_accepts_an_explicit_terminal_attestation_only_when_opted_in() -> None:
    attempts = 0

    async def finalized_then_exhausted() -> str:
        nonlocal attempts
        attempts += 1
        return "FINALIZATION ATTESTED\nsubtype=error_max_turns"

    async def exercise() -> None:
        await run_session(
            finalized_then_exhausted,
            succeeded=lambda: True,
            label="materialize layer 2",
            attempts=4,
            base_delay=0.001,
            accept_max_turns_if_succeeded=True,
        )

    anyio.run(exercise)
    assert attempts == 1


def test_sdk_result_subtype_reaches_the_classifier() -> None:
    """The SDK reports max-turn termination only as ResultMessage.subtype — never as an
    assistant text block — so the collected signal must carry it explicitly or a real
    exhaustion is retried once and mislabeled session_stalled."""

    class ResultMessage:  # duck-typed like the SDK message the collectors iterate
        subtype = "error_max_turns"

    class AssistantMessage:
        subtype = "irrelevant"  # only ResultMessage carries a session verdict

    signal = result_signal(ResultMessage())
    assert signal is not None
    assert result_signal(AssistantMessage()) is None

    attempts = 0

    async def exhausted() -> str:
        nonlocal attempts
        attempts += 1
        return f"planner narration that never mentions the limit\n{signal}"

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


def test_sdk_result_signal_keeps_provider_error_fields() -> None:
    class ResultMessage:
        subtype = "success"
        is_error = True
        api_error_status = 429

    assert result_signal(ResultMessage()) == (
        "[session result: subtype=success is_error=true api_error_status=429]"
    )


def test_max_turns_raised_as_sdk_process_error_is_classified() -> None:
    """Run 20260823T082044Z-22d8ab: the SDK raised AFTER yielding the result message, so
    the collected signal (with the subtype) was discarded and only the exception text was
    classified — and 'maximum number of turns' slipped past the old regex. Four full
    12-turn sessions were burned before a mislabeled `session_stalled`."""
    attempts = 0

    async def raises_like_the_sdk() -> str:
        nonlocal attempts
        attempts += 1
        raise RuntimeError(
            "Claude Code returned an error result: Reached maximum number of turns (12) "
            "(exit code: 1)"
        )

    async def exercise() -> None:
        with pytest.raises(AgentSessionFailure) as caught:
            await run_session(
                raises_like_the_sdk,
                succeeded=lambda: False,
                label="global plan",
                attempts=4,
                base_delay=0.001,
            )
        assert caught.value.terminal_cause == "max_turns_exhausted"

    anyio.run(exercise)
    assert attempts == 1


def test_successful_result_subtype_does_not_read_as_exhaustion() -> None:
    class ResultMessage:
        subtype = "success"

    signal = result_signal(ResultMessage())
    assert signal is not None  # kept in the signal for diagnostics

    async def wrote_nothing() -> str:
        return f"session ended normally having written nothing\n{signal}"

    async def exercise() -> None:
        with pytest.raises(AgentSessionFailure) as caught:
            await run_session(
                wrote_nothing,
                succeeded=lambda: False,
                label="global plan",
                attempts=2,
                base_delay=0.001,
            )
        assert caught.value.terminal_cause == "session_stalled"

    anyio.run(exercise)


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
