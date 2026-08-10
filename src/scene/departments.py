"""The department pipeline — build ONE shot as a real studio does: sequential stages over a
persistent scene, each opening the prior stage's published .blend and adding only its own layer.

A single desk building everything lets a lighting fix break the composition. Real VFX splits the
work into departments — modeling → look-dev → lighting → comp — each a distinct discipline that
opens the *published* output of the stage before it, adds one layer, and is signed off on its own
terms before the next stage starts. This module is that pipeline:

* each :class:`Stage` is a FRESH desk session (a new artist) given a department-specific brief and
  told (via ``build_scene(fresh=False)``) that the scene already holds the prior stage's work;
* it is gated on only the critic dimensions that department owns (modeling: composition/camera/
  subject; look-dev: palette; lighting: lighting; comp: overall score);
* it publishes its BEST round's .blend snapshot, and the next stage ``open_blend``s that — so a
  later stage that corrupts the scene can never clobber an already-approved upstream version.

Reuses the desk (:func:`agents.scene_builder.build_scene`), the scene critic, the per-round .blend
snapshot + best-round-open primitive (Phase 2a), the transcript logger, and the diagnostics.
"""

from __future__ import annotations

import base64
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from agents.desk_log import make_message_logger
from agents.desk_memory import LessonBook, ShotContext, default_lessons_path
from agents.scene_builder import DeskResult, ReferenceImage, build_scene
from agents.art_director import critique_craft
from agents.dept_critics import critique_fx, critique_layout, critique_lighting, critique_lookdev
from agents.scene_critic import SceneCritique, critique_scene
from develop.ledger import Layer
from develop.verdict import Lever, Verdict
from footage.inspect import FrameSample
from scene.diagnostics import black_frame_hint, is_black, scene_digest
from scene.harness import Iteration, format_feedback

DeskBuilder = Callable[..., Awaitable[DeskResult]]
Critic = Callable[..., Awaitable[SceneCritique]]
Progress = Callable[[str], None]

_DEFAULT_ACCEPT_SCORE = 0.8
_OVERALL = "overall"  # a gate that means "judge the whole frame by score", not one dimension

# Each department reviewed by its own domain-expert critic (Stage.critic keys into this).
DEFAULT_CRITICS: dict[str, Critic] = {
    "layout": critique_layout,
    "lookdev": critique_lookdev,
    "fx": critique_fx,
    "lighting": critique_lighting,
    "craft": critique_craft,
    "structural": critique_scene,  # the match critic, still available for pure reconstruction
}


@dataclass(frozen=True)
class Stage:
    name: str
    brief_suffix: str  # appended to the base brief — what THIS department does (and must not touch)
    gate_dims: tuple[str, ...]  # critic dims that must pass for this stage ('overall' → use score)
    max_rounds: int = 3
    engine: str | None = None  # critique-render engine; None → the pipeline's stage_engine default
    critic: str = "craft"  # department reviewer key (layout/lookdev/lighting/craft), see DEFAULT_CRITICS
    shippable: bool = False  # a FINISHED stage (post-lighting) that can be the delivered frame
    use_assets: bool = False  # this stage's desk gets acquire_asset/list_assets (source, don't sculpt)
    look_budget: int = 3  # visual looks (viewport_snapshot+render) the desk may spend per round; a
    # structural stage needs ~1 (scene_graph carries it), an appearance stage a few. Keeps the desk on
    # a viewport→dailies cadence instead of re-rendering every edit.


# EEVEE (fast, forgiving exposure) for the PRE-LIGHTING stages — modeling/look-dev have no lights
# yet, so a path-tracer renders them black; only judge in Cycles once lighting exists.
_EEVEE = "BLENDER_EEVEE_NEXT"
_CYCLES = "CYCLES"

