"""Immutable prior-running-status evidence for HIR-0172 reconciliation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

from vfx_harness.domain.prior_running_status import (
    PRIOR_RUNNING_STATUS_EVIDENCE_SCHEMA,
    RUN_STATUS_V2_SCHEMA,
    PriorRunningStatusEvidence,
    running_status_snapshot_locator,
)
from vfx_harness.domain.run_owner_claims import (
    RUN_OWNER_FENCE_IMPLEMENTATION,
    RunOwnerClaim,
)
from vfx_harness.domain.run_status import RunStatusV2
from vfx_harness.domain.stop_envelope_primitives import canonical_digest

_RUN = "prior-running-status-001"
_T0 = "2026-09-01T00:00:00+00:00"
_T1 = "2026-09-01T00:01:00+00:00"


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _owner(*, run_id: str = _RUN) -> RunOwnerClaim:
    return RunOwnerClaim(
        run_id=run_id,
        command="plan",
        invocation_digest=_digest("invocation"),
        owner_kind="direct",
        owner_id=_digest("owner"),
        process_id=1234,
        process_start_token=_digest("process-start"),
        shot_root_device=51,
        shot_root_inode=5101,
        runs_directory_device=52,
        runs_directory_inode=5201,
        run_root_device=53,
        run_root_inode=5301,
        owner_directory_device=54,
        owner_directory_inode=5401,
        claim_device=55,
        claim_inode=5501,
        fence_locator="owner/fence.lock",
        fence_implementation=RUN_OWNER_FENCE_IMPLEMENTATION,
        fence_device=51,
        fence_inode=99,
        descriptor_inheritable=False,
        manifest_locator="manifest.json",
        manifest_sha256=_digest("manifest"),
        claimed_at=_T0,
    )


def _status_value(
    owner: RunOwnerClaim,
    *,
    owner_locator: str = "owner/claim.json",
) -> dict:
    return RunStatusV2.mint(
        run_id=owner.run_id,
        state="running",
        updated_at=_T1,
        record_locator=owner_locator,
        selected_record=owner,
        detail="root invocation owns the run",
    ).as_dict()


def _status_bytes(owner: RunOwnerClaim) -> bytes:
    return (json.dumps(_status_value(owner), indent=2, sort_keys=True) + "\n").encode()


def _rehash_status(value: dict) -> bytes:
    value["status_digest"] = canonical_digest({key: item for key, item in value.items() if key != "status_digest"})
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def test_mint_round_trip_binds_exact_bytes_running_matrix_and_owner() -> None:
    owner = _owner()
    source = _status_bytes(owner)
    evidence = PriorRunningStatusEvidence.mint(
        status_bytes=source,
        owner=owner,
        captured_at=_T1,
    )

    source_sha256 = hashlib.sha256(source).hexdigest()
    assert evidence.status_snapshot_ref.locator == running_status_snapshot_locator(source_sha256)
    assert evidence.status_snapshot_ref.sha256 == source_sha256
    assert evidence.status_snapshot_ref.record_schema == RUN_STATUS_V2_SCHEMA
    assert evidence.status_snapshot_ref.record_digest == evidence.status_digest
    assert evidence.owner_claim_locator == "owner/claim.json"
    assert evidence.owner_claim_digest == owner.digest
    assert evidence.as_dict()["schema"] == PRIOR_RUNNING_STATUS_EVIDENCE_SCHEMA
    assert (
        PriorRunningStatusEvidence.from_dict(
            evidence.as_dict(),
            status_bytes=source,
            owner=owner,
        )
        == evidence
    )


def test_exact_byte_substitution_is_rejected_even_when_json_semantics_match() -> None:
    owner = _owner()
    source = _status_bytes(owner)
    evidence = PriorRunningStatusEvidence.mint(
        status_bytes=source,
        owner=owner,
        captured_at=_T1,
    )
    semantically_equal_different_bytes = json.dumps(
        _status_value(owner),
        separators=(",", ":"),
        sort_keys=True,
    ).encode()

    with pytest.raises(ValueError, match="exact source reference"):
        evidence.require_source_bytes(semantically_equal_different_bytes)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("schema", "vfx-harness.run-status/v1", "schema must be"),
        ("run_id", "another-run", "names another run"),
        ("state", "failed", "state must be 'running'"),
        ("owner_claim", "owner/other.json", "canonical owner claim"),
        ("owner_claim_digest", "f" * 64, "exact owner claim"),
    ],
)
def test_mint_rejects_wrong_status_or_owner_identity(
    field: str,
    value: str,
    message: str,
) -> None:
    owner = _owner()
    status = _status_value(owner)
    status[field] = value
    source = _rehash_status(status)

    with pytest.raises(ValueError, match=message):
        PriorRunningStatusEvidence.mint(
            status_bytes=source,
            owner=owner,
            captured_at=_T1,
        )


def test_stale_status_digest_and_terminal_selector_are_rejected() -> None:
    owner = _owner()
    stale = _status_value(owner)
    stale["updated_at"] = "2026-09-01T00:02:00+00:00"
    stale_source = (json.dumps(stale, indent=2, sort_keys=True) + "\n").encode()
    with pytest.raises(ValueError, match="status_digest is stale"):
        PriorRunningStatusEvidence.mint(
            status_bytes=stale_source,
            owner=owner,
            captured_at=_T1,
        )

    hybrid = _status_value(owner)
    hybrid["summary"] = "reports/summary.json"
    hybrid["summary_digest"] = _digest("summary")
    hybrid_source = _rehash_status(hybrid)
    with pytest.raises(ValueError, match="permits only owner_claim authority"):
        PriorRunningStatusEvidence.mint(
            status_bytes=hybrid_source,
            owner=owner,
            captured_at=_T1,
        )


def test_snapshot_reference_must_be_content_addressed_and_create_only_compatible() -> None:
    owner = _owner()
    source = _status_bytes(owner)
    evidence = PriorRunningStatusEvidence.mint(
        status_bytes=source,
        owner=owner,
        captured_at=_T1,
    )

    with pytest.raises(ValueError, match="content-addressed locator"):
        replace(
            evidence,
            status_snapshot_ref=replace(
                evidence.status_snapshot_ref,
                locator="status.json",
            ),
        )
    with pytest.raises(ValueError, match=r"must be 'status\.json'"):
        replace(evidence, source_status_locator="reports/status.json")


def test_evidence_wire_is_closed_and_digest_protected() -> None:
    owner = _owner()
    source = _status_bytes(owner)
    evidence = PriorRunningStatusEvidence.mint(
        status_bytes=source,
        owner=owner,
        captured_at=_T1,
    )

    extra = evidence.as_dict()
    extra["retryable"] = False
    with pytest.raises(ValueError, match="fields mismatch"):
        PriorRunningStatusEvidence.from_dict(extra)

    stale = evidence.as_dict()
    stale["captured_at"] = "2026-09-01T00:03:00+00:00"
    with pytest.raises(ValueError, match="evidence_digest is stale"):
        PriorRunningStatusEvidence.from_dict(stale)


def test_status_source_rejects_duplicate_keys_nonfinite_values_and_mutable_bytes() -> None:
    owner = _owner()
    source = _status_bytes(owner)
    duplicate = source.replace(
        b'{\n  "detail"',
        b'{\n  "run_id": "prior-running-status-001",\n  "detail"',
        1,
    )
    with pytest.raises(ValueError, match="duplicate JSON key 'run_id'"):
        PriorRunningStatusEvidence.mint(
            status_bytes=duplicate,
            owner=owner,
            captured_at=_T1,
        )

    nonfinite_value = _status_value(owner)
    nonfinite_value["detail"] = float("nan")
    nonfinite = json.dumps(nonfinite_value, sort_keys=True).encode()
    with pytest.raises(ValueError, match="finite UTF-8 JSON object"):
        PriorRunningStatusEvidence.mint(
            status_bytes=nonfinite,
            owner=owner,
            captured_at=_T1,
        )

    with pytest.raises(ValueError, match="exact immutable bytes"):
        PriorRunningStatusEvidence.mint(
            status_bytes=bytearray(source),  # type: ignore[arg-type]
            owner=owner,
            captured_at=_T1,
        )


@pytest.mark.parametrize("encoding", ["utf-16", "utf-32"])
def test_status_source_rejects_non_utf8_json_bytes(encoding: str) -> None:
    owner = _owner()
    source = json.dumps(
        _status_value(owner),
        indent=2,
        sort_keys=True,
    ).encode(encoding)

    with pytest.raises(ValueError, match="finite UTF-8 JSON object"):
        PriorRunningStatusEvidence.mint(
            status_bytes=source,
            owner=owner,
            captured_at=_T1,
        )


def test_optional_owner_verification_rejects_substituted_claim() -> None:
    owner = _owner()
    source = _status_bytes(owner)
    evidence = PriorRunningStatusEvidence.mint(
        status_bytes=source,
        owner=owner,
        captured_at=_T1,
    )
    substituted = replace(owner, claimed_at=_T1)

    with pytest.raises(ValueError, match="exact owner claim"):
        evidence.require_source_bytes(source, owner=substituted)


def test_running_status_chronology_is_bound_between_owner_claim_and_capture() -> None:
    owner = _owner()
    before_owner = _status_value(owner)
    before_owner["updated_at"] = "2026-08-31T23:59:00+00:00"
    with pytest.raises(ValueError, match="owner/prior running status"):
        PriorRunningStatusEvidence.mint(
            status_bytes=_rehash_status(before_owner),
            owner=owner,
            captured_at=_T1,
        )

    with pytest.raises(ValueError, match="prior running status/capture"):
        PriorRunningStatusEvidence.mint(
            status_bytes=_status_bytes(owner),
            owner=owner,
            captured_at=_T0,
        )
