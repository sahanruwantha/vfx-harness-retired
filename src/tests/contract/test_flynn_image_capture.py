"""Cold image observations bind candidates, priors and immutable evidence."""

from __future__ import annotations

import asyncio
import json

import flynn_agents_sdk as flynn
import pytest
from PIL import Image

from tests.integration.test_flynn_unit_lifecycle import _authority
from vfx_harness.agents.builder import candidate_script, flynn_image_capture, flynn_unit
from vfx_harness.orchestration.builder_execution_fence import builder_execution_fence
from vfx_harness.orchestration.plan_bundle_integrity import digest


class Session:
    def __init__(self, layout):
        self.calls = []
        self.layout = layout
        self.after_render = lambda: None

    def run(self, source, **kwargs):
        self.calls.append(source)
        return {}

    def call(self, name, **kwargs):
        self.calls.append((name, kwargs))
        renders = sum(isinstance(c, tuple) for c in self.calls)
        path = self.layout.scratch / f'frame-{renders}.png'
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new('RGB', (32, 32), (20, 20, 20) if renders == 1 else (180, 180, 180)).save(path)
        self.after_render()
        return {'image_path': str(path), 'frame': kwargs['frame'], 'mode': kwargs['mode'],
                'resolution': [32, 32, 50]}


def bind(tmp_path, monkeypatch, lease, *, session=None):
    shot, _, _, _, guard, layout = _authority(tmp_path, monkeypatch)
    path = candidate_script.exact_candidate_script_path(tmp_path, guard)
    candidate_script.write_scratch_candidate(tmp_path, path, 'pass\n', guard)
    source = tmp_path / 'prior.py'
    source.write_text('pass\n')
    session = session or Session(layout)
    capture = flynn_image_capture.UnitImageCapture(
        shot=shot, attempt_guard=guard, fence_lease=lease, session=session,
        prior_paths=[source], check_candidate=lambda: None,
    )
    return capture, path, source, session, layout


def test_capture_and_payment_share_a_journal_without_acceptance(tmp_path, monkeypatch):
    with builder_execution_fence(tmp_path) as lease:
        capture, candidate, _, session, layout = bind(tmp_path, monkeypatch, lease)
        class Adapter:
            def __init__(self):
                self.calls = 0

            def plan_output(self, request, available):
                return flynn.OutputReservation('scripted', 0)

            async def generate(self, request):
                self.calls += 1
                if self.calls == 1:
                    call = flynn.ToolCall('capture_unit_frame', '{"frame": 240}')
                else:
                    observation = json.loads(request.observation)
                    data = observation['data']
                    assert len(observation['content']) == 4
                    assert len(request.images) == 2
                    assert all(image.url.startswith('data:image/') for image in request.images)
                    assert 'base64' not in request.observation
                    assert 'replay_inputs' not in data
                    call = flynn.ToolCall('propose_checks', json.dumps({
                        'checks': [{'id': 'image-gap', 'frame': 240, 'axis': 'final_lock',
                                    'metric': 'region_mean', 'regions': {'r': [0, 0, 1, 1]},
                                    'op': '>=', 'lo': 80, 'ref': 'refs/a.png'}],
                        'after_handle': data['candidate']['handle'],
                    }))
                return await flynn.ScriptedAdapter([call]).generate(request)

        with flynn.SQLiteRun.create(layout.checkpoints / 'capture.sqlite', run_id='capture', initial_state='{}',
                                   limits=flynn.RunLimits(2, 2, 2, 60)) as run:
            runtime = flynn.Runtime(inference=Adapter(), tools=flynn.ToolBroker(capture.tools),
                                    evaluator=flynn_unit._ObservationEvaluator(), run=run,
                                    grants=('capture_unit_frame', 'propose_checks'), guards=(capture.guard,),
                                    prepare_request=flynn_unit._prepare_feedback)
            first = asyncio.run(runtime.step('Capture.'))
            data = json.loads(flynn.ToolResult.from_json(first.candidate.output).data_json)
            assert data['candidate']['candidate_sha256'] == digest(candidate.read_bytes())
            assert data['adversary']['candidate_sha256'] is None
            result = asyncio.run(runtime.step('Propose the measured check.'))
            payment = json.loads(flynn.ToolResult.from_json(result.candidate.output).data_json)
            assert payment['kept_ids'] == ['image-gap']
            assert payment['accepted'] is False
            assert run.read().revision == 0
            assert run.remaining()['external'] == 0
        assert sum(isinstance(c, tuple) for c in session.calls) == 2
        report = json.loads((tmp_path / data['report']).read_text())
        assert report['replay_inputs'][0]['script_path'] == 'prior.py'
        assert digest((tmp_path / data['report']).read_bytes()) == data['report_sha256']


