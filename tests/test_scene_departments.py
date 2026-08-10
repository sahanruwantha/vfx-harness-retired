"""The department pipeline — stage order, per-stage handoff, focused gating (fakes, no SDK/Blender)."""

from __future__ import annotations

import asyncio

from agents.scene_builder import DeskResult
from agents.scene_critic import SceneCritique
from develop.ledger import Layer
from develop.verdict import Lever, Verdict
from scene.departments import (
    DEFAULT_CRITICS,
    DEFAULT_STAGES,
    DEFAULT_STAGES_WITH_FX,
    FX_STAGE,
    Stage,
    _best_of,
    _stage_passed,
    build_shot_pipeline,
    with_fx,
)
from scene.harness import Iteration


def _dims(**kw):
    base = {"composition": False, "camera": False, "subject_match": False, "lighting": False, "palette": False}
    base.update(kw)
    return base


# --- gating helpers (pure) ----------------------------------------------------------


def test_stage_passed_uses_score_for_overall_and_dims_otherwise():
    comp = Stage("comp", "", ("overall",))
    assert _stage_passed(comp, _dims(), 0.9, 0.8) is True
    assert _stage_passed(comp, _dims(), 0.5, 0.8) is False
    modeling = Stage("modeling", "", ("composition", "camera", "subject_match"))
    assert _stage_passed(modeling, _dims(composition=True, camera=True, subject_match=True), 0.1, 0.8) is True
    assert _stage_passed(modeling, _dims(composition=True, camera=True), 0.99, 0.8) is False  # subject_match missing


def _it(index, dims, score, blend="b"):
    v = Verdict(beat="shot", layer=Layer.CLIP, passed=False, lever=Lever.RE_SOURCE, dimensions=dims)
    return Iteration(index, "", f"r{index}.jpg", None, v, score=score, blend_path=blend)


def test_best_of_prefers_a_gate_passing_round_over_a_higher_score():
    stage = Stage("modeling", "", ("composition", "camera", "subject_match"))
    passes = _it(0, _dims(composition=True, camera=True, subject_match=True), 0.5)
    higher = _it(1, _dims(composition=True, camera=False, subject_match=True), 0.9)  # higher score, gate fails
    assert _best_of([passes, higher], stage, 0.8) is passes


# --- the pipeline -------------------------------------------------------------------


class FakeBridge:
    def __init__(self):
        self.saved: list[str] = []
        self.opened: list[str] = []
        self.passes: list[str] = []

    def run_python(self, code):  # comp graph on the final render
        return {"ok": True, "result": {"comp": True}}

    def render_passes(self, **p):
        self.passes.append(p["path"])
        return {"path": p["path"], "passes": ["combined", "z"], "multilayer": True, "bytes": 100}

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


def _builder():
    calls: list[dict] = []

    async def builder(*, bridge, brief, reference_images, feedback=None, resume=None, fresh=True,
                      on_message=None, context=None, lessons=None, preview_engine=None, asset_library=None, look_budget=None):
        calls.append({"fresh": fresh, "resume": resume, "brief": brief, "context": context or "", "lessons": lessons or ""})
        return DeskResult(submitted=True, note="built", session_id="sess", outcome="complete",
                          stop_reason="ok", turns=1, cost_usd=0.0, bpy_log=("import bpy",))

    return builder, calls


def _critic(scripted):
    """scripted: list of (dims, score) per critic call in order."""
    state = {"n": 0}

    async def critic(*, beat_id, subject, reference_images, render_frames):
        dims, score = scripted[min(state["n"], len(scripted) - 1)]
        state["n"] += 1
        v = Verdict(beat="shot", layer=Layer.CLIP, passed=False, lever=Lever.RE_SOURCE, dimensions=dims)
        return SceneCritique(verdict=v, outcome="complete", stop_reason="ok", turns=1, cost_usd=0.0, score=score)

    return critic


