"""Installed-generation lifecycle for isolated plan-consumer views."""

from __future__ import annotations

import hashlib
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace
from pathlib import Path

from vfx_harness.domain.authority_head_records import canonical_json_bytes
from vfx_harness.observability.prepared_publication_descriptors import (
    block_deferred_signals,
)
from vfx_harness.observability.run_artifacts import RunLayout
from vfx_harness.orchestration import plan_consumer_view_registry as _registry
from vfx_harness.orchestration.plan_consumer_view import PlanConsumerViewMarker
from vfx_harness.orchestration.plan_consumer_view_allocation import (
    require_canonical_plan_consumer_run_layout,
)
from vfx_harness.orchestration.plan_consumer_view_capabilities import (
    PreparedPlanConsumerViewInstallation,
)
from vfx_harness.orchestration.plan_consumer_view_descriptors import (
    PlanConsumerViewMutationConflict,
    absolute_path,
)
from vfx_harness.orchestration.plan_consumer_view_installation import (
    PlanConsumerViewInstallationRename,
    _discard_prepared_plan_consumer_temporary,
    _discard_previous_plan_consumer_view,
    _install_and_verify_allocated_plan_consumer_view,
    require_plan_consumer_view_install_destination_isolated,
)
from vfx_harness.orchestration.plan_consumer_view_transaction import (
    _exclusive_plan_consumer_view_transaction,
)


@dataclass(slots=True)
class _InstalledViewClaim:
    key: tuple[Path, Path]
    record: _registry.InstalledViewRecord
    consume: bool = False


def _resolve_installation(
    prepared: PreparedPlanConsumerViewInstallation,
) -> _registry.InstallationRecord:
    if type(prepared) is not PreparedPlanConsumerViewInstallation:
        raise PlanConsumerViewMutationConflict(
            "plan-consumer installation requires its exact opaque proof"
        )
    with block_deferred_signals(), _registry.locked():
        entry = _registry.INSTALLATIONS.get(id(prepared))
        if entry is None or entry.reference() is not prepared:
            raise PlanConsumerViewMutationConflict(
                "plan-consumer installation proof is unregistered, expired, copied, "
                "or forked"
            )
        record = entry.record
    if (
        record.process_id != os.getpid()
        or record.process_token is not _registry.PROCESS_TOKEN
        or record.thread_id != threading.get_ident()
        or record.thread_token is not _registry.thread_token()
    ):
        raise PlanConsumerViewMutationConflict(
            "plan-consumer installation proof belongs to another process or thread"
        )
    return record


def require_plan_consumer_view_install_destination(layout: RunLayout) -> Path:
    """Fail before snapshot work if the final view name is shot authority."""

    if not isinstance(layout, RunLayout):
        raise PlanConsumerViewMutationConflict(
            "plan-consumer install preflight requires a typed run layout"
        )
    require_canonical_plan_consumer_run_layout(layout)
    installed = absolute_path(layout.scratch / "plan-consumer-view")
    require_plan_consumer_view_install_destination_isolated(
        shot=layout.shot,
        scratch=layout.scratch,
        installed=installed,
    )
    return installed


def discard_prepared_plan_consumer_view_installation(
    prepared: PreparedPlanConsumerViewInstallation,
) -> None:
    """Retire only the exact temporary still owned by an uninstalled proof."""

    record = _resolve_installation(prepared)
    _discard_prepared_plan_consumer_temporary(
        shot=record.shot,
        scratch=record.scratch,
        temporary=record.temporary,
        shot_identity=record.shot_identity,
        scratch_identity=record.scratch_identity,
        temporary_identity=record.view_identity,
        marker_sha256=record.marker_sha256,
        ledger_binding=record.ledger_binding,
    )
    with block_deferred_signals(), _registry.locked():
        entry = _registry.INSTALLATIONS.get(id(prepared))
        if entry is None or entry.reference() is not prepared or entry.record is not record:
            raise PlanConsumerViewMutationConflict(
                "plan-consumer installation proof changed during exact cleanup"
            )
        _registry.INSTALLATIONS.pop(id(prepared), None)


