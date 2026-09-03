"""Run-owned immutable archive manifest for interruption evidence (HIR-0172).

The capturer copies every authority source and transcript it observed into create-only
content-addressed storage owned by the target run and publishes this closed manifest.
Historical verification reopens those archived bytes, never the mutable shot tree, so a
later valid authority publication cannot falsify an already committed interruption.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, ClassVar

from vfx_harness.domain.run_authority_source_closure import (
    InterruptionAuthoritySourceClosure,
)
from vfx_harness.domain.run_authority_source_identity import (
    AuthoritySourceIdentity,
    canonical_digest,
    exact_record,
    require_digest,
)
from vfx_harness.domain.run_ids import require_run_id
from vfx_harness.domain.run_lifecycle_primitives import (
    positive_int,
    relative_locator,
    timestamp,
)

INTERRUPTION_ARCHIVE_MANIFEST_SCHEMA = "vfx-harness.interruption-archive-manifest/v1"
INTERRUPTION_ARCHIVE_MANIFEST_LOCATOR = "reports/interruption-archive-manifest.json"
INTERRUPTION_ARCHIVE_OBJECT_DIRECTORY = "archive/interruption/objects"
ARCHIVED_SOURCE_OBJECT_SCHEMA = "vfx-harness.interruption-archived-source/v1"
ARCHIVE_NAMESPACES = frozenset({"shot", "run"})


def archive_object_locator(sha256: str) -> str:
    """Return the sole run-relative locator of one archived byte stream."""

    digest = require_digest(sha256, "archive object sha256")
    return f"{INTERRUPTION_ARCHIVE_OBJECT_DIRECTORY}/{digest}"


def iter_authority_sources(
    closure: InterruptionAuthoritySourceClosure,
) -> tuple[AuthoritySourceIdentity, ...]:
    """Return every source identity of one closure in a stable order."""

    if not isinstance(closure, InterruptionAuthoritySourceClosure):
        raise ValueError("authority source enumeration requires the typed closure")
    plan = closure.selected_plan
    accepted = closure.accepted_state
    durable = closure.durable_state
    rows: list[AuthoritySourceIdentity | None] = [
        plan.plan_pointer,
        plan.bundle_manifest,
        *plan.bundle_members,
        plan.effective_view_pointer,
        *plan.effective_view_members,
        plan.plan_amendments,
        plan.plan_resolutions,
        accepted.ledger,
        *accepted.members,
        accepted.judgment_debts,
        accepted.judgment_payment_attempts,
        durable.current_pointer,
        durable.head,
        *durable.records,
        *durable.members,
        durable.pending,
        *closure.authored_inputs.sources(),
    ]
    return tuple(row for row in rows if row is not None)


@dataclass(frozen=True, slots=True)
class ArchivedSourceObject:
    """Exact bytes of one observed source copied into the run-owned archive."""

    SCHEMA: ClassVar[str] = ARCHIVED_SOURCE_OBJECT_SCHEMA

    namespace: str
    locator: str
    byte_count: int
    sha256: str

    def __post_init__(self) -> None:
        if self.namespace not in ARCHIVE_NAMESPACES:
            raise ValueError(
                f"ArchivedSourceObject.namespace must be one of {sorted(ARCHIVE_NAMESPACES)}"
            )
        relative_locator(self.locator, "ArchivedSourceObject.locator")
        positive_int(self.byte_count, "ArchivedSourceObject.byte_count", allow_zero=True)
        require_digest(self.sha256, "ArchivedSourceObject.sha256")

    @property
    def key(self) -> tuple[str, str]:
        return (self.namespace, self.locator)

    @property
    def archive_locator(self) -> str:
        return archive_object_locator(self.sha256)

    def _payload(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "namespace": self.namespace,
            "locator": self.locator,
            "byte_count": self.byte_count,
            "sha256": self.sha256,
            "archive_locator": self.archive_locator,
        }

    @property
    def digest(self) -> str:
        return canonical_digest(self._payload())

    def as_dict(self) -> dict[str, Any]:
        return {**self._payload(), "object_digest": self.digest}

    @classmethod
    def from_dict(
        cls,
        value: object,
        where: str = "archived source object",
    ) -> ArchivedSourceObject:
        row = exact_record(
            value,
            where,
            schema=cls.SCHEMA,
            fields=frozenset(
                {"namespace", "locator", "byte_count", "sha256", "archive_locator", "object_digest"}
            ),
        )
        candidate = cls(row["namespace"], row["locator"], row["byte_count"], row["sha256"])
        if row["archive_locator"] != candidate.archive_locator:
            raise ValueError(f"{where}.archive_locator is not content-addressed by its sha256")
        observed = require_digest(row["object_digest"], f"{where}.object_digest")
        if observed != candidate.digest:
            raise ValueError(f"{where}.object_digest is stale")
        return candidate


def _coverage_rows(
    rows: Iterable[tuple[str, str, int, str]],
) -> tuple[tuple[str, str, int, str], ...]:
    return tuple(rows)


@dataclass(frozen=True, slots=True)
class InterruptionArchiveManifest:
    """Closed table of every archived source that one interruption receipt binds."""

    SCHEMA: ClassVar[str] = INTERRUPTION_ARCHIVE_MANIFEST_SCHEMA

    run_id: str
    captured_at: str
    authority_digest: str
    objects: tuple[ArchivedSourceObject, ...]

    def __post_init__(self) -> None:
        require_run_id(self.run_id, "InterruptionArchiveManifest.run_id")
        timestamp(self.captured_at, "InterruptionArchiveManifest.captured_at")
        require_digest(self.authority_digest, "InterruptionArchiveManifest.authority_digest")
        if not isinstance(self.objects, tuple) or any(
            not isinstance(row, ArchivedSourceObject) for row in self.objects
        ):
            raise ValueError("InterruptionArchiveManifest.objects must be a tuple of typed objects")
        keys = [row.key for row in self.objects]
        if len(keys) != len(set(keys)):
            raise ValueError("InterruptionArchiveManifest.objects contains duplicate source locators")
        if keys != sorted(keys):
            raise ValueError("InterruptionArchiveManifest.objects must be sorted by namespace and locator")

    @classmethod
    def mint(
        cls,
        *,
        run_id: str,
        captured_at: str,
        authority_digest: str,
        objects: Iterable[ArchivedSourceObject],
    ) -> InterruptionArchiveManifest:
        """Sort and deduplicate exact duplicate rows; conflicting rows fail closed."""

        table: dict[tuple[str, str], ArchivedSourceObject] = {}
        for row in objects:
            if not isinstance(row, ArchivedSourceObject):
                raise ValueError("archive manifest rows must be typed archived source objects")
            existing = table.get(row.key)
            if existing is not None and existing != row:
                raise ValueError(
                    f"archive manifest observed two different byte streams for {row.namespace} "
                    f"source {row.locator!r}"
                )
            table[row.key] = row
        return cls(
            run_id=run_id,
            captured_at=captured_at,
            authority_digest=authority_digest,
            objects=tuple(table[key] for key in sorted(table)),
        )

    def object_for(self, namespace: str, locator: str) -> ArchivedSourceObject | None:
        for row in self.objects:
            if row.key == (namespace, locator):
                return row
        return None

    def require_covers(
        self,
        rows: Iterable[tuple[str, str, int, str]],
        where: str,
    ) -> None:
        """Require an exact archived object for every (namespace, locator, bytes, sha)."""

        for namespace, locator, byte_count, sha256 in _coverage_rows(rows):
            archived = self.object_for(namespace, locator)
            if archived is None:
                raise ValueError(f"{where}: archive manifest has no object for {namespace} source {locator!r}")
            if (archived.byte_count, archived.sha256) != (byte_count, sha256):
                raise ValueError(
                    f"{where}: archive manifest object for {namespace} source {locator!r} "
                    "does not match the observed bytes"
                )

    def require_covers_closure(self, closure: InterruptionAuthoritySourceClosure) -> None:
        self.require_covers(
            (
                ("shot", source.locator, source.byte_count, source.sha256)
                for source in iter_authority_sources(closure)
            ),
            "interruption archive authority coverage",
        )

    def _payload(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "run_id": self.run_id,
            "captured_at": self.captured_at,
            "authority_digest": self.authority_digest,
            "objects": [row.as_dict() for row in self.objects],
        }

    @property
    def digest(self) -> str:
        return canonical_digest(self._payload())

    def as_dict(self) -> dict[str, Any]:
        return {**self._payload(), "manifest_digest": self.digest}

    @classmethod
    def from_dict(
        cls,
        value: object,
        where: str = "interruption archive manifest",
    ) -> InterruptionArchiveManifest:
        row = exact_record(
            value,
            where,
            schema=cls.SCHEMA,
            fields=frozenset({"run_id", "captured_at", "authority_digest", "objects", "manifest_digest"}),
        )
        raw_objects = row["objects"]
        if not isinstance(raw_objects, list):
            raise ValueError(f"{where}.objects must be a list")
        candidate = cls(
            run_id=row["run_id"],
            captured_at=row["captured_at"],
            authority_digest=row["authority_digest"],
            objects=tuple(
                ArchivedSourceObject.from_dict(item, f"{where}.objects[{index}]")
                for index, item in enumerate(raw_objects)
            ),
        )
        observed = require_digest(row["manifest_digest"], f"{where}.manifest_digest")
        if observed != candidate.digest:
            raise ValueError(f"{where}.manifest_digest is stale")
        return candidate


__all__ = [
    "ARCHIVED_SOURCE_OBJECT_SCHEMA",
    "ARCHIVE_NAMESPACES",
    "INTERRUPTION_ARCHIVE_MANIFEST_LOCATOR",
    "INTERRUPTION_ARCHIVE_MANIFEST_SCHEMA",
    "INTERRUPTION_ARCHIVE_OBJECT_DIRECTORY",
    "ArchivedSourceObject",
    "InterruptionArchiveManifest",
    "archive_object_locator",
    "iter_authority_sources",
]