def test_pipeline_runs_stages_in_order_hands_off_and_gates_each(tmp_path):
    builder, calls = _builder()
    # modeling passes r0; look-dev fails r0 (palette) then passes r1; lighting passes r0; comp passes r0
    critic = _critic([
        (_dims(composition=True, camera=True, subject_match=True), 0.5),   # modeling r0 → gate OK
        (_dims(subject_match=True, palette=False), 0.55),                  # look-dev r0 → palette FIX
        (_dims(subject_match=True, palette=True), 0.6),                    # look-dev r1 → OK
        (_dims(lighting=True), 0.7),                                       # lighting r0 → OK
        (_dims(composition=True, camera=True, subject_match=True, palette=True, lighting=True), 0.85),  # comp → score OK
    ])
    bridge = FakeBridge()

    result = asyncio.run(build_shot_pipeline(
        bridge=bridge, brief="a tower", reference_images=[("image/jpeg", "R")], subject="s",
        out_dir=tmp_path, builder=builder, critics={"layout": critic, "lookdev": critic, "lighting": critic, "craft": critic}, accept_score=0.8,
    ))

    # four departments, in order, each signed off on its own gate
    assert [s.name for s in result.stages] == ["modeling", "look-dev", "lighting", "comp"]
    assert all(s.passed for s in result.stages)
    # look-dev took two rounds (revise within the stage)
    assert len(result.stages[1].iterations) == 2
    # only the very first desk call built fresh; every later stage builds on the prior scene
    assert calls[0]["fresh"] is True
    assert all(c["fresh"] is False for c in calls[1:])
    # look-dev's second round resumed the same desk session
    assert calls[2]["resume"] == "sess"
    # each stage's department brief reached the desk
    assert "MODELING" in calls[0]["brief"] and "LOOK-DEV" in calls[1]["brief"] and "LIGHTING" in calls[3]["brief"]
    # every stage published a .blend, and the next stage opened it (handoff)
    assert all(s.published_path and s.published_path.endswith("published.blend") for s in result.stages)
    assert any("00_modeling" in p and p.endswith("published.blend") for p in bridge.opened)  # look-dev opened modeling's
    assert result.final_render and result.final_render.endswith("final.jpg")
    # DESK MEMORY: durable lessons injected from the first call; per-shot context accumulates so a
    # later department's desk sees earlier departments' decisions.
    assert "BLENDER_EEVEE" in calls[0]["lessons"]  # seeded gotcha present from the start
    assert "modeling" not in calls[0]["context"]  # nothing decided yet at stage 0
    assert "modeling" in calls[-1]["context"]  # comp's desk sees the modeling decision in context


def test_deliverable_is_the_latest_finished_stage(tmp_path):
    # all four departments deliver — the shipped frame is the latest FINISHED (shippable) stage: comp.
    # pre-finish stages (modeling/look-dev) are never shipped; cross-critic scores aren't compared.
    builder, _ = _builder()
    critic = _critic([
        (_dims(composition=True, camera=True, subject_match=True), 0.5),  # modeling (layout)
        (_dims(palette=True, subject_match=True), 0.6),                   # look-dev
        (_dims(lighting=True), 0.7),                                      # lighting (shippable)
        (_dims(composition=True, camera=True, subject_match=True, palette=True, lighting=True), 0.85),  # comp (shippable)
    ])
    bridge = FakeBridge()
    result = asyncio.run(build_shot_pipeline(
        bridge=bridge, brief="a tower", reference_images=[("image/jpeg", "R")], subject="s",
        out_dir=tmp_path, builder=builder, critics={"layout": critic, "lookdev": critic, "lighting": critic, "craft": critic}, accept_score=0.8,
    ))
    assert result.published_path.endswith("03_comp/published.blend")  # comp is the latest finished stage
    assert bridge.opened[-1].endswith("03_comp/published.blend")  # final render opened comp's scene


