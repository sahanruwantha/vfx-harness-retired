"""Content- and locator-bound references used by run lifecycle receipts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from vfx_harness.domain.run_lifecycle_primitives import exact_record, relative_locator
from vfx_harness.domain.stop_envelope_primitives import (
    canonical_digest,
    require_canonical_digest,
    require_digest,
    require_text,
)

RUN_RECORD_REF_SCHEMA = "vfx-harness.run-record-ref/v1"


@dataclass(frozen=True, slots=True)
class RunRecordRef:
    """One immutable run-owned typed JSON source by path, bytes, and record identity."""

    SCHEMA: ClassVar[str] = RUN_RECORD_REF_SCHEMA

    locator: str
    sha256: str
    record_schema: str
    record_digest: str

    def __post_init__(self) -> None:
        relative_locator(self.locator, "RunRecordRef.locator")
        require_digest(self.sha256, "RunRecordRef.sha256")
        require_text(self.record_schema, "RunRecordRef.record_schema")
        require_digest(self.record_digest, "RunRecordRef.record_digest")

    def _payload(self) -> dict[str, str]:
        return {
            "schema": self.SCHEMA,
            "locator": self.locator,
            "sha256": self.sha256,
            "record_schema": self.record_schema,
            "record_digest": self.record_digest,
        }

    @property
    def digest(self) -> str:
        return canonical_digest(self._payload())

    def as_dict(self) -> dict[str, str]:
        return {**self._payload(), "ref_digest": self.digest}

    @classmethod
    def from_dict(
        cls,
        value: object,
        where: str = "run record reference",
    ) -> RunRecordRef:
        fields = frozenset({"locator", "sha256", "record_schema", "record_digest", "ref_digest"})
        row = exact_record(value, where, cls.SCHEMA, fields)
        candidate = cls(
            locator=row["locator"],
            sha256=row["sha256"],
            record_schema=row["record_schema"],
            record_digest=row["record_digest"],
        )
        require_canonical_digest(
            row["ref_digest"],
            candidate.digest,
            where,
            "ref_digest",
        )
        return candidate

    def require_record(self, *, schema: str, digest: str, where: str) -> None:
        if self.record_schema != schema or self.record_digest != digest:
            raise ValueError(f"{where} does not bind the exact {schema!r} record digest")