def _registration_is_current(
    prepared: PreparedPlanConsumerViewInstallation,
    installed_key: tuple[Path, Path],
    installed_record: _registry.InstalledViewRecord | None,
    receipt: PlanConsumerViewInstallationRename | None = None,
) -> bool:
    if installed_record is None:
        return False
    with _registry.locked():
        current = _registry.INSTALLED.get(installed_key)
        installation = _registry.INSTALLATIONS.get(id(prepared))
        return (
            current is installed_record
            and installation is None
            and (receipt is None or current.cleanup_receipt is receipt)
        )


def _recover_installed_cleanup(
    key: tuple[Path, Path],
    record: _registry.InstalledViewRecord,
) -> _registry.InstalledViewRecord:
    receipt = record.cleanup_receipt
    if receipt is None:
        return record
    recovered = replace(record, cleanup_receipt=None)

    def complete_registration() -> None:
        with _registry.locked():
            current = _registry.INSTALLED.get(key)
            if current is not record:
                raise PlanConsumerViewMutationConflict(
                    "installed plan-consumer generation changed during cleanup recovery"
                )
            _registry.INSTALLED[key] = recovered

    _discard_previous_plan_consumer_view(
        receipt,
        complete_registration=complete_registration,
    )
    return recovered


@contextmanager
def _claiming_installation_key(
    prepared: PreparedPlanConsumerViewInstallation,
    record: _registry.InstallationRecord,
    installed_key: tuple[Path, Path],
) -> Iterator[_registry.InstalledViewRecord | None]:
    """Arm release before claiming the physical install/mutation key."""

    claimed = False
    try:
        with block_deferred_signals(), _registry.locked():
            entry = _registry.INSTALLATIONS.get(id(prepared))
            if (
                entry is None
                or entry.reference() is not prepared
                or entry.record is not record
            ):
                raise PlanConsumerViewMutationConflict(
                    "plan-consumer installation proof changed during registration"
                )
            if installed_key in _registry.INSTALLED_CLAIMS:
                raise PlanConsumerViewMutationConflict(
                    "cannot install a plan-consumer generation while its predecessor "
                    "is mutating or installing"
                )
            _registry.INSTALLED_CLAIMS.add(installed_key)
            claimed = True
            predecessor = _registry.INSTALLED.get(installed_key)
        yield predecessor
    finally:
        if claimed:
            with block_deferred_signals(), _registry.locked():
                _registry.INSTALLED_CLAIMS.discard(installed_key)


def install_plan_consumer_view(
    prepared: PreparedPlanConsumerViewInstallation,
    view: str | Path,
) -> Path:
    """Rename, verify, and register one construction generation transactionally."""

    record = _resolve_installation(prepared)
    installed = absolute_path(view)
    expected = record.scratch / "plan-consumer-view"
    if installed != expected:
        raise PlanConsumerViewMutationConflict(
            "installed plan-consumer view must use the exact layout scratch destination"
        )
    installed_key = (record.shot, installed)
    layout = RunLayout(
        shot=record.shot,
        run_id=record.scratch.parent.name,
        root=record.scratch.parent,
    )
    installed_record: _registry.InstalledViewRecord | None = None
    with _exclusive_plan_consumer_view_transaction(
        layout,
        expected_shot_identity=record.shot_identity,
        expected_scratch_identity=record.scratch_identity,
    ), _claiming_installation_key(
        prepared,
        record,
        installed_key,
    ) as predecessor:
        if predecessor is not None:
            predecessor = _recover_installed_cleanup(installed_key, predecessor)

        def register_installation(
            receipt: PlanConsumerViewInstallationRename,
        ) -> None:
            nonlocal installed_record
            candidate = _registry.InstalledViewRecord(
                installation_identifier=id(prepared),
                shot=record.shot,
                scratch=record.scratch,
                view=installed,
                shot_identity=record.shot_identity,
                scratch_identity=record.scratch_identity,
                view_identity=record.view_identity,
                marker_sha256=record.marker_sha256,
                ledger_binding=record.ledger_binding,
                cleanup_receipt=receipt,
                process_id=os.getpid(),
                process_token=_registry.PROCESS_TOKEN,
                thread_id=threading.get_ident(),
                thread_token=_registry.thread_token(),
            )
            with block_deferred_signals(), _registry.locked():
                entry = _registry.INSTALLATIONS.get(id(prepared))
                if (
                    entry is None
                    or entry.reference() is not prepared
                    or entry.record is not record
                ):
                    raise PlanConsumerViewMutationConflict(
                        "plan-consumer installation authority changed before "
                        "registration"
                    )
                _registry.INSTALLED[installed_key] = candidate
                _registry.INSTALLATIONS.pop(id(prepared), None)
                installed_record = candidate

        def registration_is_current(
            receipt: PlanConsumerViewInstallationRename,
        ) -> bool:
            return _registration_is_current(
                prepared,
                installed_key,
                installed_record,
                receipt,
            )

        _install_and_verify_allocated_plan_consumer_view(
            shot=record.shot,
            scratch=record.scratch,
            temporary=record.temporary,
            installed=installed,
            expected_shot_identity=record.shot_identity,
            expected_scratch_identity=record.scratch_identity,
            expected_identity=record.view_identity,
            marker_sha256=record.marker_sha256,
            ledger_binding=record.ledger_binding,
            register_installation=register_installation,
            registration_is_current=registration_is_current,
        )
        assert installed_record is not None
        with suppress(PlanConsumerViewMutationConflict):
            _recover_installed_cleanup(installed_key, installed_record)
            # The registered record retains the exact cleanup receipt.
        return installed


