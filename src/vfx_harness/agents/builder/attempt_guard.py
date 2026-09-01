"""Exact work-unit attempt authority for short builder operations."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from functools import wraps
from pathlib import Path
from typing import Any, TypeVar

from vfx_harness.agents.builder.execution_guard import (
    ExecutionAuthorityLost,
    ExecutionGuard,
    LedgerPublicationScope,
)
from vfx_harness.domain.unit_attempts import UnitAttemptClaim
from vfx_harness.domain.work_units import WorkUnit
from vfx_harness.orchestration.authority_selection import ResolvedSelectedAuthority
from vfx_harness.orchestration.unit_state_claims import active_unit_attempt_guard

_T = TypeVar("_T")


class UnitAttemptAuthorityLost(ExecutionAuthorityLost):
    """The exact claimed unit attempt no longer owns execution or publication."""


@dataclass(frozen=True, slots=True)
class UnitAttemptGuard:
    """Bind one claim to its selected authority, complete DAG, and active unit.

    The shot-wide builder fence excludes another live builder.  This narrower guard
    handles a different race: a legitimate concurrent replan may revoke the durable
    claim.  Each short builder mutation therefore holds selected-authority SH followed
    by unit-state SH while it verifies and uses the exact claim.
    """

    folder: Path
    layer_id: str
    unit: WorkUnit
    units: tuple[WorkUnit, ...]
    claim: UnitAttemptClaim
    expected_plan_hash: str
    selected_authority: ResolvedSelectedAuthority

    @classmethod
    def bind(
        cls,
        folder: str | Path,
        layer_id: str,
        unit: WorkUnit,
        units: tuple[WorkUnit, ...],
        claim: UnitAttemptClaim,
        *,
        expected_plan_hash: str,
        selected_authority: ResolvedSelectedAuthority,
    ) -> UnitAttemptGuard:
        guard = cls(
            folder=Path(folder),
            layer_id=str(layer_id),
            unit=unit,
            units=tuple(units),
            claim=claim,
            expected_plan_hash=str(expected_plan_hash),
            selected_authority=selected_authority,
        )
        guard.check("bind work-unit attempt guard")
        return guard

    def promoted(self, claim: UnitAttemptClaim) -> UnitAttemptGuard:
        """Return the same authority binding with its exact promoted claim."""

        promoted = replace(self, claim=claim)
        promoted.check("bind promoted work-unit attempt guard")
        return promoted

    @property
    def label(self) -> str:
        """Human-readable diagnostics; exact authority remains the typed claim."""

        return f"unit {self.layer_id}.{self.unit.id}"

    @property
    def authority_binding(self) -> dict[str, Any]:
        """Typed identity incorporated into prepared builder publications."""

        return {"attempt": self.claim.as_dict()}

    @property
    def ledger_publication_scope(self) -> LedgerPublicationScope:
        """Permit this attempt to mutate only its exact unit diagnostic row."""

        return LedgerPublicationScope(
            kind="unit_attempt",
            milestone_id=f"{self.layer_id}@{self.unit.id}",
            claim_id=self.claim.claim_id,
        )

    def require_unit_boundary(
        self,
        milestone: object,
        active_unit: WorkUnit,
        *,
        layer: object | None = None,
        script_rel: str | None = None,
    ) -> None:
        """Refuse caller-supplied identities outside this exact unit attempt."""

        expected_milestone = self.ledger_publication_scope.milestone_id
        observed_milestone = str(getattr(milestone, "id", ""))
        if observed_milestone != expected_milestone:
            raise ValueError(
                "work-unit attempt requires its exact unit milestone: "
                f"expected {expected_milestone!r}, found {observed_milestone!r}"
            )
        if active_unit != self.unit:
            raise ValueError(
                "work-unit attempt active unit does not match its exact claimed unit"
            )
        if layer is not None and str(getattr(layer, "id", "")) != self.layer_id:
            raise ValueError(
                "work-unit attempt layer view belongs to another layer: "
                f"expected {self.layer_id!r}, found {str(getattr(layer, 'id', ''))!r}"
            )
        if script_rel is not None:
            spans = tuple(self.unit.mutates.script_spans)
            if len(spans) != 1 or str(script_rel) != spans[0]:
                raise ValueError(
                    "work-unit attempt script does not match its identity-derived unit file: "
                    f"expected {list(spans)!r}, found {str(script_rel)!r}"
                )

    @contextmanager
    def hold(self, operation: str) -> Iterator[UnitAttemptClaim]:
        """Keep the exact claim current for one bounded operation."""

        try:
            with active_unit_attempt_guard(
                self.folder,
                self.layer_id,
                self.unit.id,
                self.units,
                self.claim,
                expected_plan_hash=self.expected_plan_hash,
                selection_token=self.selected_authority.selection_token,
            ) as current:
                yield current
        except UnitAttemptAuthorityLost:
            raise
        except (KeyError, OSError, TypeError, ValueError) as exc:
            raise UnitAttemptAuthorityLost(
                f"{operation} refused because work-unit attempt "
                f"{self.layer_id}.{self.unit.id} ({self.claim.claim_id}) lost authority: {exc}"
            ) from exc

    def check(self, operation: str) -> UnitAttemptClaim:
        """Revalidate without retaining either inner lock."""

        with self.hold(operation) as current:
            return current

    def publish(self, operation: str, mutation: Callable[[], _T]) -> _T:
        """Run one short durable mutation while replan revocation is excluded."""

        with self.hold(operation):
            return mutation()


class AttemptBoundBlenderSession:
    """Proxy every Blender operation through one exact execution guard.

    The proxy deliberately guards reads as well as writes.  Several apparently
    read-only Blender commands re-evaluate frames, write renders, or manipulate a
    transactional diagnostic scene, so a name-based allowlist would be incomplete.
    """

    def __init__(self, session: Any, guard: ExecutionGuard) -> None:
        self._session = session
        self._execution_guard = guard

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._session, name)
        if not callable(attribute):
            return attribute

        if name == "snapshot":
            def snapshot(tag: str) -> dict:
                self._execution_guard.check("start Blender snapshot staging")
                staged = self._session.stage_snapshot(tag)
                self._execution_guard.check("finish Blender snapshot staging")
                prepared = self._session.prepare_snapshot_publication(staged)
                try:
                    return self._execution_guard.publish(
                        "publish Blender snapshot checkpoint",
                        lambda: self._session.commit_snapshot_publication(prepared),
                    )
                except BaseException:
                    self._session.discard_snapshot_publication(prepared)
                    raise

            return snapshot

        if name == "journal":
            def journal(*args, **kwargs) -> dict:
                self._execution_guard.check("start Blender journal staging")
                staged = self._session.stage_journal(*args, **kwargs)
                if staged[1] is None:
                    self._execution_guard.check("finish Blender journal read")
                    return staged[0]
                self._execution_guard.check("finish Blender journal staging")
                prepared = self._session.prepare_journal_publication(staged)
                try:
                    return self._execution_guard.publish(
                        "publish Blender journal checkpoint",
                        lambda: self._session.commit_journal_publication(prepared),
                    )
                except BaseException:
                    self._session.discard_journal_publication(prepared)
                    raise

            return journal

        @wraps(attribute)
        def invoke(*args, **kwargs):
            self._execution_guard.check(f"start Blender session operation {name}")
            result = attribute(*args, **kwargs)
            self._execution_guard.check(f"finish Blender session operation {name}")
            return result

        return invoke
