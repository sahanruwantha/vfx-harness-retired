"""The review/handoff loop, with a fake desk builder + critic + bridge — no SDK, no Blender.

build_shot no longer execs code: it spawns the desk (fake ``build_scene`` returning a DeskResult),
renders the deliverable off the bridge, runs the critic (dailies), and resumes the desk with notes.
The fakes let us assert the round count, convergence, note threading, best-by-score, and the
black-render handoff without a model or Blender.
"""

from __future__ import annotations

import asyncio

from agents.scene_builder import DeskResult
from agents.scene_critic import SceneCritique
from develop.ledger import Layer
from develop.verdict import Lever, Verdict
from scene.harness import build_shot, format_feedback


class FakeBridge:
    def __init__(self, *, luma: float = 0.4) -> None:
        self.luma = luma  # >0.03 → not black (default takes the critic path)
        self.renders = 0
        self.saved: list[str] = []

    def save_blend(self, **params) -> dict:
        self.saved.append(params["path"])
        return {"path": params["path"], "bytes": 10}

    def render(self, **params) -> dict:
        self.renders += 1
        return {"path": params["path"], "bytes": 100, "image_b64": "SU1H"}

    def image_stats(self, **_params) -> dict:
        return {"luma_mean": self.luma, "luma_std": 0.2, "mean_rgb": [0.4, 0.4, 0.4], "hist8": [0.125] * 8}

    def get_scene_graph(self) -> dict:
        return {
            "scene": {"active_camera": "cam", "engine": "BLENDER_EEVEE", "resolution": [768, 432], "object_count": 3},
            "objects": [{"type": "MESH", "materials": []}, {"type": "LIGHT", "light": {"energy": 100}}, {"type": "CAMERA"}],
        }


def _builder(notes, *, session="sess", submitted=True):
    """A fake desk: records each call's feedback/resume, returns the next note as a DeskResult."""
    calls: list[dict] = []

    async def builder(*, bridge, brief, reference_images, feedback=None, resume=None, fresh=True,
                      on_message=None, context=None, lessons=None, preview_engine=None, asset_library=None):
        calls.append({"feedback": feedback, "resume": resume, "fresh": fresh, "context": context, "lessons": lessons})
        note = notes[min(len(calls) - 1, len(notes) - 1)]
        return DeskResult(
            submitted=submitted, note=note, session_id=session, outcome="complete",
            stop_reason="ok", turns=1, cost_usd=0.0, bpy_log=("import bpy",),
        )

    return builder, calls


def _critic(items):
    """items: list of (verdict, score)."""
    state = {"n": 0}

    async def critic(*, beat_id, subject, reference_images, render_frames):
        verdict, score = items[min(state["n"], len(items) - 1)]
        state["n"] += 1
        return SceneCritique(verdict=verdict, outcome="complete", stop_reason="ok", turns=1, cost_usd=0.0, score=score)

    return critic


def _fail(reason="off"):
    return Verdict(
        beat="shot", layer=Layer.CLIP, passed=False, lever=Lever.RE_SOURCE, reason=reason,
        dimensions={"composition": False, "camera": True},
    )


def _pass():
    return Verdict(beat="shot", layer=Layer.CLIP, passed=True)


def test_loop_stops_on_pass_and_threads_rich_feedback(tmp_path):
    builder, calls = _builder(["note0", "note1", "note2"])
    critic = _critic([(_fail("too bright"), 0.4), (_pass(), 0.95)])
    bridge = FakeBridge()

    result = asyncio.run(build_shot(
        bridge=bridge, brief="b", reference_images=[("image/jpeg", "R")], subject="s",
        out_dir=tmp_path, builder=builder, critic=critic, max_rounds=6, accept_score=0.8,
    ))

    assert result.converged and result.best.passed
    assert len(result.iterations) == 2
    # each round snapshots its scene to a versioned .blend (persistent handoff)
    assert result.best.blend_path is not None and bridge.saved
    # round 1 resumed the same desk session, carrying the critic's per-axis notes
    assert calls[1]["resume"] == "sess"
    fb = calls[1]["feedback"]
    assert "too bright" in fb and "composition=FIX" in fb and "camera=OK" in fb
    # round 0 is a fresh desk — no resume, no feedback
    assert calls[0]["resume"] is None and calls[0]["feedback"] is None


