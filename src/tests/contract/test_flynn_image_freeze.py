"""Freeze and canonical dispatch recheck current payment evidence before external spend."""

import asyncio
import hashlib
import json

import flynn_agents_sdk as flynn
import pytest

from tests.integration.test_flynn_image_unit_lifecycle import PROGRAM, image_authority
from tests.integration.test_flynn_unit_lifecycle import _authority
from tests.unit.test_plan_records import _unit_provides
from vfx_harness.agents.builder import flynn_unit
from vfx_harness.orchestration.builder_execution_fence import builder_execution_fence
from vfx_harness.orchestration.ledger import Milestone


@pytest.mark.parametrize('change', ['unpaid', 'during_probe', 'before_freeze', 'after_freeze'])
def test_changed_or_unpaid_image_evidence_never_reaches_publication(tmp_path, monkeypatch, change):
    shot, layer, unit, selected, guard, layout = _authority(tmp_path, monkeypatch, configure=image_authority)
    rows = [] if change == 'unpaid' else [
        {'id': 'image-gap', 'frame': 240, 'axis': 'final_lock', 'metric': 'region_mean'},
    ]
    # Isolate the dispatch boundary. The real payment reader and canonical receipt
    # publishers are exercised by the Blender lifecycle gate, not certified here.
    monkeypatch.setattr(flynn_unit.checks, 'load_image_contract_payment_rows', lambda *args, **kwargs: list(rows))

    def mutate():
        rows[0] = {**rows[0], 'lo': 100}

    async def probe(*args, **kwargs):
        if change == 'during_probe':
            mutate()
        return 'passed'

    monkeypatch.setattr(flynn_unit.verify, '_verify_script', probe)
    evaluate = flynn_unit._ObservationEvaluator.evaluate

    async def after_freeze(self, candidate):
        result = await evaluate(self, candidate)
        if change == 'after_freeze' and candidate.call.name == 'freeze_candidate':
            mutate()
        return result

    monkeypatch.setattr(flynn_unit._ObservationEvaluator, 'evaluate', after_freeze)
    calls = iter([
        flynn.ToolCall('write_candidate', json.dumps({'source': PROGRAM})),
        flynn.ToolCall('probe_candidate', '{}'),
        flynn.ToolCall('freeze_candidate', json.dumps({'sha256': hashlib.sha256(PROGRAM.encode()).hexdigest()})),
    ])

    class Adapter:
        async def generate(self, request):
            call = next(calls)
            if call.name == 'freeze_candidate':
                if change == 'unpaid':
                    assert 'freeze_candidate' not in request.allowed_tools
                if change == 'before_freeze':
                    assert 'freeze_candidate' in request.allowed_tools
                    mutate()
            return await flynn.ScriptedAdapter([call]).generate(request)

    with builder_execution_fence(tmp_path) as lease, pytest.raises(
        (ValueError, flynn.ContractError), match=r'not granted|[Ii]mage payments changed',
    ):
        asyncio.run(flynn_unit.build_unit(
            shot, Milestone('1@lock', 240, 'refs/a.png', 'image freeze boundary'),
            unit.mutates.script_spans[0], [], object(), inference=Adapter(), limits=flynn.RunLimits(8, 8, 7, 180),
            layer=layer, active_unit=unit, selected_authority=selected, attempt_guard=guard,
            fence_lease=lease, verbose=False,
        ))
    assert not (tmp_path / unit.mutates.script_spans[0]).exists()
    with flynn.SQLiteRun.open(layout.checkpoints / 'flynn' / f'{guard.claim.claim_id}.sqlite') as run:
        assert run.read().revision == 0
        assert not run.records()['commits']
        assert run.remaining()['external'] == 5  # Candidate write and probe only.


def test_fixture_projects_only_global_capabilities():
    assert _unit_provides({'stages': [{'mutates': {'roles': ['surface']},
                                     'provides': ['geometry', 'illumination', 'camera']}]}) == {'camera': ['surface']}
