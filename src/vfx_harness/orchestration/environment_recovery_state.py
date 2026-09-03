"""Durable state records for receipt-backed external environment recovery."""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from pathlib import Path

from vfx_harness.domain.environment_recovery import EnvironmentRecoveryCommit
from vfx_harness.domain.environment_results import EnvironmentResult
from vfx_harness.domain.stop_transaction_state import StopEvidenceRef
from vfx_harness.domain.stop_transactions import PostconditionEvaluation
from vfx_harness.observability.run_artifacts import shot_state_dir
from vfx_harness.orchestration import immutable_records

ROOT = Path("recovery") / "environment"
RESULTS = ROOT / "results"
EVALUATIONS = ROOT / "evaluations"
LOCKS = ROOT / "locks"


def _require_key(value: str) -> str:
    return immutable_records.require_key(value, where="environment recovery idempotency key")

def _paths(shot_folder: str | Path) -> tuple[Path, Path]:
    requested_shot = Path(shot_folder)
    if not requested_shot.is_dir() or requested_shot.is_symlink():
        raise ValueError(
            f"environment recovery shot root {requested_shot} must be an existing real directory"
        )
    shot = requested_shot.resolve()
    root = shot_state_dir(shot) / ROOT
    for directory in (
        shot_state_dir(shot),
        shot_state_dir(shot) / "recovery",
        root,
        shot_state_dir(shot) / RESULTS,
        shot_state_dir(shot) / EVALUATIONS,
        shot_state_dir(shot) / LOCKS,
    ):
        if directory.is_symlink():
            raise ValueError(f"environment recovery state directory {directory} must not be a symlink")
        if directory.exists() and not directory.is_dir():
            raise ValueError(f"environment recovery state path {directory} must be a directory")
    return shot, root


def _state_root(shot_folder: str | Path) -> tuple[Path, Path]:
    shot, root = _paths(shot_folder)
    for directory in (
        shot_state_dir(shot),
        shot_state_dir(shot) / "recovery",
        root,
        shot_state_dir(shot) / RESULTS,
        shot_state_dir(shot) / EVALUATIONS,
        shot_state_dir(shot) / LOCKS,
    ):
        if directory.is_symlink():
            raise ValueError(f"environment recovery state directory {directory} must not be a symlink")
        existed = directory.exists()
        directory.mkdir(exist_ok=True)
        if not directory.is_dir():
            raise ValueError(f"environment recovery state path {directory} must be a directory")
        if not existed:
            immutable_records.fsync_directory(directory.parent)
        immutable_records.fsync_directory(directory)
    return shot, root


@contextmanager
def environment_recovery_lock(shot_folder: str | Path, idempotency_key: str):
    """Serialize one environment adapter without sharing the receipt-store lock."""

    _require_key(idempotency_key)
    _shot, root = _state_root(shot_folder)
    with immutable_records.key_lock(root / "locks" / f"{idempotency_key}.lock"):
        yield


def publish_environment_result(
    shot_folder: str | Path,
    result: EnvironmentResult,
) -> StopEvidenceRef:
    if not isinstance(result, EnvironmentResult):
        raise ValueError("environment recovery result must be an EnvironmentResult")
    shot, root = _state_root(shot_folder)
    raw = immutable_records.canonical_bytes(result.as_dict())
    path = root / "results" / f"{result.digest}.json"
    immutable_records.publish_immutable(path, raw)
    observed = EnvironmentResult.from_dict(
        immutable_records.read_json(path, "environment recovery result"),
        "environment recovery result",
    )
    if observed != result or path.read_bytes() != raw:
        raise ValueError("published environment recovery result failed exact read-back")
    return immutable_records.evidence_ref(
        shot,
        path,
        kind="environment_result",
        schema=result.SCHEMA,
        digest=result.digest,
    )


def read_environment_result(
    shot_folder: str | Path,
    evidence: StopEvidenceRef,
) -> EnvironmentResult:
    if (
        not isinstance(evidence, StopEvidenceRef)
        or evidence.kind != "environment_result"
        or evidence.record_schema != EnvironmentResult.SCHEMA
        or evidence.record_digest is None
    ):
        raise ValueError("environment recovery evidence must cite an exact environment result")
    shot, root = _paths(shot_folder)
    expected = root / "results" / f"{evidence.record_digest}.json"
    expected_locator = expected.relative_to(shot).as_posix()
    if evidence.locator != expected_locator:
        raise ValueError("environment result evidence locator is not its content-addressed state path")
    path = expected
    value = immutable_records.read_json(path, "environment recovery result")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != evidence.sha256:
        raise ValueError("environment result evidence SHA-256 mismatch")
    result = EnvironmentResult.from_dict(value, "environment recovery result")
    if result.digest != evidence.record_digest or raw != immutable_records.canonical_bytes(result.as_dict()):
        raise ValueError("environment result evidence identity or canonical bytes mismatch")
    return result


