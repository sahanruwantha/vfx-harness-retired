"""Exclusive work-unit attempt claims close readiness/spend races."""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from threading import Event, Thread, current_thread
from types import SimpleNamespace

import pytest

from tests.architecture.test_staged_architecture import _unit
from tests.unit_attempt_fixtures import (
    executed_replay_input,
    freeze_unit,
    publish_passed_evaluation,
)
from vfx_harness.domain.work_units import canonical_unit_script_path
from vfx_harness.orchestration import (
    authority_selection_heads,
    hypothesis_falsification_projection,
    unit_completion_state,
    unit_evaluation_receipts,
    unit_state,
    unit_state_claims,
)
from vfx_harness.orchestration.authority_selection_transaction import (
    AuthoritySelectionToken,
)
from vfx_harness.orchestration.hypothesis_falsification_state import (
    load_state_backed_falsification,
    reconcile_falsification_projection,
)
from vfx_harness.orchestration.ledger import ledger_lock

_PLAN_A = "a" * 64
_PLAN_B = "b" * 64
_PLAN_C = "c" * 64


def _token(*, revision: int = 0) -> AuthoritySelectionToken:
    return AuthoritySelectionToken(
        plan_revision=revision,
        plan_pointer_sha256=None if revision == 0 else str(revision) * 64,
        jit_revision=0,
        jit_pointer_sha256=None,
    )


@pytest.mark.parametrize("run_id", ["../x", "/absolute", "latest", ".", "a/b", " space"])
def test_attempt_claim_refuses_run_ids_receipt_storage_cannot_represent(
    run_id: str,
) -> None:
    unit = _unit("form")
    with pytest.raises(ValueError, match="must match"):
        unit_state_claims.UnitAttemptClaim.mint(
            attempt_revision=1,
            run_id=run_id,
            layer_id="1",
            unit_id=unit.id,
            unit_digest=unit_state.unit_digest(unit),
            plan_hash=_PLAN_A,
            selection_token=_token().to_dict(),
            phase="planning",
            at="2026-01-01T00:00:00+00:00",
        )


def _claim_planning(
    folder,
    units,
    unit_id: str,
    *,
    plan_hash: str = _PLAN_A,
    token: AuthoritySelectionToken | None = None,
    eligible_passed: set[str] | None = None,
):
    return unit_state_claims.claim_ready_unit_for_planning(
        folder,
        "1",
        unit_id,
        units,
        expected_plan_hash=plan_hash,
        eligible_passed=eligible_passed,
        run_id="run-claim",
        selection_token=token or _token(),
        reason="dependency closure proved ready",
    )


def _promote(folder, units, claim, *, eligible_passed=None):
    return unit_state_claims.claim_ready_unit_for_build(
        folder,
        "1",
        claim.unit_id,
        units,
        claim,
        expected_plan_hash=claim.plan_hash,
        eligible_passed=eligible_passed,
        run_id=claim.run_id,
        selection_token=_token(),
        reason="gated plan ready for builder execution",
    )


def _pass_unclaimed(folder, unit, units):
    planning = _claim_planning(folder, units, unit.id)
    building = _promote(folder, units, planning)
    freeze_unit(folder, "1", unit, building, selection_token=_token())
    unit_state.transition(
        folder,
        "1",
        unit.id,
        "evaluating",
        reason="fixture",
        attempt=building,
        selection_token=_token(),
    )
    publish_passed_evaluation(folder, "1", unit, building)
    return unit_state_claims.complete_unit_attempt(
        folder,
        "1",
        unit.id,
        units,
        building,
        expected_plan_hash=building.plan_hash,
        selection_token=_token(),
        reason="fixture",
        evidence=["fixture:pass"],
    )


def _evaluation_payload(layer_id, unit, claim, *, passed: bool = True):
    evidence = [
        {
            "id": binding.id,
            "source": (
                "interface_contract"
                if binding.kind == "scene_contract"
                else binding.kind
            ),
            "pass": True,
            "authoritative": True,
        }
        for unit_claim in unit.evaluation.claims
        if unit_claim.required
        for binding in unit_claim.evidence
    ]
    verdicts = [
        (
            (point.frame, point.ref),
            {
                "pass": passed,
                "mean": 5.0 if passed else 1.0,
                "issues": [] if passed else ["evaluator rejected candidate"],
                "evidence": evidence,
                "evidence_kind": "executable_only",
                "decided_by": "unit_executable_evidence",
                "judge_conflict": False,
                "contract_gap": False,
            },
        )
        for point in unit.evaluation.judges
    ]
    rounds = [
        {
            "kind": "canonical",
            "render": "",
            "pass": passed,
            "evidence": evidence,
            "decided_by": "unit_executable_evidence",
            "judge_conflict": False,
            "contract_gap": False,
        }
        for _point in unit.evaluation.judges
    ]
    return verdicts, {
        "run_id": claim.run_id,
        "attempt": claim.attempt_revision,
        "status": "passed",
        "script": canonical_unit_script_path(str(layer_id), unit.id),
        "unit_hash": claim.unit_digest,
        "artifact_unit_hash": claim.unit_digest,
        "best": {"render": None},
        "rounds": rounds,
    }


