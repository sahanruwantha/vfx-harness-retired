from __future__ import annotations

import json
from dataclasses import replace
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from tests.architecture.test_staged_architecture import _unit
from tests.unit_attempt_fixtures import (
    claim_for_build,
    executed_replay_input,
    freeze_unit,
    legacy_apply_replan,
)
from vfx_harness.agents import builder
from vfx_harness.agents.builder import revalidate, unit_evaluation
from vfx_harness.agents.builder.attempt_guard import (
    UnitAttemptAuthorityLost,
    UnitAttemptGuard,
)
from vfx_harness.domain.brief import Shot
from vfx_harness.domain.work_units import JudgePoint, canonical_unit_script_path
from vfx_harness.orchestration import (
    unit_evaluation_receipts,
    unit_state,
    unit_state_claims,
)
from vfx_harness.orchestration.authority_selection_transaction import (
    AuthoritySelectionToken,
)
from vfx_harness.orchestration.ledger import Layer, Ledger, Milestone


class _RevalidationSession:
    def run(self, source, **_kwargs):
        if "bpy.app.version_string" in source:
            return {"result": "4.2.0"}
        if "bvfx_role" in source:
            return {"result": {}}
        return {"result": None}


def test_revalidation_refuses_unverified_layer_outcome_before_blender(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        revalidate.layer_publication,
        "require_current_layer_publication",
        lambda *_args: (_ for _ in ()).throw(
            revalidate.layer_publication.LayerPublicationConflict(
                "no current terminal finalization receipt"
            )
        ),
    )

    class _NoBlender:
        def run(self, *_args, **_kwargs):
            pytest.fail("unverified revalidation must stop before Blender")

    observed = revalidate._try_revalidate(
        SimpleNamespace(folder=tmp_path),
        None,
        "build/units/01/hero.py",
        [],
        _NoBlender(),
        layer=SimpleNamespace(id="01"),
        ledger=None,
        t_layer=0.0,
        active_unit=None,
        attempt_guard=None,
        selected_authority=SimpleNamespace(),
    )

    assert observed is None


def test_revalidation_refuses_layer_milestone_under_unit_attempt(
    tmp_path,
    monkeypatch,
) -> None:
    token = AuthoritySelectionToken(0, None, 0, None)
    selected = SimpleNamespace(selection_token=token, plan=None)
    unit = _two_judge_unit()
    units = (unit,)
    layer = Layer(
        id="04",
        script="build/layer_04.py",
        title="fixture layer",
        judges=((40, "refs/f040.png"), (80, "refs/f080.png")),
        reads="fixture",
        owns=("form",),
        primary_judge=40,
        stages=units,
    )
    plan_hash = "c" * 64
    unit_state.initialize(tmp_path, layer.id, units, plan_hash=plan_hash)
    attempt = claim_for_build(
        tmp_path,
        layer.id,
        units,
        unit.id,
        plan_hash=plan_hash,
        selection_token=token,
    )
    guard = UnitAttemptGuard.bind(
        tmp_path,
        layer.id,
        unit,
        units,
        attempt,
        expected_plan_hash=plan_hash,
        selected_authority=selected,
    )
    monkeypatch.setattr(
        revalidate.layer_publication,
        "require_current_layer_publication",
        lambda *_args: SimpleNamespace(
            receipt=SimpleNamespace(receipt_digest="f" * 64),
            outcome_bytes=b"{}",
            outcome_sha256="e" * 64,
        ),
    )
    shot = Shot(
        folder=tmp_path,
        frontmatter={"id": "wrong-milestone", "frames": 100, "fps": 24},
        body="",
    )

    class _NoBlender:
        def run(self, *_args, **_kwargs):
            pytest.fail("wrong milestone must fail before Blender")

    with pytest.raises(ValueError, match="exact unit milestone"):
        revalidate._try_revalidate(
            shot,
            layer.as_milestone({}),
            canonical_unit_script_path(layer.id, unit.id),
            [],
            _NoBlender(),
            layer=layer,
            ledger=Ledger(shot),
            t_layer=0.0,
            active_unit=unit,
            attempt_guard=guard,
            selected_authority=selected,
        )

    assert not (tmp_path / "shot.json").exists()


