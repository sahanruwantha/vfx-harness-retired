"""One closed classification serves capture and evaluation (HIR-0172)."""

from __future__ import annotations

import hashlib
import json

import pytest

from vfx_harness.domain.authority_head_records import canonical_json_bytes
from vfx_harness.domain.authority_state_record_primitives import AuthorityStateRecordRef
from vfx_harness.domain.run_authority_source_identity import canonical_digest
from vfx_harness.domain.run_authority_source_records import (
    OPAQUE_SOURCE_KINDS,
    classify_authority_source,
    decode_strict_json_object,
)


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _plan_pointer() -> dict:
    content_hash = _digest("bundle")
    return {
        "schema": "vfx-harness.plan-pointer/v2",
        "revision": 1,
        "run_id": "run-001",
        "bundle": f"runs/run-001/checkpoints/plans/bundles/{content_hash}",
        "content_hash": content_hash,
        "outcome": "clean",
        "published_at": "2026-09-01T00:00:00+00:00",
    }


_OPAQUE_LOCATORS = {
    "plan_amendments": "plan_amendments.jsonl",
    "plan_resolutions": "state/plan-resolutions.jsonl",
    "judgment_debts": "state/judgment-debts.jsonl",
    "judgment_payment_attempts": "state/judgment-payment-attempts.jsonl",
    "accepted_ledger": "shot.json",
    "accepted_member": "build/01_camera.py",
    "plan_bundle_member": f"runs/run-001/checkpoints/plans/bundles/{_digest('bundle')}/layers.json",
    "effective_view_member": f"state/jit-layers/views/{_digest('view')}/layers.json",
    "durable_state_member": "state/work-units/1.json",
}


def test_opaque_kinds_bind_exact_bytes_and_never_invent_record_identity() -> None:
    assert set(_OPAQUE_LOCATORS) == set(OPAQUE_SOURCE_KINDS)
    payload = b"{not json at all"
    for kind, locator in _OPAQUE_LOCATORS.items():
        identity = classify_authority_source(kind, locator, payload)
        assert identity.source_state == "opaque_valid"
        assert (identity.byte_count, identity.sha256) == (len(payload), _sha(payload))
        assert identity.record_schema is None and identity.record_digest is None
        assert classify_authority_source(kind, locator, payload) == identity


def test_plan_pointer_classifies_through_the_domain_parser() -> None:
    pointer = _plan_pointer()
    payload = (json.dumps(pointer, indent=2, sort_keys=True) + "\n").encode()
    identity = classify_authority_source("plan_pointer", "plans/current.json", payload)
    assert identity.source_state == "record_valid"
    assert identity.record_schema == "vfx-harness.plan-pointer/v2"
    assert identity.record_digest == canonical_digest(pointer)

    other_schema = {**pointer, "schema": "vfx-harness.jit-layer-view/v2"}
    wrong = classify_authority_source(
        "plan_pointer",
        "plans/current.json",
        json.dumps(other_schema).encode(),
    )
    assert (wrong.source_state, wrong.invalid_reason) == ("raw_invalid", "unsupported_schema")

    incomplete = {key: value for key, value in pointer.items() if key != "content_hash"}
    fields = classify_authority_source(
        "plan_pointer",
        "plans/current.json",
        json.dumps(incomplete).encode(),
    )
    assert (fields.source_state, fields.invalid_reason) == ("raw_invalid", "fields_mismatch")


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (b"\xff\xfe", "non_utf8"),
        (b"{", "malformed_json"),
        (b'{"schema": "vfx-harness.plan-pointer/v2", "schema": "x/v1"}', "duplicate_json_key"),
        (b"[]", "unsupported_schema"),
        (b'{"schema": "NOT A SCHEMA"}', "unsupported_schema"),
        (b'{"revision": 1}', "unsupported_schema"),
        (b'{"schema": "vfx-harness.plan-pointer/v2", "value": NaN}', "malformed_json"),
    ],
)
def test_invalid_typed_sources_keep_raw_bytes_with_a_closed_reason(payload: bytes, reason: str) -> None:
    identity = classify_authority_source("plan_pointer", "plans/current.json", payload)
    assert identity.source_state == "raw_invalid"
    assert identity.invalid_reason == reason
    assert (identity.byte_count, identity.sha256) == (len(payload), _sha(payload))
    assert identity.record_schema is None and identity.record_digest is None