def environment_commit_path(shot_folder: str | Path, idempotency_key: str) -> Path:
    _require_key(idempotency_key)
    return shot_state_dir(shot_folder) / ROOT / f"{idempotency_key}.json"


def read_environment_commit(
    shot_folder: str | Path,
    idempotency_key: str,
) -> EnvironmentRecoveryCommit | None:
    _require_key(idempotency_key)
    shot, root = _paths(shot_folder)
    path = root / f"{idempotency_key}.json"
    if not path.exists():
        return None
    commit = EnvironmentRecoveryCommit.from_dict(
        immutable_records.read_json(path, "environment recovery commit"),
        "environment recovery commit",
    )
    if commit.idempotency_key != idempotency_key:
        raise ValueError("environment recovery commit belongs to another idempotency key")
    if path.read_bytes() != immutable_records.canonical_bytes(commit.as_dict()):
        raise ValueError("environment recovery commit bytes are not canonical")
    path.resolve().relative_to(shot)
    return commit


def publish_environment_commit(
    shot_folder: str | Path,
    commit: EnvironmentRecoveryCommit,
) -> StopEvidenceRef:
    if not isinstance(commit, EnvironmentRecoveryCommit):
        raise ValueError("environment recovery commit must be typed")
    shot, root = _state_root(shot_folder)
    path = root / f"{commit.idempotency_key}.json"
    raw = immutable_records.canonical_bytes(commit.as_dict())
    immutable_records.publish_immutable(path, raw)
    observed = read_environment_commit(shot, commit.idempotency_key)
    if observed != commit or path.read_bytes() != raw:
        raise ValueError("published environment recovery commit failed exact read-back")
    return environment_commit_evidence_ref(shot, commit)


def environment_commit_evidence_ref(
    shot_folder: str | Path,
    commit: EnvironmentRecoveryCommit,
) -> StopEvidenceRef:
    """Reference a commit only after exact immutable read-back by its deterministic key."""

    if not isinstance(commit, EnvironmentRecoveryCommit):
        raise ValueError("environment recovery commit must be typed")
    shot, root = _paths(shot_folder)
    observed = read_environment_commit(shot, commit.idempotency_key)
    if observed != commit:
        raise ValueError("environment recovery commit evidence requires the exact stored commit")
    path = root / f"{commit.idempotency_key}.json"
    return immutable_records.evidence_ref(
        shot,
        path,
        kind="authority_record",
        schema=commit.SCHEMA,
        digest=commit.digest,
    )


def read_postcondition_evaluation(
    shot_folder: str | Path,
    idempotency_key: str,
) -> PostconditionEvaluation | None:
    _require_key(idempotency_key)
    _shot, root = _paths(shot_folder)
    path = root / "evaluations" / f"{idempotency_key}.json"
    if not path.exists():
        return None
    evaluation = PostconditionEvaluation.from_dict(
        immutable_records.read_json(path, "environment postcondition evaluation"),
        "environment postcondition evaluation",
    )
    if evaluation.idempotency_key != idempotency_key:
        raise ValueError("postcondition evaluation belongs to another idempotency key")
    if path.read_bytes() != immutable_records.canonical_bytes(evaluation.as_dict()):
        raise ValueError("postcondition evaluation bytes are not canonical")
    return evaluation


def publish_postcondition_evaluation(
    shot_folder: str | Path,
    evaluation: PostconditionEvaluation,
) -> StopEvidenceRef:
    if not isinstance(evaluation, PostconditionEvaluation):
        raise ValueError("environment postcondition evaluation must be typed")
    shot, root = _state_root(shot_folder)
    path = root / "evaluations" / f"{evaluation.idempotency_key}.json"
    raw = immutable_records.canonical_bytes(evaluation.as_dict())
    immutable_records.publish_immutable(path, raw)
    observed = read_postcondition_evaluation(shot, evaluation.idempotency_key)
    if observed != evaluation or path.read_bytes() != raw:
        raise ValueError("published environment postcondition evaluation failed exact read-back")
    return immutable_records.evidence_ref(
        shot,
        path,
        kind="authority_record",
        schema=evaluation.SCHEMA,
        digest=evaluation.digest,
    )
