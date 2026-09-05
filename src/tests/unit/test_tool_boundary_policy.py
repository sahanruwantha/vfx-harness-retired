"""An escape is a defect, and a repeat does not execute (HIR-0224).

Two shots spent twelve and five consecutive calls on a `TypeError` that reached them as
ordinary prose. The handler that raised it caught `(ValueError, OSError,
json.JSONDecodeError)` while its neighbours caught `TypeError` too — ten distinct
hand-picked tuples across the plan tools, so whether a programming error became
retryable text depended on which tool it happened in.
"""

from __future__ import annotations

import asyncio
import contextlib

from claude_agent_sdk import tool

from vfx_harness.agents.plan_tools.boundary import DEFECT_RULE, guard_tool_boundary


def _call(guarded, args=None):
    return asyncio.run(guarded.handler(args or {}))


def _text_of(result) -> str:
    return "\n".join(
        str(item.get("text", "")) for item in (result.get("content") or [])
    )


def _raising(exc: Exception):
    calls = {"n": 0}

    @tool("boom", "raises", {"type": "object"})
    async def _handler(args):
        calls["n"] += 1
        raise exc

    return guard_tool_boundary(_handler), calls


def test_an_escaped_exception_is_typed_as_a_defect_not_a_finding() -> None:
    """The exact escape that killed two runs, and the type is not enumerated anywhere."""
    guarded, _ = _raising(
        TypeError("expected str, bytes or os.PathLike object, not NoneType")
    )
    text = _text_of(_call(guarded))

    assert "boom raised TypeError" in text
    assert "not NoneType" in text
    assert DEFECT_RULE in text
    assert "Do not retry this tool" in text
    assert "no JSON pointer" in text


def test_a_repeat_does_not_reach_the_handler() -> None:
    """The bound is mechanical, so cost stops whether or not the session reasons well.

    hansa called finalize_materialization twelve times and room five. Each attempt ran
    the handler. Here the second call never does.
    """
    guarded, calls = _raising(RuntimeError("deterministic"))

    first = _text_of(_call(guarded))
    assert calls["n"] == 1
    assert "boom raised RuntimeError" in first

    for _ in range(10):
        later = _text_of(_call(guarded))
        assert "already failed with an unrecoverable harness defect" in later
        assert "RuntimeError" in later
    assert calls["n"] == 1, "the handler must not run again after a defect"


def test_any_exception_type_is_covered_because_escaping_is_the_signal() -> None:
    """No tuple to keep in sync: the classification is that it escaped at all."""
    for exc in (
        TypeError("a"), AttributeError("b"), KeyError("c"),
        RuntimeError("d"), ZeroDivisionError("e"),
    ):
        guarded, _ = _raising(exc)
        assert DEFECT_RULE in _text_of(_call(guarded)), type(exc).__name__


def test_a_handlers_own_refusal_is_untouched() -> None:
    """Refusals keep their wording; only escapes are reclassified."""
    @tool("refuser", "refuses", {"type": "object"})
    async def _handler(args):
        return {
            "content": [{"type": "text", "text": "/layer/stages/0: fix the row"}],
            "is_error": True,
        }

    result = _call(guard_tool_boundary(_handler))
    assert _text_of(result) == "/layer/stages/0: fix the row"
    assert DEFECT_RULE not in _text_of(result)
    assert result.get("is_error") is True


def test_a_successful_call_passes_through_unchanged() -> None:
    @tool("ok", "works", {"type": "object"})
    async def _handler(args):
        return {"content": [{"type": "text", "text": "VALIDATION PASSED"}]}

    result = _call(guard_tool_boundary(_handler))
    assert _text_of(result) == "VALIDATION PASSED"
    assert "is_error" not in result


def test_the_defect_is_reported_to_the_run_not_only_to_the_model() -> None:
    seen: list[tuple[str, str, int]] = []

    @tool("boom", "raises", {"type": "object"})
    async def _handler(args):
        raise TypeError("boom")

    guarded = guard_tool_boundary(
        _handler, on_defect=lambda name, exc, n: seen.append((name, type(exc).__name__, n))
    )
    _call(guarded)
    assert seen == [("boom", "TypeError", 1)]


def test_the_guard_changes_behaviour_rather_than_only_existing() -> None:
    """The discriminator: the same handler, with and without the policy.

    Deleting the module makes the other tests fail on import, which proves the module is
    new and nothing else. This compares the two behaviours directly, so it states what
    the mechanism does rather than that it is present.

    Unguarded is the pre-fix world: the exception escapes the handler, the SDK renders it
    as an ordinary tool error, and every call runs the handler again — twelve times in
    one shot, five in another.
    """
    calls = {"raw": 0, "wrapped": 0}

    @tool("raw", "raises", {"type": "object"})
    async def _raw(args):
        calls["raw"] += 1
        raise TypeError("expected str, bytes or os.PathLike object, not NoneType")

    @tool("wrapped", "raises", {"type": "object"})
    async def _wrapped(args):
        calls["wrapped"] += 1
        raise TypeError("expected str, bytes or os.PathLike object, not NoneType")

    guarded = guard_tool_boundary(_wrapped)

    # Unguarded: the exception leaves the handler on every call, so nothing bounds it.
    for _ in range(3):
        with contextlib.suppress(TypeError):
            _call(_raw)
    assert calls["raw"] == 3, "without the policy every call reaches the handler"

    # Guarded: one execution, then refusals that never reach the handler, and the first
    # answer already says it is not addressable.
    first = _text_of(_call(guarded))
    for _ in range(2):
        _call(guarded)
    assert calls["wrapped"] == 1, "with the policy only the first call reaches the handler"
    assert "Do not retry this tool" in first
