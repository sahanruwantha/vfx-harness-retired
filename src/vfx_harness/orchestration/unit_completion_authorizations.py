"""Typed, state-bound authorization views for accepted work-unit receipts.

The durable ``passed`` lifecycle bit records what happened to a unit.  It does not
say that the receipt is current authority.  These ephemeral values are minted only
after the owning coordinator or candidate-preview verifier has proved that separate
fact, and bind the resulting receipt set to the exact completion-relevant state
projection consumed by readiness and predecessor-interface readers.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, TypeAlias

from vfx_harness.domain.authority_head_records import (
    AuthoritySelectionTokenProjection,
)
from vfx_harness.domain.authority_preview_records import AuthorityPreviewReference
from vfx_harness.domain.authority_state_records import AuthorityStateRecordRef
from vfx_harness.domain.stop_envelope_primitives import canonical_digest, require_digest
from vfx_harness.domain.unit_completion_receipts import UnitCompletionReceipt


class UnitCompletionAuthorizationError(ValueError):
    """A typed completion authorization does not match the state being consumed."""


def _layer_id(value: object, where: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise UnitCompletionAuthorizationError(
            f"{where} must be a non-empty trimmed string"
        )
    return value


def _receipt_rows(
    value: tuple[tuple[str, str], ...],
    where: str,
) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, tuple):
        raise UnitCompletionAuthorizationError(f"{where} must be a tuple")
    rows: list[tuple[str, str]] = []
    for index, item in enumerate(value):
        if not isinstance(item, tuple) or len(item) != 2:
            raise UnitCompletionAuthorizationError(
                f"{where}[{index}] must be a (unit_id, receipt_digest) pair"
            )
        unit_id = _layer_id(item[0], f"{where}[{index}].unit_id")
        try:
            receipt_digest = require_digest(
                item[1],
                f"{where}[{index}].receipt_digest",
            )
        except ValueError as exc:
            raise UnitCompletionAuthorizationError(str(exc)) from exc
        rows.append((unit_id, receipt_digest))
    if len(rows) != len({unit_id for unit_id, _digest in rows}):
        raise UnitCompletionAuthorizationError(f"{where} contains duplicate unit ids")
    normalized = tuple(sorted(rows))
    if value != normalized:
        raise UnitCompletionAuthorizationError(f"{where} must be sorted by unit id")
    return normalized


def _passed_completion_rows(
    state: Mapping[str, Any],
) -> tuple[dict[str, str], ...]:
    units = state.get("units")
    if not isinstance(units, Mapping):
        raise UnitCompletionAuthorizationError(
            "work-unit completion projection requires state.units to be an object"
        )
    rows: list[dict[str, str]] = []
    for raw_unit_id, slot in units.items():
        unit_id = _layer_id(raw_unit_id, "work-unit completion projection unit id")
        if not isinstance(slot, Mapping):
            raise UnitCompletionAuthorizationError(
                f"work-unit completion projection slot {unit_id!r} must be an object"
            )
        if slot.get("status") != "passed":
            continue
        try:
            unit_hash = require_digest(
                slot.get("unit_hash"),
                f"work-unit completion projection {unit_id}.unit_hash",
            )
            receipt = UnitCompletionReceipt.parse(
                slot.get("completion_receipt"),
                f"work-unit completion projection {unit_id}.completion_receipt",
            )
        except ValueError as exc:
            raise UnitCompletionAuthorizationError(str(exc)) from exc
        rows.append(
            {
                "unit_id": unit_id,
                "unit_generation_digest": unit_hash,
                "completion_receipt_digest": receipt.receipt_digest,
            }
        )
    return tuple(sorted(rows, key=lambda row: row["unit_id"]))


def completion_projection_digest(state: Mapping[str, Any]) -> str:
    """Hash only state facts that can grant accepted-unit dependency authority.

    Attempt lifecycle mutations such as ``planning -> building`` deliberately do not
    stale this projection.  Completing, revoking, replacing, or re-binding a passed
    unit always does.
    """

    if not isinstance(state, Mapping):
        raise UnitCompletionAuthorizationError(
            "work-unit completion projection requires a state object"
        )
    layer_id = _layer_id(state.get("layer"), "work-unit completion projection layer")
    digest_schema = state.get("digest_schema")
    if (
        not isinstance(digest_schema, int)
        or isinstance(digest_schema, bool)
        or digest_schema < 1
    ):
        raise UnitCompletionAuthorizationError(
            "work-unit completion projection digest_schema must be a positive integer"
        )
    try:
        layer_generation_digest = require_digest(
            state.get("plan_hash"),
            "work-unit completion projection layer generation",
        )
    except ValueError as exc:
        raise UnitCompletionAuthorizationError(str(exc)) from exc
    return canonical_digest(
        {
            "schema": "vfx-harness.unit-completion-projection/v1",
            "digest_schema": digest_schema,
            "layer_id": layer_id,
            "layer_generation_digest": layer_generation_digest,
            "passed_units": list(_passed_completion_rows(state)),
        }
    )


@dataclass(frozen=True, slots=True)
class AuthorizedUnitCompletionSet:
    """Exact current coordinator authorization for one layer's passed units."""

    layer_id: str
    selection_token: AuthoritySelectionTokenProjection
    authority_state_head_ref: AuthorityStateRecordRef
    layer_generation_digest: str
    completion_projection_digest: str
    receipts: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        _layer_id(self.layer_id, "authorized completion layer_id")
        if not isinstance(
            self.selection_token,
            AuthoritySelectionTokenProjection,
        ):
            raise UnitCompletionAuthorizationError(
                "authorized completion selection_token must be typed"
            )
        if not isinstance(self.authority_state_head_ref, AuthorityStateRecordRef):
            raise UnitCompletionAuthorizationError(
                "authorized completion authority_state_head_ref must be typed"
            )
        try:
            require_digest(
                self.layer_generation_digest,
                "authorized completion layer_generation_digest",
            )
            require_digest(
                self.completion_projection_digest,
                "authorized completion projection digest",
            )
        except ValueError as exc:
            raise UnitCompletionAuthorizationError(str(exc)) from exc
        _receipt_rows(self.receipts, "authorized completion receipts")

    def receipt_digest(self, unit_id: str) -> str | None:
        unit_id = str(unit_id)
        return next(
            (digest for candidate, digest in self.receipts if candidate == unit_id),
            None,
        )

    @property
    def unit_ids(self) -> frozenset[str]:
        return frozenset(unit_id for unit_id, _digest in self.receipts)


