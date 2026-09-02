"""One-shot authority for prepared publication under a live layer finalization.

The shot writer lease proves only the selected-authority generation.  A prepared
layer publication also needs to prove that its exact claim or terminal-receipt
guard is held at the mutation boundary.  This module owns that short-lived proof
without importing the builder adapter that is its sole production issuer.
"""

from __future__ import annotations

import os
import sys
import threading
import weakref
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import vfx_harness.orchestration.shot_authority_capture as shot_authority_capture
from vfx_harness.domain.layer_finalizations import (
    LayerFinalizationClaim,
    LayerFinalizationReceipt,
)
from vfx_harness.observability import fork_coordination
from vfx_harness.orchestration.authority_selection_process_registry import (
    canonical_authority_shot_path,
    current_process_token,
)


class LayerFinalizationPreparedMutationConflict(ValueError):
    """A prepared mutation is not inside its exact live finalization guard."""


class LayerFinalizationPreparedMutationAuthorization:
    """Opaque one-shot proof of a held finalization guard and shot writer lease."""

    __slots__ = ("__weakref__",)

    def __new__(
        cls,
        *_args: Any,
        **_kwargs: Any,
    ) -> LayerFinalizationPreparedMutationAuthorization:
        raise LayerFinalizationPreparedMutationConflict(
            "layer-finalization prepared-mutation authority is issued only by "
            "the active builder guard"
        )

    def __copy__(self) -> LayerFinalizationPreparedMutationAuthorization:
        raise LayerFinalizationPreparedMutationConflict(
            "layer-finalization prepared-mutation authority cannot be copied"
        )

    def __deepcopy__(
        self,
        _memo: dict[int, Any],
    ) -> LayerFinalizationPreparedMutationAuthorization:
        raise LayerFinalizationPreparedMutationConflict(
            "layer-finalization prepared-mutation authority cannot be copied"
        )

    def __reduce_ex__(self, _protocol: int) -> Any:
        raise LayerFinalizationPreparedMutationConflict(
            "layer-finalization prepared-mutation authority cannot be serialized"
        )

    def __repr__(self) -> str:
        return "<LayerFinalizationPreparedMutationAuthorization opaque>"


class _LayerFinalizationPreparedMutationIssuer:
    """Unforgeable-in-process mint authority retained by the builder guard module."""

    __slots__ = ()

    def __new__(
        cls,
        *_args: Any,
        **_kwargs: Any,
    ) -> _LayerFinalizationPreparedMutationIssuer:
        raise LayerFinalizationPreparedMutationConflict(
            "layer-finalization prepared-mutation issuer is bound only by "
            "the concrete builder guard module"
        )


@dataclass(frozen=True, slots=True)
class _PreparedMutationIssuerBinding:
    issuer: _LayerFinalizationPreparedMutationIssuer
    claim_guard_type: type
    receipt_guard_type: type


@dataclass(frozen=True, slots=True)
class _PreparedMutationRecord:
    guard: object
    guard_type: type
    selected_authority: object
    shot: Path
    claim: LayerFinalizationClaim
    receipt: LayerFinalizationReceipt | None
    writer_capability: shot_authority_capture.ShotAuthorityWriterCapability
    transaction_binding: object
    hold_token: object
    process_id: int
    process_token: str
    thread_id: int
    thread_token: object


@dataclass(frozen=True, slots=True)
class _PreparedMutationEntry:
    reference: weakref.ReferenceType[LayerFinalizationPreparedMutationAuthorization]
    record: _PreparedMutationRecord


_REGISTRY_LOCK = threading.RLock()
_REGISTRY: dict[int, _PreparedMutationEntry] = {}
_THREAD_LOCAL = threading.local()
_ACTIVE_HOLD_LOCAL = threading.local()
_ISSUER_LOCK = threading.RLock()
_ISSUER_BINDING: _PreparedMutationIssuerBinding | None = None
_BUILDER_GUARD_MODULE = "vfx_harness.agents.builder.layer_finalization_guard"