def test_authority_state_records_digest_through_their_typed_parsers() -> None:
    reference = AuthorityStateRecordRef.mint(
        locator=f"state/authority-state/objects/{_digest('object')}/record.json",
        sha256=_digest("object"),
        record_schema="vfx-harness.authority-state-head/v1",
        record_digest=_digest("head"),
    )
    payload = canonical_json_bytes(reference.as_dict())
    locator = f"state/authority-state/objects/{_sha(payload)}/record.json"
    identity = classify_authority_source("durable_state_record", locator, payload)
    assert identity.source_state == "record_valid"
    assert identity.record_schema == reference.SCHEMA
    assert identity.record_digest == reference.digest

    stale = dict(reference.as_dict())
    stale["ref_digest"] = _digest("tampered")
    stale_payload = canonical_json_bytes(stale)
    stale_identity = classify_authority_source(
        "durable_state_record",
        f"state/authority-state/objects/{_sha(stale_payload)}/record.json",
        stale_payload,
    )
    assert (stale_identity.source_state, stale_identity.invalid_reason) == ("raw_invalid", "stale_record_digest")

    truncated = {key: value for key, value in reference.as_dict().items() if key != "locator"}
    truncated_payload = canonical_json_bytes(truncated)
    truncated_identity = classify_authority_source(
        "durable_state_record",
        f"state/authority-state/objects/{_sha(truncated_payload)}/record.json",
        truncated_payload,
    )
    assert (truncated_identity.source_state, truncated_identity.invalid_reason) == ("raw_invalid", "fields_mismatch")


def test_other_schema_bearing_objects_digest_as_canonical_json() -> None:
    document = {"schema": "vfx-harness.work-unit-state/v1", "units": {"camera": {"status": "passed"}}}
    payload = (json.dumps(document, indent=2) + "\n").encode()
    locator = f"state/authority-state/objects/{_sha(payload)}/record.json"
    identity = classify_authority_source("durable_state_record", locator, payload)
    assert identity.source_state == "record_valid"
    assert identity.record_schema == "vfx-harness.work-unit-state/v1"
    assert identity.record_digest == canonical_digest(document)


def test_current_pointer_is_the_head_record_not_a_reference() -> None:
    reference = AuthorityStateRecordRef.mint(
        locator=f"state/authority-state/objects/{_digest('object')}/record.json",
        sha256=_digest("object"),
        record_schema="vfx-harness.authority-state-head/v1",
        record_digest=_digest("head"),
    )
    identity = classify_authority_source(
        "durable_state_pointer",
        "state/authority-state/current.json",
        canonical_json_bytes(reference.as_dict()),
    )
    assert (identity.source_state, identity.invalid_reason) == ("raw_invalid", "unsupported_schema")
    head_shaped = classify_authority_source(
        "durable_state_pointer",
        "state/authority-state/current.json",
        b'{"schema": "vfx-harness.authority-state-head/v1", "revision": 1}',
    )
    assert (head_shaped.source_state, head_shaped.invalid_reason) == ("raw_invalid", "fields_mismatch")


def test_strict_decoder_names_the_closed_reasons() -> None:
    assert decode_strict_json_object(b'{"schema": "x/v1"}') == ({"schema": "x/v1"}, None)
    assert decode_strict_json_object(b"[1]") == (None, "unsupported_schema")
    assert decode_strict_json_object(b'{"a": 1, "a": 2}') == (None, "duplicate_json_key")
    with pytest.raises(ValueError, match="unknown authority source kind"):
        classify_authority_source("mystery", "x", b"")
