"""Strict selected-authority amendment proposal contracts.

This leaf owns the amendment-specific target, postcondition, and closed gate vocabulary.
The public transaction facade re-exports both records and retains wire parsing policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from vfx_harness.domain.stop_envelope_primitives import (
    require_digest,
    require_id,
    require_text,
    require_text_tuple,
)
from vfx_harness.domain.stop_transaction_state import (
    EvidenceRecordAssertion,
    SelectedAuthorityAssertionV2,
    _StrictRecord,
)

AUTHORITY_SCOPES = frozenset({"global_plan", "layer_view"})
_AMENDMENT_GATE_SCHEMA = "vfx-harness.plan-gate/v1"
_AMENDMENT_VALIDATION_SCOPE = "structural_authority"


@dataclass(frozen=True, slots=True)
class PublishValidatedAmendmentTarget(_StrictRecord):
    SCHEMA: ClassVar[str] = "vfx-harness.stop-target.publish-validated-amendment/v2"
    DIGEST_FIELD: ClassVar[str] = "target_digest"
    scope: str
    base_authority: SelectedAuthorityAssertionV2
    layer_id: str | None
    findings: tuple[EvidenceRecordAssertion, ...]
    owner_authority_id: str
    gate_policy_id: str
    gate_schema: str
    validation_scope: str

    def __post_init__(self) -> None:
        if self.scope not in AUTHORITY_SCOPES:
            raise ValueError(
                "PublishValidatedAmendmentTarget.scope must be one of "
                f"{sorted(AUTHORITY_SCOPES)}"
            )
        if not isinstance(self.base_authority, SelectedAuthorityAssertionV2):
            raise ValueError(
                "PublishValidatedAmendmentTarget requires shared selected-authority state"
            )
        if self.scope == "layer_view":
            require_id(self.layer_id, "PublishValidatedAmendmentTarget.layer_id")
            if (
                self.base_authority.selection != "selected"
                or self.base_authority.effective_view is None
                or self.base_authority.effective_view.source not in {"bundle", "jit"}
            ):
                raise ValueError(
                    "layer-view amendment requires selected bundle or JIT effective authority"
                )
        elif self.layer_id is not None:
            raise ValueError("global-plan amendment cannot name a layer target")
        if not isinstance(self.findings, tuple) or not self.findings:
            raise ValueError("PublishValidatedAmendmentTarget.findings must be non-empty")
        if any(
            not isinstance(item, EvidenceRecordAssertion)
            or item.record_kind != "finding"
            for item in self.findings
        ):
            raise ValueError(
                "PublishValidatedAmendmentTarget.findings must be finding assertions"
            )
        if len({item.digest for item in self.findings}) != len(self.findings):
            raise ValueError("PublishValidatedAmendmentTarget.findings contains duplicates")
        object.__setattr__(
            self,
            "findings",
            tuple(sorted(self.findings, key=lambda item: item.digest)),
        )
        require_id(
            self.owner_authority_id,
            "PublishValidatedAmendmentTarget.owner_authority_id",
        )
        require_text(
            self.gate_policy_id,
            "PublishValidatedAmendmentTarget.gate_policy_id",
        )
        if self.gate_schema != _AMENDMENT_GATE_SCHEMA:
            raise ValueError(
                "PublishValidatedAmendmentTarget.gate_schema must be "
                f"{_AMENDMENT_GATE_SCHEMA!r}"
            )
        if self.validation_scope != _AMENDMENT_VALIDATION_SCOPE:
            raise ValueError(
                "PublishValidatedAmendmentTarget.validation_scope must be "
                f"{_AMENDMENT_VALIDATION_SCOPE!r}"
            )


@dataclass(frozen=True, slots=True)
class SelectedAuthorityAmendmentCommitted(_StrictRecord):
    SCHEMA: ClassVar[str] = "vfx-harness.stop-postcondition.selected-authority-amendment/v2"
    DIGEST_FIELD: ClassVar[str] = "postcondition_digest"
    scope: str
    base_authority_digest: str
    layer_id: str | None
    finding_ids: tuple[str, ...]
    owner_authority_id: str
    gate_policy_id: str
    gate_schema: str
    validation_scope: str
    required_after_source: str

    def __post_init__(self) -> None:
        if self.scope not in AUTHORITY_SCOPES:
            raise ValueError(
                "authority amendment scope must be one of "
                f"{sorted(AUTHORITY_SCOPES)}"
            )
        require_digest(
            self.base_authority_digest,
            "SelectedAuthorityAmendmentCommitted.base_authority_digest",
        )
        if self.scope == "layer_view":
            require_id(
                self.layer_id,
                "SelectedAuthorityAmendmentCommitted.layer_id",
            )
        elif self.layer_id is not None:
            raise ValueError("global authority postcondition cannot name a layer")
        object.__setattr__(
            self,
            "finding_ids",
            require_text_tuple(
                self.finding_ids,
                "SelectedAuthorityAmendmentCommitted.finding_ids",
            ),
        )
        require_id(
            self.owner_authority_id,
            "SelectedAuthorityAmendmentCommitted.owner_authority_id",
        )
        require_text(
            self.gate_policy_id,
            "SelectedAuthorityAmendmentCommitted.gate_policy_id",
        )
        if self.gate_schema != _AMENDMENT_GATE_SCHEMA:
            raise ValueError(
                "SelectedAuthorityAmendmentCommitted.gate_schema must be "
                f"{_AMENDMENT_GATE_SCHEMA!r}"
            )
        if self.validation_scope != _AMENDMENT_VALIDATION_SCOPE:
            raise ValueError(
                "SelectedAuthorityAmendmentCommitted.validation_scope must be "
                f"{_AMENDMENT_VALIDATION_SCOPE!r}"
            )
        expected_after_source = "bundle" if self.scope == "global_plan" else "jit"
        if self.required_after_source != expected_after_source:
            raise ValueError(
                "SelectedAuthorityAmendmentCommitted.required_after_source must be "
                f"{expected_after_source!r} for scope {self.scope!r}"
            )
