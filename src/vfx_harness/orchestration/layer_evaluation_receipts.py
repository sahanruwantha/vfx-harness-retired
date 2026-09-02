"""Immutable run-scoped evaluation receipts for claimed layer finalization."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import weakref
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import vfx_harness.orchestration.shot_authority_capture as shot_authority_capture
from vfx_harness.domain.layer_evaluation_receipts import (
    LayerEvaluationReceipt,
    canonical_layer_evaluation_receipt_bytes,
)
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
from vfx_harness.orchestration.layer_finalization_publication_authority import (
    LayerFinalizationPreparedMutationAuthorization,
    LayerFinalizationPreparedMutationConflict,
    consume_layer_finalization_prepared_mutation_authorization,
)
from vfx_harness.orchestration.layer_replay_receipts import (
    LayerFinalizationPublicationGuard,
    LayerReplayReceiptConflict,
    capture_layer_replay_causal_sources,
    load_layer_replay_receipt,
)

_RECEIPT_DIRECTORY = Path("checkpoints/layer-finalizations")


class LayerEvaluationReceiptConflict(ValueError):
    """An immutable evaluation receipt is missing, stale, or conflicts by bytes."""


class _LayerEvaluationReceiptMutationConflict(RuntimeError):
    """Carry a typed sink refusal through the finalization guard boundary."""


@dataclass(frozen=True, slots=True)
class StoredLayerEvaluationReceipt:
    receipt: LayerEvaluationReceipt
    locator: str
    sha256: str
    source_binding: TrustedFileBinding


class PreparedLayerEvaluationReceipt:
    """Opaque exact-object capability for one evaluation-receipt publication."""

    __slots__ = ("__weakref__",)

    def __new__(cls, *_args: Any, **_kwargs: Any) -> PreparedLayerEvaluationReceipt:
        raise LayerEvaluationReceiptConflict(
            "prepared layer evaluation receipts can be minted only from live finalization authority"
        )

    def __getattr__(self, name: str) -> Any:
        record = _require_prepared_layer_evaluation_receipt(self)
        if name in {"receipt", "locator", "destination", "sha256"}:
            return getattr(record, name)
        raise AttributeError(name)

    def __copy__(self) -> PreparedLayerEvaluationReceipt:
        raise LayerEvaluationReceiptConflict("prepared layer evaluation receipt capabilities cannot be copied")

    def __deepcopy__(self, _memo: dict[int, Any]) -> PreparedLayerEvaluationReceipt:
        raise LayerEvaluationReceiptConflict("prepared layer evaluation receipt capabilities cannot be copied")

    def __reduce_ex__(self, _protocol: int) -> Any:
        raise LayerEvaluationReceiptConflict("prepared layer evaluation receipt capabilities cannot be serialized")

    def __repr__(self) -> str:
        return "<PreparedLayerEvaluationReceipt opaque>"


_DESTINATION_ISSUER = _bind_prepared_publication_destination_issuer(
    family="layer-evaluation-receipt",
    owner_type=PreparedLayerEvaluationReceipt,
)


@dataclass(frozen=True, slots=True)
class _PreparedLayerEvaluationReceiptRecord:
    shot: Path
    receipt: LayerEvaluationReceipt
    locator: str
    destination: Path
    sha256: str
    publication: PreparedFilePublication | None
    existing_binding: TrustedFileBinding | None
    replay_receipt_bindings: tuple[TrustedFileBinding, ...]
    replay_causal_bindings: tuple[TrustedFileBinding, ...]
    authority_binding: str
    publication_guard: LayerFinalizationPublicationGuard
    process_id: int
    process_token: object
    thread_id: int
    thread_token: object


@dataclass(slots=True)
class _PreparedLayerEvaluationReceiptEntry:
    reference: weakref.ReferenceType[PreparedLayerEvaluationReceipt]
    record: _PreparedLayerEvaluationReceiptRecord


_PREPARED_EVALUATION_RECEIPT_LOCK = threading.RLock()
_PREPARED_EVALUATION_RECEIPTS: dict[int, _PreparedLayerEvaluationReceiptEntry] = {}
_PREPARED_EVALUATION_RECEIPT_PROCESS_TOKEN = object()
_PREPARED_EVALUATION_RECEIPT_THREAD_LOCAL = threading.local()


def _after_prepared_evaluation_receipt_fork_child() -> None:
    global _PREPARED_EVALUATION_RECEIPT_LOCK
    global _PREPARED_EVALUATION_RECEIPT_PROCESS_TOKEN
    global _PREPARED_EVALUATION_RECEIPT_THREAD_LOCAL

    _PREPARED_EVALUATION_RECEIPTS.clear()
    _PREPARED_EVALUATION_RECEIPT_LOCK = threading.RLock()
    _PREPARED_EVALUATION_RECEIPT_PROCESS_TOKEN = object()
    _PREPARED_EVALUATION_RECEIPT_THREAD_LOCAL = threading.local()


fork_coordination.register_fork_participant(
    "orchestration.layer_evaluation_receipts",
    lock_factory=lambda: _PREPARED_EVALUATION_RECEIPT_LOCK,
    after_in_child=_after_prepared_evaluation_receipt_fork_child,
)


@contextmanager
def _prepared_evaluation_receipt_locked() -> Iterator[None]:
    with fork_coordination.fork_coordinated_lock(
        _PREPARED_EVALUATION_RECEIPT_LOCK
    ):
        yield


def _current_prepared_evaluation_receipt_thread_token() -> object:
    process_id = os.getpid()
    token = getattr(_PREPARED_EVALUATION_RECEIPT_THREAD_LOCAL, "token", None)
    owner_process_id = getattr(
        _PREPARED_EVALUATION_RECEIPT_THREAD_LOCAL,
        "process_id",
        None,
    )
    if token is None or owner_process_id != process_id:
        token = object()
        _PREPARED_EVALUATION_RECEIPT_THREAD_LOCAL.token = token
        _PREPARED_EVALUATION_RECEIPT_THREAD_LOCAL.process_id = process_id
    return token


def _prepared_layer_evaluation_receipt_gone(
    identifier: int,
    observed: weakref.ReferenceType[PreparedLayerEvaluationReceipt],
) -> None:
    with _prepared_evaluation_receipt_locked():
        entry = _PREPARED_EVALUATION_RECEIPTS.get(identifier)
        if entry is not None and entry.reference is observed:
            _PREPARED_EVALUATION_RECEIPTS.pop(identifier, None)


def _require_prepared_layer_evaluation_receipt(
    prepared: PreparedLayerEvaluationReceipt,
) -> _PreparedLayerEvaluationReceiptRecord:
    if type(prepared) is not PreparedLayerEvaluationReceipt:
        raise LayerEvaluationReceiptConflict("layer evaluation receipt operation requires an exact prepared capability")
    with _prepared_evaluation_receipt_locked():
        entry = _PREPARED_EVALUATION_RECEIPTS.get(id(prepared))
        if entry is None or entry.reference() is not prepared:
            raise LayerEvaluationReceiptConflict(
                "prepared layer evaluation receipt is unregistered, expired, consumed, or copied"
            )
        record = entry.record
        if (
            record.process_id != os.getpid()
            or record.process_token is not _PREPARED_EVALUATION_RECEIPT_PROCESS_TOKEN
            or record.thread_id != threading.get_ident()
            or record.thread_token is not _current_prepared_evaluation_receipt_thread_token()
        ):
            raise LayerEvaluationReceiptConflict(
                "prepared layer evaluation receipt belongs to another process or thread"
            )
        return record


def _mint_prepared_layer_evaluation_receipt(
    *,
    shot: Path,
    receipt: LayerEvaluationReceipt,
    locator: str,
    destination: Path,
    sha256: str,
    publication: PreparedFilePublication | None,
    existing_binding: TrustedFileBinding | None,
    replay_receipt_bindings: tuple[TrustedFileBinding, ...],
    replay_causal_bindings: tuple[TrustedFileBinding, ...],
    authority_binding: str,
    publication_guard: LayerFinalizationPublicationGuard,
) -> PreparedLayerEvaluationReceipt:
    record = _PreparedLayerEvaluationReceiptRecord(
        shot=shot,
        receipt=receipt,
        locator=locator,
        destination=destination,
        sha256=sha256,
        publication=publication,
        existing_binding=existing_binding,
        replay_receipt_bindings=replay_receipt_bindings,
        replay_causal_bindings=replay_causal_bindings,
        authority_binding=authority_binding,
        publication_guard=publication_guard,
        process_id=os.getpid(),
        process_token=_PREPARED_EVALUATION_RECEIPT_PROCESS_TOKEN,
        thread_id=threading.get_ident(),
        thread_token=_current_prepared_evaluation_receipt_thread_token(),
    )
    _require_evaluation_record_target(record)
    prepared = object.__new__(PreparedLayerEvaluationReceipt)
    identifier = id(prepared)
    reference = weakref.ref(
        prepared,
        lambda observed, key=identifier: _prepared_layer_evaluation_receipt_gone(key, observed),
    )
    with _prepared_evaluation_receipt_locked():
        if identifier in _PREPARED_EVALUATION_RECEIPTS:  # pragma: no cover - live id guarantee
            raise LayerEvaluationReceiptConflict(
                "prepared layer evaluation receipt identity collided with a live capability"
            )
        _PREPARED_EVALUATION_RECEIPTS[identifier] = _PreparedLayerEvaluationReceiptEntry(reference, record)
    return prepared


def _retire_prepared_layer_evaluation_receipt(
    prepared: PreparedLayerEvaluationReceipt,
) -> _PreparedLayerEvaluationReceiptRecord:
    record = _require_prepared_layer_evaluation_receipt(prepared)
    with _prepared_evaluation_receipt_locked():
        entry = _PREPARED_EVALUATION_RECEIPTS.get(id(prepared))
        if entry is None or entry.reference() is not prepared:
            raise LayerEvaluationReceiptConflict(
                "prepared layer evaluation receipt is unregistered, expired, consumed, or copied"
            )
        _PREPARED_EVALUATION_RECEIPTS.pop(id(prepared), None)
    return record


def _absolute(path: str | Path) -> Path:
    return Path(os.path.abspath(Path(path).expanduser()))


def _shot_root(path: str | Path) -> Path:
    try:
        shot = _absolute(path)
    except (OSError, TypeError, ValueError) as exc:
        raise LayerEvaluationReceiptConflict("layer evaluation receipt requires a valid shot root path") from exc
    if not shot.is_absolute():
        raise LayerEvaluationReceiptConflict("layer evaluation receipt requires a canonical absolute shot root")
    return shot


def _authority_binding(receipt: LayerEvaluationReceipt) -> str:
    return f"layer-evaluation:{receipt.claim.claim_id}:{receipt.receipt_digest}"


def _canonical_receipt_bytes(receipt: LayerEvaluationReceipt) -> bytes:
    if not isinstance(receipt, LayerEvaluationReceipt):
        raise LayerEvaluationReceiptConflict("receipt must be a typed layer evaluation receipt")
    try:
        return canonical_layer_evaluation_receipt_bytes(receipt)
    except ValueError as exc:
        raise LayerEvaluationReceiptConflict(str(exc)) from exc


def _authority_target(
    shot: Path,
    receipt: LayerEvaluationReceipt,
) -> tuple[str, Path]:
    """Derive the only destination owned by one typed layer evaluation."""

    locator = layer_evaluation_receipt_locator(receipt)
    return locator, shot / locator


def _require_prepared_target(
    shot: Path,
    prepared: PreparedLayerEvaluationReceipt,
    guard: LayerFinalizationPublicationGuard,
) -> Path:
    """Refuse outer, nested, no-op, and cross-shot target substitutions."""

    record = _require_prepared_layer_evaluation_receipt(prepared)
    if record.shot != shot:
        raise LayerEvaluationReceiptConflict("prepared layer evaluation receipt belongs to another shot root")
    if record.publication_guard is not guard:
        raise LayerEvaluationReceiptConflict(
            "prepared layer evaluation receipt belongs to another exact finalization guard"
        )
    try:
        guard_shot = _shot_root(guard.folder)
    except AttributeError as exc:
        raise LayerEvaluationReceiptConflict("layer evaluation publication guard does not bind a shot root") from exc
    if guard_shot != shot:
        raise LayerEvaluationReceiptConflict("prepared layer evaluation receipt belongs to another shot root")
    return _require_evaluation_record_target(record)


def _require_evaluation_record_target(
    record: _PreparedLayerEvaluationReceiptRecord,
) -> Path:
    """Verify one private record against its receipt-derived physical target."""

    locator, destination = _authority_target(record.shot, record.receipt)
    expected_sha256 = hashlib.sha256(_canonical_receipt_bytes(record.receipt)).hexdigest()
    expected_authority = _authority_binding(record.receipt)
    if (
        record.locator != locator
        or record.destination != destination
        or record.sha256 != expected_sha256
        or record.authority_binding != expected_authority
    ):
        raise LayerEvaluationReceiptConflict(
            "prepared layer evaluation receipt target does not match its typed claim identity"
        )
    publication = record.publication
    if publication is None:
        binding = record.existing_binding
        if (
            not isinstance(binding, TrustedFileBinding)
            or binding.root != record.shot
            or binding.path != destination
            or binding.relative != locator
        ):
            raise LayerEvaluationReceiptConflict(
                "reused layer evaluation receipt requires its exact authority-derived destination binding"
            )
    elif (
        not isinstance(publication, PreparedFilePublication)
        or record.existing_binding is not None
        or publication.shot != record.shot
        or publication.relative_path != Path(locator)
        or publication.destination != destination
        or publication.destination_parent != destination.parent
        or publication.destination_name != destination.name
        or publication.temporary.parent != destination.parent
        or publication.authority_binding != record.authority_binding
        or publication.payload_sha256 != record.sha256
    ):
        raise LayerEvaluationReceiptConflict(
            "prepared layer evaluation publication does not match its authority-derived destination"
        )
    return destination


def _require_commit_origin(
    shot_folder: str | Path,
    prepared: PreparedLayerEvaluationReceipt,
    guard: LayerFinalizationPublicationGuard,
) -> Path:
    """Bind caller origin, preparation, and finalization guard before leasing."""

    shot = _shot_root(shot_folder)
    record = _require_prepared_layer_evaluation_receipt(prepared)
    if record.shot != shot:
        raise LayerEvaluationReceiptConflict("prepared layer evaluation receipt belongs to another shot root")
    if record.publication_guard is not guard:
        raise LayerEvaluationReceiptConflict(
            "prepared layer evaluation receipt belongs to another exact finalization guard"
        )
    try:
        guard_shot = _shot_root(guard.folder)
    except AttributeError as exc:
        raise LayerEvaluationReceiptConflict("layer evaluation publication guard does not bind a shot root") from exc
    if guard_shot != shot:
        raise LayerEvaluationReceiptConflict("layer evaluation publication guard belongs to another shot root")
    return shot


def layer_evaluation_receipt_locator(receipt: LayerEvaluationReceipt) -> str:
    if not isinstance(receipt, LayerEvaluationReceipt):
        raise LayerEvaluationReceiptConflict("layer evaluation locator requires a typed receipt")
    return (
        Path("runs") / receipt.claim.run_id / _RECEIPT_DIRECTORY / f"{receipt.claim.claim_id}.evaluation.json"
    ).as_posix()


def capture_layer_evaluation_causal_sources(
    folder: str | Path,
    receipt: LayerEvaluationReceipt,
) -> tuple[
    tuple[TrustedFileBinding, ...],
    tuple[TrustedFileBinding, ...],
]:
    """Derive the complete replay-receipt and replay-input source closure."""

    _canonical_receipt_bytes(receipt)
    shot = _shot_root(folder)
    replay_receipt_bindings: list[TrustedFileBinding] = []
    replay_causal_bindings: list[TrustedFileBinding] = []
    try:
        for index, binding in enumerate(receipt.replay_receipts):
            stored = load_layer_replay_receipt(
                shot,
                binding.locator,
                expected_sha256=binding.sha256,
                expected_digest=binding.receipt.receipt_digest,
            )
            if stored.receipt != binding.receipt:
                raise LayerEvaluationReceiptConflict(
                    f"layer evaluation replay group {index} bytes disagree with binding"
                )
            replay_receipt_bindings.append(stored.source_binding)
            replay_causal_bindings.extend(capture_layer_replay_causal_sources(shot, stored.receipt))
    except LayerReplayReceiptConflict as exc:
        raise LayerEvaluationReceiptConflict(str(exc)) from exc
    return tuple(replay_receipt_bindings), tuple(replay_causal_bindings)


def prepare_layer_evaluation_receipt(
    folder: str | Path,
    receipt: LayerEvaluationReceipt,
    guard: LayerFinalizationPublicationGuard,
) -> PreparedLayerEvaluationReceipt:
    """Fsync immutable evaluation bytes outside selected/state locks."""

    raw = _canonical_receipt_bytes(receipt)
    sha256 = hashlib.sha256(raw).hexdigest()
    locator = layer_evaluation_receipt_locator(receipt)
    authority = _authority_binding(receipt)
    shot = _shot_root(folder)
    if guard.claim != receipt.claim:
        raise LayerEvaluationReceiptConflict("layer evaluation preparation guard belongs to another claim")
    if _shot_root(guard.folder) != shot:
        raise LayerEvaluationReceiptConflict("layer evaluation preparation guard belongs to another shot")
    guard.check("start layer evaluation receipt preparation")
    expected_locator, destination = _authority_target(shot, receipt)
    if locator != expected_locator:
        raise LayerEvaluationReceiptConflict("layer evaluation receipt locator disagrees with its typed claim identity")
    replay_receipt_bindings, replay_causal_bindings = capture_layer_evaluation_causal_sources(shot, receipt)

    def create_only(current: bytes | None) -> tuple[bytes | None, None]:
        if current is None:
            return raw, None
        if current != raw:
            raise FilePublicationConflict("immutable layer evaluation receipt conflicts with existing bytes")
        return None, None

    def require_commit_authority(
        authorization: object,
        transaction_binding: object,
    ) -> None:
        for source in (*replay_receipt_bindings, *replay_causal_bindings):
            require_trusted_file_unchanged(
                source,
                "layer evaluation causal source",
            )
        consume_layer_finalization_prepared_mutation_authorization(
            authorization,
            expected_guard=guard,
            expected_shot=shot,
            expected_claim=guard.claim,
            expected_transaction_binding=transaction_binding,
        )

    try:
        prepared = prepare_file_update(
            shot,
            destination,
            create_only,
            authority_binding=authority,
            commit_policy=require_commit_authority,
            destination_authorization=(
                _issue_prepared_publication_destination_authorization(
                    issuer=_DESTINATION_ISSUER,
                    shot_folder=shot,
                    destination=destination,
                )
            ),
        )
    except FilePublicationConflict as exc:
        raise LayerEvaluationReceiptConflict(str(exc)) from exc
    existing = None
    if prepared.publication is None:
        try:
            existing = read_trusted_file(
                shot,
                shot / locator,
                "immutable layer evaluation receipt",
                require_nonempty=True,
            ).binding
        except TrustedFileError as exc:
            raise LayerEvaluationReceiptConflict(str(exc)) from exc
    try:
        return _mint_prepared_layer_evaluation_receipt(
            shot=shot,
            receipt=receipt,
            locator=locator,
            destination=destination,
            sha256=sha256,
            publication=prepared.publication,
            existing_binding=existing,
            replay_receipt_bindings=replay_receipt_bindings,
            replay_causal_bindings=replay_causal_bindings,
            authority_binding=authority,
            publication_guard=guard,
        )
    except BaseException:
        discard_prepared_file(prepared.publication)
        raise


def _derive_noop_receipt_binding(
    shot: Path,
    record: _PreparedLayerEvaluationReceiptRecord,
) -> TrustedFileBinding | None:
    """Reopen and strictly verify an asserted immutable-evaluation no-op."""

    if record.publication is not None:
        return None
    stored = load_layer_evaluation_receipt(
        shot,
        record.locator,
        expected_sha256=record.sha256,
        expected_digest=record.receipt.receipt_digest,
    )
    if stored.receipt != record.receipt:
        raise LayerEvaluationReceiptConflict("unchanged layer evaluation receipt bytes disagree with typed authority")
    if stored.source_binding != record.existing_binding:
        raise LayerEvaluationReceiptConflict(
            "prepared unchanged layer evaluation receipt audit does not match its rederived destination binding"
        )
    return stored.source_binding


def _commit_layer_evaluation_receipt(
    shot: Path,
    prepared: PreparedLayerEvaluationReceipt,
    record: _PreparedLayerEvaluationReceiptRecord,
    guard: LayerFinalizationPublicationGuard,
    replay_receipt_sources: tuple[TrustedFileBinding, ...],
    replay_causal_sources: tuple[TrustedFileBinding, ...],
    noop_receipt_binding: TrustedFileBinding | None,
    payload_verification: PreparedFilePayloadVerification | None,
    *,
    authorization: LayerFinalizationPreparedMutationAuthorization,
) -> None:
    """Perform the final evaluation CAS under one exact live writer lease."""

    _require_prepared_target(shot, prepared, guard)
    for source in (*replay_receipt_sources, *replay_causal_sources):
        require_trusted_file_unchanged(source, "layer evaluation causal source")
    if record.publication is not None:
        if payload_verification is None:
            raise LayerEvaluationReceiptConflict(
                "replacement layer evaluation receipt requires held staged-payload verification"
            )
        commit_prepared_file(
            record.publication,
            authority_binding=record.authority_binding,
            payload_verification=payload_verification,
            transaction_binding=prepared,
            commit_authorization=authorization,
        )
        return
    if payload_verification is not None:
        raise LayerEvaluationReceiptConflict("unchanged layer evaluation receipt cannot carry replacement-byte proof")
    if noop_receipt_binding is None or record.existing_binding != noop_receipt_binding:
        raise LayerEvaluationReceiptConflict(
            "unchanged layer evaluation receipt requires its rederived exact destination binding"
        )
    require_trusted_file_unchanged(
        noop_receipt_binding,
        "immutable layer evaluation receipt",
    )
    consume_layer_finalization_prepared_mutation_authorization(
        authorization,
        expected_guard=guard,
        expected_shot=shot,
        expected_claim=guard.claim,
        expected_transaction_binding=prepared,
    )


def commit_layer_evaluation_receipt(
    shot_folder: str | Path,
    prepared: PreparedLayerEvaluationReceipt,
    guard: LayerFinalizationPublicationGuard,
) -> StoredLayerEvaluationReceipt:
    """Publish one evaluation from its explicit originating shot."""

    if not isinstance(prepared, PreparedLayerEvaluationReceipt):
        raise LayerEvaluationReceiptConflict("layer evaluation commit requires typed prepared receipt")
    record = _require_prepared_layer_evaluation_receipt(prepared)
    if not isinstance(record.receipt, LayerEvaluationReceipt):
        raise LayerEvaluationReceiptConflict("prepared layer evaluation receipt must retain its typed receipt")
    if guard.claim != record.receipt.claim:
        raise LayerEvaluationReceiptConflict("layer evaluation publication guard belongs to another claim")
    if record.publication_guard is not guard:
        raise LayerEvaluationReceiptConflict(
            "prepared layer evaluation receipt belongs to another exact finalization guard"
        )
    shot = _require_commit_origin(shot_folder, prepared, guard)
    _require_prepared_target(shot, prepared, guard)
    try:
        replay_receipt_sources, replay_causal_sources = capture_layer_evaluation_causal_sources(
            shot,
            record.receipt,
        )
        if replay_receipt_sources != record.replay_receipt_bindings:
            raise LayerEvaluationReceiptConflict("prepared layer evaluation replay-receipt closure changed")
        if replay_causal_sources != record.replay_causal_bindings:
            raise LayerEvaluationReceiptConflict("prepared layer evaluation replay causal-source closure changed")
        noop_receipt_binding = _derive_noop_receipt_binding(shot, record)
        payload_verification = (
            None
            if record.publication is None
            else verify_prepared_file_payload(
                record.publication,
                expected_sha256=record.sha256,
                transaction_binding=prepared,
            )
        )
        publication_calls = 0
        publication_completed = False

        def publish(
            authorization: LayerFinalizationPreparedMutationAuthorization,
        ) -> None:
            nonlocal publication_calls, publication_completed
            publication_calls += 1
            if publication_calls != 1:
                raise _LayerEvaluationReceiptMutationConflict(
                    "layer evaluation finalization guard invoked its prepared mutation more than once"
                )
            try:
                _commit_layer_evaluation_receipt(
                    shot,
                    prepared,
                    record,
                    guard,
                    replay_receipt_sources,
                    replay_causal_sources,
                    noop_receipt_binding,
                    payload_verification,
                    authorization=authorization,
                )
            except LayerEvaluationReceiptConflict as exc:
                raise _LayerEvaluationReceiptMutationConflict(str(exc)) from exc
            publication_completed = True

        guard.publish_prepared(
            "publish immutable layer evaluation receipt",
            prepared,
            publish,
        )
        if publication_calls != 1 or not publication_completed:
            raise _LayerEvaluationReceiptMutationConflict(
                "layer evaluation finalization guard returned without completing its exact prepared mutation"
            )
    except (
        FilePublicationConflict,
        TrustedFileError,
        _LayerEvaluationReceiptMutationConflict,
        LayerFinalizationPreparedMutationConflict,
        shot_authority_capture.AuthoritySelectionConflict,
    ) as exc:
        raise LayerEvaluationReceiptConflict(str(exc)) from exc
    stored = load_layer_evaluation_receipt(
        shot,
        record.locator,
        expected_sha256=record.sha256,
        expected_digest=record.receipt.receipt_digest,
    )
    _retire_prepared_layer_evaluation_receipt(prepared)
    return stored


def discard_layer_evaluation_receipt(
    prepared: PreparedLayerEvaluationReceipt,
) -> None:
    if not isinstance(prepared, PreparedLayerEvaluationReceipt):
        return
    record = _require_prepared_layer_evaluation_receipt(prepared)
    try:
        discard_prepared_file(record.publication)
    except FilePublicationConflict as exc:
        raise LayerEvaluationReceiptConflict(str(exc)) from exc
    _retire_prepared_layer_evaluation_receipt(prepared)


def load_layer_evaluation_receipt(
    folder: str | Path,
    locator: str,
    *,
    expected_sha256: str | None = None,
    expected_digest: str | None = None,
) -> StoredLayerEvaluationReceipt:
    """Read and strictly verify one immutable evaluation receipt and its bytes."""

    shot = _shot_root(folder)
    try:
        snapshot = read_trusted_file(
            shot,
            shot / locator,
            "immutable layer evaluation receipt",
            require_nonempty=True,
        )
    except TrustedFileError as exc:
        raise LayerEvaluationReceiptConflict(str(exc)) from exc
    sha256 = hashlib.sha256(snapshot.payload).hexdigest()
    if expected_sha256 is not None and sha256 != expected_sha256:
        raise LayerEvaluationReceiptConflict("immutable layer evaluation receipt file digest changed")
    try:
        receipt = LayerEvaluationReceipt.parse(
            json.loads(snapshot.payload),
            "immutable layer evaluation receipt",
        )
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise LayerEvaluationReceiptConflict(str(exc)) from exc
    if expected_digest is not None and receipt.receipt_digest != expected_digest:
        raise LayerEvaluationReceiptConflict("immutable layer evaluation receipt semantic digest changed")
    return StoredLayerEvaluationReceipt(
        receipt=receipt,
        locator=locator,
        sha256=sha256,
        source_binding=snapshot.binding,
    )


__all__ = [
    "LayerEvaluationReceiptConflict",
    "PreparedLayerEvaluationReceipt",
    "StoredLayerEvaluationReceipt",
    "capture_layer_evaluation_causal_sources",
    "commit_layer_evaluation_receipt",
    "discard_layer_evaluation_receipt",
    "layer_evaluation_receipt_locator",
    "load_layer_evaluation_receipt",
    "prepare_layer_evaluation_receipt",
]