def _persist_evaluation_ledger(folder, milestone_id: str, slot) -> None:
    (folder / "shot.json").write_text(
        json.dumps(
            {
                "shot": "unit-evaluation-fixture",
                "milestones": {milestone_id: slot},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _replay_inputs(folder, layer_id: str, unit):
    script_path = canonical_unit_script_path(layer_id, unit.id)
    return (executed_replay_input(folder, script_path),)


def test_load_rejects_passed_state_without_completion_receipt(tmp_path) -> None:
    unit = _unit("form")
    unit_state.initialize(tmp_path, "1", (unit,), plan_hash=_PLAN_A)
    state_path = tmp_path / "state" / "work-units" / "layer_1.json"
    value = json.loads(state_path.read_text(encoding="utf-8"))
    value["units"][unit.id]["status"] = "passed"
    state_path.write_text(json.dumps(value) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="requires its completion receipt"):
        unit_state.load(tmp_path, "1")


def test_completion_receipt_guards_exact_checkpoint_script_and_selection(tmp_path) -> None:
    unit = _unit("form")
    units = (unit,)
    unit_state.initialize(tmp_path, "1", units, plan_hash=_PLAN_A)

    receipt = _pass_unclaimed(tmp_path, unit, units)

    slot = unit_state.load(tmp_path, "1")["units"][unit.id]
    assert slot["status"] == "passed"
    assert slot["completion_receipt"] == receipt.as_dict()
    with unit_state_claims.completed_unit_attempt_guard(
        tmp_path,
        "1",
        unit.id,
        units,
        receipt,
        expected_plan_hash=_PLAN_A,
        selection_token=_token(),
    ) as current:
        assert current == receipt

    (tmp_path / receipt.script_path).write_text("# changed after acceptance\n")
    with (
        pytest.raises(ValueError, match="script changed after acceptance"),
        unit_state_claims.completed_unit_attempt_guard(
            tmp_path,
            "1",
            unit.id,
            units,
            receipt,
            expected_plan_hash=_PLAN_A,
            selection_token=_token(),
        ),
    ):
        pass

    (tmp_path / receipt.script_path).write_text(
        "# deterministic fixture unit 1.form\npass\n",
        encoding="utf-8",
    )
    evaluation_path = tmp_path / receipt.evaluation_receipt_locator
    evaluation_path.write_text("{}\n", encoding="utf-8")
    with (
        pytest.raises(ValueError, match="evaluation receipt"),
        unit_state_claims.completed_unit_attempt_guard(
            tmp_path,
            "1",
            unit.id,
            units,
            receipt,
            expected_plan_hash=_PLAN_A,
            selection_token=_token(),
        ),
    ):
        pass


def test_completed_receipt_hashing_finishes_before_selection_state_guard(
    tmp_path,
    monkeypatch,
) -> None:
    unit = _unit("form")
    units = (unit,)
    unit_state.initialize(tmp_path, "1", units, plan_hash=_PLAN_A)
    receipt = _pass_unclaimed(tmp_path, unit, units)
    original_lock = unit_completion_state.authority_selection_lock
    original_read = unit_completion_state.plan_bundle_integrity.read_real_file
    held = False

    @contextmanager
    def observed_lock(*args, **kwargs):
        nonlocal held
        with original_lock(*args, **kwargs) as value:
            held = True
            try:
                yield value
            finally:
                held = False

    def observed_read(*args, **kwargs):
        assert not held, "completion source bytes were read under selection/state locks"
        return original_read(*args, **kwargs)

    monkeypatch.setattr(unit_completion_state, "authority_selection_lock", observed_lock)
    monkeypatch.setattr(
        unit_completion_state.plan_bundle_integrity,
        "read_real_file",
        observed_read,
    )
    with unit_state_claims.completed_unit_attempt_guard(
        tmp_path,
        "1",
        unit.id,
        units,
        receipt,
        expected_plan_hash=_PLAN_A,
        selection_token=_token(),
    ):
        pass


def test_completion_refuses_declaration_names_without_evaluator_receipt(tmp_path) -> None:
    unit = _unit("form")
    units = (unit,)
    unit_state.initialize(tmp_path, "1", units, plan_hash=_PLAN_A)
    building = _promote(tmp_path, units, _claim_planning(tmp_path, units, unit.id))
    freeze_unit(tmp_path, "1", unit, building, selection_token=_token())
    unit_state.transition(
        tmp_path,
        "1",
        unit.id,
        "evaluating",
        reason="fixture evidence ready",
        attempt=building,
        selection_token=_token(),
    )

    with pytest.raises(
        unit_state_claims.UnitAttemptConflict,
        match="independent canonical evaluation is not authoritative",
    ):
        unit_state_claims.complete_unit_attempt(
            tmp_path,
            "1",
            unit.id,
            units,
            building,
            expected_plan_hash=_PLAN_A,
            selection_token=_token(),
            reason="invented evidence must not complete",
            evidence=["fixture:invented"],
        )
    slot = unit_state.load(tmp_path, "1")["units"][unit.id]
    assert slot["status"] == "evaluating"
    assert slot["active_attempt"] == building.as_dict()
    assert "completion_receipt" not in slot


def test_failing_evaluator_cannot_publish_correct_declared_evidence(tmp_path) -> None:
    unit = _unit("form")
    units = (unit,)
    unit_state.initialize(tmp_path, "1", units, plan_hash=_PLAN_A)
    building = _promote(tmp_path, units, _claim_planning(tmp_path, units, unit.id))
    freeze_unit(tmp_path, "1", unit, building, selection_token=_token())
    verdicts, ledger_slot = _evaluation_payload("1", unit, building, passed=False)
    _persist_evaluation_ledger(tmp_path, "1", ledger_slot)

    with pytest.raises(ValueError, match="non-passing canonical verdict"):
        unit_evaluation_receipts.prepare_unit_evaluation_receipt(
            tmp_path,
            "1",
            unit,
            building,
            result="passed",
            canonical_verdicts=verdicts,
            ledger_slot=ledger_slot,
            replay_inputs=_replay_inputs(tmp_path, "1", unit),
        )


def test_evaluator_receipt_refuses_same_id_from_wrong_evidence_family(tmp_path) -> None:
    unit = _unit("form")
    units = (unit,)
    unit_state.initialize(tmp_path, "1", units, plan_hash=_PLAN_A)
    building = _promote(tmp_path, units, _claim_planning(tmp_path, units, unit.id))
    freeze_unit(tmp_path, "1", unit, building, selection_token=_token())
    verdicts, ledger_slot = _evaluation_payload("1", unit, building)
    for _point, verdict in verdicts:
        verdict["evidence"][0]["source"] = "image_contract"
    ledger_slot["rounds"][0]["evidence"][0]["source"] = "image_contract"
    _persist_evaluation_ledger(tmp_path, "1", ledger_slot)

    with pytest.raises(ValueError, match="passing required evidence"):
        unit_evaluation_receipts.prepare_unit_evaluation_receipt(
            tmp_path,
            "1",
            unit,
            building,
            result="passed",
            canonical_verdicts=verdicts,
            ledger_slot=ledger_slot,
            replay_inputs=_replay_inputs(tmp_path, "1", unit),
        )


def test_evaluation_commit_refuses_source_mutation_after_prepare(tmp_path) -> None:
    unit = _unit("form")
    units = (unit,)
    unit_state.initialize(tmp_path, "1", units, plan_hash=_PLAN_A)
    building = _promote(tmp_path, units, _claim_planning(tmp_path, units, unit.id))
    freeze_unit(tmp_path, "1", unit, building, selection_token=_token())
    verdicts, ledger_slot = _evaluation_payload("1", unit, building)
    _persist_evaluation_ledger(tmp_path, "1", ledger_slot)
    prepared = unit_evaluation_receipts.prepare_unit_evaluation_receipt(
        tmp_path,
        "1",
        unit,
        building,
        result="passed",
        canonical_verdicts=verdicts,
        ledger_slot=ledger_slot,
        replay_inputs=_replay_inputs(tmp_path, "1", unit),
    )
    try:
        (tmp_path / prepared.receipt.script_path).write_text(
            "# changed between prepare and authority commit\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="causal input changed before publication"):
            unit_evaluation_receipts.commit_unit_evaluation_receipt(prepared)
        assert not prepared.destination.exists()
    finally:
        unit_evaluation_receipts.discard_prepared_unit_evaluation_receipt(prepared)


def test_evaluation_prepare_refuses_stale_local_slot_over_durable_failure(
    tmp_path,
) -> None:
    unit = _unit("form")
    units = (unit,)
    unit_state.initialize(tmp_path, "1", units, plan_hash=_PLAN_A)
    building = _promote(tmp_path, units, _claim_planning(tmp_path, units, unit.id))
    freeze_unit(tmp_path, "1", unit, building, selection_token=_token())
    verdicts, stale_local_slot = _evaluation_payload("1", unit, building)
    durable_slot = json.loads(json.dumps(stale_local_slot))
    durable_slot["status"] = "failed"
    _persist_evaluation_ledger(tmp_path, "1", durable_slot)

    with pytest.raises(
        ValueError,
        match="caller ledger slot does not match descriptor-read durable milestone",
    ):
        unit_evaluation_receipts.prepare_unit_evaluation_receipt(
            tmp_path,
            "1",
            unit,
            building,
            result="passed",
            canonical_verdicts=verdicts,
            ledger_slot=stale_local_slot,
            replay_inputs=_replay_inputs(tmp_path, "1", unit),
        )


def test_evaluation_commit_refuses_durable_ledger_mutation_after_prepare(
    tmp_path,
) -> None:
    unit = _unit("form")
    units = (unit,)
    unit_state.initialize(tmp_path, "1", units, plan_hash=_PLAN_A)
    building = _promote(tmp_path, units, _claim_planning(tmp_path, units, unit.id))
    freeze_unit(tmp_path, "1", unit, building, selection_token=_token())
    verdicts, ledger_slot = _evaluation_payload("1", unit, building)
    _persist_evaluation_ledger(tmp_path, "1", ledger_slot)
    prepared = unit_evaluation_receipts.prepare_unit_evaluation_receipt(
        tmp_path,
        "1",
        unit,
        building,
        result="passed",
        canonical_verdicts=verdicts,
        ledger_slot=ledger_slot,
        replay_inputs=_replay_inputs(tmp_path, "1", unit),
    )
    try:
        durable_bytes = (tmp_path / "shot.json").read_bytes()
        assert prepared.ledger_binding.path == tmp_path / "shot.json"
        assert prepared.ledger_sha256 == hashlib.sha256(durable_bytes).hexdigest()
        mutated = json.loads((tmp_path / "shot.json").read_text(encoding="utf-8"))
        mutated["milestones"]["1"]["status"] = "failed"
        (tmp_path / "shot.json").write_text(
            json.dumps(mutated, indent=2) + "\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="causal input changed before publication"):
            unit_evaluation_receipts.commit_unit_evaluation_receipt(prepared)
        assert not prepared.destination.exists()
    finally:
        unit_evaluation_receipts.discard_prepared_unit_evaluation_receipt(prepared)


def test_evaluation_commit_fails_closed_when_durable_ledger_is_busy(tmp_path) -> None:
    unit = _unit("form")
    units = (unit,)
    unit_state.initialize(tmp_path, "1", units, plan_hash=_PLAN_A)
    building = _promote(tmp_path, units, _claim_planning(tmp_path, units, unit.id))
    freeze_unit(tmp_path, "1", unit, building, selection_token=_token())
    verdicts, ledger_slot = _evaluation_payload("1", unit, building)
    _persist_evaluation_ledger(tmp_path, "1", ledger_slot)
    prepared = unit_evaluation_receipts.prepare_unit_evaluation_receipt(
        tmp_path,
        "1",
        unit,
        building,
        result="passed",
        canonical_verdicts=verdicts,
        ledger_slot=ledger_slot,
        replay_inputs=_replay_inputs(tmp_path, "1", unit),
    )
    try:
        with (
            ledger_lock(tmp_path / "shot.json", exclusive=True),
            pytest.raises(ValueError, match="durable ledger is busy"),
        ):
            unit_evaluation_receipts.commit_unit_evaluation_receipt(prepared)
        assert not prepared.destination.exists()
    finally:
        unit_evaluation_receipts.discard_prepared_unit_evaluation_receipt(prepared)


def test_replan_archives_receipt_so_old_debt_authority_cannot_revive(tmp_path) -> None:
    unit = _unit("form")
    units = (unit,)
    unit_state.initialize(tmp_path, "1", units, plan_hash=_PLAN_A)
    receipt = _pass_unclaimed(tmp_path, unit, units)

    unit_state.apply_replan(
        tmp_path,
        "1",
        units,
        units,
        old_plan_hash=_PLAN_A,
        new_plan_hash=_PLAN_B,
        owner="fixture",
        trigger="authority generation changed",
        evidence=["fixture:replan"],
    )

    slot = unit_state.load(tmp_path, "1")["units"][unit.id]
    assert slot["status"] == "retryable"
    assert "completion_receipt" not in slot
    assert slot["completion_receipt_history"][-1]["receipt"] == receipt.as_dict()
    assert slot["completion_receipt_history"][-1]["disposition"] == "revoked"
    assert unit_state.digest_matched_passed(
        unit_state.load(tmp_path, "1"), units
    ) == set()


def test_plan_change_receipt_revocation_blocks_a_digest_identical_successor(
    tmp_path,
) -> None:
    producer = _unit("form")
    successor = _unit("finish", depends_on=[producer.id])
    units = (producer, successor)
    unit_state.initialize(tmp_path, "1", units, plan_hash=_PLAN_A)
    _pass_unclaimed(tmp_path, producer, units)

    unit_state.apply_replan(
        tmp_path,
        "1",
        units,
        units,
        old_plan_hash=_PLAN_A,
        new_plan_hash=_PLAN_B,
        owner="fixture",
        trigger="authority generation changed",
        evidence=["fixture:replan"],
    )

    state = unit_state.load(tmp_path, "1")
    assert state["units"][producer.id]["unit_hash"] == unit_state.unit_digest(producer)
    assert state["units"][producer.id]["status"] == "retryable"
    assert unit_state.digest_matched_passed(state, units) == set()
    assert [unit.id for unit in unit_state.ready_from_durable_state(
        tmp_path,
        "1",
        units,
    )] == [producer.id]


def _preserve_accepted_falsification(folder, unit, units):
    return unit_state.record_hypothesis_falsification(
        folder,
        "1",
        unit,
        units,
        bundle_hash="d" * 64,
        unit_plan_hash="e" * 64,
        candidate_hash="f" * 64,
        settings_hash="1" * 64,
        contract_ids=["form.contract"],
        observations=[{"pass": False}],
        decisions=[],
        conflict={
            "kind": "contract",
            "required_authority": "amend form authority",
            "roles": ["form"],
            "controls": [],
        },
        evidence=["run:evidence/failure.json"],
        preserve_accepted_source=True,
        selection_token=_token(),
    )


def test_same_plan_falsification_reopen_archives_passed_completion_receipt(
    tmp_path,
) -> None:
    unit = _unit("form")
    units = (unit,)
    unit_state.initialize(tmp_path, "1", units, plan_hash=_PLAN_A)
    receipt = _pass_unclaimed(tmp_path, unit, units)
    finding = _preserve_accepted_falsification(tmp_path, unit, units)

    unit_state.apply_replan(
        tmp_path,
        "1",
        units,
        units,
        old_plan_hash=_PLAN_A,
        new_plan_hash=_PLAN_A,
        owner="fixture",
        trigger="consume composed falsification",
        evidence=["fixture:falsification"],
        falsification_id=finding["record_id"],
        falsification_payload=finding,
        reopen={unit.id},
    )

    state = unit_state.load(tmp_path, "1")
    assert state["units"][unit.id]["status"] == "pending"
    assert "completion_receipt" not in state["units"][unit.id]
    retired = state["superseded"][-1]
    assert "completion_receipt" not in retired
    assert retired["completion_receipt_history"][-1]["receipt"] == receipt.as_dict()
    assert retired["completion_receipt_history"][-1]["disposition"] == "superseded"


def test_concurrent_falsification_replan_consumers_commit_exactly_once(tmp_path) -> None:
    unit = _unit("form")
    units = (unit,)
    unit_state.initialize(tmp_path, "1", units, plan_hash=_PLAN_A)
    _pass_unclaimed(tmp_path, unit, units)
    finding = _preserve_accepted_falsification(tmp_path, unit, units)
    results: list[dict] = []
    errors: list[BaseException] = []

    stale_payload = json.loads(json.dumps(finding))
    stale_payload["evidence"] = ["run:evidence/substituted.json"]
    with pytest.raises(ValueError, match="falsification"):
        unit_state.apply_replan(
            tmp_path,
            "1",
            units,
            units,
            old_plan_hash=_PLAN_A,
            new_plan_hash=_PLAN_A,
            owner="fixture",
            trigger="stale preload must not consume",
            evidence=["fixture:falsification"],
            falsification_id=finding["record_id"],
            falsification_payload=stale_payload,
            reopen={unit.id},
        )

    def operation():
        return unit_state.apply_replan(
            tmp_path,
            "1",
            units,
            units,
            old_plan_hash=_PLAN_A,
            new_plan_hash=_PLAN_A,
            owner="fixture",
            trigger="consume composed falsification",
            evidence=["fixture:falsification"],
            falsification_id=finding["record_id"],
            falsification_payload=finding,
            reopen={unit.id},
        )
    contenders = [
        Thread(target=_run, kwargs={"operation": operation, "results": results, "errors": errors})
        for _index in range(2)
    ]
    for contender in contenders:
        contender.start()
    for contender in contenders:
        contender.join(timeout=5)

    assert all(not contender.is_alive() for contender in contenders)
    assert len(results) == 1
    assert len(errors) == 1
    assert "unconsumed record" in str(errors[0])
    state = unit_state.load(tmp_path, "1")
    assert [
        row.get("falsification_id") for row in state["replans"]
    ] == [finding["record_id"]]


def _run(operation, *, results, errors) -> None:
    try:
        results.append(operation())
    except BaseException as exc:  # pragma: no cover - asserted by caller
        errors.append(exc)


@pytest.mark.parametrize("source", ["pending", "blocked", "retryable"])
def test_planning_claim_owns_every_reviewable_ready_source(tmp_path, source: str) -> None:
    unit = _unit("form")
    units = (unit,)
    unit_state.initialize(tmp_path, "1", units, plan_hash=_PLAN_A)
    if source == "blocked":
        unit_state.transition(tmp_path, "1", unit.id, "blocked", reason="fixture")
    elif source == "retryable":
        prior = _claim_planning(tmp_path, units, unit.id)
        unit_state_claims.release_unit_attempt(
            tmp_path,
            "1",
            unit.id,
            units,
            prior,
            expected_plan_hash=_PLAN_A,
            selection_token=_token(),
            reason="fixture reviewed retry",
            evidence=["fixture:review"],
        )

    claim = _claim_planning(tmp_path, units, unit.id)

    slot = unit_state.load(tmp_path, "1")["units"][unit.id]
    assert slot["status"] == "planning"
    assert slot["attempt_revision"] == (2 if source == "retryable" else 1)
    assert slot["active_attempt"] == claim.as_dict()
    assert claim.phase == "planning"
    assert claim.unit_digest == unit_state.unit_digest(unit)
    assert claim.plan_hash == _PLAN_A


def test_planning_state_is_never_implicitly_reclaimed(tmp_path) -> None:
    unit = _unit("form")
    units = (unit,)
    unit_state.initialize(tmp_path, "1", units, plan_hash=_PLAN_A)
    path = unit_state._path(tmp_path, "1")
    value = json.loads(path.read_text(encoding="utf-8"))
    value["units"][unit.id]["status"] = "planning"
    path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(
        unit_state_claims.UnitAttemptConflict,
        match=r"cannot claim.*planning",
    ):
        _claim_planning(tmp_path, units, unit.id)


def test_build_promotion_requires_exact_claim_and_revalidates_readiness(tmp_path) -> None:
    producer = _unit("producer")
    consumer = _unit("consumer", depends_on=[producer.id])
    units = (producer, consumer)
    unit_state.initialize(tmp_path, "1", units, plan_hash=_PLAN_A)
    _pass_unclaimed(tmp_path, producer, units)
    planning = _claim_planning(
        tmp_path,
        units,
        consumer.id,
        eligible_passed={producer.id},
    )

    with pytest.raises(unit_state_claims.UnitAttemptConflict, match="claim changed"):
        wrong = planning.promoted(phase="building", at=planning.updated_at)
        _promote(tmp_path, units, wrong, eligible_passed={producer.id})

    building = _promote(
        tmp_path,
        units,
        planning,
        eligible_passed={producer.id},
    )
    assert building.claim_id == planning.claim_id
    assert building.attempt_revision == planning.attempt_revision
    assert building.phase == "building"
    assert unit_state.load(tmp_path, "1")["units"][consumer.id]["status"] == "building"


def test_build_promotion_rejects_exact_plan_unit_and_selection_mismatches(tmp_path) -> None:
    unit = _unit("form")
    units = (unit,)
    unit_state.initialize(tmp_path, "1", units, plan_hash=_PLAN_A)
    planning = _claim_planning(tmp_path, units, unit.id)
    changed_units = (_unit("form", proposition_suffix=" and amended"),)

    with pytest.raises(ValueError, match="plan identity changed"):
        unit_state_claims.claim_ready_unit_for_build(
            tmp_path,
            "1",
            unit.id,
            units,
            planning,
            expected_plan_hash=_PLAN_B,
            eligible_passed=None,
            run_id=planning.run_id,
            selection_token=_token(),
            reason="wrong plan",
        )
    with pytest.raises(ValueError, match="hashes do not match"):
        unit_state_claims.claim_ready_unit_for_build(
            tmp_path,
            "1",
            unit.id,
            changed_units,
            planning,
            expected_plan_hash=_PLAN_A,
            eligible_passed=None,
            run_id=planning.run_id,
            selection_token=_token(),
            reason="wrong unit generation",
        )
    with pytest.raises(ValueError, match="authority selection changed"):
        unit_state_claims.claim_ready_unit_for_build(
            tmp_path,
            "1",
            unit.id,
            units,
            planning,
            expected_plan_hash=_PLAN_A,
            eligible_passed=None,
            run_id=planning.run_id,
            selection_token=_token(revision=2),
            reason="wrong authority token",
        )
    assert unit_state.load(tmp_path, "1")["units"][unit.id]["active_attempt"] == planning.as_dict()


def test_claim_fails_when_eligible_producer_set_does_not_seal_dependency(tmp_path) -> None:
    producer = _unit("producer")
    consumer = _unit("consumer", depends_on=[producer.id])
    units = (producer, consumer)
    unit_state.initialize(tmp_path, "1", units, plan_hash=_PLAN_A)
    _pass_unclaimed(tmp_path, producer, units)

    with pytest.raises(unit_state_claims.UnitAttemptConflict, match="no longer dependency-ready"):
        _claim_planning(tmp_path, units, consumer.id, eligible_passed=set())


def test_exact_claim_is_checked_before_same_status_and_checkpoint_freeze(tmp_path) -> None:
    unit = _unit("form")
    units = (unit,)
    unit_state.initialize(tmp_path, "1", units, plan_hash=_PLAN_A)
    planning = _claim_planning(tmp_path, units, unit.id)
    building = _promote(tmp_path, units, planning)

    with pytest.raises(ValueError, match="claim changed"):
        unit_state.transition(
            tmp_path,
            "1",
            unit.id,
            "building",
            reason="stale idempotent caller",
            attempt=planning,
            selection_token=_token(),
        )
    with pytest.raises(ValueError, match="claim changed"):
        unit_state.freeze_checkpoint(
            tmp_path,
            "1",
            unit,
            active_contract_ids=(),
            candidate_hash="candidate",
            settings_hash="settings",
            script_hash="script",
            input_hash="input",
            attempt=planning,
            selection_token=_token(),
        )

    unit_state.freeze_checkpoint(
        tmp_path,
        "1",
        unit,
        active_contract_ids=(),
        candidate_hash="candidate",
        settings_hash="settings",
        script_hash="script",
        input_hash="input",
        attempt=building,
        selection_token=_token(),
    )


def test_reviewed_release_is_exact_and_evidence_bound(tmp_path) -> None:
    unit = _unit("form")
    units = (unit,)
    unit_state.initialize(tmp_path, "1", units, plan_hash=_PLAN_A)
    planning = _claim_planning(tmp_path, units, unit.id)

    state = unit_state_claims.release_unit_attempt(
        tmp_path,
        "1",
        unit.id,
        units,
        planning,
        expected_plan_hash=_PLAN_A,
        selection_token=_token(),
        reason="review proved prior planner process exited",
        evidence=["run:prior/terminal", "operator:review-1"],
    )

    slot = state["units"][unit.id]
    assert slot["status"] == "retryable"
    assert "active_attempt" not in slot
    archive = slot["attempt_history"][-1]
    assert archive["disposition"] == "released"
    assert archive["evidence"] == ["run:prior/terminal", "operator:review-1"]
    assert slot["history"][-1]["metadata"]["evidence"] == archive["evidence"]
    with pytest.raises(unit_state_claims.UnitAttemptConflict, match=r"stale|not actively owned"):
        unit_state_claims.release_unit_attempt(
            tmp_path,
            "1",
            unit.id,
            units,
            planning,
            expected_plan_hash=_PLAN_A,
            selection_token=_token(),
            reason="stale cleanup",
            evidence=["operator:review-1"],
        )

    replacement = _claim_planning(tmp_path, units, unit.id)
    assert replacement.attempt_revision == 2
    assert replacement.claim_id != planning.claim_id


@pytest.mark.parametrize(
    "source",
    ["failed", "planning", "building", "frozen", "evaluating", "repairing"],
)
def test_reviewed_unclaimed_retry_is_one_exact_evidence_bound_transaction(
    tmp_path,
    source: str,
) -> None:
    unit = _unit("form")
    units = (unit,)
    unit_state.initialize(tmp_path, "1", units, plan_hash=_PLAN_A)
    path = unit_state._path(tmp_path, "1")
    value = json.loads(path.read_text(encoding="utf-8"))
    value["units"][unit.id]["status"] = source
    path.write_text(json.dumps(value), encoding="utf-8")

    state = unit_state_claims.release_unclaimed_unit_for_retry(
        tmp_path,
        "1",
        unit.id,
        units,
        expected_plan_hash=_PLAN_A,
        selection_token=_token(),
        reason="review proved historical owner is gone",
        evidence=["run:historical/terminal"],
    )

    event = state["units"][unit.id]["history"][-1]
    assert state["units"][unit.id]["status"] == "retryable"
    assert event["from"] == source
    assert event["metadata"]["reviewed_unclaimed_retry"] is True
    assert event["metadata"]["evidence"] == ["run:historical/terminal"]
    assert event["metadata"]["selection_token"] == _token().to_dict()


def test_unclaimed_retry_refuses_active_attempt_and_wrong_generation(tmp_path) -> None:
    unit = _unit("form")
    units = (unit,)
    unit_state.initialize(tmp_path, "1", units, plan_hash=_PLAN_A)
    _claim_planning(tmp_path, units, unit.id)

    with pytest.raises(unit_state_claims.UnitAttemptConflict, match="active attempt"):
        unit_state_claims.release_unclaimed_unit_for_retry(
            tmp_path,
            "1",
            unit.id,
            units,
            expected_plan_hash=_PLAN_A,
            selection_token=_token(),
            reason="incorrect recovery path",
            evidence=["operator:review"],
        )
    with pytest.raises(ValueError, match="plan identity changed"):
        unit_state_claims.release_unclaimed_unit_for_retry(
            tmp_path,
            "1",
            unit.id,
            units,
            expected_plan_hash=_PLAN_B,
            selection_token=_token(),
            reason="wrong generation",
            evidence=["operator:review"],
        )
    with pytest.raises(
        unit_state_claims.UnitAttemptConflict,
        match="typed authority selection token",
    ):
        unit_state_claims.release_unclaimed_unit_for_retry(
            tmp_path,
            "1",
            unit.id,
            units,
            expected_plan_hash=_PLAN_A,
            selection_token={},  # type: ignore[arg-type]
            reason="untyped authority",
            evidence=["operator:review"],
        )


def test_competing_claimers_publish_exactly_one_attempt(tmp_path, monkeypatch) -> None:
    unit = _unit("form")
    units = (unit,)
    unit_state.initialize(tmp_path, "1", units, plan_hash=_PLAN_A)
    first_at_write = Event()
    release_first = Event()
    second_finished = Event()
    results = []
    errors: list[BaseException] = []
    original_write = unit_state_claims._write

    def blocked_write(path, value) -> None:
        if current_thread().name == "claim-first":
            first_at_write.set()
            assert release_first.wait(2)
        original_write(path, value)

    monkeypatch.setattr(unit_state_claims, "_write", blocked_write)
    first = Thread(
        name="claim-first",
        target=_run,
        kwargs={
            "operation": lambda: _claim_planning(tmp_path, units, unit.id),
            "results": results,
            "errors": errors,
        },
    )

    def second_operation():
        try:
            return _claim_planning(tmp_path, units, unit.id)
        finally:
            second_finished.set()

    second = Thread(
        name="claim-second",
        target=_run,
        kwargs={"operation": second_operation, "results": results, "errors": errors},
    )
    first.start()
    assert first_at_write.wait(2)
    second.start()
    assert not second_finished.wait(0.1)
    release_first.set()
    first.join(2)
    second.join(2)

    assert len(results) == 1
    assert len(errors) == 1
    assert "cannot claim" in str(errors[0])
    slot = unit_state.load(tmp_path, "1")["units"][unit.id]
    assert slot["attempt_revision"] == 1
    assert len([row for row in slot["history"] if row["to"] == "planning"]) == 1


def test_producer_invalidation_wins_before_dependent_claim(tmp_path, monkeypatch) -> None:
    producer = _unit("producer")
    consumer = _unit("consumer", depends_on=[producer.id])
    units = (producer, consumer)
    unit_state.initialize(tmp_path, "1", units, plan_hash=_PLAN_A)
    _pass_unclaimed(tmp_path, producer, units)
    invalidation_at_write = Event()
    release_invalidation = Event()
    claim_finished = Event()
    invalidation_results = []
    invalidation_errors: list[BaseException] = []
    claim_results = []
    claim_errors: list[BaseException] = []
    original_write = unit_state._write

    def blocked_write(path, value) -> None:
        if current_thread().name == "invalidate-first":
            invalidation_at_write.set()
            assert release_invalidation.wait(2)
        original_write(path, value)

    monkeypatch.setattr(unit_state, "_write", blocked_write)
    invalidation = Thread(
        name="invalidate-first",
        target=_run,
        kwargs={
            "operation": lambda: unit_state.invalidate_checkpoint(
                tmp_path,
                "1",
                producer.id,
                units,
                reason="producer proof revoked",
                evidence=["run:failure"],
            ),
            "results": invalidation_results,
            "errors": invalidation_errors,
        },
    )

    def claim_operation():
        try:
            return _claim_planning(
                tmp_path,
                units,
                consumer.id,
                eligible_passed={producer.id},
            )
        finally:
            claim_finished.set()

    claim = Thread(
        name="dependent-claim",
        target=_run,
        kwargs={"operation": claim_operation, "results": claim_results, "errors": claim_errors},
    )
    invalidation.start()
    assert invalidation_at_write.wait(2)
    claim.start()
    assert not claim_finished.wait(0.1)
    release_invalidation.set()
    invalidation.join(2)
    claim.join(2)

    assert invalidation_errors == []
    assert len(claim_errors) == 1
    assert "no longer dependency-ready" in str(claim_errors[0])
    slot = unit_state.load(tmp_path, "1")["units"][consumer.id]
    assert slot["status"] == "blocked"
    assert "active_attempt" not in slot


def test_same_id_replan_winning_lock_prevents_stale_unclaimed_retry(
    tmp_path,
    monkeypatch,
) -> None:
    old_unit = _unit("form")
    new_unit = _unit("form", proposition_suffix=" and amended")
    old_units = (old_unit,)
    new_units = (new_unit,)
    unit_state.initialize(tmp_path, "1", old_units, plan_hash=_PLAN_A)
    path = unit_state._path(tmp_path, "1")
    value = json.loads(path.read_text(encoding="utf-8"))
    value["units"][old_unit.id]["status"] = "planning"
    path.write_text(json.dumps(value), encoding="utf-8")
    replan_at_write = Event()
    release_replan = Event()
    retry_finished = Event()
    replan_results = []
    replan_errors: list[BaseException] = []
    retry_results = []
    retry_errors: list[BaseException] = []
    original_write = unit_state._write

    def blocked_write(path, value) -> None:
        if current_thread().name == "replan-first":
            replan_at_write.set()
            assert release_replan.wait(2)
        original_write(path, value)

    monkeypatch.setattr(unit_state, "_write", blocked_write)
    replan = Thread(
        name="replan-first",
        target=_run,
        kwargs={
            "operation": lambda: unit_state.apply_replan(
                tmp_path,
                "1",
                old_units,
                new_units,
                old_plan_hash=_PLAN_A,
                new_plan_hash=_PLAN_B,
                owner="test",
                trigger="same-id unit authority changed",
                evidence=["plan:new-generation"],
            ),
            "results": replan_results,
            "errors": replan_errors,
        },
    )

    def retry_operation():
        try:
            return unit_state_claims.release_unclaimed_unit_for_retry(
                tmp_path,
                "1",
                old_unit.id,
                old_units,
                expected_plan_hash=_PLAN_A,
                selection_token=_token(),
                reason="stale reviewed retry",
                evidence=["operator:old-review"],
            )
        finally:
            retry_finished.set()

    retry = Thread(
        name="stale-retry",
        target=_run,
        kwargs={"operation": retry_operation, "results": retry_results, "errors": retry_errors},
    )
    replan.start()
    assert replan_at_write.wait(2)
    retry.start()
    assert not retry_finished.wait(0.1)
    release_replan.set()
    replan.join(2)
    retry.join(2)

    assert replan_errors == []
    assert len(retry_errors) == 1
    state = unit_state.load(tmp_path, "1")
    assert state["plan_hash"] == _PLAN_B
    assert state["units"][new_unit.id]["status"] == "pending"
    assert all(
        event.get("reason") != "stale reviewed retry"
        for event in state["units"][new_unit.id]["history"]
    )


def test_invalidation_after_claim_revokes_it_before_stale_promotion(
    tmp_path,
    monkeypatch,
) -> None:
    producer = _unit("producer")
    consumer = _unit("consumer", depends_on=[producer.id])
    units = (producer, consumer)
    unit_state.initialize(tmp_path, "1", units, plan_hash=_PLAN_A)
    _pass_unclaimed(tmp_path, producer, units)
    planning = _claim_planning(
        tmp_path,
        units,
        consumer.id,
        eligible_passed={producer.id},
    )
    invalidation_at_write = Event()
    release_invalidation = Event()
    promotion_finished = Event()
    invalidation_results = []
    invalidation_errors: list[BaseException] = []
    promotion_results = []
    promotion_errors: list[BaseException] = []
    original_write = unit_state._write

    def blocked_write(path, value) -> None:
        if current_thread().name == "invalidate-after-claim":
            invalidation_at_write.set()
            assert release_invalidation.wait(2)
        original_write(path, value)

    monkeypatch.setattr(unit_state, "_write", blocked_write)
    invalidation = Thread(
        name="invalidate-after-claim",
        target=_run,
        kwargs={
            "operation": lambda: unit_state.invalidate_checkpoint(
                tmp_path,
                "1",
                producer.id,
                units,
                reason="producer evidence revoked",
                evidence=["run:producer-failure"],
            ),
            "results": invalidation_results,
            "errors": invalidation_errors,
        },
    )

    def promote_operation():
        try:
            return _promote(
                tmp_path,
                units,
                planning,
                eligible_passed={producer.id},
            )
        finally:
            promotion_finished.set()

    promotion = Thread(
        name="stale-promotion",
        target=_run,
        kwargs={
            "operation": promote_operation,
            "results": promotion_results,
            "errors": promotion_errors,
        },
    )
    invalidation.start()
    assert invalidation_at_write.wait(2)
    promotion.start()
    assert not promotion_finished.wait(0.1)
    release_invalidation.set()
    invalidation.join(2)
    promotion.join(2)

    assert invalidation_errors == []
    assert len(promotion_errors) == 1
    state = unit_state.load(tmp_path, "1")
    slot = state["units"][consumer.id]
    assert slot["status"] == "blocked"
    assert "active_attempt" not in slot
    assert slot["attempt_history"][-1]["claim"] == planning.as_dict()
    assert slot["attempt_history"][-1]["disposition"] == "revoked"
    assert all(event["to"] != "building" for event in slot["history"])


def test_replan_revokes_even_digest_preserved_claim_and_reopens_retryable(tmp_path) -> None:
    unit = _unit("form")
    units = (unit,)
    unit_state.initialize(tmp_path, "1", units, plan_hash=_PLAN_A)
    claim = _claim_planning(tmp_path, units, unit.id)

    unit_state.apply_replan(
        tmp_path,
        "1",
        units,
        units,
        old_plan_hash=_PLAN_A,
        new_plan_hash=_PLAN_B,
        owner="test",
        trigger="selected authority generation changed",
        evidence=["layers.json sha256 changed"],
    )

    slot = unit_state.load(tmp_path, "1")["units"][unit.id]
    assert slot["status"] == "retryable"
    assert "active_attempt" not in slot
    assert slot["attempt_history"][-1]["claim"] == claim.as_dict()
    assert slot["attempt_history"][-1]["disposition"] == "revoked"


def test_release_from_frozen_archives_checkpoint_with_exact_claim(tmp_path) -> None:
    unit = _unit("form")
    units = (unit,)
    unit_state.initialize(tmp_path, "1", units, plan_hash=_PLAN_A)
    planning = _claim_planning(tmp_path, units, unit.id)
    building = _promote(tmp_path, units, planning)
    unit_state.freeze_checkpoint(
        tmp_path,
        "1",
        unit,
        active_contract_ids=(),
        candidate_hash="candidate",
        settings_hash="settings",
        script_hash="script",
        input_hash="input",
        attempt=building,
        selection_token=_token(),
    )

    state = unit_state_claims.release_unit_attempt(
        tmp_path,
        "1",
        unit.id,
        units,
        building,
        expected_plan_hash=_PLAN_A,
        selection_token=_token(),
        reason="review proved frozen attempt owner exited",
        evidence=["run:prior/terminal"],
    )

    slot = state["units"][unit.id]
    assert slot["status"] == "retryable"
    assert "checkpoint" not in slot
    archived = slot["attempt_checkpoint_history"][-1]
    assert archived["claim_id"] == building.claim_id
    assert archived["disposition"] == "released"
    assert archived["checkpoint"]["candidate_hash"] == "candidate"


def test_digest_preserving_replan_revocation_archives_checkpoint(tmp_path) -> None:
    unit = _unit("form")
    units = (unit,)
    unit_state.initialize(tmp_path, "1", units, plan_hash=_PLAN_A)
    planning = _claim_planning(tmp_path, units, unit.id)
    building = _promote(tmp_path, units, planning)
    unit_state.freeze_checkpoint(
        tmp_path,
        "1",
        unit,
        active_contract_ids=(),
        candidate_hash="candidate",
        settings_hash="settings",
        script_hash="script",
        input_hash="input",
        attempt=building,
        selection_token=_token(),
    )

    unit_state.apply_replan(
        tmp_path,
        "1",
        units,
        units,
        old_plan_hash=_PLAN_A,
        new_plan_hash=_PLAN_B,
        owner="test",
        trigger="selected authority generation changed",
        evidence=["layers.json sha256 changed"],
    )

    slot = unit_state.load(tmp_path, "1")["units"][unit.id]
    assert slot["status"] == "retryable"
    assert "checkpoint" not in slot
    archived = slot["attempt_checkpoint_history"][-1]
    assert archived["claim_id"] == building.claim_id
    assert archived["disposition"] == "revoked"


def test_attempt_revision_is_monotone_across_same_id_reopen(tmp_path) -> None:
    unit = _unit("form")
    units = (unit,)
    unit_state.initialize(tmp_path, "1", units, plan_hash=_PLAN_A)
    stale = _claim_planning(tmp_path, units, unit.id)

    unit_state.apply_replan(
        tmp_path,
        "1",
        units,
        units,
        old_plan_hash=_PLAN_A,
        new_plan_hash=_PLAN_B,
        owner="test",
        trigger="reviewed same-id reopen",
        evidence=["finding:reopen"],
        reopen={unit.id},
    )
    replacement = _claim_planning(
        tmp_path,
        units,
        unit.id,
        plan_hash=_PLAN_B,
    )

    assert stale.attempt_revision == 1
    assert replacement.attempt_revision == 2
    assert replacement.claim_id != stale.claim_id
    state = unit_state.load(tmp_path, "1")
    assert state["attempt_lineage"] == {unit.id: 2}
    with pytest.raises(unit_state_claims.UnitAttemptConflict, match="claim changed"):
        unit_state_claims.release_unit_attempt(
            tmp_path,
            "1",
            unit.id,
            units,
            stale,
            expected_plan_hash=_PLAN_B,
            selection_token=_token(),
            reason="stale ABA release",
            evidence=["review:stale"],
        )


def test_attempt_revision_is_monotone_across_remove_and_readd_same_id(tmp_path) -> None:
    unit = _unit("form")
    units = (unit,)
    unit_state.initialize(tmp_path, "1", units, plan_hash=_PLAN_A)
    stale = _claim_planning(tmp_path, units, unit.id)

    unit_state.apply_replan(
        tmp_path,
        "1",
        units,
        (),
        old_plan_hash=_PLAN_A,
        new_plan_hash=_PLAN_B,
        owner="test",
        trigger="unit removed from authority",
        evidence=["plan:remove"],
    )
    unit_state.initialize(tmp_path, "1", units, plan_hash=_PLAN_C)
    replacement = _claim_planning(
        tmp_path,
        units,
        unit.id,
        plan_hash=_PLAN_C,
    )

    assert replacement.attempt_revision == 2
    assert replacement.claim_id != stale.claim_id
    state = unit_state.load(tmp_path, "1")
    assert state["attempt_lineage"] == {unit.id: 2}
    with pytest.raises(unit_state_claims.UnitAttemptConflict, match="claim changed"):
        unit_state_claims.release_unit_attempt(
            tmp_path,
            "1",
            unit.id,
            units,
            stale,
            expected_plan_hash=_PLAN_C,
            selection_token=_token(),
            reason="stale ABA release",
            evidence=["review:stale"],
        )


@pytest.mark.parametrize("status", ["planning", "building"])
def test_generic_transition_cannot_enter_spend_states(tmp_path, status: str) -> None:
    unit = _unit("form")
    units = (unit,)
    unit_state.initialize(tmp_path, "1", units, plan_hash=_PLAN_A)

    with pytest.raises(ValueError, match="generic unclaimed transition refuses"):
        unit_state.transition(tmp_path, "1", unit.id, status, reason="bypass")


@pytest.mark.parametrize("status", ["retryable", "blocked", "failed", "superseded"])
def test_generic_transition_cannot_release_or_revoke_claim(tmp_path, status: str) -> None:
    unit = _unit("form")
    units = (unit,)
    unit_state.initialize(tmp_path, "1", units, plan_hash=_PLAN_A)
    claim = _claim_planning(tmp_path, units, unit.id)

    with pytest.raises(ValueError, match="generic claimed transition"):
        unit_state.transition(
            tmp_path,
            "1",
            unit.id,
            status,
            reason="bypass",
            attempt=claim,
            selection_token=_token(),
        )


def test_generic_transition_cannot_falsify_or_pass_without_ownership(tmp_path) -> None:
    unit = _unit("form")
    units = (unit,)
    unit_state.initialize(tmp_path, "1", units, plan_hash=_PLAN_A)

    with pytest.raises(ValueError, match="generic unclaimed transition refuses"):
        unit_state.transition(
            tmp_path,
            "1",
            unit.id,
            "hypothesis_falsified",
            reason="bypass",
        )
    with pytest.raises(ValueError, match="generic unclaimed transition refuses"):
        unit_state.transition(
            tmp_path,
            "1",
            unit.id,
            "passed",
            reason="bypass",
        )


def test_typed_failure_completes_exact_attempt_and_archives_checkpoint(tmp_path) -> None:
    unit = _unit("form")
    units = (unit,)
    unit_state.initialize(tmp_path, "1", units, plan_hash=_PLAN_A)
    planning = _claim_planning(tmp_path, units, unit.id)
    building = _promote(tmp_path, units, planning)
    unit_state.freeze_checkpoint(
        tmp_path,
        "1",
        unit,
        active_contract_ids=(),
        candidate_hash="candidate",
        settings_hash="settings",
        script_hash="script",
        input_hash="input",
        attempt=building,
        selection_token=_token(),
    )

    state = unit_state_claims.fail_unit_attempt(
        tmp_path,
        "1",
        unit.id,
        units,
        building,
        expected_plan_hash=_PLAN_A,
        selection_token=_token(),
        reason="executable evidence failed",
        evidence=["run:evidence/failure.json"],
        metadata={"contract_ids": ["form.contract"]},
    )

    slot = state["units"][unit.id]
    assert slot["status"] == "failed"
    assert "active_attempt" not in slot
    assert "checkpoint" not in slot
    assert slot["attempt_history"][-1]["disposition"] == "completed"
    assert slot["attempt_checkpoint_history"][-1]["disposition"] == "revoked"
    assert slot["history"][-1]["metadata"]["evidence"] == [
        "run:evidence/failure.json"
    ]
    with pytest.raises(ValueError, match="generic unclaimed transition refuses"):
        unit_state.transition(
            tmp_path,
            "1",
            unit.id,
            "retryable",
            reason="bypass reviewed retry",
        )


def test_generic_lifecycle_cannot_bypass_checkpoint_or_typed_completion(tmp_path) -> None:
    unit = _unit("form")
    units = (unit,)
    unit_state.initialize(tmp_path, "1", units, plan_hash=_PLAN_A)
    planning = _claim_planning(tmp_path, units, unit.id)
    building = _promote(tmp_path, units, planning)

    with pytest.raises(ValueError, match="generic claimed transition"):
        unit_state.transition(
            tmp_path,
            "1",
            unit.id,
            "frozen",
            reason="bypass checkpoint",
            attempt=building,
            selection_token=_token(),
        )
    with pytest.raises(ValueError, match="generic claimed transition"):
        unit_state.transition(
            tmp_path,
            "1",
            unit.id,
            "passed",
            reason="bypass evidence",
            attempt=building,
            selection_token=_token(),
        )

    path = unit_state._path(tmp_path, "1")
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["units"][unit.id]["status"] = "evaluating"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(
        unit_state_claims.UnitAttemptConflict,
        match="exact frozen checkpoint is missing",
    ):
        unit_state_claims.complete_unit_attempt(
            tmp_path,
            "1",
            unit.id,
            units,
            building,
            expected_plan_hash=_PLAN_A,
            selection_token=_token(),
            reason="malformed historical completion",
            evidence=["fixture:pass"],
        )


def test_require_active_attempt_returns_exact_current_and_refuses_stale_after_replan(
    tmp_path,
) -> None:
    unit = _unit("form")
    units = (unit,)
    unit_state.initialize(tmp_path, "1", units, plan_hash=_PLAN_A)
    claim = _claim_planning(tmp_path, units, unit.id)

    assert unit_state_claims.require_active_unit_attempt(
        tmp_path,
        "1",
        unit.id,
        units,
        claim,
        expected_plan_hash=_PLAN_A,
        selection_token=_token(),
    ) == claim
    unit_state.apply_replan(
        tmp_path,
        "1",
        units,
        units,
        old_plan_hash=_PLAN_A,
        new_plan_hash=_PLAN_B,
        owner="test",
        trigger="authority changed",
        evidence=["plan:new"],
    )
    with pytest.raises(unit_state_claims.UnitAttemptConflict, match="plan identity changed"):
        unit_state_claims.require_active_unit_attempt(
            tmp_path,
            "1",
            unit.id,
            units,
            claim,
            expected_plan_hash=_PLAN_A,
            selection_token=_token(),
        )


def test_attempt_state_publications_refuse_head_commit_without_state_move(
    tmp_path,
    monkeypatch,
) -> None:
    unit = _unit("form")
    units = (unit,)
    unit_state.initialize(tmp_path, "1", units, plan_hash=_PLAN_A)
    planning = _claim_planning(tmp_path, units, unit.id)
    building = _promote(tmp_path, units, planning)
    monkeypatch.setattr(
        authority_selection_heads,
        "read_authority_selection_heads",
        lambda _folder: SimpleNamespace(token=_token(revision=2)),
    )

    with pytest.raises(ValueError, match="authority selection changed"):
        unit_state.transition(
            tmp_path,
            "1",
            unit.id,
            "building",
            reason="same-status stale publication",
            attempt=building,
            selection_token=_token(),
        )
    with pytest.raises(ValueError, match="authority selection changed"):
        unit_state.freeze_checkpoint(
            tmp_path,
            "1",
            unit,
            active_contract_ids=(),
            candidate_hash="candidate",
            settings_hash="settings",
            script_hash="script",
            input_hash="input",
            attempt=building,
            selection_token=_token(),
        )
    with pytest.raises(ValueError, match="authority selection changed"):
        unit_state.record_hypothesis_falsification(
            tmp_path,
            "1",
            unit,
            units,
            bundle_hash="d" * 64,
            unit_plan_hash="e" * 64,
            candidate_hash="f" * 64,
            settings_hash="1" * 64,
            contract_ids=["form.contract"],
            observations=[{"pass": False}],
            decisions=[],
            conflict={
                "kind": "contract",
                "required_authority": "amend form authority",
                "roles": ["form"],
                "controls": [],
            },
            evidence=["run:evidence/failure.json"],
            attempt=building,
            selection_token=_token(),
        )
    assert unit_state.load(tmp_path, "1")["units"][unit.id][
        "active_attempt"
    ] == building.as_dict()


def test_attempt_state_publication_requires_supplied_token_to_match_claim(tmp_path) -> None:
    unit = _unit("form")
    units = (unit,)
    unit_state.initialize(tmp_path, "1", units, plan_hash=_PLAN_A)
    claim = _claim_planning(tmp_path, units, unit.id)

    with pytest.raises(ValueError, match="authority selection changed"):
        unit_state.transition(
            tmp_path,
            "1",
            unit.id,
            "planning",
            reason="same-status wrong token",
            attempt=claim,
            selection_token=_token(revision=2),
        )


def test_active_falsification_archives_candidate_but_accepted_preservation_keeps_it(
    tmp_path,
) -> None:
    unit = _unit("form")
    units = (unit,)
    unit_state.initialize(tmp_path, "1", units, plan_hash=_PLAN_A)
    planning = _claim_planning(tmp_path, units, unit.id)
    building = _promote(tmp_path, units, planning)
    unit_state.freeze_checkpoint(
        tmp_path,
        "1",
        unit,
        active_contract_ids=(),
        candidate_hash="candidate",
        settings_hash="settings",
        script_hash="script",
        input_hash="input",
        attempt=building,
        selection_token=_token(),
    )
    unit_state.transition(
        tmp_path,
        "1",
        unit.id,
        "evaluating",
        reason="candidate ready for typed finding",
        attempt=building,
        selection_token=_token(),
    )
    common = {
        "bundle_hash": "d" * 64,
        "unit_plan_hash": "e" * 64,
        "candidate_hash": "f" * 64,
        "settings_hash": "1" * 64,
        "contract_ids": ["form.contract"],
        "observations": [{"pass": False}],
        "decisions": [],
        "conflict": {
            "kind": "contract",
            "required_authority": "amend form authority",
            "roles": ["form"],
            "controls": [],
        },
        "evidence": ["run:evidence/failure.json"],
    }
    unit_state.record_hypothesis_falsification(
        tmp_path,
        "1",
        unit,
        units,
        **common,
        attempt=building,
        selection_token=_token(),
    )
    falsified = unit_state.load(tmp_path, "1")["units"][unit.id]
    assert "checkpoint" not in falsified
    assert falsified["attempt_checkpoint_history"][-1]["disposition"] == "revoked"

    accepted_root = tmp_path / "accepted"
    accepted_root.mkdir()
    unit_state.initialize(accepted_root, "1", units, plan_hash=_PLAN_A)
    accepted_planning = _claim_planning(accepted_root, units, unit.id)
    accepted_building = _promote(accepted_root, units, accepted_planning)
    candidate = accepted_root / "runs" / accepted_building.run_id / "evidence" / "accepted.png"
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_bytes(b"accepted candidate")
    candidate_hash = hashlib.sha256(candidate.read_bytes()).hexdigest()
    freeze_unit(
        accepted_root,
        "1",
        unit,
        accepted_building,
        candidate_hash=candidate_hash,
        selection_token=_token(),
    )
    unit_state.transition(
        accepted_root,
        "1",
        unit.id,
        "evaluating",
        reason="judge",
        attempt=accepted_building,
        selection_token=_token(),
    )
    publish_passed_evaluation(
        accepted_root,
        "1",
        unit,
        accepted_building,
        candidate_path=candidate.relative_to(accepted_root).as_posix(),
    )
    unit_state_claims.complete_unit_attempt(
        accepted_root,
        "1",
        unit.id,
        units,
        accepted_building,
        expected_plan_hash=_PLAN_A,
        selection_token=_token(),
        reason="accepted",
        evidence=["fixture:accepted"],
    )
    unit_state.record_hypothesis_falsification(
        accepted_root,
        "1",
        unit,
        units,
        **common,
        preserve_accepted_source=True,
        selection_token=_token(),
    )
    preserved = unit_state.load(accepted_root, "1")["units"][unit.id]
    assert preserved["status"] == "passed"
    assert preserved["checkpoint"]["candidate_hash"] == candidate_hash


def test_falsification_state_write_failure_leaves_no_authoritative_record(
    tmp_path,
    monkeypatch,
) -> None:
    unit = _unit("form")
    units = (unit,)
    unit_state.initialize(tmp_path, "1", units, plan_hash=_PLAN_A)
    planning = _claim_planning(tmp_path, units, unit.id)
    building = _promote(tmp_path, units, planning)
    unit_state.freeze_checkpoint(
        tmp_path,
        "1",
        unit,
        active_contract_ids=(),
        candidate_hash="candidate",
        settings_hash="settings",
        script_hash="script",
        input_hash="input",
        attempt=building,
        selection_token=_token(),
    )
    unit_state.transition(
        tmp_path,
        "1",
        unit.id,
        "evaluating",
        reason="judge",
        attempt=building,
        selection_token=_token(),
    )

    def fail_state_write(*_args, **_kwargs) -> None:
        raise OSError("injected state commit failure")

    monkeypatch.setattr(unit_state, "_write", fail_state_write)
    with pytest.raises(OSError, match="injected state commit failure"):
        unit_state.record_hypothesis_falsification(
            tmp_path,
            "1",
            unit,
            units,
            bundle_hash="d" * 64,
            unit_plan_hash="e" * 64,
            candidate_hash="f" * 64,
            settings_hash="1" * 64,
            contract_ids=["form.contract"],
            observations=[{"pass": False}],
            decisions=[],
            conflict={
                "kind": "contract",
                "required_authority": "amend form authority",
                "roles": ["form"],
                "controls": [],
            },
            evidence=["run:evidence/failure.json"],
            attempt=building,
            selection_token=_token(),
        )

    state = unit_state.load(tmp_path, "1")
    slot = state["units"][unit.id]
    assert state.get("falsifications") in (None, [])
    assert "falsification" not in slot
    assert slot["active_attempt"] == building.as_dict()
    assert slot["checkpoint"]["candidate_hash"] == "candidate"
    assert not list(
        (tmp_path / "state/work-units/hypothesis-falsifications").glob("hf-*.json")
    )


def test_falsification_projection_failure_is_state_native_and_reconcilable(
    tmp_path,
    monkeypatch,
) -> None:
    unit = _unit("form")
    units = (unit,)
    unit_state.initialize(tmp_path, "1", units, plan_hash=_PLAN_A)
    building = _promote(tmp_path, units, _claim_planning(tmp_path, units, unit.id))
    freeze_unit(tmp_path, "1", unit, building, selection_token=_token())
    unit_state.transition(
        tmp_path,
        "1",
        unit.id,
        "evaluating",
        reason="judge",
        attempt=building,
        selection_token=_token(),
    )
    original_publish = hypothesis_falsification_projection.publish_projection

    def fail_projection(*_args, **_kwargs) -> None:
        raise OSError("injected derived projection failure")

    monkeypatch.setattr(
        hypothesis_falsification_projection,
        "publish_projection",
        fail_projection,
    )
    with pytest.raises(
        hypothesis_falsification_projection.FalsificationProjectionPending,
        match="committed to durable state",
    ) as caught:
        unit_state.record_hypothesis_falsification(
            tmp_path,
            "1",
            unit,
            units,
            bundle_hash="d" * 64,
            unit_plan_hash="e" * 64,
            candidate_hash="f" * 64,
            settings_hash="1" * 64,
            contract_ids=["form.contract"],
            observations=[{"pass": False}],
            decisions=[],
            conflict={
                "kind": "contract",
                "required_authority": "amend form authority",
                "roles": ["form"],
                "controls": [],
            },
            evidence=["run:evidence/failure.json"],
            attempt=building,
            selection_token=_token(),
        )

    record_id = caught.value.record_id
    state = unit_state.load(tmp_path, "1")
    assert state["units"][unit.id]["status"] == "hypothesis_falsified"
    assert "active_attempt" not in state["units"][unit.id]
    assert load_state_backed_falsification(tmp_path, "1", record_id) == caught.value.payload
    projection = hypothesis_falsification_projection.projection_path(tmp_path, record_id)
    assert not projection.exists()

    monkeypatch.setattr(
        hypothesis_falsification_projection,
        "publish_projection",
        original_publish,
    )
    assert reconcile_falsification_projection(tmp_path, "1", record_id) == projection
    assert json.loads(projection.read_text(encoding="utf-8")) == caught.value.payload


def test_load_rejects_malformed_present_claim_instead_of_treating_it_as_absent(tmp_path) -> None:
    unit = _unit("form")
    units = (unit,)
    unit_state.initialize(tmp_path, "1", units, plan_hash=_PLAN_A)
    path = unit_state._path(tmp_path, "1")
    value = json.loads(path.read_text(encoding="utf-8"))
    value["units"][unit.id]["attempt_revision"] = 1
    value["units"][unit.id]["active_attempt"] = {"schema": "unknown"}
    path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(ValueError, match="fields mismatch"):
        unit_state.load(tmp_path, "1")