DEFAULT_STAGES: tuple[Stage, ...] = (
    Stage(
        "modeling",
        "STAGE: MODELING / LAYOUT. Build ONLY the geometry, the camera, and the staging. Use neutral "
        "or simple placeholder emissive materials — do NOT do final look development and do NOT set up "
        "scene lighting yet. Nail composition, camera height/angle, and the subject's scale and place "
        "in frame to match the reference. Later departments add materials and lighting.",
        ("composition", "camera", "subject_match"),
        engine=_EEVEE,
        critic="layout",  # a layout/blocking reviewer — staging/camera/silhouette, not finish
        use_assets=True,  # the modeling desk can acquire a real hero mesh instead of sculpting cubes
        look_budget=2,  # structural: scene_graph verifies the build; ~1 snapshot for blocking + a check
    ),
    Stage(
        "look-dev",
        "STAGE: LOOK-DEV. Geometry, camera and staging already exist (inspect with scene_graph). "
        "Develop ONLY materials and shading — base colours, emission colour/strength, roughness — to "
        "match the reference's palette and how the subject reads. Do NOT move geometry or the camera, "
        "and do NOT add scene lights.",
        ("palette", "subject_match"),
        engine=_EEVEE,
        critic="lookdev",  # a look-dev reviewer — materials/palette, not lighting/finish
    ),
    Stage(
        "lighting",
        "STAGE: LIGHTING. Geometry, materials and any effects are built. Add ONLY lighting — key/fill/rim lights, "
        "the world/sky, volumetrics, and exposure/view-transform — to match the reference's light "
        "direction, contrast and mood. Do NOT change geometry, camera, or materials.",
        ("lighting",),
        max_rounds=4,  # lighting is the hard finishing stage — give it more iteration
        engine=_CYCLES,
        critic="lighting",  # a lighting/DP reviewer — key/contrast/mood/volumetrics
        shippable=True,  # lit scene is a deliverable frame
        look_budget=4,  # appearance + Cycles: lighting is inherently visual — a few looks per round
    ),
    Stage(
        "comp",
        "STAGE: COMP / FINISHING. The scene is modelled, shaded and lit. Do ONLY finishing to match "
        "the reference's final look — subtle exposure/contrast and emissive glow via real light energy "
        "and materials (the pipeline adds the compositor bloom/grade at the final). Do NOT restructure "
        "the scene.",
        (_OVERALL,),
        max_rounds=4,  # comp finishing gets more iteration too
        engine=_CYCLES,
        shippable=True,  # the finished comp is the primary deliverable
    ),
)


# The FX / simulation department — a real, separate discipline, not something the modeling desk fakes
# with a lumpy mesh. It runs AFTER look-dev (the hero geo/materials exist to sit the effect against)
# and BEFORE lighting (which then lights the effect). Judged in EEVEE like the other pre-lighting
# stages — enough world/ambient light for the volume's SHAPE and DENSITY to read — by the FX critic,
# which owns exactly one thing: does the effect read as the phenomenon (subject_match), not a blob.
# Not every shot needs it (a portrait has no storm), so it is NOT in DEFAULT_STAGES; a shot that calls
# for an effect gets it via ``with_fx`` (the supervisor's per-shot decision, see build_shot_pipeline).
FX_STAGE = Stage(
    "fx",
    "STAGE: FX / SIMULATION. The hero geometry, camera and materials already exist (inspect with "
    "scene_graph). Build ONLY the effect the shot calls for — a volumetric storm/cloud/smoke/haze, a "
    "particle system, or atmospheric density. Use a REAL volume, not a sculpted mesh: a domain cube "
    "with a Principled Volume shader whose Density is driven by a Noise/Musgrave texture (shape it via "
    "the texture, a ColorRamp/Map Range and mapping — never by deforming geometry, which gives tarry "
    "blobs). Give the effect enough world/ambient light that its shape and density read for review; the "
    "lighting department does final lighting next. Set the frame to a representative moment. Do NOT "
    "change the hero geometry, camera, or hero materials.",
    ("subject_match",),
    max_rounds=4,  # sims are hard — give the FX desk room to iterate on density/turbulence
    engine=_EEVEE,
    critic="fx",
    shippable=False,  # an unlit effect pass is not a deliverable; lighting/comp finish it
    look_budget=4,  # a volume is inherently visual — the FX desk needs to see density/turbulence
)


def with_fx(stages: tuple[Stage, ...] = DEFAULT_STAGES, fx_stage: Stage = FX_STAGE) -> tuple[Stage, ...]:
    """The stage list for a shot that needs an effect: insert the FX department just before the first
    finishing (shippable) stage — i.e. after look-dev, before lighting — so the effect exists to be
    lit. Idempotent (an existing 'fx' stage is left as-is). This is the seam the supervisor uses when
    a shot calls for simulation; the default (effect-free) pipeline is unchanged."""
    if any(st.name == fx_stage.name for st in stages):
        return stages
    insert_at = next((i for i, st in enumerate(stages) if st.shippable), len(stages))
    return (*stages[:insert_at], fx_stage, *stages[insert_at:])


