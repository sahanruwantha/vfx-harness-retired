"""Flynn execution against actual attempt, replay and completion publishers.

The selected plan is seeded by the existing materialization fixture; planning quality
is outside this test. No unit verdict, evaluation or completion receipt is mocked.
"""

from __future__ import annotations

import asyncio
import hashlib
import json

import pytest

from tests.unit.test_plan_records import _candidate, _write, publish_current
from vfx_harness.agents.builder.attempt_guard import UnitAttemptAuthorityLost, UnitAttemptGuard
from vfx_harness.agents.builder.models import BuildUnpassed
from vfx_harness.agents.builder.unit_completion import complete_and_resolve_unit, resolve_completed_unit
from vfx_harness.blender.session import BlenderSession
from vfx_harness.domain.brief import Shot
from vfx_harness.observability import run_artifacts
from vfx_harness.observability.runid import RUN_ID
from vfx_harness.orchestration import unit_state, unit_state_claims
from vfx_harness.orchestration.authority_capsule_resolution import selected_layer_capsule_digest
from vfx_harness.orchestration.authority_selection import resolve_selected_authority
from vfx_harness.orchestration.builder_execution_fence import builder_execution_fence
from vfx_harness.orchestration.ledger import Milestone, load_layers

flynn = pytest.importorskip("flynn_agents_sdk", reason="Install the private .[flynn] migration extra")
flynn_unit = pytest.importorskip("vfx_harness.agents.builder.flynn_unit")


_PROGRAM = """import bpy
host = bpy.data.objects.new('unit_control', None)
bpy.context.scene.collection.objects.link(host)
host['bvfx_role'] = 'comp'
"""


def _authority(root, monkeypatch, expected=1):
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    _candidate(root)
    layers = json.loads((root / "layers.json").read_text())
    layer = layers["layers"][0]
    layer["evidence_domains"] = ["scene"]
    stage = layer["stages"][0]
    stage["evaluation"].pop("composition_context")
    stage["evaluation"]["temporal_evidence"] = "none"
    claim = stage["evaluation"]["claims"][0]
    claim.update(property="object_count", asserts="scene")
    _write(root / "layers.json", layers)
    _write(root / "scene_checks.json", {"schema": 2, "contracts": [{
        "id": "final-lock", "kind": "object_count", "roles": ["comp"],
        "owner_layer": "1", "fault_owner": "1", "activates_at": "1",
        "lifecycle": "layer", "axis": "final_lock", "op": "eq", "value": expected,
    }]})
    requirements = json.loads((root / "requirements.json").read_text())
    requirements["requirements"][0]["resolution"] = {
        "kind": "deferred_owner", "ids": [], "owner_layer": "1",
        "due": {"kind": "before_layer", "layer": "1"}, "evidence_domains": ["scene"],
    }
    _write(root / "requirements.json", requirements)
    _write(root / "obligations.json", {"schema": "vfx-harness.obligations/v1", "obligations": []})
    layout = run_artifacts.create(root, RUN_ID)
    publish_current(root, layout, outcome="clean_with_deferred")
    monkeypatch.setenv(run_artifacts.ENV, str(layout.root))
    selected = resolve_selected_authority(root)
    shot = Shot(root, {"frames": 240, "fps": 24, "resolution": [64, 64]}, "Fixture")
    layer = load_layers(shot, selected_authority=selected)["1"]
    unit = layer.stages[0]
    plan_hash = selected_layer_capsule_digest(root, "1", selected)
    unit_state.initialize(root, "1", layer.stages, plan_hash=plan_hash)
    kwargs = {
        "expected_plan_hash": plan_hash, "eligible_passed": set(), "completion_authorization": None,
        "run_id": layout.run_id, "selection_token": selected.selection_token, "reason": "scripted unit gate",
    }
    planning = unit_state_claims.claim_ready_unit_for_planning(root, "1", unit.id, layer.stages, **kwargs)
    attempt = unit_state_claims.claim_ready_unit_for_build(root, "1", unit.id, layer.stages, planning, **kwargs)
    guard = UnitAttemptGuard.bind(
        root, "1", unit, layer.stages, attempt, expected_plan_hash=plan_hash, selected_authority=selected,
    )
    return shot, layer, unit, selected, guard, layout


