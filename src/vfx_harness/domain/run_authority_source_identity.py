"""Dependency-free primitives for HIR-0172 authority-source closures."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Any, ClassVar

AUTHORITY_SOURCE_IDENTITY_SCHEMA = "vfx-harness.interruption-authority-source-identity/v1"
SOURCE_KINDS = frozenset(
    {
        "brief",
        "reference_still",
        "refobs_registration",
        "refobs_crop",
        "plan_pointer",
        "plan_bundle_manifest",
        "plan_bundle_member",
        "plan_amendments",
        "plan_resolutions",
        "effective_view_pointer",
        "effective_view_member",
        "accepted_ledger",
        "accepted_member",
        "judgment_debts",
        "judgment_payment_attempts",
        "durable_state_pointer",
        "durable_state_pending",
        "durable_state_record",
        "durable_state_member",
    }
)
SOURCE_STATES = frozenset({"record_valid", "opaque_valid", "raw_invalid"})
FAMILY_STATES = frozenset({"absent", "present_valid", "present_invalid"})
INVALID_REASONS = frozenset(
    {
        "duplicate_json_key",
        "fields_mismatch",
        "malformed_json",
        "non_utf8",
        "reference_mismatch",
        "stale_record_digest",
        "unsupported_schema",
    }
)

DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SCHEMA_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*/v[1-9][0-9]*$")
BUNDLE_MANIFEST_PATTERN = re.compile(r"^runs/[^/]+/checkpoints/plans/bundles/([0-9a-f]{64})/bundle\.json$")
VIEW_MEMBER_PATTERN = re.compile(r"^state/jit-layers/views/([0-9a-f]{64})/(.+)$")
STATE_OBJECT_PATTERN = re.compile(r"^state/authority-state/objects/([0-9a-f]{64})/record\.json$")

_FIXED_LOCATORS = {
    "plan_pointer": "plans/current.json",
    "plan_amendments": "plan_amendments.jsonl",
    "plan_resolutions": "state/plan-resolutions.jsonl",
    "effective_view_pointer": "state/jit-layers/current.json",
    "accepted_ledger": "shot.json",
    "judgment_debts": "state/judgment-debts.jsonl",
    "judgment_payment_attempts": "state/judgment-payment-attempts.jsonl",
    "durable_state_pointer": "state/authority-state/current.json",
    "durable_state_pending": "state/authority-state/pending.json",
}
_FIXED_RECORD_SCHEMAS = {
    "plan_pointer": "vfx-harness.plan-pointer/v2",
    "plan_bundle_manifest": "vfx-harness.plan-bundle/v1",
    "effective_view_pointer": "vfx-harness.jit-layer-view/v2",
    # ``state/authority-state/current.json`` holds the exact bytes of the selected
    # coordinator head record; its content-addressed object copy is the closure's head.
    "durable_state_pointer": "vfx-harness.authority-state-head/v1",
    "durable_state_pending": "vfx-harness.authority-state-pending/v1",
    # A selected construction witness binds its typed registration record (HIR-0172
    # step 4); the crop bytes beside it are opaque.
    "refobs_registration": "vfx-harness.refobs/v1",
}
_TYPED_SOURCE_KINDS = frozenset(
    {
        *_FIXED_RECORD_SCHEMAS,
        "durable_state_record",
    }
)
_OPAQUE_ONLY_SOURCE_KINDS = frozenset(
    {
        "brief",
        "reference_still",
        "refobs_crop",
        "plan_amendments",
        "plan_resolutions",
        "judgment_debts",
        "judgment_payment_attempts",
    }
)
FIXED_RECORD_SCHEMAS = MappingProxyType(dict(_FIXED_RECORD_SCHEMAS))
TYPED_SOURCE_KINDS = _TYPED_SOURCE_KINDS
OPAQUE_ONLY_SOURCE_KINDS = _OPAQUE_ONLY_SOURCE_KINDS


def canonical_digest(value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def require_digest(value: object, where: str) -> str:
    if not isinstance(value, str) or DIGEST_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{where} must be a lowercase SHA-256 digest")
    return value


def require_schema(value: object, where: str) -> str:
    if not isinstance(value, str) or SCHEMA_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{where} must be a versioned record schema")
    return value


def require_locator(value: object, where: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{where} must be a non-empty trimmed locator")
    if "\\" in value:
        raise ValueError(f"{where} must use POSIX separators")
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"{where} must be a normalized safe relative locator")
    return value


def exact_record(
    value: object,
    where: str,
    *,
    schema: str,
    fields: frozenset[str],
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{where} must be an object")
    expected = {"schema", *fields}
    found = set(value)
    if found != expected:
        raise ValueError(
            f"{where} fields mismatch; missing={sorted(expected - found)}; unexpected={sorted(found - expected)}"
        )
    if value.get("schema") != schema:
        raise ValueError(f"{where}.schema must be {schema!r}, found {value.get('schema')!r}")
    return value


@dataclass(frozen=True, slots=True)
class AuthoritySourceIdentity:
    """Exact bytes and optional typed-record identity of one authority source."""

    SCHEMA: ClassVar[str] = AUTHORITY_SOURCE_IDENTITY_SCHEMA

    source_kind: str
    locator: str
    byte_count: int
    sha256: str
    source_state: str
    record_schema: str | None
    record_digest: str | None
    invalid_reason: str | None

    def __post_init__(self) -> None:
        if self.source_kind not in SOURCE_KINDS:
            raise ValueError(f"AuthoritySourceIdentity.source_kind must be one of {sorted(SOURCE_KINDS)}")
        locator = require_locator(self.locator, "AuthoritySourceIdentity.locator")
        if not isinstance(self.byte_count, int) or isinstance(self.byte_count, bool) or self.byte_count < 0:
            raise ValueError("AuthoritySourceIdentity.byte_count must be a non-negative integer")
        require_digest(self.sha256, "AuthoritySourceIdentity.sha256")
        if self.source_state not in SOURCE_STATES:
            raise ValueError(f"AuthoritySourceIdentity.source_state must be one of {sorted(SOURCE_STATES)}")

        fixed = _FIXED_LOCATORS.get(self.source_kind)
        if fixed is not None and locator != fixed:
            raise ValueError(f"{self.source_kind} source must use exact locator {fixed!r}")
        if self.source_kind == "plan_bundle_manifest" and BUNDLE_MANIFEST_PATTERN.fullmatch(locator) is None:
            raise ValueError("plan_bundle_manifest must name an immutable selected bundle manifest")
        if self.source_kind == "plan_bundle_member" and "/bundle.json" in locator:
            raise ValueError("plan_bundle_member cannot impersonate bundle.json")
        if self.source_kind == "effective_view_member" and VIEW_MEMBER_PATTERN.fullmatch(locator) is None:
            raise ValueError("effective_view_member must live below a content-addressed JIT view")
        if self.source_kind == "durable_state_record":
            match = STATE_OBJECT_PATTERN.fullmatch(locator)
            if match is None or match.group(1) != self.sha256:
                raise ValueError("durable_state_record locator must be content-addressed by its byte SHA")
        if self.source_kind == "durable_state_member" and not (
            locator.startswith("state/work-units/") and locator.endswith(".json")
        ):
            raise ValueError("durable_state_member must name a state/work-units JSON source")

        if self.source_state == "record_valid":
            if self.source_kind in _OPAQUE_ONLY_SOURCE_KINDS:
                raise ValueError(f"{self.source_kind} source is an exact raw-byte stream, not one typed JSON record")
            record_schema = require_schema(
                self.record_schema,
                "AuthoritySourceIdentity.record_schema",
            )
            require_digest(
                self.record_digest,
                "AuthoritySourceIdentity.record_digest",
            )
            if self.invalid_reason is not None:
                raise ValueError("record_valid source cannot carry invalid_reason")
            expected_schema = _FIXED_RECORD_SCHEMAS.get(self.source_kind)
            if expected_schema is not None and record_schema != expected_schema:
                raise ValueError(f"{self.source_kind} source record_schema must be {expected_schema!r}")
            return

        if self.source_state == "opaque_valid":
            if self.source_kind in _TYPED_SOURCE_KINDS:
                raise ValueError(f"{self.source_kind} source requires record_valid typed identity")
            if self.record_schema is not None or self.record_digest is not None or self.invalid_reason is not None:
                raise ValueError("opaque_valid source cannot invent record identity or invalid_reason")
            return

        if self.record_schema is not None or self.record_digest is not None:
            raise ValueError("raw_invalid source cannot invent record_schema or record_digest")
        if self.invalid_reason not in INVALID_REASONS:
            raise ValueError(f"raw_invalid source invalid_reason must be one of {sorted(INVALID_REASONS)}")

    @classmethod
    def valid(
        cls,
        *,
        source_kind: str,
        locator: str,
        byte_count: int,
        sha256: str,
        record_schema: str,
        record_digest: str,
    ) -> AuthoritySourceIdentity:
        return cls(
            source_kind,
            locator,
            byte_count,
            sha256,
            "record_valid",
            record_schema,
            record_digest,
            None,
        )

    @classmethod
    def raw_invalid(
        cls,
        *,
        source_kind: str,
        locator: str,
        byte_count: int,
        sha256: str,
        invalid_reason: str,
    ) -> AuthoritySourceIdentity:
        return cls(
            source_kind,
            locator,
            byte_count,
            sha256,
            "raw_invalid",
            None,
            None,
            invalid_reason,
        )

    @classmethod
    def opaque_valid(
        cls,
        *,
        source_kind: str,
        locator: str,
        byte_count: int,
        sha256: str,
    ) -> AuthoritySourceIdentity:
        return cls(
            source_kind,
            locator,
            byte_count,
            sha256,
            "opaque_valid",
            None,
            None,
            None,
        )

    def _payload(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "source_kind": self.source_kind,
            "locator": self.locator,
            "byte_count": self.byte_count,
            "sha256": self.sha256,
            "source_state": self.source_state,
            "record_schema": self.record_schema,
            "record_digest": self.record_digest,
            "invalid_reason": self.invalid_reason,
        }

    @property
    def digest(self) -> str:
        return canonical_digest(self._payload())

    def as_dict(self) -> dict[str, Any]:
        return {**self._payload(), "source_digest": self.digest}

    @classmethod
    def from_dict(
        cls,
        value: object,
        where: str = "authority source identity",
    ) -> AuthoritySourceIdentity:
        row = exact_record(
            value,
            where,
            schema=cls.SCHEMA,
            fields=frozenset(
                {
                    "source_kind",
                    "locator",
                    "byte_count",
                    "sha256",
                    "source_state",
                    "record_schema",
                    "record_digest",
                    "invalid_reason",
                    "source_digest",
                }
            ),
        )
        candidate = cls(
            row["source_kind"],
            row["locator"],
            row["byte_count"],
            row["sha256"],
            row["source_state"],
            row["record_schema"],
            row["record_digest"],
            row["invalid_reason"],
        )
        observed = require_digest(row["source_digest"], f"{where}.source_digest")
        if observed != candidate.digest:
            raise ValueError(f"{where}.source_digest is stale; expected {candidate.digest!r}, found {observed!r}")
        return candidate


def require_source_kind(
    source: AuthoritySourceIdentity | None,
    kind: str,
    where: str,
) -> AuthoritySourceIdentity:
    if not isinstance(source, AuthoritySourceIdentity) or source.source_kind != kind:
        raise ValueError(f"{where} must be a typed {kind!r} source")
    return source


def canonical_source_rows(
    values: Iterable[AuthoritySourceIdentity],
    where: str,
    *,
    kind: str,
) -> tuple[AuthoritySourceIdentity, ...]:
    rows = tuple(values)
    for index, source in enumerate(rows):
        require_source_kind(source, kind, f"{where}[{index}]")
    locators = tuple(source.locator for source in rows)
    if len(locators) != len(set(locators)):
        raise ValueError(f"{where} contains duplicate locators")
    return tuple(sorted(rows, key=lambda source: source.locator))


def require_canonical_source_rows(
    rows: tuple[AuthoritySourceIdentity, ...],
    where: str,
    *,
    kind: str,
) -> None:
    if not isinstance(rows, tuple):
        raise ValueError(f"{where} must be a tuple")
    if rows != canonical_source_rows(rows, where, kind=kind):
        raise ValueError(f"{where} must be sorted by locator")


def derived_family_state(sources: Iterable[AuthoritySourceIdentity]) -> str:
    rows = tuple(sources)
    if not rows:
        return "absent"
    return (
        "present_valid"
        if all(source.source_state in {"record_valid", "opaque_valid"} for source in rows)
        else "present_invalid"
    )


def require_family_state(value: object, expected: str, where: str) -> None:
    if value not in FAMILY_STATES:
        raise ValueError(f"{where} must be one of {sorted(FAMILY_STATES)}")
    if value != expected:
        raise ValueError(f"{where} must be derived as {expected!r}, found {value!r}")


__all__ = [
    "AUTHORITY_SOURCE_IDENTITY_SCHEMA",
    "BUNDLE_MANIFEST_PATTERN",
    "FIXED_RECORD_SCHEMAS",
    "OPAQUE_ONLY_SOURCE_KINDS",
    "TYPED_SOURCE_KINDS",
    "VIEW_MEMBER_PATTERN",
    "AuthoritySourceIdentity",
    "canonical_digest",
    "canonical_source_rows",
    "derived_family_state",
    "exact_record",
    "require_canonical_source_rows",
    "require_digest",
    "require_family_state",
    "require_source_kind",
]
