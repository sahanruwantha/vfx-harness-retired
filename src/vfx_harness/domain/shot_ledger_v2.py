"""Strict accepted-build authority for ``vfx-harness.shot-ledger/v2``.

The v2 ledger is an index, not a diagnostic work log.  Its selected authority,
accepted layer rows, and optional acceptance evidence are closed values whose
canonical digest can be verified without filesystem access.  An owning writer is
responsible for deriving the complete row set while holding the shot-authority
capture fence; callers never supply a second member list.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, ClassVar

from vfx_harness.domain.acceptance_outcomes import AcceptanceOutcome
from vfx_harness.domain.authority_head_records import (
    AuthoritySelectionTokenProjection,
    parse_authority_selection_token,
)
from vfx_harness.domain.authority_state_records import (
    AUTHORITY_STATE_HEAD_SCHEMA,
    AuthorityStateRecordRef,
    authority_selection_token_dict,
)
from vfx_harness.domain.script_locators import (
    is_composed_layer_script_locator,
)
from vfx_harness.domain.stop_envelope_primitives import (
    canonical_digest,
    list_value,
    record,
    require_canonical_digest,
    require_digest,
    require_id,
)

SHOT_LEDGER_SCHEMA = "vfx-harness.shot-ledger/v2"
ACCEPTED_LAYER_SCHEMA = "vfx-harness.shot-ledger-accepted-layer/v1"
ACCEPTANCE_BINDING_SCHEMA = "vfx-harness.shot-ledger-acceptance/v1"
MOMENT_EVIDENCE_BINDING_SCHEMA = "vfx-harness.shot-ledger-moment-evidence-binding/v1"


def _relative_locator(value: object, where: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{where} must be a non-empty trimmed locator")
    if "\\" in value or "\x00" in value or "#" in value or "?" in value:
        raise ValueError(f"{where} must be a normalized safe relative locator")
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"{where} must be a normalized safe relative locator")
    return value


def _composed_script_locator(value: object, where: str) -> str:
    locator = _relative_locator(value, where)
    if not is_composed_layer_script_locator(locator):
        raise ValueError(
            f"{where} must name a composed Python artifact below build/, not a unit script"
        )
    return locator


def canonical_layer_outcome_locator(layer_id: str) -> str:
    """Return the sole sealed-outcome locator for an exact layer identity."""

    identity = require_id(layer_id, "canonical layer outcome.layer_id")
    segment = "layer-" + identity.encode("utf-8").hex()
    return f"plans/outcomes/{segment}.json"


@dataclass(frozen=True, slots=True)
class AcceptedLayerAuthority:
    """One current accepted layer and every terminal artifact it selects."""

    SCHEMA: ClassVar[str] = ACCEPTED_LAYER_SCHEMA

    layer_id: str
    layer_generation_digest: str
    finalization_receipt_digest: str
    composed_script_locator: str
    composed_script_sha256: str
    sealed_outcome_locator: str
    sealed_outcome_sha256: str

    def __post_init__(self) -> None:
        require_id(self.layer_id, "AcceptedLayerAuthority.layer_id")
        require_digest(
            self.layer_generation_digest,
            "AcceptedLayerAuthority.layer_generation_digest",
        )
        require_digest(
            self.finalization_receipt_digest,
            "AcceptedLayerAuthority.finalization_receipt_digest",
        )
        _composed_script_locator(
            self.composed_script_locator,
            "AcceptedLayerAuthority.composed_script_locator",
        )
        require_digest(
            self.composed_script_sha256,
            "AcceptedLayerAuthority.composed_script_sha256",
        )
        _relative_locator(
            self.sealed_outcome_locator,
            "AcceptedLayerAuthority.sealed_outcome_locator",
        )
        expected_outcome = canonical_layer_outcome_locator(self.layer_id)
        if self.sealed_outcome_locator != expected_outcome:
            raise ValueError(
                "AcceptedLayerAuthority.sealed_outcome_locator must equal the "
                f"canonical layer locator {expected_outcome!r}"
            )
        require_digest(
            self.sealed_outcome_sha256,
            "AcceptedLayerAuthority.sealed_outcome_sha256",
        )

    def _payload(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "layer_id": self.layer_id,
            "layer_generation_digest": self.layer_generation_digest,
            "finalization_receipt_digest": self.finalization_receipt_digest,
            "composed_script_locator": self.composed_script_locator,
            "composed_script_sha256": self.composed_script_sha256,
            "sealed_outcome_locator": self.sealed_outcome_locator,
            "sealed_outcome_sha256": self.sealed_outcome_sha256,
        }

    @property
    def digest(self) -> str:
        return canonical_digest(self._payload())

    def as_dict(self) -> dict[str, Any]:
        return {**self._payload(), "accepted_layer_digest": self.digest}

    @classmethod
    def from_dict(cls, value: object, where: str) -> AcceptedLayerAuthority:
        row = record(
            value,
            where,
            cls.SCHEMA,
            (
                "layer_id",
                "layer_generation_digest",
                "finalization_receipt_digest",
                "composed_script_locator",
                "composed_script_sha256",
                "sealed_outcome_locator",
                "sealed_outcome_sha256",
                "accepted_layer_digest",
            ),
        )
        candidate = cls(
            layer_id=row["layer_id"],
            layer_generation_digest=row["layer_generation_digest"],
            finalization_receipt_digest=row["finalization_receipt_digest"],
            composed_script_locator=row["composed_script_locator"],
            composed_script_sha256=row["composed_script_sha256"],
            sealed_outcome_locator=row["sealed_outcome_locator"],
            sealed_outcome_sha256=row["sealed_outcome_sha256"],
        )
        require_canonical_digest(
            row["accepted_layer_digest"],
            candidate.digest,
            where,
            "accepted_layer_digest",
        )
        return candidate


@dataclass(frozen=True, slots=True)
class AcceptanceMomentEvidenceBinding:
    """Complete immutable evidence edges for one typed acceptance moment."""

    SCHEMA: ClassVar[str] = MOMENT_EVIDENCE_BINDING_SCHEMA

    moment_id: str
    evidence_digest: str
    evidence_record_locator: str
    evidence_record_sha256: str
    render_locator: str
    render_sha256: str
    reference_locator: str
    reference_sha256: str

    def __post_init__(self) -> None:
        require_id(self.moment_id, "AcceptanceMomentEvidenceBinding.moment_id")
        for field in (
            "evidence_digest",
            "evidence_record_sha256",
            "render_sha256",
            "reference_sha256",
        ):
            require_digest(
                getattr(self, field),
                f"AcceptanceMomentEvidenceBinding.{field}",
            )
        evidence_record = _relative_locator(
            self.evidence_record_locator,
            "AcceptanceMomentEvidenceBinding.evidence_record_locator",
        )
        if PurePosixPath(evidence_record).suffix != ".json":
            raise ValueError("AcceptanceMomentEvidenceBinding.evidence_record_locator must name a JSON record")
        render = _relative_locator(
            self.render_locator,
            "AcceptanceMomentEvidenceBinding.render_locator",
        )
        reference = _relative_locator(
            self.reference_locator,
            "AcceptanceMomentEvidenceBinding.reference_locator",
        )
        if len({evidence_record, render, reference}) != 3:
            raise ValueError("AcceptanceMomentEvidenceBinding locators must name three distinct sources")

    def _payload(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "moment_id": self.moment_id,
            "evidence_digest": self.evidence_digest,
            "evidence_record_locator": self.evidence_record_locator,
            "evidence_record_sha256": self.evidence_record_sha256,
            "render_locator": self.render_locator,
            "render_sha256": self.render_sha256,
            "reference_locator": self.reference_locator,
            "reference_sha256": self.reference_sha256,
        }

    @property
    def digest(self) -> str:
        return canonical_digest(self._payload())

    def as_dict(self) -> dict[str, Any]:
        return {**self._payload(), "binding_digest": self.digest}

    @classmethod
    def from_dict(
        cls,
        value: object,
        where: str,
    ) -> AcceptanceMomentEvidenceBinding:
        row = record(
            value,
            where,
            cls.SCHEMA,
            (
                "moment_id",
                "evidence_digest",
                "evidence_record_locator",
                "evidence_record_sha256",
                "render_locator",
                "render_sha256",
                "reference_locator",
                "reference_sha256",
                "binding_digest",
            ),
        )
        candidate = cls(
            moment_id=row["moment_id"],
            evidence_digest=row["evidence_digest"],
            evidence_record_locator=row["evidence_record_locator"],
            evidence_record_sha256=row["evidence_record_sha256"],
            render_locator=row["render_locator"],
            render_sha256=row["render_sha256"],
            reference_locator=row["reference_locator"],
            reference_sha256=row["reference_sha256"],
        )
        require_canonical_digest(
            row["binding_digest"],
            candidate.digest,
            where,
            "binding_digest",
        )
        return candidate


@dataclass(frozen=True, slots=True)
class ShotLedgerAcceptance:
    """A passing typed outcome with an exact evidence binding for every moment."""

    SCHEMA: ClassVar[str] = ACCEPTANCE_BINDING_SCHEMA

    outcome: AcceptanceOutcome
    moment_evidence: tuple[AcceptanceMomentEvidenceBinding, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, AcceptanceOutcome):
            raise ValueError("ShotLedgerAcceptance.outcome must be an AcceptanceOutcome")
        if not self.outcome.passed:
            raise ValueError("ShotLedgerAcceptance.outcome must be passing")
        if any(moment.decided_by == "no_optical_signal" for moment in self.outcome.moments):
            raise ValueError("ShotLedgerAcceptance cannot treat no-optical-signal evidence as passing acceptance")
        if not isinstance(self.moment_evidence, tuple) or any(
            not isinstance(binding, AcceptanceMomentEvidenceBinding) for binding in self.moment_evidence
        ):
            raise ValueError("ShotLedgerAcceptance.moment_evidence must be a tuple of typed bindings")
        outcome_ids = tuple(moment.moment_id for moment in self.outcome.moments)
        binding_ids = tuple(binding.moment_id for binding in self.moment_evidence)
        if binding_ids != outcome_ids:
            raise ValueError("ShotLedgerAcceptance.moment_evidence must cover the outcome's exact ordered moment set")
        outcome_digests = tuple(moment.evidence_digest for moment in self.outcome.moments)
        binding_digests = tuple(binding.evidence_digest for binding in self.moment_evidence)
        if binding_digests != outcome_digests:
            raise ValueError("ShotLedgerAcceptance.moment_evidence digests must match the typed outcome")

    def _payload(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "outcome": self.outcome.as_dict(),
            "moment_evidence": [binding.as_dict() for binding in self.moment_evidence],
        }

    @property
    def digest(self) -> str:
        return canonical_digest(self._payload())

    def as_dict(self) -> dict[str, Any]:
        return {**self._payload(), "acceptance_binding_digest": self.digest}

    @classmethod
    def from_dict(cls, value: object, where: str) -> ShotLedgerAcceptance:
        row = record(
            value,
            where,
            cls.SCHEMA,
            ("outcome", "moment_evidence", "acceptance_binding_digest"),
        )
        outcome = AcceptanceOutcome.from_dict(row["outcome"], f"{where}.outcome")
        bindings = tuple(
            AcceptanceMomentEvidenceBinding.from_dict(
                item,
                f"{where}.moment_evidence[{index}]",
            )
            for index, item in enumerate(list_value(row["moment_evidence"], f"{where}.moment_evidence"))
        )
        candidate = cls(outcome=outcome, moment_evidence=bindings)
        require_canonical_digest(
            row["acceptance_binding_digest"],
            candidate.digest,
            where,
            "acceptance_binding_digest",
        )
        return candidate


@dataclass(frozen=True, slots=True)
class ShotLedgerV2:
    """Canonical accepted-build index for one exact selected shot authority.

    ``accepted_layers`` preserves the selected DAG's stable topological replay order;
    this pure contract proves only that the ordered tuple has unique identities.  The
    future source-verifying writer/evaluator must prove that order against the selected
    DAG.  ``accepted_chain_digest`` is the opaque digest of the existing full
    ``acceptance-chain/v2`` authority, not a projection derived from these reduced rows.
    """

    SCHEMA: ClassVar[str] = SHOT_LEDGER_SCHEMA

    selection_token: AuthoritySelectionTokenProjection
    authority_state_head_ref: AuthorityStateRecordRef
    accepted_layers: tuple[AcceptedLayerAuthority, ...]
    accepted_chain_digest: str
    acceptance: ShotLedgerAcceptance | None

    def __post_init__(self) -> None:
        if not isinstance(self.selection_token, AuthoritySelectionTokenProjection):
            raise ValueError("ShotLedgerV2.selection_token must be an AuthoritySelectionTokenProjection")
        parse_authority_selection_token(
            authority_selection_token_dict(self.selection_token),
            "ShotLedgerV2.selection_token",
        )
        if not isinstance(self.authority_state_head_ref, AuthorityStateRecordRef):
            raise ValueError("ShotLedgerV2.authority_state_head_ref must be an AuthorityStateRecordRef")
        if self.authority_state_head_ref.record_schema != AUTHORITY_STATE_HEAD_SCHEMA:
            raise ValueError(
                f"ShotLedgerV2.authority_state_head_ref must name an exact {AUTHORITY_STATE_HEAD_SCHEMA} record"
            )
        if not isinstance(self.accepted_layers, tuple) or any(
            not isinstance(layer, AcceptedLayerAuthority) for layer in self.accepted_layers
        ):
            raise ValueError("ShotLedgerV2.accepted_layers must be a tuple of AcceptedLayerAuthority values")
        layer_ids = tuple(layer.layer_id for layer in self.accepted_layers)
        if len(layer_ids) != len(set(layer_ids)):
            raise ValueError("ShotLedgerV2.accepted_layers contains duplicate layer ids")
        require_digest(
            self.accepted_chain_digest,
            "ShotLedgerV2.accepted_chain_digest",
        )
        finalization_digests = tuple(layer.finalization_receipt_digest for layer in self.accepted_layers)
        if len(finalization_digests) != len(set(finalization_digests)):
            raise ValueError("ShotLedgerV2.accepted_layers contains duplicate finalization receipt digests")
        script_locators = tuple(layer.composed_script_locator for layer in self.accepted_layers)
        if len(script_locators) != len(set(script_locators)):
            raise ValueError("ShotLedgerV2.accepted_layers contains duplicate composed script locators")
        if self.accepted_layers and self.selection_token.plan_revision == 0:
            raise ValueError("ShotLedgerV2 cannot select accepted layers without a selected plan")
        if self.acceptance is not None and not isinstance(
            self.acceptance,
            ShotLedgerAcceptance,
        ):
            raise ValueError("ShotLedgerV2.acceptance must be a ShotLedgerAcceptance or null")
        if self.acceptance is not None:
            if not self.accepted_layers:
                raise ValueError("ShotLedgerV2 cannot select acceptance without accepted layers")
            if self.acceptance.outcome.chain_digest != self.accepted_chain_digest:
                raise ValueError("ShotLedgerV2 acceptance chain digest does not match its exact accepted_chain_digest")

    def _payload(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "selection_token": authority_selection_token_dict(self.selection_token),
            "authority_state_head_ref": self.authority_state_head_ref.as_dict(),
            "accepted_layers": [layer.as_dict() for layer in self.accepted_layers],
            "accepted_chain_digest": self.accepted_chain_digest,
            "acceptance": None if self.acceptance is None else self.acceptance.as_dict(),
        }

    @property
    def index_digest(self) -> str:
        return canonical_digest(self._payload())

    def as_dict(self) -> dict[str, Any]:
        return {**self._payload(), "index_digest": self.index_digest}

    @classmethod
    def from_dict(cls, value: object, where: str = "shot ledger") -> ShotLedgerV2:
        row = record(
            value,
            where,
            cls.SCHEMA,
            (
                "selection_token",
                "authority_state_head_ref",
                "accepted_layers",
                "accepted_chain_digest",
                "acceptance",
                "index_digest",
            ),
        )
        selection_token = parse_authority_selection_token(
            row["selection_token"],
            f"{where}.selection_token",
        )
        authority_state_head_ref = AuthorityStateRecordRef.parse(
            row["authority_state_head_ref"],
            f"{where}.authority_state_head_ref",
        )
        accepted_layers = tuple(
            AcceptedLayerAuthority.from_dict(
                item,
                f"{where}.accepted_layers[{index}]",
            )
            for index, item in enumerate(list_value(row["accepted_layers"], f"{where}.accepted_layers"))
        )
        acceptance = (
            None
            if row["acceptance"] is None
            else ShotLedgerAcceptance.from_dict(
                row["acceptance"],
                f"{where}.acceptance",
            )
        )
        candidate = cls(
            selection_token=selection_token,
            authority_state_head_ref=authority_state_head_ref,
            accepted_layers=accepted_layers,
            accepted_chain_digest=row["accepted_chain_digest"],
            acceptance=acceptance,
        )
        require_canonical_digest(
            row["index_digest"],
            candidate.index_digest,
            where,
            "index_digest",
        )
        return candidate


__all__ = [
    "ACCEPTANCE_BINDING_SCHEMA",
    "ACCEPTED_LAYER_SCHEMA",
    "MOMENT_EVIDENCE_BINDING_SCHEMA",
    "SHOT_LEDGER_SCHEMA",
    "AcceptanceMomentEvidenceBinding",
    "AcceptedLayerAuthority",
    "ShotLedgerAcceptance",
    "ShotLedgerV2",
    "canonical_layer_outcome_locator",
]
