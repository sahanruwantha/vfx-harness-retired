"""Per-department critics — each judges only its discipline (content framing + parsing, no SDK)."""

from __future__ import annotations

import asyncio

import agents.dept_critics as dc
from agents.dept_critics import (
    FX_PROMPT,
    LAYOUT_PROMPT,
    LIGHTING_PROMPT,
    LOOKDEV_PROMPT,
    _build_content,
    critique_fx,
    critique_layout,
    critique_lighting,
    critique_lookdev,
)
from agents.scene_critic import SceneCritique
from develop.verdict import Lever
from develop.ledger import FrameSample


def _frames():
    return [FrameSample("00:00", 0.0, "SU1H")]


def test_layout_prompt_scopes_to_blocking_and_ignores_look():
    # a layout reviewer judges staging/camera/silhouette and treats greybox as correct, not a fault
    assert "LAYOUT" in LAYOUT_PROMPT and "GREYBOX" in LAYOUT_PROMPT
    assert "Do NOT comment on or score colour, materials, lighting" in LAYOUT_PROMPT


def test_lookdev_and_lighting_prompts_own_their_discipline():
    assert "LOOK-DEV" in LOOKDEV_PROMPT and "UNLIT" in LOOKDEV_PROMPT  # judges materials, not lighting
    assert "LIGHTING" in LIGHTING_PROMPT and "key/fill/rim" in LIGHTING_PROMPT


def test_build_content_names_the_discipline_and_pairs_reference_with_render():
    blocks = _build_content(discipline="layout", subject="a tower",
                            reference_images=[("image/jpeg", "R")], render_frames=_frames())
    texts = " ".join(b.get("text", "") for b in blocks)
    assert "layout" in texts
    assert sum(1 for b in blocks if b["type"] == "image") == 2  # reference + render


class FakeResult:
    def __init__(self, result, is_error=False, subtype="success", session_id="s", num_turns=1,
                 total_cost_usd=0.02, terminal_reason="completed"):
        self.result, self.is_error, self.subtype = result, is_error, subtype
        self.session_id, self.num_turns, self.total_cost_usd = session_id, num_turns, total_cost_usd
        self.terminal_reason, self.errors = terminal_reason, None


def _fake_query(result):
    def query(*, prompt, options):  # noqa: A002
        async def gen():
            yield result
        return gen()
    return query


def test_layout_critic_returns_a_scored_verdict(monkeypatch):
    reply = ('{"passed": false, "lever": "re_source", "reason": "tower silhouette too squat; raise the shaft", '
             '"match": 0.6, "confidence": 0.8, '
             '"dimensions": {"composition": true, "camera": true, "subject_match": false, "lighting": true, "palette": true}}')
    monkeypatch.setattr(dc, "ResultMessage", FakeResult)
    monkeypatch.setattr(dc, "query", _fake_query(FakeResult(reply)))
    out = asyncio.run(critique_layout(beat_id="shot", subject="a tower", reference_images=[("image/jpeg", "R")], render_frames=_frames()))
    assert isinstance(out, SceneCritique) and out.score == 0.6
    assert out.verdict.lever is Lever.RE_SOURCE and "silhouette" in out.verdict.reason
    assert out.verdict.dimensions["subject_match"] is False and out.verdict.dimensions["lighting"] is True


def test_fx_prompt_owns_the_phenomenon_and_flags_the_blob():
    # an FX reviewer judges density/turbulence/scale and calls out the tarry-blob failure by name
    assert "FX / SIMULATION" in FX_PROMPT and "phenomenon" in FX_PROMPT
    assert "blob" in FX_PROMPT and "subject_match" in FX_PROMPT  # reports on subject_match, not lighting


def test_fx_critic_scores_the_effect_on_subject_match(monkeypatch):
    reply = ('{"passed": false, "lever": "re_source", "reason": "storm reads as a solid tarry blob; drive density with noise", '
             '"match": 0.35, "confidence": 0.8, '
             '"dimensions": {"composition": true, "camera": true, "subject_match": false, "lighting": true, "palette": true}}')
    monkeypatch.setattr(dc, "ResultMessage", FakeResult)
    monkeypatch.setattr(dc, "query", _fake_query(FakeResult(reply)))
    out = asyncio.run(critique_fx(beat_id="shot", subject="a storm", reference_images=[("image/jpeg", "R")], render_frames=_frames()))
    assert isinstance(out, SceneCritique) and out.score == 0.35
    assert out.verdict.dimensions["subject_match"] is False and "blob" in out.verdict.reason


def test_dept_critics_are_distinct_callables():
    assert critique_layout is not critique_lookdev is not critique_lighting is not critique_fx


def test_no_frames_is_re_source():
    out = asyncio.run(critique_lighting(beat_id="shot", subject="x", reference_images=[], render_frames=[]))
    assert not out.verdict.passed and out.verdict.lever is Lever.RE_SOURCE and out.score == 0.0
