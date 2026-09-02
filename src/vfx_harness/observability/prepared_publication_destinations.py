"""Exact owner capabilities for protected prepared-publication destinations.

The generic side-file transaction is intentionally useful for non-authoritative
run data.  It must not also be a public write primitive for executable layer
artifacts, finalization receipts, or sealed outcomes.  Their typed owners mint a
single-use capability for one exact shot-relative destination before staging.
"""

from __future__ import annotations

import os
import sys
import threading
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vfx_harness.domain.script_locators import (
    is_composed_layer_script_locator,
)
from vfx_harness.domain.shot_ledger_paths import is_shot_ledger_target
from vfx_harness.observability import fork_coordination
from vfx_harness.observability.prepared_publication_capabilities import (
    PreparedPublicationRegistryError,
)


class PreparedPublicationDestinationConflict(PreparedPublicationRegistryError):
    """A protected destination lacks its exact typed owner capability."""


class _PreparedPublicationDestinationAuthorization:
    """Opaque one-shot authority to stage one exact protected destination."""

    __slots__ = ("__weakref__",)

    def __new__(
        cls,
        *_args: Any,
        **_kwargs: Any,
    ) -> _PreparedPublicationDestinationAuthorization:
        raise PreparedPublicationDestinationConflict(
            "protected prepared-publication destinations are authorized only by "
            "their typed owner"
        )

    def __copy__(self) -> _PreparedPublicationDestinationAuthorization:
        raise PreparedPublicationDestinationConflict(
            "protected destination authorizations cannot be copied"
        )

    def __deepcopy__(
        self,
        _memo: dict[int, Any],
    ) -> _PreparedPublicationDestinationAuthorization:
        raise PreparedPublicationDestinationConflict(
            "protected destination authorizations cannot be copied"
        )

    def __reduce_ex__(self, _protocol: int) -> Any:
        raise PreparedPublicationDestinationConflict(
            "protected destination authorizations cannot be serialized"
        )

    def __repr__(self) -> str:
        return "<_PreparedPublicationDestinationAuthorization opaque>"


class _PreparedPublicationDestinationIssuer:
    """Process-local issuer retained by one exact typed sink module."""

    __slots__ = ()

    def __new__(
        cls,
        *_args: Any,
        **_kwargs: Any,
    ) -> _PreparedPublicationDestinationIssuer:
        raise PreparedPublicationDestinationConflict(
            "protected destination issuers are bound only to exact typed owners"
        )


@dataclass(frozen=True, slots=True)
class _OwnerSpec:
    module: str
    symbol: str


@dataclass(frozen=True, slots=True)
class _IssuerBinding:
    issuer: _PreparedPublicationDestinationIssuer
    family: str
    owner_type: type
    process_id: int
    process_token: object


@dataclass(frozen=True, slots=True)
class _AuthorizationRecord:
    issuer: _PreparedPublicationDestinationIssuer
    family: str
    shot: Path
    relative: Path
    process_id: int
    process_token: object
    thread_id: int
    thread_token: object


@dataclass(frozen=True, slots=True)
class _AuthorizationEntry:
    reference: weakref.ReferenceType[_PreparedPublicationDestinationAuthorization]
    record: _AuthorizationRecord


_OWNER_SPECS = {
    "layer-artifact": _OwnerSpec(
        "vfx_harness.agents.builder.layer_artifact",
        "PreparedLayerArtifact",
    ),
    "layer-replay-receipt": _OwnerSpec(
        "vfx_harness.orchestration.layer_replay_receipts",
        "PreparedLayerReplayReceipt",
    ),
    "layer-evaluation-receipt": _OwnerSpec(
        "vfx_harness.orchestration.layer_evaluation_receipts",
        "PreparedLayerEvaluationReceipt",
    ),
    "layer-outcome": _OwnerSpec(
        "vfx_harness.orchestration.layer_outcome_publication",
        "PreparedLayerOutcomePublication",
    ),
    "shot-ledger": _OwnerSpec(
        "vfx_harness.orchestration.shot_ledger_publication",
        "PreparedShotLedgerPublication",
    ),
    "plan-consumer-ledger": _OwnerSpec(
        "vfx_harness.orchestration.plan_consumer_view_capabilities",
        "PlanConsumerViewMutationCapability",
    ),
}
_LOCK = threading.RLock()
_PROCESS_TOKEN = object()
_THREAD_LOCAL = threading.local()
_ISSUERS: dict[str, _IssuerBinding] = {}
_AUTHORIZATIONS: dict[int, _AuthorizationEntry] = {}


