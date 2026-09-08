"""The root-owner boundary: one owned v2 run per public invocation (HIR-0172).

A direct public command creates a prepared run, acquires the exclusive run-owner fence,
publishes the v2 ``running`` status that selects its claim, records signal intent, and
terminalizes exactly once: ``passed`` from the run summary, ``failed`` from the typed
stop envelope, or ``interrupted`` through the terminalizer after the recorded SIGINT or
SIGTERM intent.  A stage inherited inside a driver run publishes only its typed stop
envelope; the root owner selects every terminal status.  Signal handlers record intent
and request cancellation by raising; they perform no filesystem or domain work.
"""

from __future__ import annotations

import os
import signal
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from vfx_harness.domain.run_signal_intent import SIGNAL_INTERRUPTION_KINDS, RecordedSignalIntent
from vfx_harness.domain.run_status import RUN_SUMMARY_SCHEMA, STOP_ENVELOPE_LOCATOR
from vfx_harness.observability import run_artifacts
from vfx_harness.observability.run_owner_fence import RunOwnerFenceError, RunOwnerFenceLease, acquire_run_owner_fence
from vfx_harness.observability.runid import RUN_ID
from vfx_harness.orchestration import run_terminalizer

_CURRENT_OWNER: ContextVar[RunOwnerFenceLease | None] = ContextVar("vfx_current_root_owner", default=None)


def require_current_owner(layout: run_artifacts.RunLayout) -> RunOwnerFenceLease:
    """Return the live same-process owner, never derive a capability from disk or env."""
    lease = _CURRENT_OWNER.get()
    if lease is None or lease.acquisition_kind != "owner":
        raise RunOwnerFenceError("operation requires execution inside the live root-owner process")
    lease.require_current_identity()
    if lease.run_root != layout.root or lease.claim.run_id != layout.run_id:
        raise RunOwnerFenceError("current run owner does not own the requested run")
    return lease


class RunCancellation(BaseException):
    """Cancellation requested by a recorded SIGINT or SIGTERM intent."""

    def __init__(self, intent: RecordedSignalIntent) -> None:
        if not isinstance(intent, RecordedSignalIntent):
            raise TypeError("run cancellation carries only a RecordedSignalIntent")
        super().__init__(
            f"run cancellation requested by {intent.kind} (signal {intent.signal_number})"
        )
        self.intent = intent

    @property
    def kind(self) -> str:
        return self.intent.kind

    @property
    def signal_number(self) -> int:
        return self.intent.signal_number


class RunInterrupted(SystemExit):
    """The invocation ended as an interruption; the exit code is the receipt's."""

    def __init__(self, exit_code: int, run_id: str, *, detail: str | None = None) -> None:
        super().__init__(exit_code)
        self.run_id = run_id
        self.detail = detail


@dataclass(slots=True)
class SignalIntent:
    """The first recorded cessation intent; later deliveries converge on it."""

    intent: RecordedSignalIntent | None = None
    deliveries: int = 0

    @property
    def kind(self) -> str | None:
        return None if self.intent is None else self.intent.kind

    @property
    def signal_number(self) -> int | None:
        return None if self.intent is None else self.intent.signal_number


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


class _MonotonicClock:
    """Strictly increasing timestamps for one terminal transaction."""

    def __init__(self) -> None:
        self._last = ""

    def __call__(self) -> str:
        value = _now()
        while value <= self._last:
            value = _now()
        self._last = value
        return value


@contextmanager
def signal_intent_scope() -> Iterator[SignalIntent]:
    """Record the first SIGINT/SIGTERM as intent and raise cancellation once.

    Handlers install only on the main thread, where Python delivers signals.  A second
    delivery while the first intent drains is counted and otherwise ignored, so repeated
    signals converge on one receipt.  Previous handlers are restored on exit.
    """

    intent = SignalIntent()
    if threading.current_thread() is not threading.main_thread():
        yield intent
        return

    def handler(signum: int, _frame: Any) -> None:
        intent.deliveries += 1
        if intent.intent is not None:
            return
        # The only production issuer of a RecordedSignalIntent (architecture-pinned):
        # the terminalizer accepts no kind string, so nothing but a delivered signal can
        # select `interrupted` for an owned run.
        intent.intent = RecordedSignalIntent(
            SIGNAL_INTERRUPTION_KINDS[int(signum)], int(signum), _now()
        )
        raise RunCancellation(intent.intent)

    previous = {signum: signal.signal(signum, handler) for signum in SIGNAL_INTERRUPTION_KINDS}
    try:
        yield intent
    finally:
        for signum, handler_before in previous.items():
            signal.signal(signum, handler_before)


def _metadata(layout: run_artifacts.RunLayout, exc: BaseException | None = None) -> dict[str, Any]:
    reserved = {"schema", "run_id", "state", "updated_at", "exit_code", "detail", "command"}
    rows = dict(layout.terminal_metadata)
    if exc is not None and isinstance(getattr(exc, "run_metadata", None), dict):
        rows.update(exc.run_metadata)
    return {key: value for key, value in rows.items() if key not in reserved}


