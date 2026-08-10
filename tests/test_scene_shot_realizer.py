"""The department-pipeline render_3d realizer — PipelineResult→Clip adaptation + the FX decision seam.

No Blender, no department run: a fake ``pipeline`` records the kwargs it was handed and returns a
canned :class:`PipelineResult`, so we assert on the stage selection and the Clip the adapter produces.
"""

from __future__ import annotations

import asyncio
import base64

from agents.scene_supervisor import Methodology, ShotElement
from contracts.ledger import BeatEntry, Clip, Intent, Realization
from scene.departments import DEFAULT_STAGES, PipelineResult, StageResult
from scene.shot_realizer import default_brief, make_pipeline_realizer, stages_for
from scene.supervisor import make_supervised_realizer


def _beat():
    e = BeatEntry(id="b1", intent=Intent("reconstruct", "a green storm over a data tower", "[Doc 1]", "1. Tower", 0))
    e.realization = Realization("the market gathers like weather")
    return e


def _pipeline_result(*, stages, final_render, score=0.82):
    stage_results = [StageResult(name=s.name, passed=True, iterations=[], published_path=f"/x/{s.name}.blend") for s in stages]
    return PipelineResult(stages=stage_results, published_path="/x/deliverable.blend", final_render=final_render,
                          final_score=score, final_reason="reads like a real storm frame")


def _fake_pipeline(captured, final_render):
    async def pipeline(**kw):
        captured.update(kw)
        return _pipeline_result(stages=kw["stages"], final_render=final_render)
    return pipeline


# --- brief + stage selection (pure) --------------------------------------------------


def test_default_brief_carries_subject_vo_and_facts():
    brief = default_brief(_beat())
    assert "a green storm over a data tower" in brief
    assert "the market gathers like weather" in brief and "[Doc 1]" in brief


def test_stages_for_inserts_fx_only_when_the_breakdown_calls_for_it():
    fx = Methodology("3D", False, elements=(ShotElement("storm", "fx"), ShotElement("tower", "3d")))
    clean = Methodology("3D", False, elements=(ShotElement("tower", "3d"),))
    assert tuple(s.name for s in stages_for(fx, DEFAULT_STAGES)) == ("modeling", "look-dev", "fx", "lighting", "comp")
    assert stages_for(clean, DEFAULT_STAGES) is DEFAULT_STAGES  # no fx element → base pipeline untouched
    assert stages_for(None, DEFAULT_STAGES) is DEFAULT_STAGES   # no supe → base pipeline


# --- PipelineResult → Clip -----------------------------------------------------------


def test_realizer_adapts_a_finished_pipeline_into_a_judged_clip(tmp_path):
    frame = tmp_path / "final.jpg"
    frame.write_bytes(b"JPEGBYTES")
    captured: dict = {}
    realize = make_pipeline_realizer(bridge=object(), out_dir=tmp_path, pipeline=_fake_pipeline(captured, str(frame)))
    clip = asyncio.run(realize(_beat()))

    assert isinstance(clip, Clip) and clip.licence == "KNOWN"
    assert len(clip.frames) == 1
    assert clip.frames[0].jpeg_b64 == base64.b64encode(b"JPEGBYTES").decode("ascii")  # the final frame is judged
    assert str(clip.blend_path) == "/x/deliverable.blend"  # deliverable rides along for refine
    assert clip.render_meta["final_score"] == "0.82" and clip.render_meta["ran_fx"] == "False"
    assert captured["subject"] == "a green storm over a data tower"  # beat → pipeline subject


def test_realizer_reports_a_missing_render_as_an_acquisition_gap(tmp_path):
    realize = make_pipeline_realizer(bridge=object(), out_dir=tmp_path, pipeline=_fake_pipeline({}, None))
    clip = asyncio.run(realize(_beat()))
    assert clip.frames == () and clip.acquisition_gap and "no final render" in clip.acquisition_gap
    assert str(clip.blend_path) == "/x/deliverable.blend"  # a gap still hands back the .blend it got to


def test_realizer_runs_the_fx_department_when_the_methodology_needs_it(tmp_path):
    frame = tmp_path / "final.jpg"
    frame.write_bytes(b"IMG")
    captured: dict = {}
    realize = make_pipeline_realizer(bridge=object(), out_dir=tmp_path, pipeline=_fake_pipeline(captured, str(frame)))
    m = Methodology("3D", False, elements=(ShotElement("storm sky", "fx"),))
    clip = asyncio.run(realize(_beat(), methodology=m))
    assert tuple(s.name for s in captured["stages"]) == ("modeling", "look-dev", "fx", "lighting", "comp")
    assert clip.render_meta["needs_fx"] == "True" and clip.render_meta["ran_fx"] == "True"


# --- end to end through the supervisor router ---------------------------------------


def test_supervised_leaf_routes_an_fx_shot_through_the_fx_pipeline(tmp_path):
    frame = tmp_path / "final.jpg"
    frame.write_bytes(b"IMG")
    captured: dict = {}
    realize = make_pipeline_realizer(bridge=object(), out_dir=tmp_path, pipeline=_fake_pipeline(captured, str(frame)))

    async def decide(**_kw):  # the supe breaks the shot down and tags the storm fx
        return Methodology("3D", False, "storm over tower",
                           elements=(ShotElement("storm", "fx"), ShotElement("tower", "3d")))

    leaf = make_supervised_realizer(realizers={"3d_still": realize}, decide=decide)
    clip = asyncio.run(leaf(_beat()))
    # the supe's fx tag flowed all the way to the pipeline's stage list
    assert "fx" in tuple(s.name for s in captured["stages"])
    assert clip.render_meta["methodology"] == "3D" and clip.render_meta["needs_fx"] == "True"
