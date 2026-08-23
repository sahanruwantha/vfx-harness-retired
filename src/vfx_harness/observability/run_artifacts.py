"""Canonical, machine-readable storage for one harness invocation.

The shot root contains authored inputs and the currently accepted build. Generated output
belongs to a run. Keeping that distinction explicit prevents transcripts, renders, temporary
Blender files, checkpoints, and reports from different attempts from becoming one directory
that an agent has to reverse-engineer.

`VFXH_RUN_DIR` is inherited by stage subprocesses. A direct stage invocation creates its own
structured run instead of writing ad hoc shot-root output.
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ENV = "VFXH_RUN_DIR"
SCHEMA = "vfx-harness.run/v1"
LATEST_SCHEMA = "vfx-harness.latest-run/v1"
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_RESERVED_STATUS_FIELDS = {"schema", "run_id", "state", "updated_at", "exit_code", "detail"}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _validate_run_id(run_id: str) -> str:
    value = str(run_id).strip()
    if not _SAFE_ID.fullmatch(value) or value in {".", "..", "latest"}:
        raise ValueError(f"invalid run id: {run_id!r}")
    return value


@dataclass(frozen=True, slots=True)
class RunLayout:
    shot: Path
    run_id: str
    root: Path
    # A stage may learn terminal authority before the invocation context exits. Keeping
    # these structured fields beside the layout lets the context publish them atomically
    # with status.json/summary.json instead of reducing them to an exception string.
    terminal_metadata: dict[str, Any] = field(default_factory=dict, compare=False)

    @property
    def manifest(self) -> Path:
        return self.root / "manifest.json"

    @property
    def status(self) -> Path:
        return self.root / "status.json"

    @property
    def inventory(self) -> Path:
        return self.root / "artifacts.json"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def reports(self) -> Path:
        return self.root / "reports"

    @property
    def evidence(self) -> Path:
        return self.root / "evidence"

    @property
    def checkpoints(self) -> Path:
        return self.root / "checkpoints"

    @property
    def scratch(self) -> Path:
        return self.root / "scratch"

    @property
    def deliverables(self) -> Path:
        return self.root / "deliverables"

    def relative(self, path: str | Path) -> str:
        return Path(path).resolve().relative_to(self.shot).as_posix()

    def set_status(self, state: str, *, exit_code: int | None = None,
                   detail: str | None = None, metadata: dict[str, Any] | None = None) -> None:
        rec: dict[str, Any] = {
            "schema": SCHEMA,
            "run_id": self.run_id,
            "state": state,
            "updated_at": _now(),
        }
        if exit_code is not None:
            rec["exit_code"] = int(exit_code)
        if detail:
            rec["detail"] = str(detail)[:1000]
        if metadata:
            rec.update({key: value for key, value in metadata.items() if key not in _RESERVED_STATUS_FIELDS})
        _atomic_json(self.status, rec)
        _write_latest(self, state=state)

    def write_inventory(self) -> Path:
        """Publish a compact file catalog so readers never have to infer artifact roles."""
        rows = []
        for path in sorted(p for p in self.root.rglob("*") if p.is_file()):
            if path == self.inventory or ".tmp." in path.name:
                continue
            rel = path.relative_to(self.root).as_posix()
            top = rel.split("/", 1)[0]
            rows.append({
                "path": rel,
                "category": top if top in {
                    "logs", "reports", "evidence", "checkpoints", "scratch", "deliverables"
                } else "run-metadata",
                "bytes": path.stat().st_size,
                "media_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            })
        _atomic_json(self.inventory, {
            "schema": "vfx-harness.artifact-index/v1",
            "run_id": self.run_id,
            "generated_at": _now(),
            "artifacts": rows,
        })
        return self.inventory

    def write_summary(self, value: dict[str, Any]) -> Path:
        out = self.reports / "summary.json"
        _atomic_json(out, value)
        return out

    def write_report(self, name: str, value: dict[str, Any]) -> Path:
        """Publish one named structured report inside this invocation."""
        if not _SAFE_ID.fullmatch(name):
            raise ValueError(f"invalid report name: {name!r}")
        out = self.reports / f"{name}.json"
        _atomic_json(out, value)
        return out


def create(shot_folder: str | Path, run_id: str, *, shot_id: str | None = None,
           argv: list[str] | None = None, parameters: dict[str, Any] | None = None) -> RunLayout:
    shot = Path(shot_folder).expanduser().resolve()
    rid = _validate_run_id(run_id)
    layout = RunLayout(shot=shot, run_id=rid, root=shot / "runs" / rid)
    for folder in (
        layout.logs,
        layout.reports / "layers",
        layout.evidence / "renders",
        layout.evidence / "comparisons",
        layout.checkpoints / "blender",
        layout.checkpoints / "scripts",
        layout.scratch / "blender",
        layout.deliverables,
    ):
        folder.mkdir(parents=True, exist_ok=True)
    os.environ[ENV] = str(layout.root)
    manifest = {
        "schema": SCHEMA,
        "run_id": rid,
        "shot_id": shot_id or shot.name,
        "started_at": _now(),
        "invocation": {
            "argv": list(argv if argv is not None else sys.argv),
            "parameters": parameters or {},
        },
        "layout": {
            "status": "status.json",
            "artifact_index": "artifacts.json",
            "logs": "logs/",
            "reports": "reports/",
            "evidence": "evidence/",
            "checkpoints": "checkpoints/",
            "scratch": "scratch/",
            "deliverables": "deliverables/",
        },
        "authority": {
            "authored_inputs": "../../brief.md and ../../refs/",
            "published_plan": "../../plans/current.json when present",
            "plan_authoring_workspace": "scratch/plan-workspace/ for global plan invocations",
            "selected_plan_consumers": (
                "../../plans/current.json; pointer-less archived fixtures only use "
                "compatibility reads"
            ),
            "accepted_build": "../../build/ and ../../shot.json",
            "generated_output": "this directory",
        },
        "reader_entrypoint": "manifest.json",
    }
    _atomic_json(layout.manifest, manifest)
    layout.set_status("running")
    layout.write_inventory()
    return layout


def ensure(shot_folder: str | Path, run_id: str | None = None, *,
           shot_id: str | None = None, command: str = "direct-stage") -> RunLayout:
    """Return the inherited run or create a strict structured run for a direct stage."""
    current = active(shot_folder)
    if current:
        return current
    if run_id is None:
        from vfx_harness.observability.runid import RUN_ID
        run_id = RUN_ID
    return create(
        shot_folder,
        run_id,
        shot_id=shot_id,
        argv=list(sys.argv),
        parameters={"command": command, "direct": True},
    )


def _terminal_record(exc: BaseException) -> tuple[str, int, str, str]:
    """Classify a failed invocation without reducing distinct stops to exit code 1."""
    if isinstance(exc, KeyboardInterrupt):
        return "interrupted", 130, "interrupted", "interrupted by operator"

    code = exc.code if isinstance(exc, SystemExit) and isinstance(exc.code, int) else 1
    cause = str(getattr(exc, "terminal_cause", "") or "")
    metadata = getattr(exc, "run_metadata", {})
    outcome = str(metadata.get("outcome") or "") if isinstance(metadata, dict) else ""
    if not cause and outcome:
        cause = {
            "stalled": "gate_stalled",
            "budget": "plan_budget_exhausted",
        }.get(outcome, "gate_rejected")
    if not cause:
        cause = "requested_exit" if isinstance(exc, SystemExit) else "process_error"
    detail = str(exc).strip() or {
        "max_turns_exhausted": "model turn budget exhausted",
        "usage_limit": "model usage limit reached",
        "session_stalled": "model session produced no publishable artifact",
        "gate_stalled": "plan gate stopped improving",
        "plan_budget_exhausted": "plan repair budget exhausted",
    }.get(cause, exc.__class__.__name__)
    return "failed", int(code), cause, detail


@contextmanager
def invocation(shot_folder: str | Path, command: str, *,
               shot_id: str | None = None, parameters: dict[str, Any] | None = None):
    """Give a direct CLI stage a terminal run record; inherited driver runs stay open."""
    inherited = active(shot_folder)
    layout = inherited or create(
        shot_folder,
        os.environ.get("VFXH_RUN_ID") or _direct_run_id(),
        shot_id=shot_id,
        parameters={"command": command, "direct": True, **(parameters or {})},
    )
    try:
        yield layout
    except BaseException as exc:
        if inherited is None:
            state, code, terminal_cause, detail = _terminal_record(exc)
            metadata = {
                key: value
                for key, value in {
                    **layout.terminal_metadata,
                    **getattr(exc, "run_metadata", {}),
                    "terminal_cause": terminal_cause,
                }.items()
                if key not in _RESERVED_STATUS_FIELDS
            }
            layout.set_status(state, exit_code=code, detail=detail, metadata=metadata)
            layout.write_summary({
                "schema": "vfx-harness.run-summary/v1",
                "run_id": layout.run_id,
                "command": command,
                "state": state,
                "exit_code": code,
                "detail": detail[:1000],
                **metadata,
            })
            layout.write_inventory()
        raise
    else:
        if inherited is None:
            metadata = {
                key: value
                for key, value in layout.terminal_metadata.items()
                if key not in _RESERVED_STATUS_FIELDS
            }
            layout.set_status("passed", exit_code=0, metadata=metadata)
            layout.write_summary({
                "schema": "vfx-harness.run-summary/v1",
                "run_id": layout.run_id,
                "command": command,
                "state": "passed",
                "exit_code": 0,
                **metadata,
            })
            layout.write_inventory()


def _direct_run_id() -> str:
    from vfx_harness.observability.runid import RUN_ID
    return RUN_ID


def active(shot_folder: str | Path) -> RunLayout | None:
    configured = os.environ.get(ENV)
    if not configured:
        return None
    shot = Path(shot_folder).expanduser().resolve()
    root = Path(configured).expanduser().resolve()
    runs = shot / "runs"
    try:
        rel = root.relative_to(runs)
    except ValueError:
        return None
    if len(rel.parts) != 1:
        return None
    return RunLayout(shot=shot, run_id=_validate_run_id(rel.name), root=root)


def latest(shot_folder: str | Path) -> RunLayout | None:
    shot = Path(shot_folder).expanduser().resolve()
    pointer = shot / "runs" / "latest.json"
    try:
        rec = json.loads(pointer.read_text(encoding="utf-8"))
        rid = _validate_run_id(rec["run_id"])
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    root = shot / "runs" / rid
    return RunLayout(shot=shot, run_id=rid, root=root) if root.is_dir() else None


def select(shot_folder: str | Path, run_id: str | None = None) -> RunLayout | None:
    shot = Path(shot_folder).expanduser().resolve()
    if run_id:
        rid = _validate_run_id(run_id)
        root = shot / "runs" / rid
        return RunLayout(shot=shot, run_id=rid, root=root) if root.is_dir() else None
    return active(shot) or latest(shot)


def list_runs(shot_folder: str | Path) -> list[dict[str, Any]]:
    shot = Path(shot_folder).expanduser().resolve()
    out = []
    runs = shot / "runs"
    if not runs.is_dir():
        return out
    for root in sorted((p for p in runs.iterdir() if p.is_dir()), reverse=True):
        try:
            manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            manifest = {}
        try:
            status = json.loads((root / "status.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            status = {}
        out.append({
            "run_id": root.name,
            "started_at": manifest.get("started_at"),
            "state": status.get("state", "unknown"),
            "exit_code": status.get("exit_code"),
            "manifest": str(root / "manifest.json"),
        })
    return out


def logs_dir(shot_folder: str | Path) -> Path:
    return ensure(shot_folder).logs


def reports_dir(shot_folder: str | Path) -> Path:
    return ensure(shot_folder).reports


def renders_dir(shot_folder: str | Path) -> Path:
    return ensure(shot_folder).evidence / "renders"


def readable_renders_dir(shot_folder: str | Path) -> Path:
    """Return the selected run's renders; mixed shot-wide output is not supported."""
    layout = select(shot_folder)
    if not layout:
        raise FileNotFoundError(
            f"no structured run under {Path(shot_folder) / 'runs'}; run the shot first"
        )
    return layout.evidence / "renders"


def checkpoints_dir(shot_folder: str | Path) -> Path:
    return ensure(shot_folder).checkpoints


def scratch_dir(shot_folder: str | Path) -> Path:
    return ensure(shot_folder).scratch


def deliverables_dir(shot_folder: str | Path) -> Path:
    return ensure(shot_folder).deliverables


def shot_state_dir(shot_folder: str | Path) -> Path:
    """Durable cross-run state, separate from authored inputs and generated output."""
    return Path(shot_folder) / "state"


def _write_latest(layout: RunLayout, *, state: str) -> None:
    _atomic_json(layout.shot / "runs" / "latest.json", {
        "schema": LATEST_SCHEMA,
        "run_id": layout.run_id,
        "path": layout.run_id,
        "state": state,
        "updated_at": _now(),
    })
