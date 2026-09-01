"""Pure evidence that a reconciler acquired an exact lost-owner fence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from vfx_harness.domain.prior_running_status import PriorRunningStatusEvidence
from vfx_harness.domain.run_ids import require_run_id
from vfx_harness.domain.run_lifecycle_primitives import (
    chronological as _chronological,
)
from vfx_harness.domain.run_lifecycle_primitives import (
    exact_record as _exact_record,
)
from vfx_harness.domain.run_lifecycle_primitives import (
    positive_int as _positive_int,
)
from vfx_harness.domain.run_lifecycle_primitives import (
    relative_locator as _relative_locator,
)
from vfx_harness.domain.run_lifecycle_primitives import (
    supervisor_wait_result as _supervisor_wait_result,
)
from vfx_harness.domain.run_lifecycle_primitives import (
    timestamp as _timestamp,
)
from vfx_harness.domain.run_owner_claims import RunOwnerClaim
from vfx_harness.domain.run_record_refs import RunRecordRef
from vfx_harness.domain.stop_envelope_primitives import (
    canonical_digest,
    require_canonical_digest,
    require_digest,
)

RUN_OWNER_LOSS_OBSERVATION_SCHEMA = "vfx-harness.run-owner-loss-observation/v1"
RUN_OWNER_LOSS_RECONCILER_MANIFEST_LOCATOR = "reports/reconciler-manifest.json"
RUN_OWNER_LOSS_RECONCILER_MANIFEST_SCHEMA = "vfx-harness.run/v2"


@dataclass(frozen=True, slots=True)
class RunOwnerLossObservation:
    """A reconciler's exclusive acquisition of the exact prior owner's fence."""

    SCHEMA: ClassVar[str] = RUN_OWNER_LOSS_OBSERVATION_SCHEMA

    run_id: str
    prior_owner_digest: str
    reconciler_run_id: str
    reconciler_manifest_ref: RunRecordRef
    fence_locator: str
    fence_device: int
    fence_inode: int
    prior_running_status: PriorRunningStatusEvidence
    exclusive_fence_acquired: bool
    exclusive_acquisition_observed_at: str
    supervisor_wait_status: int | None

    def __post_init__(self) -> None:
        require_run_id(self.run_id, "RunOwnerLossObservation.run_id")
        require_digest(
            self.prior_owner_digest,
            "RunOwnerLossObservation.prior_owner_digest",
        )
        require_run_id(
            self.reconciler_run_id,
            "RunOwnerLossObservation.reconciler_run_id",
        )
        if self.reconciler_run_id == self.run_id:
            raise ValueError("owner-loss reconciler must be a distinct root run")
        if not isinstance(self.reconciler_manifest_ref, RunRecordRef):
            raise ValueError("owner-loss reconciler manifest reference must be typed")
        if self.reconciler_manifest_ref.locator != RUN_OWNER_LOSS_RECONCILER_MANIFEST_LOCATOR:
            raise ValueError("owner-loss reconciler manifest must select its canonical archive locator")
        if self.reconciler_manifest_ref.record_schema != RUN_OWNER_LOSS_RECONCILER_MANIFEST_SCHEMA:
            raise ValueError("owner-loss reconciler manifest must bind a vfx-harness.run/v2 record")
        _relative_locator(
            self.fence_locator,
            "RunOwnerLossObservation.fence_locator",
        )
        _positive_int(
            self.fence_device,
            "RunOwnerLossObservation.fence_device",
            allow_zero=True,
        )
        _positive_int(self.fence_inode, "RunOwnerLossObservation.fence_inode")
        if not isinstance(self.prior_running_status, PriorRunningStatusEvidence):
            raise ValueError("RunOwnerLossObservation.prior_running_status must be typed evidence")
        if (
            self.prior_running_status.run_id,
            self.prior_running_status.owner_claim_digest,
        ) != (self.run_id, self.prior_owner_digest):
            raise ValueError("owner-loss prior running status does not bind the exact prior owner")
        if self.exclusive_fence_acquired is not True:
            raise ValueError("RunOwnerLossObservation.exclusive_fence_acquired must be true")
        _timestamp(
            self.exclusive_acquisition_observed_at,
            "RunOwnerLossObservation.exclusive_acquisition_observed_at",
        )
        _chronological(
            self.exclusive_acquisition_observed_at,
            self.prior_running_status.captured_at,
            "owner-loss fence/prior-status capture",
        )
        _supervisor_wait_result(self.supervisor_wait_status)

    @property
    def signal_number(self) -> int | None:
        return _supervisor_wait_result(self.supervisor_wait_status)[0]

    @property
    def exit_code(self) -> int | None:
        return _supervisor_wait_result(self.supervisor_wait_status)[1]

    def require_matches_owner(self, owner: RunOwnerClaim) -> None:
        if not isinstance(owner, RunOwnerClaim):
            raise ValueError("owner-loss verification requires a typed owner claim")
        self.prior_running_status.require_matches_owner(owner)
        expected = (
            owner.run_id,
            owner.digest,
            owner.fence_locator,
            owner.fence_device,
            owner.fence_inode,
        )
        observed = (
            self.run_id,
            self.prior_owner_digest,
            self.fence_locator,
            self.fence_device,
            self.fence_inode,
        )
        if observed != expected:
            raise ValueError("owner-loss observation does not reacquire the exact prior owner fence")

    def _payload(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "run_id": self.run_id,
            "prior_owner_digest": self.prior_owner_digest,
            "reconciler_run_id": self.reconciler_run_id,
            "reconciler_manifest_ref": self.reconciler_manifest_ref.as_dict(),
            "fence_locator": self.fence_locator,
            "fence_device": self.fence_device,
            "fence_inode": self.fence_inode,
            "prior_running_status": self.prior_running_status.as_dict(),
            "exclusive_fence_acquired": self.exclusive_fence_acquired,
            "exclusive_acquisition_observed_at": self.exclusive_acquisition_observed_at,
            "supervisor_wait_status": self.supervisor_wait_status,
        }

    @property
    def digest(self) -> str:
        return canonical_digest(self._payload())

    def as_dict(self) -> dict[str, Any]:
        return {**self._payload(), "observation_digest": self.digest}

    @classmethod
    def from_dict(
        cls,
        value: object,
        where: str = "run owner loss observation",
        *,
        prior_owner: RunOwnerClaim | None = None,
    ) -> RunOwnerLossObservation:
        fields = frozenset(
            {
                "run_id",
                "prior_owner_digest",
                "reconciler_run_id",
                "reconciler_manifest_ref",
                "fence_locator",
                "fence_device",
                "fence_inode",
                "prior_running_status",
                "exclusive_fence_acquired",
                "exclusive_acquisition_observed_at",
                "supervisor_wait_status",
                "observation_digest",
            }
        )
        row = _exact_record(value, where, cls.SCHEMA, fields)
        candidate = cls(
            run_id=row["run_id"],
            prior_owner_digest=row["prior_owner_digest"],
            reconciler_run_id=row["reconciler_run_id"],
            reconciler_manifest_ref=RunRecordRef.from_dict(
                row["reconciler_manifest_ref"],
                f"{where}.reconciler_manifest_ref",
            ),
            fence_locator=row["fence_locator"],
            fence_device=row["fence_device"],
            fence_inode=row["fence_inode"],
            prior_running_status=PriorRunningStatusEvidence.from_dict(
                row["prior_running_status"],
                f"{where}.prior_running_status",
                owner=prior_owner,
            ),
            exclusive_fence_acquired=row["exclusive_fence_acquired"],
            exclusive_acquisition_observed_at=row["exclusive_acquisition_observed_at"],
            supervisor_wait_status=row["supervisor_wait_status"],
        )
        require_canonical_digest(
            row["observation_digest"],
            candidate.digest,
            where,
            "observation_digest",
        )
        if prior_owner is not None:
            candidate.require_matches_owner(prior_owner)
        return candidate