def test_candidate_rewrite_requires_recapture_and_retires_old_handles(tmp_path, monkeypatch):
    with builder_execution_fence(tmp_path) as lease:
        capture, candidate, _, session, _ = bind(tmp_path, monkeypatch, lease)
        old = json.loads(asyncio.run(capture.capture({'frame': 240})).data_json)
        candidate.write_text('pass # next candidate\n')
        with pytest.raises(ValueError, match='candidate changed since capture'):
            capture.require_current_images()
        new = json.loads(asyncio.run(capture.capture({'frame': 240})).data_json)
        assert new['adversary'] == old['adversary']
        assert new['candidate']['handle'] != old['candidate']['handle']
        assert old['candidate']['handle'] not in capture.state['image_artifacts']
        assert sum(isinstance(c, tuple) for c in session.calls) == 3
        capture.require_current_images()


@pytest.mark.parametrize('failure', ['candidate', 'prior', 'render', 'frame', 'report', 'setup', 'order'])
def test_failed_capture_never_registers_a_handle(tmp_path, monkeypatch, failure):
    with builder_execution_fence(tmp_path) as lease:
        capture, candidate, source, session, _ = bind(tmp_path, monkeypatch, lease)
        if failure == 'candidate':
            session.after_render = lambda: candidate.write_text('pass # replaced\n')
        elif failure == 'prior':
            source.write_text('pass # replaced prior\n')
        elif failure == 'setup':
            capture.shot.frontmatter['fps'] = 30
        elif failure == 'order':
            capture.prior_paths.append(source)
        elif failure == 'render':
            def fail():
                raise RuntimeError('render failed')
            session.after_render = fail
        elif failure == 'report':
            original = flynn_image_capture.run_artifacts.RunLayout.write_report
            def replace_report(self, name, report):
                path = original(self, name, report)
                path.write_text('{}')
                return path
            monkeypatch.setattr(flynn_image_capture.run_artifacts.RunLayout, 'write_report', replace_report)
        with pytest.raises((ValueError, RuntimeError)):
            asyncio.run(capture.capture({'frame': 1 if failure == 'frame' else 240}))
        assert capture.state['image_artifacts'] == {}
        assert capture.state['image_adversaries'] == {}


def test_candidate_change_during_inference_denies_payment_before_external_spend(tmp_path, monkeypatch):
    with builder_execution_fence(tmp_path) as lease:
        capture, candidate, _, _, layout = bind(tmp_path, monkeypatch, lease)
        data = json.loads(asyncio.run(capture.capture({'frame': 240})).data_json)
        class Adapter:
            def plan_output(self, request, available):
                return flynn.OutputReservation('scripted', 0)

            async def generate(self, request):
                candidate.write_text('pass # changed during inference\n')
                return await flynn.ScriptedAdapter([flynn.ToolCall('propose_checks', json.dumps({
                    'checks': [], 'after_handle': data['candidate']['handle'],
                }))]).generate(request)

        with flynn.SQLiteRun.create(layout.checkpoints / 'stale-payment.sqlite', run_id='stale',
                                   initial_state='{}', limits=flynn.RunLimits(1, 1, 1, 60)) as run:
            runtime = flynn.Runtime(inference=Adapter(), tools=flynn.ToolBroker(capture.tools),
                                    evaluator=flynn_unit._ObservationEvaluator(), run=run,
                                    grants=('propose_checks',), guards=(capture.guard,))
            with pytest.raises(ValueError, match='candidate changed since capture'):
                asyncio.run(runtime.step('Propose the measured check.'))
            assert run.remaining()['external'] == 1
            assert run.remaining()['inference'] == 0
        assert not (tmp_path / 'runtime_checks.json').exists()


def test_receipt_backed_prior_is_rechecked_after_render(tmp_path, monkeypatch):
    with builder_execution_fence(tmp_path) as lease:
        capture, _, _, session, _ = bind(tmp_path, monkeypatch, lease)
        capture.prior_paths = flynn_image_capture.prior._ReceiptBackedPriorPaths(
            capture.prior_paths, shot=capture.shot,
            selected_authority=capture.attempt.selected_authority, publications=(),
        )
        changed = False

        def check_receipt(self, where):
            if changed:
                raise ValueError('prior receipt changed')

        def revoke():
            nonlocal changed
            changed = True

        monkeypatch.setattr(flynn_image_capture.prior._ReceiptBackedPriorPaths, 'require_current', check_receipt)
        session.after_render = revoke
        with pytest.raises(ValueError, match='prior receipt changed'):
            asyncio.run(capture.capture({'frame': 240}))
        assert capture.state['image_artifacts'] == {}