@contextmanager
def claiming_installed_view(
    shot: Path,
    view: Path,
    marker: PlanConsumerViewMarker,
) -> Iterator[_InstalledViewClaim]:
    """Hold an installed claim whose release is armed before registry mutation."""

    marker_digest = hashlib.sha256(canonical_json_bytes(marker.to_dict())).hexdigest()
    key = (shot, view)
    claimed = False
    lease: _InstalledViewClaim | None = None
    cleanup_error: BaseException | None = None
    try:
        with block_deferred_signals(), _registry.locked():
            record = _registry.INSTALLED.get(key)
            if record is None:
                raise PlanConsumerViewMutationConflict(
                    "plan-consumer mutation target was not installed from this "
                    "process's constructed view"
                )
            if (
                record.process_id != os.getpid()
                or record.process_token is not _registry.PROCESS_TOKEN
                or record.thread_id != threading.get_ident()
                or record.thread_token is not _registry.thread_token()
                or record.shot != shot
                or record.view != view
                or record.marker_sha256 != marker_digest
            ):
                raise PlanConsumerViewMutationConflict(
                    "installed plan-consumer registration belongs to another generation"
                )
            if key in _registry.INSTALLED_CLAIMS:
                raise PlanConsumerViewMutationConflict(
                    "installed plan-consumer view already has an active mutation claim"
                )
            _registry.INSTALLED_CLAIMS.add(key)
            claimed = True
            lease = _InstalledViewClaim(key, record)
        yield lease
    finally:
        if claimed:
            assert lease is not None
            with block_deferred_signals(), _registry.locked():
                current = _registry.INSTALLED.get(key)
                if lease.consume and current is not lease.record:
                    cleanup_error = PlanConsumerViewMutationConflict(
                        "installed plan-consumer registration changed before claim "
                        "consumption"
                    )
                _registry.INSTALLED_CLAIMS.discard(key)
                if lease.consume and current is lease.record:
                    _registry.INSTALLED.pop(key, None)
            if cleanup_error is not None:
                raise cleanup_error


def plan_consumer_view_installation_is_committed(
    prepared: PreparedPlanConsumerViewInstallation,
    view: str | Path,
) -> bool:
    """Report only an exact registered commit for one still-live proof object."""

    if type(prepared) is not PreparedPlanConsumerViewInstallation:
        return False
    installed = absolute_path(view)
    with _registry.locked():
        return any(
            record.installation_identifier == id(prepared)
            and record.view == installed
            and record.process_id == os.getpid()
            and record.process_token is _registry.PROCESS_TOKEN
            for record in _registry.INSTALLED.values()
        )


def recover_installed_cleanup(
    key: tuple[Path, Path],
    record: _registry.InstalledViewRecord,
) -> _registry.InstalledViewRecord:
    return _recover_installed_cleanup(key, record)


__all__ = [
    "claiming_installed_view",
    "discard_prepared_plan_consumer_view_installation",
    "install_plan_consumer_view",
    "plan_consumer_view_installation_is_committed",
    "recover_installed_cleanup",
    "require_plan_consumer_view_install_destination",
]
