"""Immutable run-scoped replay receipts for claimed layer finalization."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import weakref
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TypeVar

import vfx_harness.orchestration.shot_authority_capture as shot_authority_capture
from vfx_harness.domain.layer_finalizations import (
    LayerFinalizationClaim,
    LayerReplayReceipt,
    canonical_layer_replay_receipt_bytes,
)
from vfx_harness.infrastructure.trusted_files import (
    TrustedFileBinding,
    TrustedFileError,
    TrustedFileNotFound,
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

_RECEIPT_DIRECTORY = Path("checkpoints/layer-finalizations")
_T = TypeVar("_T")


class LayerReplayReceiptConflict(ValueError):
    """An immutable replay receipt is missing, stale, or conflicts by bytes."""


class _LayerReplayReceiptMutationConflict(RuntimeError):
    """Carry a typed sink refusal through the finalization guard boundary."""


class LayerFinalizationPublicationGuard(Protocol):
    """The small guard surface used by replay and projection publishers."""

    folder: Path
    claim: LayerFinalizationClaim

    def check(self, operation: str) -> LayerFinalizationClaim: ...

    def publish_prepared(
        self,
        operation: str,
        transaction_binding: object,
        mutation: Callable[
            [LayerFinalizationPreparedMutationAuthorization],
            _T,
        ],
    ) -> _T: ...


@dataclass(frozen=True, slots=True)
class StoredLayerReplayReceipt:
    receipt: LayerReplayReceipt
    locator: str
    sha256: str
    source_binding: TrustedFileBinding


class PreparedLayerReplayReceipt:
    """Opaque exact-object capability for one replay-receipt publication."""

    __slots__ = ("__weakref__",)

    def __new__(cls, *_args: Any, **_kwargs: Any) -> PreparedLayerReplayReceipt:
        raise LayerReplayReceiptConflict(
            "prepared layer replay receipts can be minted only from live finalization authority"
        )

    def __getattr__(self, name: str) -> Any:
        record = _require_prepared_layer_replay_receipt(self)
        if name in {"receipt", "locator", "destination", "sha256"}:
            return getattr(record, name)
        raise AttributeError(name)

    def __copy__(self) -> PreparedLayerReplayReceipt:
        raise LayerReplayReceiptConflict("prepared layer replay receipt capabilities cannot be copied")

    def __deepcopy__(self, _memo: dict[int, Any]) -> PreparedLayerReplayReceipt:
        raise LayerReplayReceiptConflict("prepared layer replay receipt capabilities cannot be copied")

    def __reduce_ex__(self, _protocol: int) -> Any:
        raise LayerReplayReceiptConflict("prepared layer replay receipt capabilities cannot be serialized")

    def __repr__(self) -> str:
        return "<PreparedLayerReplayReceipt opaque>"


_DESTINATION_ISSUER = _bind_prepared_publication_destination_issuer(
    family="layer-replay-receipt",
    owner_type=PreparedLayerReplayReceipt,
)


@dataclass(frozen=True, slots=True)
class _PreparedLayerReplayReceiptRecord:
    shot: Path
    receipt: LayerReplayReceipt
    locator: str
    destination: Path
    sha256: str
    publication: PreparedFilePublication | None
    existing_binding: TrustedFileBinding | None
    causal_source_bindings: tuple[TrustedFileBinding, ...]
    authority_binding: str
    publication_guard: LayerFinalizationPublicationGuard
    process_id: int
    process_token: object
    thread_id: int
    thread_token: object


@dataclass(slots=True)
class _PreparedLayerReplayReceiptEntry:
    reference: weakref.ReferenceType[PreparedLayerReplayReceipt]
    record: _PreparedLayerReplayReceiptRecord


_PREPARED_REPLAY_RECEIPT_LOCK = threading.RLock()
_PREPARED_REPLAY_RECEIPTS: dict[int, _PreparedLayerReplayReceiptEntry] = {}
_PREPARED_REPLAY_RECEIPT_PROCESS_TOKEN = object()
_PREPARED_REPLAY_RECEIPT_THREAD_LOCAL = threading.local()


def _after_prepared_replay_receipt_fork_child() -> None:
    global _PREPARED_REPLAY_RECEIPT_LOCK
    global _PREPARED_REPLAY_RECEIPT_PROCESS_TOKEN
    global _PREPARED_REPLAY_RECEIPT_THREAD_LOCAL

    _PREPARED_REPLAY_RECEIPTS.clear()
    _PREPARED_REPLAY_RECEIPT_LOCK = threading.RLock()
    _PREPARED_REPLAY_RECEIPT_PROCESS_TOKEN = object()
    _PREPARED_REPLAY_RECEIPT_THREAD_LOCAL = threading.local()


fork_coordination.register_fork_participant(
    "orchestration.layer_replay_receipts",
    lock_factory=lambda: _PREPARED_REPLAY_RECEIPT_LOCK,
    after_in_child=_after_prepared_replay_receipt_fork_child,
)


@contextmanager
def _prepared_replay_receipt_locked() -> Iterator[None]:
    with fork_coordination.fork_coordinated_lock(_PREPARED_REPLAY_RECEIPT_LOCK):
        yield


def _current_prepared_replay_receipt_thread_token() -> object:
    process_id = os.getpid()
    token = getattr(_PREPARED_REPLAY_RECEIPT_THREAD_LOCAL, "token", None)
    owner_process_id = getattr(
        _PREPARED_REPLAY_RECEIPT_THREAD_LOCAL,
        "process_id",
        None,
    )
    if token is None or owner_process_id != process_id:
        token = object()
        _PREPARED_REPLAY_RECEIPT_THREAD_LOCAL.token = token
        _PREPARED_REPLAY_RECEIPT_THREAD_LOCAL.process_id = process_id
    return token


def _prepared_layer_replay_receipt_gone(
    identifier: int,
    observed: weakref.ReferenceType[PreparedLayerReplayReceipt],
) -> None:
    with _prepared_replay_receipt_locked():
        entry = _PREPARED_REPLAY_RECEIPTS.get(identifier)
        if entry is not None and entry.reference is observed:
            _PREPARED_REPLAY_RECEIPTS.pop(identifier, None)


def _require_prepared_layer_replay_receipt(
    prepared: PreparedLayerReplayReceipt,
) -> _PreparedLayerReplayReceiptRecord:
    if type(prepared) is not PreparedLayerReplayReceipt:
        raise LayerReplayReceiptConflict("layer replay receipt operation requires an exact prepared capability")
    with _prepared_replay_receipt_locked():
        entry = _PREPARED_REPLAY_RECEIPTS.get(id(prepared))
        if entry is None or entry.reference() is not prepared:
            raise LayerReplayReceiptConflict(
                "prepared layer replay receipt is unregistered, expired, consumed, or copied"
            )
        record = entry.record
        if (
            record.process_id != os.getpid()
            or record.process_token is not _PREPARED_REPLAY_RECEIPT_PROCESS_TOKEN
            or record.thread_id != threading.get_ident()
            or record.thread_token is not _current_prepared_replay_receipt_thread_token()
        ):
            raise LayerReplayReceiptConflict("prepared layer replay receipt belongs to another process or thread")
        return record


def _mint_prepared_layer_replay_receipt(
    *,
    shot: Path,
    receipt: LayerReplayReceipt,
    locator: str,
    destination: Path,
    sha256: str,
    publication: PreparedFilePublication | None,
    existing_binding: TrustedFileBinding | None,
    causal_source_bindings: tuple[TrustedFileBinding, ...],
    authority_binding: str,
    publication_guard: LayerFinalizationPublicationGuard,
) -> PreparedLayerReplayReceipt:
    record = _PreparedLayerReplayReceiptRecord(
        shot=shot,
        receipt=receipt,
        locator=locator,
        destination=destination,
        sha256=sha256,
        publication=publication,
        existing_binding=existing_binding,
        causal_source_bindings=causal_source_bindings,
        authority_binding=authority_binding,
        publication_guard=publication_guard,
        process_id=os.getpid(),
        process_token=_PREPARED_REPLAY_RECEIPT_PROCESS_TOKEN,
        thread_id=threading.get_ident(),
        thread_token=_current_prepared_replay_receipt_thread_token(),
    )
    _require_replay_record_target(record)
    prepared = object.__new__(PreparedLayerReplayReceipt)
    identifier = id(prepared)
    reference = weakref.ref(
        prepared,
        lambda observed, key=identifier: _prepared_layer_replay_receipt_gone(key, observed),
    )
    with _prepared_replay_receipt_locked():
        if identifier in _PREPARED_REPLAY_RECEIPTS:  # pragma: no cover - live id guarantee
            raise LayerReplayReceiptConflict("prepared layer replay receipt identity collided with a live capability")
        _PREPARED_REPLAY_RECEIPTS[identifier] = _PreparedLayerReplayReceiptEntry(
            reference,
            record,
        )
    return prepared


def _retire_prepared_layer_replay_receipt(
    prepared: PreparedLayerReplayReceipt,
) -> _PreparedLayerReplayReceiptRecord:
    record = _require_prepared_layer_replay_receipt(prepared)
    with _prepared_replay_receipt_locked():
        entry = _PREPARED_REPLAY_RECEIPTS.get(id(prepared))
        if entry is None or entry.reference() is not prepared:
            raise LayerReplayReceiptConflict(
                "prepared layer replay receipt is unregistered, expired, consumed, or copied"
            )
        _PREPARED_REPLAY_RECEIPTS.pop(id(prepared), None)
    return record


def _absolute(path: str | Path) -> Path:
    return Path(os.path.abspath(Path(path).expanduser()))


def _shot_root(path: str | Path) -> Path:
    try:
        shot = _absolute(path)
    except (OSError, TypeError, ValueError) as exc:
        raise LayerReplayReceiptConflict("layer replay receipt requires a valid shot root path") from exc
    if not shot.is_absolute():
        raise LayerReplayReceiptConflict("layer replay receipt requires a canonical absolute shot root")
    return shot


def _authority_target(
    shot: Path,
    receipt: LayerReplayReceipt,
) -> tuple[str, Path]:
    """Derive the only destination owned by one typed replay-group identity."""

    locator = layer_replay_receipt_locator(
        receipt.claim,
        receipt.observation.group_index,
    )
    return locator, shot / locator


def _require_prepared_target(
    shot: Path,
    prepared: PreparedLayerReplayReceipt,
    guard: LayerFinalizationPublicationGuard,
) -> Path:
    """Refuse outer, nested, no-op, and cross-shot target substitutions."""

    record = _require_prepared_layer_replay_receipt(prepared)
    if record.shot != shot:
        raise LayerReplayReceiptConflict("prepared layer replay receipt belongs to another shot root")
    if record.publication_guard is not guard:
        raise LayerReplayReceiptConflict("prepared layer replay receipt belongs to another exact finalization guard")
    try:
        guard_shot = _shot_root(guard.folder)
    except AttributeError as exc:
        raise LayerReplayReceiptConflict("layer replay publication guard does not bind a shot root") from exc
    if guard_shot != shot:
        raise LayerReplayReceiptConflict("prepared layer replay receipt belongs to another shot root")
    return _require_replay_record_target(record)


def _require_replay_record_target(
    record: _PreparedLayerReplayReceiptRecord,
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
        raise LayerReplayReceiptConflict(
            "prepared layer replay receipt target does not match its typed claim and replay-group identity"
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
            raise LayerReplayReceiptConflict(
                "reused layer replay receipt requires its exact authority-derived destination binding"
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
        raise LayerReplayReceiptConflict(
            "prepared layer replay publication does not match its authority-derived destination"
        )
    return destination


def _require_commit_origin(
    shot_folder: str | Path,
    prepared: PreparedLayerReplayReceipt,
    guard: LayerFinalizationPublicationGuard,
) -> Path:
    """Bind caller origin, preparation, and finalization guard before leasing."""

    shot = _shot_root(shot_folder)
    record = _require_prepared_layer_replay_receipt(prepared)
    if record.shot != shot:
        raise LayerReplayReceiptConflict("prepared layer replay receipt belongs to another shot root")
    if record.publication_guard is not guard:
        raise LayerReplayReceiptConflict("prepared layer replay receipt belongs to another exact finalization guard")
    try:
        guard_shot = _shot_root(guard.folder)
    except AttributeError as exc:
        raise LayerReplayReceiptConflict("layer replay publication guard does not bind a shot root") from exc
    if guard_shot != shot:
        raise LayerReplayReceiptConflict("layer replay publication guard belongs to another shot root")
    return shot


def layer_replay_receipt_locator(
    claim: LayerFinalizationClaim,
    group_index: int = 0,
) -> str:
    if not isinstance(claim, LayerFinalizationClaim):
        raise LayerReplayReceiptConflict("layer replay receipt requires a typed finalization claim")
    if not isinstance(group_index, int) or isinstance(group_index, bool) or group_index < 0:
        raise LayerReplayReceiptConflict("layer replay receipt group_index must be a non-negative integer")
    return (
        Path("runs") / claim.run_id / _RECEIPT_DIRECTORY / f"{claim.claim_id}.group-{group_index}.replay.json"
    ).as_posix()


def _authority_binding(receipt: LayerReplayReceipt) -> str:
    return f"layer-replay:{receipt.claim.claim_id}:{receipt.receipt_digest}"


def _canonical_receipt_bytes(receipt: LayerReplayReceipt) -> bytes:
    if not isinstance(receipt, LayerReplayReceipt):
        raise LayerReplayReceiptConflict("receipt must be a typed layer replay receipt")
    try:
        return canonical_layer_replay_receipt_bytes(receipt)
    except ValueError as exc:
        raise LayerReplayReceiptConflict(str(exc)) from exc


def capture_layer_replay_causal_sources(
    shot: Path,
    receipt: LayerReplayReceipt,
) -> tuple[TrustedFileBinding, ...]:
    _canonical_receipt_bytes(receipt)
    sources: list[TrustedFileBinding] = []
    seen: set[tuple[str, str]] = set()

    def capture(locator: str, sha256: str, where: str) -> None:
        key = (locator, sha256)
        if key in seen:
            return
        try:
            snapshot = read_trusted_file(
                shot,
                shot / locator,
                where,
                require_nonempty=True,
            )
        except TrustedFileError as exc:
            raise LayerReplayReceiptConflict(str(exc)) from exc
        observed = hashlib.sha256(snapshot.payload).hexdigest()
        if observed != sha256:
            raise LayerReplayReceiptConflict(f"{where} SHA-256 changed; expected={sha256}, observed={observed}")
        sources.append(snapshot.binding)
        seen.add(key)

    for input_index, replay_input in enumerate(receipt.replay_inputs):
        capture(
            replay_input.script_path,
            replay_input.script_sha256,
            f"layer replay input {input_index}",
        )
        for dependency_index, dependency in enumerate(replay_input.dependencies):
            capture(
                dependency.path,
                dependency.sha256,
                f"layer replay input {input_index} dependency {dependency_index}",
            )
    for point_index, point in enumerate(receipt.observation.points):
        capture(
            point.ref,
            point.ref_sha256,
            f"layer replay point {point_index} reference",
        )
        if point.render is not None:
            assert point.render_sha256 is not None
            capture(
                point.render,
                point.render_sha256,
                f"layer replay point {point_index} render",
            )
    for capture_index, auxiliary in enumerate(receipt.observation.auxiliary_captures):
        capture(
            auxiliary["locator"],
            auxiliary["sha256"],
            f"layer replay auxiliary capture {capture_index}",
        )
    return tuple(sources)


def prepare_layer_replay_receipt(
    folder: str | Path,
    receipt: LayerReplayReceipt,
    guard: LayerFinalizationPublicationGuard,
) -> PreparedLayerReplayReceipt:
    """Fsync immutable receipt bytes outside selected/state locks."""

    raw = _canonical_receipt_bytes(receipt)
    sha256 = hashlib.sha256(raw).hexdigest()
    locator = layer_replay_receipt_locator(
        receipt.claim,
        receipt.observation.group_index,
    )
    authority = _authority_binding(receipt)
    shot = _shot_root(folder)
    if guard.claim != receipt.claim:
        raise LayerReplayReceiptConflict("layer replay preparation guard belongs to another claim")
    if _shot_root(guard.folder) != shot:
        raise LayerReplayReceiptConflict("layer replay preparation guard belongs to another shot")
    guard.check("start layer replay receipt preparation")
    expected_locator, destination = _authority_target(shot, receipt)
    if locator != expected_locator:
        raise LayerReplayReceiptConflict("layer replay receipt locator disagrees with its typed replay-group identity")
    causal_sources = capture_layer_replay_causal_sources(shot, receipt)

    def create_only(current: bytes | None) -> tuple[bytes | None, None]:
        if current is None:
            return raw, None
        if current != raw:
            raise FilePublicationConflict("immutable layer replay receipt conflicts with existing bytes")
        return None, None

    def require_commit_authority(
        authorization: object,
        transaction_binding: object,
    ) -> None:
        for source in causal_sources:
            require_trusted_file_unchanged(source, "layer replay causal source")
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
        raise LayerReplayReceiptConflict(str(exc)) from exc
    existing: TrustedFileBinding | None = None
    if prepared.publication is None:
        try:
            existing = read_trusted_file(
                shot,
                shot / locator,
                "immutable layer replay receipt",
                require_nonempty=True,
            ).binding
        except TrustedFileError as exc:
            raise LayerReplayReceiptConflict(str(exc)) from exc
    try:
        return _mint_prepared_layer_replay_receipt(
            shot=shot,
            receipt=receipt,
            locator=locator,
            destination=destination,
            sha256=sha256,
            publication=prepared.publication,
            existing_binding=existing,
            causal_source_bindings=causal_sources,
            authority_binding=authority,
            publication_guard=guard,
        )
    except BaseException:
        discard_prepared_file(prepared.publication)
        raise


def _derive_noop_receipt_binding(
    shot: Path,
    record: _PreparedLayerReplayReceiptRecord,
) -> TrustedFileBinding | None:
    """Reopen and strictly verify an asserted immutable-receipt no-op."""

    if record.publication is not None:
        return None
    stored = load_layer_replay_receipt(
        shot,
        record.locator,
        expected_sha256=record.sha256,
        expected_digest=record.receipt.receipt_digest,
    )
    if stored.receipt != record.receipt:
        raise LayerReplayReceiptConflict("unchanged layer replay receipt bytes disagree with typed authority")
    if stored.source_binding != record.existing_binding:
        raise LayerReplayReceiptConflict(
            "prepared unchanged layer replay receipt audit does not match its rederived destination binding"
        )
    return stored.source_binding


def _commit_layer_replay_receipt(
    shot: Path,
    prepared: PreparedLayerReplayReceipt,
    record: _PreparedLayerReplayReceiptRecord,
    guard: LayerFinalizationPublicationGuard,
    causal_sources: tuple[TrustedFileBinding, ...],
    noop_receipt_binding: TrustedFileBinding | None,
    payload_verification: PreparedFilePayloadVerification | None,
    *,
    authorization: LayerFinalizationPreparedMutationAuthorization,
) -> None:
    """Perform the final receipt CAS under one exact live writer lease."""

    _require_prepared_target(shot, prepared, guard)
    for source in causal_sources:
        require_trusted_file_unchanged(source, "layer replay causal source")
    if record.publication is not None:
        if payload_verification is None:
            raise LayerReplayReceiptConflict(
                "replacement layer replay receipt requires held staged-payload verification"
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
        raise LayerReplayReceiptConflict("unchanged layer replay receipt cannot carry replacement-byte proof")
    if noop_receipt_binding is None or record.existing_binding != noop_receipt_binding:
        raise LayerReplayReceiptConflict(
            "unchanged layer replay receipt requires its rederived exact destination binding"
        )
    require_trusted_file_unchanged(
        noop_receipt_binding,
        "immutable layer replay receipt",
    )
    consume_layer_finalization_prepared_mutation_authorization(
        authorization,
        expected_guard=guard,
        expected_shot=shot,
        expected_claim=guard.claim,
        expected_transaction_binding=prepared,
    )


def commit_layer_replay_receipt(
    shot_folder: str | Path,
    prepared: PreparedLayerReplayReceipt,
    guard: LayerFinalizationPublicationGuard,
) -> StoredLayerReplayReceipt:
    """Publish one prepared replay receipt from its explicit originating shot."""

    if not isinstance(prepared, PreparedLayerReplayReceipt):
        raise LayerReplayReceiptConflict("layer replay commit requires typed prepared receipt")
    record = _require_prepared_layer_replay_receipt(prepared)
    if not isinstance(record.receipt, LayerReplayReceipt):
        raise LayerReplayReceiptConflict("prepared layer replay receipt must retain its typed receipt")
    if guard.claim != record.receipt.claim:
        raise LayerReplayReceiptConflict("layer replay publication guard belongs to another claim")
    if record.publication_guard is not guard:
        raise LayerReplayReceiptConflict("prepared layer replay receipt belongs to another exact finalization guard")
    shot = _require_commit_origin(shot_folder, prepared, guard)
    _require_prepared_target(shot, prepared, guard)
    try:
        causal_sources = capture_layer_replay_causal_sources(
            shot,
            record.receipt,
        )
        if causal_sources != record.causal_source_bindings:
            raise LayerReplayReceiptConflict("prepared layer replay receipt causal-source closure changed")
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
                raise _LayerReplayReceiptMutationConflict(
                    "layer replay finalization guard invoked its prepared mutation more than once"
                )
            try:
                _commit_layer_replay_receipt(
                    shot,
                    prepared,
                    record,
                    guard,
                    causal_sources,
                    noop_receipt_binding,
                    payload_verification,
                    authorization=authorization,
                )
            except LayerReplayReceiptConflict as exc:
                raise _LayerReplayReceiptMutationConflict(str(exc)) from exc
            publication_completed = True

        guard.publish_prepared(
            "publish immutable layer replay receipt",
            prepared,
            publish,
        )
        if publication_calls != 1 or not publication_completed:
            raise _LayerReplayReceiptMutationConflict(
                "layer replay finalization guard returned without completing its exact prepared mutation"
            )
    except (
        FilePublicationConflict,
        TrustedFileError,
        _LayerReplayReceiptMutationConflict,
        LayerFinalizationPreparedMutationConflict,
        shot_authority_capture.AuthoritySelectionConflict,
    ) as exc:
        raise LayerReplayReceiptConflict(str(exc)) from exc
    stored = load_layer_replay_receipt(
        shot,
        record.locator,
        expected_sha256=record.sha256,
        expected_digest=record.receipt.receipt_digest,
    )
    _retire_prepared_layer_replay_receipt(prepared)
    return stored


def discard_layer_replay_receipt(prepared: PreparedLayerReplayReceipt) -> None:
    if not isinstance(prepared, PreparedLayerReplayReceipt):
        return
    record = _require_prepared_layer_replay_receipt(prepared)
    try:
        discard_prepared_file(record.publication)
    except FilePublicationConflict as exc:
        raise LayerReplayReceiptConflict(str(exc)) from exc
    _retire_prepared_layer_replay_receipt(prepared)


def load_layer_replay_receipt(
    folder: str | Path,
    locator: str,
    *,
    expected_sha256: str | None = None,
    expected_digest: str | None = None,
) -> StoredLayerReplayReceipt:
    """Read and strictly verify one immutable replay receipt and its bytes."""

    shot = _shot_root(folder)
    path = shot / locator
    try:
        snapshot = read_trusted_file(
            shot,
            path,
            "immutable layer replay receipt",
            require_nonempty=True,
        )
    except (TrustedFileError, TrustedFileNotFound) as exc:
        raise LayerReplayReceiptConflict(str(exc)) from exc
    sha256 = hashlib.sha256(snapshot.payload).hexdigest()
    if expected_sha256 is not None and sha256 != expected_sha256:
        raise LayerReplayReceiptConflict("immutable layer replay receipt file digest changed")
    try:
        receipt = LayerReplayReceipt.parse(
            json.loads(snapshot.payload),
            "immutable layer replay receipt",
        )
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise LayerReplayReceiptConflict(str(exc)) from exc
    if expected_digest is not None and receipt.receipt_digest != expected_digest:
        raise LayerReplayReceiptConflict("immutable layer replay receipt semantic digest changed")
    return StoredLayerReplayReceipt(
        receipt=receipt,
        locator=locator,
        sha256=sha256,
        source_binding=snapshot.binding,
    )


__all__ = [
    "LayerFinalizationPublicationGuard",
    "LayerReplayReceiptConflict",
    "PreparedLayerReplayReceipt",
    "StoredLayerReplayReceipt",
    "capture_layer_replay_causal_sources",
    "commit_layer_replay_receipt",
    "discard_layer_replay_receipt",
    "layer_replay_receipt_locator",
    "load_layer_replay_receipt",
    "prepare_layer_replay_receipt",
]