@pytest.mark.parametrize("expected,program", [
    (1, _PROGRAM), (2, _PROGRAM),
    (1, _PROGRAM.replace("'comp'", "'undeclared'")),
    (1, "raise RuntimeError('injected replay failure')\n"),
])
def test_flynn_unit_earns_completion_only_from_canonical_evidence(tmp_path, monkeypatch, expected, program):
    shot, layer, unit, selected, guard, layout = _authority(tmp_path, monkeypatch, expected)
    digest = hashlib.sha256(program.encode()).hexdigest()
    should_pass = expected == 1 and program == _PROGRAM
    calls = [
        flynn.ToolCall("inspect_unit", "{}"),
        flynn.ToolCall("write_candidate", json.dumps({"source": program})),
        flynn.ToolCall("probe_candidate", "{}"),
        flynn.ToolCall("freeze_candidate", json.dumps({"sha256": digest})),
    ]
    class PhaseAdapter:
        def __init__(self):
            self.scripted = flynn.ScriptedAdapter(calls)
            self.requests = 0

        async def generate(self, request):
            if self.requests:
                assert "inspect_unit" not in request.allowed_tools
                assert {"write_candidate", "probe_candidate", "freeze_candidate"} & set(request.allowed_tools)
            self.requests += 1
            return await self.scripted.generate(request)

    milestone = Milestone("1@lock", 240, "refs/a.png", "control exists")
    script_rel = unit.mutates.script_spans[0]
    with builder_execution_fence(tmp_path) as lease, BlenderSession(
        artifacts_dir=layout.scratch / "worker", cwd=tmp_path,
    ) as session:
        ledger = asyncio.run(flynn_unit.build_unit(
            shot, milestone, script_rel, [], session,
            inference=PhaseAdapter(), limits=flynn.RunLimits(5, 5, 4, 180),
            layer=layer, active_unit=unit, selected_authority=selected, attempt_guard=guard,
            fence_lease=lease, verbose=False,
        ))
        assert ledger.status(milestone) == ("passed" if should_pass else "failed")
        database = layout.checkpoints / "flynn" / f"{guard.claim.claim_id}.sqlite"
        with flynn.SQLiteRun.open(database) as run:
            assert run.read().revision == 0
            assert len(run.records()["operations"]) == 5
            assert run.remaining()["external"] == 0
            assert run.outcome() is not None
        if not should_pass:
            assert (tmp_path / script_rel).read_text() == program
            assert ledger._slot(milestone)["script_sha"] == digest[:16]
            assert unit_state.load(tmp_path, "1")["units"][unit.id]["status"] == "building"
            return
        # The executor's terminal SQLite record is not a completed VFX unit.
        assert unit_state.load(tmp_path, "1")["units"][unit.id]["status"] == "building"
        frozen = unit_state.freeze_checkpoint(
            tmp_path, "1", unit, active_contract_ids=(), candidate_hash=digest,
            settings_hash=hashlib.sha256(b"executable-only").hexdigest(), script_hash=digest,
            input_hash=guard.expected_plan_hash, layer_active_vis_ids=(),
            attempt=guard.claim, selection_token=selected.selection_token,
        )
        unit_state.transition(
            tmp_path, "1", unit.id, "evaluating", reason="canonical evaluation sealed",
            attempt=guard.claim, selection_token=selected.selection_token,
        )
        receipt = complete_and_resolve_unit(
            tmp_path, "1", unit, layer.stages, guard.claim,
            expected_plan_hash=guard.expected_plan_hash, selected_authority=selected,
            checkpoint_hash=frozen["units"][unit.id]["checkpoint"]["candidate_hash"],
        )
        assert receipt.script_hash == digest
        assert unit_state.load(tmp_path, "1")["units"][unit.id]["status"] == "passed"

        (tmp_path / script_rel).write_text(program + "\n# injected source drift\n")
        with pytest.raises(ValueError):
            resolve_completed_unit(
                tmp_path, "1", unit, layer.stages, receipt,
                expected_plan_hash=guard.expected_plan_hash, selected_authority=selected,
            )


