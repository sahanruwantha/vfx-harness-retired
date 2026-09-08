"""Builder dispatch refuses stale claim/candidate authority before external spend."""

from __future__ import annotations

import asyncio
import hashlib
import json

import flynn_agents_sdk as flynn
import pytest

from tests.integration.test_flynn_unit_lifecycle import _PROGRAM, _authority
from vfx_harness.agents.builder import candidate_script, flynn_unit, verify
from vfx_harness.agents.builder.attempt_guard import UnitAttemptAuthorityLost
from vfx_harness.orchestration import unit_state_claims
from vfx_harness.orchestration.builder_execution_fence import builder_execution_fence
from vfx_harness.orchestration.ledger import Milestone
from vfx_harness.orchestration.plan_bundle_integrity import PlanPublicationError


def execute(bound, adapter):
    shot, layer, unit, selected, guard, _ = bound
    with builder_execution_fence(shot.folder) as lease:
        return asyncio.run(flynn_unit.build_unit(
            shot, Milestone("1@lock", 240, "refs/a.png", "control exists"),
            unit.mutates.script_spans[0], [], object(), inference=adapter,
            limits=flynn.RunLimits(6, 6, 5, 180), layer=layer, active_unit=unit,
            selected_authority=selected, attempt_guard=guard, fence_lease=lease, verbose=False,
        ))


def database(bound):
    return bound[5].checkpoints / "flynn" / f"{bound[4].claim.claim_id}.sqlite"


def revoke(bound):
    shot, layer, unit, selected, guard, _ = bound
    unit_state_claims.release_unit_attempt(
        shot.folder, "1", unit.id, layer.stages, guard.claim,
        expected_plan_hash=guard.expected_plan_hash, selection_token=selected.selection_token,
        reason="injected exact claim revocation", evidence=["test:revocation"],
    )


@pytest.mark.parametrize("mutation", ["revoked", "foreign_write", "foreign_replace", "foreign_delete", "foreign_link"])
def test_changed_authority_during_inference_preserves_external_budget(tmp_path, monkeypatch, mutation):
    bound = _authority(tmp_path, monkeypatch)
    candidate = candidate_script.exact_candidate_script_path(tmp_path, bound[4])
    first_call = mutation == "foreign_write"

    class Adapter:
        calls = 0

        async def generate(self, request):
            self.calls += 1
            if self.calls == 1 and not first_call:
                return flynn.InferenceResult.scripted(
                    flynn.ToolCall("write_candidate", json.dumps({"source": _PROGRAM})),
                )
            if mutation == "revoked":
                revoke(bound)
            elif mutation == "foreign_delete":
                candidate.unlink()
            elif mutation == "foreign_link":
                candidate.unlink()
                other = tmp_path / "foreign.py"
                other.write_text("# external file\n")
                candidate.symlink_to(other)
            else:
                candidate.parent.mkdir(parents=True, exist_ok=True)
                candidate.write_text("# foreign candidate\n")
            return flynn.InferenceResult.scripted(flynn.ToolCall(
                "write_candidate" if first_call else "probe_candidate",
                json.dumps({"source": _PROGRAM}) if first_call else "{}",
            ))

    with pytest.raises((ValueError, UnitAttemptAuthorityLost, PlanPublicationError)):
        execute(bound, Adapter())
    if mutation in {"foreign_write", "foreign_replace"}:
        assert candidate.read_text() == "# foreign candidate\n"
    elif mutation == "foreign_delete":
        assert not candidate.exists()
    elif mutation == "foreign_link":
        assert candidate.is_symlink() and candidate.read_text() == "# external file\n"
    else:
        assert candidate.read_text() == _PROGRAM
    dispatched = 0 if first_call else 1
    with flynn.SQLiteRun.open(database(bound)) as run:
        assert run.remaining()["tool"] == 6 - dispatched
        assert run.remaining()["external"] == 5 - dispatched
        assert len(run.records()["guard_decisions"]) == dispatched
        assert run.records()["commits"] == []
        assert run.usage_summary()["scripted_invocations"] == dispatched + 1
    assert not (tmp_path / bound[2].mutates.script_spans[0]).exists()


def test_preexisting_candidate_refuses_before_inference_or_ledger_write(tmp_path, monkeypatch):
    bound = _authority(tmp_path, monkeypatch)
    candidate = candidate_script.exact_candidate_script_path(tmp_path, bound[4])
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_text("# unowned predecessor\n")
    before = (tmp_path / "shot.json").read_bytes()
    monkeypatch.setattr(flynn_unit.unit_runtime, "start_unit_runtime",
                        lambda *a, **kw: pytest.fail("unowned candidate reached ledger mutation"))
    with pytest.raises(ValueError, match="outside this attempt's writes"):
        execute(bound, flynn.ScriptedAdapter([]))
    assert candidate.read_text() == "# unowned predecessor\n"
    assert (tmp_path / "shot.json").read_bytes() == before
    with flynn.SQLiteRun.open(database(bound)) as run:
        assert run.remaining() == {"inference": 6, "tool": 6, "external": 5}
        assert run.records()["operations"] == []


