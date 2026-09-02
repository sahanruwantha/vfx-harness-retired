"""Opaque physical authority for mutable plan-consumer view members.

A plan-consumer view deliberately contains a scratch copy of ``shot.json``.  Its
basename alone is therefore indistinguishable from the canonical ledger to a
generic filesystem writer.  This module proves the missing distinction with a
short-lived capability bound to two simultaneously held directory identities:
the source shot and one physically different view.

The capability never escapes the operation that acquired it.  Every protected
ledger create, no-op, and replacement revalidates the held and named directory
generations immediately before the prepared-file CAS.
"""

from __future__ import annotations

import hashlib
import os
import stat
import threading
import weakref
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from vfx_harness.domain.authority_head_records import canonical_json_bytes
from vfx_harness.observability.prepared_publication_descriptors import (
    block_deferred_signals,
)
from vfx_harness.observability.run_artifacts import RunLayout
from vfx_harness.observability.run_owner_fork_guard import (
    ForkProtectedAcquisition,
    GuardedDescriptor,
    managed_fork_protected_acquisition,
)
from vfx_harness.orchestration import plan_consumer_view_registry as _registry
from vfx_harness.orchestration.plan_consumer_view import PlanConsumerViewMarker
from vfx_harness.orchestration.plan_consumer_view_allocation import (
    PreparedPlanConsumerViewAllocation,
    allocating_plan_consumer_view,
    claiming_plan_consumer_view_allocation,
    discard_plan_consumer_view_allocation,
)
from vfx_harness.orchestration.plan_consumer_view_capabilities import (
    PlanConsumerViewMutationCapability,
    PreparedPlanConsumerViewInstallation,
)
from vfx_harness.orchestration.plan_consumer_view_descriptors import (
    PlanConsumerDirectoryIdentity as _DirectoryIdentity,
)
from vfx_harness.orchestration.plan_consumer_view_descriptors import (
    PlanConsumerViewMutationConflict as PlanConsumerViewMutationConflict,
)
from vfx_harness.orchestration.plan_consumer_view_descriptors import (
    absolute_path as _absolute,
)
from vfx_harness.orchestration.plan_consumer_view_descriptors import (
    capture_ledger_binding_at as _capture_ledger_binding_at,
)
from vfx_harness.orchestration.plan_consumer_view_descriptors import (
    close_descriptors as _close_descriptors,
)
from vfx_harness.orchestration.plan_consumer_view_descriptors import (
    ledger_leaf_identity as _ledger_leaf_identity,
)
from vfx_harness.orchestration.plan_consumer_view_descriptors import (
    open_real_directory as _open_real_directory,
)
from vfx_harness.orchestration.plan_consumer_view_descriptors import (
    read_marker as _read_marker,
)
from vfx_harness.orchestration.plan_consumer_view_descriptors import (
    same_named_directory as _same_named_directory,
)
from vfx_harness.orchestration.plan_consumer_view_lifecycle import (
    claiming_installed_view as _claiming_installed_view,
)
from vfx_harness.orchestration.plan_consumer_view_lifecycle import (
    discard_prepared_plan_consumer_view_installation,
    install_plan_consumer_view,
    plan_consumer_view_installation_is_committed,
    require_plan_consumer_view_install_destination,
)
from vfx_harness.orchestration.plan_consumer_view_lifecycle import (
    recover_installed_cleanup as _recover_installed_cleanup,
)
from vfx_harness.orchestration.plan_consumer_view_transaction import (
    _exclusive_plan_consumer_view_transaction,
)

_MARKER_CREATE_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
)


def _create_construction_marker(
    view_descriptor: int,
    acquisition: ForkProtectedAcquisition,
    payload: bytes,
) -> None:
    """Create the marker only through the retained exact view descriptor."""

    try:
        descriptor = acquisition.open_descriptor(
            lambda: os.open(
                ".plan-consumer-view.json",
                _MARKER_CREATE_FLAGS,
                0o600,
                dir_fd=view_descriptor,
            )
        )
    except OSError as exc:
        raise PlanConsumerViewMutationConflict(
            "plan-consumer construction marker must be absent and creatable only "
            "through the exact allocated view"
        ) from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise PlanConsumerViewMutationConflict(
                "plan-consumer construction marker must be a real regular file"
            )
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise PlanConsumerViewMutationConflict(
                    "plan-consumer construction marker write made no progress"
                )
            offset += written
        os.fsync(descriptor)
        os.fsync(view_descriptor)
    finally:
        acquisition.retire(descriptor)


