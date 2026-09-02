"""Owned v2 run fixtures for tests that need terminal run statuses (HIR-0172).

A terminal status is selected only by the root owner holding its fence, so tests that
used to write a status directly own a prepared run through the real boundary and
terminalize it through the real terminalizer.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from vfx_harness.domain.run_status import RUN_SUMMARY_SCHEMA, RunStatusV2
from vfx_harness.domain.stop_envelopes import StopEnvelope
from vfx_harness.observability import run_artifacts
from vfx_harness.observability.run_owner_fence import RunOwnerFenceLease
from vfx_harness.orchestration import run_owner_boundary, run_terminalizer


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


@contextmanager
def owned_run(
    shot: str | Path,
    run_id: str,
    *,
    command: str = "plan",
    dispatch_kind: str = "direct",
    shot_id: str | None = None,
    parameters: dict[str, Any] | None = None,
) -> Iterator[tuple[run_artifacts.RunLayout, RunOwnerFenceLease]]:
    """Create a prepared run, own it, and publish its running status for the block."""

    layout = run_artifacts.create(
        shot,
        run_id,
        command=command,
        dispatch_kind=dispatch_kind,
        shot_id=shot_id,
        parameters=parameters,
    )
    owner_kind = "driver" if dispatch_kind == "driver" else "direct"
    with run_owner_boundary.owned_root_run(layout, command=command, owner_kind=owner_kind) as lease:
        yield layout, lease


def fail_run(
    layout: run_artifacts.RunLayout,
    lease: RunOwnerFenceLease,
    envelope: StopEnvelope,
    *,
    exit_code: int,
    detail: str | None = None,
) -> RunStatusV2:
    """Publish one typed stop envelope and select it as the failed terminal status."""

    layout.write_stop_envelope(envelope)
    return run_terminalizer.publish_failed_status(
        layout.root,
        lease=lease,
        envelope=envelope,
        exit_code=exit_code,
        updated_at=now(),
        detail=detail,
    )


def pass_run(
    layout: run_artifacts.RunLayout,
    lease: RunOwnerFenceLease,
    *,
    command: str = "plan",
    state: str = "passed",
    extra: dict[str, Any] | None = None,
) -> RunStatusV2:
    """Publish the run summary and select it as the passed (or dry-run) terminal status."""

    summary = {
        "schema": RUN_SUMMARY_SCHEMA,
        "run_id": layout.run_id,
        "command": command,
        "state": state,
        "exit_code": 0,
        **(extra or {}),
    }
    layout.write_summary(summary)
    return run_terminalizer.publish_passed_status(
        layout.root,
        lease=lease,
        summary=summary,
        updated_at=now(),
        state=state,
    )


__all__ = ["fail_run", "now", "owned_run", "pass_run"]