@contextmanager
def _registry_locked() -> Iterator[None]:
    with fork_coordination.fork_coordinated_lock(_REGISTRY_LOCK):
        yield


@contextmanager
def _issuer_locked() -> Iterator[None]:
    with fork_coordination.fork_coordinated_lock(_ISSUER_LOCK):
        yield


def _thread_token() -> object:
    token = getattr(_THREAD_LOCAL, "prepared_mutation_token", None)
    if token is None:
        token = object()
        _THREAD_LOCAL.prepared_mutation_token = token
    return token


def _after_fork_child() -> None:
    global _ACTIVE_HOLD_LOCAL, _ISSUER_LOCK, _REGISTRY_LOCK, _THREAD_LOCAL

    _REGISTRY.clear()
    _REGISTRY_LOCK = threading.RLock()
    _ISSUER_LOCK = threading.RLock()
    _THREAD_LOCAL = threading.local()
    _ACTIVE_HOLD_LOCAL = threading.local()


fork_coordination.register_fork_participant(
    "orchestration.layer_finalization_publication_authority.issuer",
    lock_factory=lambda: _ISSUER_LOCK,
    after_in_child=lambda: None,
)
fork_coordination.register_fork_participant(
    "orchestration.layer_finalization_publication_authority.registry",
    lock_factory=lambda: _REGISTRY_LOCK,
    after_in_child=_after_fork_child,
)


def _authorization_gone(
    identifier: int,
    observed: weakref.ReferenceType[LayerFinalizationPreparedMutationAuthorization],
) -> None:
    with _registry_locked():
        entry = _REGISTRY.get(identifier)
        if entry is not None and entry.reference is observed:
            _REGISTRY.pop(identifier, None)


def _guard_selected_authority(guard: object) -> object:
    try:
        selected_authority = guard.selected_authority  # type: ignore[attr-defined]
    except AttributeError as exc:
        raise LayerFinalizationPreparedMutationConflict(
            "layer-finalization prepared-mutation guard has no selected authority"
        ) from exc
    return selected_authority


def _bind_layer_finalization_prepared_mutation_issuer(
    *,
    claim_guard_type: type,
    receipt_guard_type: type,
) -> _LayerFinalizationPreparedMutationIssuer:
    """Bind the sole issuer to the two concrete classes owned by the builder module."""

    owner_module = sys.modules.get(_BUILDER_GUARD_MODULE)
    if (
        owner_module is None
        or getattr(owner_module, "LayerFinalizationClaimGuard", None)
        is not claim_guard_type
        or getattr(owner_module, "LayerFinalizationReceiptGuard", None)
        is not receipt_guard_type
        or claim_guard_type.__module__ != _BUILDER_GUARD_MODULE
        or claim_guard_type.__qualname__ != "LayerFinalizationClaimGuard"
        or receipt_guard_type.__module__ != _BUILDER_GUARD_MODULE
        or receipt_guard_type.__qualname__ != "LayerFinalizationReceiptGuard"
    ):
        raise LayerFinalizationPreparedMutationConflict(
            "prepared-mutation issuer binding requires the exact concrete builder guard classes"
        )
    with _issuer_locked():
        global _ISSUER_BINDING

        if _ISSUER_BINDING is not None:
            raise LayerFinalizationPreparedMutationConflict(
                "prepared-mutation issuer is already bound for this process"
            )
        issuer = object.__new__(_LayerFinalizationPreparedMutationIssuer)
        _ISSUER_BINDING = _PreparedMutationIssuerBinding(
            issuer=issuer,
            claim_guard_type=claim_guard_type,
            receipt_guard_type=receipt_guard_type,
        )
        return issuer


def _require_issuer_guard_type(
    issuer: _LayerFinalizationPreparedMutationIssuer,
    guard: object,
    receipt: LayerFinalizationReceipt | None,
) -> _PreparedMutationIssuerBinding:
    with _issuer_locked():
        binding = _ISSUER_BINDING
        if (
            binding is None
            or type(issuer) is not _LayerFinalizationPreparedMutationIssuer
            or binding.issuer is not issuer
        ):
            raise LayerFinalizationPreparedMutationConflict(
                "prepared-mutation mint requires the builder guard's exact private issuer"
            )
        expected_guard_type = (
            binding.claim_guard_type
            if receipt is None
            else binding.receipt_guard_type
        )
        if type(guard) is not expected_guard_type:
            raise LayerFinalizationPreparedMutationConflict(
                "prepared-mutation mint requires the exact concrete claim or receipt guard"
            )
        return binding


