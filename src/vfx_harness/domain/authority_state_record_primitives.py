"""Pure primitive records for crash-safe authority/state transitions.

The record graph is deliberately acyclic::

    proposal -> layer binding/state -> intent -> commit -> evaluation -> head

Historical execution receipts remain immutable.  These records authorize their
continued use without rewriting the execution identity they originally proved.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, fields
from pathlib import PurePosixPath
from typing import Any, ClassVar, TypeVar

from vfx_harness.domain.authority_head_records import (
    AUTHORITY_SELECTION_TOKEN_SCHEMA,
    AuthorityHeadRecordError,
    AuthoritySelectionTokenProjection,
    parse_authority_selection_token,
)
from vfx_harness.domain.stop_envelope_primitives import canonical_digest

AUTHORITY_STATE_RECORD_REF_SCHEMA = "vfx-harness.authority-state-record-ref/v1"
AUTHORITY_UNIT_BINDING_SCHEMA = "vfx-harness.authority-unit-binding/v1"
PREDECESSOR_LAYER_BINDING_SCHEMA = "vfx-harness.predecessor-layer-binding/v1"
LAYER_AUTHORITY_BINDING_SCHEMA = "vfx-harness.layer-authority-binding/v1"
AUTHORITY_POINTER_IMAGE_SCHEMA = "vfx-harness.authority-pointer-image/v1"
AUTHORITY_POINTER_TRANSITION_SCHEMA = "vfx-harness.authority-pointer-transition/v1"
AUTHORITY_STATE_MEMBER_IMAGE_SCHEMA = "vfx-harness.authority-state-member-image/v1"
AUTHORITY_STATE_MEMBER_TRANSITION_SCHEMA = "vfx-harness.authority-state-member-transition/v1"
AUTHORITY_STATE_LAYER_EFFECT_SCHEMA = "vfx-harness.authority-state-layer-effect/v1"
AUTHORITY_STATE_TRANSITION_PROPOSAL_SCHEMA = "vfx-harness.authority-state-transition-proposal/v1"
AUTHORITY_STATE_TRANSITION_INTENT_SCHEMA = "vfx-harness.authority-state-transition-intent/v1"
AUTHORITY_STATE_PENDING_POINTER_SCHEMA = "vfx-harness.authority-state-pending/v1"
AUTHORITY_STATE_TRANSITION_COMMIT_SCHEMA = "vfx-harness.authority-state-transition-commit/v1"
AUTHORITY_STATE_TRANSITION_EVALUATION_SCHEMA = "vfx-harness.authority-state-transition-evaluation/v1"
AUTHORITY_STATE_HEAD_SCHEMA = "vfx-harness.authority-state-head/v1"
AUTHORITY_CAPSULE_SET_SCHEMA = "vfx-harness.authority-capsule-set/v1"

POINTER_KINDS = frozenset({"plan", "jit"})
LAYER_EFFECT_KINDS = frozenset({"added", "removed", "unchanged", "changed", "incomparable", "downstream_invalidated"})
EVALUATION_RESULTS = frozenset({"satisfied", "failed"})


class AuthorityStateRecordError(ValueError):
    """An authority-state record is noncanonical or internally inconsistent."""


def authority_selection_token_dict(
    token: AuthoritySelectionTokenProjection,
) -> dict[str, Any]:
    """Return the sole wire representation of an exact selection token."""

    if not isinstance(token, AuthoritySelectionTokenProjection):
        raise AuthorityStateRecordError("authority selection token must be an AuthoritySelectionTokenProjection")
    return {
        "schema": AUTHORITY_SELECTION_TOKEN_SCHEMA,
        "plan_revision": token.plan_revision,
        "plan_pointer_sha256": token.plan_pointer_sha256,
        "jit_revision": token.jit_revision,
        "jit_pointer_sha256": token.jit_pointer_sha256,
    }


def _token(value: object, where: str) -> AuthoritySelectionTokenProjection:
    if isinstance(value, AuthoritySelectionTokenProjection):
        return value
    try:
        return parse_authority_selection_token(value, where)
    except AuthorityHeadRecordError as exc:
        raise AuthorityStateRecordError(str(exc)) from exc


def _digest(value: object, where: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise AuthorityStateRecordError(f"{where} must be a lowercase SHA-256 digest")
    return value


def _optional_digest(value: object, where: str) -> str | None:
    return None if value is None else _digest(value, where)


def _text(value: object, where: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise AuthorityStateRecordError(f"{where} must be a non-empty trimmed string")
    return value


def _identifier(value: object, where: str) -> str:
    value = _text(value, where)
    if any(
        character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-" for character in value
    ):
        raise AuthorityStateRecordError(f"{where} must be an identifier")
    if not value[0].isalnum():
        raise AuthorityStateRecordError(f"{where} must be an identifier")
    return value


def _revision(value: object, where: str, *, allow_zero: bool = False) -> int:
    minimum = 0 if allow_zero else 1
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        qualifier = "non-negative" if allow_zero else "positive"
        raise AuthorityStateRecordError(f"{where} must be a {qualifier} integer")
    return value


def _locator(value: object, where: str) -> str:
    value = _text(value, where)
    if "\\" in value:
        raise AuthorityStateRecordError(f"{where} must use POSIX separators")
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value or any(part in {"", ".", ".."} for part in path.parts):
        raise AuthorityStateRecordError(f"{where} must be a normalized safe relative locator")
    return value


def _timestamp(value: object, where: str) -> str:
    return _text(value, where)


def _optional_identifier(value: object, where: str) -> str | None:
    return None if value is None else _identifier(value, where)


def _sorted_ids(value: object, where: str) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise AuthorityStateRecordError(f"{where} must be a tuple")
    rows = tuple(_identifier(item, f"{where}[{index}]") for index, item in enumerate(value))
    if rows != tuple(sorted(set(rows))):
        raise AuthorityStateRecordError(f"{where} must be sorted and unique")
    return rows


def _mint_ids(values: Iterable[str], where: str) -> tuple[str, ...]:
    rows = tuple(_identifier(item, where) for item in values)
    if len(rows) != len(set(rows)):
        raise AuthorityStateRecordError(f"{where} contains duplicates")
    return tuple(sorted(rows))


def _wire_encode(value: Any) -> Any:
    if isinstance(value, _SemanticRecord):
        return value.as_dict()
    if isinstance(value, AuthoritySelectionTokenProjection):
        return authority_selection_token_dict(value)
    if isinstance(value, tuple):
        return [_wire_encode(item) for item in value]
    return value


def _identity_encode(value: Any) -> Any:
    if isinstance(value, _SemanticRecord):
        return value.identity_dict()
    if isinstance(value, AuthoritySelectionTokenProjection):
        return authority_selection_token_dict(value)
    if isinstance(value, tuple):
        return [_identity_encode(item) for item in value]
    return value


class _SemanticRecord:
    SCHEMA: ClassVar[str]
    DIGEST_FIELD: ClassVar[str]
    AUDIT_FIELDS: ClassVar[frozenset[str]] = frozenset()

    def _payload(self, *, identity: bool) -> dict[str, Any]:
        encoder = _identity_encode if identity else _wire_encode
        return {
            "schema": self.SCHEMA,
            **{
                item.name: encoder(getattr(self, item.name))
                for item in fields(self)
                if not identity or item.name not in self.AUDIT_FIELDS
            },
        }

    @property
    def digest(self) -> str:
        return canonical_digest(self._payload(identity=True))

    def identity_dict(self) -> dict[str, Any]:
        return {**self._payload(identity=True), self.DIGEST_FIELD: self.digest}

    def as_dict(self) -> dict[str, Any]:
        return {**self._payload(identity=False), self.DIGEST_FIELD: self.digest}


RecordT = TypeVar("RecordT", bound=_SemanticRecord)


def _row(cls: type[RecordT], value: object, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AuthorityStateRecordError(f"{where} must be an object")
    expected = {"schema", cls.DIGEST_FIELD, *(item.name for item in fields(cls))}
    found = set(value)
    if found != expected:
        raise AuthorityStateRecordError(
            f"{where} fields mismatch; missing={sorted(expected - found)}; unexpected={sorted(found - expected)}"
        )
    if value.get("schema") != cls.SCHEMA:
        raise AuthorityStateRecordError(f"{where}.schema must be {cls.SCHEMA!r}, found {value.get('schema')!r}")
    return value


def _finish(candidate: RecordT, row: Mapping[str, Any], where: str) -> RecordT:
    found = _digest(row[candidate.DIGEST_FIELD], f"{where}.{candidate.DIGEST_FIELD}")
    if found != candidate.digest:
        raise AuthorityStateRecordError(
            f"{where}.{candidate.DIGEST_FIELD} is stale; expected {candidate.digest!r}, found {found!r}"
        )
    return candidate


def _record_list(value: object, where: str) -> list[object]:
    if not isinstance(value, list):
        raise AuthorityStateRecordError(f"{where} must be a list")
    return value


def _sorted_records(
    values: Iterable[RecordT],
    where: str,
    *,
    key: str,
) -> tuple[RecordT, ...]:
    rows = tuple(values)
    if any(not isinstance(item, _SemanticRecord) for item in rows):
        raise AuthorityStateRecordError(f"{where} must contain typed records")
    keys = tuple(getattr(item, key) for item in rows)
    if len(keys) != len(set(keys)):
        raise AuthorityStateRecordError(f"{where} contains duplicate {key} values")
    return tuple(sorted(rows, key=lambda item: getattr(item, key)))


def _require_sorted_records(value: tuple[RecordT, ...], where: str, *, key: str) -> None:
    expected = _sorted_records(value, where, key=key)
    if value != expected:
        raise AuthorityStateRecordError(f"{where} must be sorted by {key}")


@dataclass(frozen=True, slots=True)
class AuthorityStateRecordRef(_SemanticRecord):
    SCHEMA: ClassVar[str] = AUTHORITY_STATE_RECORD_REF_SCHEMA
    DIGEST_FIELD: ClassVar[str] = "ref_digest"

    locator: str
    sha256: str
    record_schema: str
    record_digest: str

    def __post_init__(self) -> None:
        _locator(self.locator, "authority-state record ref.locator")
        _digest(self.sha256, "authority-state record ref.sha256")
        _text(self.record_schema, "authority-state record ref.record_schema")
        _digest(self.record_digest, "authority-state record ref.record_digest")

    @classmethod
    def mint(cls, *, locator: str, sha256: str, record_schema: str, record_digest: str) -> AuthorityStateRecordRef:
        return cls(locator, sha256, record_schema, record_digest)

    @classmethod
    def parse(cls, value: object, where: str = "authority-state record ref") -> AuthorityStateRecordRef:
        row = _row(cls, value, where)
        return _finish(
            cls(row["locator"], row["sha256"], row["record_schema"], row["record_digest"]),
            row,
            where,
        )


@dataclass(frozen=True, slots=True)
class AuthorityUnitBinding(_SemanticRecord):
    SCHEMA: ClassVar[str] = AUTHORITY_UNIT_BINDING_SCHEMA
    DIGEST_FIELD: ClassVar[str] = "unit_binding_digest"

    unit_id: str
    unit_generation_digest: str
    completion_receipt_digest: str | None

    def __post_init__(self) -> None:
        _identifier(self.unit_id, "authority unit binding.unit_id")
        _digest(self.unit_generation_digest, "authority unit binding.unit_generation_digest")
        _optional_digest(
            self.completion_receipt_digest,
            "authority unit binding.completion_receipt_digest",
        )

    @classmethod
    def mint(
        cls,
        *,
        unit_id: str,
        unit_generation_digest: str,
        completion_receipt_digest: str | None = None,
    ) -> AuthorityUnitBinding:
        return cls(unit_id, unit_generation_digest, completion_receipt_digest)

    @classmethod
    def parse(cls, value: object, where: str = "authority unit binding") -> AuthorityUnitBinding:
        row = _row(cls, value, where)
        return _finish(
            cls(row["unit_id"], row["unit_generation_digest"], row["completion_receipt_digest"]),
            row,
            where,
        )


@dataclass(frozen=True, slots=True)
class PredecessorLayerBinding(_SemanticRecord):
    SCHEMA: ClassVar[str] = PREDECESSOR_LAYER_BINDING_SCHEMA
    DIGEST_FIELD: ClassVar[str] = "predecessor_binding_digest"

    layer_id: str
    layer_generation_digest: str
    finalization_receipt_digest: str | None

    def __post_init__(self) -> None:
        _identifier(self.layer_id, "predecessor layer binding.layer_id")
        _digest(
            self.layer_generation_digest,
            "predecessor layer binding.layer_generation_digest",
        )
        _optional_digest(
            self.finalization_receipt_digest,
            "predecessor layer binding.finalization_receipt_digest",
        )

    @classmethod
    def mint(
        cls,
        *,
        layer_id: str,
        layer_generation_digest: str,
        finalization_receipt_digest: str | None = None,
    ) -> PredecessorLayerBinding:
        return cls(layer_id, layer_generation_digest, finalization_receipt_digest)

    @classmethod
    def parse(cls, value: object, where: str = "predecessor layer binding") -> PredecessorLayerBinding:
        row = _row(cls, value, where)
        return _finish(
            cls(
                row["layer_id"],
                row["layer_generation_digest"],
                row["finalization_receipt_digest"],
            ),
            row,
            where,
        )


@dataclass(frozen=True, slots=True)
class LayerAuthorityBinding(_SemanticRecord):
    SCHEMA: ClassVar[str] = LAYER_AUTHORITY_BINDING_SCHEMA
    DIGEST_FIELD: ClassVar[str] = "binding_digest"

    transition_revision: int
    transition_proposal_digest: str
    selection_token: AuthoritySelectionTokenProjection
    layer_id: str
    layer_generation_digest: str
    units: tuple[AuthorityUnitBinding, ...]
    predecessors: tuple[PredecessorLayerBinding, ...]
    finalization_receipt_digest: str | None

    def __post_init__(self) -> None:
        _revision(self.transition_revision, "layer authority binding.transition_revision")
        _digest(
            self.transition_proposal_digest,
            "layer authority binding.transition_proposal_digest",
        )
        _token(self.selection_token, "layer authority binding.selection_token")
        _identifier(self.layer_id, "layer authority binding.layer_id")
        _digest(
            self.layer_generation_digest,
            "layer authority binding.layer_generation_digest",
        )
        if not self.units:
            raise AuthorityStateRecordError("layer authority binding.units must be non-empty")
        _require_sorted_records(self.units, "layer authority binding.units", key="unit_id")
        _require_sorted_records(
            self.predecessors,
            "layer authority binding.predecessors",
            key="layer_id",
        )
        if any(item.layer_id == self.layer_id for item in self.predecessors):
            raise AuthorityStateRecordError("layer authority binding cannot depend on itself")
        receipts = tuple(
            item.completion_receipt_digest for item in self.units if item.completion_receipt_digest is not None
        )
        if len(receipts) != len(set(receipts)):
            raise AuthorityStateRecordError("layer authority binding completion receipts must be unique")
        _optional_digest(
            self.finalization_receipt_digest,
            "layer authority binding.finalization_receipt_digest",
        )

    @classmethod
    def mint(
        cls,
        *,
        transition_revision: int,
        transition_proposal_digest: str,
        selection_token: AuthoritySelectionTokenProjection | Mapping[str, Any],
        layer_id: str,
        layer_generation_digest: str,
        units: Iterable[AuthorityUnitBinding],
        predecessors: Iterable[PredecessorLayerBinding] = (),
        finalization_receipt_digest: str | None = None,
    ) -> LayerAuthorityBinding:
        return cls(
            transition_revision,
            transition_proposal_digest,
            _token(selection_token, "layer authority binding.selection_token"),
            layer_id,
            layer_generation_digest,
            _sorted_records(units, "layer authority binding.units", key="unit_id"),
            _sorted_records(
                predecessors,
                "layer authority binding.predecessors",
                key="layer_id",
            ),
            finalization_receipt_digest,
        )

    @classmethod
    def parse(cls, value: object, where: str = "layer authority binding") -> LayerAuthorityBinding:
        row = _row(cls, value, where)
        units = tuple(
            AuthorityUnitBinding.parse(item, f"{where}.units[{index}]")
            for index, item in enumerate(_record_list(row["units"], f"{where}.units"))
        )
        predecessors = tuple(
            PredecessorLayerBinding.parse(item, f"{where}.predecessors[{index}]")
            for index, item in enumerate(_record_list(row["predecessors"], f"{where}.predecessors"))
        )
        return _finish(
            cls(
                row["transition_revision"],
                row["transition_proposal_digest"],
                _token(row["selection_token"], f"{where}.selection_token"),
                row["layer_id"],
                row["layer_generation_digest"],
                units,
                predecessors,
                row["finalization_receipt_digest"],
            ),
            row,
            where,
        )


@dataclass(frozen=True, slots=True)
class AuthorityPointerImage(_SemanticRecord):
    SCHEMA: ClassVar[str] = AUTHORITY_POINTER_IMAGE_SCHEMA
    DIGEST_FIELD: ClassVar[str] = "pointer_image_digest"

    revision: int
    locator: str
    sha256: str

    def __post_init__(self) -> None:
        _revision(self.revision, "authority pointer image.revision")
        _locator(self.locator, "authority pointer image.locator")
        _digest(self.sha256, "authority pointer image.sha256")

    @classmethod
    def mint(cls, *, revision: int, locator: str, sha256: str) -> AuthorityPointerImage:
        return cls(revision, locator, sha256)

    @classmethod
    def parse(cls, value: object, where: str = "authority pointer image") -> AuthorityPointerImage:
        row = _row(cls, value, where)
        return _finish(cls(row["revision"], row["locator"], row["sha256"]), row, where)


@dataclass(frozen=True, slots=True)
class AuthorityPointerTransition(_SemanticRecord):
    SCHEMA: ClassVar[str] = AUTHORITY_POINTER_TRANSITION_SCHEMA
    DIGEST_FIELD: ClassVar[str] = "pointer_transition_digest"

    pointer_kind: str
    live_locator: str
    before: AuthorityPointerImage | None
    after: AuthorityPointerImage | None

    def __post_init__(self) -> None:
        if self.pointer_kind not in POINTER_KINDS:
            raise AuthorityStateRecordError(
                f"authority pointer transition.pointer_kind must be one of {sorted(POINTER_KINDS)}"
            )
        _locator(self.live_locator, "authority pointer transition.live_locator")
        if self.before is not None and not isinstance(self.before, AuthorityPointerImage):
            raise AuthorityStateRecordError("authority pointer transition.before is invalid")
        if self.after is not None and not isinstance(self.after, AuthorityPointerImage):
            raise AuthorityStateRecordError("authority pointer transition.after is invalid")

    @classmethod
    def mint(
        cls,
        *,
        pointer_kind: str,
        live_locator: str,
        before: AuthorityPointerImage | None,
        after: AuthorityPointerImage | None,
    ) -> AuthorityPointerTransition:
        return cls(pointer_kind, live_locator, before, after)

    @classmethod
    def parse(cls, value: object, where: str = "authority pointer transition") -> AuthorityPointerTransition:
        row = _row(cls, value, where)
        before = None if row["before"] is None else AuthorityPointerImage.parse(row["before"], f"{where}.before")
        after = None if row["after"] is None else AuthorityPointerImage.parse(row["after"], f"{where}.after")
        return _finish(cls(row["pointer_kind"], row["live_locator"], before, after), row, where)


@dataclass(frozen=True, slots=True)
class AuthorityStateMemberImage(_SemanticRecord):
    SCHEMA: ClassVar[str] = AUTHORITY_STATE_MEMBER_IMAGE_SCHEMA
    DIGEST_FIELD: ClassVar[str] = "member_image_digest"

    layer_id: str
    locator: str
    sha256: str
    state_revision: int
    binding: LayerAuthorityBinding

    def __post_init__(self) -> None:
        _identifier(self.layer_id, "authority state member image.layer_id")
        _locator(self.locator, "authority state member image.locator")
        _digest(self.sha256, "authority state member image.sha256")
        _revision(self.state_revision, "authority state member image.state_revision")
        if not isinstance(self.binding, LayerAuthorityBinding):
            raise AuthorityStateRecordError("authority state member image.binding is invalid")
        if self.binding.layer_id != self.layer_id:
            raise AuthorityStateRecordError("authority state member image layer_id disagrees with its binding")

    @classmethod
    def mint(
        cls,
        *,
        layer_id: str,
        locator: str,
        sha256: str,
        state_revision: int,
        binding: LayerAuthorityBinding,
    ) -> AuthorityStateMemberImage:
        return cls(layer_id, locator, sha256, state_revision, binding)

    @classmethod
    def parse(cls, value: object, where: str = "authority state member image") -> AuthorityStateMemberImage:
        row = _row(cls, value, where)
        return _finish(
            cls(
                row["layer_id"],
                row["locator"],
                row["sha256"],
                row["state_revision"],
                LayerAuthorityBinding.parse(row["binding"], f"{where}.binding"),
            ),
            row,
            where,
        )


@dataclass(frozen=True, slots=True)
class AuthorityStateMemberTransition(_SemanticRecord):
    SCHEMA: ClassVar[str] = AUTHORITY_STATE_MEMBER_TRANSITION_SCHEMA
    DIGEST_FIELD: ClassVar[str] = "member_transition_digest"

    layer_id: str
    live_locator: str
    before: AuthorityStateMemberImage | None
    after: AuthorityStateMemberImage | None

    def __post_init__(self) -> None:
        _identifier(self.layer_id, "authority state member transition.layer_id")
        _locator(self.live_locator, "authority state member transition.live_locator")
        if self.before is None and self.after is None:
            raise AuthorityStateRecordError("authority state member transition must have a before or after image")
        for side_name, side in (("before", self.before), ("after", self.after)):
            if side is not None and not isinstance(side, AuthorityStateMemberImage):
                raise AuthorityStateRecordError(f"authority state member transition.{side_name} is invalid")
            if side is not None and side.layer_id != self.layer_id:
                raise AuthorityStateRecordError(f"authority state member transition.{side_name} layer_id disagrees")

    @classmethod
    def mint(
        cls,
        *,
        layer_id: str,
        live_locator: str,
        before: AuthorityStateMemberImage | None,
        after: AuthorityStateMemberImage | None,
    ) -> AuthorityStateMemberTransition:
        return cls(layer_id, live_locator, before, after)

    @classmethod
    def parse(cls, value: object, where: str = "authority state member transition") -> AuthorityStateMemberTransition:
        row = _row(cls, value, where)
        before = None if row["before"] is None else AuthorityStateMemberImage.parse(row["before"], f"{where}.before")
        after = None if row["after"] is None else AuthorityStateMemberImage.parse(row["after"], f"{where}.after")
        return _finish(cls(row["layer_id"], row["live_locator"], before, after), row, where)


@dataclass(frozen=True, slots=True)
class AuthorityStateLayerEffect(_SemanticRecord):
    SCHEMA: ClassVar[str] = AUTHORITY_STATE_LAYER_EFFECT_SCHEMA
    DIGEST_FIELD: ClassVar[str] = "effect_digest"

    layer_id: str
    effect_kind: str
    preserved_units: tuple[AuthorityUnitBinding, ...]
    preserved_finalization_receipt_digest: str | None
    invalidation_seed_unit_ids: tuple[str, ...]
    invalidated_unit_ids: tuple[str, ...]
    invalidated_downstream_layer_ids: tuple[str, ...]
    revoked_unit_attempt_claim_ids: tuple[str, ...]
    revoked_layer_finalization_claim_id: str | None

    def __post_init__(self) -> None:
        _identifier(self.layer_id, "authority state layer effect.layer_id")
        if self.effect_kind not in LAYER_EFFECT_KINDS:
            raise AuthorityStateRecordError(
                f"authority state layer effect.effect_kind must be one of {sorted(LAYER_EFFECT_KINDS)}"
            )
        _require_sorted_records(
            self.preserved_units,
            "authority state layer effect.preserved_units",
            key="unit_id",
        )
        _optional_digest(
            self.preserved_finalization_receipt_digest,
            "authority state layer effect.preserved_finalization_receipt_digest",
        )
        seeds = _sorted_ids(
            self.invalidation_seed_unit_ids,
            "authority state layer effect.invalidation_seed_unit_ids",
        )
        invalidated = _sorted_ids(
            self.invalidated_unit_ids,
            "authority state layer effect.invalidated_unit_ids",
        )
        downstream = _sorted_ids(
            self.invalidated_downstream_layer_ids,
            "authority state layer effect.invalidated_downstream_layer_ids",
        )
        _sorted_ids(
            self.revoked_unit_attempt_claim_ids,
            "authority state layer effect.revoked_unit_attempt_claim_ids",
        )
        _optional_identifier(
            self.revoked_layer_finalization_claim_id,
            "authority state layer effect.revoked_layer_finalization_claim_id",
        )
        if not set(seeds).issubset(invalidated):
            raise AuthorityStateRecordError("authority state layer effect invalidation seeds must be invalidated")
        preserved_ids = {item.unit_id for item in self.preserved_units}
        if preserved_ids.intersection(invalidated):
            raise AuthorityStateRecordError("authority state layer effect cannot preserve and invalidate one unit")
        if self.layer_id in downstream:
            raise AuthorityStateRecordError("authority state layer effect downstream closure cannot contain itself")
        if self.preserved_finalization_receipt_digest is not None and (
            not self.preserved_units or any(item.completion_receipt_digest is None for item in self.preserved_units)
        ):
            raise AuthorityStateRecordError("a preserved layer finalization requires completed preserved units")
        if self.effect_kind in {"added", "removed", "incomparable"} and (
            self.preserved_units or self.preserved_finalization_receipt_digest is not None
        ):
            raise AuthorityStateRecordError(f"effect kind {self.effect_kind!r} cannot preserve prior authority")

    @classmethod
    def mint(
        cls,
        *,
        layer_id: str,
        effect_kind: str,
        preserved_units: Iterable[AuthorityUnitBinding] = (),
        preserved_finalization_receipt_digest: str | None = None,
        invalidation_seed_unit_ids: Iterable[str] = (),
        invalidated_unit_ids: Iterable[str] = (),
        invalidated_downstream_layer_ids: Iterable[str] = (),
        revoked_unit_attempt_claim_ids: Iterable[str] = (),
        revoked_layer_finalization_claim_id: str | None = None,
    ) -> AuthorityStateLayerEffect:
        return cls(
            layer_id,
            effect_kind,
            _sorted_records(
                preserved_units,
                "authority state layer effect.preserved_units",
                key="unit_id",
            ),
            preserved_finalization_receipt_digest,
            _mint_ids(
                invalidation_seed_unit_ids,
                "authority state layer effect.invalidation_seed_unit_ids",
            ),
            _mint_ids(
                invalidated_unit_ids,
                "authority state layer effect.invalidated_unit_ids",
            ),
            _mint_ids(
                invalidated_downstream_layer_ids,
                "authority state layer effect.invalidated_downstream_layer_ids",
            ),
            _mint_ids(
                revoked_unit_attempt_claim_ids,
                "authority state layer effect.revoked_unit_attempt_claim_ids",
            ),
            revoked_layer_finalization_claim_id,
        )

    @classmethod
    def parse(cls, value: object, where: str = "authority state layer effect") -> AuthorityStateLayerEffect:
        row = _row(cls, value, where)
        preserved = tuple(
            AuthorityUnitBinding.parse(item, f"{where}.preserved_units[{index}]")
            for index, item in enumerate(_record_list(row["preserved_units"], f"{where}.preserved_units"))
        )
        return _finish(
            cls(
                row["layer_id"],
                row["effect_kind"],
                preserved,
                row["preserved_finalization_receipt_digest"],
                tuple(_record_list(row["invalidation_seed_unit_ids"], f"{where}.invalidation_seed_unit_ids")),
                tuple(_record_list(row["invalidated_unit_ids"], f"{where}.invalidated_unit_ids")),
                tuple(
                    _record_list(row["invalidated_downstream_layer_ids"], f"{where}.invalidated_downstream_layer_ids")
                ),
                tuple(_record_list(row["revoked_unit_attempt_claim_ids"], f"{where}.revoked_unit_attempt_claim_ids")),
                row["revoked_layer_finalization_claim_id"],
            ),
            row,
            where,
        )


def authority_state_effects_digest(effects: tuple[AuthorityStateLayerEffect, ...]) -> str:
    """Digest one canonical, layer-sorted effect set."""

    _require_sorted_records(effects, "authority state effects", key="layer_id")
    return canonical_digest({"effects": [item.identity_dict() for item in effects]})


def _require_pointer_matches_token(
    transition: AuthorityPointerTransition,
    *,
    before: AuthoritySelectionTokenProjection,
    after: AuthoritySelectionTokenProjection,
) -> None:
    kind = transition.pointer_kind
    before_revision = getattr(before, f"{kind}_revision")
    before_sha = getattr(before, f"{kind}_pointer_sha256")
    after_revision = getattr(after, f"{kind}_revision")
    after_sha = getattr(after, f"{kind}_pointer_sha256")
    for side_name, image, revision, sha in (
        ("before", transition.before, before_revision, before_sha),
        ("after", transition.after, after_revision, after_sha),
    ):
        if revision == 0:
            if image is not None:
                raise AuthorityStateRecordError(f"{kind} {side_name} pointer must be absent at revision zero")
        elif image is None or image.revision != revision or image.sha256 != sha:
            raise AuthorityStateRecordError(f"{kind} {side_name} pointer does not match the exact selection token")


__all__ = [
    "AUTHORITY_CAPSULE_SET_SCHEMA",
    "AUTHORITY_POINTER_IMAGE_SCHEMA",
    "AUTHORITY_POINTER_TRANSITION_SCHEMA",
    "AUTHORITY_STATE_HEAD_SCHEMA",
    "AUTHORITY_STATE_LAYER_EFFECT_SCHEMA",
    "AUTHORITY_STATE_MEMBER_IMAGE_SCHEMA",
    "AUTHORITY_STATE_MEMBER_TRANSITION_SCHEMA",
    "AUTHORITY_STATE_PENDING_POINTER_SCHEMA",
    "AUTHORITY_STATE_RECORD_REF_SCHEMA",
    "AUTHORITY_STATE_TRANSITION_COMMIT_SCHEMA",
    "AUTHORITY_STATE_TRANSITION_EVALUATION_SCHEMA",
    "AUTHORITY_STATE_TRANSITION_INTENT_SCHEMA",
    "AUTHORITY_STATE_TRANSITION_PROPOSAL_SCHEMA",
    "AUTHORITY_UNIT_BINDING_SCHEMA",
    "LAYER_AUTHORITY_BINDING_SCHEMA",
    "PREDECESSOR_LAYER_BINDING_SCHEMA",
    "AuthorityPointerImage",
    "AuthorityPointerTransition",
    "AuthorityStateLayerEffect",
    "AuthorityStateMemberImage",
    "AuthorityStateMemberTransition",
    "AuthorityStateRecordError",
    "AuthorityStateRecordRef",
    "AuthorityUnitBinding",
    "LayerAuthorityBinding",
    "PredecessorLayerBinding",
    "authority_selection_token_dict",
    "authority_state_effects_digest",
]