def test_wrapped_up_round_is_not_resumed(tmp_path):
    # round 0 wraps up (submitted=False); round 1 must NOT resume that poisoned session, and must
    # build on the existing scene (fresh=False), not clear it.
    calls: list[dict] = []
    n = {"i": 0}

    async def builder(*, bridge, brief, reference_images, feedback=None, resume=None, fresh=True,
                      on_message=None, context=None, lessons=None, preview_engine=None, asset_library=None, look_budget=None):
        calls.append({"fresh": fresh, "resume": resume})
        submitted = n["i"] > 0  # first call wraps up
        n["i"] += 1
        return DeskResult(submitted=submitted, note="x", session_id="sess", outcome="complete",
                          stop_reason="ok", turns=1, cost_usd=0.0, bpy_log=())

    critic = _critic([
        (_dims(composition=True, camera=True, subject_match=False), 0.4),  # r0 → gate FIX → revise
        (_dims(composition=True, camera=True, subject_match=True), 0.5),   # r1 → pass
    ])
    only_modeling = (Stage("modeling", "build geometry", ("composition", "camera", "subject_match"), max_rounds=2),)
    asyncio.run(build_shot_pipeline(
        bridge=FakeBridge(), brief="a tower", reference_images=[("image/jpeg", "R")], subject="s",
        out_dir=tmp_path, stages=only_modeling, builder=builder, critics={"layout": critic, "lookdev": critic, "lighting": critic, "craft": critic}, accept_score=0.8,
    ))
    assert calls[0]["fresh"] is True and calls[0]["resume"] is None
    assert calls[1]["resume"] is None  # the wrapped-up session was NOT resumed
    assert calls[1]["fresh"] is False  # but the scene was NOT cleared either — build on top


def test_deliverable_falls_back_to_lighting_without_a_comp_stage(tmp_path):
    # 3-stage pipeline (no comp) — the latest finished stage is lighting, so lighting ships.
    builder, _ = _builder()
    critic = _critic([
        (_dims(composition=True, camera=True, subject_match=True), 0.5),  # modeling (layout)
        (_dims(palette=True, subject_match=True), 0.6),                   # look-dev
        (_dims(lighting=True), 0.7),                                      # lighting (shippable, latest)
    ])
    bridge = FakeBridge()
    result = asyncio.run(build_shot_pipeline(
        bridge=bridge, brief="a tower", reference_images=[("image/jpeg", "R")], subject="s",
        out_dir=tmp_path, stages=DEFAULT_STAGES[:3], builder=builder,
        critics={"layout": critic, "lookdev": critic, "lighting": critic, "craft": critic}, accept_score=0.8,
    ))
    assert result.published_path.endswith("02_lighting/published.blend")  # lighting is the deliverable


def test_default_stages_cover_the_four_departments():
    assert tuple(s.name for s in DEFAULT_STAGES) == ("modeling", "look-dev", "lighting", "comp")
    assert DEFAULT_STAGES[-1].gate_dims == ("overall",)


# --- FX / simulation department -----------------------------------------------------


def test_fx_stage_is_a_prelighting_effect_pass_judged_by_the_fx_critic():
    # runs in EEVEE (pre-lighting, like modeling/look-dev), gates on the phenomenon reading, never ships
    assert FX_STAGE.name == "fx" and FX_STAGE.critic == "fx"
    assert FX_STAGE.engine == "BLENDER_EEVEE_NEXT" and FX_STAGE.gate_dims == ("subject_match",)
    assert FX_STAGE.shippable is False and DEFAULT_CRITICS["fx"] is not None


def test_fx_is_absent_by_default_but_insertable_before_lighting():
    # a plain shot never runs a sim desk; with_fx slots FX in after look-dev, before the first finish
    assert not any(s.name == "fx" for s in DEFAULT_STAGES)
    names = tuple(s.name for s in DEFAULT_STAGES_WITH_FX)
    assert names == ("modeling", "look-dev", "fx", "lighting", "comp")
    # inserted immediately before the first shippable (finishing) stage
    assert DEFAULT_STAGES_WITH_FX[names.index("fx") + 1].shippable is True


def test_with_fx_is_idempotent():
    assert with_fx(DEFAULT_STAGES_WITH_FX) is DEFAULT_STAGES_WITH_FX  # already has fx → unchanged
