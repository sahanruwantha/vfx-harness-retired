"""Durable, key-addressable transaction receipt chains (HIR-0164, HIR-0166).

Receipt records are immutable and content addressed.  One atomically replaced pointer
selects the current head for an idempotency key; readers verify that head and its full
prepared-first predecessor chain.  This module persists transaction facts only.  It
does not choose or dispatch a stop action.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vfx_harness.domain.stop_envelope_primitives import (
    canonical_digest,
    require_canonical_digest,
    require_digest,
)
from vfx_harness.domain.stop_transaction_state import StopEvidenceRef
from vfx_harness.domain.transaction_receipts import (
    TRANSACTION_RECEIPT_PHASES,
    TransactionReceipt,
    validate_receipt_chain,
)
from vfx_harness.observability.run_artifacts import shot_state_dir

TRANSACTIONS_DIR = "transactions"
RECEIPTS_DIR = "receipts"
CURRENT_POINTER = "current.json"
POINTER_SCHEMA = "vfx-harness.transaction-receipt-pointer/v1"


class TransactionReceiptStoreError(ValueError):
    """Stored transaction receipt authority is missing, conflicting, or corrupt."""


def _positive_revision(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise TransactionReceiptStoreError(f"{where} must be a positive integer")
    return value


def _require_key(value: Any, where: str = "idempotency_key") -> str:
    try:
        return require_digest(value, where)
    except ValueError as exc:
        raise TransactionReceiptStoreError(str(exc)) from exc


@dataclass(frozen=True, slots=True)
class _ReceiptPointer:
    idempotency_key: str
    revision: int
    phase: str
    receipt_locator: str
    receipt_sha256: str
    receipt_digest: str

    def __post_init__(self) -> None:
        _require_key(self.idempotency_key, "transaction receipt pointer.idempotency_key")
        _positive_revision(self.revision, "transaction receipt pointer.revision")
        if self.phase not in TRANSACTION_RECEIPT_PHASES:
            raise TransactionReceiptStoreError(
                "transaction receipt pointer.phase must be one of "
                f"{sorted(TRANSACTION_RECEIPT_PHASES)}"
            )
        _require_key(self.receipt_sha256, "transaction receipt pointer.receipt_sha256")
        _require_key(self.receipt_digest, "transaction receipt pointer.receipt_digest")
        expected_locator = f"{RECEIPTS_DIR}/{self.receipt_digest}.json"
        if self.receipt_locator != expected_locator:
            raise TransactionReceiptStoreError(
                "transaction receipt pointer locator must name its exact content-addressed receipt"
            )

    def _payload(self) -> dict[str, Any]:
        return {
            "schema": POINTER_SCHEMA,
            "idempotency_key": self.idempotency_key,
            "revision": self.revision,
            "phase": self.phase,
            "receipt_locator": self.receipt_locator,
            "receipt_sha256": self.receipt_sha256,
            "receipt_digest": self.receipt_digest,
        }

    @property
    def digest(self) -> str:
        return canonical_digest(self._payload())

    def as_dict(self) -> dict[str, Any]:
        return {**self._payload(), "pointer_digest": self.digest}

    @classmethod
    def from_dict(cls, value: Any, where: str) -> _ReceiptPointer:
        expected = {
            "schema",
            "idempotency_key",
            "revision",
            "phase",
            "receipt_locator",
            "receipt_sha256",
            "receipt_digest",
            "pointer_digest",
        }
        if not isinstance(value, Mapping):
            raise TransactionReceiptStoreError(f"{where} must contain an object")
        found = set(value)
        if found != expected:
            raise TransactionReceiptStoreError(
                f"{where} fields mismatch; missing={sorted(expected - found)}; "
                f"unexpected={sorted(found - expected)}"
            )
        if value["schema"] != POINTER_SCHEMA:
            raise TransactionReceiptStoreError(
                f"{where}.schema must be {POINTER_SCHEMA!r}, found {value['schema']!r}"
            )
        pointer = cls(
            idempotency_key=value["idempotency_key"],
            revision=value["revision"],
            phase=value["phase"],
            receipt_locator=value["receipt_locator"],
            receipt_sha256=value["receipt_sha256"],
            receipt_digest=value["receipt_digest"],
        )
        try:
            require_canonical_digest(
                value["pointer_digest"],
                pointer.digest,
                where,
                "pointer_digest",
            )
        except ValueError as exc:
            raise TransactionReceiptStoreError(str(exc)) from exc
        return pointer


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise TransactionReceiptStoreError(
                f"transaction receipt authority contains duplicate JSON key {key!r}"
            )
        value[key] = item
    return value


def _decode_json(raw: bytes, where: str) -> Any:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TransactionReceiptStoreError(f"{where} is not UTF-8 JSON") from exc
    try:
        return json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise TransactionReceiptStoreError(f"{where} is invalid JSON: {exc}") from exc


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
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
        raise TransactionReceiptStoreError(
            "transaction receipt authority must be finite canonical JSON"
        ) from exc


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _validate_store_parents(shot_folder: str | Path, idempotency_key: str) -> Path:
    """Reject path substitution without creating any part of the read path."""

    shot = Path(shot_folder)
    if shot.is_symlink():
        raise TransactionReceiptStoreError(
            f"transaction receipt shot root {shot} must not be a symlink"
        )
    state = shot_state_dir(shot)
    transactions = state / TRANSACTIONS_DIR
    key_root = transactions / idempotency_key
    receipts = key_root / RECEIPTS_DIR
    for directory in (state, transactions, key_root, receipts):
        if directory.is_symlink():
            raise TransactionReceiptStoreError(
                f"transaction receipt store directory {directory} must not be a symlink"
            )
        if directory.exists() and not directory.is_dir():
            raise TransactionReceiptStoreError(
                f"transaction receipt store path {directory} must be a directory"
            )
    return key_root


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_file(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ensure_store_directories(shot_folder: str | Path, idempotency_key: str) -> Path:
    shot = Path(shot_folder)
    if not shot.is_dir() or shot.is_symlink():
        raise TransactionReceiptStoreError(
            f"transaction receipt shot root {shot} must be an existing real directory"
        )
    state = shot_state_dir(shot)
    transactions = state / TRANSACTIONS_DIR
    key_root = transactions / idempotency_key
    receipts = key_root / RECEIPTS_DIR
    parent = shot
    for directory in (state, transactions, key_root, receipts):
        if directory.is_symlink():
            raise TransactionReceiptStoreError(
                f"transaction receipt store directory {directory} must not be a symlink"
            )
        existed = directory.exists()
        directory.mkdir(exist_ok=True)
        if not directory.is_dir():
            raise TransactionReceiptStoreError(
                f"transaction receipt store path {directory} must be a directory"
            )
        if not existed:
            _fsync_directory(parent)
        _fsync_directory(directory)
        parent = directory
    return key_root


@contextmanager
def _locked_key(shot_folder: str | Path, idempotency_key: str):
    key_root = _ensure_store_directories(shot_folder, idempotency_key)
    lock_path = key_root / ".lock"
    if lock_path.is_symlink() or (lock_path.exists() and not lock_path.is_file()):
        raise TransactionReceiptStoreError(
            f"transaction receipt lock {lock_path} must be a regular file"
        )
    created = not lock_path.exists()
    with lock_path.open("a+b") as handle:
        if created:
            handle.flush()
            os.fsync(handle.fileno())
            _fsync_directory(key_root)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield key_root
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _receipt_bytes(receipt: TransactionReceipt) -> bytes:
    return _canonical_bytes(receipt.as_dict())


def _read_receipt_path(path: Path, expected_digest: str, where: str) -> tuple[TransactionReceipt, bytes]:
    if path.is_symlink() or not path.is_file():
        raise TransactionReceiptStoreError(f"{where} is not an immutable receipt file")
    raw = path.read_bytes()
    value = _decode_json(raw, where)
    try:
        receipt = TransactionReceipt.from_dict(value, where)
    except (TypeError, ValueError) as exc:
        raise TransactionReceiptStoreError(f"{where} is not a valid transaction receipt: {exc}") from exc
    if receipt.digest != expected_digest:
        raise TransactionReceiptStoreError(
            f"{where} digest does not match its content-addressed filename"
        )
    if raw != _receipt_bytes(receipt):
        raise TransactionReceiptStoreError(
            f"{where} bytes are not the canonical immutable receipt encoding"
        )
    return receipt, raw


def _read_receipt(key_root: Path, digest: str, where: str) -> tuple[TransactionReceipt, bytes]:
    _require_key(digest, f"{where}.digest")
    return _read_receipt_path(
        key_root / RECEIPTS_DIR / f"{digest}.json",
        digest,
        where,
    )


def _read_pointer(path: Path) -> tuple[_ReceiptPointer, bytes]:
    where = f"transaction receipt pointer {path}"
    if path.is_symlink() or not path.is_file():
        raise TransactionReceiptStoreError(f"{where} is not a regular file")
    raw = path.read_bytes()
    pointer = _ReceiptPointer.from_dict(_decode_json(raw, where), where)
    if raw != _canonical_bytes(pointer.as_dict()):
        raise TransactionReceiptStoreError(f"{where} bytes are not the canonical pointer encoding")
    return pointer, raw


def _verified_chain(
    key_root: Path,
    pointer: _ReceiptPointer,
) -> tuple[TransactionReceipt, ...]:
    current, current_bytes = _read_receipt(
        key_root,
        pointer.receipt_digest,
        "selected transaction receipt",
    )
    if _sha256(current_bytes) != pointer.receipt_sha256:
        raise TransactionReceiptStoreError(
            "selected transaction receipt bytes do not match current.json"
        )
    if (
        current.idempotency_key != pointer.idempotency_key
        or current.revision != pointer.revision
        or current.phase != pointer.phase
    ):
        raise TransactionReceiptStoreError(
            "selected transaction receipt identity disagrees with current.json"
        )

    newest_first = [current]
    seen = {current.digest}
    while newest_first[-1].predecessor_receipt_digest is not None:
        predecessor_digest = newest_first[-1].predecessor_receipt_digest
        if predecessor_digest in seen:
            raise TransactionReceiptStoreError("transaction receipt predecessor chain contains a cycle")
        predecessor, _raw = _read_receipt(
            key_root,
            predecessor_digest,
            f"transaction receipt predecessor revision {newest_first[-1].revision - 1}",
        )
        if predecessor.idempotency_key != pointer.idempotency_key:
            raise TransactionReceiptStoreError(
                "transaction receipt predecessor belongs to another idempotency key"
            )
        newest_first.append(predecessor)
        seen.add(predecessor.digest)
    chain = tuple(reversed(newest_first))
    try:
        head = validate_receipt_chain(chain)
    except ValueError as exc:
        raise TransactionReceiptStoreError(
            f"selected transaction receipt chain is invalid: {exc}"
        ) from exc
    if len(chain) != current.revision or head != current:
        raise TransactionReceiptStoreError(
            "selected transaction receipt chain does not contain every contiguous revision"
        )
    return chain


def _chain_from_root(
    key_root: Path,
    idempotency_key: str,
) -> tuple[TransactionReceipt, ...]:
    pointer_path = key_root / CURRENT_POINTER
    if pointer_path.is_symlink():
        raise TransactionReceiptStoreError(
            f"transaction receipt pointer {pointer_path} must not be a symlink"
        )
    if not pointer_path.exists():
        return ()
    pointer, _raw = _read_pointer(pointer_path)
    if pointer.idempotency_key != idempotency_key:
        raise TransactionReceiptStoreError(
            "transaction receipt pointer belongs to another idempotency key"
        )
    return _verified_chain(key_root, pointer)


def _current_from_root(key_root: Path, idempotency_key: str) -> TransactionReceipt | None:
    chain = _chain_from_root(key_root, idempotency_key)
    return chain[-1] if chain else None


def transaction_receipt_chain(
    shot_folder: str | Path,
    idempotency_key: str,
) -> tuple[TransactionReceipt, ...]:
    """Read the complete verified prepared-first chain without creating state."""

    key = _require_key(idempotency_key)
    key_root = _validate_store_parents(shot_folder, key)
    return _chain_from_root(key_root, key)


def current_transaction_receipt(
    shot_folder: str | Path,
    idempotency_key: str,
) -> TransactionReceipt | None:
    """Read and verify the exact selected receipt chain without creating state."""

    chain = transaction_receipt_chain(shot_folder, idempotency_key)
    return chain[-1] if chain else None


def reconcile_transaction_receipt_crash_orphan(
    shot_folder: str | Path,
    idempotency_key: str,
) -> TransactionReceipt | None:
    """Select one exact direct successor left between record and pointer publish.

    This is storage recovery, not action dispatch.  It never chooses among competing
    records and never advances more than one already-written revision.  An absent
    orphan returns ``None``; ambiguous, unrelated, or non-successor records fail
    closed.
    """

    key = _require_key(idempotency_key)
    with _locked_key(shot_folder, key) as key_root:
        chain = _chain_from_root(key_root, key)
        current = chain[-1] if chain else None
        selected_digests = {receipt.digest for receipt in chain}
        orphans: list[tuple[TransactionReceipt, bytes]] = []
        for path in sorted((key_root / RECEIPTS_DIR).glob("*.json")):
            receipt, raw = _read_receipt_path(
                path,
                path.stem,
                f"transaction receipt crash-orphan candidate {path}",
            )
            if receipt.idempotency_key != key:
                raise TransactionReceiptStoreError(
                    f"transaction receipt file {path} belongs to another idempotency key"
                )
            if receipt.digest not in selected_digests:
                orphans.append((receipt, raw))
        if not orphans:
            return None
        if len(orphans) != 1:
            raise TransactionReceiptStoreError(
                "multiple unselected transaction receipts exist; exact crash recovery is ambiguous"
            )
        orphan, raw = orphans[0]
        if current is None:
            if orphan.phase != "prepared" or orphan.revision != 1:
                raise TransactionReceiptStoreError(
                    "unselected transaction receipt is not a prepared-first crash orphan"
                )
        else:
            if current.phase == "terminal":
                raise TransactionReceiptStoreError(
                    "an unselected receipt cannot follow a terminal transaction"
                )
            try:
                orphan.assert_successor(current)
            except ValueError as exc:
                raise TransactionReceiptStoreError(
                    f"unselected transaction receipt is not the exact direct successor: {exc}"
                ) from exc
        pointer = _ReceiptPointer(
            idempotency_key=key,
            revision=orphan.revision,
            phase=orphan.phase,
            receipt_locator=f"{RECEIPTS_DIR}/{orphan.digest}.json",
            receipt_sha256=_sha256(raw),
            receipt_digest=orphan.digest,
        )
        _publish_pointer(key_root / CURRENT_POINTER, pointer)
        selected = _current_from_root(key_root, key)
        if selected != orphan:
            raise TransactionReceiptStoreError(
                "transaction receipt crash-orphan reconciliation selected another record"
            )
        return selected


def _write_immutable(path: Path, raw: bytes) -> None:
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != raw:
            raise TransactionReceiptStoreError(
                f"immutable transaction receipt {path} conflicts with existing bytes"
            )
        _fsync_file(path)
        _fsync_directory(path.parent)
        return
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.tmp-",
        dir=path.parent,
    )
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
                raise TransactionReceiptStoreError(
                    f"immutable transaction receipt {path} conflicts with existing bytes"
                ) from None
    finally:
        temporary.unlink(missing_ok=True)
        _fsync_directory(path.parent)


def _publish_pointer(path: Path, pointer: _ReceiptPointer) -> None:
    raw = _canonical_bytes(pointer.as_dict())
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.tmp-",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _assert_no_conflicting_orphan(
    key_root: Path,
    current: TransactionReceipt | None,
    candidate: TransactionReceipt,
) -> None:
    receipts_root = key_root / RECEIPTS_DIR
    selected_digests: set[str] = set()
    selected = current
    while selected is not None:
        if selected.digest in selected_digests:
            raise TransactionReceiptStoreError(
                "selected transaction receipt predecessor chain contains a cycle"
            )
        selected_digests.add(selected.digest)
        predecessor_digest = selected.predecessor_receipt_digest
        if predecessor_digest is None:
            selected = None
        else:
            selected, _raw = _read_receipt(
                key_root,
                predecessor_digest,
                f"selected transaction receipt predecessor revision {selected.revision - 1}",
            )
    allowed_digests = selected_digests | {candidate.digest}
    for path in sorted(receipts_root.glob("*.json")):
        expected_digest = path.stem
        existing, _raw = _read_receipt_path(
            path,
            expected_digest,
            f"transaction receipt file {path}",
        )
        if existing.idempotency_key != candidate.idempotency_key:
            raise TransactionReceiptStoreError(
                f"transaction receipt file {path} belongs to another idempotency key"
            )
        if existing.digest not in allowed_digests:
            raise TransactionReceiptStoreError(
                "an unselected transaction receipt exists outside the verified chain; "
                "reconcile that exact crash orphan before publishing another successor"
            )


def publish_transaction_receipt(
    shot_folder: str | Path,
    receipt: TransactionReceipt,
) -> TransactionReceipt:
    """Publish one legal receipt revision and atomically select it.

    Re-publishing the selected record is idempotent, including after terminal.  A
    complete but unselected receipt left by a crash is selected only when the caller
    presents that exact record; a competing successor fails closed.
    """

    if not isinstance(receipt, TransactionReceipt):
        raise TransactionReceiptStoreError("receipt must be a TransactionReceipt")
    key = _require_key(receipt.idempotency_key)
    with _locked_key(shot_folder, key) as key_root:
        current = _current_from_root(key_root, key)
        if current == receipt:
            _assert_no_conflicting_orphan(key_root, current, receipt)
            return current
        if current is None:
            if receipt.phase != "prepared" or receipt.revision != 1:
                raise TransactionReceiptStoreError(
                    "the first selected transaction receipt must be prepared revision 1"
                )
        else:
            if current.phase == "terminal":
                raise TransactionReceiptStoreError(
                    "a terminal transaction receipt is immutable; only its exact replay is legal"
                )
            try:
                receipt.assert_successor(current)
            except ValueError as exc:
                raise TransactionReceiptStoreError(
                    f"transaction receipt is not the next legal revision: {exc}"
                ) from exc

        _assert_no_conflicting_orphan(key_root, current, receipt)
        raw = _receipt_bytes(receipt)
        receipt_path = key_root / RECEIPTS_DIR / f"{receipt.digest}.json"
        _write_immutable(receipt_path, raw)
        pointer = _ReceiptPointer(
            idempotency_key=key,
            revision=receipt.revision,
            phase=receipt.phase,
            receipt_locator=f"{RECEIPTS_DIR}/{receipt.digest}.json",
            receipt_sha256=_sha256(raw),
            receipt_digest=receipt.digest,
        )
        _publish_pointer(key_root / CURRENT_POINTER, pointer)
        selected = _current_from_root(key_root, key)
        if selected != receipt:
            raise TransactionReceiptStoreError(
                "transaction receipt pointer did not select the published exact record"
            )
        return selected


def transaction_receipt_evidence_ref(
    shot_folder: str | Path,
    receipt: TransactionReceipt,
) -> StopEvidenceRef:
    """Return a receipt ref only while the exact record is the verified selected head."""

    if not isinstance(receipt, TransactionReceipt):
        raise TransactionReceiptStoreError("receipt must be a TransactionReceipt")
    key = _require_key(receipt.idempotency_key)
    key_root = _validate_store_parents(shot_folder, key)
    selected_before = _current_from_root(key_root, key)
    if selected_before != receipt:
        raise TransactionReceiptStoreError(
            "transaction receipt evidence requires the exact current selected receipt"
        )
    receipt_path = key_root / RECEIPTS_DIR / f"{receipt.digest}.json"
    stored, raw = _read_receipt_path(
        receipt_path,
        receipt.digest,
        "transaction receipt evidence source",
    )
    selected_after = _current_from_root(key_root, key)
    if stored != receipt or selected_after != receipt:
        raise TransactionReceiptStoreError(
            "transaction receipt selection changed while its evidence reference was read"
        )
    locator = receipt_path.relative_to(Path(shot_folder)).as_posix()
    return receipt.evidence_ref(locator=locator, sha256=_sha256(raw))
