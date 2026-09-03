"""Durable state for the receipt-backed run controller (ADR-0010).

Every dispatch the controller makes leaves three key-addressed immutable records under the
shot's cross-run state: the rematerialization commit that proves the authority transition,
the independent postcondition evaluation, and the prior-dispatch attempt that later runs
consult for convergence. Records publish once, read back exactly, and never change.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from vfx_harness.domain.controller_commits import RematerializationCommit
from vfx_harness.domain.stop_envelopes import PriorDispatchAttempt
from vfx_harness.domain.stop_transaction_state import StopEvidenceRef
from vfx_harness.domain.stop_transactions import PostconditionEvaluation
from vfx_harness.observability.run_artifacts import shot_state_dir
from vfx_harness.orchestration import immutable_records

ROOT = Path("recovery") / "controller"
COMMITS = ROOT / "commits"
EVALUATIONS = ROOT / "evaluations"
ATTEMPTS = ROOT / "attempts"
LOCKS = ROOT / "locks"


def _require_key(value: str) -> str:
    return immutable_records.require_key(value, where="controller idempotency key")


def _paths(shot_folder: str | Path) -> tuple[Path, Path]:
    requested = Path(shot_folder)
    if not requested.is_dir() or requested.is_symlink():
        raise ValueError(f"controller state shot root {requested} must be an existing real directory")
    shot = requested.resolve()
    return shot, shot_state_dir(shot) / ROOT


def _state_root(shot_folder: str | Path) -> tuple[Path, Path]:
    shot, root = _paths(shot_folder)
    state = shot_state_dir(shot)
    immutable_records.ensure_directories(
        state,
        state / "recovery",
        root,
        state / COMMITS,
        state / EVALUATIONS,
        state / ATTEMPTS,
        state / LOCKS,
    )
    return shot, root


@contextmanager
def controller_lock(shot_folder: str | Path, idempotency_key: str) -> Iterator[None]:
    """Serialize one controller transaction key without sharing the receipt-store lock."""

    _require_key(idempotency_key)
    _shot, root = _state_root(shot_folder)
    with immutable_records.key_lock(root / "locks" / f"{idempotency_key}.lock"):
        yield


def read_commit(shot_folder: str | Path, idempotency_key: str) -> RematerializationCommit | None:
    _require_key(idempotency_key)
    shot, root = _paths(shot_folder)
    path = root / "commits" / f"{idempotency_key}.json"
    if not path.exists():
        return None
    commit = RematerializationCommit.from_dict(
        immutable_records.read_json(path, "rematerialization commit"),
        "rematerialization commit",
    )
    if commit.idempotency_key != idempotency_key:
        raise ValueError("rematerialization commit belongs to another idempotency key")
    if path.read_bytes() != immutable_records.canonical_bytes(commit.as_dict()):
        raise ValueError("rematerialization commit bytes are not canonical")
    path.resolve().relative_to(shot)
    return commit


def commit_evidence_ref(shot_folder: str | Path, commit: RematerializationCommit) -> StopEvidenceRef:
    """Reference a commit only after exact immutable read-back by its deterministic key."""

    if not isinstance(commit, RematerializationCommit):
        raise ValueError("rematerialization commit must be typed")
    shot, root = _paths(shot_folder)
    if read_commit(shot, commit.idempotency_key) != commit:
        raise ValueError("rematerialization commit evidence requires the exact stored commit")
    return immutable_records.evidence_ref(
        shot,
        root / "commits" / f"{commit.idempotency_key}.json",
        kind="authority_record",
        schema=commit.SCHEMA,
        digest=commit.digest,
    )


def publish_commit(shot_folder: str | Path, commit: RematerializationCommit) -> StopEvidenceRef:
    if not isinstance(commit, RematerializationCommit):
        raise ValueError("rematerialization commit must be typed")
    shot, root = _state_root(shot_folder)
    path = root / "commits" / f"{commit.idempotency_key}.json"
    raw = immutable_records.canonical_bytes(commit.as_dict())
    immutable_records.publish_immutable(path, raw)
    if read_commit(shot, commit.idempotency_key) != commit or path.read_bytes() != raw:
        raise ValueError("published rematerialization commit failed exact read-back")
    return commit_evidence_ref(shot, commit)


def read_evaluation(shot_folder: str | Path, idempotency_key: str) -> PostconditionEvaluation | None:
    _require_key(idempotency_key)
    _shot, root = _paths(shot_folder)
    path = root / "evaluations" / f"{idempotency_key}.json"
    if not path.exists():
        return None
    evaluation = PostconditionEvaluation.from_dict(
        immutable_records.read_json(path, "controller postcondition evaluation"),
        "controller postcondition evaluation",
    )
    if evaluation.idempotency_key != idempotency_key:
        raise ValueError("controller postcondition evaluation belongs to another idempotency key")
    if path.read_bytes() != immutable_records.canonical_bytes(evaluation.as_dict()):
        raise ValueError("controller postcondition evaluation bytes are not canonical")
    return evaluation


def publish_evaluation(shot_folder: str | Path, evaluation: PostconditionEvaluation) -> Path:
    if not isinstance(evaluation, PostconditionEvaluation):
        raise ValueError("controller postcondition evaluation must be typed")
    shot, root = _state_root(shot_folder)
    path = root / "evaluations" / f"{evaluation.idempotency_key}.json"
    raw = immutable_records.canonical_bytes(evaluation.as_dict())
    immutable_records.publish_immutable(path, raw)
    if read_evaluation(shot, evaluation.idempotency_key) != evaluation or path.read_bytes() != raw:
        raise ValueError("published controller postcondition evaluation failed exact read-back")
    return path


def publish_attempt(shot_folder: str | Path, attempt: PriorDispatchAttempt) -> Path:
    """Record one receipt-backed dispatch so later runs converge instead of repeating it."""

    if not isinstance(attempt, PriorDispatchAttempt):
        raise ValueError("controller dispatch attempt must be typed")
    _shot, root = _state_root(shot_folder)
    path = root / "attempts" / f"{attempt.evaluation.idempotency_key}.json"
    raw = immutable_records.canonical_bytes(attempt.as_dict())
    immutable_records.publish_immutable(path, raw)
    observed = PriorDispatchAttempt.from_dict(
        immutable_records.read_json(path, "controller dispatch attempt"),
        "controller dispatch attempt",
    )
    if observed != attempt or path.read_bytes() != raw:
        raise ValueError("published controller dispatch attempt failed exact read-back")
    return path


def read_attempts(shot_folder: str | Path) -> tuple[PriorDispatchAttempt, ...]:
    """Every prior receipt-backed dispatch of this shot, oldest key first."""

    _shot, root = _paths(shot_folder)
    directory = root / "attempts"
    if not directory.is_dir():
        return ()
    attempts: list[PriorDispatchAttempt] = []
    for path in sorted(directory.glob("*.json")):
        attempt = PriorDispatchAttempt.from_dict(
            immutable_records.read_json(path, f"controller dispatch attempt {path.name}"),
            f"controller dispatch attempt {path.name}",
        )
        if path.stem != attempt.evaluation.idempotency_key:
            raise ValueError(f"controller dispatch attempt {path.name} is filed under another key")
        attempts.append(attempt)
    return tuple(attempts)


__all__ = [
    "ATTEMPTS",
    "COMMITS",
    "EVALUATIONS",
    "LOCKS",
    "ROOT",
    "commit_evidence_ref",
    "controller_lock",
    "publish_attempt",
    "publish_commit",
    "publish_evaluation",
    "read_attempts",
    "read_commit",
    "read_evaluation",
]