def _require_guard_binding(
    guard: object,
    *,
    shot: Path,
    claim: LayerFinalizationClaim,
    receipt: LayerFinalizationReceipt | None,
) -> object:
    try:
        guard_shot = canonical_authority_shot_path(guard.folder)  # type: ignore[attr-defined]
        guard_claim = guard.claim  # type: ignore[attr-defined]
    except AttributeError as exc:
        raise LayerFinalizationPreparedMutationConflict(
            "layer-finalization prepared-mutation guard is missing its shot or claim"
        ) from exc
    if guard_shot != shot or guard_claim is not claim:
        raise LayerFinalizationPreparedMutationConflict(
            "layer-finalization prepared-mutation guard does not bind the exact shot and claim"
        )
    if receipt is None:
        if hasattr(guard, "receipt"):
            raise LayerFinalizationPreparedMutationConflict(
                "claim prepared-mutation authority requires a concrete claim guard"
            )
    else:
        if receipt.claim is not claim or getattr(guard, "receipt", None) is not receipt:
            raise LayerFinalizationPreparedMutationConflict(
                "terminal prepared-mutation authority requires its exact receipt guard"
            )
    return _guard_selected_authority(guard)


@dataclass(frozen=True, slots=True)
class _ActiveFinalizationHold:
    guard: object
    guard_type: type
    selected_authority: object
    shot: Path
    claim: LayerFinalizationClaim
    receipt: LayerFinalizationReceipt | None
    hold_token: object
    process_id: int
    process_token: str
    thread_id: int
    thread_token: object


def _active_hold_stack() -> list[_ActiveFinalizationHold]:
    stack = getattr(_ACTIVE_HOLD_LOCAL, "stack", None)
    if stack is None:
        stack = []
        _ACTIVE_HOLD_LOCAL.stack = stack
    return stack


def _require_active_finalization_hold(
    guard: object,
    *,
    shot: Path,
    claim: LayerFinalizationClaim,
    receipt: LayerFinalizationReceipt | None,
) -> _ActiveFinalizationHold:
    selected_authority = _require_guard_binding(
        guard,
        shot=shot,
        claim=claim,
        receipt=receipt,
    )
    for active in reversed(_active_hold_stack()):
        if (
            active.guard is guard
            and active.guard_type is type(guard)
            and active.selected_authority is selected_authority
            and active.shot == shot
            and active.claim is claim
            and active.receipt is receipt
            and active.process_id == os.getpid()
            and active.process_token == current_process_token()
            and active.thread_id == threading.get_ident()
            and active.thread_token is _thread_token()
        ):
            return active
    raise LayerFinalizationPreparedMutationConflict(
        "prepared-mutation authority requires its exact finalization guard to be actively held"
    )


@contextmanager
def _hold_layer_finalization_prepared_mutation_guard(
    *,
    issuer: _LayerFinalizationPreparedMutationIssuer,
    guard: object,
    shot_folder: str | Path,
    claim: LayerFinalizationClaim,
    receipt: LayerFinalizationReceipt | None,
) -> Iterator[None]:
    """Register the concrete guard only while its underlying state hold is live."""

    _require_issuer_guard_type(issuer, guard, receipt)
    try:
        shot = canonical_authority_shot_path(shot_folder)
    except (OSError, TypeError, ValueError) as exc:
        raise LayerFinalizationPreparedMutationConflict(
            "active prepared-mutation guard requires one canonical shot root"
        ) from exc
    selected_authority = _require_guard_binding(
        guard,
        shot=shot,
        claim=claim,
        receipt=receipt,
    )
    active = _ActiveFinalizationHold(
        guard=guard,
        guard_type=type(guard),
        selected_authority=selected_authority,
        shot=shot,
        claim=claim,
        receipt=receipt,
        hold_token=object(),
        process_id=os.getpid(),
        process_token=current_process_token(),
        thread_id=threading.get_ident(),
        thread_token=_thread_token(),
    )
    stack = _active_hold_stack()
    stack.append(active)
    try:
        yield
    finally:
        if not stack or stack[-1] is not active:
            raise LayerFinalizationPreparedMutationConflict(
                "active finalization guard hold stack changed before release"
            )
        stack.pop()