@pytest.mark.parametrize(
    "fault", ["ungranted", "false_freeze", "revoked", "interrupted", "abstained", "unobserved_rewrite"],
)
def test_flynn_refuses_false_finish_stale_attempt_and_session_resume(tmp_path, monkeypatch, fault):
    shot, layer, unit, selected, guard, layout = _authority(tmp_path, monkeypatch)

    class FaultAdapter:
        calls = 0

        async def generate(self, request):
            self.calls += 1
            if fault == "abstained":
                return flynn.ToolCall("abstain", json.dumps({"reason": "No supported construction identified"}))
            if fault == "ungranted":
                return flynn.ToolCall("accept_unit", "{}")
            if fault == "false_freeze":
                return flynn.ToolCall("freeze_candidate", json.dumps({"sha256": "a" * 64}))
            if self.calls == 1:
                return flynn.ToolCall("write_candidate", json.dumps({"source": _PROGRAM}))
            if fault == "interrupted":
                raise asyncio.CancelledError()
            if fault == "unobserved_rewrite":
                return flynn.ToolCall("write_candidate", json.dumps({"source": "# unobserved replacement"}))
            unit_state_claims.release_unit_attempt(
                tmp_path, "1", unit.id, layer.stages, guard.claim,
                expected_plan_hash=guard.expected_plan_hash, selection_token=selected.selection_token,
                reason="injected exact claim revocation", evidence=["test:revocation"],
            )
            # Use a currently granted operation so the stale-attempt guard, rather
            # than the earlier SDK grant check, must refuse the revoked authority.
            return flynn.ToolCall("probe_candidate", "{}")

    adapter = FaultAdapter()
    exception = {
        "ungranted": flynn.ContractError, "false_freeze": flynn.ContractError,
        "revoked": UnitAttemptAuthorityLost, "interrupted": asyncio.CancelledError,
        "abstained": BuildUnpassed, "unobserved_rewrite": flynn.ContractError,
    }[fault]
    with builder_execution_fence(tmp_path) as lease, pytest.raises(exception):
        asyncio.run(flynn_unit.build_unit(
            shot, Milestone("1@lock", 240, "refs/a.png", "control exists"),
            unit.mutates.script_spans[0], [], object(),
            inference=adapter, limits=flynn.RunLimits(5, 5, 4, 180),
            layer=layer, active_unit=unit, selected_authority=selected, attempt_guard=guard,
            fence_lease=lease, verbose=False,
        ))
    assert not (tmp_path / unit.mutates.script_spans[0]).exists()
    slot = unit_state.load(tmp_path, "1")["units"][unit.id]
    assert slot["status"] != "passed" and not slot.get("completion_receipt")
    database = layout.checkpoints / "flynn" / f"{guard.claim.claim_id}.sqlite"
    with flynn.SQLiteRun.open(database) as run:
        assert run.read().revision == 0
        assert run.outcome() == ("unit_abstained" if fault == "abstained" else None)
        assert run.remaining()["inference"] == 5 - adapter.calls
    if fault in {"revoked", "interrupted", "unobserved_rewrite"}:
        candidate = layout.scratch / "unit-candidates" / f"{guard.claim.claim_id}.py"
        assert candidate.read_text() == _PROGRAM
    if fault == "interrupted":
        # Re-entering the same attempt must not mint another database/reset its spend.
        with builder_execution_fence(tmp_path) as lease, pytest.raises(FileExistsError):
            asyncio.run(flynn_unit.build_unit(
                shot, Milestone("1@lock", 240, "refs/a.png", "control exists"),
                unit.mutates.script_spans[0], [], object(),
                inference=adapter, limits=flynn.RunLimits(5, 5, 4, 180),
                layer=layer, active_unit=unit, selected_authority=selected, attempt_guard=guard,
                fence_lease=lease, verbose=False,
            ))
        assert adapter.calls == 2


