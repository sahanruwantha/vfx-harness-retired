"""Pure, closed records for process-owned run termination.

An interruption is an observation that execution stopped.  It is not a failed-work
classification and grants no recovery transaction.  This module therefore keeps the
HIR-0164 :class:`~vfx_harness.domain.stop_envelopes.StopEnvelope` on the failed branch
of the run-status matrix and gives interruption its own action-free receipt.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, ClassVar

from vfx_harness.domain.run_authority_source_closure import (
    InterruptionAuthoritySourceClosure,
)
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
    timestamp as _timestamp,
)
from vfx_harness.domain.run_owner_claims import RUN_OWNER_CLAIM_LOCATOR, RunOwnerClaim
from vfx_harness.domain.run_owner_loss import RunOwnerLossObservation
from vfx_harness.domain.run_record_refs import RunRecordRef
from vfx_harness.domain.stop_envelope_primitives import (
    canonical_digest,
    require_canonical_digest,
    require_digest,
    require_id,
)

RUN_AUTHORITY_SNAPSHOT_SCHEMA = "vfx-harness.run-authority-snapshot/v1"
INTERRUPTION_AUTHORITY_OBSERVATION_SCHEMA = "vfx-harness.interruption-authority-observation/v1"
INTERRUPTION_TRANSCRIPT_FRONTIER_SCHEMA = "vfx-harness.interruption-transcript-frontier/v1"
INTERRUPTION_RECEIPT_SCHEMA = "vfx-harness.interruption-receipt/v1"
INTERRUPTION_RECEIPT_LOCATOR = "reports/interruption-receipt.json"
INTERRUPTION_AUTHORITY_OBSERVATION_LOCATOR = "reports/interruption-authority-observation.json"
INTERRUPTION_OWNER_LOSS_OBSERVATION_LOCATOR = "reports/run-owner-loss-observation.json"
INTERRUPTION_TRANSCRIPT_FRONTIER_DIRECTORY = "reports/interruption-transcript-frontiers"
INTERRUPTION_KINDS = frozenset({"operator_interrupt", "termination_request", "owner_lost"})
TRANSCRIPT_FRONTIER_STATES = frozenset({"closed", "incomplete"})

_KIND_SIGNAL = {
    "operator_interrupt": 2,
    "termination_request": 15,
}


def transcript_frontier_record_locator(frontier: InterruptionTranscriptFrontier) -> str:
    """Return the only run-owned record locator for one transcript frontier."""

    if not isinstance(frontier, InterruptionTranscriptFrontier):
        raise ValueError("transcript frontier record locator requires a typed frontier")
    return f"{INTERRUPTION_TRANSCRIPT_FRONTIER_DIRECTORY}/{frontier.digest}.json"


@dataclass(frozen=True, slots=True)
class RunAuthoritySnapshot:
    """One timestamped, closed source graph for selected and durable authority."""

    SCHEMA: ClassVar[str] = RUN_AUTHORITY_SNAPSHOT_SCHEMA

    run_id: str
    captured_at: str
    source_closure: InterruptionAuthoritySourceClosure

    def __post_init__(self) -> None:
        require_run_id(self.run_id, "RunAuthoritySnapshot.run_id")
        _timestamp(self.captured_at, "RunAuthoritySnapshot.captured_at")
        if not isinstance(self.source_closure, InterruptionAuthoritySourceClosure):
            raise ValueError("RunAuthoritySnapshot.source_closure must be the typed authority source closure")

    @classmethod
    def mint(
        cls,
        *,
        run_id: str,
        source_closure: InterruptionAuthoritySourceClosure,
        captured_at: str,
    ) -> RunAuthoritySnapshot:
        return cls(
            run_id=run_id,
            captured_at=captured_at,
            source_closure=source_closure,
        )

    @property
    def authority_digest(self) -> str:
        return self.source_closure.digest

    def _payload(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "run_id": self.run_id,
            "captured_at": self.captured_at,
            "source_closure": self.source_closure.as_dict(),
            "authority_digest": self.authority_digest,
        }

    @property
    def digest(self) -> str:
        return canonical_digest(self._payload())

    def as_dict(self) -> dict[str, Any]:
        return {**self._payload(), "snapshot_digest": self.digest}

    @classmethod
    def from_dict(
        cls,
        value: object,
        where: str = "run authority snapshot",
    ) -> RunAuthoritySnapshot:
        fields = frozenset(
            {
                "run_id",
                "captured_at",
                "source_closure",
                "authority_digest",
                "snapshot_digest",
            }
        )
        row = _exact_record(value, where, cls.SCHEMA, fields)
        candidate = cls.mint(
            run_id=row["run_id"],
            source_closure=InterruptionAuthoritySourceClosure.from_dict(
                row["source_closure"],
                f"{where}.source_closure",
            ),
            captured_at=row["captured_at"],
        )
        require_canonical_digest(
            row["authority_digest"],
            candidate.authority_digest,
            where,
            "authority_digest",
        )
        require_canonical_digest(
            row["snapshot_digest"],
            candidate.digest,
            where,
            "snapshot_digest",
        )
        return candidate


@dataclass(frozen=True, slots=True)
class InterruptionAuthorityObservation:
    """Exact equal authority snapshots bracketing interruption reconciliation."""

    SCHEMA: ClassVar[str] = INTERRUPTION_AUTHORITY_OBSERVATION_SCHEMA

    run_id: str
    before: RunAuthoritySnapshot
    after: RunAuthoritySnapshot

    def __post_init__(self) -> None:
        require_run_id(self.run_id, "InterruptionAuthorityObservation.run_id")
        if not isinstance(self.before, RunAuthoritySnapshot) or not isinstance(
            self.after,
            RunAuthoritySnapshot,
        ):
            raise ValueError("InterruptionAuthorityObservation requires typed before and after snapshots")
        if self.before.run_id != self.run_id or self.after.run_id != self.run_id:
            raise ValueError("interruption authority snapshots name another run")
        if self.before.source_closure != self.after.source_closure:
            raise ValueError("interruption authority changed during terminal reconciliation")
        _chronological(
            self.before.captured_at,
            self.after.captured_at,
            "interruption authority observation",
        )

    def _payload(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "run_id": self.run_id,
            "before": self.before.as_dict(),
            "after": self.after.as_dict(),
            "authority_digest": self.before.authority_digest,
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
        where: str = "interruption authority observation",
    ) -> InterruptionAuthorityObservation:
        fields = frozenset({"run_id", "before", "after", "authority_digest", "observation_digest"})
        row = _exact_record(value, where, cls.SCHEMA, fields)
        candidate = cls(
            run_id=row["run_id"],
            before=RunAuthoritySnapshot.from_dict(row["before"], f"{where}.before"),
            after=RunAuthoritySnapshot.from_dict(row["after"], f"{where}.after"),
        )
        require_canonical_digest(
            row["authority_digest"],
            candidate.before.authority_digest,
            where,
            "authority_digest",
        )
        require_canonical_digest(
            row["observation_digest"],
            candidate.digest,
            where,
            "observation_digest",
        )
        return candidate


@dataclass(frozen=True, slots=True)
class InterruptionTranscriptFrontier:
    """Hashed bytes and the last complete event of one existing transcript."""

    SCHEMA: ClassVar[str] = INTERRUPTION_TRANSCRIPT_FRONTIER_SCHEMA

    run_id: str
    locator: str
    sha256: str
    byte_count: int
    truncated_tail: bool
    last_complete_sequence: int | None
    last_complete_kind: str | None
    state: str
    captured_at: str

    def __post_init__(self) -> None:
        require_run_id(self.run_id, "InterruptionTranscriptFrontier.run_id")
        _relative_locator(
            self.locator,
            "InterruptionTranscriptFrontier.locator",
        )
        require_digest(self.sha256, "InterruptionTranscriptFrontier.sha256")
        _positive_int(
            self.byte_count,
            "InterruptionTranscriptFrontier.byte_count",
            allow_zero=True,
        )
        if not isinstance(self.truncated_tail, bool):
            raise ValueError("InterruptionTranscriptFrontier.truncated_tail must be a Boolean")
        sequence = self.last_complete_sequence
        if sequence is not None:
            _positive_int(
                sequence,
                "InterruptionTranscriptFrontier.last_complete_sequence",
            )
        kind = self.last_complete_kind
        if (sequence is None) is not (kind is None):
            raise ValueError("transcript frontier sequence and event kind must appear together")
        if kind is not None:
            require_id(kind, "InterruptionTranscriptFrontier.last_complete_kind")
        if self.state not in TRANSCRIPT_FRONTIER_STATES:
            raise ValueError(
                f"InterruptionTranscriptFrontier.state must be one of {sorted(TRANSCRIPT_FRONTIER_STATES)}"
            )
        if self.state == "closed" and kind != "close":
            raise ValueError("a closed transcript frontier must end at a real close event")
        if self.state == "closed" and self.truncated_tail:
            raise ValueError("a closed transcript frontier cannot have a truncated tail")
        if self.state == "incomplete" and kind == "close":
            raise ValueError("an incomplete transcript frontier cannot relabel a close event")
        if self.byte_count == 0 and self.truncated_tail:
            raise ValueError("an empty transcript cannot have a truncated tail")
        _timestamp(self.captured_at, "InterruptionTranscriptFrontier.captured_at")

    def _payload(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "run_id": self.run_id,
            "locator": self.locator,
            "sha256": self.sha256,
            "byte_count": self.byte_count,
            "truncated_tail": self.truncated_tail,
            "last_complete_sequence": self.last_complete_sequence,
            "last_complete_kind": self.last_complete_kind,
            "state": self.state,
            "captured_at": self.captured_at,
        }

    @property
    def digest(self) -> str:
        return canonical_digest(self._payload())

    def as_dict(self) -> dict[str, Any]:
        return {**self._payload(), "frontier_digest": self.digest}

    @classmethod
    def from_dict(
        cls,
        value: object,
        where: str = "interruption transcript frontier",
    ) -> InterruptionTranscriptFrontier:
        fields = frozenset(
            {
                "run_id",
                "locator",
                "sha256",
                "byte_count",
                "truncated_tail",
                "last_complete_sequence",
                "last_complete_kind",
                "state",
                "captured_at",
                "frontier_digest",
            }
        )
        row = _exact_record(value, where, cls.SCHEMA, fields)
        candidate = cls(
            run_id=row["run_id"],
            locator=row["locator"],
            sha256=row["sha256"],
            byte_count=row["byte_count"],
            truncated_tail=row["truncated_tail"],
            last_complete_sequence=row["last_complete_sequence"],
            last_complete_kind=row["last_complete_kind"],
            state=row["state"],
            captured_at=row["captured_at"],
        )
        require_canonical_digest(
            row["frontier_digest"],
            candidate.digest,
            where,
            "frontier_digest",
        )
        return candidate


def _frontiers(
    value: Iterable[InterruptionTranscriptFrontier],
    where: str,
) -> tuple[InterruptionTranscriptFrontier, ...]:
    if isinstance(value, (str, bytes, Mapping, set, frozenset)):
        raise ValueError(f"{where} must be an ordered iterable of typed frontiers")
    rows = tuple(value)
    if any(not isinstance(row, InterruptionTranscriptFrontier) for row in rows):
        raise ValueError(f"{where} must contain only typed transcript frontiers")
    locators = tuple(row.locator for row in rows)
    if len(locators) != len(set(locators)):
        raise ValueError(f"{where} contains duplicate transcript locators")
    if locators != tuple(sorted(locators)):
        raise ValueError(f"{where} must be sorted by transcript locator")
    return rows


@dataclass(frozen=True, slots=True)
class RunInterruptionReceipt:
    """Action-free terminal evidence for one owned interruption."""

    SCHEMA: ClassVar[str] = INTERRUPTION_RECEIPT_SCHEMA

    run_id: str
    interruption_kind: str
    terminalizer_kind: str
    owner: RunOwnerClaim
    owner_ref: RunRecordRef
    owner_loss: RunOwnerLossObservation | None
    owner_loss_ref: RunRecordRef | None
    authority: InterruptionAuthorityObservation
    authority_ref: RunRecordRef
    transcript_frontiers: tuple[InterruptionTranscriptFrontier, ...]
    transcript_frontier_refs: tuple[RunRecordRef, ...]
    signal_number: int | None
    exit_code: int | None
    interrupted_at: str

    def __post_init__(self) -> None:
        require_run_id(self.run_id, "RunInterruptionReceipt.run_id")
        if self.interruption_kind not in INTERRUPTION_KINDS:
            raise ValueError(f"RunInterruptionReceipt.interruption_kind must be one of {sorted(INTERRUPTION_KINDS)}")
        expected_terminalizer = "reconciler" if self.interruption_kind == "owner_lost" else "owner"
        if self.terminalizer_kind != expected_terminalizer:
            raise ValueError("interruption terminalizer_kind must be derived from interruption_kind")
        if not isinstance(self.owner, RunOwnerClaim):
            raise ValueError("interruption receipt requires the typed run owner")
        if self.owner.run_id != self.run_id:
            raise ValueError("interruption receipt owner names another run")
        if not isinstance(self.owner_ref, RunRecordRef):
            raise ValueError("interruption receipt requires the typed owner source reference")
        self.owner_ref.require_record(
            schema=RunOwnerClaim.SCHEMA,
            digest=self.owner.digest,
            where="interruption owner reference",
        )
        if self.owner_ref.locator != RUN_OWNER_CLAIM_LOCATOR:
            raise ValueError("interruption owner reference must select the canonical owner claim")
        if not isinstance(self.authority, InterruptionAuthorityObservation):
            raise ValueError("interruption receipt requires typed authority reconciliation")
        if self.authority.run_id != self.run_id:
            raise ValueError("interruption receipt authority names another run")
        if not isinstance(self.authority_ref, RunRecordRef):
            raise ValueError("interruption receipt requires the typed authority source reference")
        self.authority_ref.require_record(
            schema=InterruptionAuthorityObservation.SCHEMA,
            digest=self.authority.digest,
            where="interruption authority reference",
        )
        if self.authority_ref.locator != INTERRUPTION_AUTHORITY_OBSERVATION_LOCATOR:
            raise ValueError("interruption authority reference must select its canonical report locator")
        frontiers = _frontiers(
            self.transcript_frontiers,
            "RunInterruptionReceipt.transcript_frontiers",
        )
        object.__setattr__(self, "transcript_frontiers", frontiers)
        if any(row.run_id != self.run_id for row in frontiers):
            raise ValueError("interruption receipt transcript names another run")
        refs = tuple(self.transcript_frontier_refs)
        if len(refs) != len(frontiers) or any(not isinstance(ref, RunRecordRef) for ref in refs):
            raise ValueError("interruption receipt requires one typed source reference per transcript frontier")
        if len({ref.locator for ref in refs}) != len(refs):
            raise ValueError("interruption receipt contains duplicate frontier source locators")
        for index, (frontier, ref) in enumerate(zip(frontiers, refs, strict=True)):
            ref.require_record(
                schema=InterruptionTranscriptFrontier.SCHEMA,
                digest=frontier.digest,
                where=f"interruption transcript frontier reference {index}",
            )
            if ref.locator != transcript_frontier_record_locator(frontier):
                raise ValueError(
                    f"interruption transcript frontier reference {index} must select its canonical record locator"
                )
        object.__setattr__(self, "transcript_frontier_refs", refs)
        all_refs = (
            self.owner_ref,
            self.authority_ref,
            *refs,
            *(
                ()
                if not isinstance(self.owner_loss, RunOwnerLossObservation)
                else (self.owner_loss.prior_running_status.status_snapshot_ref,)
            ),
            *((self.owner_loss_ref,) if self.owner_loss_ref is not None else ()),
        )
        if len({ref.locator for ref in all_refs}) != len(all_refs):
            raise ValueError("interruption receipt source locators must be unique")

        _timestamp(self.interrupted_at, "RunInterruptionReceipt.interrupted_at")
        expected_signal: int | None
        expected_exit: int | None
        if self.interruption_kind == "owner_lost":
            if not isinstance(self.owner_loss, RunOwnerLossObservation):
                raise ValueError("owner_lost interruption requires an owner-loss observation")
            if not isinstance(self.owner_loss_ref, RunRecordRef):
                raise ValueError("owner_lost interruption requires its source reference")
            self.owner_loss.require_matches_owner(self.owner)
            self.owner_loss_ref.require_record(
                schema=RunOwnerLossObservation.SCHEMA,
                digest=self.owner_loss.digest,
                where="interruption owner-loss reference",
            )
            if self.owner_loss_ref.locator != INTERRUPTION_OWNER_LOSS_OBSERVATION_LOCATOR:
                raise ValueError("interruption owner-loss reference must select its canonical report locator")
            # A supervisor wait result is exact audit evidence, but owner loss does
            # not prove which signal or exit caused the process to disappear.
            expected_signal = None
            expected_exit = None
            _chronological(
                self.owner.claimed_at,
                self.owner_loss.exclusive_acquisition_observed_at,
                "owner-loss interruption",
            )
            _chronological(
                self.owner_loss.exclusive_acquisition_observed_at,
                self.authority.before.captured_at,
                "owner-loss fence/authority capture",
            )
            _chronological(
                self.owner_loss.prior_running_status.captured_at,
                self.authority.before.captured_at,
                "owner-loss prior-status/authority capture",
            )
            _chronological(
                self.owner_loss.prior_running_status.captured_at,
                self.interrupted_at,
                "owner-loss prior-status capture/seal",
            )
        else:
            if self.owner_loss is not None or self.owner_loss_ref is not None:
                raise ValueError("signal-owned interruption cannot carry an owner-loss observation")
            expected_signal = _KIND_SIGNAL[self.interruption_kind]
            expected_exit = 128 + expected_signal
        if self.signal_number != expected_signal or self.exit_code != expected_exit:
            raise ValueError("interruption signal and exit code must be derived from its typed observation")

        _chronological(
            self.owner.claimed_at,
            self.authority.before.captured_at,
            "interruption receipt owner/authority",
        )
        _chronological(
            self.authority.after.captured_at,
            self.interrupted_at,
            "interruption receipt authority/seal",
        )
        for frontier in frontiers:
            _chronological(
                self.owner.claimed_at,
                frontier.captured_at,
                "interruption receipt owner/transcript frontier",
            )
            if self.owner_loss is not None:
                _chronological(
                    self.owner_loss.exclusive_acquisition_observed_at,
                    frontier.captured_at,
                    "owner-loss fence/transcript frontier",
                )
                _chronological(
                    self.owner_loss.prior_running_status.captured_at,
                    frontier.captured_at,
                    "owner-loss prior-status/transcript frontier",
                )
            _chronological(
                frontier.captured_at,
                self.interrupted_at,
                "interruption transcript frontier/seal",
            )

    def _payload(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "run_id": self.run_id,
            "interruption_kind": self.interruption_kind,
            "terminalizer_kind": self.terminalizer_kind,
            "owner": self.owner.as_dict(),
            "owner_ref": self.owner_ref.as_dict(),
            "owner_loss": None if self.owner_loss is None else self.owner_loss.as_dict(),
            "owner_loss_ref": (None if self.owner_loss_ref is None else self.owner_loss_ref.as_dict()),
            "authority": self.authority.as_dict(),
            "authority_ref": self.authority_ref.as_dict(),
            "transcript_frontiers": [row.as_dict() for row in self.transcript_frontiers],
            "transcript_frontier_refs": [row.as_dict() for row in self.transcript_frontier_refs],
            "signal_number": self.signal_number,
            "exit_code": self.exit_code,
            "interrupted_at": self.interrupted_at,
        }

    @property
    def digest(self) -> str:
        return canonical_digest(self._payload())

    def as_dict(self) -> dict[str, Any]:
        return {**self._payload(), "receipt_digest": self.digest}

    @classmethod
    def from_dict(
        cls,
        value: object,
        where: str = "interruption receipt",
    ) -> RunInterruptionReceipt:
        fields = frozenset(
            {
                "run_id",
                "interruption_kind",
                "terminalizer_kind",
                "owner",
                "owner_ref",
                "owner_loss",
                "owner_loss_ref",
                "authority",
                "authority_ref",
                "transcript_frontiers",
                "transcript_frontier_refs",
                "signal_number",
                "exit_code",
                "interrupted_at",
                "receipt_digest",
            }
        )
        row = _exact_record(value, where, cls.SCHEMA, fields)
        raw_frontiers = row["transcript_frontiers"]
        raw_frontier_refs = row["transcript_frontier_refs"]
        if not isinstance(raw_frontiers, list):
            raise ValueError(f"{where}.transcript_frontiers must be a list")
        if not isinstance(raw_frontier_refs, list):
            raise ValueError(f"{where}.transcript_frontier_refs must be a list")
        owner = RunOwnerClaim.from_dict(row["owner"], f"{where}.owner")
        owner_loss = (
            None
            if row["owner_loss"] is None
            else RunOwnerLossObservation.from_dict(
                row["owner_loss"],
                f"{where}.owner_loss",
                prior_owner=owner,
            )
        )
        owner_loss_ref = (
            None
            if row["owner_loss_ref"] is None
            else RunRecordRef.from_dict(
                row["owner_loss_ref"],
                f"{where}.owner_loss_ref",
            )
        )
        candidate = cls(
            run_id=row["run_id"],
            interruption_kind=row["interruption_kind"],
            terminalizer_kind=row["terminalizer_kind"],
            owner=owner,
            owner_ref=RunRecordRef.from_dict(row["owner_ref"], f"{where}.owner_ref"),
            owner_loss=owner_loss,
            owner_loss_ref=owner_loss_ref,
            authority=InterruptionAuthorityObservation.from_dict(
                row["authority"],
                f"{where}.authority",
            ),
            authority_ref=RunRecordRef.from_dict(
                row["authority_ref"],
                f"{where}.authority_ref",
            ),
            transcript_frontiers=tuple(
                InterruptionTranscriptFrontier.from_dict(
                    frontier,
                    f"{where}.transcript_frontiers[{index}]",
                )
                for index, frontier in enumerate(raw_frontiers)
            ),
            transcript_frontier_refs=tuple(
                RunRecordRef.from_dict(
                    ref,
                    f"{where}.transcript_frontier_refs[{index}]",
                )
                for index, ref in enumerate(raw_frontier_refs)
            ),
            signal_number=row["signal_number"],
            exit_code=row["exit_code"],
            interrupted_at=row["interrupted_at"],
        )
        require_canonical_digest(
            row["receipt_digest"],
            candidate.digest,
            where,
            "receipt_digest",
        )
        return candidate
