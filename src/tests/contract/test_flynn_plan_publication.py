"""Native planning publication preserves the existing external authority boundary."""

from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import sys
from dataclasses import replace

import flynn_agents_sdk as flynn
import pytest

from tests.unit.test_unit_plan_publication_stamp import _CONTENT, _selected_shot
from vfx_harness.agents import flynn_plan_tools
from vfx_harness.orchestration import authority_selection, unit_plan_content
from vfx_harness.orchestration.authority_selection import resolve_selected_authority
from vfx_harness.orchestration.layer_plans import (
    validate_work_unit_plan_authority,
    work_unit_plan_authority_path,
)
from vfx_harness.orchestration.work_unit_plan_transaction import work_unit_plan_transaction


class ObservationOnly:
    async def evaluate(self, candidate):
        return flynn.Evaluation(candidate, "fixture", "transport", flynn.Verdict.SATISFIED,
                                "External integrity and gate readers own acceptance")


@pytest.fixture
def bound(tmp_path):
    root = _selected_shot(tmp_path)
    selected = resolve_selected_authority(root)
    target = root / "plans" / "units" / "aim_target.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    return root, selected, target


def execute(bound, arguments, *, check=lambda: None, after_inference=lambda: None, rollback=False):
    root, selected, target = bound

    class Adapter:
        async def generate(self, request):
            after_inference()
            return flynn.InferenceResult.scripted(flynn.ToolCall("publish_unit_plan", json.dumps(arguments)))

    async def run():
        async with work_unit_plan_transaction(target, work_unit_plan_authority_path(target)) as transaction:
            transaction.claim_current()
            tool, guard = flynn_plan_tools.unit_plan_publication(
                shot_folder=root, target=target, selected_authority=selected, check_current=check,
                transaction=transaction,
            )
            with flynn.SQLiteRun.create(root / "native.sqlite", run_id="native-plan",
                                       initial_state="unaccepted", limits=flynn.RunLimits(1, 1, 1)) as journal:
                session = flynn.Session(
                    inference=Adapter(), tools=flynn.ToolBroker([tool]), evaluator=ObservationOnly(),
                    run=journal, grants=(tool.name,), guards=(guard,),
                    policy=lambda view: (flynn.SessionStop("content recorded; terminal gate remains due")
                                         if view.completed_steps
                                         else flynn.SessionStep("Write unit plan", (tool.name,))),
                )
                try:
                    await session.execute()
                    return flynn.ToolResult.from_json(journal.latest_observation())
                finally:
                    if rollback:
                        transaction.rollback()
    return asyncio.run(run())


def test_native_publication_records_integrity_without_committing_or_attesting_gate(bound):
    root, selected, target = bound
    result = execute(bound, {"content": _CONTENT})
    data = json.loads(result.data_json)
    assert data["path"] == target.relative_to(root).as_posix()
    assert data["sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()
    stamp = work_unit_plan_authority_path(target).read_bytes()
    assert data["integrity_stamp_sha256"] == hashlib.sha256(stamp).hexdigest()
    assert data["selection"] == selected.selection_token.to_dict()
    assert data["gate_attested"] is False
    validate_work_unit_plan_authority(root, target, require_gate=False, selected_authority=selected)
    with pytest.raises(ValueError, match="clean-gate attestation"):
        validate_work_unit_plan_authority(root, target, require_gate=True, selected_authority=selected)
    with flynn.SQLiteRun.open(root / "native.sqlite") as run:
        assert run.records()["commits"] == []
        assert run.read().value == "unaccepted"
        assert run.remaining() == {"inference": 0, "tool": 0, "external": 0}
        assert flynn.SessionTermination.from_json(run.outcome()).kind == "stopped"


@pytest.mark.parametrize("arguments", [
    {"content": _CONTENT, "path": "plans/other.md"},
    {"content": "too short"},
    {"content": "x" * 24001},
    {"content": "line\n" * 160},
    {"content": 123},
])
def test_invalid_content_never_dispatches_or_replaces_plan(bound, arguments):
    root, _, target = bound
    prior = target.read_bytes() if target.exists() else None
    with pytest.raises(flynn.ProposalRejected):
        execute(bound, arguments)
    assert (target.read_bytes() if target.exists() else None) == prior
    with flynn.SQLiteRun.open(root / "native.sqlite") as run:
        assert run.remaining()["tool"] == run.remaining()["external"] == 1


def test_attempt_expiry_during_inference_refuses_before_external_spend(bound):
    current = True

    def expire():
        nonlocal current
        current = False

    def check():
        if not current:
            raise ValueError("planning attempt superseded")

    with pytest.raises(ValueError, match="planning attempt superseded"):
        execute(bound, {"content": _CONTENT}, check=check, after_inference=expire)
    with flynn.SQLiteRun.open(bound[0] / "native.sqlite") as run:
        assert run.remaining()["tool"] == run.remaining()["external"] == 1


def test_writer_failure_retains_uncertain_effect_and_does_not_retry(bound, monkeypatch):
    calls = []

    def fail(*args, **kwargs):
        calls.append(args)
        raise OSError("injected sidecar publication failure")

    monkeypatch.setattr(unit_plan_content, "publish_unit_plan_content", fail)
    with pytest.raises(OSError, match="injected"):
        execute(bound, {"content": _CONTENT})
    assert len(calls) == 1
    with flynn.SQLiteRun.open(bound[0] / "native.sqlite") as run:
        assert run.pending().stage == "dispatched"
        assert run.remaining()["external"] == 0
        assert run.records()["commits"] == []


@pytest.mark.parametrize("target_kind", ["outside", "other_surface", "symlink"])
def test_target_is_harness_owned_and_links_refuse(bound, tmp_path, target_kind):
    root, selected, target = bound
    if target_kind == "outside":
        target = root.parent / "outside.md"
    elif target_kind == "other_surface":
        target = root / "brief.md"
    else:
        target = root / "plans" / "linked.md"
        target.symlink_to(root / "brief.md")
    prior = (root / "brief.md").read_bytes()
    with pytest.raises((ValueError, RuntimeError)):
        execute((root, selected, target), {"content": _CONTENT})
    assert (root / "brief.md").read_bytes() == prior


def test_native_planning_imports_without_claude():
    code = '''
import sys
class BlockClaude:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith("claude_agent_sdk"):
            raise AssertionError("native planning imported Claude")
sys.meta_path.insert(0, BlockClaude())
from vfx_harness.agents import flynn_plan_tools
'''
    subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, text=True)


