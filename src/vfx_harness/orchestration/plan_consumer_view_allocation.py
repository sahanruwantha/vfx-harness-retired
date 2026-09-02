"""Owned directory allocation for isolated plan-consumer views.

The consumer-view mutation owner must not accept an arbitrary caller-created
directory.  A lexical temp name, marker, and absent ``shot.json`` do not prove
that a path is not another shot root.  This module creates the directory itself
below the exact run scratch descriptor and returns an opaque, one-shot receipt
bound to the allocated directory inode.
"""

from __future__ import annotations

import os
import secrets
import threading
import weakref
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vfx_harness.domain.run_ids import require_run_id
from vfx_harness.observability import fork_coordination
from vfx_harness.observability.prepared_publication_descriptors import (
    block_deferred_signals,
)
from vfx_harness.observability.run_artifacts import RunLayout
from vfx_harness.observability.run_owner_fork_guard import (
    ForkProtectedAcquisition,
    managed_fork_protected_acquisition,
)
from vfx_harness.orchestration.plan_consumer_owned_directory import (
    create_and_publish_empty_directory,
)
from vfx_harness.orchestration.plan_consumer_view_cleanup import (
    retire_owned_directory,
)
from vfx_harness.orchestration.plan_consumer_view_descriptors import (
    PlanConsumerDirectoryIdentity,
    PlanConsumerViewMutationConflict,
    absolute_path,
    open_real_directory,
)

_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)


class PreparedPlanConsumerViewAllocation:
    """Opaque proof of one directory allocated by this owner."""

    __slots__ = ("__weakref__",)

    def __new__(
        cls,
        *_args: Any,
        **_kwargs: Any,
    ) -> PreparedPlanConsumerViewAllocation:
        raise PlanConsumerViewMutationConflict(
            "plan-consumer allocation receipts are issued only by the view owner"
        )

    def __copy__(self) -> PreparedPlanConsumerViewAllocation:
        raise PlanConsumerViewMutationConflict(
            "plan-consumer allocation receipts cannot be copied"
        )

    def __deepcopy__(
        self,
        _memo: dict[int, Any],
    ) -> PreparedPlanConsumerViewAllocation:
        raise PlanConsumerViewMutationConflict(
            "plan-consumer allocation receipts cannot be copied"
        )

    def __reduce_ex__(self, _protocol: int) -> Any:
        raise PlanConsumerViewMutationConflict(
            "plan-consumer allocation receipts cannot be serialized"
        )

    def __repr__(self) -> str:
        return "<PreparedPlanConsumerViewAllocation opaque>"


@dataclass(frozen=True, slots=True)
class PlanConsumerViewAllocationClaim:
    """Internal exact record returned only while a receipt is claimed."""

    path: Path
    identity: PlanConsumerDirectoryIdentity
    shot: Path
    scratch: Path
    process_id: int
    process_token: object
    thread_id: int
    thread_token: object


@dataclass(frozen=True, slots=True)
class _AllocationEntry:
    reference: weakref.ReferenceType[PreparedPlanConsumerViewAllocation]
    claim: PlanConsumerViewAllocationClaim
    scope_token: object
    scope_owner: PreparedPlanConsumerViewAllocation | None


@dataclass(slots=True)
class _AllocationScopeEntry:
    shot: Path
    scratch: Path
    name: str | None = None
    identity: PlanConsumerDirectoryIdentity | None = None
    receipt: PreparedPlanConsumerViewAllocation | None = None


_LOCK = threading.RLock()
_PROCESS_TOKEN = object()
_THREAD_LOCAL = threading.local()
_ALLOCATIONS: dict[int, _AllocationEntry] = {}
_CLAIMED: set[int] = set()
_ALLOCATION_SCOPES: dict[object, _AllocationScopeEntry] = {}


def _after_fork_child() -> None:
    global _LOCK, _PROCESS_TOKEN, _THREAD_LOCAL

    _ALLOCATIONS.clear()
    _CLAIMED.clear()
    _ALLOCATION_SCOPES.clear()
    _LOCK = threading.RLock()
    _PROCESS_TOKEN = object()
    _THREAD_LOCAL = threading.local()


fork_coordination.register_fork_participant(
    "orchestration.plan_consumer_view_allocation",
    lock_factory=lambda: _LOCK,
    after_in_child=_after_fork_child,
)


@contextmanager
def _locked() -> Iterator[None]:
    with fork_coordination.fork_coordinated_lock(_LOCK):
        yield


def _thread_token() -> object:
    token = getattr(_THREAD_LOCAL, "token", None)
    if token is None:
        token = object()
        _THREAD_LOCAL.token = token
    return token


