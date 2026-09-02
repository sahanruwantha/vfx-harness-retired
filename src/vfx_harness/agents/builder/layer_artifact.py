"""Prepared publication of one claim-bound composed layer artifact."""

from __future__ import annotations

import hashlib
import os
import threading
import weakref
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import vfx_harness.orchestration.shot_authority_capture as shot_authority_capture
from vfx_harness.agents.builder.composition_helpers import compose_unit_artifact_source
from vfx_harness.agents.builder.layer_finalization_guard import (
    LayerFinalizationClaimGuard,
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
from vfx_harness.orchestration.authority_selection_process_registry import (
    canonical_authority_shot_path,
)
from vfx_harness.orchestration.layer_finalization_publication_authority import (
    LayerFinalizationPreparedMutationAuthorization,
    LayerFinalizationPreparedMutationConflict,
    consume_layer_finalization_prepared_mutation_authorization,
)


class LayerArtifactPublicationConflict(ValueError):
    """The claimed constituent bytes cannot publish this layer artifact."""


class _LayerArtifactMutationConflict(RuntimeError):
    """Carry a typed sink rejection through the guard without reclassification."""


class PreparedLayerArtifact:
    """Opaque exact-object capability for one composed layer artifact."""

    __slots__ = ("__weakref__",)

    def __new__(cls, *_args: Any, **_kwargs: Any) -> PreparedLayerArtifact:
        raise LayerArtifactPublicationConflict(
            "prepared layer artifacts can be minted only from a live finalization claim"
        )

    @property
    def destination(self) -> Path:
        return _require_prepared_layer_artifact(self).destination

    @property
    def relative(self) -> str:
        return _require_prepared_layer_artifact(self).relative

    @property
    def sha256(self) -> str:
        return _require_prepared_layer_artifact(self).sha256

    def __copy__(self) -> PreparedLayerArtifact:
        raise LayerArtifactPublicationConflict(
            "prepared layer artifact capabilities cannot be copied"
        )

    def __deepcopy__(self, _memo: dict[int, Any]) -> PreparedLayerArtifact:
        raise LayerArtifactPublicationConflict(
            "prepared layer artifact capabilities cannot be copied"
        )

    def __reduce_ex__(self, _protocol: int) -> Any:
        raise LayerArtifactPublicationConflict(
            "prepared layer artifact capabilities cannot be serialized"
        )

    def __repr__(self) -> str:
        return "<PreparedLayerArtifact opaque>"


_DESTINATION_ISSUER = _bind_prepared_publication_destination_issuer(
    family="layer-artifact",
    owner_type=PreparedLayerArtifact,
)


@dataclass(frozen=True, slots=True)
class _PreparedLayerArtifactRecord:
    shot: Path
    destination: Path
    relative: str
    sha256: str
    source_bindings: tuple[TrustedFileBinding, ...]
    existing_destination: TrustedFileBinding | None
    publication: PreparedFilePublication | None
    authority_binding: str
    publication_guard: LayerFinalizationClaimGuard
    process_id: int
    process_token: object
    thread_id: int
    thread_token: object


@dataclass(slots=True)
class _PreparedLayerArtifactEntry:
    reference: weakref.ReferenceType[PreparedLayerArtifact]
    record: _PreparedLayerArtifactRecord


_PREPARED_ARTIFACT_LOCK = threading.RLock()
_PREPARED_ARTIFACTS: dict[int, _PreparedLayerArtifactEntry] = {}
_PREPARED_ARTIFACT_PROCESS_TOKEN = object()
_PREPARED_ARTIFACT_THREAD_LOCAL = threading.local()


def _after_prepared_artifact_fork_child() -> None:
    global _PREPARED_ARTIFACT_LOCK
    global _PREPARED_ARTIFACT_PROCESS_TOKEN
    global _PREPARED_ARTIFACT_THREAD_LOCAL

    _PREPARED_ARTIFACTS.clear()
    _PREPARED_ARTIFACT_LOCK = threading.RLock()
    _PREPARED_ARTIFACT_PROCESS_TOKEN = object()
    _PREPARED_ARTIFACT_THREAD_LOCAL = threading.local()


fork_coordination.register_fork_participant(
    "agents.builder.layer_artifact",
    lock_factory=lambda: _PREPARED_ARTIFACT_LOCK,
    after_in_child=_after_prepared_artifact_fork_child,
)


@contextmanager
def _prepared_artifact_locked() -> Iterator[None]:
    with fork_coordination.fork_coordinated_lock(_PREPARED_ARTIFACT_LOCK):
        yield


def _current_prepared_artifact_thread_token() -> object:
    process_id = os.getpid()
    token = getattr(_PREPARED_ARTIFACT_THREAD_LOCAL, "token", None)
    owner_process_id = getattr(_PREPARED_ARTIFACT_THREAD_LOCAL, "process_id", None)
    if token is None or owner_process_id != process_id:
        token = object()
        _PREPARED_ARTIFACT_THREAD_LOCAL.token = token
        _PREPARED_ARTIFACT_THREAD_LOCAL.process_id = process_id
    return token


def _prepared_layer_artifact_gone(
    identifier: int,
    observed: weakref.ReferenceType[PreparedLayerArtifact],
) -> None:
    with _prepared_artifact_locked():
        entry = _PREPARED_ARTIFACTS.get(identifier)
        if entry is not None and entry.reference is observed:
            _PREPARED_ARTIFACTS.pop(identifier, None)


def _require_prepared_layer_artifact(
    prepared: PreparedLayerArtifact,
) -> _PreparedLayerArtifactRecord:
    if type(prepared) is not PreparedLayerArtifact:
        raise LayerArtifactPublicationConflict(
            "layer artifact operation requires an exact prepared capability"
        )
    with _prepared_artifact_locked():
        entry = _PREPARED_ARTIFACTS.get(id(prepared))
        if entry is None or entry.reference() is not prepared:
            raise LayerArtifactPublicationConflict(
                "prepared layer artifact is unregistered, expired, consumed, or copied"
            )
        record = entry.record
        if (
            record.process_id != os.getpid()
            or record.process_token is not _PREPARED_ARTIFACT_PROCESS_TOKEN
            or record.thread_id != threading.get_ident()
            or record.thread_token is not _current_prepared_artifact_thread_token()
        ):
            raise LayerArtifactPublicationConflict(
                "prepared layer artifact belongs to another process or thread"
            )
        return record


def _mint_prepared_layer_artifact(
    *,
    shot: Path,
    destination: Path,
    relative: str,
    sha256: str,
    source_bindings: tuple[TrustedFileBinding, ...],
    existing_destination: TrustedFileBinding | None,
    publication: PreparedFilePublication | None,
    authority_binding: str,
    publication_guard: LayerFinalizationClaimGuard,
) -> PreparedLayerArtifact:
    prepared = object.__new__(PreparedLayerArtifact)
    identifier = id(prepared)
    reference = weakref.ref(
        prepared,
        lambda observed, key=identifier: _prepared_layer_artifact_gone(
            key,
            observed,
        ),
    )
    record = _PreparedLayerArtifactRecord(
        shot=shot,
        destination=destination,
        relative=relative,
        sha256=sha256,
        source_bindings=source_bindings,
        existing_destination=existing_destination,
        publication=publication,
        authority_binding=authority_binding,
        publication_guard=publication_guard,
        process_id=os.getpid(),
        process_token=_PREPARED_ARTIFACT_PROCESS_TOKEN,
        thread_id=threading.get_ident(),
        thread_token=_current_prepared_artifact_thread_token(),
    )
    with _prepared_artifact_locked():
        if identifier in _PREPARED_ARTIFACTS:  # pragma: no cover - live id guarantee
            raise LayerArtifactPublicationConflict(
                "prepared layer artifact identity collided with a live capability"
            )
        _PREPARED_ARTIFACTS[identifier] = _PreparedLayerArtifactEntry(
            reference,
            record,
        )
    return prepared


def _retire_prepared_layer_artifact(
    prepared: PreparedLayerArtifact,
) -> _PreparedLayerArtifactRecord:
    record = _require_prepared_layer_artifact(prepared)
    with _prepared_artifact_locked():
        entry = _PREPARED_ARTIFACTS.get(id(prepared))
        if entry is None or entry.reference() is not prepared:
            raise LayerArtifactPublicationConflict(
                "prepared layer artifact is unregistered, expired, consumed, or copied"
            )
        _PREPARED_ARTIFACTS.pop(id(prepared), None)
    return record


def _absolute_shot(folder: str | Path, label: str) -> Path:
    try:
        return canonical_authority_shot_path(folder)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise LayerArtifactPublicationConflict(f"{label} must name one canonical shot root") from exc


def _require_preparation_shot(
    record: _PreparedLayerArtifactRecord,
    guard: LayerFinalizationClaimGuard,
) -> Path:
    if not isinstance(record.shot, Path):
        raise LayerArtifactPublicationConflict("prepared layer artifact shot binding must be a canonical absolute Path")
    canonical_shot = _absolute_shot(
        record.shot,
        "prepared layer artifact shot binding",
    )
    if record.shot != canonical_shot:
        raise LayerArtifactPublicationConflict("prepared layer artifact shot binding is not canonical and absolute")
    guard_shot = _absolute_shot(
        guard.folder,
        "layer finalization guard shot binding",
    )
    if canonical_shot != guard_shot:
        raise LayerArtifactPublicationConflict("prepared layer artifact belongs to another shot")
    expected_destination = canonical_shot / record.relative
    if not isinstance(record.destination, Path) or record.destination != expected_destination:
        raise LayerArtifactPublicationConflict(
            "prepared layer artifact destination does not belong to its originating shot"
        )
    if any(binding.root != canonical_shot for binding in record.source_bindings):
        raise LayerArtifactPublicationConflict("prepared layer artifact source binding belongs to another shot")
    publication = record.publication
    existing_destination = record.existing_destination
    try:
        if publication is None:
            if (
                not isinstance(existing_destination, TrustedFileBinding)
                or existing_destination.root != canonical_shot
                or existing_destination.path != record.destination
                or existing_destination.relative != record.relative
            ):
                raise LayerArtifactPublicationConflict(
                    "unchanged prepared layer artifact requires its exact destination binding"
                )
        elif existing_destination is not None:
            raise LayerArtifactPublicationConflict(
                "replacement layer artifact cannot also claim an unchanged destination"
            )
        elif (
            publication.shot != canonical_shot
            or publication.destination != record.destination
            or publication.relative_path != Path(record.relative)
        ):
            raise LayerArtifactPublicationConflict(
                "prepared layer artifact publication target belongs to another shot"
            )
    except FilePublicationConflict as exc:
        raise LayerArtifactPublicationConflict(str(exc)) from exc
    return canonical_shot


def _require_commit_shot(
    folder: str | Path,
    record: _PreparedLayerArtifactRecord,
    guard: LayerFinalizationClaimGuard,
) -> Path:
    shot = _absolute_shot(folder, "layer artifact commit shot")
    prepared_shot = _require_preparation_shot(record, guard)
    if shot != prepared_shot:
        raise LayerArtifactPublicationConflict("layer artifact commit folder does not match its originating shot")
    return shot


def _composed_payload(
    shot: Path,
    inputs: Iterable[tuple[str, str, str | None]],
    *,
    evaluation_barrier: str,
) -> tuple[bytes, tuple[TrustedFileBinding, ...]]:
    parts: list[tuple[str, str, str]] = []
    bindings: list[TrustedFileBinding] = []
    for index, (unit_id, script_path, expected_sha256) in enumerate(inputs):
        try:
            source = read_trusted_file(
                shot,
                shot / script_path,
                f"layer finalization unit input {index}",
                require_nonempty=True,
            )
        except TrustedFileError as exc:
            raise LayerArtifactPublicationConflict(str(exc)) from exc
        observed_sha256 = hashlib.sha256(source.payload).hexdigest()
        if expected_sha256 is not None and observed_sha256 != expected_sha256:
            raise LayerArtifactPublicationConflict(f"layer finalization unit input changed: {script_path}")
        try:
            text = source.payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise LayerArtifactPublicationConflict(
                f"layer finalization unit input is not UTF-8: {script_path}"
            ) from exc
        parts.append((unit_id, script_path, text))
        bindings.append(source.binding)
    payload = compose_unit_artifact_source(
        parts,
        evaluation_barrier=evaluation_barrier,
    ).encode("utf-8")
    return payload, tuple(bindings)


def proposed_layer_artifact_sha256(
    folder: str | Path,
    unit_inputs: Iterable[tuple[str, str]],
    *,
    evaluation_barrier: str,
) -> str:
    """Safely derive the composed bytes that a future claim will bind."""

    shot = _absolute_shot(folder, "layer artifact proposal shot")
    payload, bindings = _composed_payload(
        shot,
        ((str(unit_id), str(script_path), None) for unit_id, script_path in unit_inputs),
        evaluation_barrier=evaluation_barrier,
    )
    try:
        for binding in bindings:
            require_trusted_file_unchanged(
                binding,
                "proposed composed layer unit input",
            )
    except TrustedFileError as exc:
        raise LayerArtifactPublicationConflict(str(exc)) from exc
    return hashlib.sha256(payload).hexdigest()


def _require_source_bindings_current(
    bindings: tuple[TrustedFileBinding, ...],
) -> None:
    try:
        for binding in bindings:
            require_trusted_file_unchanged(binding, "composed layer unit input")
    except TrustedFileError as exc:
        raise LayerArtifactPublicationConflict(str(exc)) from exc


def _derive_claim_source_bindings(
    shot: Path,
    guard: LayerFinalizationClaimGuard,
) -> tuple[TrustedFileBinding, ...]:
    """Reopen the exact source closure named by the live typed claim."""

    bindings: list[TrustedFileBinding] = []
    for index, row in enumerate(guard.claim.unit_inputs):
        try:
            source = read_trusted_file(
                shot,
                shot / row.script_path,
                f"claimed composed layer unit input {index}",
                require_nonempty=True,
            )
        except TrustedFileError as exc:
            raise LayerArtifactPublicationConflict(str(exc)) from exc
        if source.sha256 != row.script_sha256:
            raise LayerArtifactPublicationConflict(
                f"claimed composed layer unit input changed after finalization claim: {row.script_path}"
            )
        bindings.append(source.binding)
    return tuple(bindings)


def _require_prepared_source_audit(
    record: _PreparedLayerArtifactRecord,
    claimed_bindings: tuple[TrustedFileBinding, ...],
) -> None:
    if record.source_bindings != claimed_bindings:
        raise LayerArtifactPublicationConflict(
            "prepared layer artifact source audit does not exactly match the "
            "unit closure rederived from its finalization claim"
        )


def _derive_noop_destination_binding(
    shot: Path,
    record: _PreparedLayerArtifactRecord,
    guard: LayerFinalizationClaimGuard,
) -> TrustedFileBinding | None:
    """Reopen and hash an asserted no-op destination from typed claim authority."""

    if record.publication is not None:
        return None
    try:
        existing = read_trusted_file(
            shot,
            shot / guard.claim.layer_script_path,
            "claimed unchanged composed layer artifact",
            require_nonempty=True,
        )
    except TrustedFileError as exc:
        raise LayerArtifactPublicationConflict(str(exc)) from exc
    if existing.sha256 != guard.claim.layer_script_sha256:
        raise LayerArtifactPublicationConflict(
            "unchanged composed layer artifact bytes do not match its finalization claim"
        )
    if existing.binding != record.existing_destination:
        raise LayerArtifactPublicationConflict(
            "prepared unchanged layer artifact audit does not match its rederived destination binding"
        )
    return existing.binding


def _require_prepared_claim(
    record: _PreparedLayerArtifactRecord,
    guard: LayerFinalizationClaimGuard,
) -> None:
    _require_preparation_shot(record, guard)
    claim = guard.claim
    if record.relative != claim.layer_script_path:
        raise LayerArtifactPublicationConflict("prepared layer artifact path does not match its finalization claim")
    if record.sha256 != claim.layer_script_sha256:
        raise LayerArtifactPublicationConflict(
            "prepared layer artifact SHA-256 does not match the proposed composed bytes bound by its finalization claim"
        )
    expected_authority = f"layer-artifact:{claim.claim_id}:{claim.layer_script_sha256}"
    if record.authority_binding != expected_authority:
        raise LayerArtifactPublicationConflict(
            "prepared layer artifact authority does not match its finalization claim"
        )
    if record.publication_guard is not guard:
        raise LayerArtifactPublicationConflict(
            "prepared layer artifact belongs to another exact finalization guard"
        )


def prepare_layer_artifact(
    folder: str | Path,
    guard: LayerFinalizationClaimGuard,
    *,
    evaluation_barrier: str,
) -> PreparedLayerArtifact:
    """Read exact unit bytes and fsync the composed artifact outside claim locks."""

    shot = _absolute_shot(folder, "layer artifact preparation shot")
    guard_shot = _absolute_shot(
        guard.folder,
        "layer finalization guard shot binding",
    )
    if shot != guard_shot:
        raise LayerArtifactPublicationConflict(
            "layer artifact preparation folder does not match its finalization guard shot"
        )
    guard.check("start composed layer artifact preparation")
    payload, bindings = _composed_payload(
        shot,
        ((row.unit_id, row.script_path, row.script_sha256) for row in guard.claim.unit_inputs),
        evaluation_barrier=evaluation_barrier,
    )
    sha256 = hashlib.sha256(payload).hexdigest()
    if sha256 != guard.claim.layer_script_sha256:
        raise LayerArtifactPublicationConflict(
            "composed layer artifact SHA-256 does not match the proposed bytes bound by its finalization claim"
        )
    authority = f"layer-artifact:{guard.claim.claim_id}:{sha256}"

    def replace_if_needed(current: bytes | None) -> tuple[bytes | None, None]:
        if current == payload:
            return None, None
        return payload, None

    def require_commit_authority(
        authorization: object,
        transaction_binding: object,
    ) -> None:
        _require_source_bindings_current(bindings)
        consume_layer_finalization_prepared_mutation_authorization(
            authorization,
            expected_guard=guard,
            expected_shot=shot,
            expected_claim=guard.claim,
            expected_transaction_binding=transaction_binding,
        )

    try:
        update = prepare_file_update(
            shot,
            guard.claim.layer_script_path,
            replace_if_needed,
            authority_binding=authority,
            commit_policy=require_commit_authority,
            destination_authorization=(
                _issue_prepared_publication_destination_authorization(
                    issuer=_DESTINATION_ISSUER,
                    shot_folder=shot,
                    destination=guard.claim.layer_script_path,
                )
            ),
        )
    except FilePublicationConflict as exc:
        raise LayerArtifactPublicationConflict(str(exc)) from exc
    existing_destination: TrustedFileBinding | None = None
    if update.publication is None:
        try:
            existing = read_trusted_file(
                shot,
                shot / guard.claim.layer_script_path,
                "unchanged composed layer artifact",
                require_nonempty=True,
            )
        except TrustedFileError as exc:
            raise LayerArtifactPublicationConflict(str(exc)) from exc
        if existing.sha256 != sha256:
            raise LayerArtifactPublicationConflict("unchanged composed layer artifact bytes changed during preparation")
        existing_destination = existing.binding
    try:
        prepared = _mint_prepared_layer_artifact(
            shot=shot,
            destination=shot / guard.claim.layer_script_path,
            relative=guard.claim.layer_script_path,
            sha256=sha256,
            source_bindings=bindings,
            existing_destination=existing_destination,
            publication=update.publication,
            authority_binding=authority,
            publication_guard=guard,
        )
        record = _require_prepared_layer_artifact(prepared)
        _require_prepared_claim(record, guard)
        _require_source_bindings_current(bindings)
        return prepared
    except BaseException:
        if "prepared" in locals():
            discard_layer_artifact(prepared)
        else:
            discard_prepared_file(update.publication)
        raise


def _commit_layer_artifact_under_writer(
    shot: Path,
    prepared: PreparedLayerArtifact,
    record: _PreparedLayerArtifactRecord,
    guard: LayerFinalizationClaimGuard,
    claimed_bindings: tuple[TrustedFileBinding, ...],
    noop_destination_binding: TrustedFileBinding | None,
    payload_verification: PreparedFilePayloadVerification | None,
    *,
    authorization: LayerFinalizationPreparedMutationAuthorization,
) -> None:
    """Perform the raw artifact mutation under one exact live writer lease."""

    if _require_prepared_layer_artifact(prepared) is not record:
        raise LayerArtifactPublicationConflict(
            "prepared layer artifact registry changed during publication"
        )
    _require_commit_shot(shot, record, guard)
    _require_prepared_claim(record, guard)
    _require_prepared_source_audit(record, claimed_bindings)
    _require_source_bindings_current(claimed_bindings)
    publication = record.publication
    if publication is not None:
        if payload_verification is None:
            raise LayerArtifactPublicationConflict(
                "replacement layer artifact requires held staged-payload verification"
            )
        commit_prepared_file(
            publication,
            authority_binding=record.authority_binding,
            payload_verification=payload_verification,
            transaction_binding=prepared,
            commit_authorization=authorization,
        )
        return

    if payload_verification is not None:
        raise LayerArtifactPublicationConflict("unchanged layer artifact cannot carry replacement payload verification")
    if (
        noop_destination_binding is None
        or record.existing_destination != noop_destination_binding
    ):
        raise LayerArtifactPublicationConflict(
            "unchanged layer artifact requires its rederived exact destination binding"
        )
    try:
        require_trusted_file_unchanged(
            noop_destination_binding,
            "unchanged composed layer artifact",
        )
    except TrustedFileError as exc:
        raise LayerArtifactPublicationConflict(str(exc)) from exc
    consume_layer_finalization_prepared_mutation_authorization(
        authorization,
        expected_guard=guard,
        expected_shot=shot,
        expected_claim=guard.claim,
        expected_transaction_binding=prepared,
    )


def commit_layer_artifact(
    folder: str | Path,
    prepared: PreparedLayerArtifact,
    guard: LayerFinalizationClaimGuard,
) -> Path:
    """CAS-publish prepared bytes under the exact claim and shot writer lease."""

    record = _require_prepared_layer_artifact(prepared)
    shot = _require_commit_shot(folder, record, guard)
    _require_prepared_claim(record, guard)
    claimed_bindings = _derive_claim_source_bindings(shot, guard)
    _require_prepared_source_audit(record, claimed_bindings)
    _require_source_bindings_current(claimed_bindings)
    noop_destination_binding = _derive_noop_destination_binding(
        shot,
        record,
        guard,
    )
    payload_verification: PreparedFilePayloadVerification | None = None
    if record.publication is not None:
        try:
            payload_verification = verify_prepared_file_payload(
                record.publication,
                expected_sha256=guard.claim.layer_script_sha256,
                transaction_binding=prepared,
            )
        except FilePublicationConflict as exc:
            raise LayerArtifactPublicationConflict(str(exc)) from exc

    publication_calls = 0
    publication_completed = False

    def publish(authorization: LayerFinalizationPreparedMutationAuthorization) -> None:
        nonlocal publication_calls, publication_completed
        publication_calls += 1
        if publication_calls != 1:
            raise _LayerArtifactMutationConflict(
                "layer artifact finalization guard invoked its prepared mutation more than once"
            )
        try:
            _commit_layer_artifact_under_writer(
                shot,
                prepared,
                record,
                guard,
                claimed_bindings,
                noop_destination_binding,
                payload_verification,
                authorization=authorization,
            )
        except (
            LayerArtifactPublicationConflict,
            shot_authority_capture.AuthoritySelectionConflict,
        ) as exc:
            raise _LayerArtifactMutationConflict(str(exc)) from exc
        publication_completed = True

    try:
        guard.publish_prepared(
            "publish composed layer artifact",
            prepared,
            publish,
        )
        if publication_calls != 1 or not publication_completed:
            raise _LayerArtifactMutationConflict(
                "layer artifact finalization guard returned without completing its exact prepared mutation"
            )
    except (
        FilePublicationConflict,
        _LayerArtifactMutationConflict,
        LayerFinalizationPreparedMutationConflict,
        shot_authority_capture.AuthoritySelectionConflict,
    ) as exc:
        raise LayerArtifactPublicationConflict(str(exc)) from exc
    _retire_prepared_layer_artifact(prepared)
    return record.destination


def discard_layer_artifact(prepared: PreparedLayerArtifact) -> None:
    if type(prepared) is not PreparedLayerArtifact:
        return
    record = _require_prepared_layer_artifact(prepared)
    try:
        discard_prepared_file(record.publication)
    except FilePublicationConflict as exc:
        raise LayerArtifactPublicationConflict(str(exc)) from exc
    _retire_prepared_layer_artifact(prepared)


__all__ = [
    "LayerArtifactPublicationConflict",
    "PreparedLayerArtifact",
    "commit_layer_artifact",
    "discard_layer_artifact",
    "prepare_layer_artifact",
    "proposed_layer_artifact_sha256",
]