def test_evaluation_receipt_staging_rejects_temp_name_substitution(
    tmp_path,
) -> None:
    destination = tmp_path / "runs" / "fixture" / "checkpoints" / "receipt.json"
    authority = "unit-evaluation:" + "a" * 64
    publication, existing = unit_evaluation_receipts._prepare_immutable_receipt(
        tmp_path,
        destination,
        b'{"schema":"fixture"}\n',
        authority_binding=authority,
    )
    assert publication is not None
    assert existing is None
    detached = publication.temporary.with_suffix(".held")
    publication.temporary.rename(detached)
    publication.temporary.write_bytes(b"substitute")

    try:
        with pytest.raises(
            unit_evaluation_receipts.FilePublicationConflict,
            match="prepared side-file inode changed",
        ):
            unit_evaluation_receipts.commit_prepared_file(
                publication,
                authority_binding=authority,
            )
    finally:
        unit_evaluation_receipts.discard_prepared_file(publication)

    assert publication.temporary.read_bytes() == b"substitute"
    assert detached.read_bytes() == b'{"schema":"fixture"}\n'
    assert not destination.exists()


def _two_judge_unit():
    unit = _unit(
        "hero",
        frame=40,
        script_span="build/units/04/hero.py",
    )
    claim = replace(unit.evaluation.claims[0], moments=(40, 80))
    policy = replace(
        unit.evaluation,
        judges=(
            JudgePoint(40, "refs/f040.png"),
            JudgePoint(80, "refs/f080.png"),
        ),
        claims=(claim,),
    )
    return replace(unit, evaluation=policy)


def test_replan_does_not_wait_for_evaluation_receipt_staging(
    tmp_path,
    monkeypatch,
) -> None:
    token = AuthoritySelectionToken(0, None, 0, None)
    unit = _unit("hero")
    units = (unit,)
    plan_hash = "a" * 64
    unit_state.initialize(tmp_path, "1", units, plan_hash=plan_hash)
    attempt = claim_for_build(
        tmp_path,
        "1",
        units,
        unit.id,
        plan_hash=plan_hash,
        selection_token=token,
    )
    guard = UnitAttemptGuard.bind(
        tmp_path,
        "1",
        unit,
        units,
        attempt,
        expected_plan_hash=plan_hash,
        selected_authority=SimpleNamespace(selection_token=token),
    )
    prepare_started = Event()
    release_prepare = Event()
    replan_done = Event()
    discarded = Event()
    commits: list[object] = []
    failures: list[BaseException] = []
    prepared = object()

    def prepare(*_args, **_kwargs):
        prepare_started.set()
        assert release_prepare.wait(5)
        return prepared

    monkeypatch.setattr(
        unit_evaluation_receipts,
        "prepare_unit_evaluation_receipt",
        prepare,
    )
    monkeypatch.setattr(
        unit_evaluation_receipts,
        "commit_unit_evaluation_receipt",
        lambda value: commits.append(value),
    )
    monkeypatch.setattr(
        unit_evaluation_receipts,
        "discard_prepared_unit_evaluation_receipt",
        lambda value: discarded.set() if value is prepared else None,
    )

    def publish_receipt() -> None:
        try:
            unit_evaluation.publish_unit_evaluation_outcome(
                tmp_path,
                "1",
                unit,
                guard,
                result="passed",
                canonical_verdicts=(),
                ledger_slot={},
                replay_inputs=(),
                candidate_path=None,
            )
        except BaseException as exc:  # asserted below
            failures.append(exc)

    def replan() -> None:
        try:
            legacy_apply_replan(
                tmp_path,
                "1",
                units,
                units,
                old_plan_hash=plan_hash,
                new_plan_hash=plan_hash,
                owner="fixture",
                trigger="revoke during evaluator receipt staging",
                evidence=["fixture:replan"],
                reopen={unit.id},
            )
        finally:
            replan_done.set()

    publish_thread = Thread(target=publish_receipt)
    replan_thread = Thread(target=replan)
    publish_thread.start()
    assert prepare_started.wait(2)
    replan_thread.start()
    try:
        assert replan_done.wait(2), "replan waited on evaluator receipt hashing or fsync"
    finally:
        release_prepare.set()
    publish_thread.join(5)
    replan_thread.join(5)

    assert len(failures) == 1
    assert isinstance(failures[0], UnitAttemptAuthorityLost)
    assert discarded.is_set()
    assert commits == []


