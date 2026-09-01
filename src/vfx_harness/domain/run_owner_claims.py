"""Pure identity contract for the one process that may terminalize a run."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from vfx_harness.domain.run_ids import require_run_id
from vfx_harness.domain.run_lifecycle_primitives import (
    exact_record,
    positive_int,
    relative_locator,
    timestamp,
)
from vfx_harness.domain.stop_envelope_primitives import (
    canonical_digest,
    require_canonical_digest,
    require_digest,
    require_id,
)

RUN_OWNER_CLAIM_SCHEMA = "vfx-harness.run-owner-claim/v1"
RUN_OWNER_KINDS = frozenset({"direct", "driver"})
RUN_OWNER_FENCE_IMPLEMENTATION = "posix-flock-inode/v1"
RUN_OWNER_CLAIM_LOCATOR = "owner/claim.json"
RUN_OWNER_FENCE_LOCATOR = "owner/fence.lock"


def _require_filesystem_identity(device: object, inode: object, where: str) -> None:
    positive_int(device, f"{where}.device", allow_zero=True)
    positive_int(inode, f"{where}.inode")


@dataclass(frozen=True, slots=True)
class RunOwnerClaim:
    """Create-only root ownership bound to one held, non-inheritable inode fence."""

    SCHEMA: ClassVar[str] = RUN_OWNER_CLAIM_SCHEMA

    run_id: str
    command: str
    invocation_digest: str
    owner_kind: str
    owner_id: str
    process_id: int
    process_start_token: str
    shot_root_device: int
    shot_root_inode: int
    runs_directory_device: int
    runs_directory_inode: int
    run_root_device: int
    run_root_inode: int
    owner_directory_device: int
    owner_directory_inode: int
    claim_device: int
    claim_inode: int
    fence_locator: str
    fence_implementation: str
    fence_device: int
    fence_inode: int
    descriptor_inheritable: bool
    manifest_locator: str
    manifest_sha256: str
    claimed_at: str

    def __post_init__(self) -> None:
        require_run_id(self.run_id, "RunOwnerClaim.run_id")
        require_id(self.command, "RunOwnerClaim.command")
        require_digest(self.invocation_digest, "RunOwnerClaim.invocation_digest")
        if self.owner_kind not in RUN_OWNER_KINDS:
            raise ValueError(f"RunOwnerClaim.owner_kind must be one of {sorted(RUN_OWNER_KINDS)}")
        require_digest(self.owner_id, "RunOwnerClaim.owner_id")
        positive_int(self.process_id, "RunOwnerClaim.process_id")
        require_digest(
            self.process_start_token,
            "RunOwnerClaim.process_start_token",
        )
        _require_filesystem_identity(
            self.shot_root_device,
            self.shot_root_inode,
            "RunOwnerClaim.shot_root",
        )
        _require_filesystem_identity(
            self.runs_directory_device,
            self.runs_directory_inode,
            "RunOwnerClaim.runs_directory",
        )
        _require_filesystem_identity(
            self.run_root_device,
            self.run_root_inode,
            "RunOwnerClaim.run_root",
        )
        _require_filesystem_identity(
            self.owner_directory_device,
            self.owner_directory_inode,
            "RunOwnerClaim.owner_directory",
        )
        _require_filesystem_identity(
            self.claim_device,
            self.claim_inode,
            "RunOwnerClaim.claim",
        )
        fence_locator = relative_locator(self.fence_locator, "RunOwnerClaim.fence_locator")
        if fence_locator != RUN_OWNER_FENCE_LOCATOR:
            raise ValueError(f"RunOwnerClaim.fence_locator must be {RUN_OWNER_FENCE_LOCATOR!r}")
        if self.fence_implementation != RUN_OWNER_FENCE_IMPLEMENTATION:
            raise ValueError(f"RunOwnerClaim.fence_implementation must be {RUN_OWNER_FENCE_IMPLEMENTATION!r}")
        positive_int(
            self.fence_device,
            "RunOwnerClaim.fence_device",
            allow_zero=True,
        )
        positive_int(self.fence_inode, "RunOwnerClaim.fence_inode")
        if self.descriptor_inheritable is not False:
            raise ValueError(
                "RunOwnerClaim.descriptor_inheritable must be false; inherited descriptors cannot own run finalization"
            )
        manifest = relative_locator(
            self.manifest_locator,
            "RunOwnerClaim.manifest_locator",
        )
        if manifest != "manifest.json":
            raise ValueError("RunOwnerClaim.manifest_locator must be 'manifest.json'")
        require_digest(self.manifest_sha256, "RunOwnerClaim.manifest_sha256")
        timestamp(self.claimed_at, "RunOwnerClaim.claimed_at")

    def _identity_payload(self) -> dict[str, Any]:
        """Semantic identity excludes the audit timestamp by HIR-0172."""

        return {
            "schema": self.SCHEMA,
            "run_id": self.run_id,
            "command": self.command,
            "invocation_digest": self.invocation_digest,
            "owner_kind": self.owner_kind,
            "owner_id": self.owner_id,
            "process_id": self.process_id,
            "process_start_token": self.process_start_token,
            "shot_root_device": self.shot_root_device,
            "shot_root_inode": self.shot_root_inode,
            "runs_directory_device": self.runs_directory_device,
            "runs_directory_inode": self.runs_directory_inode,
            "run_root_device": self.run_root_device,
            "run_root_inode": self.run_root_inode,
            "owner_directory_device": self.owner_directory_device,
            "owner_directory_inode": self.owner_directory_inode,
            "claim_device": self.claim_device,
            "claim_inode": self.claim_inode,
            "fence_locator": self.fence_locator,
            "fence_implementation": self.fence_implementation,
            "fence_device": self.fence_device,
            "fence_inode": self.fence_inode,
            "descriptor_inheritable": self.descriptor_inheritable,
            "manifest_locator": self.manifest_locator,
            "manifest_sha256": self.manifest_sha256,
        }

    @property
    def digest(self) -> str:
        return canonical_digest(self._payload())

    @property
    def semantic_identity_digest(self) -> str:
        return canonical_digest(self._identity_payload())

    def _payload(self) -> dict[str, Any]:
        return {
            **self._identity_payload(),
            "claimed_at": self.claimed_at,
            "semantic_identity_digest": self.semantic_identity_digest,
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            **self._payload(),
            "claim_digest": self.digest,
        }

    @classmethod
    def from_dict(
        cls,
        value: object,
        where: str = "run owner claim",
    ) -> RunOwnerClaim:
        fields = frozenset(
            {
                "run_id",
                "command",
                "invocation_digest",
                "owner_kind",
                "owner_id",
                "process_id",
                "process_start_token",
                "shot_root_device",
                "shot_root_inode",
                "runs_directory_device",
                "runs_directory_inode",
                "run_root_device",
                "run_root_inode",
                "owner_directory_device",
                "owner_directory_inode",
                "claim_device",
                "claim_inode",
                "fence_locator",
                "fence_implementation",
                "fence_device",
                "fence_inode",
                "descriptor_inheritable",
                "manifest_locator",
                "manifest_sha256",
                "claimed_at",
                "semantic_identity_digest",
                "claim_digest",
            }
        )
        row = exact_record(value, where, cls.SCHEMA, fields)
        candidate = cls(
            run_id=row["run_id"],
            command=row["command"],
            invocation_digest=row["invocation_digest"],
            owner_kind=row["owner_kind"],
            owner_id=row["owner_id"],
            process_id=row["process_id"],
            process_start_token=row["process_start_token"],
            shot_root_device=row["shot_root_device"],
            shot_root_inode=row["shot_root_inode"],
            runs_directory_device=row["runs_directory_device"],
            runs_directory_inode=row["runs_directory_inode"],
            run_root_device=row["run_root_device"],
            run_root_inode=row["run_root_inode"],
            owner_directory_device=row["owner_directory_device"],
            owner_directory_inode=row["owner_directory_inode"],
            claim_device=row["claim_device"],
            claim_inode=row["claim_inode"],
            fence_locator=row["fence_locator"],
            fence_implementation=row["fence_implementation"],
            fence_device=row["fence_device"],
            fence_inode=row["fence_inode"],
            descriptor_inheritable=row["descriptor_inheritable"],
            manifest_locator=row["manifest_locator"],
            manifest_sha256=row["manifest_sha256"],
            claimed_at=row["claimed_at"],
        )
        require_canonical_digest(
            row["semantic_identity_digest"],
            candidate.semantic_identity_digest,
            where,
            "semantic_identity_digest",
        )
        require_canonical_digest(
            row["claim_digest"],
            candidate.digest,
            where,
            "claim_digest",
        )
        return candidate
