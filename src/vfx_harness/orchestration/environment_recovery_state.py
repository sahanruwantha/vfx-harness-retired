"""Durable state records for receipt-backed external environment recovery."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from vfx_harness.domain.environment_recovery import EnvironmentRecoveryCommit
from vfx_harness.domain.environment_results import EnvironmentResult
from vfx_harness.domain.stop_transaction_state import StopEvidenceRef
from vfx_harness.domain.stop_transactions import PostconditionEvaluation
from vfx_harness.observability.run_artifacts import shot_state_dir

ROOT = Path("recovery") / "environment"
RESULTS = ROOT / "results"
EVALUATIONS = ROOT / "evaluations"
LOCKS = ROOT / "locks"

def _canonical_bytes(value: dict[str, Any]) -> bytes:
    try:
        return (
            json.dumps(
                value,
                allow_nan=False,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("environment recovery state must be finite canonical JSON") from exc


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _require_key(value: str) -> str:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError("environment recovery idempotency key must be a lowercase SHA-256 digest")
    return value


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
            _fsync_directory(directory.parent)
        _fsync_directory(directory)
    return shot, root


def _read_json(path: Path, where: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{where} must be an immutable regular file")
    try:
        value = json.loads(path.read_bytes(), object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{where} is malformed JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{where} must contain an object")
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"environment recovery state contains duplicate JSON key {key!r}")
        value[key] = item
    return value


def _publish_immutable(path: Path, raw: bytes) -> None:
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != raw:
            raise ValueError(f"immutable environment recovery record {path} conflicts with existing bytes")
        return
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.tmp-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.is_symlink() or not path.is_file() or path.read_bytes() != raw:
                raise ValueError(
                    f"immutable environment recovery record {path} conflicts with existing bytes"
                ) from None
    finally:
        temporary.unlink(missing_ok=True)
        _fsync_directory(path.parent)


def _ref(
    shot: Path,
    path: Path,
    *,
    kind: str,
    schema: str,
    digest: str,
) -> StopEvidenceRef:
    raw = path.read_bytes()
    return StopEvidenceRef(
        kind=kind,
        locator=path.resolve().relative_to(shot).as_posix(),
        sha256=hashlib.sha256(raw).hexdigest(),
        record_schema=schema,
        record_digest=digest,
    )


@contextmanager
def environment_recovery_lock(shot_folder: str | Path, idempotency_key: str):
    """Serialize one environment adapter without sharing the receipt-store lock."""

    _require_key(idempotency_key)
    _shot, root = _state_root(shot_folder)
    lock_path = root / "locks" / f"{idempotency_key}.lock"
    if lock_path.is_symlink() or (lock_path.exists() and not lock_path.is_file()):
        raise ValueError(f"environment recovery lock {lock_path} must be a regular file")
    with lock_path.open("a+b") as handle:
        handle.flush()
        os.fsync(handle.fileno())
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def publish_environment_result(
    shot_folder: str | Path,
    result: EnvironmentResult,
) -> StopEvidenceRef:
    if not isinstance(result, EnvironmentResult):
        raise ValueError("environment recovery result must be an EnvironmentResult")
    shot, root = _state_root(shot_folder)
    raw = _canonical_bytes(result.as_dict())
    path = root / "results" / f"{result.digest}.json"
    _publish_immutable(path, raw)
    observed = EnvironmentResult.from_dict(
        _read_json(path, "environment recovery result"),
        "environment recovery result",
    )
    if observed != result or path.read_bytes() != raw:
        raise ValueError("published environment recovery result failed exact read-back")
    return _ref(
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
    value = _read_json(path, "environment recovery result")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != evidence.sha256:
        raise ValueError("environment result evidence SHA-256 mismatch")
    result = EnvironmentResult.from_dict(value, "environment recovery result")
    if result.digest != evidence.record_digest or raw != _canonical_bytes(result.as_dict()):
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
        _read_json(path, "environment recovery commit"),
        "environment recovery commit",
    )
    if commit.idempotency_key != idempotency_key:
        raise ValueError("environment recovery commit belongs to another idempotency key")
    if path.read_bytes() != _canonical_bytes(commit.as_dict()):
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
    raw = _canonical_bytes(commit.as_dict())
    _publish_immutable(path, raw)
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
    return _ref(
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
        _read_json(path, "environment postcondition evaluation"),
        "environment postcondition evaluation",
    )
    if evaluation.idempotency_key != idempotency_key:
        raise ValueError("postcondition evaluation belongs to another idempotency key")
    if path.read_bytes() != _canonical_bytes(evaluation.as_dict()):
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
    raw = _canonical_bytes(evaluation.as_dict())
    _publish_immutable(path, raw)
    observed = read_postcondition_evaluation(shot, evaluation.idempotency_key)
    if observed != evaluation or path.read_bytes() != raw:
        raise ValueError("published environment postcondition evaluation failed exact read-back")
    return _ref(
        shot,
        path,
        kind="authority_record",
        schema=evaluation.SCHEMA,
        digest=evaluation.digest,
    )
