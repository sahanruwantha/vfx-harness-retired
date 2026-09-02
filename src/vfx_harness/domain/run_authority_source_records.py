"""Closed classification of authority-source bytes into typed identities (HIR-0172).

One pure function serves both the live capturer and the independent evaluator, so a
source's identity is a function of its kind, locator, and exact bytes alone.  Typed
kinds decode strictly and resolve their semantic digest through a closed registry:
authority-state records digest through their own typed parsers, plan and view pointers
validate through the domain parsers and digest as canonical JSON, and any other JSON
object with a schema field digests as canonical JSON.  Every other kind is an exact
opaque byte stream.  Failures map to the closed ``INVALID_REASONS`` vocabulary and never
raise for evidence problems.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Any

from vfx_harness.domain.authority_head_records import (
    AuthorityHeadRecordError,
    parse_jit_view_pointer,
    parse_plan_pointer,
)
from vfx_harness.domain.authority_state_commit_records import (
    AuthorityStateCoordinatorHead,
    AuthorityStatePendingPointer,
    AuthorityStateTransitionCommit,
    AuthorityStateTransitionEvaluation,
)
from vfx_harness.domain.authority_state_record_primitives import (
    AuthorityStateRecordError,
    AuthorityStateRecordRef,
)
from vfx_harness.domain.authority_state_transition_records import (
    AuthorityStateTransitionIntent,
    AuthorityStateTransitionProposal,
)
from vfx_harness.domain.run_authority_source_identity import (
    FIXED_RECORD_SCHEMAS,
    SCHEMA_PATTERN,
    SOURCE_KINDS,
    TYPED_SOURCE_KINDS,
    AuthoritySourceIdentity,
    canonical_digest,
)

OPAQUE_SOURCE_KINDS = frozenset(SOURCE_KINDS - TYPED_SOURCE_KINDS)

SEMANTIC_RECORD_CLASSES: Mapping[str, Any] = MappingProxyType(
    {
        cls.SCHEMA: cls
        for cls in (
            AuthorityStateTransitionIntent,
            AuthorityStateTransitionProposal,
            AuthorityStateTransitionCommit,
            AuthorityStateTransitionEvaluation,
            AuthorityStateCoordinatorHead,
            AuthorityStatePendingPointer,
            AuthorityStateRecordRef,
        )
    }
)
RECORD_VALIDATORS: Mapping[str, Callable[[object], object]] = MappingProxyType(
    {
        FIXED_RECORD_SCHEMAS["plan_pointer"]: parse_plan_pointer,
        FIXED_RECORD_SCHEMAS["effective_view_pointer"]: parse_jit_view_pointer,
    }
)


class _DuplicateJsonKey(ValueError):
    """A JSON object repeated a key; two readers could disagree on its value."""


class _NonFiniteJson(ValueError):
    """A JSON number was NaN or infinite, which canonical JSON cannot encode."""


class StaleRecordDigest(ValueError):
    """A typed record's own digest field disagrees with its recomputed identity."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateJsonKey(key)
        value[key] = item
    return value


def _reject_non_finite(constant: str) -> None:
    raise _NonFiniteJson(constant)


def decode_strict_json_object(payload: bytes) -> tuple[dict[str, Any] | None, str | None]:
    """Decode one exact UTF-8 JSON object, or name the closed reason it is not one."""

    try:
        text = bytes(payload).decode("utf-8")
    except UnicodeDecodeError:
        return None, "non_utf8"
    try:
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite,
        )
    except _DuplicateJsonKey:
        return None, "duplicate_json_key"
    except ValueError:
        return None, "malformed_json"
    if not isinstance(value, dict):
        return None, "unsupported_schema"
    return value, None


def semantic_record_digest(schema: str, record: Mapping[str, Any]) -> str:
    """Return the closed semantic digest of one decoded typed record.

    Raises :class:`StaleRecordDigest` when the record carries a digest field that does
    not match its recomputed identity, and :class:`ValueError` for any other typed
    parse failure.
    """

    record_class = SEMANTIC_RECORD_CLASSES.get(schema)
    if record_class is not None:
        try:
            return record_class.parse(record, "authority source record").digest
        except AuthorityStateRecordError as exc:
            if " is stale" in str(exc):
                raise StaleRecordDigest(str(exc)) from exc
            raise ValueError(str(exc)) from exc
    validator = RECORD_VALIDATORS.get(schema)
    if validator is not None:
        try:
            validator(record)
        except AuthorityHeadRecordError as exc:
            raise ValueError(str(exc)) from exc
    return canonical_digest(record)


def classify_authority_source(
    source_kind: str,
    locator: str,
    payload: bytes,
) -> AuthoritySourceIdentity:
    """Classify exact source bytes as one typed, opaque, or raw-invalid identity."""

    if source_kind not in SOURCE_KINDS:
        raise ValueError(f"unknown authority source kind {source_kind!r}")
    if not isinstance(payload, (bytes, bytearray)):
        raise ValueError("authority source classification requires exact bytes")
    payload = bytes(payload)
    byte_count = len(payload)
    sha256 = hashlib.sha256(payload).hexdigest()
    if source_kind in OPAQUE_SOURCE_KINDS:
        return AuthoritySourceIdentity.opaque_valid(
            source_kind=source_kind,
            locator=locator,
            byte_count=byte_count,
            sha256=sha256,
        )

    def invalid(reason: str) -> AuthoritySourceIdentity:
        return AuthoritySourceIdentity.raw_invalid(
            source_kind=source_kind,
            locator=locator,
            byte_count=byte_count,
            sha256=sha256,
            invalid_reason=reason,
        )

    record, reason = decode_strict_json_object(payload)
    if record is None:
        assert reason is not None
        return invalid(reason)
    schema = record.get("schema")
    if not isinstance(schema, str) or SCHEMA_PATTERN.fullmatch(schema) is None:
        return invalid("unsupported_schema")
    expected_schema = FIXED_RECORD_SCHEMAS.get(source_kind)
    if expected_schema is not None and schema != expected_schema:
        return invalid("unsupported_schema")
    try:
        record_digest = semantic_record_digest(schema, record)
    except StaleRecordDigest:
        return invalid("stale_record_digest")
    except ValueError:
        return invalid("fields_mismatch")
    return AuthoritySourceIdentity.valid(
        source_kind=source_kind,
        locator=locator,
        byte_count=byte_count,
        sha256=sha256,
        record_schema=schema,
        record_digest=record_digest,
    )


__all__ = [
    "OPAQUE_SOURCE_KINDS",
    "RECORD_VALIDATORS",
    "SEMANTIC_RECORD_CLASSES",
    "StaleRecordDigest",
    "classify_authority_source",
    "decode_strict_json_object",
    "semantic_record_digest",
]
