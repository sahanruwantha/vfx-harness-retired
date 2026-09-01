from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace

import anyio
import pytest

from tests.architecture.test_staged_architecture import _unit
from tests.unit_attempt_fixtures import legacy_apply_replan
from vfx_harness.agents.builder import drain as drain_runtime
from vfx_harness.agents.builder import verify as verify_runtime
from vfx_harness.agents.builder.attempt_guard import (
    AttemptBoundBlenderSession,
    UnitAttemptAuthorityLost,
    UnitAttemptGuard,
)
from vfx_harness.agents.builder.unit_construction import resolve_unit_construction
from vfx_harness.domain.construction import ConstructionSpec
from vfx_harness.orchestration import generate_construction, unit_state
from vfx_harness.orchestration.authority_selection_transaction import (
    AuthoritySelectionToken,
)
from vfx_harness.orchestration.ledger import Milestone
from vfx_harness.orchestration.unit_state_claims import (
    claim_ready_unit_for_build,
    claim_ready_unit_for_planning,
)


def test_replan_does_not_wait_for_long_blender_staging_and_postcheck_refuses(
    tmp_path,
) -> None:
    token = AuthoritySelectionToken(0, None, 0, None)
    unit = _unit("hero")
    units = (unit,)
    plan_hash = "a" * 64
    unit_state.initialize(tmp_path, "1", units, plan_hash=plan_hash)
    planning = claim_ready_unit_for_planning(
        tmp_path,
        "1",
        unit.id,
        units,
        expected_plan_hash=plan_hash,
        eligible_passed=set(),
        completion_authorization=None,
        run_id="render-race",
        selection_token=token,
        reason="fixture planning",
    )
    building = claim_ready_unit_for_build(
        tmp_path,
        "1",
        unit.id,
        units,
        planning,
        expected_plan_hash=plan_hash,
        eligible_passed=set(),
        completion_authorization=None,
        run_id="render-race",
        selection_token=token,
        reason="fixture building",
    )
    guard = UnitAttemptGuard.bind(
        tmp_path,
        "1",
        unit,
        units,
        building,
        expected_plan_hash=plan_hash,
        selected_authority=SimpleNamespace(selection_token=token),
    )
    render_started = Event()
    release_render = Event()
    replan_done = Event()
    failures: list[BaseException] = []

    class _Session:
        def render(self):
            render_started.set()
            assert release_render.wait(5)
            return "runs/r1/scratch/render.png"

    proxy = AttemptBoundBlenderSession(_Session(), guard)

    def run_render() -> None:
        try:
            proxy.render()
        except BaseException as exc:  # asserted below
            failures.append(exc)

    def run_replan() -> None:
        try:
            legacy_apply_replan(
                tmp_path,
                "1",
                units,
                units,
                old_plan_hash=plan_hash,
                new_plan_hash=plan_hash,
                owner="fixture",
                trigger="revoke during render",
                evidence=["fixture:replan"],
                reopen={unit.id},
            )
        finally:
            replan_done.set()

    render_thread = Thread(target=run_render)
    replan_thread = Thread(target=run_replan)
    render_thread.start()
    assert render_started.wait(2)
    replan_thread.start()
    try:
        assert replan_done.wait(2), "replan waited on the long Blender render"
    finally:
        release_render.set()
    render_thread.join(5)
    replan_thread.join(5)

    assert len(failures) == 1
    assert isinstance(failures[0], UnitAttemptAuthorityLost)


