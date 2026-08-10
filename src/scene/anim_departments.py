"""The MOTION department pipeline — the animation twin of :mod:`scene.departments`.

A still shot splits into modeling → look-dev → lighting → comp. A moving shot splits differently:

    layout → anim → lighting

each a fresh desk over the persistent scene, but with a PER-STAGE critic — because the departments
judge different things:

* **layout** (still): build both worlds' geometry + camera + staging, at rest. Judged by the scene
  critic on frame 1 — composition / camera / subject.
* **anim** (motion): open layout's scene and add ONLY the keyframed motion across all frames — the
  camera move, the roll, the world-swap timing. Judged by the MOTION critic on a start/mid/end strip.
* **lighting** (still): open the animated scene and add ONLY lights/world/atmosphere — never touching
  the keyframes. Judged by the scene critic (lighting) on a stable lit frame.

The win over the monolithic ``build_animation``: the motion is locked once and lighting can't wreck
the keyframed roll (it opens the anim-published scene and only adds lights), and the motion never has
to be re-derived when the look changes. Reuses the persistent-.blend handoff, desk memory
(context + lessons), the per-round snapshot + best-round primitive, and the two live-test fixes
(don't resume a wrapped-up session; ship the delivered stage) from :mod:`scene.departments`.

Deliverable: the full rendered sequence from the last stage that delivered a non-black result.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from agents.desk_log import make_message_logger
from agents.desk_memory import LessonBook, ShotContext, default_lessons_path
from agents.motion_critic import MotionCritique, critique_motion
from agents.scene_builder import DeskResult, ReferenceImage, build_scene
from agents.art_director import critique_craft
from agents.scene_critic import SceneCritique, critique_scene
from contracts.ledger import Layer
from contracts.verdict import Lever, Verdict
from contracts.ledger import FrameSample
from scene.animate import _strip_frame_numbers
from scene.departments import _best_of, _stage_passed
from scene.diagnostics import black_frame_hint, is_black, scene_digest
from scene.harness import Iteration

DeskBuilder = Callable[..., Awaitable[DeskResult]]
SceneCriticFn = Callable[..., Awaitable[SceneCritique]]
MotionCriticFn = Callable[..., Awaitable[MotionCritique]]
Progress = Callable[[str], None]

_DEFAULT_ACCEPT_SCORE = 0.75
_STILL_JUDGE_FRAME = 1  # still stages judge frame 1 (a stable lit pose; avoids the blackout middle)


@dataclass(frozen=True)
class AnimStage:
    name: str
    kind: str  # "still" (scene critic on a frame) or "motion" (motion critic on a strip)
    brief_suffix: str
    gate_dims: tuple[str, ...]  # scene dims for still stages; ("overall",) = score for the motion stage
    max_rounds: int = 2


MOTION_STAGES: tuple[AnimStage, ...] = (
    AnimStage(
        "layout", "still",
        "STAGE: LAYOUT. Build ONLY the geometry, the camera, and the staging for BOTH worlds this shot "
        "moves between (place them apart in space, e.g. offset in Y). Keep it STATIC and at rest — no "
        "motion/keyframes yet, and no final lighting. Nail composition, camera height/angle and the "
        "subject in frame for the START world. Later stages add motion and lighting.",
        ("composition", "camera", "subject_match"),
    ),
    AnimStage(
        "anim", "motion",
        "STAGE: ANIMATION. The geometry for both worlds and the camera already exist (inspect with "
        "scene_graph). Add ONLY the keyframed MOTION across all frames — the camera move/roll, the "
        "world-swap timing, any blackout. Block the WHOLE arc first. Do NOT re-model, re-shade, or "
        "add lights.",
        ("overall",),
    ),
    AnimStage(
        "lighting", "still",
        "STAGE: LIGHTING. The animated scene exists — geometry + keyframed motion. Add ONLY lighting, "
        "world/sky, volumetrics and exposure for both worlds. Do NOT touch the keyframes, the camera "
        "path, or the geometry.",
        ("lighting",),
    ),
)


@dataclass
class AnimStageResult:
    name: str
    kind: str
    passed: bool
    iterations: list[Iteration]
    published_path: str | None

    @property
    def best(self) -> Iteration | None:
        rendered = [it for it in self.iterations if it.render_path is not None]
        return max(rendered, key=lambda it: (it.score, it.index)) if rendered else None


@dataclass
class AnimPipelineResult:
    stages: list[AnimStageResult]
    published_path: str | None  # the scene the final sequence was rendered from
    sequence_dir: str | None
    frames: int
    accept_score: float = _DEFAULT_ACCEPT_SCORE

    @property
    def passed(self) -> bool:
        return bool(self.stages) and all(s.passed for s in self.stages)


async def _run_anim_stage(
    *,
    bridge,
    stage: AnimStage,
    stage_index: int,
    base_brief: str,
    reference_images: list[ReferenceImage],
    subject: str,
    out_dir: Path,
    frames: int,
    resolution: tuple[int, int],
    samples: int,
    scene_critic: SceneCriticFn,
    motion_critic: MotionCriticFn,
    builder: DeskBuilder,
    accept_score: float,
    ctx: ShotContext,
    lessons: LessonBook,
    emit: Progress,
) -> AnimStageResult:
    out_dir.mkdir(parents=True, exist_ok=True)
    brief = base_brief.strip() + "\n\n" + stage.brief_suffix
    iterations: list[Iteration] = []
    session: str | None = None
    last_submitted = True
    feedback: str | None = None

    def render_still(r: int) -> tuple[FrameSample, dict]:
        bridge.run_python(f"import bpy\nbpy.context.scene.frame_set({_STILL_JUDGE_FRAME})\nresult={{'f':{_STILL_JUDGE_FRAME}}}")
        path = out_dir / f"round_{r:02d}.jpg"
        shot = bridge.render(path=str(path), format="JPEG", resolution=list(resolution), samples=samples, return_base64=True)
        return FrameSample("00:00", 0.0, shot.get("image_b64", "")), bridge.image_stats(path=str(path))

    def render_strip(r: int) -> tuple[list[FrameSample], dict]:
        strip: list[FrameSample] = []
        start_stats: dict = {}
        for j, fnum in enumerate(_strip_frame_numbers(frames)):
            bridge.run_python(f"import bpy\nbpy.context.scene.frame_set({fnum})\nresult={{'f':{fnum}}}")
            path = out_dir / f"round_{r:02d}_f{fnum:04d}.jpg"
            shot = bridge.render(path=str(path), format="JPEG", resolution=list(resolution), samples=samples, return_base64=True)
            strip.append(FrameSample(f"f{fnum:04d}", float(fnum), shot.get("image_b64", "")))
            if j == 0:
                start_stats = bridge.image_stats(path=str(path))
        return strip, start_stats

    for r in range(stage.max_rounds):
        emit(f"stage {stage_index} {stage.name} ({stage.kind}) | round {r} | " + ("building" if r == 0 else "revising"))
        logger = make_message_logger(out_dir / f"round_{r:02d}.transcript.md", out_dir / f"round_{r:02d}.messages.jsonl",
                                     title=f"{stage.name} desk — round {r}")
        resume = session if (session and last_submitted) else None
        desk = await builder(
            bridge=bridge, brief=brief, reference_images=reference_images,
            feedback=feedback, resume=resume, fresh=(stage_index == 0 and r == 0),
            animation_frames=(frames if stage.kind == "motion" else None),
            context=ctx.render(), lessons=lessons.render(), on_message=logger,
        )
        session = desk.session_id
        last_submitted = desk.submitted
        note = (desk.note or "").strip()
        (out_dir / f"round_{r:02d}.bpy.py").write_text("\n\n".join(desk.bpy_log))
        ctx.add_decision(f"{stage.name} r{r}", note)
        lessons.harvest_and_add(note)

        blend_path: str | None = str(out_dir / f"round_{r:02d}.blend")
        try:
            bridge.save_blend(path=blend_path)
        except Exception:
            blend_path = None

        # render + judge with the stage's own critic
        if stage.kind == "motion":
            strip, start_stats = render_strip(r)
            digest = scene_digest(bridge.get_scene_graph())
            frame0, stats = strip[0], start_stats
        else:
            frame0, stats = render_still(r)
            digest = scene_digest(bridge.get_scene_graph())

        start_path = str(out_dir / (f"round_{r:02d}_f0001.jpg" if stage.kind == "motion" else f"round_{r:02d}.jpg"))
        if is_black(stats):
            hint = black_frame_hint(digest)
            verdict = Verdict(beat="shot", layer=Layer.CLIP, passed=False, lever=Lever.RE_SOURCE, reason="render is nearly black", confidence=0.9)
            iterations.append(Iteration(r, "", start_path, frame0, verdict, score=0.0, builder_raw=note, feedback_out=hint, blend_path=blend_path))
            emit(f"stage {stage_index} {stage.name} | round {r} | BLACK — skipping critic")
            feedback = hint
            continue

        if stage.kind == "motion":
            crit = await motion_critic(beat_id="shot", brief=base_brief, strip_frames=strip)
        else:
            crit = await scene_critic(beat_id="shot", subject=subject, reference_images=reference_images, render_frames=[frame0])
        verdict = crit.verdict
        passed = _stage_passed(stage, verdict.dimensions, crit.score, accept_score)
        gate = "motion score" if stage.kind == "motion" else "+".join(stage.gate_dims)
        dims = " ".join(f"{k}={'OK' if v else 'FIX'}" for k, v in verdict.dimensions.items())
        next_feedback = None if passed else (
            f"This is the {stage.name.upper()} stage — its job is {gate}. Score {crit.score:.2f}/1.0 "
            f"(need >= {accept_score:.2f}). Per-axis: {dims}. Fix {gate}; leave other departments alone. {verdict.reason} | {digest}"
        )
        iterations.append(Iteration(r, "", start_path, frame0, verdict, score=crit.score, builder_raw=note,
                                    critic_raw=getattr(crit, "raw", ""), feedback_out=next_feedback, blend_path=blend_path))
        emit(f"stage {stage_index} {stage.name} | round {r} | score {crit.score:.2f} | gate[{gate}]={'PASS' if passed else 'FIX'}")
        if passed:
            break
        feedback = next_feedback

    best = _best_of(iterations, stage, accept_score)
    published: str | None = None
    if best is not None and best.blend_path:
        try:
            bridge.open_blend(path=best.blend_path)
            published = str(out_dir / "published.blend")
            bridge.save_blend(path=published)
            ctx.add_fact(stage.name, f"published: {scene_digest(bridge.get_scene_graph())}")
        except Exception:
            published = best.blend_path
    passed = best is not None and _stage_passed(stage, best.verdict.dimensions if best.verdict else {}, best.score, accept_score)
    if not passed and best is not None and best.verdict is not None:
        ctx.set_open_issues(f"{stage.name} not fully signed off: {best.verdict.reason}")
    emit(f"stage {stage_index} {stage.name} | DONE | passed={passed} | published={published}")
    return AnimStageResult(name=stage.name, kind=stage.kind, passed=passed, iterations=iterations, published_path=published)


async def build_animation_pipeline(
    *,
    bridge,
    brief: str,
    reference_images: list[ReferenceImage],
    subject: str,
    out_dir: str | Path,
    frames: int = 30,
    stages: tuple[AnimStage, ...] = MOTION_STAGES,
    accept_score: float = _DEFAULT_ACCEPT_SCORE,
    resolution: tuple[int, int] = (640, 360),
    samples: int = 16,
    builder: DeskBuilder = build_scene,
    scene_critic: SceneCriticFn = critique_craft,
    motion_critic: MotionCriticFn = critique_motion,
    context: ShotContext | None = None,
    lessons: LessonBook | None = None,
    final_engine: str = "CYCLES",
    final_samples: int = 64,
    on_progress: Progress | None = None,
) -> AnimPipelineResult:
    """Build one moving shot through layout → anim → lighting over the persistent scene, then render
    the full sequence from the last stage that delivered a non-black result."""
    log = on_progress or (lambda _m: None)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    trace = out / "trace.log"
    ctx = context or ShotContext(path=out / "context.md")
    ctx.set_intent(brief)
    lessons = lessons or LessonBook(path=default_lessons_path())

    def emit(line: str) -> None:
        with trace.open("a") as fh:
            fh.write(line + "\n")
        log(line)

    published: str | None = None
    stage_results: list[AnimStageResult] = []
    for s, stage in enumerate(stages):
        if s > 0 and published:
            emit(f"stage {s} {stage.name} | opening prior published scene {Path(published).name}")
            try:
                bridge.open_blend(path=published)
            except Exception:
                emit(f"stage {s} {stage.name} | open_blend failed — building on the live scene")
        result = await _run_anim_stage(
            bridge=bridge, stage=stage, stage_index=s, base_brief=brief, reference_images=reference_images,
            subject=subject, out_dir=out / f"{s:02d}_{stage.name}", frames=frames, resolution=resolution,
            samples=samples, scene_critic=scene_critic, motion_critic=motion_critic, builder=builder,
            accept_score=accept_score, ctx=ctx, lessons=lessons, emit=emit,
        )
        stage_results.append(result)
        published = result.published_path or published

    # deliverable = the LAST stage that delivered a non-black result (scores across the still/motion
    # critics aren't comparable, so we take the most-complete delivered scene, not a cross-critic max).
    deliverable_stage = next(
        (s for s in reversed(stage_results) if s.published_path and s.best is not None and s.best.score > 0),
        None,
    )
    deliverable = deliverable_stage.published_path if deliverable_stage else published
    sequence_dir: str | None = None
    if deliverable:
        try:
            bridge.open_blend(path=deliverable)
            seq = bridge.render_sequence(start=1, end=frames, step=1, prefix="final", dir=str(out / "final"),
                                         resolution=list(resolution), samples=final_samples, engine=final_engine)
            sequence_dir = seq.get("dir")
        except Exception:
            sequence_dir = None
    won = deliverable_stage.name if deliverable_stage else "none"
    emit(f"done | stages {len(stage_results)} | passed {sum(s.passed for s in stage_results)}/{len(stage_results)} | deliverable={won} | seq={sequence_dir}")
    return AnimPipelineResult(stages=stage_results, published_path=deliverable, sequence_dir=sequence_dir, frames=frames, accept_score=accept_score)
