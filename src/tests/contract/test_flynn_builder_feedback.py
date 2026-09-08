"""Selected builder images and refusal feedback survive transport without becoming state."""

import asyncio
import hashlib
import json
from dataclasses import replace

import flynn_agents_sdk as flynn
import pytest

from vfx_harness.agents.builder import flynn_unit


def test_latest_images_and_complete_refusal_reach_model_and_original_stays_in_journal(tmp_path):
    images = (flynn.ImageContent('data:image/png;base64,' + 'A' * 20000, 'low'),
              flynn.ImageContent('data:image/png;base64,' + 'B' * 20000, 'original'))
    capture = flynn.ToolResult(
        (flynn.TextContent('Adversary'), images[0], flynn.TextContent('Candidate'), images[1]),
        json.dumps({'candidate_sha256': 'exact-source', 'accepted': False}),
    ).to_json()
    reason = 'Threshold refused. ' + 'Measured explanation. ' * 40 + 'Legal window: 40 < lo <= 90.'
    refusal = flynn.ToolResult((flynn.TextContent(reason),), '{"unpaid_ids":["contrast"]}', 'refused').to_json()
    outputs = iter((capture, refusal, '{}'))
    requests = []

    async def observe(_):
        return next(outputs)

    class Adapter:
        async def generate(self, request):
            requests.append(request)
            flynn.ContextCompiler(max_characters=2000).compile((
                flynn.ContextItem('feedback', request.observation or '', required=True),
            ))
            return await flynn.ScriptedAdapter([flynn.ToolCall('observe', '{}')]).generate(request)

    with flynn.SQLiteRun.create(tmp_path / 'feedback.sqlite', run_id='feedback', initial_state='unaccepted',
                               limits=flynn.RunLimits(3, 3, 0)) as run:
        runtime = flynn.Runtime(
            inference=Adapter(), tools=flynn.ToolBroker([
                flynn.Tool('observe', lambda _: None, observe, observation=True),
            ]),
            evaluator=flynn_unit._ObservationEvaluator(), run=run, grants=('observe',),
            prepare_request=flynn_unit._prepare_feedback,
        )
        first = asyncio.run(runtime.step('Inspect.'))
        second = asyncio.run(runtime.step('Measure.'))
        asyncio.run(runtime.step('Correct.'))
        assert first.candidate.output == capture
        assert second.candidate.output == refusal
        assert run.read().revision == 0
        records = run.records()
        assert not records['commits']
        assert capture in [row['output'] for row in records['operations']]
    assert [len(request.images) for request in requests] == [0, 2, 0]
    assert requests[1].images == tuple(flynn.ImageInput(image.url, image.detail) for image in images)
    feedback = json.loads(requests[1].observation)
    assert feedback['content'] == [
        {'type': 'text', 'text': 'Adversary'}, {'type': 'image', 'image_index': 0},
        {'type': 'text', 'text': 'Candidate'}, {'type': 'image', 'image_index': 1},
    ]
    assert feedback['observation_sha256'] == hashlib.sha256(capture.encode()).hexdigest()
    assert feedback['data']['candidate_sha256'] == 'exact-source'
    assert 'base64' not in requests[1].observation
    feedback = json.loads(requests[2].observation)
    assert feedback['status'] == 'refused'
    assert feedback['content'][0]['text'] == reason
    assert feedback['data']['unpaid_ids'] == ['contrast']


@pytest.mark.parametrize('schema', ['flynn.tool-result/v1', 'flynn.tool-result/v2'])
def test_invalid_or_unsupported_structured_observation_is_refused(schema):
    with pytest.raises(flynn.ContractError):
        flynn_unit._selected_feedback(json.dumps({'schema': schema, 'content': []}))


def test_empty_observation_removes_previous_images():
    request = flynn.InferenceRequest('Observe.', flynn.State(0, '{}'), (), images=(flynn.ImageInput('old'),))
    assert flynn_unit._prepare_feedback(request) == replace(request, images=())


@pytest.mark.parametrize('status,expected', [('ok', flynn.Verdict.SATISFIED), ('refused', flynn.Verdict.FAILED)])
def test_execution_refusal_is_not_a_satisfied_observation(status, expected):
    candidate = flynn.Candidate('operation', flynn.State(0, '{}'), flynn.ToolCall('observe', '{}'),
                                flynn.ToolResult(status=status).to_json())
    evaluation = asyncio.run(flynn_unit._ObservationEvaluator().evaluate(candidate))
    assert evaluation.verdict == expected


def test_plain_canonical_feedback_keeps_failed_evidence_and_identity():
    raw = json.dumps({'canonical': 'failed', 'verdicts': [
        [[37, 'refs/37.png'], {'pass': False, 'issues': ['contrast failed'],
                              'evidence': [{'id': 'contrast', 'pass': False, 'value': 12, 'reason': 'too low'}],
                              'missing_evidence': ['silhouette']}],
    ]})
    text, images = flynn_unit._selected_feedback(raw)
    selected = json.loads(text)
    assert not images
    assert selected['canonical'] == 'failed'
    assert selected['points'][0]['frame'] == 37
    assert selected['points'][0]['evidence'][0]['reason'] == 'too low'
    assert selected['points'][0]['missing_evidence'] == ['silhouette']
    assert selected['observation_sha256'] == hashlib.sha256(raw.encode()).hexdigest()