def test_evaluation_receipt_readback_runs_after_guarded_commit(
    tmp_path,
    monkeypatch,
) -> None:
    unit = _unit("hero")
    prepared = object()
    committed = object()
    finalized = object()
    events: list[str] = []

    class _Guard:
        claim = object()
        held = False

        def check(self, _operation):
            events.append("check")

        def publish(self, _operation, mutation):
            self.held = True
            try:
                return mutation()
            finally:
                self.held = False

    guard = _Guard()

    def prepare(*_args, **_kwargs):
        assert not guard.held
        events.append("prepare")
        return prepared

    def commit(value):
        assert guard.held
        assert value is prepared
        events.append("commit")
        return committed

    def finalize(prepared_value, committed_value):
        assert not guard.held
        assert prepared_value is prepared
        assert committed_value is committed
        events.append("finalize")
        return finalized

    monkeypatch.setattr(
        unit_evaluation_receipts,
        "prepare_unit_evaluation_receipt",
        prepare,
    )
    monkeypatch.setattr(
        unit_evaluation_receipts,
        "commit_unit_evaluation_receipt",
        commit,
    )
    monkeypatch.setattr(
        unit_evaluation_receipts,
        "finalize_committed_unit_evaluation_receipt",
        finalize,
    )
    monkeypatch.setattr(
        unit_evaluation_receipts,
        "discard_prepared_unit_evaluation_receipt",
        lambda value: events.append("discard") if value is prepared else None,
    )

    observed = unit_evaluation.publish_unit_evaluation_outcome(
        tmp_path,
        "1",
        unit,
        guard,
        result="passed",
        canonical_verdicts=(),
        ledger_slot={},
        replay_inputs=(),
        candidate_path=None,
    )

    assert observed is finalized
    assert events == ["check", "prepare", "commit", "finalize", "discard"]


