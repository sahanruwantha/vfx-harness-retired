"""The motion department pipeline — layout→anim→lighting, per-stage critic (fakes, no SDK/Blender)."""

from __future__ import annotations

import asyncio

from agents.motion_critic import MotionCritique
from agents.scene_builder import DeskResult
from agents.scene_critic import SceneCritique
from develop.ledger import Layer
from develop.verdict import Lever, Verdict
from scene.anim_departments import MOTION_STAGES, build_animation_pipeline


def _dims(**kw):
    base = {"composition": False, "camera": False, "subject_match": False, "lighting": False, "palette": False}
    base.update(kw)
    return base


class FakeBridge:
    def __init__(self):
        self.saved: list[str] = []
        self.opened: list[str] = []
        self.sequences = 0

    def run_python(self, code):  # frame_set only
        return {"ok": True, "result": {}}

    def save_blend(self, **p):
        self.saved.append(p["path"])
        return {"path": p["path"], "bytes": 10}

    def open_blend(self, **p):
        self.opened.append(p["path"])
        return {"opened": p["path"], "objects": 3}

    def render(self, **p):
        return {"path": p["path"], "bytes": 100, "image_b64": "SU1H"}

    def image_stats(self, **_p):
        return {"luma_mean": 0.4, "luma_std": 0.2, "mean_rgb": [0.4, 0.4, 0.4], "hist8": [0.125] * 8}

    def get_scene_graph(self):
        return {"scene": {"active_camera": "cam", "engine": "EEVEE"}, "objects": [{"type": "CAMERA"}]}

    def render_sequence(self, **p):
        self.sequences += 1
        return {"paths": [], "count": p["end"], "dir": p["dir"]}


def _builder():
    calls: list[dict] = []

    async def builder(*, bridge, brief, reference_images, feedback=None, resume=None, fresh=True,
                      animation_frames=None, on_message=None, context=None, lessons=None, preview_engine=None, asset_library=None):
        calls.append({"fresh": fresh, "resume": resume, "animation_frames": animation_frames, "brief": brief})
        return DeskResult(submitted=True, note="built", session_id="sess", outcome="complete",
                          stop_reason="ok", turns=1, cost_usd=0.0, bpy_log=("import bpy",))

    return builder, calls


def _scene_critic(scripted):
    state = {"n": 0}

    async def critic(*, beat_id, subject, reference_images, render_frames):
        dims, score = scripted[min(state["n"], len(scripted) - 1)]
        state["n"] += 1
        v = Verdict(beat="shot", layer=Layer.CLIP, passed=False, lever=Lever.RE_SOURCE, dimensions=dims)
        return SceneCritique(verdict=v, outcome="complete", stop_reason="ok", turns=1, cost_usd=0.0, score=score)

    return critic


def _motion_critic(scores):
    state = {"n": 0}
    calls = {"n": 0}

    async def critic(*, beat_id, brief, strip_frames):
        calls["n"] += 1
        score = scores[min(state["n"], len(scores) - 1)]
        state["n"] += 1
        v = Verdict(beat="shot", layer=Layer.CLIP, passed=False, lever=Lever.RE_SOURCE,
                    dimensions={"start_pose": True, "transition": score > 0.6, "end_pose": True, "continuity": True})
        return MotionCritique(verdict=v, outcome="complete", score=score, cost_usd=0.0)

    critic.calls = calls  # type: ignore[attr-defined]
    return critic


def test_motion_pipeline_runs_layout_anim_lighting_with_the_right_critic(tmp_path):
    builder, calls = _builder()
    # layout passes composition/camera/subject; lighting passes lighting; anim judged by motion critic
    scene_c = _scene_critic([
        (_dims(composition=True, camera=True, subject_match=True), 0.5),  # layout
        (_dims(lighting=True), 0.7),                                      # lighting
    ])
    motion_c = _motion_critic([0.85])  # anim clears motion score
    bridge = FakeBridge()

    result = asyncio.run(build_animation_pipeline(
        bridge=bridge, brief="green tower barrel-roll to purple 2.0", reference_images=[("image/jpeg", "R")],
        subject="s", out_dir=tmp_path, frames=30, builder=builder, scene_critic=scene_c, motion_critic=motion_c,
        accept_score=0.75,
    ))

    assert [s.name for s in result.stages] == ["layout", "anim", "lighting"]
    assert [s.kind for s in result.stages] == ["still", "motion", "still"]
    assert result.passed is True
    # ONLY the anim stage was told to animate; layout/lighting are static
    assert calls[0]["animation_frames"] is None       # layout
    assert calls[1]["animation_frames"] == 30         # anim
    assert calls[2]["animation_frames"] is None       # lighting
    # the anim stage was judged by the motion critic (called exactly once — it passed round 0)
    assert motion_c.calls["n"] == 1
    # only layout built fresh; anim + lighting opened the prior published scene
    assert calls[0]["fresh"] is True and calls[1]["fresh"] is False and calls[2]["fresh"] is False
    assert any("00_layout" in p and p.endswith("published.blend") for p in bridge.opened)  # anim opened layout's
    # deliverable is the full sequence from the last delivered stage
    assert bridge.sequences == 1 and result.sequence_dir is not None
    assert result.published_path.endswith("02_lighting/published.blend")


def test_motion_stages_are_the_three_departments():
    assert tuple((s.name, s.kind) for s in MOTION_STAGES) == (("layout", "still"), ("anim", "motion"), ("lighting", "still"))