DEFAULT_STAGES_WITH_FX: tuple[Stage, ...] = with_fx(DEFAULT_STAGES)


@dataclass
class StageResult:
    name: str
    passed: bool
    iterations: list[Iteration]
    published_path: str | None  # the .blend handed to the next stage (best round's snapshot)

    @property
    def best(self) -> Iteration | None:
        rendered = [it for it in self.iterations if it.render_path is not None]
        return max(rendered, key=lambda it: (it.score, it.index)) if rendered else None


@dataclass
class PipelineResult:
    stages: list[StageResult]
    published_path: str | None  # the deliverable scene (best finished stage)
    final_render: str | None
    final_score: float = 0.0  # craft-critic score of the FINISHED composited deliverable
    final_reason: str = ""  # the craft critic's note on the finished frame
    accept_score: float = _DEFAULT_ACCEPT_SCORE

    @property
    def passed(self) -> bool:
        return self.final_score >= self.accept_score


def _stage_passed(stage: Stage, dims, score: float, accept: float) -> bool:
    if _OVERALL in stage.gate_dims:
        return score >= accept
    return all(bool((dims or {}).get(d)) for d in stage.gate_dims)


def _best_of(iterations: list[Iteration], stage: Stage, accept: float) -> Iteration | None:
    """The round to publish: prefer one that passed this stage's gate, else the highest-scoring."""
    rendered = [it for it in iterations if it.render_path is not None and it.blend_path]
    if not rendered:
        return None
    passed = [it for it in rendered if _stage_passed(stage, it.verdict.dimensions if it.verdict else {}, it.score, accept)]
    return max(passed or rendered, key=lambda it: (it.score, it.index))