def _allocation_gone(
    identifier: int,
    reference: weakref.ReferenceType[PreparedPlanConsumerViewAllocation],
) -> None:
    with _locked():
        entry = _ALLOCATIONS.get(identifier)
        if entry is not None and entry.reference is reference:
            _ALLOCATIONS.pop(identifier, None)
            _CLAIMED.discard(identifier)


def _require_plan_consumer_run_id(layout: RunLayout) -> str:
    if not isinstance(layout, RunLayout):
        raise PlanConsumerViewMutationConflict(
            "plan-consumer work requires a typed run layout"
        )
    try:
        return require_run_id(layout.run_id, "plan-consumer run_id")
    except ValueError as exc:
        raise PlanConsumerViewMutationConflict(str(exc)) from exc


def _canonical_layout_descriptors(
    layout: RunLayout,
    acquisition: ForkProtectedAcquisition,
) -> tuple[int, int, int]:
    """Open and prove the exact ``shot/runs/run_id/scratch`` identity chain."""

    run_id = _require_plan_consumer_run_id(layout)
    shot = absolute_path(layout.shot)
    root = absolute_path(layout.root)
    scratch = absolute_path(layout.scratch)
    runs = shot / "runs"
    if (
        root.parent != runs
        or root.name != run_id
        or scratch != root / "scratch"
    ):
        raise PlanConsumerViewMutationConflict(
            "plan-consumer run root must be the exact canonical "
            "<shot>/runs/<run_id> directory"
        )
    shot_descriptor = open_real_directory(shot, acquisition)
    root_descriptor = open_real_directory(root, acquisition)
    scratch_descriptor = open_real_directory(scratch, acquisition)
    try:
        runs_descriptor = acquisition.open_descriptor(
            lambda: os.open("runs", _DIRECTORY_FLAGS, dir_fd=shot_descriptor)
        )
        run_descriptor = acquisition.open_descriptor(
            lambda: os.open(
                run_id,
                _DIRECTORY_FLAGS,
                dir_fd=runs_descriptor,
            )
        )
        nested_scratch_descriptor = acquisition.open_descriptor(
            lambda: os.open(
                "scratch",
                _DIRECTORY_FLAGS,
                dir_fd=run_descriptor,
            )
        )
        for descriptor in (
            runs_descriptor,
            run_descriptor,
            nested_scratch_descriptor,
        ):
            os.set_inheritable(descriptor, False)
    except OSError as exc:
        raise PlanConsumerViewMutationConflict(
            "plan-consumer run layout is not a real canonical "
            "<shot>/runs/<run_id>/scratch chain"
        ) from exc
    shot_identity = PlanConsumerDirectoryIdentity.capture(shot_descriptor)
    root_identity = PlanConsumerDirectoryIdentity.capture(root_descriptor)
    scratch_identity = PlanConsumerDirectoryIdentity.capture(scratch_descriptor)
    if (
        PlanConsumerDirectoryIdentity.capture(run_descriptor) != root_identity
        or PlanConsumerDirectoryIdentity.capture(nested_scratch_descriptor)
        != scratch_identity
        or len({shot_identity, root_identity, scratch_identity}) != 3
    ):
        raise PlanConsumerViewMutationConflict(
            "plan-consumer run layout physically aliases or escapes its canonical "
            "shot/run/scratch identity chain"
        )
    acquisition.retire(nested_scratch_descriptor)
    acquisition.retire(run_descriptor)
    acquisition.retire(runs_descriptor)
    return shot_descriptor, root_descriptor, scratch_descriptor


def require_canonical_plan_consumer_run_layout(layout: RunLayout) -> None:
    """Fail closed unless a typed layout names its exact canonical run chain."""

    _require_plan_consumer_run_id(layout)
    with managed_fork_protected_acquisition() as acquisition:
        _canonical_layout_descriptors(layout, acquisition)


