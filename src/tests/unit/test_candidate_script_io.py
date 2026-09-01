from __future__ import annotations

from threading import Event, Thread
from types import SimpleNamespace

from tests.architecture.test_staged_architecture import _unit
from tests.unit_attempt_fixtures import claim_for_build, legacy_apply_replan
from vfx_harness.agents.builder import candidate_script
from vfx_harness.agents.builder.attempt_guard import (
    UnitAttemptAuthorityLost,
    UnitAttemptGuard,
)
from vfx_harness.orchestration import unit_state
from vfx_harness.orchestration.authority_selection_transaction import (
    AuthoritySelectionToken,
)


def test_replan_does_not_wait_for_inert_candidate_write(
    tmp_path,
    monkeypatch,
) -> None:
    token = AuthoritySelectionToken(0, None, 0, None)
    unit = _unit("hero")
    units = (unit,)
    plan_hash = "c" * 64
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
    candidate = candidate_script.exact_candidate_script_path(tmp_path, guard)
    write_started = Event()
    release_write = Event()
    replan_done = Event()
    failures: list[BaseException] = []

    def blocked_write(_folder, path, payload):
        write_started.set()
        assert release_write.wait(5)
        target = path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)

    monkeypatch.setattr(
        candidate_script,
        "durable_replace_file_bytes",
        blocked_write,
    )

    def write() -> None:
        try:
            candidate_script.write_scratch_candidate(
                tmp_path,
                candidate,
                "# stale scratch only\n",
                guard,
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
                trigger="revoke during scratch candidate write",
                evidence=["fixture:replan"],
                reopen={unit.id},
            )
        finally:
            replan_done.set()

    write_thread = Thread(target=write)
    replan_thread = Thread(target=replan)
    write_thread.start()
    assert write_started.wait(2)
    replan_thread.start()
    try:
        assert replan_done.wait(2), "replan waited on inert scratch candidate I/O"
    finally:
        release_write.set()
    write_thread.join(5)
    replan_thread.join(5)

    assert len(failures) == 1
    assert isinstance(failures[0], UnitAttemptAuthorityLost)
    assert candidate.read_text(encoding="utf-8") == "# stale scratch only\n"
    assert not (tmp_path / "build/units/01/hero.py").exists()
