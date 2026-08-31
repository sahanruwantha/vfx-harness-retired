"""Strict evidence references and authoritative state assertions for stop dispatch."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from pathlib import PurePosixPath
from typing import Any, ClassVar, TypeAlias

from vfx_harness.domain.stop_envelope_primitives import (
    canonical_digest,
    record,
    require_canonical_digest,
    require_digest,
    require_id,
    require_optional_digest,
    require_text,
)

EVIDENCE_KINDS = frozenset(
    {
        "authority_record",
        "checkpoint",
        "decision_record",
        "engineering_route",
        "environment_result",
        "finding_record",
        "journal",
        "run_report",
        "stop_evidence",
        "transaction_receipt",
        "unit_state",
    }
)
RECORD_KINDS = frozenset({"finding", "question", "defect"})
UNIT_STATUSES = frozenset(
    {
        "pending",
        "planning",
        "building",
        "frozen",
        "evaluating",
        "repairing",
        "passed",
        "failed",
        "hypothesis_falsified",
        "retryable",
        "blocked",
        "superseded",
    }
)
SELECTED_AUTHORITY_OUTCOMES = frozenset(
    {"clean", "clean_with_assumptions", "clean_with_deferred"}
)
SELECTED_AUTHORITY_VIEW_SOURCES = frozenset({"bundle", "jit"})


def _optional_text(value: Any, where: str) -> str | None:
    return None if value is None else require_text(value, where)


def _positive_int(value: Any, where: str, *, allow_zero: bool = False) -> int:
    minimum = 0 if allow_zero else 1
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        qualifier = "non-negative" if allow_zero else "positive"
        raise ValueError(f"{where} must be a {qualifier} integer")
    return value


def _safe_locator(value: Any, where: str) -> str:
    value = require_text(value, where)
    if "\\" in value:
        raise ValueError(f"{where} must use shot-relative POSIX separators")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"{where} must be a normalized shot-relative path")
    return value


def _closed_mapping(
    value: Any,
    where: str,
    expected: frozenset[str],
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{where} must be an object")
    found = set(value)
    if found != expected:
        missing = sorted(expected - found)
        unexpected = sorted(found - expected)
        raise ValueError(
            f"{where} fields mismatch; missing={missing}; unexpected={unexpected}"
        )
    return value


def _wire_encode(value: Any) -> Any:
    if isinstance(value, _StrictRecord):
        return value.as_dict()
    if isinstance(value, tuple):
        return [_wire_encode(item) for item in value]
    return value


def _identity_encode(value: Any) -> Any:
    if isinstance(value, _StrictRecord):
        return value.identity_dict()
    if isinstance(value, tuple):
        return [_identity_encode(item) for item in value]
    return value


class _StrictRecord:
    SCHEMA: ClassVar[str]
    DIGEST_FIELD: ClassVar[str]

    def _payload(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            **{item.name: _wire_encode(getattr(self, item.name)) for item in fields(self)},
        }

    def _identity_payload(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            **{item.name: _identity_encode(getattr(self, item.name)) for item in fields(self)},
        }

    @property
    def digest(self) -> str:
        return canonical_digest(self._identity_payload())

    def identity_dict(self) -> dict[str, Any]:
        return {**self._identity_payload(), self.DIGEST_FIELD: self.digest}

    def as_dict(self) -> dict[str, Any]:
        return {**self._payload(), self.DIGEST_FIELD: self.digest}


def _row(cls: type[_StrictRecord], value: Any, where: str) -> Any:
    names = tuple(item.name for item in fields(cls))
    return record(value, where, cls.SCHEMA, (*names, cls.DIGEST_FIELD))


def _finish(candidate: _StrictRecord, row: Any, where: str) -> Any:
    require_canonical_digest(
        row[candidate.DIGEST_FIELD],
        candidate.digest,
        where,
        candidate.DIGEST_FIELD,
    )
    return candidate


@dataclass(frozen=True, slots=True)
class StopEvidenceRef(_StrictRecord):
    SCHEMA: ClassVar[str] = "vfx-harness.stop-evidence-ref/v1"
    DIGEST_FIELD: ClassVar[str] = "evidence_ref_digest"

    kind: str
    locator: str
    sha256: str
    record_schema: str | None
    record_digest: str | None

    def __post_init__(self) -> None:
        if self.kind not in EVIDENCE_KINDS:
            raise ValueError(f"StopEvidenceRef.kind must be one of {sorted(EVIDENCE_KINDS)}")
        _safe_locator(self.locator, "StopEvidenceRef.locator")
        require_digest(self.sha256, "StopEvidenceRef.sha256")
        _optional_text(self.record_schema, "StopEvidenceRef.record_schema")
        require_optional_digest(self.record_digest, "StopEvidenceRef.record_digest")
        if (self.record_schema is None) is not (self.record_digest is None):
            raise ValueError("StopEvidenceRef record_schema and record_digest must appear together")
        if self.kind not in {"checkpoint", "journal"} and self.record_schema is None:
            raise ValueError(f"StopEvidenceRef kind {self.kind!r} requires a typed record identity")

    def _identity_payload(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "kind": self.kind,
            "sha256": self.sha256,
            "record_schema": self.record_schema,
            "record_digest": self.record_digest,
        }

    @classmethod
    def from_dict(cls, value: Any, where: str) -> StopEvidenceRef:
        row = _row(cls, value, where)
        candidate = cls(
            row["kind"],
            row["locator"],
            row["sha256"],
            row["record_schema"],
            row["record_digest"],
        )
        return _finish(candidate, row, where)


def _evidence_tuple(
    value: Any,
    where: str,
    *,
    allow_empty: bool = False,
) -> tuple[StopEvidenceRef, ...]:
    if not isinstance(value, tuple) or (not value and not allow_empty):
        raise ValueError(f"{where} must be a{' non-empty' if not allow_empty else ''} tuple")
    if any(not isinstance(item, StopEvidenceRef) for item in value):
        raise ValueError(f"{where} must contain StopEvidenceRef values")
    if len({item.digest for item in value}) != len(value):
        raise ValueError(f"{where} contains duplicate evidence references")
    return tuple(sorted(value, key=lambda item: item.digest))


@dataclass(frozen=True, slots=True)
class SelectedBundleAssertion(_StrictRecord):
    SCHEMA: ClassVar[str] = "vfx-harness.stop-state.selected-bundle/v1"
    DIGEST_FIELD: ClassVar[str] = "assertion_digest"
    bundle_digest: str | None
    selection_digest: str

    def __post_init__(self) -> None:
        require_optional_digest(self.bundle_digest, "SelectedBundleAssertion.bundle_digest")
        require_digest(self.selection_digest, "SelectedBundleAssertion.selection_digest")


@dataclass(frozen=True, slots=True)
class SelectedViewAssertion(_StrictRecord):
    SCHEMA: ClassVar[str] = "vfx-harness.stop-state.selected-view/v1"
    DIGEST_FIELD: ClassVar[str] = "assertion_digest"
    bundle_digest: str
    view_digest: str
    selection_digest: str

    def __post_init__(self) -> None:
        require_digest(self.bundle_digest, "SelectedViewAssertion.bundle_digest")
        require_digest(self.view_digest, "SelectedViewAssertion.view_digest")
        require_digest(self.selection_digest, "SelectedViewAssertion.selection_digest")


@dataclass(frozen=True, slots=True)
class SelectedAuthorityBundle:
    """Semantic identity of one verified immutable plan bundle."""

    digest: str
    outcome: str
    semantic_manifest_digest: str

    def __post_init__(self) -> None:
        require_digest(self.digest, "SelectedAuthorityBundle.digest")
        if self.outcome not in SELECTED_AUTHORITY_OUTCOMES:
            raise ValueError(
                "SelectedAuthorityBundle.outcome must be one of "
                f"{sorted(SELECTED_AUTHORITY_OUTCOMES)}"
            )
        require_digest(
            self.semantic_manifest_digest,
            "SelectedAuthorityBundle.semantic_manifest_digest",
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "digest": self.digest,
            "outcome": self.outcome,
            "semantic_manifest_digest": self.semantic_manifest_digest,
        }

    @classmethod
    def from_dict(cls, value: Any, where: str) -> SelectedAuthorityBundle:
        row = _closed_mapping(
            value,
            where,
            frozenset({"digest", "outcome", "semantic_manifest_digest"}),
        )
        return cls(
            digest=row["digest"],
            outcome=row["outcome"],
            semantic_manifest_digest=row["semantic_manifest_digest"],
        )


@dataclass(frozen=True, slots=True)
class SelectedAuthorityView:
    """Semantic identity of the effective consumer view for a selected bundle."""

    source: str
    digest: str
    semantic_manifest_digest: str

    def __post_init__(self) -> None:
        if self.source not in SELECTED_AUTHORITY_VIEW_SOURCES:
            raise ValueError(
                "SelectedAuthorityView.source must be one of "
                f"{sorted(SELECTED_AUTHORITY_VIEW_SOURCES)}"
            )
        require_digest(self.digest, "SelectedAuthorityView.digest")
        require_digest(
            self.semantic_manifest_digest,
            "SelectedAuthorityView.semantic_manifest_digest",
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "source": self.source,
            "digest": self.digest,
            "semantic_manifest_digest": self.semantic_manifest_digest,
        }

    @classmethod
    def from_dict(cls, value: Any, where: str) -> SelectedAuthorityView:
        row = _closed_mapping(
            value,
            where,
            frozenset({"source", "digest", "semantic_manifest_digest"}),
        )
        return cls(
            source=row["source"],
            digest=row["digest"],
            semantic_manifest_digest=row["semantic_manifest_digest"],
        )


@dataclass(frozen=True, slots=True)
class SelectedAuthorityAssertionV2(_StrictRecord):
    """One canonical semantic snapshot of selected plan and effective-view authority."""

    SCHEMA: ClassVar[str] = "vfx-harness.selected-authority-state/v2"
    DIGEST_FIELD: ClassVar[str] = "assertion_digest"

    selection: str
    bundle: SelectedAuthorityBundle | None
    effective_view: SelectedAuthorityView | None

    def __post_init__(self) -> None:
        if self.selection not in {"absent", "selected"}:
            raise ValueError(
                "SelectedAuthorityAssertionV2.selection must be 'absent' or 'selected'"
            )
        if self.selection == "absent":
            if self.bundle is not None or self.effective_view is not None:
                raise ValueError(
                    "absent selected authority requires null bundle and effective_view"
                )
            return
        if not isinstance(self.bundle, SelectedAuthorityBundle) or not isinstance(
            self.effective_view, SelectedAuthorityView
        ):
            raise ValueError(
                "selected authority requires typed bundle and effective_view records"
            )
        if (
            self.effective_view.source == "bundle"
            and self.effective_view.digest != self.bundle.digest
        ):
            raise ValueError(
                "bundle-backed effective view digest must equal the selected bundle digest"
            )

    def _payload(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "selection": self.selection,
            "bundle": None if self.bundle is None else self.bundle.as_dict(),
            "effective_view": (
                None if self.effective_view is None else self.effective_view.as_dict()
            ),
        }

    def _identity_payload(self) -> dict[str, Any]:
        return self._payload()

    @classmethod
    def from_dict(cls, value: Any, where: str) -> SelectedAuthorityAssertionV2:
        row = _row(cls, value, where)
        candidate = cls(
            selection=row["selection"],
            bundle=(
                None
                if row["bundle"] is None
                else SelectedAuthorityBundle.from_dict(row["bundle"], f"{where}.bundle")
            ),
            effective_view=(
                None
                if row["effective_view"] is None
                else SelectedAuthorityView.from_dict(
                    row["effective_view"],
                    f"{where}.effective_view",
                )
            ),
        )
        return _finish(candidate, row, where)


@dataclass(frozen=True, slots=True)
class UnitStateAssertion(_StrictRecord):
    SCHEMA: ClassVar[str] = "vfx-harness.stop-state.unit/v1"
    DIGEST_FIELD: ClassVar[str] = "assertion_digest"
    layer_id: str
    unit_id: str
    unit_digest: str
    unit_plan_digest: str
    plan_digest: str
    revision: int
    status: str
    state_digest: str
    candidate_digest: str | None
    checkpoint_digest: str | None

    def __post_init__(self) -> None:
        require_id(self.layer_id, "UnitStateAssertion.layer_id")
        require_id(self.unit_id, "UnitStateAssertion.unit_id")
        for name in ("unit_digest", "unit_plan_digest", "plan_digest", "state_digest"):
            require_digest(getattr(self, name), f"UnitStateAssertion.{name}")
        _positive_int(self.revision, "UnitStateAssertion.revision")
        if self.status not in UNIT_STATUSES:
            raise ValueError(f"UnitStateAssertion.status must be one of {sorted(UNIT_STATUSES)}")
        require_optional_digest(self.candidate_digest, "UnitStateAssertion.candidate_digest")
        require_optional_digest(self.checkpoint_digest, "UnitStateAssertion.checkpoint_digest")


@dataclass(frozen=True, slots=True)
class EvidenceRecordAssertion(_StrictRecord):
    SCHEMA: ClassVar[str] = "vfx-harness.stop-state.evidence-record/v1"
    DIGEST_FIELD: ClassVar[str] = "assertion_digest"
    record_kind: str
    record_id: str
    evidence: StopEvidenceRef

    def __post_init__(self) -> None:
        if self.record_kind not in RECORD_KINDS:
            raise ValueError(f"EvidenceRecordAssertion.record_kind must be one of {sorted(RECORD_KINDS)}")
        require_id(self.record_id, "EvidenceRecordAssertion.record_id")
        if not isinstance(self.evidence, StopEvidenceRef):
            raise ValueError("EvidenceRecordAssertion.evidence must be a StopEvidenceRef")


@dataclass(frozen=True, slots=True)
class EnvironmentResultAssertion(_StrictRecord):
    SCHEMA: ClassVar[str] = "vfx-harness.stop-state.environment-result/v1"
    DIGEST_FIELD: ClassVar[str] = "assertion_digest"
    probe_id: str
    probe_spec_digest: str
    result_digest: str

    def __post_init__(self) -> None:
        require_id(self.probe_id, "EnvironmentResultAssertion.probe_id")
        require_digest(self.probe_spec_digest, "EnvironmentResultAssertion.probe_spec_digest")
        require_digest(self.result_digest, "EnvironmentResultAssertion.result_digest")


@dataclass(frozen=True, slots=True)
class ResumeRecordAssertion(_StrictRecord):
    SCHEMA: ClassVar[str] = "vfx-harness.stop-state.resume-record/v1"
    DIGEST_FIELD: ClassVar[str] = "assertion_digest"
    layer_id: str
    unit_id: str
    unit_digest: str
    session_id: str
    checkpoint: StopEvidenceRef
    journal: StopEvidenceRef
    journal_index: int
    ledger_revision: int
    ledger_digest: str

    def __post_init__(self) -> None:
        require_id(self.layer_id, "ResumeRecordAssertion.layer_id")
        require_id(self.unit_id, "ResumeRecordAssertion.unit_id")
        require_digest(self.unit_digest, "ResumeRecordAssertion.unit_digest")
        require_id(self.session_id, "ResumeRecordAssertion.session_id")
        if not isinstance(self.checkpoint, StopEvidenceRef) or self.checkpoint.kind != "checkpoint":
            raise ValueError("ResumeRecordAssertion.checkpoint must be checkpoint evidence")
        if not isinstance(self.journal, StopEvidenceRef) or self.journal.kind != "journal":
            raise ValueError("ResumeRecordAssertion.journal must be journal evidence")
        _positive_int(self.journal_index, "ResumeRecordAssertion.journal_index", allow_zero=True)
        _positive_int(self.ledger_revision, "ResumeRecordAssertion.ledger_revision")
        require_digest(self.ledger_digest, "ResumeRecordAssertion.ledger_digest")


@dataclass(frozen=True, slots=True)
class BudgetStateAssertion(_StrictRecord):
    SCHEMA: ClassVar[str] = "vfx-harness.stop-state.budget/v1"
    DIGEST_FIELD: ClassVar[str] = "assertion_digest"
    budget_key: str
    policy_digest: str
    consumed_attempts: int
    remaining_attempts: int
    state_digest: str

    def __post_init__(self) -> None:
        require_id(self.budget_key, "BudgetStateAssertion.budget_key")
        require_digest(self.policy_digest, "BudgetStateAssertion.policy_digest")
        _positive_int(
            self.consumed_attempts,
            "BudgetStateAssertion.consumed_attempts",
            allow_zero=True,
        )
        _positive_int(
            self.remaining_attempts,
            "BudgetStateAssertion.remaining_attempts",
            allow_zero=True,
        )
        require_digest(self.state_digest, "BudgetStateAssertion.state_digest")


StopStateAssertion: TypeAlias = (
    SelectedBundleAssertion
    | SelectedViewAssertion
    | SelectedAuthorityAssertionV2
    | UnitStateAssertion
    | EvidenceRecordAssertion
    | EnvironmentResultAssertion
    | ResumeRecordAssertion
    | BudgetStateAssertion
)
_ASSERTION_TYPES = {
    cls.SCHEMA: cls
    for cls in (
        SelectedBundleAssertion,
        SelectedViewAssertion,
        SelectedAuthorityAssertionV2,
        UnitStateAssertion,
        EvidenceRecordAssertion,
        EnvironmentResultAssertion,
        ResumeRecordAssertion,
        BudgetStateAssertion,
    )
}


def state_assertion_from_dict(value: Any, where: str) -> StopStateAssertion:
    if not isinstance(value, dict) or value.get("schema") not in _ASSERTION_TYPES:
        raise ValueError(f"{where}.schema must name a supported stop-state assertion")
    cls = _ASSERTION_TYPES[value["schema"]]
    row = _row(cls, value, where)
    kwargs = {item.name: row[item.name] for item in fields(cls)}
    if cls is SelectedAuthorityAssertionV2:
        kwargs["bundle"] = (
            None
            if row["bundle"] is None
            else SelectedAuthorityBundle.from_dict(row["bundle"], f"{where}.bundle")
        )
        kwargs["effective_view"] = (
            None
            if row["effective_view"] is None
            else SelectedAuthorityView.from_dict(
                row["effective_view"],
                f"{where}.effective_view",
            )
        )
    elif cls is EvidenceRecordAssertion:
        kwargs["evidence"] = StopEvidenceRef.from_dict(row["evidence"], f"{where}.evidence")
    elif cls is ResumeRecordAssertion:
        kwargs["checkpoint"] = StopEvidenceRef.from_dict(
            row["checkpoint"],
            f"{where}.checkpoint",
        )
        kwargs["journal"] = StopEvidenceRef.from_dict(row["journal"], f"{where}.journal")
    return _finish(cls(**kwargs), row, where)


def _assertions(
    value: tuple[StopStateAssertion, ...],
    where: str,
) -> tuple[StopStateAssertion, ...]:
    if not isinstance(value, tuple) or not value:
        raise ValueError(f"{where} must be a non-empty tuple")
    if any(not isinstance(item, tuple(_ASSERTION_TYPES.values())) for item in value):
        raise ValueError(f"{where} contains an unsupported state assertion")
    if len({item.digest for item in value}) != len(value):
        raise ValueError(f"{where} contains duplicate state assertions")
    return tuple(sorted(value, key=lambda item: item.digest))
