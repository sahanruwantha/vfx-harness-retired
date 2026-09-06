"""Every model stream carries the deadline, and its remaining budget is readable (HIR-0200).

Caesar's layer-1 materialization was transcript-silent for ten minutes while healthy; three
sessions each invented a liveness heuristic from mtime and two were wrong. The one method
that worked was reading /proc counters of the SDK child. Meanwhile the deadline those
watchers were reasoning about did not exist on that stream at all: only the builder's drain
loop enforced one.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import anyio
import pytest

from vfx_harness.agents.model_stream import with_idle_deadline
from vfx_harness.agents.resilience import AgentSessionFailure
from vfx_harness.observability import phase_heartbeat

SRC = Path(__file__).resolve().parents[2] / "vfx_harness"


def _Message(name: str):
    """A message whose class name is what the harness reads; distinct per name."""
    return type(name, (), {})()


async def _stream(items, *, stall_after: int | None = None):
    for index, item in enumerate(items):
        if stall_after is not None and index >= stall_after:
            await anyio.sleep(30)
        yield item


def test_heartbeat_records_the_deadline_and_the_last_event(tmp_path: Path) -> None:
    path = tmp_path / phase_heartbeat.NAME
    phase_heartbeat.begin(path, stage="plan", label="materialize-layer-1", deadline_seconds=360)

    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["schema"] == phase_heartbeat.SCHEMA
    assert record["deadline_seconds"] == 360
    assert record["state"] == "waiting" and record["events"] == 0
    assert record["stage"] == "plan" and record["label"] == "materialize-layer-1"
    assert record["last_event_at"] == record["started_at"]

    phase_heartbeat.event("AssistantMessage")
    snapshot = phase_heartbeat.snapshot()
    assert snapshot["events"] == 1 and snapshot["last_event_kind"] == "AssistantMessage"

    phase_heartbeat.end()
    assert json.loads(path.read_text(encoding="utf-8"))["state"] == "closed"
    assert phase_heartbeat.snapshot() == {}

    # An unwritable destination never breaks the stage it observes.
    unwritable = tmp_path / "missing-dir" / "x" / phase_heartbeat.NAME
    phase_heartbeat.begin(unwritable, stage="s", label=None, deadline_seconds=1)
    phase_heartbeat.event("SystemMessage")
    phase_heartbeat.end()


def test_a_stalled_stream_fails_closed_with_the_typed_cause(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[tuple] = []
    from vfx_harness.agents import model_stream

    monkeypatch.setattr(
        model_stream.transcript, "event", lambda kind, **fields: events.append((kind, fields))
    )

    async def drain_ok():
        seen = []
        async for message in with_idle_deadline(
            _stream([_Message("SystemMessage"), _Message("ResultMessage")]), label="plan verify", idle_seconds=5
        ):
            seen.append(type(message).__name__)
        return seen

    assert anyio.run(drain_ok) == ["SystemMessage", "ResultMessage"]
    assert events == []

    async def drain_stalled():
        async for _message in with_idle_deadline(
            _stream([_Message("SystemMessage"), _Message("AssistantMessage")], stall_after=1),
            label="materialization",
            idle_seconds=1,
        ):
            pass

    with pytest.raises(AgentSessionFailure) as raised:
        anyio.run(drain_stalled)

    assert raised.value.terminal_cause == "model_session_idle_timeout"
    assert "materialization" in str(raised.value) and "1s" in str(raised.value)
    [(kind, fields)] = events
    assert kind == "model_event_idle_timeout"
    assert fields["idle_seconds"] == 1
    assert fields["messages_seen"] == 1
    assert fields["last_message_type"] == "SystemMessage"


def test_a_nonpositive_deadline_is_refused() -> None:
    async def drain():
        async for _ in with_idle_deadline(_stream([]), label="x", idle_seconds=0):
            pass

    with pytest.raises(ValueError, match="positive"):
        anyio.run(drain)


def test_every_model_stream_is_iterated_through_the_deadline_helper() -> None:
    """A stream added without the helper is one no deadline can bound (HIR-0138)."""
    offenders: list[str] = []
    for path in SRC.rglob("*.py"):
        if path.name in {"model_stream.py", "drain.py"}:
            continue  # the helper itself, and the builder drain's own richer loop
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFor):
                continue
            call = node.iter
            name = getattr(getattr(call, "func", None), "id", None) or getattr(
                getattr(call, "func", None), "attr", None
            )
            if name in {"query", "receive_response"}:
                offenders.append(f"{path.relative_to(SRC)}:{node.lineno}")

    assert offenders == [], (
        "iterate SDK streams through agents.model_stream.with_idle_deadline so the "
        f"event-idle deadline applies (HIR-0200): {offenders}"
    )


def test_begin_reads_the_clock_once(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The flake, made deterministic.

    `begin` captured `started_at` and `last_event_at` from two separate `_now()` calls
    at millisecond resolution, so the two disagreed whenever a millisecond boundary fell
    between them. That is one value derived twice: before any event, "when the phase
    started" and "when it last saw something" are the same instant by definition.

    Observed once in a full-suite run and never in isolation (5/5), which is exactly the
    signature of a clock race and exactly why asserting the identity alone cannot pin it.
    Forcing every `_now()` call to differ makes the identity a real discriminator.
    """
    ticks = iter(f"2026-09-06T00:00:00.{index:03d}+00:00" for index in range(100))
    monkeypatch.setattr(phase_heartbeat, "_now", lambda: next(ticks))

    path = tmp_path / phase_heartbeat.NAME
    phase_heartbeat.begin(path, stage="plan", label=None, deadline_seconds=360)

    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["last_event_at"] == record["started_at"]

    # And an actual event still moves it: the fix must not freeze the field.
    phase_heartbeat.event("AssistantMessage")
    assert phase_heartbeat.snapshot()["last_event_at"] != record["started_at"]
    phase_heartbeat.end()