def _allocate_plan_consumer_view(
    layout: RunLayout,
    scope_token: object,
) -> tuple[Path, PreparedPlanConsumerViewAllocation]:
    """Create one exact scratch child and mint its unforgeable allocation receipt."""

    if not isinstance(layout, RunLayout):
        raise PlanConsumerViewMutationConflict(
            "plan-consumer allocation requires a typed run layout"
        )
    _require_plan_consumer_run_id(layout)
    shot = absolute_path(layout.shot)
    scratch = absolute_path(layout.scratch)
    with _locked():
        scope = _ALLOCATION_SCOPES.get(scope_token)
        if (
            scope is None
            or scope.shot != shot
            or scope.scratch != scratch
            or scope.name is not None
        ):
            raise PlanConsumerViewMutationConflict(
                "plan-consumer allocation has no exact pre-armed owner scope"
            )
    with managed_fork_protected_acquisition() as acquisition:
        _shot_descriptor, _root_descriptor, scratch_descriptor = (
            _canonical_layout_descriptors(layout, acquisition)
        )
        with block_deferred_signals():
            for _attempt in range(32):
                candidate = f".plan-consumer-view.tmp-{secrets.token_hex(16)}"
                with _locked():
                    current_scope = _ALLOCATION_SCOPES.get(scope_token)
                    if current_scope is not scope or scope.name is not None:
                        raise PlanConsumerViewMutationConflict(
                            "plan-consumer allocation owner scope changed before mkdir"
                        )
                    scope.name = candidate
                try:
                    _child_descriptor, created_identity = (
                        create_and_publish_empty_directory(
                            scratch_descriptor,
                            candidate,
                            acquisition,
                            where="plan-consumer root allocation",
                        )
                    )
                except FileExistsError:
                    with _locked():
                        if _ALLOCATION_SCOPES.get(scope_token) is scope:
                            scope.name = None
                    continue
                except OSError as exc:
                    raise PlanConsumerViewMutationConflict(
                        "plan-consumer allocation could not create a directory below "
                        "the exact run scratch"
                    ) from exc
                break
            else:  # pragma: no cover - cryptographic collision bound
                raise PlanConsumerViewMutationConflict(
                    "plan-consumer allocation exhausted unique directory names"
                )

            assert scope.name is not None
            with _locked():
                current_scope = _ALLOCATION_SCOPES.get(scope_token)
                if current_scope is not scope:
                    raise PlanConsumerViewMutationConflict(
                        "plan-consumer allocation owner scope changed during capture"
                    )
                scope.identity = created_identity
            path = scratch / scope.name
            prepared = object.__new__(PreparedPlanConsumerViewAllocation)
            claim = PlanConsumerViewAllocationClaim(
                path=path,
                identity=created_identity,
                shot=shot,
                scratch=scratch,
                process_id=os.getpid(),
                process_token=_PROCESS_TOKEN,
                thread_id=threading.get_ident(),
                thread_token=_thread_token(),
            )
            identifier = id(prepared)
            reference = weakref.ref(
                prepared,
                lambda observed, key=identifier: _allocation_gone(key, observed),
            )
            with _locked():
                current_scope = _ALLOCATION_SCOPES.get(scope_token)
                if current_scope is not scope or scope.identity != created_identity:
                    raise PlanConsumerViewMutationConflict(
                        "plan-consumer allocation owner scope changed before receipt"
                    )
                if identifier in _ALLOCATIONS:  # pragma: no cover - live id guarantee
                    raise PlanConsumerViewMutationConflict(
                        "plan-consumer allocation receipt identity collided"
                    )
                scope.receipt = prepared
                _ALLOCATIONS[identifier] = _AllocationEntry(
                    reference,
                    claim,
                    scope_token,
                    prepared,
                )
            return path, prepared


def _cleanup_allocation_scope(
    layout: RunLayout,
    scope_token: object,
) -> None:
    with _locked():
        scope = _ALLOCATION_SCOPES.get(scope_token)
        if scope is None:
            return
        receipt = scope.receipt
        name = scope.name
        identity = scope.identity
    if receipt is not None:
        discard_plan_consumer_view_allocation(layout, receipt)
        return
    if name is None:
        with _locked():
            if _ALLOCATION_SCOPES.get(scope_token) is scope:
                _ALLOCATION_SCOPES.pop(scope_token, None)
        return
    if identity is None:
        try:
            os.stat(scope.scratch / name, follow_symlinks=False)
        except (FileNotFoundError, NotADirectoryError):
            with _locked():
                if _ALLOCATION_SCOPES.get(scope_token) is scope:
                    _ALLOCATION_SCOPES.pop(scope_token, None)
            return
        except OSError:
            pass
        with _locked():
            if _ALLOCATION_SCOPES.get(scope_token) is scope:
                _ALLOCATION_SCOPES.pop(scope_token, None)
        raise PlanConsumerViewMutationConflict(
            "plan-consumer allocation failed before it captured deletion authority; "
            "the scratch name was preserved for engineering recovery"
        )
    with managed_fork_protected_acquisition() as acquisition:
        _shot_descriptor, _root_descriptor, scratch_descriptor = (
            _canonical_layout_descriptors(layout, acquisition)
        )
        retire_owned_directory(scratch_descriptor, name, identity)
    with _locked():
        if _ALLOCATION_SCOPES.get(scope_token) is scope:
            _ALLOCATION_SCOPES.pop(scope_token, None)


