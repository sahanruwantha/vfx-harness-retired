"""Native gaps publish durable VFX evidence, not SDK state or accepted decisions."""

from __future__ import annotations

import json
from contextlib import nullcontext

import flynn_agents_sdk as flynn
import pytest

from tests.contract.test_flynn_materialization_tools import bound as bound
from tests.contract.test_flynn_planning_knowledge import execute
from tests.unit.test_vocabulary_gap_closure import FORM_STATEMENT, _fixture_root, _validated_form
from vfx_harness.domain.vocabulary_gaps import recorded_vocabulary_gap_ids, vocabulary_gaps_path
from vfx_harness.observability import prepared_publication
from vfx_harness.orchestration import vocabulary_gap_publication as publication


def gap(**overrides):
    return {"requirement_id": "R-final-lock", "claim": "frames 239 and 240 are unchanged",
            "attempted": [{"kind": "node_count", "why_it_cannot_certify": "Counts cannot certify image equality."}],
            "note": "Requires an explicit provisional decision.", **overrides}


def test_native_gap_retry_returns_one_stored_record_without_committing_state(bound):
    first, second = execute(bound, [("escalate_vocabulary_gap", gap())] * 2)
    rows = [json.loads(result.data_json) for result in (first, second)]
    assert rows[0]["created"] and not rows[1]["created"]
    assert rows[0]["record"] == rows[1]["record"]
    assert rows[0]["ledger_sha256"] == rows[1]["ledger_sha256"]
    assert not rows[0]["plan_authority_changed"]
    assert recorded_vocabulary_gap_ids(bound[0].shot) == {"R-final-lock": ("VG-001",)}
    with flynn.SQLiteRun.open(bound[0].checkpoints / "knowledge.sqlite") as journal:
        assert journal.records()["commits"] == []
        assert journal.remaining() == {"inference": 8, "tool": 8, "external": 8}


@pytest.mark.parametrize("arguments", [
    gap(requirement_id="R-foreign"), gap(claim="A different claim"),
    gap(attempted=[]), gap(attempted=[{"kind": "invented", "why_it_cannot_certify": "Unknown"}]),
    gap(attempted=[{"kind": "node_count", "why_it_cannot_certify": " "}]),
    gap(attempted=gap()["attempted"] * 2), gap(extra=True),
])
def test_invalid_gap_is_rejected_before_execution(bound, arguments):
    with pytest.raises(flynn.ProposalRejected):
        execute(bound, [("escalate_vocabulary_gap", arguments)])
    assert not vocabulary_gaps_path(bound[0].shot).exists()
    with flynn.SQLiteRun.open(bound[0].checkpoints / "knowledge.sqlite") as journal:
        assert journal.remaining()["external"] == journal.remaining()["tool"] == 10


def test_unit_session_does_not_receive_gap_publication(bound):
    with pytest.raises(flynn.ContractError, match="not granted"):
        execute(bound, [("escalate_vocabulary_gap", gap())], layer_id="1", unit_id="lock")
    assert not vocabulary_gaps_path(bound[0].shot).exists()


def test_owner_loss_after_preparation_discards_only_staged_gap(bound, monkeypatch):
    live = True
    original = prepared_publication.prepare_file_update

    def prepare(*args, **kwargs):
        nonlocal live
        result = original(*args, **kwargs)
        live = False
        return result

    def check():
        if not live:
            raise ValueError("owner released")

    monkeypatch.setattr(prepared_publication, "prepare_file_update", prepare)
    with pytest.raises(ValueError, match="owner released"):
        execute(bound, [("escalate_vocabulary_gap", gap())], check=check)
    assert not vocabulary_gaps_path(bound[0].shot).exists()
    assert not list(vocabulary_gaps_path(bound[0].shot).parent.glob("*.prepared.*"))


def test_concurrent_gap_write_is_not_overwritten(bound, monkeypatch):
    original = prepared_publication.prepare_file_update
    path = vocabulary_gaps_path(bound[0].shot)
    other = {"schema": "vfx-harness.vocabulary-gap/v1", "id": "VG-900", "run_id": "other", **gap()}
    payload = json.dumps(other).encode() + b"\n"

    def prepare(*args, **kwargs):
        result = original(*args, **kwargs)
        path.write_bytes(payload)
        return result

    monkeypatch.setattr(prepared_publication, "prepare_file_update", prepare)
    with pytest.raises(prepared_publication.FilePublicationConflict):
        execute(bound, [("escalate_vocabulary_gap", gap())])
    assert path.read_bytes() == payload


def test_malformed_ledger_refuses_publication_without_repairing_bytes(bound):
    path = vocabulary_gaps_path(bound[0].shot)
    path.parent.mkdir(parents=True)
    path.write_bytes(b'{"schema":')
    with pytest.raises(ValueError):
        execute(bound, [("escalate_vocabulary_gap", gap())])
    assert path.read_bytes() == b'{"schema":'


def test_published_gap_is_consumed_by_real_materialization_validator(tmp_path):
    root = _fixture_root(tmp_path)
    binding = {"requirement_id": "R-form", "decision": {
        "statement": FORM_STATEMENT, "decision_strength": "approved_start",
    }}
    with pytest.raises(ValueError, match="escalate_vocabulary_gap"):
        _validated_form(root, binding)
    observed = publication.record_gap(
        shot=root, run_id="gap-gate-test", arguments=gap(requirement_id="R-form", claim=FORM_STATEMENT),
        statements={"R-form": FORM_STATEMENT}, check_current=lambda: None,
        authority_binding="test-current-requirement", commit_guard=nullcontext,
    )
    assert observed["record"]["id"] == "VG-001"
    _validated_form(root, binding)