def _after_fork_child() -> None:
    global _ISSUERS, _LOCK, _PROCESS_TOKEN, _THREAD_LOCAL

    _AUTHORIZATIONS.clear()
    _LOCK = threading.RLock()
    _PROCESS_TOKEN = object()
    _THREAD_LOCAL = threading.local()
    _ISSUERS = {
        family: _IssuerBinding(
            issuer=binding.issuer,
            family=binding.family,
            owner_type=binding.owner_type,
            process_id=os.getpid(),
            process_token=_PROCESS_TOKEN,
        )
        for family, binding in _ISSUERS.items()
    }


fork_coordination.register_fork_participant(
    "observability.prepared_publication_destinations",
    lock_factory=lambda: _LOCK,
    after_in_child=_after_fork_child,
)


def _thread_token() -> object:
    token = getattr(_THREAD_LOCAL, "token", None)
    if token is None:
        token = object()
        _THREAD_LOCAL.token = token
    return token


def absolute_prepared_publication_path(path: str | Path) -> Path:
    return Path(os.path.abspath(Path(path).expanduser()))


def normalize_prepared_publication_destination(
    shot_folder: str | Path,
    destination: str | Path,
) -> tuple[Path, Path, Path]:
    """Return the exact lexical target used by descriptor-safe traversal."""

    shot = absolute_prepared_publication_path(shot_folder)
    raw = Path(destination).expanduser()
    target = absolute_prepared_publication_path(
        raw if raw.is_absolute() else shot / raw
    )
    try:
        relative = target.relative_to(shot)
    except ValueError as exc:
        raise PreparedPublicationDestinationConflict(
            f"side-file publication destination escapes the shot root: {target}"
        ) from exc
    if relative == Path(".") or not relative.name:
        raise PreparedPublicationDestinationConflict(
            "side-file publication requires a non-root file path"
        )
    return shot, target, relative


def _protected_family_for_parts(parts: tuple[str, ...]) -> tuple[bool, str | None]:
    relative = Path(*parts)
    if is_shot_ledger_target(relative):
        return True, "shot-ledger"
    if is_composed_layer_script_locator(relative.as_posix()):
        return True, "layer-artifact"
    if parts in {("build",), ("build", "units")}:
        return True, None
    if (
        len(parts) >= 4
        and parts[0] == "runs"
        and parts[2:4] == ("checkpoints", "layer-finalizations")
    ):
        if len(parts) != 5:
            return True, None
        name = parts[4]
        if name.endswith(".replay.json"):
            return True, "layer-replay-receipt"
        if name.endswith(".evaluation.json"):
            return True, "layer-evaluation-receipt"
        return True, None
    if len(parts) >= 2 and parts[:2] == ("plans", "outcomes"):
        return True, "layer-outcome"
    return False, None


def _protected_family(
    relative: Path,
    *,
    target: Path,
) -> tuple[bool, str | None]:
    """Classify the target independently of the caller-selected root.

    The generic publisher accepts a root so it can retain and revalidate that
    directory by descriptor.  That root is not authority over namespace
    classification: a caller could otherwise pass ``<shot>/plans`` and make
    ``outcomes/...`` look like an ordinary side file.  Scan every normalized
    absolute suffix as well as the requested relative path so re-rooting at or
    above a reserved namespace never removes its typed owner.
    """

    matches: set[str | None] = set()
    protected, family = _protected_family_for_parts(relative.parts)
    if protected:
        matches.add(family)
    absolute_parts = target.parts
    for index in range(len(absolute_parts)):
        protected, family = _protected_family_for_parts(
            absolute_parts[index:],
        )
        if protected:
            matches.add(family)
    if not matches:
        return False, None
    if len(matches) != 1 or None in matches:
        return True, None
    return True, next(iter(matches))


def _owner_may_authorize_family(owner_family: str, family: str) -> bool:
    """Keep canonical shot.json reserved while permitting one proven scratch copy.

    Namespace classification intentionally treats every basename ``shot.json`` as
    canonical by default.  The alternate family is not inferred from a path; only
    the isolated-view owner can mint its exact one-shot authorization.
    """

    return owner_family == family or (
        family == "shot-ledger" and owner_family == "plan-consumer-ledger"
    )


def _bind_prepared_publication_destination_issuer(
    *,
    family: str,
    owner_type: type,
) -> _PreparedPublicationDestinationIssuer:
    """Bind one protected family to its exact production capability type."""

    spec = _OWNER_SPECS.get(family)
    owner_module = None if spec is None else sys.modules.get(spec.module)
    if (
        spec is None
        or owner_module is None
        or owner_type.__module__ != spec.module
        or owner_type.__qualname__ != spec.symbol
        or getattr(owner_module, spec.symbol, None) is not owner_type
    ):
        raise PreparedPublicationDestinationConflict(
            "protected destination issuer binding requires its exact typed owner"
        )
    with fork_coordination.fork_coordinated_lock(_LOCK):
        if family in _ISSUERS:
            raise PreparedPublicationDestinationConflict(
                f"protected destination family is already bound: {family}"
            )
        issuer = object.__new__(_PreparedPublicationDestinationIssuer)
        _ISSUERS[family] = _IssuerBinding(
            issuer=issuer,
            family=family,
            owner_type=owner_type,
            process_id=os.getpid(),
            process_token=_PROCESS_TOKEN,
        )
        return issuer


