"""Closed authority-source families observed at run interruption.

This module is pure domain state. Source-byte and typed-record primitives live in
:mod:`run_authority_source_identity`; filesystem discovery and verification belong
to an observability adapter.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, ClassVar

from vfx_harness.domain.run_authority_source_identity import (
    AUTHORITY_SOURCE_IDENTITY_SCHEMA,
    AuthoritySourceIdentity,
)
from vfx_harness.domain.run_authority_source_identity import (
    VIEW_MEMBER_PATTERN as _VIEW_MEMBER,
)
from vfx_harness.domain.run_authority_source_identity import (
    canonical_digest as _canonical_digest,
)
from vfx_harness.domain.run_authority_source_identity import (
    canonical_source_rows as _source_rows,
)
from vfx_harness.domain.run_authority_source_identity import (
    derived_family_state as _family_state,
)
from vfx_harness.domain.run_authority_source_identity import (
    exact_record as _exact_record,
)
from vfx_harness.domain.run_authority_source_identity import (
    require_canonical_source_rows as _require_canonical_rows,
)
from vfx_harness.domain.run_authority_source_identity import (
    require_digest as _digest,
)
from vfx_harness.domain.run_authority_source_identity import (
    require_family_state as _require_family_state,
)
from vfx_harness.domain.run_authority_source_identity import (
    require_source_kind as _require_kind,
)

SELECTED_PLAN_SOURCE_CLOSURE_SCHEMA = "vfx-harness.interruption-selected-plan-source-closure/v1"
ACCEPTED_STATE_SOURCE_CLOSURE_SCHEMA = "vfx-harness.interruption-accepted-state-source-closure/v1"
DURABLE_STATE_SOURCE_CLOSURE_SCHEMA = "vfx-harness.interruption-durable-state-source-closure/v1"
INTERRUPTION_AUTHORITY_SOURCE_CLOSURE_SCHEMA = "vfx-harness.interruption-authority-source-closure/v1"


@dataclass(frozen=True, slots=True)
class SelectedPlanSourceClosure:
    """Explicit absence or the exact selected plan/effective-view source graph."""

    SCHEMA: ClassVar[str] = SELECTED_PLAN_SOURCE_CLOSURE_SCHEMA

    state: str
    plan_pointer: AuthoritySourceIdentity | None
    bundle_manifest: AuthoritySourceIdentity | None
    bundle_members: tuple[AuthoritySourceIdentity, ...]
    effective_view_pointer: AuthoritySourceIdentity | None
    effective_view_members: tuple[AuthoritySourceIdentity, ...]
    plan_amendments: AuthoritySourceIdentity | None = None
    plan_resolutions: AuthoritySourceIdentity | None = None
    decision_state: str = "absent"

    def __post_init__(self) -> None:
        _require_canonical_rows(
            self.bundle_members,
            "SelectedPlanSourceClosure.bundle_members",
            kind="plan_bundle_member",
        )
        _require_canonical_rows(
            self.effective_view_members,
            "SelectedPlanSourceClosure.effective_view_members",
            kind="effective_view_member",
        )
        decision_sources = tuple(
            _require_kind(source, kind, f"SelectedPlanSourceClosure.{field}")
            for field, kind, source in (
                ("plan_amendments", "plan_amendments", self.plan_amendments),
                ("plan_resolutions", "plan_resolutions", self.plan_resolutions),
            )
            if source is not None
        )
        _require_family_state(
            self.decision_state,
            _family_state(decision_sources),
            "SelectedPlanSourceClosure.decision_state",
        )
        selected_sources: list[AuthoritySourceIdentity] = []
        if self.state == "absent":
            if any(
                (
                    self.plan_pointer is not None,
                    self.bundle_manifest is not None,
                    bool(self.bundle_members),
                    self.effective_view_pointer is not None,
                    bool(self.effective_view_members),
                )
            ):
                raise ValueError("absent selected plan closure cannot carry selected plan sources")
        else:
            pointer = _require_kind(
                self.plan_pointer,
                "plan_pointer",
                "SelectedPlanSourceClosure.plan_pointer",
            )
            selected_sources.append(pointer)
            if pointer.source_state == "raw_invalid":
                if any(
                    (
                        self.bundle_manifest is not None,
                        bool(self.bundle_members),
                        self.effective_view_pointer is not None,
                        bool(self.effective_view_members),
                    )
                ):
                    raise ValueError("an invalid plan pointer cannot claim derived authority sources")
            else:
                manifest = _require_kind(
                    self.bundle_manifest,
                    "plan_bundle_manifest",
                    "SelectedPlanSourceClosure.bundle_manifest",
                )
                selected_sources.append(manifest)
                if manifest.source_state == "raw_invalid":
                    if any(
                        (
                            bool(self.bundle_members),
                            self.effective_view_pointer is not None,
                            bool(self.effective_view_members),
                        )
                    ):
                        raise ValueError("an invalid bundle manifest cannot claim derived members or view")
                else:
                    if not self.bundle_members:
                        raise ValueError("a valid selected bundle manifest requires its non-empty member closure")
                    selected_sources.extend(self.bundle_members)
                    manifest_root = self.bundle_manifest.locator.removesuffix("bundle.json")
                    if any(
                        not source.locator.startswith(manifest_root) or source.locator == self.bundle_manifest.locator
                        for source in self.bundle_members
                    ):
                        raise ValueError("selected plan bundle members must live below the exact manifest root")

                    if self.effective_view_pointer is None:
                        if self.effective_view_members:
                            raise ValueError("effective-view members require an effective-view pointer")
                    else:
                        view_pointer = _require_kind(
                            self.effective_view_pointer,
                            "effective_view_pointer",
                            "SelectedPlanSourceClosure.effective_view_pointer",
                        )
                        selected_sources.append(view_pointer)
                        if view_pointer.source_state == "raw_invalid":
                            if self.effective_view_members:
                                raise ValueError("an invalid effective-view pointer cannot claim derived members")
                        else:
                            if not self.effective_view_members:
                                raise ValueError("a valid effective-view pointer requires its non-empty member closure")
                            selected_sources.extend(self.effective_view_members)
                            roots = {
                                match.group(1)
                                for source in self.effective_view_members
                                if (match := _VIEW_MEMBER.fullmatch(source.locator)) is not None
                            }
                            if len(roots) != 1:
                                raise ValueError("effective-view members must share one content-addressed view root")

            _require_family_state(
                self.state,
                _family_state(selected_sources),
                "SelectedPlanSourceClosure.state",
            )

        all_locators = tuple(source.locator for source in (*selected_sources, *decision_sources))
        if len(all_locators) != len(set(all_locators)):
            raise ValueError("selected plan source closure contains duplicate locators")

    @classmethod
    def absent(
        cls,
        *,
        plan_amendments: AuthoritySourceIdentity | None = None,
        plan_resolutions: AuthoritySourceIdentity | None = None,
    ) -> SelectedPlanSourceClosure:
        return cls(
            "absent",
            None,
            None,
            (),
            None,
            (),
            plan_amendments,
            plan_resolutions,
            _family_state(tuple(source for source in (plan_amendments, plan_resolutions) if source is not None)),
        )

    @classmethod
    def mint_present(
        cls,
        *,
        plan_pointer: AuthoritySourceIdentity,
        bundle_manifest: AuthoritySourceIdentity | None = None,
        bundle_members: Iterable[AuthoritySourceIdentity] = (),
        effective_view_pointer: AuthoritySourceIdentity | None = None,
        effective_view_members: Iterable[AuthoritySourceIdentity] = (),
        plan_amendments: AuthoritySourceIdentity | None = None,
        plan_resolutions: AuthoritySourceIdentity | None = None,
    ) -> SelectedPlanSourceClosure:
        members = _source_rows(
            bundle_members,
            "SelectedPlanSourceClosure.bundle_members",
            kind="plan_bundle_member",
        )
        view_members = _source_rows(
            effective_view_members,
            "SelectedPlanSourceClosure.effective_view_members",
            kind="effective_view_member",
        )
        selected_sources = tuple(
            source
            for source in (
                plan_pointer,
                bundle_manifest,
                *members,
                effective_view_pointer,
                *view_members,
            )
            if source is not None
        )
        decision_sources = tuple(source for source in (plan_amendments, plan_resolutions) if source is not None)
        return cls(
            _family_state(selected_sources),
            plan_pointer,
            bundle_manifest,
            members,
            effective_view_pointer,
            view_members,
            plan_amendments,
            plan_resolutions,
            _family_state(decision_sources),
        )

    def _payload(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "state": self.state,
            "plan_pointer": (None if self.plan_pointer is None else self.plan_pointer.as_dict()),
            "bundle_manifest": (None if self.bundle_manifest is None else self.bundle_manifest.as_dict()),
            "bundle_members": [source.as_dict() for source in self.bundle_members],
            "effective_view_pointer": (
                None if self.effective_view_pointer is None else self.effective_view_pointer.as_dict()
            ),
            "effective_view_members": [source.as_dict() for source in self.effective_view_members],
            "plan_amendments": None if self.plan_amendments is None else self.plan_amendments.as_dict(),
            "plan_resolutions": None if self.plan_resolutions is None else self.plan_resolutions.as_dict(),
            "decision_state": self.decision_state,
        }

    @property
    def digest(self) -> str:
        return _canonical_digest(self._payload())

    def as_dict(self) -> dict[str, Any]:
        return {**self._payload(), "closure_digest": self.digest}

    @classmethod
    def from_dict(
        cls,
        value: object,
        where: str = "selected plan source closure",
    ) -> SelectedPlanSourceClosure:
        row = _exact_record(
            value,
            where,
            schema=cls.SCHEMA,
            fields=frozenset(
                {
                    "state",
                    "plan_pointer",
                    "bundle_manifest",
                    "bundle_members",
                    "effective_view_pointer",
                    "effective_view_members",
                    "plan_amendments",
                    "plan_resolutions",
                    "decision_state",
                    "closure_digest",
                }
            ),
        )
        if not isinstance(row["bundle_members"], list) or not isinstance(row["effective_view_members"], list):
            raise ValueError(f"{where} member closures must be lists")
        candidate = cls(
            state=row["state"],
            plan_pointer=(
                None
                if row["plan_pointer"] is None
                else AuthoritySourceIdentity.from_dict(row["plan_pointer"], f"{where}.plan_pointer")
            ),
            bundle_manifest=(
                None
                if row["bundle_manifest"] is None
                else AuthoritySourceIdentity.from_dict(row["bundle_manifest"], f"{where}.bundle_manifest")
            ),
            bundle_members=tuple(
                AuthoritySourceIdentity.from_dict(item, f"{where}.bundle_members[{index}]")
                for index, item in enumerate(row["bundle_members"])
            ),
            effective_view_pointer=(
                None
                if row["effective_view_pointer"] is None
                else AuthoritySourceIdentity.from_dict(
                    row["effective_view_pointer"],
                    f"{where}.effective_view_pointer",
                )
            ),
            effective_view_members=tuple(
                AuthoritySourceIdentity.from_dict(
                    item,
                    f"{where}.effective_view_members[{index}]",
                )
                for index, item in enumerate(row["effective_view_members"])
            ),
            plan_amendments=(
                None
                if row["plan_amendments"] is None
                else AuthoritySourceIdentity.from_dict(
                    row["plan_amendments"],
                    f"{where}.plan_amendments",
                )
            ),
            plan_resolutions=(
                None
                if row["plan_resolutions"] is None
                else AuthoritySourceIdentity.from_dict(
                    row["plan_resolutions"],
                    f"{where}.plan_resolutions",
                )
            ),
            decision_state=row["decision_state"],
        )
        observed = _digest(row["closure_digest"], f"{where}.closure_digest")
        if observed != candidate.digest:
            raise ValueError(f"{where}.closure_digest is stale")
        return candidate


@dataclass(frozen=True, slots=True)
class AcceptedStateSourceClosure:
    """Explicit absence or exact durable ledger and referenced accepted sources."""

    SCHEMA: ClassVar[str] = ACCEPTED_STATE_SOURCE_CLOSURE_SCHEMA

    state: str
    ledger: AuthoritySourceIdentity | None
    members: tuple[AuthoritySourceIdentity, ...]
    judgment_debts: AuthoritySourceIdentity | None = None
    judgment_payment_attempts: AuthoritySourceIdentity | None = None
    judgment_state: str = "absent"

    def __post_init__(self) -> None:
        _require_canonical_rows(
            self.members,
            "AcceptedStateSourceClosure.members",
            kind="accepted_member",
        )
        judgment_sources = tuple(
            _require_kind(source, kind, f"AcceptedStateSourceClosure.{field}")
            for field, kind, source in (
                ("judgment_debts", "judgment_debts", self.judgment_debts),
                (
                    "judgment_payment_attempts",
                    "judgment_payment_attempts",
                    self.judgment_payment_attempts,
                ),
            )
            if source is not None
        )
        _require_family_state(
            self.judgment_state,
            _family_state(judgment_sources),
            "AcceptedStateSourceClosure.judgment_state",
        )
        accepted_sources: tuple[AuthoritySourceIdentity, ...]
        if self.state == "absent":
            if self.ledger is not None or self.members:
                raise ValueError("absent accepted-state closure cannot carry accepted ledger sources")
            accepted_sources = ()
        else:
            ledger = _require_kind(
                self.ledger,
                "accepted_ledger",
                "AcceptedStateSourceClosure.ledger",
            )
            if ledger.source_state == "raw_invalid" and self.members:
                raise ValueError("an invalid accepted ledger cannot claim derived members")
            accepted_sources = (ledger, *self.members)
            _require_family_state(
                self.state,
                _family_state(accepted_sources),
                "AcceptedStateSourceClosure.state",
            )
        sources = (*accepted_sources, *judgment_sources)
        if len({source.locator for source in sources}) != len(sources):
            raise ValueError("accepted-state source closure contains duplicate locators")

    @classmethod
    def absent(
        cls,
        *,
        judgment_debts: AuthoritySourceIdentity | None = None,
        judgment_payment_attempts: AuthoritySourceIdentity | None = None,
    ) -> AcceptedStateSourceClosure:
        return cls(
            "absent",
            None,
            (),
            judgment_debts,
            judgment_payment_attempts,
            _family_state(
                tuple(source for source in (judgment_debts, judgment_payment_attempts) if source is not None)
            ),
        )

    @classmethod
    def mint_present(
        cls,
        *,
        ledger: AuthoritySourceIdentity,
        members: Iterable[AuthoritySourceIdentity] = (),
        judgment_debts: AuthoritySourceIdentity | None = None,
        judgment_payment_attempts: AuthoritySourceIdentity | None = None,
    ) -> AcceptedStateSourceClosure:
        rows = _source_rows(
            members,
            "AcceptedStateSourceClosure.members",
            kind="accepted_member",
        )
        accepted_sources = (ledger, *rows)
        judgment_sources = tuple(source for source in (judgment_debts, judgment_payment_attempts) if source is not None)
        return cls(
            _family_state(accepted_sources),
            ledger,
            rows,
            judgment_debts,
            judgment_payment_attempts,
            _family_state(judgment_sources),
        )

    def _payload(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "state": self.state,
            "ledger": None if self.ledger is None else self.ledger.as_dict(),
            "members": [source.as_dict() for source in self.members],
            "judgment_debts": None if self.judgment_debts is None else self.judgment_debts.as_dict(),
            "judgment_payment_attempts": (
                None if self.judgment_payment_attempts is None else self.judgment_payment_attempts.as_dict()
            ),
            "judgment_state": self.judgment_state,
        }

    @property
    def digest(self) -> str:
        return _canonical_digest(self._payload())

    def as_dict(self) -> dict[str, Any]:
        return {**self._payload(), "closure_digest": self.digest}

    @classmethod
    def from_dict(
        cls,
        value: object,
        where: str = "accepted-state source closure",
    ) -> AcceptedStateSourceClosure:
        row = _exact_record(
            value,
            where,
            schema=cls.SCHEMA,
            fields=frozenset(
                {
                    "state",
                    "ledger",
                    "members",
                    "judgment_debts",
                    "judgment_payment_attempts",
                    "judgment_state",
                    "closure_digest",
                }
            ),
        )
        if not isinstance(row["members"], list):
            raise ValueError(f"{where}.members must be a list")
        candidate = cls(
            state=row["state"],
            ledger=(
                None if row["ledger"] is None else AuthoritySourceIdentity.from_dict(row["ledger"], f"{where}.ledger")
            ),
            members=tuple(
                AuthoritySourceIdentity.from_dict(item, f"{where}.members[{index}]")
                for index, item in enumerate(row["members"])
            ),
            judgment_debts=(
                None
                if row["judgment_debts"] is None
                else AuthoritySourceIdentity.from_dict(
                    row["judgment_debts"],
                    f"{where}.judgment_debts",
                )
            ),
            judgment_payment_attempts=(
                None
                if row["judgment_payment_attempts"] is None
                else AuthoritySourceIdentity.from_dict(
                    row["judgment_payment_attempts"],
                    f"{where}.judgment_payment_attempts",
                )
            ),
            judgment_state=row["judgment_state"],
        )
        observed = _digest(row["closure_digest"], f"{where}.closure_digest")
        if observed != candidate.digest:
            raise ValueError(f"{where}.closure_digest is stale")
        return candidate


@dataclass(frozen=True, slots=True)
class DurableStateSourceClosure:
    """Independent roots over one canonical deduplicated object/member table."""

    SCHEMA: ClassVar[str] = DURABLE_STATE_SOURCE_CLOSURE_SCHEMA

    current_state: str
    current_pointer: AuthoritySourceIdentity | None
    head: AuthoritySourceIdentity | None
    records: tuple[AuthoritySourceIdentity, ...]
    members: tuple[AuthoritySourceIdentity, ...]
    pending: AuthoritySourceIdentity | None = None
    pending_state: str = "absent"

    def __post_init__(self) -> None:
        _require_canonical_rows(
            self.records,
            "DurableStateSourceClosure.records",
            kind="durable_state_record",
        )
        _require_canonical_rows(
            self.members,
            "DurableStateSourceClosure.members",
            kind="durable_state_member",
        )
        pending = (
            None
            if self.pending is None
            else _require_kind(
                self.pending,
                "durable_state_pending",
                "DurableStateSourceClosure.pending",
            )
        )

        current_sources: tuple[AuthoritySourceIdentity, ...]
        current_graph_is_valid = False
        if self.current_state == "absent":
            if self.current_pointer is not None or self.head is not None:
                raise ValueError("absent durable-state current closure cannot carry a current pointer or head")
            current_sources = ()
        else:
            pointer = _require_kind(
                self.current_pointer,
                "durable_state_pointer",
                "DurableStateSourceClosure.current_pointer",
            )
            if pointer.source_state == "raw_invalid":
                if self.head is not None:
                    raise ValueError("an invalid durable-state current pointer cannot claim a derived head")
                current_sources = (pointer,)
            else:
                head = _require_kind(
                    self.head,
                    "durable_state_record",
                    "DurableStateSourceClosure.head",
                )
                if head.source_state == "record_valid" and head.record_schema != (
                    "vfx-harness.authority-state-head/v1"
                ):
                    raise ValueError("DurableStateSourceClosure.head must name an authority-state head record")
                if head.source_state == "record_valid" and not self.records:
                    raise ValueError("a valid authority-state head requires its immutable record closure")
                current_sources = (pointer, head, *self.records, *self.members)
                current_graph_is_valid = head.source_state == "record_valid"
            _require_family_state(
                self.current_state,
                _family_state(current_sources),
                "DurableStateSourceClosure.current_state",
            )

        pending_sources: tuple[AuthoritySourceIdentity, ...]
        pending_graph_is_valid = False
        if pending is None:
            pending_sources = ()
            _require_family_state(
                self.pending_state,
                "absent",
                "DurableStateSourceClosure.pending_state",
            )
        elif pending.source_state == "raw_invalid":
            pending_sources = (pending,)
            _require_family_state(
                self.pending_state,
                "present_invalid",
                "DurableStateSourceClosure.pending_state",
            )
        else:
            if not self.records:
                raise ValueError("a valid durable-state pending pointer requires its immutable record closure")
            pending_sources = (
                pending,
                *((self.head,) if self.head is not None else ()),
                *self.records,
                *self.members,
            )
            pending_graph_is_valid = True
            _require_family_state(
                self.pending_state,
                _family_state(pending_sources),
                "DurableStateSourceClosure.pending_state",
            )

        if (self.records or self.members) and not (current_graph_is_valid or pending_graph_is_valid):
            raise ValueError("durable-state object/member tables require a valid current or pending root")
        all_sources = (*current_sources[:2], *pending_sources[:1], *self.records, *self.members)
        locators = tuple(source.locator for source in all_sources)
        if len(locators) != len(set(locators)):
            raise ValueError("durable-state source closure contains duplicate locators")

    @classmethod
    def absent(
        cls,
        *,
        pending: AuthoritySourceIdentity | None = None,
        records: Iterable[AuthoritySourceIdentity] = (),
        members: Iterable[AuthoritySourceIdentity] = (),
    ) -> DurableStateSourceClosure:
        record_rows = _source_rows(
            records,
            "DurableStateSourceClosure.records",
            kind="durable_state_record",
        )
        member_rows = _source_rows(
            members,
            "DurableStateSourceClosure.members",
            kind="durable_state_member",
        )
        pending_sources = tuple(source for source in (pending, *record_rows, *member_rows) if source is not None)
        return cls(
            "absent",
            None,
            None,
            record_rows,
            member_rows,
            pending,
            "absent" if pending is None else _family_state(pending_sources),
        )

    @classmethod
    def mint_present(
        cls,
        *,
        current_pointer: AuthoritySourceIdentity,
        head: AuthoritySourceIdentity | None = None,
        records: Iterable[AuthoritySourceIdentity] = (),
        members: Iterable[AuthoritySourceIdentity] = (),
        pending: AuthoritySourceIdentity | None = None,
    ) -> DurableStateSourceClosure:
        record_rows = _source_rows(
            records,
            "DurableStateSourceClosure.records",
            kind="durable_state_record",
        )
        member_rows = _source_rows(
            members,
            "DurableStateSourceClosure.members",
            kind="durable_state_member",
        )
        current_sources = tuple(
            source for source in (current_pointer, head, *record_rows, *member_rows) if source is not None
        )
        pending_sources = tuple(source for source in (pending, head, *record_rows, *member_rows) if source is not None)
        return cls(
            _family_state(current_sources),
            current_pointer,
            head,
            record_rows,
            member_rows,
            pending,
            "absent" if pending is None else _family_state(pending_sources),
        )

    def _payload(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "current_state": self.current_state,
            "current_pointer": (None if self.current_pointer is None else self.current_pointer.as_dict()),
            "head": None if self.head is None else self.head.as_dict(),
            "records": [source.as_dict() for source in self.records],
            "members": [source.as_dict() for source in self.members],
            "pending": None if self.pending is None else self.pending.as_dict(),
            "pending_state": self.pending_state,
        }

    @property
    def digest(self) -> str:
        return _canonical_digest(self._payload())

    def as_dict(self) -> dict[str, Any]:
        return {**self._payload(), "closure_digest": self.digest}

    @classmethod
    def from_dict(
        cls,
        value: object,
        where: str = "durable-state source closure",
    ) -> DurableStateSourceClosure:
        row = _exact_record(
            value,
            where,
            schema=cls.SCHEMA,
            fields=frozenset(
                {
                    "current_state",
                    "current_pointer",
                    "head",
                    "records",
                    "members",
                    "pending",
                    "pending_state",
                    "closure_digest",
                }
            ),
        )
        if not isinstance(row["records"], list) or not isinstance(row["members"], list):
            raise ValueError(f"{where} record/member closures must be lists")
        candidate = cls(
            current_state=row["current_state"],
            current_pointer=(
                None
                if row["current_pointer"] is None
                else AuthoritySourceIdentity.from_dict(row["current_pointer"], f"{where}.current_pointer")
            ),
            head=(None if row["head"] is None else AuthoritySourceIdentity.from_dict(row["head"], f"{where}.head")),
            records=tuple(
                AuthoritySourceIdentity.from_dict(item, f"{where}.records[{index}]")
                for index, item in enumerate(row["records"])
            ),
            members=tuple(
                AuthoritySourceIdentity.from_dict(item, f"{where}.members[{index}]")
                for index, item in enumerate(row["members"])
            ),
            pending=(
                None
                if row["pending"] is None
                else AuthoritySourceIdentity.from_dict(
                    row["pending"],
                    f"{where}.pending",
                )
            ),
            pending_state=row["pending_state"],
        )
        observed = _digest(row["closure_digest"], f"{where}.closure_digest")
        if observed != candidate.digest:
            raise ValueError(f"{where}.closure_digest is stale")
        return candidate


@dataclass(frozen=True, slots=True)
class InterruptionAuthoritySourceClosure:
    """One canonical, non-projectable authority source observation."""

    SCHEMA: ClassVar[str] = INTERRUPTION_AUTHORITY_SOURCE_CLOSURE_SCHEMA

    selected_plan: SelectedPlanSourceClosure
    accepted_state: AcceptedStateSourceClosure
    durable_state: DurableStateSourceClosure

    def __post_init__(self) -> None:
        if not isinstance(self.selected_plan, SelectedPlanSourceClosure):
            raise ValueError("InterruptionAuthoritySourceClosure.selected_plan must be typed")
        if not isinstance(self.accepted_state, AcceptedStateSourceClosure):
            raise ValueError("InterruptionAuthoritySourceClosure.accepted_state must be typed")
        if not isinstance(self.durable_state, DurableStateSourceClosure):
            raise ValueError("InterruptionAuthoritySourceClosure.durable_state must be typed")

    def _payload(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "selected_plan": self.selected_plan.as_dict(),
            "accepted_state": self.accepted_state.as_dict(),
            "durable_state": self.durable_state.as_dict(),
        }

    @property
    def digest(self) -> str:
        return _canonical_digest(self._payload())

    def as_dict(self) -> dict[str, Any]:
        return {**self._payload(), "closure_digest": self.digest}

    @classmethod
    def from_dict(
        cls,
        value: object,
        where: str = "interruption authority source closure",
    ) -> InterruptionAuthoritySourceClosure:
        row = _exact_record(
            value,
            where,
            schema=cls.SCHEMA,
            fields=frozenset(
                {
                    "selected_plan",
                    "accepted_state",
                    "durable_state",
                    "closure_digest",
                }
            ),
        )
        candidate = cls(
            selected_plan=SelectedPlanSourceClosure.from_dict(row["selected_plan"], f"{where}.selected_plan"),
            accepted_state=AcceptedStateSourceClosure.from_dict(row["accepted_state"], f"{where}.accepted_state"),
            durable_state=DurableStateSourceClosure.from_dict(row["durable_state"], f"{where}.durable_state"),
        )
        observed = _digest(row["closure_digest"], f"{where}.closure_digest")
        if observed != candidate.digest:
            raise ValueError(f"{where}.closure_digest is stale")
        return candidate


__all__ = [
    "ACCEPTED_STATE_SOURCE_CLOSURE_SCHEMA",
    "AUTHORITY_SOURCE_IDENTITY_SCHEMA",
    "DURABLE_STATE_SOURCE_CLOSURE_SCHEMA",
    "INTERRUPTION_AUTHORITY_SOURCE_CLOSURE_SCHEMA",
    "SELECTED_PLAN_SOURCE_CLOSURE_SCHEMA",
    "AcceptedStateSourceClosure",
    "AuthoritySourceIdentity",
    "DurableStateSourceClosure",
    "InterruptionAuthoritySourceClosure",
    "SelectedPlanSourceClosure",
]
