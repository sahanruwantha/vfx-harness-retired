"""Exact attempt scratch-candidate paths and unlocked mutation helpers."""

from __future__ import annotations

from pathlib import Path

from vfx_harness.agents.builder.attempt_guard import UnitAttemptGuard
from vfx_harness.orchestration.authority_selection_transaction import (
    durable_replace_file_bytes,
)
from vfx_harness.orchestration.plan_bundle_integrity import read_real_file


def exact_candidate_script_path(
    shot_folder: str | Path,
    attempt_guard: UnitAttemptGuard,
) -> Path:
    """Return the sole scratch script path owned by one exact attempt."""

    root = Path(shot_folder).resolve()
    return (
        root
        / "runs"
        / attempt_guard.claim.run_id
        / "scratch"
        / "unit-candidates"
        / f"{attempt_guard.claim.claim_id}.py"
    )


def require_exact_candidate_script_path(
    shot_folder: str | Path,
    candidate_path: str | Path,
    attempt_guard: UnitAttemptGuard,
) -> Path:
    """Reject any candidate path not derived from the guard's run and claim."""

    root = Path(shot_folder).resolve()
    candidate = Path(candidate_path)
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = candidate.absolute()
    expected = exact_candidate_script_path(root, attempt_guard)
    if candidate != expected:
        raise ValueError(
            "active scratch candidate must be the exact run/claim-bound path: "
            f"expected {expected}, found {candidate}"
        )
    return candidate


def write_scratch_candidate(
    shot_folder: str | Path,
    candidate_path: str | Path,
    content: str,
    attempt_guard: UnitAttemptGuard,
) -> None:
    """Write inert scratch bytes without holding selection or unit-state locks."""

    candidate = require_exact_candidate_script_path(
        shot_folder,
        candidate_path,
        attempt_guard,
    )
    attempt_guard.check("start finalized scratch candidate write")
    durable_replace_file_bytes(
        shot_folder,
        candidate,
        content.encode("utf-8"),
    )
    attempt_guard.check("finish finalized scratch candidate write")


def edit_scratch_candidate(
    shot_folder: str | Path,
    candidate_path: str | Path,
    old: str,
    new: str,
    *,
    replace_all: bool,
    attempt_guard: UnitAttemptGuard,
) -> int:
    """Edit inert scratch bytes with exact checks before and after unlocked I/O."""

    root = Path(shot_folder).resolve()
    candidate = require_exact_candidate_script_path(root, candidate_path, attempt_guard)
    attempt_guard.check("start scratch candidate edit")
    raw = read_real_file(root, candidate, "active scratch candidate")
    text = raw.decode("utf-8")
    count = text.count(old)
    if count == 0:
        raise ValueError("old_string was not found in the active scratch candidate")
    if count > 1 and not replace_all:
        raise ValueError(
            f"old_string occurs {count} times; set replace_all=true or provide a unique span"
        )
    updated = text.replace(old, new, -1 if replace_all else 1)
    durable_replace_file_bytes(
        root,
        candidate,
        updated.encode("utf-8"),
    )
    attempt_guard.check("finish scratch candidate edit")
    return count if replace_all else 1
