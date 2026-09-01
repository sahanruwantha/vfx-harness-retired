from __future__ import annotations

import asyncio
import fcntl
import json
import os
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from tests.architecture.test_staged_architecture import _unit
from tests.unit_attempt_fixtures import legacy_apply_replan
from vfx_harness.agents.builder.attempt_guard import (
    UnitAttemptAuthorityLost,
    UnitAttemptGuard,
)
from vfx_harness.blender.tools.misc import register_misc
from vfx_harness.evidence import runtime_check_publication
from vfx_harness.observability import prepared_publication, worklists
from vfx_harness.orchestration import escalate, unit_state
from vfx_harness.orchestration.authority_selection_transaction import (
    AuthoritySelectionToken,
)
from vfx_harness.orchestration.unit_state_claims import (
    claim_ready_unit_for_build,
    claim_ready_unit_for_planning,
)


def _building_guard(tmp_path: Path):
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
        run_id="side-file-race",
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
        run_id="side-file-race",
        selection_token=token,
        reason="fixture building",
    )
    selected = SimpleNamespace(selection_token=token)
    guard = UnitAttemptGuard.bind(
        tmp_path,
        "1",
        unit,
        units,
        building,
        expected_plan_hash=plan_hash,
        selected_authority=selected,
    )
    return unit, units, plan_hash, guard


def _runtime_row(identifier: str) -> dict:
    return {
        "id": identifier,
        "layer": "1",
        "metric": "region_mean",
        "op": ">=",
        "lo": 0.5,
    }


def test_prepared_side_file_commit_performs_no_content_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    update = runtime_check_publication.prepare_runtime_check_update(
        tmp_path,
        [_runtime_row("first")],
        authority_binding="work-unit-attempt:claim-a",
    )
    assert update.publication is not None

    def refuse_content_io(*_args, **_kwargs):
        raise AssertionError("metadata-only commit attempted content I/O")

    monkeypatch.setattr(prepared_publication.os, "read", refuse_content_io)
    monkeypatch.setattr(prepared_publication.os, "write", refuse_content_io)

    prepared_publication.commit_prepared_file(
        update.publication,
        authority_binding="work-unit-attempt:claim-a",
    )

    assert json.loads((tmp_path / "runtime_checks.json").read_text()) == [
        _runtime_row("first")
    ]


def test_runtime_check_preparations_compare_and_swap_exact_predecessor(
    tmp_path: Path,
) -> None:
    first = runtime_check_publication.prepare_runtime_check_update(
        tmp_path,
        [_runtime_row("first")],
        authority_binding="work-unit-attempt:claim-a",
    )
    second = runtime_check_publication.prepare_runtime_check_update(
        tmp_path,
        [_runtime_row("second")],
        authority_binding="work-unit-attempt:claim-b",
    )
    assert first.publication is not None
    assert second.publication is not None

    prepared_publication.commit_prepared_file(
        first.publication,
        authority_binding="work-unit-attempt:claim-a",
    )
    with pytest.raises(
        prepared_publication.FilePublicationConflict,
        match="target changed after preparation",
    ):
        prepared_publication.commit_prepared_file(
            second.publication,
            authority_binding="work-unit-attempt:claim-b",
        )
    prepared_publication.discard_prepared_file(second.publication)

    assert json.loads((tmp_path / "runtime_checks.json").read_text()) == [
        _runtime_row("first")
    ]


