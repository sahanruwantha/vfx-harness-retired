from __future__ import annotations

import hashlib
from pathlib import Path

from tests.unit.test_authority_capsules import _global_documents
from vfx_harness.domain.authority_capsules import compile_authority_capsules
from vfx_harness.domain.authority_head_records import canonical_json_bytes
from vfx_harness.orchestration.authority_selection_transaction import (
    AuthoritySelectionToken,
)
from vfx_harness.orchestration.authority_state_preparation import (
    prepare_authority_state_transition,
)
from vfx_harness.orchestration.authority_state_store import read_authority_state_record
from vfx_harness.orchestration.plan_pointer import PlanPointer


def test_genesis_preparation_stages_complete_intent_without_selecting_it(tmp_path) -> None:
    documents = _global_documents()
    capsules = compile_authority_capsules(documents, documents)
    pointer = PlanPointer(
        revision=1,
        run_id="fixture-plan-run",
        bundle=Path("runs/fixture-plan-run/checkpoints/plans/bundles") / ("a" * 64),
        content_hash="a" * 64,
        outcome="clean_with_deferred",
        published_at="2026-09-01T00:00:00Z",
    )
    pointer_bytes = canonical_json_bytes(pointer.as_dict())
    producer = b'{"schema":"fixture.plan-gate/v1","clean":true}\n'

    prepared = prepare_authority_state_transition(
        tmp_path,
        expected_base_selection=AuthoritySelectionToken(0, None, 0, None),
        after_capsules=capsules,
        after_plan_pointer_bytes=pointer_bytes,
        after_plan_revision=1,
        after_jit_pointer_bytes=None,
        after_jit_revision=0,
        producer_payload=producer,
        producer_schema="fixture.plan-gate/v1",
        producer_digest=hashlib.sha256(producer).hexdigest(),
        prepared_at="2026-09-01T00:01:00Z",
    )

    assert prepared.intent.transition_revision == 1
    assert prepared.intent.proposal.predecessor_head_ref is None
    assert prepared.intent.proposal.effects == ()
    assert prepared.intent.proposal.after_selection_token.plan_revision == 1
    assert not (tmp_path / "plans" / "current.json").exists()
    assert not (tmp_path / "state" / "authority-state" / "pending.json").exists()
    stored, _object = read_authority_state_record(
        tmp_path,
        locator=prepared.intent_ref.locator,
        sha256=prepared.intent_ref.sha256,
    )
    assert stored == prepared.intent.as_dict()