def test_revalidation_records_every_canonical_judge_and_completion_consumes_receipt(
    tmp_path,
    monkeypatch,
) -> None:
    token = AuthoritySelectionToken(0, None, 0, None)
    selected = SimpleNamespace(selection_token=token, plan=None)
    unit = _two_judge_unit()
    units = (unit,)
    layer = Layer(
        id="04",
        script=canonical_unit_script_path("04", unit.id),
        title="two-judge executable unit",
        judges=((40, "refs/f040.png"), (80, "refs/f080.png")),
        reads="the executable state holds at both declared judges",
        owns=("form",),
        primary_judge=40,
        stages=units,
    )
    milestone = Milestone(
        f"{layer.id}@{unit.id}",
        unit.evaluation.primary_judge,
        "refs/f040.png",
        layer.reads,
    )
    shot = Shot(
        folder=tmp_path,
        frontmatter={"id": "revalidation-fixture", "frames": 100, "fps": 24},
        body="",
    )
    plan_hash = "a" * 64
    unit_state.initialize(tmp_path, layer.id, units, plan_hash=plan_hash)
    attempt = claim_for_build(
        tmp_path,
        layer.id,
        units,
        unit.id,
        plan_hash=plan_hash,
        selection_token=token,
    )
    guard = UnitAttemptGuard.bind(
        tmp_path,
        layer.id,
        unit,
        units,
        attempt,
        expected_plan_hash=plan_hash,
        selected_authority=selected,
    )
    script = tmp_path / canonical_unit_script_path(layer.id, unit.id)
    script.parent.mkdir(parents=True)
    script.write_text("# exact revalidated unit\npass\n", encoding="utf-8")

    ledger = Ledger(shot)
    ledger._slot(milestone).update(
        {
            "run_id": attempt.run_id,
            "attempt": attempt.attempt_revision,
            "status": "in_progress",
            "script": canonical_unit_script_path(layer.id, unit.id),
            "unit_hash": attempt.unit_digest,
            "rounds": [],
        }
    )
    outcome = {
        "best": {"round": 0, "mean": 5.0, "render": None},
        "canonical": [],
    }
    evidence = [
        {
            "id": "contract.hero",
            "source": "interface_contract",
            "pass": True,
            "authoritative": True,
        }
    ]
    report = tmp_path / "revalidation-report.json"

    publication = SimpleNamespace(
        receipt=SimpleNamespace(receipt_digest="f" * 64),
        outcome_bytes=json.dumps(outcome).encode("utf-8"),
        outcome_sha256="e" * 64,
    )
    monkeypatch.setattr(
        revalidate.layer_publication,
        "require_current_layer_publication",
        lambda *_args: publication,
    )
    manifest_calls: list[None] = []

    def current_manifest(*_args, **_kwargs):
        manifest_calls.append(None)
        return {}

    monkeypatch.setattr(revalidate, "input_manifest", current_manifest)
    monkeypatch.setattr(revalidate, "eligibility", lambda *_args: (True, []))
    monkeypatch.setattr(revalidate, "_unit_requires_raster", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(revalidate, "_load_contract_rows", lambda *_args: [])
    monkeypatch.setattr(revalidate, "plan_strips", lambda *_args: {})
    monkeypatch.setattr(revalidate, "_run_artifact_script", lambda *_args: None)
    monkeypatch.setattr(builder, "_preamble", lambda *_args: "")
    monkeypatch.setattr(builder, "_run_prior_paths", lambda *_args: [])
    monkeypatch.setattr(builder, "_render_evidence", lambda *_args, **_kwargs: evidence)

    def write_report(*_args, **_kwargs):
        report.write_text(json.dumps({"status": "passed"}), encoding="utf-8")
        return report

    monkeypatch.setattr(revalidate, "write_run", write_report)
    monkeypatch.setattr(revalidate, "run_summary", lambda *_args: "")
    monkeypatch.setattr(revalidate, "log", lambda *_args, **_kwargs: None)

    observed = revalidate._try_revalidate(
        shot,
        milestone,
        canonical_unit_script_path(layer.id, unit.id),
        [],
        _RevalidationSession(),
        layer=layer,
        ledger=ledger,
        t_layer=0.0,
        active_unit=unit,
        attempt_guard=guard,
        selected_authority=selected,
    )

    assert observed is ledger
    assert len(manifest_calls) == 2
    slot = ledger._slot(milestone)
    canonical_rounds = [row for row in slot["rounds"] if row["kind"] == "canonical"]
    assert len(canonical_rounds) == len(unit.evaluation.judges) == 2
    stored = unit_evaluation_receipts.load_unit_evaluation_receipt(tmp_path, attempt)
    assert stored.receipt.result == "reproduced"
    assert [row["frame"] for row in stored.receipt.canonical] == [40, 80]
    assert len(stored.receipt.ledger_outcome["canonical_rounds"]) == 2

    freeze_unit(
        tmp_path,
        layer.id,
        unit,
        attempt,
        selection_token=token,
    )
    unit_state.transition(
        tmp_path,
        layer.id,
        unit.id,
        "evaluating",
        reason="revalidation canonical evidence sealed",
        attempt=attempt,
        selection_token=token,
    )
    completion = unit_state_claims.complete_unit_attempt(
        tmp_path,
        layer.id,
        unit.id,
        units,
        attempt,
        expected_plan_hash=plan_hash,
        selection_token=token,
        reason="revalidated canonical evidence accepted",
        evidence=["fixture:revalidation-receipt"],
    )

    assert completion.evaluation_receipt_digest == stored.receipt.receipt_digest
    assert unit_state.load(tmp_path, layer.id)["units"][unit.id]["status"] == "passed"


def test_evaluation_receipt_commit_rejects_recreated_script_ancestor(
    tmp_path,
) -> None:
    token = AuthoritySelectionToken(0, None, 0, None)
    unit = _two_judge_unit()
    units = (unit,)
    plan_hash = "a" * 64
    unit_state.initialize(tmp_path, "04", units, plan_hash=plan_hash)
    attempt = claim_for_build(
        tmp_path,
        "04",
        units,
        unit.id,
        plan_hash=plan_hash,
        selection_token=token,
    )
    script = tmp_path / canonical_unit_script_path("04", unit.id)
    script.parent.mkdir(parents=True)
    script.write_text("# exact evaluated unit\npass\n", encoding="utf-8")
    evidence = [
        {
            "id": "contract.hero",
            "source": "interface_contract",
            "pass": True,
            "authoritative": True,
        }
    ]
    canonical_verdicts = tuple(
        (
            (judge.frame, judge.ref),
            {
                "pass": True,
                "render": None,
                "decided_by": "critic",
                "evidence": evidence,
            },
        )
        for judge in unit.evaluation.judges
    )
    rounds = [
        {
            "kind": "canonical",
            "pass": True,
            "render": None,
            "decided_by": "critic",
            "evidence": evidence,
        }
        for _judge in unit.evaluation.judges
    ]
    slot = {
        "run_id": attempt.run_id,
        "attempt": attempt.attempt_revision,
        "status": "passed",
        "script": canonical_unit_script_path("04", unit.id),
        "unit_hash": attempt.unit_digest,
        "artifact_unit_hash": attempt.unit_digest,
        "rounds": rounds,
    }
    (tmp_path / "shot.json").write_text(
        json.dumps({"milestones": {"04": slot}}) + "\n",
        encoding="utf-8",
    )
    prepared = unit_evaluation_receipts.prepare_unit_evaluation_receipt(
        tmp_path,
        "04",
        unit,
        attempt,
        result="passed",
        canonical_verdicts=canonical_verdicts,
        ledger_slot=slot,
        replay_inputs=(
            executed_replay_input(
                tmp_path,
                canonical_unit_script_path("04", unit.id),
            ),
        ),
    )
    build = tmp_path / "build"
    retired = tmp_path / "build-retired"
    build.rename(retired)
    replacement = tmp_path / canonical_unit_script_path("04", unit.id)
    replacement.parent.mkdir(parents=True)
    replacement.write_bytes(
        (retired / "units" / "04" / f"{unit.id}.py").read_bytes()
    )

    try:
        with pytest.raises(
            unit_evaluation_receipts.UnitEvaluationConflict,
            match="causal input changed before publication",
        ):
            unit_evaluation_receipts.commit_unit_evaluation_receipt(prepared)
    finally:
        unit_evaluation_receipts.discard_prepared_unit_evaluation_receipt(prepared)

    assert not prepared.destination.exists()


def test_evaluation_receipt_refuses_swap_restore_of_executed_prior(
    tmp_path,
) -> None:
    token = AuthoritySelectionToken(0, None, 0, None)
    unit = _two_judge_unit()
    units = (unit,)
    plan_hash = "a" * 64
    unit_state.initialize(tmp_path, "04", units, plan_hash=plan_hash)
    attempt = claim_for_build(
        tmp_path,
        "04",
        units,
        unit.id,
        plan_hash=plan_hash,
        selection_token=token,
    )
    prior_path = "build/units/01/base.py"
    prior = tmp_path / prior_path
    prior.parent.mkdir(parents=True)
    prior.write_text("# exact accepted prior\npass\n", encoding="utf-8")
    script_path = canonical_unit_script_path("04", unit.id)
    script = tmp_path / script_path
    script.parent.mkdir(parents=True)
    script.write_text("# exact evaluated unit\npass\n", encoding="utf-8")
    replay_inputs = (
        executed_replay_input(tmp_path, prior_path),
        executed_replay_input(tmp_path, script_path),
    )
    evidence = [
        {
            "id": "contract.hero",
            "source": "interface_contract",
            "pass": True,
            "authoritative": True,
        }
    ]
    canonical_verdicts = tuple(
        (
            (judge.frame, judge.ref),
            {
                "pass": True,
                "render": None,
                "decided_by": "critic",
                "evidence": evidence,
            },
        )
        for judge in unit.evaluation.judges
    )
    rounds = [
        {
            "kind": "canonical",
            "pass": True,
            "render": None,
            "decided_by": "critic",
            "evidence": evidence,
        }
        for _judge in unit.evaluation.judges
    ]
    slot = {
        "run_id": attempt.run_id,
        "attempt": attempt.attempt_revision,
        "status": "passed",
        "script": script_path,
        "unit_hash": attempt.unit_digest,
        "artifact_unit_hash": attempt.unit_digest,
        "rounds": rounds,
    }
    (tmp_path / "shot.json").write_text(
        json.dumps({"milestones": {"04": slot}}) + "\n",
        encoding="utf-8",
    )

    retired = tmp_path / "retired-prior.py"
    prior.rename(retired)
    prior.write_bytes(retired.read_bytes())

    with pytest.raises(
        unit_evaluation_receipts.UnitEvaluationConflict,
        match="causal input changed before publication",
    ):
        unit_evaluation_receipts.prepare_unit_evaluation_receipt(
            tmp_path,
            "04",
            unit,
            attempt,
            result="passed",
            canonical_verdicts=canonical_verdicts,
            ledger_slot=slot,
            replay_inputs=replay_inputs,
        )