def test_side_file_preparation_cleanup_never_deletes_a_substituted_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_write_all = prepared_publication._write_all

    def substitute_then_fail(descriptor: int, payload: bytes) -> None:
        original_write_all(descriptor, payload)
        temporary = next(tmp_path.glob(".runtime_checks.json.prepared.*"))
        substitute = tmp_path / "substitute"
        substitute.write_bytes(b"attacker")
        os.replace(substitute, temporary)
        raise OSError("injected failure after temp substitution")

    monkeypatch.setattr(prepared_publication, "_write_all", substitute_then_fail)

    with pytest.raises(OSError, match="injected failure"):
        runtime_check_publication.prepare_runtime_check_update(
            tmp_path,
            [_runtime_row("first")],
            authority_binding="work-unit-attempt:claim-a",
        )

    leftovers = list(tmp_path.glob(".runtime_checks.json.prepared.*"))
    assert len(leftovers) == 1
    assert leftovers[0].read_bytes() == b"attacker"
    assert not (tmp_path / "runtime_checks.json").exists()


def test_side_file_commit_refuses_post_rename_substitution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    update = runtime_check_publication.prepare_runtime_check_update(
        tmp_path,
        [_runtime_row("first")],
        authority_binding="work-unit-attempt:claim-a",
    )
    assert update.publication is not None
    attacker = tmp_path / "attacker"
    attacker.write_bytes(b"attacker")
    real_replace = prepared_publication.os.replace
    injected = False

    def substitute_after_replace(source, target, *args, **kwargs) -> None:
        nonlocal injected
        real_replace(source, target, *args, **kwargs)
        if not injected and target == "runtime_checks.json":
            injected = True
            real_replace(
                attacker.name,
                target,
                src_dir_fd=kwargs["dst_dir_fd"],
                dst_dir_fd=kwargs["dst_dir_fd"],
            )

    monkeypatch.setattr(prepared_publication.os, "replace", substitute_after_replace)

    with pytest.raises(
        prepared_publication.FilePublicationConflict,
        match="published side-file inode changed",
    ):
        prepared_publication.commit_prepared_file(
            update.publication,
            authority_binding="work-unit-attempt:claim-a",
        )
    prepared_publication.discard_prepared_file(update.publication)

    assert (tmp_path / "runtime_checks.json").read_bytes() == b"attacker"
    assert list(tmp_path.glob(".runtime_checks.json.prepared.*")) == []


