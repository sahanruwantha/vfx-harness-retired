"""Closed marker for one immutable run-scoped plan consumer snapshot."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vfx_harness.domain.authority_head_records import (
    OVERLAY_ARTIFACTS as OVERLAY_ARTIFACTS,
)
from vfx_harness.domain.authority_head_records import (
    PLAN_CONSUMER_VIEW_SCHEMA as CONSUMER_VIEW_SCHEMA,
)
from vfx_harness.domain.authority_head_records import (
    decode_canonical_json_object,
    parse_plan_consumer_view,
)
from vfx_harness.orchestration.authority_selection_transaction import (
    AuthoritySelectionConflict,
    AuthoritySelectionToken,
)


@dataclass(frozen=True, slots=True)
class PlanConsumerViewMarker:
    """Identity and exact overlay bytes captured from one verified selection."""

    shot: Path
    bundle: Path
    content_hash: str
    base_selection: AuthoritySelectionToken
    view_source: str
    view_digest: str
    artifact_hashes: dict[str, str]
    authored_inputs: dict[str, str]
    decision_inputs: dict[str, dict[str, str | int]]

    @classmethod
    def from_dict(cls, value: Any) -> PlanConsumerViewMarker:
        projection = parse_plan_consumer_view(value)
        try:
            base_selection = AuthoritySelectionToken.from_dict(
                value["base_selection"],
                "plan consumer view marker.base_selection",
            )
        except AuthoritySelectionConflict as exc:
            raise ValueError(str(exc)) from exc
        return cls(
            shot=projection.shot,
            bundle=projection.bundle,
            content_hash=projection.content_hash,
            base_selection=base_selection,
            view_source=projection.view_source,
            view_digest=projection.view_digest,
            artifact_hashes=projection.artifact_hashes,
            authored_inputs=projection.authored_inputs,
            decision_inputs=projection.decision_inputs,
        )

    @classmethod
    def from_bytes(cls, payload: bytes) -> PlanConsumerViewMarker:
        """Parse the exact canonical bytes emitted by the consumer-view producer."""

        return cls.from_dict(
            decode_canonical_json_object(payload, "plan consumer view marker")
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": CONSUMER_VIEW_SCHEMA,
            "shot": str(self.shot),
            "bundle": str(self.bundle),
            "content_hash": self.content_hash,
            "base_selection": self.base_selection.to_dict(),
            "effective_view": {
                "source": self.view_source,
                "digest": self.view_digest,
                "artifact_hashes": dict(self.artifact_hashes),
            },
            "authored_inputs": dict(self.authored_inputs),
            "decision_inputs": {
                name: dict(identity)
                for name, identity in self.decision_inputs.items()
            },
        }

    @property
    def bundle_run_id(self) -> str:
        return self.bundle.relative_to(self.shot / "runs").parts[0]