def test_stops_on_score_threshold_without_binary_pass(tmp_path):
    builder, _ = _builder(["n"])
    critic = _critic([(_fail("close, minor gaps"), 0.85)])  # rejected but score clears the bar
    bridge = FakeBridge()

    result = asyncio.run(build_shot(
        bridge=bridge, brief="b", reference_images=[("image/jpeg", "R")], subject="s",
        out_dir=tmp_path, builder=builder, critic=critic, max_rounds=6, accept_score=0.8,
    ))

    assert result.converged
    assert len(result.iterations) == 1
    assert result.best.score == 0.85


def test_wrapped_up_desk_is_still_rendered_and_judged(tmp_path):
    # the desk ran out of turns (submitted=False) — we do NOT trust submit, we judge the render
    builder, _ = _builder(["partial build"], submitted=False)
    critic = _critic([(_pass(), 0.9)])
    bridge = FakeBridge()

    result = asyncio.run(build_shot(
        bridge=bridge, brief="b", reference_images=[("image/jpeg", "R")], subject="s",
        out_dir=tmp_path, builder=builder, critic=critic, max_rounds=3,
    ))

    assert bridge.renders == 1
    assert result.converged and result.best.score == 0.9


def test_budget_exhausts_and_best_is_highest_score(tmp_path):
    builder, _ = _builder(["n"])
    critic = _critic([(_fail(), 0.3), (_fail(), 0.7), (_fail(), 0.5)])  # never passes; peak at round 1
    bridge = FakeBridge()

    result = asyncio.run(build_shot(
        bridge=bridge, brief="b", reference_images=[("image/jpeg", "R")], subject="s",
        out_dir=tmp_path, builder=builder, critic=critic, max_rounds=3, accept_score=0.8,
    ))

    assert not result.converged
    assert len(result.iterations) == 3
    assert result.best.index == 1 and result.best.score == 0.7  # best-by-score, not last


def test_black_render_skips_critic_and_hands_over_diagnostic(tmp_path):
    builder, calls = _builder(["dark0", "fixed1"])
    critic_calls = {"n": 0}

    async def counting_critic(**_kw):
        critic_calls["n"] += 1
        return SceneCritique(verdict=_pass(), outcome="complete", stop_reason="ok", turns=1, cost_usd=0.0, score=0.9)

    bridge = FakeBridge(luma=0.0)  # first render is black
    original_stats = bridge.image_stats

    def stats_then_recover(**p):
        s = original_stats(**p)
        bridge.luma = 0.5  # subsequent renders are fine
        return s

    bridge.image_stats = stats_then_recover  # type: ignore[method-assign]

    result = asyncio.run(build_shot(
        bridge=bridge, brief="b", reference_images=[("image/jpeg", "R")], subject="s",
        out_dir=tmp_path, builder=builder, critic=counting_critic, max_rounds=3,
    ))

    assert critic_calls["n"] == 1  # the black frame did NOT call the critic
    assert result.iterations[0].score == 0.0 and not result.iterations[0].passed
    # the black diagnostic is handed back to the resumed desk
    assert calls[1]["resume"] == "sess"
    assert "BLACK" in calls[1]["feedback"] and "scene state:" in calls[1]["feedback"]


def test_prepare_runs_once_and_round0_builds_on_the_prepared_scene(tmp_path):
    builder, calls = _builder(["n"])
    critic = _critic([(_pass(), 0.9)])
    ran = {"n": 0}

    def prepare():
        ran["n"] += 1

    asyncio.run(build_shot(
        bridge=FakeBridge(), brief="b", reference_images=[("image/jpeg", "R")], subject="s",
        out_dir=tmp_path, builder=builder, critic=critic, prepare=prepare, max_rounds=1,
    ))
    assert ran["n"] == 1  # prepare ran once, before the loop
    assert calls[0]["fresh"] is False  # round 0 built on the prepared scene — no clean-slate


def test_format_feedback_shows_score_and_axes():
    critique = SceneCritique(verdict=_fail("dark"), outcome="complete", stop_reason="ok", turns=1, cost_usd=0.0, score=0.62)
    fb = format_feedback(critique, 0.8)
    assert "0.62" in fb and "0.80" in fb and "composition=FIX" in fb and "camera=OK" in fb