def _require_distinct_ledger_leaf(
    shot_descriptor: int,
    shot: Path,
    view_descriptor: int,
    view: Path,
) -> None:
    source = _ledger_leaf_identity(shot_descriptor, shot)
    target = _ledger_leaf_identity(view_descriptor, view)
    if source is not None and target == source:
        raise PlanConsumerViewMutationConflict(
            "plan-consumer ledger member aliases the canonical shot.json inode"
        )


def _record_gone(
    identifier: int,
    reference: weakref.ReferenceType[PlanConsumerViewMutationCapability],
) -> None:
    with _registry.locked():
        entry = _registry.ACTIVE.get(identifier)
        if entry is not None and entry.reference is reference:
            _registry.ACTIVE.pop(identifier, None)


def _installation_gone(
    identifier: int,
    reference: weakref.ReferenceType[PreparedPlanConsumerViewInstallation],
) -> None:
    with _registry.locked():
        entry = _registry.INSTALLATIONS.get(identifier)
        if entry is not None and entry.reference is reference:
            _registry.INSTALLATIONS.pop(identifier, None)


def _register(
    capability: PlanConsumerViewMutationCapability,
    record: _registry.CapabilityRecord,
) -> None:
    identifier = id(capability)
    reference = weakref.ref(
        capability,
        lambda observed, key=identifier: _record_gone(key, observed),
    )
    with _registry.locked():
        if identifier in _registry.ACTIVE:  # pragma: no cover - live id guarantee
            raise PlanConsumerViewMutationConflict(
                "plan-consumer mutation capability identity collided"
            )
        _registry.ACTIVE[identifier] = _registry.CapabilityEntry(reference, record)


def _retire(capability: PlanConsumerViewMutationCapability) -> None:
    with _registry.locked():
        entry = _registry.ACTIVE.get(id(capability))
        if entry is not None and entry.reference() is capability:
            _registry.ACTIVE.pop(id(capability), None)


def _resolve(
    capability: PlanConsumerViewMutationCapability,
) -> _registry.CapabilityRecord:
    if type(capability) is not PlanConsumerViewMutationCapability:
        raise PlanConsumerViewMutationConflict(
            "plan-consumer mutation requires its exact opaque capability"
        )
    with _registry.locked():
        entry = _registry.ACTIVE.get(id(capability))
        if entry is None or entry.reference() is not capability:
            raise PlanConsumerViewMutationConflict(
                "plan-consumer mutation capability is unregistered, expired, copied, or forked"
            )
        record = entry.record
    if (
        record.process_id != os.getpid()
        or record.process_token is not _registry.PROCESS_TOKEN
        or record.thread_id != threading.get_ident()
        or record.thread_token is not _registry.thread_token()
    ):
        raise PlanConsumerViewMutationConflict(
            "plan-consumer mutation capability belongs to another process or thread"
        )
    return record