def _terminalize_success(layout: run_artifacts.RunLayout, lease: RunOwnerFenceLease, command: str) -> None:
    summary = {
        "schema": RUN_SUMMARY_SCHEMA,
        "run_id": layout.run_id,
        "command": command,
        "state": "passed",
        "exit_code": 0,
        **_metadata(layout),
    }
    layout.write_summary(summary)
    run_terminalizer.publish_passed_status(layout.root, lease=lease, summary=summary, updated_at=_now())
    layout.write_inventory()


def terminalize_failure(
    layout: run_artifacts.RunLayout,
    lease: RunOwnerFenceLease,
    command: str,
    exc: BaseException,
) -> None:
    _state, code, cause, detail = run_artifacts.terminal_record(exc)
    try:
        envelope = run_artifacts.publish_exception_stop(layout, command, exc, code=code, terminal_cause=cause)
    except Exception as publish_exc:
        # The failure itself is a harness defect; select it rather than leave the run open.
        envelope = run_artifacts.publish_exception_stop(
            layout,
            command,
            publish_exc,
            code=1,
            terminal_cause="stop_envelope_publication_failure",
        )
        code = 1
        cause = "stop_envelope_publication_failure"
        detail = f"stop-envelope publication failed: {publish_exc}"
    summary = {
        "schema": RUN_SUMMARY_SCHEMA,
        "run_id": layout.run_id,
        "command": command,
        "state": "failed",
        "exit_code": code,
        "detail": detail[:1000],
        "terminal_cause": cause,
        "stop_envelope": STOP_ENVELOPE_LOCATOR,
        "stop_envelope_digest": envelope.digest,
        "stop_class": envelope.stop_class,
        "stop_stage": envelope.stage,
        "cause_fingerprint": envelope.cause_fingerprint,
        **_metadata(layout, exc),
    }
    layout.write_summary(summary)
    run_terminalizer.publish_failed_status(
        layout.root,
        lease=lease,
        envelope=envelope,
        exit_code=code,
        updated_at=_now(),
        detail=detail[:1000],
    )
    layout.write_inventory()


def terminalize_cancellation(
    shot: Path,
    layout: run_artifacts.RunLayout,
    lease: RunOwnerFenceLease,
    cancellation: RunCancellation,
) -> RunInterrupted:
    try:
        result = run_terminalizer.terminalize_interruption(
            shot,
            layout.root,
            lease=lease,
            intent=cancellation.intent,
            clock=_MonotonicClock(),
        )
    except run_terminalizer.RunTerminalizationConflict as exc:
        # The run keeps its running status; its interruption authority is unavailable.
        interrupted = RunInterrupted(
            cancellation.intent.exit_code,
            layout.run_id,
            detail=f"interruption authority unavailable: {exc}",
        )
        interrupted.__cause__ = exc
        return interrupted
    return RunInterrupted(result.status.exit_code or 128 + cancellation.signal_number, layout.run_id)


@contextmanager
def owned_root_run(
    layout: run_artifacts.RunLayout,
    *,
    command: str,
    owner_kind: str,
) -> Iterator[RunOwnerFenceLease]:
    """Acquire the root fence for a prepared run and publish its running status."""

    with acquire_run_owner_fence(
        layout.root,
        run_id=layout.run_id,
        command=command,
        owner_kind=owner_kind,
    ) as lease:
        run_terminalizer.publish_running_status(layout.root, lease=lease, updated_at=_now())
        token = _CURRENT_OWNER.set(lease)
        try:
            yield lease
        finally:
            _CURRENT_OWNER.reset(token)


@contextmanager
def invocation(
    shot_folder: str | Path,
    command: str,
    *,
    shot_id: str | None = None,
    parameters: dict[str, Any] | None = None,
    arguments: list[str] | None = None,
) -> Iterator[run_artifacts.RunLayout]:
    """Own one direct public invocation, or publish a typed stop inside an inherited run."""

    inherited = run_artifacts.active(shot_folder)
    if inherited is not None:
        try:
            yield inherited
        except RunCancellation:
            raise
        except BaseException as exc:
            _state, code, cause, _detail = run_artifacts.terminal_record(exc)
            # Carry this exact publication with the same in-process exception.
            # Reclassifying it at the root's command creates a different immutable
            # stop and prevents terminalization. Never adopt a stop merely because
            # a file exists, and never replace the exception (or its SDK usage).
            exc.stop_envelope = run_artifacts.publish_exception_stop(
                inherited, command, exc, code=code, terminal_cause=cause,
            )
            raise
        return
    shot = Path(shot_folder).expanduser().resolve()
    layout = run_artifacts.create(
        shot,
        os.environ.get("VFXH_RUN_ID") or RUN_ID,
        command=command,
        dispatch_kind="direct",
        shot_id=shot_id,
        arguments=arguments,
        parameters=parameters,
    )
    with owned_root_run(layout, command=command, owner_kind="direct") as lease, signal_intent_scope():
        try:
            yield layout
        except RunCancellation as cancellation:
            raise terminalize_cancellation(shot, layout, lease, cancellation) from None
        except BaseException as exc:
            terminalize_failure(layout, lease, command, exc)
            raise
        else:
            _terminalize_success(layout, lease, command)


__all__ = [
    "SIGNAL_INTERRUPTION_KINDS",
    "RunCancellation",
    "RunInterrupted",
    "RunOwnerFenceLease",
    "SignalIntent",
    "invocation",
    "owned_root_run",
    "signal_intent_scope",
    "terminalize_cancellation",
]