@pytest.mark.parametrize("bad_program", ["# no control yet\n", "raise RuntimeError('injected candidate failure')\n"])
def test_flynn_candidate_revision_requires_measured_feedback(tmp_path, monkeypatch, bad_program):
    shot, layer, unit, selected, guard, layout = _authority(tmp_path, monkeypatch)
    repaired_digest = hashlib.sha256(_PROGRAM.encode()).hexdigest()
    scripted = flynn.ScriptedAdapter([
        flynn.ToolCall("write_candidate", json.dumps({"source": bad_program})),
        flynn.ToolCall("probe_candidate", "{}"),
        flynn.ToolCall("write_candidate", json.dumps({"source": _PROGRAM})),
        flynn.ToolCall("probe_candidate", "{}"),
        flynn.ToolCall("freeze_candidate", json.dumps({"sha256": repaired_digest})),
    ])

    class Adapter:
        calls = 0

        async def generate(self, request):
            if self.calls:
                assert "\n\nCurrent candidate: " in request.objective
                current = json.loads(request.objective.split("\n\nCurrent candidate: ", 1)[1])
                expected_source = bad_program if self.calls < 3 else _PROGRAM
                assert current["source"] == expected_source
                assert current["sha256"] == hashlib.sha256(expected_source.encode()).hexdigest()
            if self.calls in (1, 3):
                assert set(request.allowed_tools) == {"probe_candidate", "abstain"}
            if self.calls == 2:
                assert "probe_candidate" not in request.allowed_tools
                assert "write_candidate" in request.allowed_tools
                feedback = json.loads(request.observation)
                assert feedback["canonical"] == "failed"
                if bad_program.startswith("raise"):
                    assert "injected candidate failure" in feedback["replay_errors"][0]["message"]
            self.calls += 1
            return await scripted.generate(request)

    milestone = Milestone("1@lock", 240, "refs/a.png", "control exists")
    with builder_execution_fence(tmp_path) as lease, BlenderSession(
        artifacts_dir=layout.scratch / "worker", cwd=tmp_path,
    ) as session:
        ledger = asyncio.run(flynn_unit.build_unit(
            shot, milestone, unit.mutates.script_spans[0], [], session,
            inference=Adapter(), limits=flynn.RunLimits(6, 6, 5, 180),
            layer=layer, active_unit=unit, selected_authority=selected, attempt_guard=guard,
            fence_lease=lease, verbose=False,
        ))
    assert ledger.status(milestone) == "passed"
    assert (tmp_path / unit.mutates.script_spans[0]).read_text() == _PROGRAM
    with flynn.SQLiteRun.open(layout.checkpoints / "flynn" / f"{guard.claim.claim_id}.sqlite") as run:
        assert run.read().revision == 0
        assert len(run.records()["operations"]) == 6
        assert run.outcome().startswith("unit_evaluation_receipt:")


def test_flynn_required_candidate_context_refuses_overflow_before_inference(tmp_path, monkeypatch):
    shot, layer, unit, selected, guard, layout = _authority(tmp_path, monkeypatch)
    source = "#" + "x" * 12000

    class Adapter:
        calls = 0

        async def generate(self, request):
            self.calls += 1
            assert self.calls == 1, "oversized required source must refuse before another model call"
            return flynn.ToolCall("write_candidate", json.dumps({"source": source}))

    adapter = Adapter()
    with builder_execution_fence(tmp_path) as lease, pytest.raises(ValueError, match="Required context item"):
        asyncio.run(flynn_unit.build_unit(
            shot, Milestone("1@lock", 240, "refs/a.png", "control exists"),
            unit.mutates.script_spans[0], [], object(),
            inference=adapter, limits=flynn.RunLimits(4, 4, 3, 180),
            layer=layer, active_unit=unit, selected_authority=selected, attempt_guard=guard,
            fence_lease=lease, verbose=False,
        ))
    assert adapter.calls == 1
    with flynn.SQLiteRun.open(layout.checkpoints / "flynn" / f"{guard.claim.claim_id}.sqlite") as run:
        assert run.remaining()["inference"] == 3
        assert len(run.records()["operations"]) == 1
        assert run.outcome() is None
    assert not (tmp_path / unit.mutates.script_spans[0]).exists()