def _mint_layer_finalization_prepared_mutation_authorization(
    *,
    issuer: _LayerFinalizationPreparedMutationIssuer,
    guard: object,
    shot_folder: str | Path,
    claim: LayerFinalizationClaim,
    receipt: LayerFinalizationReceipt | None,
    writer_capability: shot_authority_capture.ShotAuthorityWriterCapability,
    transaction_binding: object,
) -> LayerFinalizationPreparedMutationAuthorization:
    """Mint one authorization after the caller has entered both owning guards."""

    _require_issuer_guard_type(issuer, guard, receipt)
    if not isinstance(claim, LayerFinalizationClaim):
        raise LayerFinalizationPreparedMutationConflict(
            "prepared-mutation authority requires a typed finalization claim"
        )
    if receipt is not None and not isinstance(receipt, LayerFinalizationReceipt):
        raise LayerFinalizationPreparedMutationConflict(
            "terminal prepared-mutation authority requires a typed finalization receipt"
        )
    if transaction_binding is None:
        raise LayerFinalizationPreparedMutationConflict(
            "prepared-mutation authority requires one exact transaction binding"
        )
    try:
        shot = shot_authority_capture.require_live_shot_authority_writer(
            writer_capability,
            shot_folder,
        )
    except (OSError, TypeError, ValueError) as exc:
        raise LayerFinalizationPreparedMutationConflict(str(exc)) from exc
    selected_authority = _require_guard_binding(
        guard,
        shot=shot,
        claim=claim,
        receipt=receipt,
    )
    active_hold = _require_active_finalization_hold(
        guard,
        shot=shot,
        claim=claim,
        receipt=receipt,
    )
    authorization = object.__new__(
        LayerFinalizationPreparedMutationAuthorization
    )
    identifier = id(authorization)
    reference = weakref.ref(
        authorization,
        lambda observed, key=identifier: _authorization_gone(key, observed),
    )
    record = _PreparedMutationRecord(
        guard=guard,
        guard_type=type(guard),
        selected_authority=selected_authority,
        shot=shot,
        claim=claim,
        receipt=receipt,
        writer_capability=writer_capability,
        transaction_binding=transaction_binding,
        hold_token=active_hold.hold_token,
        process_id=os.getpid(),
        process_token=current_process_token(),
        thread_id=threading.get_ident(),
        thread_token=_thread_token(),
    )
    with _registry_locked():
        if identifier in _REGISTRY:  # pragma: no cover - live object identity
            raise LayerFinalizationPreparedMutationConflict(
                "prepared-mutation authorization identity collided with a live authorization"
            )
        _REGISTRY[identifier] = _PreparedMutationEntry(reference, record)
    return authorization


def _invalidate_layer_finalization_prepared_mutation_authorization(
    authorization: LayerFinalizationPreparedMutationAuthorization,
) -> None:
    """Invalidate an unconsumed authorization before either owning guard exits."""

    with _registry_locked():
        entry = _REGISTRY.get(id(authorization))
        if entry is None:
            return
        if entry.reference() is not authorization:
            raise LayerFinalizationPreparedMutationConflict(
                "prepared-mutation authorization registry identity changed"
            )
        _REGISTRY.pop(id(authorization), None)


