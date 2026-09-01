"""Pure reference record for an unpublished authority-state preview.

The preview is not selected authority.  It names one already-prepared immutable
transition intent and the exact state images a deterministic consumer may inspect
before publication.  Consumers still have to resolve and verify every reference;
the record is an address, never a verdict.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vfx_harness.domain.authority_head_records import (
    AuthorityHeadRecordError,
    AuthoritySelectionTokenProjection,
    canonical_json_bytes,
    decode_canonical_json_object,
    parse_authority_selection_token,
)
from vfx_harness.domain.authority_state_records import (
    AuthorityStateRecordError,
    AuthorityStateRecordRef,
)
from vfx_harness.domain.stop_envelope_primitives import (
    canonical_digest,
    require_canonical_digest,
    require_digest,
)

AUTHORITY_PREVIEW_REFERENCE_SCHEMA = (
    "vfx-harness.authority-state-preview-reference/v1"
)
AUTHORITY_PREVIEW_REFERENCE_PATH = Path(".authority-state-preview.json")
_FIELDS = frozenset(
    {
        "schema",
        "preview_digest",
        "transition_intent_ref",
        "predecessor_head_ref",
        "before_selection_token",
        "after_selection_token",
        "capsule_set_digest",
        "effects_digest",
        "before_state_hashes",
        "after_state_hashes",
    }
)


def _selection_dict(
    token: AuthoritySelectionTokenProjection,
) -> dict[str, Any]:
    return {
        "schema": "vfx-harness.authority-selection-token/v1",
        "plan_revision": token.plan_revision,
        "plan_pointer_sha256": token.plan_pointer_sha256,
        "jit_revision": token.jit_revision,
        "jit_pointer_sha256": token.jit_pointer_sha256,
    }


def _state_hashes(value: object, where: str) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{where} must be an object")
    rows: list[tuple[str, str]] = []
    for layer_id, digest in value.items():
        if (
            not isinstance(layer_id, str)
            or not layer_id
            or layer_id != layer_id.strip()
        ):
            raise ValueError(f"{where} contains an invalid layer id")
        rows.append((layer_id, require_digest(digest, f"{where}[{layer_id!r}]")))
    if len(rows) != len({layer_id for layer_id, _digest in rows}):
        raise ValueError(f"{where} contains duplicate layer ids")
    return tuple(sorted(rows))


@dataclass(frozen=True, slots=True)
class AuthorityPreviewReference:
    """Content-addressed unpublished transition and exact state byte identity."""

    transition_intent_ref: AuthorityStateRecordRef
    predecessor_head_ref: AuthorityStateRecordRef
    before_selection_token: AuthoritySelectionTokenProjection
    after_selection_token: AuthoritySelectionTokenProjection
    capsule_set_digest: str
    effects_digest: str
    before_state_hashes: tuple[tuple[str, str], ...]
    after_state_hashes: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.transition_intent_ref, AuthorityStateRecordRef) or (
            self.transition_intent_ref.record_schema
            != "vfx-harness.authority-state-transition-intent/v1"
        ):
            raise ValueError(
                "authority preview transition_intent_ref must name a v1 transition intent"
            )
        if not isinstance(self.predecessor_head_ref, AuthorityStateRecordRef) or (
            self.predecessor_head_ref.record_schema
            != "vfx-harness.authority-state-head/v1"
        ):
            raise ValueError(
                "authority preview predecessor_head_ref must name a v1 coordinator head"
            )
        for field, token in (
            ("before_selection_token", self.before_selection_token),
            ("after_selection_token", self.after_selection_token),
        ):
            try:
                parsed = parse_authority_selection_token(
                    _selection_dict(token),
                    f"authority preview {field}",
                )
            except AuthorityHeadRecordError as exc:
                raise ValueError(str(exc)) from exc
            if parsed != token:
                raise ValueError(f"authority preview {field} is not canonical")
        if self.before_selection_token == self.after_selection_token:
            raise ValueError(
                "authority preview must reference a non-noop selection transition"
            )
        require_digest(
            self.capsule_set_digest,
            "authority preview capsule_set_digest",
        )
        require_digest(self.effects_digest, "authority preview effects_digest")
        if self.before_state_hashes != tuple(sorted(self.before_state_hashes)):
            raise ValueError("authority preview before_state_hashes must be sorted")
        if self.after_state_hashes != tuple(sorted(self.after_state_hashes)):
            raise ValueError("authority preview after_state_hashes must be sorted")
        _state_hashes(dict(self.before_state_hashes), "authority preview before_state_hashes")
        _state_hashes(dict(self.after_state_hashes), "authority preview after_state_hashes")

    def identity_dict(self) -> dict[str, Any]:
        return {
            "schema": AUTHORITY_PREVIEW_REFERENCE_SCHEMA,
            "transition_intent_ref": self.transition_intent_ref.as_dict(),
            "predecessor_head_ref": self.predecessor_head_ref.as_dict(),
            "before_selection_token": _selection_dict(
                self.before_selection_token
            ),
            "after_selection_token": _selection_dict(self.after_selection_token),
            "capsule_set_digest": self.capsule_set_digest,
            "effects_digest": self.effects_digest,
            "before_state_hashes": dict(self.before_state_hashes),
            "after_state_hashes": dict(self.after_state_hashes),
        }

    @property
    def preview_digest(self) -> str:
        return canonical_digest(self.identity_dict())

    def as_dict(self) -> dict[str, Any]:
        return {**self.identity_dict(), "preview_digest": self.preview_digest}

    def to_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def mint(
        cls,
        *,
        transition_intent_ref: AuthorityStateRecordRef,
        predecessor_head_ref: AuthorityStateRecordRef,
        before_selection_token: AuthoritySelectionTokenProjection,
        after_selection_token: AuthoritySelectionTokenProjection,
        capsule_set_digest: str,
        effects_digest: str,
        before_state_hashes: Mapping[str, str],
        after_state_hashes: Mapping[str, str],
    ) -> AuthorityPreviewReference:
        return cls(
            transition_intent_ref=transition_intent_ref,
            predecessor_head_ref=predecessor_head_ref,
            before_selection_token=before_selection_token,
            after_selection_token=after_selection_token,
            capsule_set_digest=capsule_set_digest,
            effects_digest=effects_digest,
            before_state_hashes=_state_hashes(
                before_state_hashes,
                "authority preview before_state_hashes",
            ),
            after_state_hashes=_state_hashes(
                after_state_hashes,
                "authority preview after_state_hashes",
            ),
        )

    @classmethod
    def from_dict(cls, value: object) -> AuthorityPreviewReference:
        if not isinstance(value, Mapping) or set(value) != _FIELDS:
            found = set(value) if isinstance(value, Mapping) else set()
            raise ValueError(
                "authority preview reference fields mismatch; "
                f"missing={sorted(_FIELDS - found)}; "
                f"unexpected={sorted(found - _FIELDS)}"
            )
        if value.get("schema") != AUTHORITY_PREVIEW_REFERENCE_SCHEMA:
            raise ValueError("authority preview reference schema is unsupported")
        try:
            record = cls(
                transition_intent_ref=AuthorityStateRecordRef.parse(
                    value.get("transition_intent_ref"),
                    "authority preview transition_intent_ref",
                ),
                predecessor_head_ref=AuthorityStateRecordRef.parse(
                    value.get("predecessor_head_ref"),
                    "authority preview predecessor_head_ref",
                ),
                before_selection_token=parse_authority_selection_token(
                    value.get("before_selection_token"),
                    "authority preview before_selection_token",
                ),
                after_selection_token=parse_authority_selection_token(
                    value.get("after_selection_token"),
                    "authority preview after_selection_token",
                ),
                capsule_set_digest=require_digest(
                    value.get("capsule_set_digest"),
                    "authority preview capsule_set_digest",
                ),
                effects_digest=require_digest(
                    value.get("effects_digest"),
                    "authority preview effects_digest",
                ),
                before_state_hashes=_state_hashes(
                    value.get("before_state_hashes"),
                    "authority preview before_state_hashes",
                ),
                after_state_hashes=_state_hashes(
                    value.get("after_state_hashes"),
                    "authority preview after_state_hashes",
                ),
            )
        except (AuthorityHeadRecordError, AuthorityStateRecordError) as exc:
            raise ValueError(str(exc)) from exc
        require_canonical_digest(
            value.get("preview_digest"),
            record.preview_digest,
            "authority preview reference",
            "preview_digest",
        )
        return record

    @classmethod
    def from_bytes(cls, payload: bytes) -> AuthorityPreviewReference:
        try:
            value = decode_canonical_json_object(
                payload,
                "authority preview reference",
            )
        except AuthorityHeadRecordError as exc:
            raise ValueError(str(exc)) from exc
        return cls.from_dict(value)


__all__ = [
    "AUTHORITY_PREVIEW_REFERENCE_PATH",
    "AUTHORITY_PREVIEW_REFERENCE_SCHEMA",
    "AuthorityPreviewReference",
]
