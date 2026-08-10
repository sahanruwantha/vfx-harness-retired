"""The art-director (craft-quality) critic — content framing + verdict parsing (no SDK)."""

from __future__ import annotations

import asyncio

import agents.art_director as ad
from agents.art_director import build_content, critique_craft
from agents.scene_critic import SceneCritique
from develop.verdict import Lever
from develop.ledger import FrameSample


def _refs():
    return [("image/jpeg", "UkVG")]


def _frames():
    return [FrameSample("00:00", 0.0, "SU1H")]


def test_build_content_frames_reference_as_style_not_pixel_target():
    blocks = build_content(subject="a data tower", reference_images=_refs(), render_frames=_frames())
    texts = " ".join(b.get("text", "") for b in blocks)
    assert "STYLE" in texts and "NOT a pixel target" in texts and "dailies" in texts.lower()
    assert sum(1 for b in blocks if b["type"] == "image") == 2  # reference + render


class FakeResult:
    def __init__(self, result, is_error=False, subtype="success", session_id="s", num_turns=1,
                 total_cost_usd=0.02, terminal_reason="completed"):
        self.result = result
        self.is_error = is_error
        self.subtype = subtype
        self.session_id = session_id
        self.num_turns = num_turns
        self.total_cost_usd = total_cost_usd
        self.terminal_reason = terminal_reason
        self.errors = None


def _fake_query(result):
    def query(*, prompt, options):  # noqa: A002
        async def gen():
            yield result
        return gen()
    return query


def test_critique_craft_returns_quality_score_and_notes(monkeypatch):
    reply = ('{"passed": false, "lever": "re_source", "reason": "key falloff too hard; add a rim", '
             '"match": 0.72, "confidence": 0.8, '
             '"dimensions": {"composition": true, "camera": true, "lighting": false, "palette": true, "subject_match": true}}')
    monkeypatch.setattr(ad, "ResultMessage", FakeResult)
    monkeypatch.setattr(ad, "query", _fake_query(FakeResult(reply)))

    out = asyncio.run(critique_craft(beat_id="shot", subject="a tower", reference_images=_refs(), render_frames=_frames()))
    assert isinstance(out, SceneCritique)
    assert out.score == 0.72  # craft-quality score (carried in "match")
    assert not out.verdict.passed and out.verdict.lever is Lever.RE_SOURCE
    assert "rim" in out.verdict.reason  # the actionable craft note
    assert out.verdict.dimensions["lighting"] is False and out.verdict.dimensions["palette"] is True


def test_no_render_frames_is_re_source():
    out = asyncio.run(critique_craft(beat_id="shot", subject="x", reference_images=_refs(), render_frames=[]))
    assert not out.verdict.passed and out.verdict.lever is Lever.RE_SOURCE and out.score == 0.0
