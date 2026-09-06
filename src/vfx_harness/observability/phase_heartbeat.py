"""How much of a model stream's idle deadline is spent, readable without process forensics.

Three sessions watching three shots each invented a different liveness heuristic, and two of
the three answers were wrong. A ten-minute assistant turn and a dead session are byte
identical in every artifact under ``runs/``: the transcript records discrete SDK events, so a
long turn writes nothing, and the console writes even less. The one method that worked was
reading ``/proc`` CPU counters of the SDK child, which no operator should need and which no
run artifact exposes (caesar run 20260904T143311Z-c0f282, HIR-0200).

The deadline already exists and is enforced; what was missing is its remaining budget. This
leaf keeps one small record per run, rewritten as events arrive, so any reader can compute
seconds since the last event and compare it with the deadline that will fire.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = "vfx-harness.phase-heartbeat/v1"
NAME = "phase-heartbeat.json"
_WRITE_INTERVAL_SECONDS = 1.0
_STATE: dict[str, object] = {}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _write(force: bool = False) -> None:
    path = _STATE.get("path")
    if not path:
        return
    monotonic = time.monotonic()
    if not force and monotonic - float(_STATE.get("written_at") or 0.0) < _WRITE_INTERVAL_SECONDS:
        return
    _STATE["written_at"] = monotonic
    record = {
        "schema": SCHEMA,
        "stage": _STATE.get("stage"),
        "label": _STATE.get("label"),
        "state": _STATE.get("state"),
        "deadline_seconds": _STATE.get("deadline_seconds"),
        "events": _STATE.get("events"),
        "last_event_kind": _STATE.get("last_event_kind"),
        "last_event_at": _STATE.get("last_event_at"),
        "started_at": _STATE.get("started_at"),
    }
    target = Path(str(path))
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.tmp.{os.getpid()}")
        temporary.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, target)
    except OSError:
        # Observability never breaks the stage it observes.
        _STATE["path"] = None


def begin(path: str | Path | None, *, stage: str, label: str | None, deadline_seconds: int | None) -> None:
    """A model phase is opening: record its deadline and start the clock."""

    _STATE.clear()
    # One clock read. Before any event, "when the phase started" and "when it last saw
    # something" are the same instant, and a reader computing remaining budget from
    # last_event_at must get the start, not a millisecond after it (HIR-0239).
    started = _now()
    _STATE.update({
        "path": None if path is None else str(path),
        "stage": stage,
        "label": label,
        "state": "waiting",
        "deadline_seconds": int(deadline_seconds) if deadline_seconds else None,
        "events": 0,
        "last_event_kind": None,
        "started_at": started,
        "last_event_at": started,
        "written_at": 0.0,
    })
    _write(force=True)


def event(kind: str) -> None:
    """One SDK event arrived; the deadline's clock restarts from here."""

    if not _STATE:
        return
    _STATE["events"] = int(_STATE.get("events") or 0) + 1
    _STATE["last_event_kind"] = kind
    _STATE["last_event_at"] = _now()
    _STATE["state"] = "waiting"
    _write()


def end(state: str = "closed") -> None:
    """The phase finished; readers should stop measuring its deadline."""

    if not _STATE:
        return
    _STATE["state"] = state
    _write(force=True)
    _STATE.clear()


def snapshot() -> dict[str, object]:
    """The live record, for tests and in-process readers."""

    return {key: value for key, value in _STATE.items() if key not in {"path", "written_at"}}


__all__ = ["NAME", "SCHEMA", "begin", "end", "event", "snapshot"]
