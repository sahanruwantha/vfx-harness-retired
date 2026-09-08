"""Native payment transport shares the executable VFX operation and exact attempt."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys

import flynn_agents_sdk as flynn
import pytest

from tests.integration.test_flynn_unit_lifecycle import _authority
from tests.unit.test_image_payment_provenance import _payment_fixture
from vfx_harness.agents.builder import flynn_image_checks, flynn_unit
from vfx_harness.evidence import image_check_operation
from vfx_harness.observability import prepared_publication
from vfx_harness.orchestration.builder_execution_fence import builder_execution_fence


def _inputs(root, guard=None, layout=None):
    row, candidate = _payment_fixture(root)
    payment = row['payment']
    state = {
        'unit_id': payment['unit_id'], 'unit_hash': payment['unit_hash'],
        'parent_chain_hash': payment['parent_chain_hash'],
        'image_artifacts': {'image:after': {**payment['candidate'], 'role': 'live_candidate',
                                           'run_id': payment['run_id']}},
        'image_adversaries': {40: payment['adversary']},
    }
    if guard is not None:
        state.update(unit_id=guard.claim.unit_id, unit_hash=guard.claim.unit_digest)
        for record in [state['image_artifacts']['image:after'], state['image_adversaries'][40]]:
            source = root / record['path']
            target = layout.evidence / 'renders' / source.name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
            record.update(path=target.relative_to(root).as_posix(), run_id=layout.run_id,
                          unit_id=guard.claim.unit_id, parent_chain_hash=state['parent_chain_hash'])
    args = {'checks': [{'id': 'form-look-f40', 'metric': 'region_mean', 'op': '>=',
                        'lo': 50, 'frame': 40, 'axis': 'form', 'regions': {'r': [0, 0, 1, 1]},
                        'ref': 'refs/a.png'}], 'after_handle': 'image:after'}
    return args, state, candidate


def test_shared_operation_imports_without_claude():
    code = '''
import sys
class BlockClaude:
    def find_spec(self, fullname, *args):
        if fullname == "claude_agent_sdk" or fullname.startswith("claude_agent_sdk."):
            raise AssertionError(fullname)
sys.meta_path.insert(0, BlockClaude())
from vfx_harness.evidence.image_check_operation import propose_checks
assert callable(propose_checks)
'''
    subprocess.run([sys.executable, '-c', code], check=True, capture_output=True, text=True)


@pytest.mark.parametrize('change', ['path', 'tampered', 'settings', 'too_many', 'unknown'])
def test_invalid_payment_cannot_publish(tmp_path, change):
    args, state, candidate = _inputs(tmp_path)
    (tmp_path / 'layers.json').write_text(json.dumps({'schema': 4, 'layers': [{'id': '2', 'judge': []}]}))
    if change == 'path':
        args['after_handle'] = str(candidate)
    elif change == 'tampered':
        candidate.write_bytes(b'changed')
    elif change == 'settings':
        state['image_artifacts']['image:after']['mode'] = 'solid'
    elif change == 'too_many':
        args['checks'] *= 21
    else:
        args['before'] = 'invented adversary'

    def forbidden(*args):
        pytest.fail('invalid evidence published')

    if change in {'too_many', 'unknown'}:
        with pytest.raises(ValueError, match='propose_checks'):
            image_check_operation.propose_checks(
                args, shot_dir=tmp_path, layer_id='2', comparison_state=state,
                selected_authority=None, prepare_and_publish=forbidden,
            )
    else:
        result = image_check_operation.propose_checks(
            args, shot_dir=tmp_path, layer_id='2', comparison_state=state,
            selected_authority=None, prepare_and_publish=forbidden,
        )
        assert result.kept_ids == ()
        assert 'REJECTED' in result.message
    assert not (tmp_path / 'runtime_checks.json').exists()


def test_native_payment_records_observation_without_state_commit(tmp_path, monkeypatch):
    shot, _, _, _, guard, layout = _authority(tmp_path, monkeypatch)
    args, state, _ = _inputs(tmp_path, guard, layout)
    state.update(unit_id=guard.claim.unit_id, unit_hash=guard.claim.unit_digest)
    calls = []
    with builder_execution_fence(shot.folder) as lease:
        capability = flynn_image_checks.image_check_tool(
            attempt_guard=guard, fence_lease=lease, comparison_state=state,
            check_candidate=lambda: calls.append('checked'),
        )
        database = layout.checkpoints / 'native-image.sqlite'
        with flynn.SQLiteRun.create(database, run_id='image-test', initial_state='{}',
                                   limits=flynn.RunLimits(1, 1, 1, 30)) as run:
            runtime = flynn.Runtime(
                inference=flynn.ScriptedAdapter([flynn.ToolCall('propose_checks', json.dumps(args))]),
                tools=flynn.ToolBroker([capability.tool]), evaluator=flynn_unit._ObservationEvaluator(),
                run=run, grants=('propose_checks',), guards=(capability.guard,),
            )
            step = asyncio.run(runtime.step('Pay the measured image check.'))
            result = flynn.ToolResult.from_json(step.candidate.output)
            data = json.loads(result.data_json)
            assert data['kept_ids'] == ['form-look-f40']
            assert data['accepted'] is False
            assert data['attempt'] == guard.claim.as_dict()
            assert run.read().revision == 0
    rows = json.loads((tmp_path / 'runtime_checks.json').read_text())
    assert rows[0]['payment']['unit_hash'] == guard.claim.unit_digest
    assert len(calls) >= 4


def test_changed_candidate_discards_prepared_payment(tmp_path, monkeypatch):
    shot, _, _, _, guard, layout = _authority(tmp_path, monkeypatch)
    args, state, _ = _inputs(tmp_path, guard, layout)
    state.update(unit_id=guard.claim.unit_id, unit_hash=guard.claim.unit_digest)
    changed = False
    original = prepared_publication.prepare_file_update

    def prepare(*a, **kw):
        nonlocal changed
        result = original(*a, **kw)
        changed = True
        return result

    def check():
        if changed:
            raise ValueError('candidate changed')

    monkeypatch.setattr(prepared_publication, 'prepare_file_update', prepare)
    with builder_execution_fence(shot.folder) as lease:
        cap = flynn_image_checks.image_check_tool(
            attempt_guard=guard, fence_lease=lease, comparison_state=state, check_candidate=check,
        )
        with pytest.raises(ValueError, match='candidate changed'):
            asyncio.run(cap.tool.execute(json.dumps(args)))
    assert not (tmp_path / 'runtime_checks.json').exists()


@pytest.mark.parametrize('change', ['unit', 'run', 'parent', 'artifact'])
def test_native_refuses_foreign_or_changed_image_state(tmp_path, monkeypatch, change):
    shot, _, _, _, guard, layout = _authority(tmp_path, monkeypatch)
    args, state, _ = _inputs(tmp_path, guard, layout)
    record = state['image_artifacts']['image:after']
    with builder_execution_fence(shot.folder) as lease:
        cap = flynn_image_checks.image_check_tool(
            attempt_guard=guard, fence_lease=lease, comparison_state=state, check_candidate=lambda: None,
        )
        if change == 'artifact':
            (tmp_path / record['path']).write_bytes(b'foreign bytes')
        else:
            field = {'unit': 'unit_id', 'run': 'run_id', 'parent': 'parent_chain_hash'}[change]
            record[field] = 'foreign'
        with pytest.raises(ValueError, match='image'):
            asyncio.run(cap.tool.execute(json.dumps(args)))
    assert not (tmp_path / 'runtime_checks.json').exists()