def test_changed_selected_authority_refuses_before_external_spend(bound, monkeypatch):
    root, selected, _ = bound
    successor = replace(selected, selection_token=replace(
        selected.selection_token, plan_revision=selected.selection_token.plan_revision + 1,
    ))

    def change_selection():
        monkeypatch.setattr(authority_selection, "resolve_selected_authority", lambda _: successor)

    with pytest.raises(ValueError, match="selection"):
        execute(bound, {"content": _CONTENT}, after_inference=change_selection)
    with flynn.SQLiteRun.open(root / "native.sqlite") as run:
        assert run.remaining()["external"] == 1


def test_partial_pair_failure_is_rolled_back_only_by_vfx_owner(bound, monkeypatch):
    root, selected, target = bound
    unit_plan_content.publish_unit_plan_content(root, target, _CONTENT, selected_authority=selected)
    sidecar = work_unit_plan_authority_path(target)
    before = (target.read_bytes(), sidecar.read_bytes())

    def fail_stamp(*args, **kwargs):
        raise OSError("injected failure after content write")

    monkeypatch.setattr(unit_plan_content.layer_plans, "stamp_work_unit_plan", fail_stamp)
    with pytest.raises(OSError, match="after content write"):
        execute(bound, {"content": _CONTENT + "changed instruction\n"}, rollback=True)
    assert (target.read_bytes(), sidecar.read_bytes()) == before
    with flynn.SQLiteRun.open(root / "native.sqlite") as run:
        assert run.pending().stage == "dispatched"
        assert run.records()["commits"] == []
        assert run.remaining()["external"] == 0


def test_tool_requires_transaction_for_exact_target(bound):
    root, selected, target = bound

    async def check():
        async with work_unit_plan_transaction(target, work_unit_plan_authority_path(target)) as transaction:
            transaction.claim_current()
            with pytest.raises(ValueError, match="exact target"):
                flynn_plan_tools.unit_plan_publication(
                    shot_folder=root, target=target.with_name("other.md"), selected_authority=selected,
                    check_current=lambda: None, transaction=transaction,
                )

    asyncio.run(check())


def test_newer_writer_during_inference_is_preserved_without_dispatch(bound):
    root, _, target = bound
    replacement = b"a newer writer owns this file\n"

    def replace_plan():
        target.write_bytes(replacement)

    with pytest.raises(ValueError, match="newer writer"):
        execute(bound, {"content": _CONTENT}, after_inference=replace_plan)
    assert target.read_bytes() == replacement
    with flynn.SQLiteRun.open(root / "native.sqlite") as run:
        assert run.remaining()["tool"] == run.remaining()["external"] == 1