async def _run_stage(
    *,
    bridge,
    stage: Stage,
    stage_index: int,
    base_brief: str,
    reference_images: list[ReferenceImage],
    subject: str,
    out_dir: Path,
    resolution: tuple[int, int],
    samples: int,
    stage_engine: str,
    builder: DeskBuilder,
    critic: Critic,
    accept_score: float,
    ctx: ShotContext,
    lessons: LessonBook,
    asset_library,
    emit: Progress,
) -> StageResult:
    """One department: build → render → focused critic → revise, on the already-prepared scene.

    Stage 0 clears the scene (fresh); later stages build on the scene the caller has already
    ``open_blend``-ed. Reads the shot context + lessons so a fresh department desk starts informed,
    and appends its decisions/lessons back. Publishes the best round's .blend for the next stage."""
    out_dir.mkdir(parents=True, exist_ok=True)
    brief = base_brief.strip() + "\n\n" + stage.brief_suffix
    iterations: list[Iteration] = []
    session: str | None = None
    last_submitted = True
    feedback: str | None = None
    engine = stage.engine or stage_engine  # this stage's engine — the desk previews in it too

    for r in range(stage.max_rounds):
        emit(f"stage {stage_index} {stage.name} | round {r} | " + ("building" if r == 0 else "revising"))
        logger = make_message_logger(out_dir / f"round_{r:02d}.transcript.md", out_dir / f"round_{r:02d}.messages.jsonl",
                                     title=f"{stage.name} desk — round {r}")
        # Don't resume a session that WRAPPED UP — its history ends with "your tools were removed",
        # which the resumed desk believes and then does nothing. Start a fresh session on the existing
        # scene instead. Only the very first round of the first stage clears the scene (fresh=True).
        resume = session if (session and last_submitted) else None
        desk = await builder(
            bridge=bridge, brief=brief, reference_images=reference_images,
            feedback=feedback, resume=resume, fresh=(stage_index == 0 and r == 0),
            context=ctx.render(), lessons=lessons.render(), preview_engine=engine,
            asset_library=(asset_library if stage.use_assets else None),
            look_budget=stage.look_budget, on_message=logger,
        )
        session = desk.session_id
        last_submitted = desk.submitted
        note = (desk.note or "").strip()
        (out_dir / f"round_{r:02d}.bpy.py").write_text("\n\n".join(desk.bpy_log))
        ctx.add_decision(f"{stage.name} r{r}", note)  # turnover note for later stages
        lessons.harvest_and_add(note)  # durable gotchas → next runs

        blend_path: str | None = str(out_dir / f"round_{r:02d}.blend")
        try:
            bridge.save_blend(path=blend_path)
        except Exception:
            blend_path = None

        path = out_dir / f"round_{r:02d}.jpg"
        shot = bridge.render(path=str(path), format="JPEG", resolution=list(resolution), samples=samples,
                             engine=engine, denoise=(engine == "CYCLES"), return_base64=True)
        frame = FrameSample(timecode="00:00", seconds=0.0, jpeg_b64=shot.get("image_b64", ""))
        digest = scene_digest(bridge.get_scene_graph())

        if is_black(bridge.image_stats(path=str(path))):
            hint = black_frame_hint(digest)
            verdict = Verdict(beat="shot", layer=Layer.CLIP, passed=False, lever=Lever.RE_SOURCE,
                              reason="render is nearly black", confidence=0.9)
            iterations.append(Iteration(r, "", str(path), frame, verdict, score=0.0, builder_raw=note,
                                        feedback_out=hint, blend_path=blend_path))
            emit(f"stage {stage_index} {stage.name} | round {r} | BLACK — skipping critic")
            feedback = hint
            continue

        critique = await critic(beat_id="shot", subject=subject, reference_images=reference_images, render_frames=[frame])
        verdict = critique.verdict
        passed = _stage_passed(stage, verdict.dimensions, critique.score, accept_score)
        gate = "score" if _OVERALL in stage.gate_dims else "+".join(stage.gate_dims)
        next_feedback = None if passed else (
            f"This is the {stage.name.upper()} stage — its job is {gate}. {format_feedback(critique, accept_score)} "
            f"Focus your fix on {gate}; leave other departments' concerns alone. | {digest}"
        )
        iterations.append(Iteration(r, "", str(path), frame, verdict, score=critique.score, builder_raw=note,
                                    critic_raw=critique.raw, feedback_out=next_feedback, blend_path=blend_path))
        emit(f"stage {stage_index} {stage.name} | round {r} | score {critique.score:.2f} | gate[{gate}]={'PASS' if passed else 'FIX'}")
        if passed:
            break
        feedback = next_feedback

    best = _best_of(iterations, stage, accept_score)
    published: str | None = None
    if best is not None and best.blend_path:
        # publish the best round: reopen it so the live scene = the approved version, then save it as
        # this stage's published output for the next department to open.
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
    return StageResult(name=stage.name, passed=passed, iterations=iterations, published_path=published)