@dataclass(frozen=True, slots=True)
class CandidateAuthorizedUnitCompletionSet:
    """Exact receipt authority proved for one unpublished candidate layer state."""

    layer_id: str
    layer_generation_digest: str
    preview_reference: AuthorityPreviewReference
    completion_projection_digest: str
    receipts: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        _layer_id(self.layer_id, "candidate completion layer_id")
        try:
            require_digest(
                self.layer_generation_digest,
                "candidate completion layer_generation_digest",
            )
            require_digest(
                self.completion_projection_digest,
                "candidate completion projection digest",
            )
        except ValueError as exc:
            raise UnitCompletionAuthorizationError(str(exc)) from exc
        if not isinstance(self.preview_reference, AuthorityPreviewReference):
            raise UnitCompletionAuthorizationError(
                "candidate completion preview_reference must be typed"
            )
        if self.layer_id not in dict(self.preview_reference.after_state_hashes):
            raise UnitCompletionAuthorizationError(
                "candidate completion layer has no exact after-state preview hash"
            )
        _receipt_rows(self.receipts, "candidate completion receipts")

    def receipt_digest(self, unit_id: str) -> str | None:
        unit_id = str(unit_id)
        return next(
            (digest for candidate, digest in self.receipts if candidate == unit_id),
            None,
        )

    @property
    def unit_ids(self) -> frozenset[str]:
        return frozenset(unit_id for unit_id, _digest in self.receipts)


UnitCompletionAuthorization: TypeAlias = (
    AuthorizedUnitCompletionSet | CandidateAuthorizedUnitCompletionSet
)


def require_completion_authorization_matches_state(
    state: Mapping[str, Any],
    authorization: UnitCompletionAuthorization,
    *,
    allow_candidate: bool,
) -> None:
    """Require one attestation to bind the exact completion-relevant state."""

    allowed_types = (
        (AuthorizedUnitCompletionSet, CandidateAuthorizedUnitCompletionSet)
        if allow_candidate
        else (AuthorizedUnitCompletionSet,)
    )
    if not isinstance(authorization, allowed_types):
        kind = "current or candidate" if allow_candidate else "current coordinator"
        raise UnitCompletionAuthorizationError(
            f"work-unit completion requires a typed {kind} authorization"
        )
    if str(state.get("layer")) != authorization.layer_id:
        raise UnitCompletionAuthorizationError(
            "work-unit completion authorization belongs to another layer"
        )
    if state.get("plan_hash") != authorization.layer_generation_digest:
        raise UnitCompletionAuthorizationError(
            "work-unit completion authorization belongs to another layer generation"
        )
    observed = completion_projection_digest(state)
    if observed != authorization.completion_projection_digest:
        raise UnitCompletionAuthorizationError(
            "work-unit completion state changed after receipt authorization"
        )


__all__ = [
    "AuthorizedUnitCompletionSet",
    "CandidateAuthorizedUnitCompletionSet",
    "UnitCompletionAuthorization",
    "UnitCompletionAuthorizationError",
    "completion_projection_digest",
    "require_completion_authorization_matches_state",
]