def test_side_file_commit_refuses_lock_contention_without_waiting(
    tmp_path: Path,
) -> None:
    first = runtime_check_publication.prepare_runtime_check_update(
        tmp_path,
        [_runtime_row("first")],
        authority_binding="work-unit-attempt:claim-a",
    )
    contender = runtime_check_publication.prepare_runtime_check_update(
        tmp_path,
        [_runtime_row("contender")],
        authority_binding="work-unit-attempt:claim-b",
    )
    assert first.publication is not None
    assert contender.publication is not None
    fcntl.flock(first.publication.lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(
            prepared_publication.FilePublicationConflict,
            match="already committing",
        ):
            prepared_publication.commit_prepared_file(
                contender.publication,
                authority_binding="work-unit-attempt:claim-b",
            )
    finally:
        fcntl.flock(first.publication.lock_descriptor, fcntl.LOCK_UN)
        prepared_publication.discard_prepared_file(first.publication)
        prepared_publication.discard_prepared_file(contender.publication)
    assert not (tmp_path / "runtime_checks.json").exists()


def test_question_preparations_cannot_overwrite_a_concurrent_append(
    tmp_path: Path,
) -> None:
    first = escalate.prepare_question(
        tmp_path,
        layer="1",
        question="First decision?",
        assumption="first assumption",
        affected_layers=["1"],
        authority_binding="work-unit-attempt:claim-a",
    )
    second = escalate.prepare_question(
        tmp_path,
        layer="2",
        question="Second decision?",
        assumption="second assumption",
        affected_layers=["2"],
        authority_binding="work-unit-attempt:claim-b",
    )
    assert first.update.publication is not None
    assert second.update.publication is not None

    escalate.commit_prepared_question(
        first,
        authority_binding="work-unit-attempt:claim-a",
    )
    with pytest.raises(
        prepared_publication.FilePublicationConflict,
        match="target changed after preparation",
    ):
        escalate.commit_prepared_question(
            second,
            authority_binding="work-unit-attempt:claim-b",
        )
    escalate.discard_prepared_question(second)

    questions = escalate.load(tmp_path)
    assert [row["question"] for row in questions] == ["First decision?"]


def test_wrong_attempt_binding_cannot_publish_prepared_side_file(
    tmp_path: Path,
) -> None:
    update = runtime_check_publication.prepare_runtime_check_update(
        tmp_path,
        [_runtime_row("bound")],
        authority_binding="work-unit-attempt:claim-a",
    )
    assert update.publication is not None
    with pytest.raises(
        prepared_publication.FilePublicationConflict,
        match="another authority binding",
    ):
        prepared_publication.commit_prepared_file(
            update.publication,
            authority_binding="work-unit-attempt:claim-b",
        )
    prepared_publication.discard_prepared_file(update.publication)
    assert not (tmp_path / "runtime_checks.json").exists()


def test_worklist_preparation_does_not_block_replan_and_stale_commit_is_discarded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unit, units, plan_hash, guard = _building_guard(tmp_path)
    prepare_started = Event()
    release_prepare = Event()
    replan_done = Event()
    failures: list[BaseException] = []
    original_write_all = prepared_publication._write_all

    def blocked_write(descriptor: int, payload: bytes) -> None:
        prepare_started.set()
        assert release_prepare.wait(5)
        original_write_all(descriptor, payload)

    monkeypatch.setattr(prepared_publication, "_write_all", blocked_write)

    async def no_blender_call(*_args, **_kwargs):
        raise AssertionError("worklist publication must not call Blender")

    _script_map, _find, worklist, *_rest = register_misc(
        session=object(),
        _call=no_blender_call,
        comparison_state={
            "unit_id": unit.id,
            "unit_hash": unit_state.unit_digest(unit),
        },
        comparison_locks={},
        shot_dir=tmp_path,
        layer_id="1",
        assets_dir=None,
        feedback_policy={},
        mutation_roles=(),
        scope_baseline=set(),
        unit_scope={},
        _black_frame_note=lambda _row: "",
        _black_search_stop=lambda: "",
        _register_candidate=lambda _row: None,
        selected_authority=guard.selected_authority,
        attempt_guard=guard,
    )

    def publish_worklist() -> None:
        try:
            asyncio.run(worklist.handler({"items": ["finish exact ticket"]}))
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
                trigger="revoke during worklist preparation",
                evidence=["fixture:side-file-race"],
                reopen={unit.id},
            )
        finally:
            replan_done.set()

    publication_thread = Thread(target=publish_worklist)
    replan_thread = Thread(target=replan)
    publication_thread.start()
    assert prepare_started.wait(2)
    replan_thread.start()
    try:
        assert replan_done.wait(2), "replan waited on unbounded worklist staging"
    finally:
        release_prepare.set()
    publication_thread.join(5)
    replan_thread.join(5)

    assert len(failures) == 1
    assert isinstance(failures[0], UnitAttemptAuthorityLost)
    target = worklists.unit_worklist_path(
        tmp_path,
        layer_id="1",
        unit_id=unit.id,
        unit_hash=unit_state.unit_digest(unit),
    )
    assert not target.exists()
    assert not list(target.parent.glob(f".{target.name}.prepared.*"))


def test_side_file_target_symlink_fails_before_preparation(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside-runtime.json"
    outside.write_text("[]\n", encoding="utf-8")
    (tmp_path / "runtime_checks.json").symlink_to(outside)
    try:
        with pytest.raises(
            prepared_publication.FilePublicationConflict,
            match="real regular file",
        ):
            runtime_check_publication.prepare_runtime_check_update(
                tmp_path,
                [_runtime_row("blocked")],
                authority_binding="work-unit-attempt:claim-a",
            )
        assert outside.read_text(encoding="utf-8") == "[]\n"
    finally:
        outside.unlink()
