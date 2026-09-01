from __future__ import annotations

import copy
import hashlib

import pytest

from vfx_harness.domain.authority_head_records import AuthoritySelectionTokenProjection
from vfx_harness.domain.authority_state_records import (
    AUTHORITY_STATE_HEAD_SCHEMA,
    AUTHORITY_STATE_TRANSITION_INTENT_SCHEMA,
    AuthorityStateRecordError,
    AuthorityStateRecordRef,
    AuthorityStateRecoveryResult,
)


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _ref(label: str, schema: str) -> AuthorityStateRecordRef:
    return AuthorityStateRecordRef.mint(
        locator=f"state/authority-state/objects/{_digest(label)}.json",
        sha256=_digest(f"{label}-bytes"),
        record_schema=schema,
        record_digest=_digest(f"{label}-record"),
    )


def _result() -> AuthorityStateRecoveryResult:
    return AuthorityStateRecoveryResult(
        disposition="recovered",
        transaction_id="transition-2",
        transition_revision=2,
        intent_ref=_ref("intent", AUTHORITY_STATE_TRANSITION_INTENT_SCHEMA),
        coordinator_head_ref=_ref("head", AUTHORITY_STATE_HEAD_SCHEMA),
        selection_token=AuthoritySelectionTokenProjection(
            plan_revision=1,
            plan_pointer_sha256=_digest("plan"),
            jit_revision=2,
            jit_pointer_sha256=_digest("jit"),
        ),
        state_member_ids=("camera", "form"),
    )


def test_authority_state_recovery_result_round_trips_strict_evidence() -> None:
    result = _result()

    assert AuthorityStateRecoveryResult.parse(result.as_dict()) == result
    assert result.as_dict()["result_digest"] == result.digest


@pytest.mark.parametrize(
    ("field", "replacement", "match"),
    [
        ("disposition", "rolled_back", "disposition"),
        ("transition_revision", 0, "positive integer"),
        ("state_member_ids", ["form", "camera"], "sorted and unique"),
    ],
)
def test_authority_state_recovery_result_rejects_tampering(
    field: str,
    replacement: object,
    match: str,
) -> None:
    damaged = copy.deepcopy(_result().as_dict())
    damaged[field] = replacement

    with pytest.raises(AuthorityStateRecordError, match=match):
        AuthorityStateRecoveryResult.parse(damaged)


def test_authority_state_recovery_result_rejects_unknown_fields() -> None:
    damaged = {**_result().as_dict(), "unexpected": True}

    with pytest.raises(AuthorityStateRecordError, match="unexpected"):
        AuthorityStateRecoveryResult.parse(damaged)