def _require_current(
    capability: PlanConsumerViewMutationCapability,
    *,
    phase: _registry.Phase | None = None,
) -> _registry.CapabilityRecord:
    record = _resolve(capability)
    if phase is not None and record.phase != phase:
        raise PlanConsumerViewMutationConflict(
            f"plan-consumer mutation requires a {phase} capability, found {record.phase}"
        )
    try:
        shot_identity = _DirectoryIdentity.capture(record.shot_descriptor)
        view_identity = _DirectoryIdentity.capture(record.view_descriptor)
    except OSError as exc:
        raise PlanConsumerViewMutationConflict(
            "plan-consumer mutation directory descriptor is no longer live"
        ) from exc
    if (
        shot_identity != record.shot_identity
        or view_identity != record.view_identity
        or shot_identity == view_identity
    ):
        raise PlanConsumerViewMutationConflict(
            "plan-consumer mutation target no longer proves a distinct shot/view identity"
        )
    _same_named_directory(record.shot, record.shot_identity)
    _same_named_directory(record.view, record.view_identity)
    if record.scratch is not None:
        if record.scratch_descriptor is None or record.scratch_identity is None:
            raise PlanConsumerViewMutationConflict(
                "construction capability has no exact scratch-directory identity"
            )
        scratch_identity = _DirectoryIdentity.capture(record.scratch_descriptor)
        if scratch_identity != record.scratch_identity:
            raise PlanConsumerViewMutationConflict(
                "plan-consumer construction scratch directory changed"
            )
        _same_named_directory(record.scratch, record.scratch_identity)
        try:
            named_view = os.stat(
                record.view.name,
                dir_fd=record.scratch_descriptor,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise PlanConsumerViewMutationConflict(
                "plan-consumer construction target disappeared from exact scratch"
            ) from exc
        if (
            named_view.st_dev,
            named_view.st_ino,
            stat.S_IFMT(named_view.st_mode),
        ) != (
            record.view_identity.device,
            record.view_identity.inode,
            record.view_identity.file_type,
        ):
            raise PlanConsumerViewMutationConflict(
                "plan-consumer construction target was rebound below scratch"
            )
    _require_distinct_ledger_leaf(
        record.shot_descriptor,
        record.shot,
        record.view_descriptor,
        record.view,
    )
    if (
        _capture_ledger_binding_at(record.view_descriptor, record.view)
        != record.ledger_binding
    ):
        raise PlanConsumerViewMutationConflict(
            "plan-consumer ledger generation or bytes changed outside its typed owner"
        )
    return record


def _advance_constructed_plan_consumer_ledger_binding(
    capability: PlanConsumerViewMutationCapability,
    expected: _registry.CapabilityRecord,
    expected_sha256: str,
) -> None:
    """Advance one live capability after its typed prepared CAS commits."""

    observed = _capture_ledger_binding_at(
        expected.view_descriptor,
        expected.view,
    )
    if observed.sha256 != expected_sha256 or observed.identity is None:
        raise PlanConsumerViewMutationConflict(
            "typed plan-consumer ledger commit did not bind its exact new generation"
        )
    identifier = id(capability)
    with _registry.locked():
        entry = _registry.ACTIVE.get(identifier)
        if (
            entry is None
            or entry.reference() is not capability
            or entry.record is not expected
        ):
            raise PlanConsumerViewMutationConflict(
                "plan-consumer capability changed before ledger binding advance"
            )
        _registry.ACTIVE[identifier] = _registry.CapabilityEntry(
            entry.reference,
            replace(expected, ledger_binding=observed),
        )


def require_plan_consumer_view_mutation(
    capability: PlanConsumerViewMutationCapability,
) -> Path:
    """Return the exact view path after revalidating its physical capability."""

    return _require_current(capability).view


def _require_current_plan_consumer_view_mutation(
    capability: PlanConsumerViewMutationCapability,
    *,
    phase: str,
) -> _registry.CapabilityRecord:
    if phase not in {"construction", "installed"}:
        raise PlanConsumerViewMutationConflict(
            f"unknown plan-consumer mutation phase: {phase!r}"
        )
    return _require_current(capability, phase=phase)  # type: ignore[arg-type]


def prepare_plan_consumer_view_installation(
    capability: PlanConsumerViewMutationCapability,
) -> PreparedPlanConsumerViewInstallation:
    """Bind the constructed inode before its owned temp name is renamed."""

    record = _require_current(capability, phase="construction")
    if record.scratch is None or record.scratch_identity is None:
        raise PlanConsumerViewMutationConflict(
            "plan-consumer installation preparation has no exact scratch root"
        )
    prepared = object.__new__(PreparedPlanConsumerViewInstallation)
    installation = _registry.InstallationRecord(
        shot=record.shot,
        scratch=record.scratch,
        temporary=record.view,
        shot_identity=record.shot_identity,
        scratch_identity=record.scratch_identity,
        view_identity=record.view_identity,
        marker_sha256=record.marker_sha256,
        ledger_binding=record.ledger_binding,
        process_id=os.getpid(),
        process_token=_registry.PROCESS_TOKEN,
        thread_id=threading.get_ident(),
        thread_token=_registry.thread_token(),
    )
    identifier = id(prepared)
    reference = weakref.ref(
        prepared,
        lambda observed, key=identifier: _installation_gone(key, observed),
    )
    with _registry.locked():
        if identifier in _registry.INSTALLATIONS:  # pragma: no cover
            raise PlanConsumerViewMutationConflict(
                "plan-consumer installation proof identity collided"
            )
        _registry.INSTALLATIONS[identifier] = _registry.InstallationEntry(
            reference,
            installation,
        )
    return prepared


@contextmanager
def _mutation_capability(
    *,
    shot: Path,
    view: Path,
    marker: PlanConsumerViewMarker,
    phase: _registry.Phase,
    scratch: Path | None,
    installed_record: _registry.InstalledViewRecord | None,
    construction_identity: _DirectoryIdentity | None,
) -> Iterator[PlanConsumerViewMutationCapability]:
    shot = _absolute(shot)
    view = _absolute(view)
    scratch = None if scratch is None else _absolute(scratch)
    marker_shot = _absolute(marker.shot)
    if marker_shot != shot:
        raise PlanConsumerViewMutationConflict(
            "plan-consumer marker belongs to another canonical shot path"
        )
    if phase == "construction":
        if scratch is None or view.parent != scratch:
            raise PlanConsumerViewMutationConflict(
                "plan-consumer construction target must be an exact child of layout.scratch"
            )
        if construction_identity is None:
            raise PlanConsumerViewMutationConflict(
                "plan-consumer construction has no module-owned allocation identity"
            )
    elif construction_identity is not None:
        raise PlanConsumerViewMutationConflict(
            "installed plan-consumer mutation cannot carry construction identity"
        )

    creator_pid = os.getpid()
    acquisition: ForkProtectedAcquisition | None = None
    descriptor_numbers: tuple[int, ...] = ()
    guarded: tuple[GuardedDescriptor, ...] = ()
    token: object | None = None
    capability: PlanConsumerViewMutationCapability | None = None
    body_error: BaseException | None = None
    body_traceback = None
    try:
        with managed_fork_protected_acquisition() as pending:
            acquisition = pending
            shot_descriptor = _open_real_directory(shot, pending)
            scratch_descriptor = (
                None if scratch is None else _open_real_directory(scratch, pending)
            )
            view_descriptor = _open_real_directory(view, pending)
            shot_identity = _DirectoryIdentity.capture(shot_descriptor)
            view_identity = _DirectoryIdentity.capture(view_descriptor)
            scratch_identity = (
                None
                if scratch_descriptor is None
                else _DirectoryIdentity.capture(scratch_descriptor)
            )
            if shot_identity == view_identity:
                raise PlanConsumerViewMutationConflict(
                    "plan-consumer mutation target physically aliases the canonical shot root"
                )
            if (
                construction_identity is not None
                and view_identity != construction_identity
            ):
                raise PlanConsumerViewMutationConflict(
                    "plan-consumer construction target is not the module-owned allocation inode"
                )
            if scratch_descriptor is not None:
                named_view = os.stat(
                    view.name,
                    dir_fd=scratch_descriptor,
                    follow_symlinks=False,
                )
                if (
                    named_view.st_dev,
                    named_view.st_ino,
                    stat.S_IFMT(named_view.st_mode),
                ) != (
                    view_identity.device,
                    view_identity.inode,
                    view_identity.file_type,
                ):
                    raise PlanConsumerViewMutationConflict(
                        "plan-consumer construction target is not the exact scratch child"
                    )
            expected_marker_payload = canonical_json_bytes(marker.to_dict())
            if phase == "construction":
                _create_construction_marker(
                    view_descriptor,
                    pending,
                    expected_marker_payload,
                )
            marker_payload = _read_marker(view_descriptor, pending, view)
            if marker_payload != expected_marker_payload:
                raise PlanConsumerViewMutationConflict(
                    "plan-consumer marker bytes differ from the verified view identity"
                )
            if installed_record is not None and (
                installed_record.shot != shot
                or installed_record.scratch != scratch
                or installed_record.view != view
                or installed_record.shot_identity != shot_identity
                or installed_record.scratch_identity != scratch_identity
                or installed_record.view_identity != view_identity
                or installed_record.marker_sha256
                != hashlib.sha256(marker_payload).hexdigest()
                or installed_record.ledger_binding
                != _capture_ledger_binding_at(view_descriptor, view)
            ):
                raise PlanConsumerViewMutationConflict(
                    "installed plan-consumer registration differs from the live view"
                )
            _require_distinct_ledger_leaf(
                shot_descriptor,
                shot,
                view_descriptor,
                view,
            )
            if phase == "construction" and _ledger_leaf_identity(
                view_descriptor,
                view,
            ) is not None:
                raise PlanConsumerViewMutationConflict(
                    "plan-consumer construction ledger member must be absent"
                )
            ledger_binding = _capture_ledger_binding_at(view_descriptor, view)
            descriptor_numbers = tuple(
                descriptor
                for descriptor in (
                    shot_descriptor,
                    scratch_descriptor,
                    view_descriptor,
                )
                if descriptor is not None
            )
            guarded = pending.guarded_descriptors(descriptor_numbers)
            token = pending.token
            with block_deferred_signals():
                pending.handoff(descriptor_numbers)

        capability = object.__new__(PlanConsumerViewMutationCapability)
        record = _registry.CapabilityRecord(
            shot=shot,
            view=view,
            phase=phase,
            shot_descriptor=shot_descriptor,
            shot_identity=shot_identity,
            view_descriptor=view_descriptor,
            view_identity=view_identity,
            scratch=scratch,
            scratch_descriptor=scratch_descriptor,
            scratch_identity=scratch_identity,
            marker_sha256=hashlib.sha256(marker_payload).hexdigest(),
            ledger_binding=ledger_binding,
            process_id=os.getpid(),
            process_token=_registry.PROCESS_TOKEN,
            thread_id=threading.get_ident(),
            thread_token=_registry.thread_token(),
        )
        _register(capability, record)
        _require_current(capability, phase=phase)
        yield capability
    except BaseException as exc:
        body_error = exc
        body_traceback = exc.__traceback__
    finally:
        retirement_error: BaseException | None = None
        cleanup_error: BaseException | None = None
        cleanup_retained = False
        with block_deferred_signals():
            if capability is not None:
                try:
                    _retire(capability)
                except BaseException as exc:
                    retirement_error = exc
            if os.getpid() != creator_pid:
                cleanup_error = PlanConsumerViewMutationConflict(
                    "forked child cannot release its parent's plan-consumer capability"
                )
            elif (
                acquisition is not None
                and descriptor_numbers
                and any(item.is_current() for item in guarded)
            ):
                assert token is not None
                cleanup_error, cleanup_retained = _close_descriptors(
                    token,
                    guarded,
                )
        if retirement_error is not None:
            if cleanup_error is None:
                cleanup_error = retirement_error
            else:
                cleanup_error.add_note(
                    "plan-consumer capability retirement diagnostic: "
                    f"{type(retirement_error).__name__}: {retirement_error}"
                )
        if body_error is not None:
            if cleanup_error is not None:
                if cleanup_retained:
                    cleanup_error.add_note(
                        "primary plan-consumer mutation failure: "
                        f"{type(body_error).__name__}: {body_error}"
                    )
                    raise cleanup_error from body_error
                body_error.add_note(
                    f"plan-consumer capability cleanup diagnostic: {cleanup_error}"
                )
            raise body_error.with_traceback(body_traceback)
        if cleanup_error is not None:
            raise cleanup_error


@contextmanager
def constructing_plan_consumer_view(
    layout: RunLayout,
    allocation: PreparedPlanConsumerViewAllocation,
    marker: PlanConsumerViewMarker,
) -> Iterator[PlanConsumerViewMutationCapability]:
    """Issue authority for the exact new temporary owned by ``layout``."""

    if not isinstance(layout, RunLayout):
        raise PlanConsumerViewMutationConflict(
            "plan-consumer construction requires a typed run layout"
        )
    with claiming_plan_consumer_view_allocation(layout, allocation) as claim, _mutation_capability(
        shot=layout.shot,
        view=claim.path,
        marker=marker,
        phase="construction",
        scratch=layout.scratch,
        installed_record=None,
        construction_identity=claim.identity,
    ) as capability:
        yield capability


@contextmanager
def mutating_plan_consumer_view(
    shot_folder: str | Path,
    view: str | Path,
    marker: PlanConsumerViewMarker,
) -> Iterator[PlanConsumerViewMutationCapability]:
    """Issue authority for one installed, physically isolated consumer view."""

    shot = _absolute(shot_folder)
    installed = _absolute(view)
    with _claiming_installed_view(shot, installed, marker) as claim:
        registered = claim.record
        key = (shot, installed)
        layout = RunLayout(
            shot=registered.shot,
            run_id=registered.scratch.parent.name,
            root=registered.scratch.parent,
        )
        with _exclusive_plan_consumer_view_transaction(
            layout,
            expected_shot_identity=registered.shot_identity,
            expected_scratch_identity=registered.scratch_identity,
        ):
            registered = _recover_installed_cleanup(key, registered)
            claim.record = registered
            with _mutation_capability(
                shot=shot,
                view=installed,
                marker=marker,
                phase="installed",
                scratch=registered.scratch,
                installed_record=registered,
                construction_identity=None,
            ) as capability:
                claim.consume = True
                yield capability


__all__ = [
    "PlanConsumerViewMutationCapability",
    "PlanConsumerViewMutationConflict",
    "PreparedPlanConsumerViewAllocation",
    "PreparedPlanConsumerViewInstallation",
    "allocating_plan_consumer_view",
    "constructing_plan_consumer_view",
    "discard_plan_consumer_view_allocation",
    "discard_prepared_plan_consumer_view_installation",
    "install_plan_consumer_view",
    "mutating_plan_consumer_view",
    "plan_consumer_view_installation_is_committed",
    "prepare_plan_consumer_view_installation",
    "require_plan_consumer_view_install_destination",
    "require_plan_consumer_view_mutation",
]
