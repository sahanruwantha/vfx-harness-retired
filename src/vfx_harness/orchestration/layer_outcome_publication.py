"""Typed, staged publication for one sealed layer outcome."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from vfx_harness.domain.authority_head_records import parse_authority_selection_token
from vfx_harness.domain.layer_finalizations import (
    LayerFinalizationClaim,
    LayerFinalizationReceipt,
)
from vfx_harness.domain.layer_outcome_projections import LayerOutcomeProjection
from vfx_harness.domain.layer_outcomes import OUTCOME_SCHEMA
from vfx_harness.domain.stop_envelope_primitives import canonical_digest, require_digest
from vfx_harness.infrastructure.trusted_files import (
    TrustedFileAbsenceBinding,
    TrustedFileBinding,
    TrustedFileError,
    TrustedFileNotFound,
    bind_trusted_file_absence,
    open_pinned_trusted_file,
)
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
from vfx_harness.orchestration.authority_selection_transaction import (
    AuthoritySelectionToken,
)
from vfx_harness.orchestration.layer_finalization_authorizations import (
    AuthorizedLayerFinalizationMutation,
)
from vfx_harness.orchestration.layer_finalization_publication_authority import (
    LayerFinalizationPreparedMutationAuthorization,
    LayerFinalizationPreparedMutationConflict,
    consume_layer_finalization_prepared_mutation_authorization,
)
from vfx_harness.orchestration.layer_outcome_paths import layer_outcome_locator


class LayerOutcomePublicationConflict(ValueError):
    """A prepared layer outcome no longer names its exact causal generation."""


class LayerOutcomeFinalizationGuard(Protocol):
    """Terminal guard surface required by outcome preparation and publication."""

    folder: Path
    receipt: LayerFinalizationReceipt

    @property
    def claim(self) -> LayerFinalizationClaim: ...

    def check(self, operation: str) -> LayerFinalizationReceipt: ...


@dataclass(frozen=True, slots=True)
class LayerOutcomePublicationAuthority:
    """Exact terminal finalization allowed to project one layer outcome."""

    kind: str
    layer_id: str
    layer_digest: str
    selection_token: AuthoritySelectionToken
    receipt: LayerFinalizationReceipt
    finalization_authorization: AuthorizedLayerFinalizationMutation

    def __post_init__(self) -> None:
        if self.kind != "finalization":
            raise LayerOutcomePublicationConflict(
                f"unsupported layer-outcome authority kind: {self.kind!r}"
            )
        if not self.layer_id or self.layer_id != self.layer_id.strip():
            raise LayerOutcomePublicationConflict(
                "layer-outcome authority requires a non-empty trimmed layer id"
            )
        require_digest(self.layer_digest, "layer-outcome layer digest")
        if not isinstance(self.selection_token, AuthoritySelectionToken):
            raise LayerOutcomePublicationConflict(
                "layer-outcome authority requires an exact selection token"
            )
        if not isinstance(self.receipt, LayerFinalizationReceipt):
            raise LayerOutcomePublicationConflict(
                "layer outcome requires an exact terminal finalization receipt"
            )
        authorization = self.finalization_authorization
        if not isinstance(authorization, AuthorizedLayerFinalizationMutation):
            raise LayerOutcomePublicationConflict(
                "layer outcome requires typed current-head finalization authorization"
            )
        current_projection = parse_authority_selection_token(
            self.selection_token.to_dict(),
            "layer-outcome current selection token",
        )
        if self.receipt.claim.layer_id != self.layer_id:
            raise LayerOutcomePublicationConflict(
                "layer-outcome finalization receipt belongs to another layer"
            )
        if (
            authorization.receipt != self.receipt
            or authorization.completion_authorization.selection_token
            != current_projection
        ):
            raise LayerOutcomePublicationConflict(
                "layer-outcome finalization authorization does not bind the exact "
                "receipt and current selection"
            )


@dataclass(frozen=True, slots=True)
class LayerOutcomePathIdentity:
    """Exact real-file identity, preserving authoritative absence."""

    path: Path
    exists: bool
    device: int | None = None
    inode: int | None = None
    size: int | None = None
    modified_ns: int | None = None
    changed_ns: int | None = None
    trusted_file_binding: TrustedFileBinding | None = None
    trusted_absence_binding: TrustedFileAbsenceBinding | None = None


class PreparedLayerOutcomePublication:
    """Opaque exact-object capability for one terminal outcome publication."""

    __slots__ = ("__weakref__",)

    def __new__(cls, *_args: Any, **_kwargs: Any) -> PreparedLayerOutcomePublication:
        raise LayerOutcomePublicationConflict(
            "prepared layer outcomes can be minted only from terminal authority"
        )

    def __getattr__(self, name: str) -> Any:
        record = _require_prepared_layer_outcome(self)
        if name in {
            "shot",
            "authority",
            "sources",
            "authority_binding",
            "payload_sha256",
        }:
            return getattr(record, name)
        if name in {"destination", "temporary", "temporary_identity"}:
            try:
                return getattr(record.publication, name)
            except FilePublicationConflict as exc:
                raise LayerOutcomePublicationConflict(str(exc)) from exc
        raise AttributeError(name)

    def __copy__(self) -> PreparedLayerOutcomePublication:
        raise LayerOutcomePublicationConflict(
            "prepared layer outcome capabilities cannot be copied"
        )

    def __deepcopy__(self, _memo: dict[int, Any]) -> PreparedLayerOutcomePublication:
        raise LayerOutcomePublicationConflict(
            "prepared layer outcome capabilities cannot be copied"
        )

    def __reduce_ex__(self, _protocol: int) -> Any:
        raise LayerOutcomePublicationConflict(
            "prepared layer outcome capabilities cannot be serialized"
        )

    def __repr__(self) -> str:
        return "<PreparedLayerOutcomePublication opaque>"


_DESTINATION_ISSUER = _bind_prepared_publication_destination_issuer(
    family="layer-outcome",
    owner_type=PreparedLayerOutcomePublication,
)


@dataclass(frozen=True, slots=True)
class _PreparedLayerOutcomeRecord:
    shot: Path
    authority: LayerOutcomePublicationAuthority
    publication: PreparedFilePublication
    sources: tuple[LayerOutcomePathIdentity, ...]
    authority_binding: str
    payload_sha256: str
    process_id: int
    process_token: object
    thread_id: int
    thread_token: object


@dataclass(slots=True)
class _PreparedLayerOutcomeEntry:
    reference: weakref.ReferenceType[PreparedLayerOutcomePublication]
    record: _PreparedLayerOutcomeRecord


_PREPARED_OUTCOME_LOCK = threading.RLock()
_PREPARED_OUTCOMES: dict[int, _PreparedLayerOutcomeEntry] = {}
_PREPARED_OUTCOME_PROCESS_TOKEN = object()
_PREPARED_OUTCOME_THREAD_LOCAL = threading.local()


def _before_prepared_outcome_fork() -> None:
    _PREPARED_OUTCOME_LOCK.acquire()


def _after_prepared_outcome_fork_parent() -> None:
    _PREPARED_OUTCOME_LOCK.release()


def _after_prepared_outcome_fork_child() -> None:
    global _PREPARED_OUTCOME_LOCK
    global _PREPARED_OUTCOME_PROCESS_TOKEN
    global _PREPARED_OUTCOME_THREAD_LOCAL

    _PREPARED_OUTCOMES.clear()
    _PREPARED_OUTCOME_LOCK = threading.RLock()
    _PREPARED_OUTCOME_PROCESS_TOKEN = object()
    _PREPARED_OUTCOME_THREAD_LOCAL = threading.local()


os.register_at_fork(
    before=_before_prepared_outcome_fork,
    after_in_parent=_after_prepared_outcome_fork_parent,
    after_in_child=_after_prepared_outcome_fork_child,
)


def _current_prepared_outcome_thread_token() -> object:
    process_id = os.getpid()
    token = getattr(_PREPARED_OUTCOME_THREAD_LOCAL, "token", None)
    owner_process_id = getattr(_PREPARED_OUTCOME_THREAD_LOCAL, "process_id", None)
    if token is None or owner_process_id != process_id:
        token = object()
        _PREPARED_OUTCOME_THREAD_LOCAL.token = token
        _PREPARED_OUTCOME_THREAD_LOCAL.process_id = process_id
    return token


def _prepared_layer_outcome_gone(
    identifier: int,
    observed: weakref.ReferenceType[PreparedLayerOutcomePublication],
) -> None:
    with _PREPARED_OUTCOME_LOCK:
        entry = _PREPARED_OUTCOMES.get(identifier)
        if entry is not None and entry.reference is observed:
            _PREPARED_OUTCOMES.pop(identifier, None)


def _require_prepared_layer_outcome(
    prepared: PreparedLayerOutcomePublication,
) -> _PreparedLayerOutcomeRecord:
    if type(prepared) is not PreparedLayerOutcomePublication:
        raise LayerOutcomePublicationConflict(
            "layer-outcome operation requires an exact prepared capability"
        )
    with _PREPARED_OUTCOME_LOCK:
        entry = _PREPARED_OUTCOMES.get(id(prepared))
        if entry is None or entry.reference() is not prepared:
            raise LayerOutcomePublicationConflict(
                "prepared layer outcome is unregistered, expired, consumed, or copied"
            )
        record = entry.record
        if (
            record.process_id != os.getpid()
            or record.process_token is not _PREPARED_OUTCOME_PROCESS_TOKEN
            or record.thread_id != threading.get_ident()
            or record.thread_token is not _current_prepared_outcome_thread_token()
        ):
            raise LayerOutcomePublicationConflict(
                "prepared layer outcome belongs to another process or thread"
            )
        return record


def _mint_prepared_layer_outcome(
    *,
    shot: Path,
    authority: LayerOutcomePublicationAuthority,
    publication: PreparedFilePublication,
    sources: tuple[LayerOutcomePathIdentity, ...],
    authority_binding: str,
    payload_sha256: str,
) -> PreparedLayerOutcomePublication:
    if shot != _absolute(shot):
        raise LayerOutcomePublicationConflict(
            "prepared layer outcome requires a canonical absolute shot root"
        )
    if not isinstance(authority, LayerOutcomePublicationAuthority):
        raise LayerOutcomePublicationConflict(
            "prepared layer outcome requires typed terminal authority"
        )
    expected, _relative = _authority_outcome_target(shot, authority)
    try:
        if publication.shot != shot or publication.destination != expected:
            raise LayerOutcomePublicationConflict(
                "prepared layer outcome physical target does not match terminal authority"
            )
        if (
            publication.authority_binding != authority_binding
            or publication.payload_sha256 != payload_sha256
        ):
            raise LayerOutcomePublicationConflict(
                "prepared layer outcome physical transaction differs from its audit"
            )
    except FilePublicationConflict as exc:
        raise LayerOutcomePublicationConflict(str(exc)) from exc
    prepared = object.__new__(PreparedLayerOutcomePublication)
    identifier = id(prepared)
    reference = weakref.ref(
        prepared,
        lambda observed, key=identifier: _prepared_layer_outcome_gone(key, observed),
    )
    record = _PreparedLayerOutcomeRecord(
        shot=shot,
        authority=authority,
        publication=publication,
        sources=sources,
        authority_binding=authority_binding,
        payload_sha256=payload_sha256,
        process_id=os.getpid(),
        process_token=_PREPARED_OUTCOME_PROCESS_TOKEN,
        thread_id=threading.get_ident(),
        thread_token=_current_prepared_outcome_thread_token(),
    )
    with _PREPARED_OUTCOME_LOCK:
        if identifier in _PREPARED_OUTCOMES:  # pragma: no cover - live id guarantee
            raise LayerOutcomePublicationConflict(
                "prepared layer outcome identity collided with a live capability"
            )
        _PREPARED_OUTCOMES[identifier] = _PreparedLayerOutcomeEntry(reference, record)
    return prepared


def _retire_prepared_layer_outcome(
    prepared: PreparedLayerOutcomePublication,
) -> _PreparedLayerOutcomeRecord:
    record = _require_prepared_layer_outcome(prepared)
    with _PREPARED_OUTCOME_LOCK:
        entry = _PREPARED_OUTCOMES.get(id(prepared))
        if entry is None or entry.reference() is not prepared:
            raise LayerOutcomePublicationConflict(
                "prepared layer outcome is unregistered, expired, consumed, or copied"
            )
        _PREPARED_OUTCOMES.pop(id(prepared), None)
    return record


PreparedLayerOutcomeVerification = PreparedFilePayloadVerification


def _absolute(path: str | Path) -> Path:
    return Path(os.path.abspath(Path(path).expanduser()))


def _shot_binding(path: str | Path) -> Path:
    if not isinstance(path, (str, Path)) or (isinstance(path, str) and not path):
        raise LayerOutcomePublicationConflict(
            "layer outcome requires a valid shot root path"
        )
    try:
        return _absolute(path)
    except (OSError, TypeError, ValueError) as exc:
        raise LayerOutcomePublicationConflict(
            "layer outcome requires a valid shot root path"
        ) from exc


def _authority_outcome_target(
    shot: Path,
    authority: LayerOutcomePublicationAuthority,
) -> tuple[Path, Path]:
    """Derive the only path one typed finalization authority may publish."""

    if not isinstance(authority, LayerOutcomePublicationAuthority):
        raise LayerOutcomePublicationConflict(
            "layer outcome requires typed finalization publication authority"
        )
    try:
        relative = Path(layer_outcome_locator(authority.layer_id))
    except (TypeError, ValueError) as exc:
        raise LayerOutcomePublicationConflict(
            "layer outcome authority does not name a canonical layer outcome target"
        ) from exc
    return shot / relative, relative


def sealed_layer_outcome_projection(
    finalization_receipt: LayerFinalizationReceipt,
) -> LayerOutcomeProjection:
    """Parse the immutable outcome projection carried by terminal authority."""

    if not isinstance(finalization_receipt, LayerFinalizationReceipt):
        raise LayerOutcomePublicationConflict(
            "sealed layer outcome requires a typed terminal finalization receipt"
        )
    return LayerOutcomeProjection.parse(
        finalization_receipt.projection["outcome"],
        claim=finalization_receipt.claim,
        layer_script_path=finalization_receipt.layer_script_path,
        final_status=finalization_receipt.final_status,
        best=finalization_receipt.projection["best"],
        receipt_canonical=finalization_receipt.canonical,
        blender_version=str(finalization_receipt.projection["blender_version"]),
        where="terminal finalization receipt outcome projection",
    )


def sealed_layer_outcome_record(
    finalization_receipt: LayerFinalizationReceipt,
) -> dict:
    """Reproduce the only mutable outcome record terminal authority can publish."""

    projection = sealed_layer_outcome_projection(finalization_receipt)
    return {
        "schema": OUTCOME_SCHEMA,
        "at": finalization_receipt.completed_at,
        **projection.as_record(),
        "finalization_receipt": finalization_receipt.as_dict(),
    }


def canonical_layer_outcome_payload(
    finalization_receipt: LayerFinalizationReceipt,
) -> bytes:
    """Return byte-stable outcome bytes derived only from terminal authority."""

    return (
        json.dumps(sealed_layer_outcome_record(finalization_receipt), indent=2) + "\n"
    ).encode("utf-8")


def _outcome_authority_binding(authority: LayerOutcomePublicationAuthority) -> str:
    """Bind the physical transaction to the exact terminal authority generation."""

    completion = authority.finalization_authorization.completion_authorization
    return canonical_digest(
        {
            "schema": "vfx-harness.layer-outcome-publication-authority/v1",
            "layer_id": authority.layer_id,
            "layer_digest": authority.layer_digest,
            "selection_token": authority.selection_token.to_dict(),
            "receipt_digest": authority.receipt.receipt_digest,
            "completion_projection_digest": completion.completion_projection_digest,
        }
    )


def _require_prepared_outcome_target(
    prepared: PreparedLayerOutcomePublication,
    authority: LayerOutcomePublicationAuthority,
) -> Path:
    """Bind the opaque physical transaction to terminal outcome authority."""

    record = _require_prepared_layer_outcome(prepared)
    expected, _relative = _authority_outcome_target(record.shot, authority)
    try:
        physical_shot = record.publication.shot
        physical_target = record.publication.destination
        physical_binding = record.publication.authority_binding
        physical_payload = record.publication.payload_sha256
    except FilePublicationConflict as exc:
        raise LayerOutcomePublicationConflict(str(exc)) from exc
    if (
        record.authority is not authority
        or physical_shot != record.shot
        or physical_target != expected
        or physical_binding != record.authority_binding
        or physical_binding != _outcome_authority_binding(authority)
        or physical_payload != record.payload_sha256
    ):
        raise LayerOutcomePublicationConflict(
            "prepared layer outcome does not match its exact authority-derived transaction"
        )
    return expected


def capture_layer_outcome_source_identities(
    paths: tuple[Path, ...],
) -> tuple[LayerOutcomePathIdentity, ...]:
    """Capture exact source lineage without following any symlink component."""

    identities: list[LayerOutcomePathIdentity] = []
    for path in sorted({_absolute(value) for value in paths}, key=lambda value: str(value)):
        where = "layer-outcome causal source"
        try:
            with open_pinned_trusted_file(path.anchor, path, where) as pinned:
                pinned.require_current()
                binding = pinned.binding
        except TrustedFileNotFound:
            try:
                absence = bind_trusted_file_absence(
                    path.anchor,
                    path,
                    where,
                )
            except TrustedFileError as exc:
                raise LayerOutcomePublicationConflict(
                    f"layer-outcome source absence is untrusted: {path}"
                ) from exc
            identities.append(
                LayerOutcomePathIdentity(
                    path=path,
                    exists=False,
                    trusted_absence_binding=absence,
                )
            )
            continue
        except TrustedFileError as exc:
            raise LayerOutcomePublicationConflict(
                f"layer-outcome source is not a trusted regular file: {path}"
            ) from exc
        observed = binding.file_identity
        identities.append(
            LayerOutcomePathIdentity(
                path=path,
                exists=True,
                device=observed.device,
                inode=observed.inode,
                size=observed.size,
                modified_ns=observed.modified_ns,
                changed_ns=observed.changed_ns,
                trusted_file_binding=binding,
            )
        )
    return tuple(identities)


def require_layer_outcome_sources_current(
    identities: tuple[LayerOutcomePathIdentity, ...],
) -> None:
    """Metadata-only CAS for already hashed outcome inputs."""

    observed = capture_layer_outcome_source_identities(
        tuple(identity.path for identity in identities)
    )
    if observed != identities:
        raise LayerOutcomePublicationConflict(
            "layer-outcome causal inputs changed after preparation"
        )


def _authority_source_paths(
    shot: Path,
    authority: LayerOutcomePublicationAuthority,
) -> tuple[Path, ...]:
    """Derive and verify the complete source closure from terminal authority."""

    # Source verification depends on revalidation, which depends on layer_plans and
    # therefore this module. Keep the proven reverse edge local rather than hiding
    # the cycle behind package initialization.
    from vfx_harness.orchestration.layer_outcome_source_verification import (  # noqa: PLC0415
        verify_sealed_layer_outcome_sources,
    )

    paths = verify_sealed_layer_outcome_sources(
        shot,
        sealed_layer_outcome_record(authority.receipt),
        finalization_receipt=authority.receipt,
    )
    return tuple(sorted(set(paths), key=lambda path: str(path)))


def _capture_stable_authority_sources(
    shot: Path,
    authority: LayerOutcomePublicationAuthority,
) -> tuple[LayerOutcomePathIdentity, ...]:
    """Capture an authority-derived closure only when it stays stable end to end."""

    before_paths = _authority_source_paths(shot, authority)
    before = capture_layer_outcome_source_identities(before_paths)
    after_paths = _authority_source_paths(shot, authority)
    after = capture_layer_outcome_source_identities(after_paths)
    if after_paths != before_paths:
        raise LayerOutcomePublicationConflict(
            "layer-outcome causal source closure changed during preparation"
        )
    if after != before:
        raise LayerOutcomePublicationConflict(
            "layer-outcome causal inputs changed during preparation"
        )
    return after


def verify_prepared_layer_outcome(
    shot_folder: str | Path,
    prepared: PreparedLayerOutcomePublication,
    authority: LayerOutcomePublicationAuthority,
) -> PreparedLayerOutcomeVerification:
    """Perform expensive source and payload verification outside finalization hold."""

    if not isinstance(prepared, PreparedLayerOutcomePublication):
        raise LayerOutcomePublicationConflict(
            "layer-outcome verification requires a typed prepared publication"
        )
    record = _require_prepared_layer_outcome(prepared)
    shot = _shot_binding(shot_folder)
    if shot != record.shot:
        raise LayerOutcomePublicationConflict(
            "prepared layer outcome belongs to another shot root"
        )
    if authority is not record.authority:
        raise LayerOutcomePublicationConflict(
            "prepared layer outcome belongs to another authority object"
        )
    _require_prepared_outcome_target(prepared, authority)
    expected_paths = _authority_source_paths(shot, authority)
    if tuple(identity.path for identity in record.sources) != expected_paths:
        raise LayerOutcomePublicationConflict(
            "prepared layer-outcome source closure does not match terminal authority"
        )
    require_layer_outcome_sources_current(record.sources)
    expected_sha256 = hashlib.sha256(
        canonical_layer_outcome_payload(authority.receipt)
    ).hexdigest()
    if expected_sha256 != record.payload_sha256:
        raise LayerOutcomePublicationConflict(
            "prepared layer-outcome audit does not match terminal receipt authority"
        )
    try:
        verification = verify_prepared_file_payload(
            record.publication,
            expected_sha256=expected_sha256,
            transaction_binding=prepared,
        )
    except FilePublicationConflict as exc:
        raise LayerOutcomePublicationConflict(str(exc)) from exc
    require_layer_outcome_sources_current(record.sources)
    return verification


def prepare_layer_outcome_publication(
    shot_folder: str | Path,
    *,
    authority: LayerOutcomePublicationAuthority,
    guard: LayerOutcomeFinalizationGuard,
) -> PreparedLayerOutcomePublication:
    """Derive and fsync the only outcome bytes allowed by terminal authority."""

    shot = _shot_binding(shot_folder)
    output, _relative = _authority_outcome_target(shot, authority)
    payload = canonical_layer_outcome_payload(authority.receipt)
    sources = _capture_stable_authority_sources(shot, authority)
    authority_binding = _outcome_authority_binding(authority)
    if _shot_binding(guard.folder) != shot:
        raise LayerOutcomePublicationConflict(
            "layer-outcome preparation guard belongs to another shot"
        )
    if guard.receipt is not authority.receipt or guard.claim is not authority.receipt.claim:
        raise LayerOutcomePublicationConflict(
            "layer-outcome preparation requires the exact terminal receipt guard"
        )
    guard.check("start layer outcome publication preparation")

    def require_commit_authority(
        authorization: object,
        transaction_binding: object,
    ) -> None:
        require_layer_outcome_sources_current(sources)
        consume_layer_finalization_prepared_mutation_authorization(
            authorization,
            expected_guard=guard,
            expected_shot=shot,
            expected_claim=authority.receipt.claim,
            expected_receipt=authority.receipt,
            expected_transaction_binding=transaction_binding,
        )

    try:
        update = prepare_file_update(
            shot,
            output,
            lambda _current: (payload, None),
            authority_binding=authority_binding,
            commit_policy=require_commit_authority,
            destination_authorization=(
                _issue_prepared_publication_destination_authorization(
                    issuer=_DESTINATION_ISSUER,
                    shot_folder=shot,
                    destination=output,
                )
            ),
        )
    except FilePublicationConflict as exc:
        raise LayerOutcomePublicationConflict(str(exc)) from exc
    if update.publication is None:
        raise LayerOutcomePublicationConflict(
            "layer-outcome preparation must produce an opaque replacement"
        )
    try:
        return _mint_prepared_layer_outcome(
            shot=shot,
            authority=authority,
            publication=update.publication,
            sources=sources,
            authority_binding=authority_binding,
            payload_sha256=hashlib.sha256(payload).hexdigest(),
        )
    except BaseException:
        discard_prepared_file(update.publication)
        raise


def commit_layer_outcome_publication(
    shot_folder: str | Path,
    prepared: PreparedLayerOutcomePublication,
    *,
    authority: LayerOutcomePublicationAuthority,
    authorization: LayerFinalizationPreparedMutationAuthorization,
    verification: PreparedLayerOutcomeVerification,
) -> Path:
    """CAS and rename a prepared outcome; caller holds its bound short authority guard."""

    if not isinstance(prepared, PreparedLayerOutcomePublication):
        raise LayerOutcomePublicationConflict(
            "layer-outcome commit requires a typed prepared publication"
        )
    record = _require_prepared_layer_outcome(prepared)
    shot = _shot_binding(shot_folder)
    if shot != record.shot:
        raise LayerOutcomePublicationConflict(
            "prepared layer outcome belongs to another shot root"
        )
    if authority is not record.authority:
        raise LayerOutcomePublicationConflict(
            "prepared layer outcome belongs to another authority object"
        )
    output = _require_prepared_outcome_target(prepared, authority)
    require_layer_outcome_sources_current(record.sources)
    try:
        commit_prepared_file(
            record.publication,
            authority_binding=record.authority_binding,
            payload_verification=verification,
            transaction_binding=prepared,
            commit_authorization=authorization,
        )
    except (
        FilePublicationConflict,
        LayerFinalizationPreparedMutationConflict,
    ) as exc:
        raise LayerOutcomePublicationConflict(str(exc)) from exc
    _retire_prepared_layer_outcome(prepared)
    return output


def discard_layer_outcome_publication(
    prepared: PreparedLayerOutcomePublication,
) -> None:
    """Consume the exact uncommitted opaque physical publication."""

    if not isinstance(prepared, PreparedLayerOutcomePublication):
        return
    record = _require_prepared_layer_outcome(prepared)
    try:
        discard_prepared_file(record.publication)
    except FilePublicationConflict as exc:
        raise LayerOutcomePublicationConflict(str(exc)) from exc
    _retire_prepared_layer_outcome(prepared)