async def build_shot_pipeline(
    *,
    bridge,
    brief: str,
    reference_images: list[ReferenceImage],
    subject: str,
    out_dir: str | Path,
    stages: tuple[Stage, ...] = DEFAULT_STAGES,
    accept_score: float = _DEFAULT_ACCEPT_SCORE,
    resolution: tuple[int, int] = (768, 432),
    samples: int = 64,
    stage_engine: str = "CYCLES",
    builder: DeskBuilder = build_scene,
    critics: dict[str, Critic] | None = None,
    context: ShotContext | None = None,
    lessons: LessonBook | None = None,
    assets=None,
    final_engine: str = "CYCLES",
    final_samples: int = 128,
    apply_comp: bool = True,
    on_progress: Progress | None = None,
) -> PipelineResult:
    """Build one still shot through the department pipeline, publishing a .blend per stage and
    handing each stage the previous stage's published scene — plus the shot context (turnover notes)
    and the durable Blender lessons, so each fresh department desk starts informed."""
    log = on_progress or (lambda _m: None)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    trace = out / "trace.log"
    ctx = context or ShotContext(path=out / "context.md")
    ctx.set_intent(brief)
    lessons = lessons or LessonBook(path=default_lessons_path())
    # per-department reviewers: each Stage.critic key resolves to its domain-expert critic.
    critics = {**DEFAULT_CRITICS, **(critics or {})}
    # the asset library the sourcing-enabled stages pull from (grows across shots); created lazily so
    # pipelines with no asset stage never touch the network deps.
    asset_library = assets
    if asset_library is None and any(st.use_assets for st in stages):
        from scene.assets import AssetLibrary
        asset_library = AssetLibrary()

    def emit(line: str) -> None:
        with trace.open("a") as fh:
            fh.write(line + "\n")
        log(line)

    published: str | None = None
    stage_results: list[StageResult] = []
    for s, stage in enumerate(stages):
        if s > 0 and published:  # hand this department the prior department's published scene
            emit(f"stage {s} {stage.name} | opening prior published scene {Path(published).name}")
            try:
                bridge.open_blend(path=published)
            except Exception:
                emit(f"stage {s} {stage.name} | open_blend failed — building on the live scene")
        # per-department reviewer: the stage's domain-expert critic (layout/lookdev/lighting/craft)
        stage_critic = critics.get(stage.critic, critique_craft)
        result = await _run_stage(
            bridge=bridge, stage=stage, stage_index=s, base_brief=brief, reference_images=reference_images,
            subject=subject, out_dir=out / f"{s:02d}_{stage.name}", resolution=resolution, samples=samples,
            stage_engine=stage_engine, builder=builder, critic=stage_critic, accept_score=accept_score,
            ctx=ctx, lessons=lessons, asset_library=asset_library, emit=emit,
        )
        stage_results.append(result)
        published = result.published_path or published

    # Deliverable = the LATEST finished (shippable) stage that rendered non-black — comp normally,
    # lighting if comp didn't run or came back black. Pre-finish stages (modeling/look-dev) are never
    # shipped, and cross-department critic scores aren't comparable, so we take the most-complete
    # finished frame (the real-pipeline default: comp is the deliverable) and craft-critique it below.
    shippable = [
        (i, r) for i, (st, r) in enumerate(zip(stages, stage_results))
        if st.shippable and r.published_path and r.best is not None and r.best.score > 0
    ]
    best_pair = shippable[-1] if shippable else None
    if best_pair is None:  # nothing finished delivered — ship the latest published scene
        pub = [(i, r) for i, r in enumerate(stage_results) if r.published_path]
        best_pair = pub[-1] if pub else None
    best_stage = best_pair[1] if best_pair else None
    deliverable = best_stage.published_path if best_stage else published
    final_render: str | None = None
    if deliverable:
        try:
            bridge.open_blend(path=deliverable)
            # clean AOVs first (path-traced, pre-comp) for the record + advanced comp
            with contextlib.suppress(Exception):
                bridge.render_passes(path=str(out / "final_passes.exr"), resolution=list(resolution),
                                     engine=final_engine, samples=final_samples)
            # then the FINISHED deliverable: path-traced beauty with the real comp graph applied
            if apply_comp:
                with contextlib.suppress(Exception):
                    bridge.run_python(comp_graph_code())
            fpath = out / "final.jpg"
            bridge.render(path=str(fpath), format="JPEG", resolution=list(resolution),
                          samples=final_samples, engine=final_engine, denoise=True, return_base64=False)
            final_render = str(fpath)
            emit(f"final render: {final_engine} {final_samples}spp" + (" + comp" if apply_comp else ""))
        except Exception:
            final_render = None

    # Judge the FINISHED deliverable (the composited frame that actually ships) — the real quality
    # number, not a mid-pipeline stage's pre-comp render. Craft-critic, on the final.jpg.
    final_score, final_reason = 0.0, ""
    if final_render and Path(final_render).exists():
        with contextlib.suppress(Exception):
            frame = FrameSample("00:00", 0.0, base64.b64encode(Path(final_render).read_bytes()).decode("ascii"))
            fc = await critics.get("craft", critique_craft)(beat_id="final", subject=subject, reference_images=reference_images, render_frames=[frame])
            final_score, final_reason = fc.score, fc.verdict.reason
            emit(f"final dailies | craft {final_score:.2f} | {final_reason[:140]}")

    won = f"{best_stage.name} ({best_stage.best.score:.2f})" if best_stage else "none"
    emit(f"done | stages {len(stage_results)} | passed {sum(s.passed for s in stage_results)}/{len(stage_results)} | deliverable={won} | final craft {final_score:.2f} | {final_render}")
    return PipelineResult(stages=stage_results, published_path=deliverable, final_render=final_render,
                          final_score=final_score, final_reason=final_reason, accept_score=accept_score)
