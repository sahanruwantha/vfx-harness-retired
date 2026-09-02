"""Opaque prepared publication for the one canonical ``shot.json`` ledger.

Large merge/serialization work happens under the ledger lock alone.  The exact
prepared bytes are then committed under a live shot-authority writer capability
and the mechanically ordered nonblocking ledger mutation lock.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import weakref
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vfx_harness.infrastructure.trusted_files import (
    TrustedFileBinding,
    TrustedFileError,
    read_trusted_file,
    require_trusted_file_unchanged,
)
from vfx_harness.observability import fork_coordination
from vfx_harness.observability.prepared_publication import (
    FilePublicationConflict,
    PreparedFilePayloadVerification,
    PreparedFilePublication,
    commit_prepared_file,
    discard_prepared_file,
    prepare_file_update,
    verify_prepared_file_payload,
)
from vfx_harness.observability.prepared_publication_destinations import (
    _bind_prepared_publication_destination_issuer,
    _issue_prepared_publication_destination_authorization,
)
from vfx_harness.orchestration.shot_authority_capture import (
    ShotAuthorityWriterCapability,
    require_live_shot_authority_writer,
)
from vfx_harness.orchestration.shot_ledger_lock import (
    LedgerSaveConflict,
    canonical_shot_ledger_path,
    ledger_lock,
    shot_ledger_mutation_lock,
)


class PreparedShotLedgerPublication:
    """Opaque exact-object capability for one merged ledger generation."""

    __slots__ = ("__weakref__",)

    def __new__(
        cls,
        *_args: Any,
        **_kwargs: Any,
    ) -> PreparedShotLedgerPublication:
        raise LedgerSaveConflict(
            "prepared shot-ledger publications are minted only by the ledger owner"
        )

    def __getattr__(self, name: str) -> Any:
        record = _require_prepared_shot_ledger(self)
        if name in {"destination", "payload_sha256"}:
            return getattr(record, name)
        raise AttributeError(name)

    def __copy__(self) -> PreparedShotLedgerPublication:
        raise LedgerSaveConflict("prepared shot-ledger capabilities cannot be copied")

    def __deepcopy__(self, _memo: dict[int, Any]) -> PreparedShotLedgerPublication:
        raise LedgerSaveConflict("prepared shot-ledger capabilities cannot be copied")

    def __reduce_ex__(self, _protocol: int) -> Any:
        raise LedgerSaveConflict(
            "prepared shot-ledger capabilities cannot be serialized"
        )

    def __repr__(self) -> str:
        return "<PreparedShotLedgerPublication opaque>"


_DESTINATION_ISSUER = _bind_prepared_publication_destination_issuer(
    family="shot-ledger",
    owner_type=PreparedShotLedgerPublication,
)


@dataclass(frozen=True, slots=True)
class _PreparedShotLedgerRecord:
    shot: Path
    destination: Path
    payload_sha256: str
    authority_binding: str
    publication: PreparedFilePublication | None
    verification: PreparedFilePayloadVerification | None
    existing_binding: TrustedFileBinding | None
    transaction_binding: object
    process_id: int
    process_token: object
    thread_id: int
    thread_token: object


@dataclass(slots=True)
class _PreparedShotLedgerEntry:
    reference: weakref.ReferenceType[PreparedShotLedgerPublication]
    record: _PreparedShotLedgerRecord
    consumed: bool = False


_LOCK = threading.RLock()
_PROCESS_TOKEN = object()
_THREAD_LOCAL = threading.local()
_PREPARED: dict[int, _PreparedShotLedgerEntry] = {}


def _after_fork_child() -> None:
    global _LOCK, _PROCESS_TOKEN, _THREAD_LOCAL

    _PREPARED.clear()
    _LOCK = threading.RLock()
    _PROCESS_TOKEN = object()
    _THREAD_LOCAL = threading.local()


fork_coordination.register_fork_participant(
    "orchestration.shot_ledger_publication",
    lock_factory=lambda: _LOCK,
    after_in_child=_after_fork_child,
)


@contextmanager
def _locked() -> Iterator[None]:
    with fork_coordination.fork_coordinated_lock(_LOCK):
        yield


def _thread_token() -> object:
    process_id = os.getpid()
    token = getattr(_THREAD_LOCAL, "token", None)
    owner_process_id = getattr(_THREAD_LOCAL, "process_id", None)
    if token is None or owner_process_id != process_id:
        token = object()
        _THREAD_LOCAL.token = token
        _THREAD_LOCAL.process_id = process_id
    return token


def _prepared_gone(
    identifier: int,
    observed: weakref.ReferenceType[PreparedShotLedgerPublication],
) -> None:
    with _locked():
        entry = _PREPARED.get(identifier)
        if entry is not None and entry.reference is observed:
            _PREPARED.pop(identifier, None)


def _resolve_prepared_shot_ledger(
    prepared: PreparedShotLedgerPublication,
) -> _PreparedShotLedgerEntry:
    if type(prepared) is not PreparedShotLedgerPublication:
        raise LedgerSaveConflict(
            "shot-ledger operation requires an exact prepared capability"
        )
    with _locked():
        entry = _PREPARED.get(id(prepared))
        if entry is None or entry.reference() is not prepared:
            raise LedgerSaveConflict(
                "prepared shot-ledger publication is unregistered, expired, consumed, or copied"
            )
        record = entry.record
        if (
            record.process_id != os.getpid()
            or record.process_token is not _PROCESS_TOKEN
            or record.thread_id != threading.get_ident()
            or record.thread_token is not _thread_token()
        ):
            raise LedgerSaveConflict(
                "prepared shot-ledger publication belongs to another process or thread"
            )
        return entry


def _require_prepared_shot_ledger(
    prepared: PreparedShotLedgerPublication,
) -> _PreparedShotLedgerRecord:
    entry = _resolve_prepared_shot_ledger(prepared)
    if entry.consumed:
        raise LedgerSaveConflict(
            "prepared shot-ledger publication is expired or already consumed"
        )
    return entry.record


def _register_prepared_shot_ledger(
    prepared: PreparedShotLedgerPublication,
    record: _PreparedShotLedgerRecord,
) -> None:
    identifier = id(prepared)
    reference = weakref.ref(
        prepared,
        lambda observed, key=identifier: _prepared_gone(key, observed),
    )
    with _locked():
        if identifier in _PREPARED:  # pragma: no cover - live object identity
            raise LedgerSaveConflict(
                "prepared shot-ledger identity collided with a live capability"
            )
        _PREPARED[identifier] = _PreparedShotLedgerEntry(reference, record)


def _retire_prepared_shot_ledger(
    prepared: PreparedShotLedgerPublication,
) -> _PreparedShotLedgerRecord:
    entry = _resolve_prepared_shot_ledger(prepared)
    with _locked():
        current = _PREPARED.get(id(prepared))
        if current is None or current is not entry or current.reference() is not prepared:
            raise LedgerSaveConflict(
                "prepared shot-ledger publication is unregistered, expired, consumed, or copied"
            )
        if current.consumed:
            raise LedgerSaveConflict(
                "prepared shot-ledger publication is expired or already consumed"
            )
        current.consumed = True
    return entry.record


def _merge_payload(
    on_disk: Mapping[str, Any],
    data: Mapping[str, Any],
    loaded: Mapping[str, Any],
    touched: frozenset[str],
    run_id: str,
) -> bytes:
    merged = dict(on_disk)
    for key, value in data.items():
        if key == "milestones":
            continue
        if key not in on_disk or value != loaded.get(key):
            merged[key] = value
    slots = dict(on_disk.get("milestones", {}))
    local_slots = data.get("milestones", {})
    for layer_id in touched:
        slots[layer_id] = local_slots.get(layer_id, {})
    merged["milestones"] = slots
    merged.setdefault("runs", [])
    if run_id not in merged["runs"]:
        merged["runs"] = (merged["runs"] + [run_id])[-20:]
    return (json.dumps(merged, indent=2) + "\n").encode()


def _binding(authority_binding: str | None) -> str:
    if authority_binding is None:
        return "vfx-harness.shot-ledger-publication-binding/v1:base"
    if not isinstance(authority_binding, str) or not authority_binding.strip():
        raise LedgerSaveConflict(
            "shot-ledger publication requires a non-empty authority binding"
        )
    return authority_binding


def _require_record_target(record: _PreparedShotLedgerRecord) -> None:
    shot, destination = canonical_shot_ledger_path(record.destination)
    if record.shot != shot or destination != record.shot / "shot.json":
        raise LedgerSaveConflict(
            "prepared shot-ledger destination does not match its exact shot root"
        )
    if (record.publication is None) == (record.existing_binding is None):
        raise LedgerSaveConflict(
            "prepared shot-ledger requires exactly one staged or unchanged generation"
        )
    if (record.publication is None) != (record.verification is None):
        raise LedgerSaveConflict(
            "prepared shot-ledger replacement requires its exact payload verification"
        )


def prepare_shot_ledger_publication(
    path: str | Path,
    data: Mapping[str, Any],
    loaded: Mapping[str, Any],
    touched: frozenset[str],
    *,
    run_id: str,
    authority_binding: str | None,
) -> PreparedShotLedgerPublication:
    """Merge and fsync one opaque exact ledger generation under ledger EX only."""

    shot, destination = canonical_shot_ledger_path(path)
    if not isinstance(data, Mapping) or not isinstance(loaded, Mapping):
        raise LedgerSaveConflict("shot-ledger merge inputs must be mappings")
    if not isinstance(touched, frozenset) or any(
        not isinstance(value, str) or not value for value in touched
    ):
        raise LedgerSaveConflict(
            "shot-ledger touched identities must be a frozenset of non-empty strings"
        )
    if not isinstance(run_id, str) or not run_id:
        raise LedgerSaveConflict("shot-ledger publication requires a non-empty run id")
    binding = _binding(authority_binding)
    prepared = object.__new__(PreparedShotLedgerPublication)
    transaction_binding = object()
    publication: PreparedFilePublication | None = None
    verification: PreparedFilePayloadVerification | None = None
    existing_binding: TrustedFileBinding | None = None
    expected_sha256: str | None = None

    def require_commit_authority(
        authorization: object,
        observed_transaction_binding: object,
    ) -> None:
        if observed_transaction_binding is not transaction_binding:
            raise FilePublicationConflict(
                "shot-ledger commit authorization belongs to another prepared transaction"
            )
        if not isinstance(authorization, ShotAuthorityWriterCapability):
            raise FilePublicationConflict(
                "shot-ledger commit requires its exact live writer capability"
            )
        require_live_shot_authority_writer(authorization, shot)

    try:
        with ledger_lock(destination, exclusive=True):
            authorization = _issue_prepared_publication_destination_authorization(
                issuer=_DESTINATION_ISSUER,
                shot_folder=shot,
                destination=destination,
            )

            def merge(current: bytes | None) -> tuple[bytes | None, str]:
                if current is None:
                    on_disk: Mapping[str, Any] = {}
                else:
                    parsed = json.loads(current)
                    if not isinstance(parsed, dict):
                        raise LedgerSaveConflict(
                            f"ledger root must be a JSON object: {destination}; repair it before retrying"
                        )
                    on_disk = parsed
                payload = _merge_payload(on_disk, data, loaded, touched, run_id)
                digest = hashlib.sha256(payload).hexdigest()
                return (None if current == payload else payload), digest

            update = prepare_file_update(
                shot,
                destination,
                merge,
                authority_binding=binding,
                commit_policy=require_commit_authority,
                destination_authorization=authorization,
            )
            publication = update.publication
            expected_sha256 = update.result
            if publication is not None:
                verification = verify_prepared_file_payload(
                    publication,
                    expected_sha256=expected_sha256,
                    transaction_binding=transaction_binding,
                )
            else:
                snapshot = read_trusted_file(
                    shot,
                    destination,
                    "unchanged canonical shot ledger",
                    require_nonempty=True,
                )
                if snapshot.sha256 != expected_sha256:
                    raise LedgerSaveConflict(
                        "unchanged shot-ledger bytes disagree with their merged generation"
                    )
                existing_binding = snapshot.binding
    except (FilePublicationConflict, TrustedFileError) as exc:
        discard_prepared_file(publication)
        raise LedgerSaveConflict(str(exc)) from exc
    except BaseException:
        discard_prepared_file(publication)
        raise

    assert expected_sha256 is not None
    record = _PreparedShotLedgerRecord(
        shot=shot,
        destination=destination,
        payload_sha256=expected_sha256,
        authority_binding=binding,
        publication=publication,
        verification=verification,
        existing_binding=existing_binding,
        transaction_binding=transaction_binding,
        process_id=os.getpid(),
        process_token=_PROCESS_TOKEN,
        thread_id=threading.get_ident(),
        thread_token=_thread_token(),
    )
    _require_record_target(record)
    try:
        _register_prepared_shot_ledger(prepared, record)
    except BaseException:
        discard_prepared_file(publication)
        raise
    return prepared


def commit_shot_ledger_publication(
    prepared: PreparedShotLedgerPublication,
    *,
    authority_binding: str | None,
    writer_capability: ShotAuthorityWriterCapability,
) -> str:
    """CAS-publish and read back one generation under the real ordered locks."""

    record = _require_prepared_shot_ledger(prepared)
    _require_record_target(record)
    if record.authority_binding != _binding(authority_binding):
        raise LedgerSaveConflict(
            "prepared shot-ledger authority binding changed; discard it and restart"
        )
    try:
        with shot_ledger_mutation_lock(record.destination, writer_capability):
            if _require_prepared_shot_ledger(prepared) is not record:
                raise LedgerSaveConflict(
                    "prepared shot-ledger registry changed during publication"
                )
            if record.publication is not None:
                assert record.verification is not None
                commit_prepared_file(
                    record.publication,
                    authority_binding=record.authority_binding,
                    payload_verification=record.verification,
                    transaction_binding=record.transaction_binding,
                    commit_authorization=writer_capability,
                )
            else:
                assert record.existing_binding is not None
                require_live_shot_authority_writer(writer_capability, record.shot)
                require_trusted_file_unchanged(
                    record.existing_binding,
                    "unchanged canonical shot ledger",
                )
            stored = read_trusted_file(
                record.shot,
                record.destination,
                "committed canonical shot ledger",
                require_nonempty=True,
            )
            if stored.sha256 != record.payload_sha256:
                raise LedgerSaveConflict(
                    "committed shot-ledger bytes disagree with the prepared generation"
                )
    except (FilePublicationConflict, TrustedFileError) as exc:
        raise LedgerSaveConflict(str(exc)) from exc
    _retire_prepared_shot_ledger(prepared)
    return record.payload_sha256


def discard_prepared_shot_ledger_publication(
    prepared: PreparedShotLedgerPublication,
) -> None:
    """Consume and clean only the exact uncommitted ledger preparation."""

    if type(prepared) is not PreparedShotLedgerPublication:
        return
    entry = _resolve_prepared_shot_ledger(prepared)
    if entry.consumed:
        return
    record = entry.record
    try:
        discard_prepared_file(record.publication)
    except FilePublicationConflict as exc:
        raise LedgerSaveConflict(str(exc)) from exc
    _retire_prepared_shot_ledger(prepared)


__all__ = [
    "LedgerSaveConflict",
    "PreparedShotLedgerPublication",
    "commit_shot_ledger_publication",
    "discard_prepared_shot_ledger_publication",
    "prepare_shot_ledger_publication",
]