def test_foreign_candidate_after_freeze_refuses_before_scripted_canonical_spend(tmp_path, monkeypatch):
    bound = _authority(tmp_path, monkeypatch)
    candidate = candidate_script.exact_candidate_script_path(tmp_path, bound[4])
    probes = []

    async def probe(*args, **kwargs):
        probes.append(args)
        return "passed"

    monkeypatch.setattr(verify, "_verify_script", probe)
    evaluate = flynn_unit._ObservationEvaluator.evaluate

    async def replace_after_freeze(self, operation):
        result = await evaluate(self, operation)
        if operation.call.name == "freeze_candidate":
            candidate.write_text("# foreign frozen replacement\n")
        return result

    monkeypatch.setattr(flynn_unit._ObservationEvaluator, "evaluate", replace_after_freeze)
    adapter = flynn.ScriptedAdapter([
        flynn.ToolCall("write_candidate", json.dumps({"source": _PROGRAM})),
        flynn.ToolCall("probe_candidate", "{}"),
        flynn.ToolCall("freeze_candidate", json.dumps({"sha256": hashlib.sha256(_PROGRAM.encode()).hexdigest()})),
    ])
    with pytest.raises(ValueError, match="changed outside this attempt"):
        execute(bound, adapter)
    assert len(probes) == 1 and candidate.read_text() == "# foreign frozen replacement\n"
    with flynn.SQLiteRun.open(database(bound)) as run:
        assert run.remaining() == {"inference": 3, "tool": 3, "external": 3}
        assert len(run.records()["operations"]) == len(run.records()["guard_decisions"]) == 3
    assert not (tmp_path / bound[2].mutates.script_spans[0]).exists()


def test_writer_cannot_adopt_bytes_other_than_its_requested_source(tmp_path, monkeypatch):
    bound = _authority(tmp_path, monkeypatch)
    candidate = candidate_script.exact_candidate_script_path(tmp_path, bound[4])

    def substituted_write(*args):
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_text("# substituted during publication\n")

    monkeypatch.setattr(candidate_script, "write_scratch_candidate", substituted_write)
    with pytest.raises(ValueError, match="changed outside this attempt"):
        execute(bound, flynn.ScriptedAdapter([
            flynn.ToolCall("write_candidate", json.dumps({"source": _PROGRAM})),
        ]))
    assert candidate.read_text() == "# substituted during publication\n"
    with flynn.SQLiteRun.open(database(bound)) as run:
        assert run.pending().stage == "dispatched"
        assert run.latest_observation() is None
        assert run.remaining() == {"inference": 5, "tool": 5, "external": 4}
        assert run.records()["commits"] == []


def test_canonical_dispatch_rechecks_claim_after_scripted_inference(tmp_path, monkeypatch):
    bound = _authority(tmp_path, monkeypatch)
    probes = []

    async def probe(*args, **kwargs):
        probes.append(args)
        return "passed"

    monkeypatch.setattr(verify, "_verify_script", probe)
    adapter = flynn.ScriptedAdapter([
        flynn.ToolCall("write_candidate", json.dumps({"source": _PROGRAM})),
        flynn.ToolCall("probe_candidate", "{}"),
        flynn.ToolCall("freeze_candidate", json.dumps({"sha256": hashlib.sha256(_PROGRAM.encode()).hexdigest()})),
    ])
    scripted_class = flynn.ScriptedAdapter

    class RevokeCanonical:
        def __init__(self, calls):
            self.scripted = scripted_class(calls)

        async def generate(self, request):
            assert request.allowed_tools == ("canonical_replay",)
            result = await self.scripted.generate(request)
            revoke(bound)
            return result

    monkeypatch.setattr(flynn, "ScriptedAdapter", RevokeCanonical)
    with pytest.raises(UnitAttemptAuthorityLost):
        execute(bound, adapter)
    assert len(probes) == 1
    with flynn.SQLiteRun.open(database(bound)) as run:
        assert run.remaining() == {"inference": 2, "tool": 3, "external": 3}
        assert len(run.records()["operations"]) == 4
        assert len(run.records()["guard_decisions"]) == 3
        assert run.records()["commits"] == []
    assert not (tmp_path / bound[2].mutates.script_spans[0]).exists()