def test_revocation_before_turn_continuation_refuses_additional_model_query(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = AuthoritySelectionToken(0, None, 0, None)
    unit = _unit("continuation")
    units = (unit,)
    plan_hash = "b" * 64
    unit_state.initialize(tmp_path, "1", units, plan_hash=plan_hash)
    planning = claim_ready_unit_for_planning(
        tmp_path,
        "1",
        unit.id,
        units,
        expected_plan_hash=plan_hash,
        eligible_passed=set(),
        completion_authorization=None,
        run_id="continuation-race",
        selection_token=token,
        reason="fixture planning",
    )
    building = claim_ready_unit_for_build(
        tmp_path,
        "1",
        unit.id,
        units,
        planning,
        expected_plan_hash=plan_hash,
        eligible_passed=set(),
        completion_authorization=None,
        run_id="continuation-race",
        selection_token=token,
        reason="fixture building",
    )
    guard = UnitAttemptGuard.bind(
        tmp_path,
        "1",
        unit,
        units,
        building,
        expected_plan_hash=plan_hash,
        selected_authority=SimpleNamespace(selection_token=token),
    )

    async def revoke_during_first_response(*_args, **_kwargs):
        legacy_apply_replan(
            tmp_path,
            "1",
            units,
            units,
            old_plan_hash=plan_hash,
            new_plan_hash=plan_hash,
            owner="fixture",
            trigger="revoke before max-turn continuation",
            evidence=["fixture:continuation-race"],
            reopen={unit.id},
        )
        return {
            "subtype": "error_max_turns",
            "turns": 10,
            "cost": 1.0,
            "is_error": False,
            "api_error_status": None,
            "tokens": {},
        }

    monkeypatch.setattr(drain_runtime, "_drain_once", revoke_during_first_response)

    class _Client:
        query_calls = 0

        async def query(self, _prompt):
            self.query_calls += 1

    client = _Client()
    with pytest.raises(UnitAttemptAuthorityLost):
        anyio.run(
            lambda: drain_runtime._drain(
                client,
                False,
                continues=1,
                attempt_guard=guard,
            )
        )

    assert client.query_calls == 0


def test_canonical_critic_refuses_revoked_attempt_before_paid_score(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = AuthoritySelectionToken(0, None, 0, None)
    unit = _unit(
        "hero",
        frame=40,
        script_span="build/units/1/hero.py",
    )
    units = (unit,)
    plan_hash = "c" * 64
    unit_state.initialize(tmp_path, "1", units, plan_hash=plan_hash)
    planning = claim_ready_unit_for_planning(
        tmp_path,
        "1",
        unit.id,
        units,
        expected_plan_hash=plan_hash,
        eligible_passed=set(),
        completion_authorization=None,
        run_id="canonical-score-race",
        selection_token=token,
        reason="fixture planning",
    )
    building = claim_ready_unit_for_build(
        tmp_path,
        "1",
        unit.id,
        units,
        planning,
        expected_plan_hash=plan_hash,
        eligible_passed=set(),
        completion_authorization=None,
        run_id="canonical-score-race",
        selection_token=token,
        reason="fixture building",
    )
    guard = UnitAttemptGuard.bind(
        tmp_path,
        "1",
        unit,
        units,
        building,
        expected_plan_hash=plan_hash,
        selected_authority=SimpleNamespace(selection_token=token),
    )

    script_rel = unit.mutates.script_spans[0]
    script = tmp_path / script_rel
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("# canonical fixture\n", encoding="utf-8")
    reference = tmp_path / "refs/f040.png"
    reference.parent.mkdir(parents=True, exist_ok=True)
    reference.write_bytes(b"reference")
    render = tmp_path / "renders/canonical.png"
    render.parent.mkdir(parents=True, exist_ok=True)
    render.write_bytes(b"render")

    package = verify_runtime.builder_package()
    monkeypatch.setattr(package, "_preamble", lambda _shot: "")
    monkeypatch.setattr(package, "_run_prior_paths", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(package, "_scene_object_manifest", lambda _session: {})
    monkeypatch.setattr(
        package,
        "_stash_render_with_receipt",
        lambda *_args, **_kwargs: (
            "renders/canonical.png",
            {"schema": "fixture-render-receipt"},
        ),
    )
    monkeypatch.setattr(verify_runtime, "_run_artifact_script", lambda *_args: None)
    monkeypatch.setattr(
        verify_runtime,
        "_unit_requires_raster",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(verify_runtime, "_unit_raster_mode", lambda _unit: "eevee")
    monkeypatch.setattr(verify_runtime, "_layer_needs_motion", lambda _layer: False)
    monkeypatch.setattr(
        verify_runtime,
        "_persist_contract_gaps",
        lambda *_args, **_kwargs: None,
    )

    def revoke_after_render(*_args, **_kwargs):
        legacy_apply_replan(
            tmp_path,
            "1",
            units,
            units,
            old_plan_hash=plan_hash,
            new_plan_hash=plan_hash,
            owner="fixture",
            trigger="revoke after canonical render",
            evidence=["fixture:canonical-score-race"],
            reopen={unit.id},
        )
        return []

    monkeypatch.setattr(package, "_render_evidence", revoke_after_render)
    paid_scores: list[int] = []

    async def paid_score(*_args, **_kwargs):
        paid_scores.append(1)
        return {"pass": True, "mean": 5.0, "scores": {}, "issues": []}

    monkeypatch.setattr(verify_runtime, "_judge_unit_or_layer", paid_score)

    class _Session:
        def run(self, *_args, **_kwargs):
            return {"result": {}}

    class _Ledger:
        def record_round(self, *_args, **_kwargs):
            raise AssertionError("revoked canonical score cannot reach the ledger")

    shot = SimpleNamespace(
        folder=tmp_path,
        frontmatter={"type": "still"},
        frames=40,
    )
    layer = SimpleNamespace(
        id="1",
        judges=((40, "refs/f040.png"),),
        stages=units,
        owns=("form",),
        temporal_evidence="none",
    )
    milestone = Milestone("1@hero", 40, "refs/f040.png", "fixture")

    async def verify() -> None:
        await verify_runtime._verify_script(
            shot,
            milestone,
            script_rel,
            [],
            _Session(),
            [("form", "declared form")],
            _Ledger(),
            False,
            layer=layer,
            active_unit=unit,
            authority_script_rel=script_rel,
            selected_authority=guard.selected_authority,
            execution_guard=guard,
        )

    with pytest.raises(UnitAttemptAuthorityLost):
        anyio.run(verify)
    assert paid_scores == []


def test_replan_does_not_wait_for_snapshot_copy_and_stale_commit_is_discarded(
    tmp_path,
) -> None:
    token = AuthoritySelectionToken(0, None, 0, None)
    unit = _unit("hero")
    units = (unit,)
    plan_hash = "b" * 64
    unit_state.initialize(tmp_path, "1", units, plan_hash=plan_hash)
    planning = claim_ready_unit_for_planning(
        tmp_path,
        "1",
        unit.id,
        units,
        expected_plan_hash=plan_hash,
        eligible_passed=set(),
        completion_authorization=None,
        run_id="snapshot-race",
        selection_token=token,
        reason="fixture planning",
    )
    building = claim_ready_unit_for_build(
        tmp_path,
        "1",
        unit.id,
        units,
        planning,
        expected_plan_hash=plan_hash,
        eligible_passed=set(),
        completion_authorization=None,
        run_id="snapshot-race",
        selection_token=token,
        reason="fixture building",
    )
    guard = UnitAttemptGuard.bind(
        tmp_path,
        "1",
        unit,
        units,
        building,
        expected_plan_hash=plan_hash,
        selected_authority=SimpleNamespace(selection_token=token),
    )
    prepare_started = Event()
    release_prepare = Event()
    replan_done = Event()
    discarded = Event()
    committed = Event()
    failures: list[BaseException] = []

    class _Session:
        def snapshot(self, _tag):
            raise AssertionError("attempt proxy must use staged snapshot publication")

        def stage_snapshot(self, _tag):
            return {"blend": "worker-scratch.blend"}

        def prepare_snapshot_publication(self, _staged):
            prepare_started.set()
            assert release_prepare.wait(5)
            return {"prepared": "parent-temp"}

        def commit_snapshot_publication(self, _prepared):
            committed.set()
            return {"blend": "accepted.blend"}

        def discard_snapshot_publication(self, _prepared):
            discarded.set()

    proxy = AttemptBoundBlenderSession(_Session(), guard)

    def run_snapshot() -> None:
        try:
            proxy.snapshot("candidate")
        except BaseException as exc:  # asserted below
            failures.append(exc)

    def run_replan() -> None:
        try:
            legacy_apply_replan(
                tmp_path,
                "1",
                units,
                units,
                old_plan_hash=plan_hash,
                new_plan_hash=plan_hash,
                owner="fixture",
                trigger="revoke during parent snapshot copy",
                evidence=["fixture:replan"],
                reopen={unit.id},
            )
        finally:
            replan_done.set()

    snapshot_thread = Thread(target=run_snapshot)
    replan_thread = Thread(target=run_replan)
    snapshot_thread.start()
    assert prepare_started.wait(2)
    replan_thread.start()
    try:
        assert replan_done.wait(2), "replan waited on the large parent snapshot copy"
    finally:
        release_prepare.set()
    snapshot_thread.join(5)
    replan_thread.join(5)

    assert len(failures) == 1
    assert isinstance(failures[0], UnitAttemptAuthorityLost)
    assert discarded.is_set()
    assert not committed.is_set()


def test_replan_does_not_wait_for_large_construction_publication_staging(
    tmp_path,
    monkeypatch,
) -> None:
    token = AuthoritySelectionToken(0, None, 0, None)
    unit = replace(
        _unit("hero"),
        construction=ConstructionSpec("generate", ("refobs-fixture",)),
    )
    units = (unit,)
    plan_hash = "a" * 64
    unit_state.initialize(tmp_path, "1", units, plan_hash=plan_hash)
    planning = claim_ready_unit_for_planning(
        tmp_path,
        "1",
        unit.id,
        units,
        expected_plan_hash=plan_hash,
        eligible_passed=set(),
        completion_authorization=None,
        run_id="construction-race",
        selection_token=token,
        reason="fixture planning",
    )
    building = claim_ready_unit_for_build(
        tmp_path,
        "1",
        unit.id,
        units,
        planning,
        expected_plan_hash=plan_hash,
        eligible_passed=set(),
        completion_authorization=None,
        run_id="construction-race",
        selection_token=token,
        reason="fixture building",
    )
    guard = UnitAttemptGuard.bind(
        tmp_path,
        "1",
        unit,
        units,
        building,
        expected_plan_hash=plan_hash,
        selected_authority=SimpleNamespace(selection_token=token),
    )
    prepare_started = Event()
    release_prepare = Event()
    replan_done = Event()
    failures: list[BaseException] = []
    committed: list[object] = []

    monkeypatch.setattr(
        generate_construction,
        "prepare_promoted_construction_reuse",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        generate_construction,
        "stage_generate_unit",
        lambda *_args, **_kwargs: object(),
    )

    def prepare(*_args, **_kwargs):
        prepare_started.set()
        assert release_prepare.wait(5)
        return object()

    monkeypatch.setattr(
        generate_construction,
        "prepare_generate_unit_promotion",
        prepare,
    )
    monkeypatch.setattr(
        generate_construction,
        "commit_generate_unit_promotion",
        lambda *_args, **_kwargs: committed.append(object()),
    )

    def run_construction() -> None:
        try:
            resolve_unit_construction(
                SimpleNamespace(folder=tmp_path),
                "1",
                unit,
                unit_state.unit_digest(unit),
                guard,
            )
        except BaseException as exc:  # asserted below
            failures.append(exc)

    def run_replan() -> None:
        try:
            legacy_apply_replan(
                tmp_path,
                "1",
                units,
                units,
                old_plan_hash=plan_hash,
                new_plan_hash=plan_hash,
                owner="fixture",
                trigger="revoke during construction CAS staging",
                evidence=["fixture:replan"],
                reopen={unit.id},
            )
        finally:
            replan_done.set()

    construction_thread = Thread(target=run_construction)
    replan_thread = Thread(target=run_replan)
    construction_thread.start()
    assert prepare_started.wait(2)
    replan_thread.start()
    try:
        assert replan_done.wait(2), "replan waited on large construction I/O"
    finally:
        release_prepare.set()
    construction_thread.join(5)
    replan_thread.join(5)

    assert len(failures) == 1
    assert isinstance(failures[0], UnitAttemptAuthorityLost)
    assert committed == []