def _require_issuer(
    issuer: _PreparedPublicationDestinationIssuer,
) -> _IssuerBinding:
    with fork_coordination.fork_coordinated_lock(_LOCK):
        matches = [
            binding
            for binding in _ISSUERS.values()
            if binding.issuer is issuer
        ]
        if len(matches) != 1:
            raise PreparedPublicationDestinationConflict(
                "protected destination authorization requires an exact typed owner issuer"
            )
        binding = matches[0]
        if (
            type(issuer) is not _PreparedPublicationDestinationIssuer
            or binding.process_id != os.getpid()
            or binding.process_token is not _PROCESS_TOKEN
        ):
            raise PreparedPublicationDestinationConflict(
                "protected destination issuer belongs to another process generation"
            )
        return binding


def _authorization_gone(
    identifier: int,
    observed: weakref.ReferenceType[_PreparedPublicationDestinationAuthorization],
) -> None:
    with fork_coordination.fork_coordinated_lock(_LOCK):
        entry = _AUTHORIZATIONS.get(identifier)
        if entry is not None and entry.reference is observed:
            _AUTHORIZATIONS.pop(identifier, None)


def _issue_prepared_publication_destination_authorization(
    *,
    issuer: _PreparedPublicationDestinationIssuer,
    shot_folder: str | Path,
    destination: str | Path,
) -> _PreparedPublicationDestinationAuthorization:
    """Mint one exact shot/path staging authorization for a typed sink."""

    binding = _require_issuer(issuer)
    shot, _target, relative = normalize_prepared_publication_destination(
        shot_folder,
        destination,
    )
    protected, family = _protected_family(relative, target=_target)
    if (
        not protected
        or family is None
        or not _owner_may_authorize_family(binding.family, family)
    ):
        raise PreparedPublicationDestinationConflict(
            f"typed {binding.family} owner cannot authorize destination {relative.as_posix()}"
        )
    authorization = object.__new__(
        _PreparedPublicationDestinationAuthorization
    )
    identifier = id(authorization)
    reference = weakref.ref(
        authorization,
        lambda observed, key=identifier: _authorization_gone(key, observed),
    )
    record = _AuthorizationRecord(
        issuer=issuer,
        family=binding.family,
        shot=shot,
        relative=relative,
        process_id=os.getpid(),
        process_token=_PROCESS_TOKEN,
        thread_id=threading.get_ident(),
        thread_token=_thread_token(),
    )
    with fork_coordination.fork_coordinated_lock(_LOCK):
        if identifier in _AUTHORIZATIONS:  # pragma: no cover - live id guarantee
            raise PreparedPublicationDestinationConflict(
                "protected destination authorization identity collided"
            )
        _AUTHORIZATIONS[identifier] = _AuthorizationEntry(reference, record)
    return authorization


def consume_prepared_publication_destination_authorization(
    authorization: object | None,
    *,
    shot: Path,
    target: Path,
    relative: Path,
) -> str | None:
    """Consume the exact owner capability required by this normalized target."""

    protected, family = _protected_family(relative, target=target)
    if not protected:
        if authorization is not None:
            raise PreparedPublicationDestinationConflict(
                "unprotected destination cannot consume a protected owner authorization"
            )
        return None
    if family is None:
        raise PreparedPublicationDestinationConflict(
            f"prepared-publication destination is in a reserved authority namespace: {relative.as_posix()}"
        )
    if type(authorization) is not _PreparedPublicationDestinationAuthorization:
        raise PreparedPublicationDestinationConflict(
            f"protected {family} destination requires its exact typed owner authorization"
        )
    with fork_coordination.fork_coordinated_lock(_LOCK):
        entry = _AUTHORIZATIONS.get(id(authorization))
        if entry is None or entry.reference() is not authorization:
            raise PreparedPublicationDestinationConflict(
                "protected destination authorization is unregistered, expired, or already consumed"
            )
        record = entry.record
        if (
            record.process_id != os.getpid()
            or record.process_token is not _PROCESS_TOKEN
            or record.thread_id != threading.get_ident()
            or record.thread_token is not _thread_token()
        ):
            raise PreparedPublicationDestinationConflict(
                "protected destination authorization belongs to another process or thread"
            )
        if (
            not _owner_may_authorize_family(record.family, family)
            or record.shot != shot
            or record.relative != relative
        ):
            raise PreparedPublicationDestinationConflict(
                "protected destination authorization belongs to another shot, path, or family"
            )
        _require_issuer(record.issuer)
        _AUTHORIZATIONS.pop(id(authorization), None)
        return record.family


__all__: list[str] = []
