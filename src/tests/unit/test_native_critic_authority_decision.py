"""Consumer decision tests use explicit transport stubs, not qualification credentials."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from tests.contract.test_flynn_critic_transport import verdict
from vfx_harness.agents.builder import critic
from vfx_harness.domain.critic_prompt import CriticPrompt
from vfx_harness.domain.work_units.claims import Claim
from vfx_harness.orchestration.ledger import Milestone


@pytest.mark.parametrize('gap', [None, 'unverified', 'missing_claim', 'extra_axis', 'unbound', 'layer'])
def test_only_complete_selected_and_admitted_scope_can_pass(tmp_path, monkeypatch, gap):
    claim = Claim.parse({
        'id': 'form', 'proposition': 'The subject has the declared form', 'axis': 'form',
        'property': 'shape', 'subject_roles': ['subject'], 'moments': [7], 'kind': 'atomic',
        'required': True, 'authority': 'qualified_qualitative_required', 'repair_owner': 'form',
        'asserts': 'image', 'evidence': [{'kind': 'qualification', 'id': 'form-v1'}],
        'qualification': {'suite': 'form-v1', 'judge_model': 'fixture', 'prompt': 'fixture',
                          'evidence_shape': 'fixture', 'artifact': 'fixture.json', 'artifact_sha256': 'f' * 64},
    }, 'fixture claim')
    # The stub stands for independently verified transport. It does not create a real credential.
    claim = replace(claim, qualification=None if gap == 'unbound' else {'stub': True})
    unit = SimpleNamespace(evaluation=SimpleNamespace(claims=(claim,))) if gap != 'layer' else None
    monkeypatch.setattr(critic, '_focus_references', lambda *a, **k: {7: 'reference.png'})
    monkeypatch.setattr(critic, '_claim_context', lambda *a, **k: ([], {'form': frozenset({'form-v1'})}, set()))
    monkeypatch.setattr(critic, '_critic_images', lambda *a, **k: ())
    monkeypatch.setattr(critic, 'critic_prompt', lambda *a, **k: CriticPrompt('fixture rubric'))

    async def observed(**kwargs):
        assert isinstance(kwargs['prompt'], CriticPrompt)
        assert kwargs['images'] == ()
        assert kwargs['claims'] == ((claim,) if unit else ())
        return {'verdict': verdict(), 'qualification_verified': gap != 'unverified',
                'qualified_claim_ids': [] if gap == 'missing_claim' else ['form'],
                'report': 'fixture-report.json', 'report_sha256': 'f' * 64}

    monkeypatch.setattr(critic.critic_session, 'execute', observed)
    axes = [('form', 'Visible form')]
    if gap == 'extra_axis':
        axes.append(('finish', 'Finish quality'))
    actual = asyncio.run(critic._critique(
        SimpleNamespace(folder=tmp_path, frontmatter={'type': 'still'}, frames=7),
        Milestone('1@form', 7, 'reference.png', 'form'), 'candidate.png', axes, object(), False,
        scope='selected scope', active_unit=unit, selected_authority=object(),
    ))
    assert actual['pass'] is (gap is None)
    if gap:
        assert actual['needs_human'] is True
        assert actual['issues'] == []
        assert actual['qualification_gap']