@contextmanager
def _issue_layer_finalization_prepared_mutation_authorization(
    *,
    issuer: _LayerFinalizationPreparedMutationIssuer,
    guard: object,
    shot_folder: str | Path,
    claim: LayerFinalizationClaim,
    receipt: LayerFinalizationReceipt | None,
    writer_capability: shot_authority_capture.ShotAuthorityWriterCapability,
    transaction_binding: object,
) -> Iterator[LayerFinalizationPreparedMutationAuthorization]:
    """Issue one callback-scoped authorization and retire it on every exit."""

    authorization = _mint_layer_finalization_prepared_mutation_authorization(
        issuer=issuer,
        guard=guard,
        shot_folder=shot_folder,
        claim=claim,
        receipt=receipt,
        writer_capability=writer_capability,
        transaction_binding=transaction_binding,
    )
    try:
        yield authorization
    finally:
        _invalidate_layer_finalization_prepared_mutation_authorization(
            authorization
        )


def consume_layer_finalization_prepared_mutation_authorization(
    authorization: LayerFinalizationPreparedMutationAuthorization,
    *,
    expected_guard: object,
    expected_shot: str | Path,
    expected_claim: LayerFinalizationClaim,
    expected_receipt: LayerFinalizationReceipt | None = None,
    expected_transaction_binding: object,
) -> None:
    """Consume the sole authorization for one exact prepared publication."""

    if type(authorization) is not LayerFinalizationPreparedMutationAuthorization:
        raise LayerFinalizationPreparedMutationConflict(
            "prepared mutation requires an exact typed finalization authorization"
        )
    if not isinstance(expected_claim, LayerFinalizationClaim):
        raise LayerFinalizationPreparedMutationConflict(
            "prepared mutation requires its typed expected finalization claim"
        )
    if expected_receipt is not None and not isinstance(
        expected_receipt,
        LayerFinalizationReceipt,
    ):
        raise LayerFinalizationPreparedMutationConflict(
            "prepared terminal mutation requires its typed expected receipt"
        )
    try:
        shot = canonical_authority_shot_path(expected_shot)
    except (OSError, TypeError, ValueError) as exc:
        raise LayerFinalizationPreparedMutationConflict(
            "prepared mutation expected shot is not one canonical shot root"
        ) from exc
    with _registry_locked():
        entry = _REGISTRY.get(id(authorization))
        if entry is None or entry.reference() is not authorization:
            raise LayerFinalizationPreparedMutationConflict(
                "prepared-mutation authorization is unregistered, expired, or already consumed"
            )
        record = entry.record
        if (
            record.process_id != os.getpid()
            or record.process_token != current_process_token()
            or record.thread_id != threading.get_ident()
            or record.thread_token is not _thread_token()
        ):
            raise LayerFinalizationPreparedMutationConflict(
                "prepared-mutation authorization belongs to another process or thread"
            )
        if (
            record.guard is not expected_guard
            or type(expected_guard) is not record.guard_type
            or record.shot != shot
            or record.claim is not expected_claim
            or record.receipt is not expected_receipt
            or record.transaction_binding is not expected_transaction_binding
            or _guard_selected_authority(expected_guard)
            is not record.selected_authority
        ):
            raise LayerFinalizationPreparedMutationConflict(
                "prepared-mutation authorization does not bind the exact guard and prepared transaction authority"
            )
        _require_guard_binding(
            expected_guard,
            shot=shot,
            claim=expected_claim,
            receipt=expected_receipt,
        )
        active_hold = _require_active_finalization_hold(
            expected_guard,
            shot=shot,
            claim=expected_claim,
            receipt=expected_receipt,
        )
        if active_hold.hold_token is not record.hold_token:
            raise LayerFinalizationPreparedMutationConflict(
                "prepared-mutation authorization belongs to another active guard hold"
            )
        try:
            shot_authority_capture.require_live_shot_authority_writer(
                record.writer_capability,
                shot,
            )
        except (OSError, TypeError, ValueError) as exc:
            raise LayerFinalizationPreparedMutationConflict(str(exc)) from exc
        _REGISTRY.pop(id(authorization), None)


__all__ = [
    "LayerFinalizationPreparedMutationAuthorization",
    "LayerFinalizationPreparedMutationConflict",
    "consume_layer_finalization_prepared_mutation_authorization",
]
