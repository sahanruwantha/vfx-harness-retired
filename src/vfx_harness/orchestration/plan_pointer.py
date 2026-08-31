"""Closed contract for the revisioned selected global-plan pointer."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vfx_harness.domain.authority_head_records import (
    PLAN_POINTER_SCHEMA,
    AuthorityHeadRecordError,
    parse_plan_pointer,
)
from vfx_harness.domain.authority_head_records import (
    PUBLISHABLE_OUTCOMES as _DOMAIN_PUBLISHABLE_OUTCOMES,
)
from vfx_harness.orchestration import plan_bundle_integrity

PLAN_POINTER_PATH = Path("plans/current.json")
PUBLISHABLE_OUTCOMES = _DOMAIN_PUBLISHABLE_OUTCOMES


@dataclass(frozen=True, slots=True)
class PlanPointer:
    """Producer-valid pointer to one immutable run-owned plan bundle."""

    revision: int
    run_id: str
    bundle: Path
    content_hash: str
    outcome: str
    published_at: str

    @classmethod
    def from_dict(cls, value: Any) -> PlanPointer:
        try:
            pointer = parse_plan_pointer(value)
        except AuthorityHeadRecordError as exc:
            raise plan_bundle_integrity.PlanPublicationError(str(exc)) from exc
        return cls(
            revision=pointer.revision,
            run_id=pointer.run_id,
            bundle=Path(pointer.bundle),
            content_hash=pointer.content_hash,
            outcome=pointer.outcome,
            published_at=pointer.published_at,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": PLAN_POINTER_SCHEMA,
            "revision": self.revision,
            "run_id": self.run_id,
            "bundle": self.bundle.as_posix(),
            "content_hash": self.content_hash,
            "outcome": self.outcome,
            "published_at": self.published_at,
        }
