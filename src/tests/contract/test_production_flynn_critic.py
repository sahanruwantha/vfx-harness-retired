"""Production critic uses native journals; unqualified scores cannot accept work."""

import asyncio
import json

import flynn_agents_sdk as flynn
import httpx
import pytest
from flynn_agents_sdk import deepseek
from PIL import Image

from tests.contract.test_flynn_critic_transport import response
from tests.integration.test_flynn_unit_lifecycle import _authority
from vfx_harness.agents import critic_session
from vfx_harness.agents.builder import critic
from vfx_harness.orchestration.builder_execution_fence import builder_execution_fence
from vfx_harness.orchestration.ledger import Milestone


def setup(tmp_path, monkeypatch, handler):
    def configure(root):
        Image.new('RGB', (32, 32), 'red').save(root / 'refs/a.png')

    shot, _layer, unit, selected, guard, layout = _authority(tmp_path, monkeypatch, configure=configure)
    Image.new('RGB', (32, 32), 'blue').save(layout.scratch / 'candidate.png')
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'offline-fixture')
    monkeypatch.setenv('VFXH_CRITIC_MODEL', deepseek.VISION_MODEL)
    monkeypatch.delenv('VFXH_RUN_MAX_USD', raising=False)
    adapter = deepseek.DeepSeekAdapter
    monkeypatch.setattr(deepseek, 'DeepSeekAdapter',
                        lambda **kwargs: adapter(**kwargs, transport=httpx.MockTransport(handler)))

    def invoke():
        with builder_execution_fence(tmp_path):
            return asyncio.run(critic._critique(
                shot, Milestone('1@lock', 240, 'refs/a.png', 'form'),
                (layout.scratch / 'candidate.png').relative_to(tmp_path).as_posix(),
                [('form', 'Visible form')], object(), False, scope='Selected unit only',
                active_unit=unit, selected_authority=selected, execution_guard=guard,
            ))

    return layout, invoke


def test_production_critic_records_known_usage_without_qualifying_a_bare_score(tmp_path, monkeypatch):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return response()

    layout, invoke = setup(tmp_path, monkeypatch, handler)
    verdict = invoke()
    assert verdict['pass'] is False
    assert verdict['needs_human'] is True
    assert verdict['issues'] == []
    assert 'measured qualification' in verdict['qualification_gap']
    assert len(requests) == 1
    assert [row['function']['name'] for row in requests[0]['tools']] == ['submit_verdict']
    report = json.loads((tmp_path / verdict['native_observation']['report']).read_text())
    assert report['usage']['known_output_tokens'] == 31
    assert report['qualification_verified'] is report['acceptance_authorized'] is False
    with flynn.SQLiteRun.open(layout.root / report['journal']) as run:
        assert run.read().revision == 0
        assert not run.records()['commits']
        assert len(run.records()['operations']) == 1


def test_malformed_production_critic_response_records_spend_and_never_retries(tmp_path, monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return response(tool='invented_tool')

    layout, invoke = setup(tmp_path, monkeypatch, handler)
    with pytest.raises(flynn.InferenceFailure):
        invoke()
    assert len(requests) == 1
    report_path, = layout.reports.glob('critic-*.json')
    report = json.loads(report_path.read_text())
    assert report['usage']['known_output_tokens'] == 31
    assert report['inputs_validated_after_inference'] is False
    assert report['acceptance_authorized'] is False


def test_native_critic_configuration_refuses_before_inference(tmp_path, monkeypatch):
    _layout, invoke = setup(tmp_path, monkeypatch, lambda _: pytest.fail('invalid config reached inference'))
    monkeypatch.setenv('VFXH_RUN_MAX_USD', '1')
    with pytest.raises(ValueError, match='unpriced'):
        invoke()


def test_claim_selection_change_refuses_before_native_dispatch(tmp_path, monkeypatch):
    _layout, invoke = setup(tmp_path, monkeypatch, lambda _: pytest.fail('stale authority reached inference'))
    monkeypatch.setattr(critic_session.authority_selection, 'resolve_selected_authority', lambda _: None)
    with pytest.raises(ValueError, match='authority'):
        invoke()


def test_changed_candidate_after_inference_refuses_consumption_and_retains_usage(tmp_path, monkeypatch):
    requests = []
    layout = None

    def handler(request):
        requests.append(request)
        (layout.scratch / 'candidate.png').write_bytes(b'changed candidate after inference')
        return response()

    layout, invoke = setup(tmp_path, monkeypatch, handler)
    with pytest.raises(ValueError, match='changed'):
        invoke()
    assert len(requests) == 1
    report_path, = layout.reports.glob('critic-*.json')
    report = json.loads(report_path.read_text())
    assert report['usage']['known_output_tokens'] == 31
    assert report['inputs_validated_after_inference'] is False


def test_cancelled_critic_is_journaled_without_retry(tmp_path, monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        raise asyncio.CancelledError('injected critic cancellation')

    layout, invoke = setup(tmp_path, monkeypatch, handler)
    with pytest.raises(asyncio.CancelledError):
        invoke()
    assert len(requests) == 1
    report_path, = layout.reports.glob('critic-*.json')
    report = json.loads(report_path.read_text())
    assert report['inputs_validated_after_inference'] is False
    assert report['acceptance_authorized'] is False
    with flynn.SQLiteRun.open(layout.root / report['journal']) as run:
        assert run.remaining()['inference'] == 0
        assert run.read().revision == 0
        assert not run.records()['commits']