@contextmanager
def allocating_plan_consumer_view(
    layout: RunLayout,
) -> Iterator[tuple[Path, PreparedPlanConsumerViewAllocation]]:
    """Own allocation cleanup before mkdir through caller handoff."""

    if not isinstance(layout, RunLayout):
        raise PlanConsumerViewMutationConflict(
            "plan-consumer allocation requires a typed run layout"
        )
    _require_plan_consumer_run_id(layout)
    scope_token = object()
    shot = absolute_path(layout.shot)
    scratch = absolute_path(layout.scratch)
    with block_deferred_signals(), _locked():
        _ALLOCATION_SCOPES[scope_token] = _AllocationScopeEntry(shot, scratch)
    try:
        path, prepared = _allocate_plan_consumer_view(layout, scope_token)
        with block_deferred_signals(), _locked():
            entry = _ALLOCATIONS.get(id(prepared))
            if (
                entry is None
                or entry.reference() is not prepared
                or entry.scope_token is not scope_token
            ):
                raise PlanConsumerViewMutationConflict(
                    "plan-consumer allocation changed before scoped handoff"
                )
            _ALLOCATIONS[id(prepared)] = _AllocationEntry(
                entry.reference,
                entry.claim,
                entry.scope_token,
                None,
            )
        yield path, prepared
    finally:
        _cleanup_allocation_scope(layout, scope_token)


@contextmanager
def claiming_plan_consumer_view_allocation(
    layout: RunLayout,
    prepared: PreparedPlanConsumerViewAllocation,
) -> Iterator[PlanConsumerViewAllocationClaim]:
    """Hold one claim with commit/abort cleanup armed before registry mutation."""

    if not isinstance(layout, RunLayout):
        raise PlanConsumerViewMutationConflict(
            "plan-consumer allocation claim requires a typed run layout"
        )
    if type(prepared) is not PreparedPlanConsumerViewAllocation:
        raise PlanConsumerViewMutationConflict(
            "plan-consumer construction requires its exact opaque allocation receipt"
        )
    require_canonical_plan_consumer_run_layout(layout)
    identifier = id(prepared)
    claimed = False
    completed = False
    try:
        with block_deferred_signals(), _locked():
            entry = _ALLOCATIONS.get(identifier)
            if entry is None or entry.reference() is not prepared:
                raise PlanConsumerViewMutationConflict(
                    "plan-consumer allocation receipt is unregistered, expired, "
                    "consumed, or forked"
                )
            claim = entry.claim
            if (
                claim.process_id != os.getpid()
                or claim.process_token is not _PROCESS_TOKEN
                or claim.thread_id != threading.get_ident()
                or claim.thread_token is not _thread_token()
                or claim.shot != absolute_path(layout.shot)
                or claim.scratch != absolute_path(layout.scratch)
            ):
                raise PlanConsumerViewMutationConflict(
                    "plan-consumer allocation receipt belongs to another layout, "
                    "process, or thread"
                )
            if identifier in _CLAIMED:
                raise PlanConsumerViewMutationConflict(
                    "plan-consumer allocation receipt already has an active claim"
                )
            _CLAIMED.add(identifier)
            claimed = True
        yield claim
        with block_deferred_signals(), _locked():
            entry = _ALLOCATIONS.get(identifier)
            if (
                entry is None
                or entry.reference() is not prepared
                or entry.claim is not claim
            ):
                raise PlanConsumerViewMutationConflict(
                    "plan-consumer allocation claim changed before commit"
                )
            _CLAIMED.discard(identifier)
            _ALLOCATIONS.pop(identifier, None)
            scope = _ALLOCATION_SCOPES.get(entry.scope_token)
            if scope is not None and scope.receipt is prepared:
                _ALLOCATION_SCOPES.pop(entry.scope_token, None)
            completed = True
    finally:
        if claimed and not completed:
            with block_deferred_signals(), _locked():
                _CLAIMED.discard(identifier)


def discard_plan_consumer_view_allocation(
    layout: RunLayout,
    prepared: PreparedPlanConsumerViewAllocation,
) -> None:
    """Retire only the exact still-owned temporary after failed construction."""

    if type(prepared) is not PreparedPlanConsumerViewAllocation:
        raise PlanConsumerViewMutationConflict(
            "plan-consumer allocation cleanup requires its exact opaque receipt"
        )
    with (
        claiming_plan_consumer_view_allocation(layout, prepared) as claim,
        managed_fork_protected_acquisition() as acquisition,
    ):
        _shot_descriptor, _root_descriptor, scratch_descriptor = (
            _canonical_layout_descriptors(layout, acquisition)
        )
        retire_owned_directory(
            scratch_descriptor,
            claim.path.name,
            claim.identity,
        )


__all__ = [
    "PreparedPlanConsumerViewAllocation",
    "allocating_plan_consumer_view",
    "discard_plan_consumer_view_allocation",
    "require_canonical_plan_consumer_run_layout",
]
