"""The department-pipeline ``render_3d`` realizer — the seam that lets ``develop`` build a shot with
the real VFX pipeline (modeling → look-dev → [fx] → lighting → comp) instead of a single desk.

This is the successor to :func:`scene.realizer.make_scene_builder` (one desk, one render): it runs
:func:`scene.departments.build_shot_pipeline` and adapts its :class:`~scene.departments.PipelineResult`
into a :class:`~develop.ledger.Clip`, exactly like the other develop leaves adapt their agents.

Two seams make it the payoff of the last three increments:

* it accepts the supervisor's per-shot decision as an optional ``methodology`` keyword (the
  :mod:`scene.supervisor` router hands it over), and when the breakdown tags a simulated element
  (``methodology.needs_fx``) it inserts the FX department via :func:`scene.departments.with_fx` — so
  a storm shot is built as a real volume, decided by the supe, with no caller change; and
* it threads a shared :class:`~scene.assets.AssetLibrary` so the modeling desk sources a real hero
  mesh (A1) across beats.

The Blender bridge and the ``pipeline`` callable are injected, so the whole leaf is testable without
Blender or the department run — a fake pipeline returns a canned ``PipelineResult`` and the adapter is
asserted on the ``Clip`` it produces.
"""

from __future__ import annotations

import base64
from collections.abc import Awaitable, Callable
from pathlib import Path

from agents.scene_supervisor import Methodology
from develop.agents import Render3D
from develop.ledger import BeatEntry, Clip
from footage.inspect import FrameSample
from scene.critic import load_reference_images
from scene.departments import DEFAULT_STAGES, PipelineResult, Stage, build_shot_pipeline, with_fx

Pipeline = Callable[..., Awaitable[PipelineResult]]
BriefFn = Callable[[BeatEntry], str]


def default_brief(entry: BeatEntry) -> str:
    """A shot brief from the frozen beat — what to build, what it must illustrate, and its facts.
    The department stages append their own discipline suffix to this base."""
    subject = (entry.intent.subject or entry.intent.heading).strip()
    vo = (entry.realization.vo if entry.realization else "").strip()
    lines = [f"Reconstruct this shot for a faceless documentary: {subject}."]
    if vo:
        lines.append(f"On screen it illustrates this voiceover: {vo}")
    if entry.intent.evidence.strip():
        lines.append(f"Stay faithful to these facts: {entry.intent.evidence.strip()}")
    return "\n".join(lines)


def _clip_from_pipeline(result: PipelineResult, *, subject: str, needs_fx: bool) -> Clip:
    """Adapt a finished pipeline into a Clip: the composited final frame becomes the judged FrameSample,
    the deliverable .blend rides along for re-open/refine, and the craft score lands in render_meta."""
    meta = {
        "engine": "CYCLES",
        "subject": subject,
        "final_score": f"{result.final_score:.2f}",
        "final_reason": result.final_reason[:280],
        "stages": str(len(result.stages)),
        "stages_passed": str(sum(1 for s in result.stages if s.passed)),
        "needs_fx": str(needs_fx),
        "ran_fx": str(any(s.name == "fx" for s in result.stages)),
    }
    blend_path = Path(result.published_path) if result.published_path else None
    if not result.final_render or not Path(result.final_render).exists():
        return Clip(licence="KNOWN", blend_path=blend_path, render_meta=meta,
                    acquisition_gap=f"3D pipeline produced no final render for {subject!r}")
    image_b64 = base64.b64encode(Path(result.final_render).read_bytes()).decode("ascii")
    return Clip(
        fetched_path=Path(result.final_render),
        licence="KNOWN",  # a self-authored render is clearable by construction
        frames=(FrameSample(timecode="00:00", seconds=0.0, jpeg_b64=image_b64),),
        blend_path=blend_path,
        render_meta=meta,
    )


def stages_for(methodology: Methodology | None, base: tuple[Stage, ...]) -> tuple[Stage, ...]:
    """The stage list for this shot: the base pipeline, plus the FX department when the supe's
    per-element breakdown says the shot contains a simulated/volumetric element."""
    return with_fx(base) if (methodology is not None and methodology.needs_fx) else base


def make_pipeline_realizer(
    *,
    bridge,
    out_dir: str | Path,
    stages: tuple[Stage, ...] = DEFAULT_STAGES,
    refs_subdir: str = "refs",
    resolution: tuple[int, int] = (768, 432),
    samples: int = 64,
    assets=None,
    pipeline: Pipeline = build_shot_pipeline,
    brief_of: BriefFn = default_brief,
    on_progress: Callable[[str], None] | None = None,
) -> Render3D:
    """A ``render_3d`` leaf that builds a beat through the department pipeline.

    Bound to a started *bridge* and writing per-beat under *out_dir*. ``assets`` (an
    :class:`~scene.assets.AssetLibrary`, shared across beats) enables the modeling desk to source a
    real hero mesh. The returned callable accepts an optional ``methodology`` keyword — the
    :mod:`scene.supervisor` router passes the supe's decision, and an fx-tagged breakdown inserts the
    FX department. Called plainly (``render(entry)``) it runs the base stages, so it also drops
    straight into ``DevelopAgents.render_3d`` without a supervisor.
    """

    async def render(entry: BeatEntry, *, methodology: Methodology | None = None) -> Clip:
        beat_dir = Path(out_dir) / entry.id
        beat_dir.mkdir(parents=True, exist_ok=True)
        subject = entry.intent.subject or entry.intent.heading
        needs_fx = methodology is not None and methodology.needs_fx
        refs = load_reference_images(beat_dir / refs_subdir)
        result = await pipeline(
            bridge=bridge, brief=brief_of(entry), reference_images=refs, subject=subject,
            out_dir=beat_dir, stages=stages_for(methodology, stages), resolution=resolution,
            samples=samples, assets=assets, on_progress=on_progress,
        )
        return _clip_from_pipeline(result, subject=subject, needs_fx=needs_fx)

    return render
