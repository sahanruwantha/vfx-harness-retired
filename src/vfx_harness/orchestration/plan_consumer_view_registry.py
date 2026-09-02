"""Process/thread registry state for opaque plan-consumer capabilities."""

from __future__ import annotations

import threading
import weakref
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from vfx_harness.observability import fork_coordination
from vfx_harness.orchestration.plan_consumer_view_capabilities import (
    PlanConsumerViewMutationCapability,
    PreparedPlanConsumerViewInstallation,
)
from vfx_harness.orchestration.plan_consumer_view_descriptors import (
    PlanConsumerDirectoryIdentity,
    PlanConsumerLedgerBinding,
)
from vfx_harness.orchestration.plan_consumer_view_installation import (
    PlanConsumerViewInstallationRename,
)

Phase = Literal["construction", "installed"]


@dataclass(frozen=True, slots=True)
class CapabilityRecord:
    shot: Path
    view: Path
    phase: Phase
    shot_descriptor: int
    shot_identity: PlanConsumerDirectoryIdentity
    view_descriptor: int
    view_identity: PlanConsumerDirectoryIdentity
    scratch: Path | None
    scratch_descriptor: int | None
    scratch_identity: PlanConsumerDirectoryIdentity | None
    marker_sha256: str
    ledger_binding: PlanConsumerLedgerBinding
    process_id: int
    process_token: object
    thread_id: int
    thread_token: object


@dataclass(frozen=True, slots=True)
class CapabilityEntry:
    reference: weakref.ReferenceType[PlanConsumerViewMutationCapability]
    record: CapabilityRecord


@dataclass(frozen=True, slots=True)
class InstallationRecord:
    shot: Path
    scratch: Path
    temporary: Path
    shot_identity: PlanConsumerDirectoryIdentity
    scratch_identity: PlanConsumerDirectoryIdentity
    view_identity: PlanConsumerDirectoryIdentity
    marker_sha256: str
    ledger_binding: PlanConsumerLedgerBinding
    process_id: int
    process_token: object
    thread_id: int
    thread_token: object


@dataclass(frozen=True, slots=True)
class InstallationEntry:
    reference: weakref.ReferenceType[PreparedPlanConsumerViewInstallation]
    record: InstallationRecord


@dataclass(frozen=True, slots=True)
class InstalledViewRecord:
    installation_identifier: int
    shot: Path
    scratch: Path
    view: Path
    shot_identity: PlanConsumerDirectoryIdentity
    scratch_identity: PlanConsumerDirectoryIdentity
    view_identity: PlanConsumerDirectoryIdentity
    marker_sha256: str
    ledger_binding: PlanConsumerLedgerBinding
    cleanup_receipt: PlanConsumerViewInstallationRename | None
    process_id: int
    process_token: object
    thread_id: int
    thread_token: object


LOCK = threading.RLock()
PROCESS_TOKEN = object()
THREAD_LOCAL = threading.local()
ACTIVE: dict[int, CapabilityEntry] = {}
INSTALLATIONS: dict[int, InstallationEntry] = {}
INSTALLED: dict[tuple[Path, Path], InstalledViewRecord] = {}
INSTALLED_CLAIMS: set[tuple[Path, Path]] = set()


def _after_fork_child() -> None:
    global LOCK, PROCESS_TOKEN, THREAD_LOCAL

    ACTIVE.clear()
    INSTALLATIONS.clear()
    INSTALLED.clear()
    INSTALLED_CLAIMS.clear()
    LOCK = threading.RLock()
    PROCESS_TOKEN = object()
    THREAD_LOCAL = threading.local()


fork_coordination.register_fork_participant(
    "orchestration.plan_consumer_view_registry",
    lock_factory=lambda: LOCK,
    after_in_child=_after_fork_child,
)


@contextmanager
def locked() -> Iterator[None]:
    """Take the consumer registry after the process-wide fork barrier."""

    with fork_coordination.fork_coordinated_lock(LOCK):
        yield


def thread_token() -> object:
    token = getattr(THREAD_LOCAL, "token", None)
    if token is None:
        token = object()
        THREAD_LOCAL.token = token
    return token


__all__: list[str] = []
