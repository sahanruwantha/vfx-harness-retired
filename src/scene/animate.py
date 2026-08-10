"""build_animation — the review/handoff loop for MOTION, the animation twin of build_shot.

build_shot makes a still and reviews it against a reference. This makes a keyframed animation and
reviews the MOTION: it spawns the agent-driven desk (:func:`agents.scene_builder.build_scene` with
``animation_frames``), which writes keyframed bpy in the live scene; the loop renders the
start/middle/end strip cheaply, the motion critic judges the arc, and the notes go back to the SAME
desk session for a revision — until the motion reads or the budget is spent. Only then is the full
sequence rendered, from the live scene the desk left. Reuses the symbolic diagnostics and the
observable trace.

NOTE (Phase 1 limitation): the final sequence is rendered from the desk's *current* live scene —
the accepted round when it converges, else the last round. True best-round selection needs the
Phase 2 persistent-.blend handoff so an earlier, higher-scoring round can be re-opened.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import base64

from agents.desk_log import make_message_logger
from agents.desk_memory import LessonBook, ShotContext, default_lessons_path
from agents.motion_critic import MotionCritique, critique_motion
from agents.scene_builder import DeskResult, ReferenceImage, build_scene
from develop.agents import Render3D
from develop.ledger import BeatEntry, Clip, Layer
from develop.verdict import Lever, Verdict
from footage.inspect import FrameSample
from scene.critic import load_reference_images
from scene.diagnostics import black_frame_hint, is_black, scene_digest
from scene.harness import Iteration

DeskBuilder = Callable[..., Awaitable[DeskResult]]
Critic = Callable[..., Awaitable[MotionCritique]]
Progress = Callable[[str], None]
_DEFAULT_ACCEPT_SCORE = 0.75


@dataclass
class AnimationResult:
    iterations: list[Iteration]
    frames: int
    final_dir: str | None  # where the full best sequence was rendered
    accept_score: float = _DEFAULT_ACCEPT_SCORE

    @property
    def best(self) -> Iteration | None:
        rendered = [it for it in self.iterations if it.render_path is not None]
        return max(rendered, key=lambda it: (it.score, it.index)) if rendered else (self.iterations[-1] if self.iterations else None)

    @property
    def converged(self) -> bool:
        b = self.best
        return b is not None and (b.passed or b.score >= self.accept_score)


def _strip_frame_numbers(frames: int) -> list[int]:
    """The three frames the motion critic judges: first, middle, last."""
    return [1, max(2, frames // 2), frames]


async def build_animation(
    *,
    bridge,
    brief: str,
    reference_images: list[ReferenceImage],
    subject: str,
    out_dir: str | Path,
    frames: int = 36,
    max_iters: int = 4,
    accept_score: float = _DEFAULT_ACCEPT_SCORE,
    resolution: tuple[int, int] = (640, 360),
    samples: int = 24,
    builder: DeskBuilder = build_scene,
    critic: Critic = critique_motion,
    on_progress: Progress | None = None,
) -> AnimationResult:
    log = on_progress or (lambda _m: None)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    trace = out / "trace.log"

    def emit(line: str) -> None:
        with trace.open("a") as fh:
            fh.write(line + "\n")
        log(line)

    def reasoning_md(i: int, desk_note: str, critic_raw: str, feedback: str | None) -> None:
        body = [f"# round {i}", "", "## desk submission", "", desk_note or "(none)", ""]
        if critic_raw:
            body += ["## motion dailies — critic reasoning + verdict", "", critic_raw, ""]
        if feedback:
            body += ["## notes handed back to the desk for the next round", "", feedback, ""]
        (out / f"round_{i:02d}.reasoning.md").write_text("\n".join(body))

    def render_strip(i: int) -> tuple[list[FrameSample], dict]:
        """Render the first/middle/last frames of the animation and return them + the start's stats."""
        strip: list[FrameSample] = []
        start_stats: dict = {}
        for j, fnum in enumerate(_strip_frame_numbers(frames)):
            bridge.run_python(f"import bpy\nbpy.context.scene.frame_set({fnum})\nresult={{'f':{fnum}}}")
            path = out / f"round_{i:02d}_f{fnum:04d}.jpg"
            shot = bridge.render(path=str(path), format="JPEG", resolution=list(resolution), samples=samples, return_base64=True)
            strip.append(FrameSample(timecode=f"f{fnum:04d}", seconds=float(fnum), jpeg_b64=shot.get("image_b64", "")))
            if j == 0:
                start_stats = bridge.image_stats(path=str(path))
        return strip, start_stats

    ctx = ShotContext(path=out / "context.md")
    ctx.set_intent(brief)
    lessons = LessonBook(path=default_lessons_path())
    iterations: list[Iteration] = []
    session: str | None = None
    last_submitted = True
    feedback: str | None = None
    anim_note = f" (these are the START/MIDDLE/END frames of a {frames}-frame animation)"

    for i in range(max_iters):
        emit(f"round {i} | " + ("animating at desk (first pass)" if i == 0 else "revising motion from dailies"))
        logger = make_message_logger(out / f"round_{i:02d}.transcript.md", out / f"round_{i:02d}.messages.jsonl",
                                     title=f"motion desk — round {i}")
        # Don't resume a wrapped-up session (history says "tools removed" → revise no-ops); start
        # fresh on the existing scene instead. Only round 0 clears the scene (fresh=True).
        resume = session if (session and last_submitted) else None
        desk = await builder(bridge=bridge, brief=brief, reference_images=reference_images,
                             feedback=feedback, resume=resume, fresh=(i == 0), animation_frames=frames,
                             context=ctx.render(), lessons=lessons.render(), on_message=logger)
        session = desk.session_id
        last_submitted = desk.submitted
        note = (desk.note or "").strip()
        ctx.add_decision(f"round {i}", note)
        lessons.harvest_and_add(note)
        code_log = "\n\n".join(desk.bpy_log)
        (out / f"round_{i:02d}.bpy.py").write_text(code_log)
        if note:
            emit(f"round {i} | desk: {note[:200]}" + ("" if desk.submitted else " [wrapped up — not a clean submit]"))

        # Snapshot this round's animated scene — so the FINAL sequence can be rendered from the
        # BEST round (via open_blend), not merely whatever the last round left live.
        blend_path: str | None = str(out / f"round_{i:02d}.blend")
        try:
            bridge.save_blend(path=blend_path)
        except Exception:
            blend_path = None

        strip, start_stats = render_strip(i)
        digest = scene_digest(bridge.get_scene_graph())
        start_path = str(out / f"round_{i:02d}_f0001.jpg")
        # a black START frame is broken (the blackout is the MIDDLE, by design — never the start)
        if is_black(start_stats):
            hint = black_frame_hint(digest)
            verdict = Verdict(beat="shot", layer=Layer.CLIP, passed=False, lever=Lever.RE_SOURCE, reason="start frame is black", confidence=0.9)
            iterations.append(Iteration(i, code_log, start_path, strip[0], verdict, score=0.0, builder_raw=note, feedback_out=hint, blend_path=blend_path))
            reasoning_md(i, note, "(critic skipped — black start)", hint)
            emit(f"round {i} | BLACK start — skipping critic | {digest}")
            feedback = hint
            continue

        critique = await critic(beat_id="shot", brief=brief, strip_frames=strip)
        verdict = critique.verdict
        done = verdict.passed or critique.score >= accept_score
        dims = " ".join(f"{k}={'OK' if v else 'FIX'}" for k, v in verdict.dimensions.items())
        next_feedback = None if done else (
            f"Motion match {critique.score:.2f}/1.0 (need >= {accept_score:.2f}). Per-beat: {dims}. "
            f"Keep what reads; fix the FIX beats. Critic: {verdict.reason}{anim_note}. {digest}"
        )
        iterations.append(Iteration(i, code_log, start_path, strip[0], verdict, score=critique.score, builder_raw=note, critic_raw=critique.raw, feedback_out=next_feedback, blend_path=blend_path))
        reasoning_md(i, note, critique.raw, next_feedback)
        emit(f"round {i} | motion {critique.score:.2f} | {dims} | {'DONE' if done else 'REVISE'}: {verdict.reason[:120]}")
        if done:
            break
        feedback = next_feedback

    # Render the full sequence from the BEST round — reopen its .blend snapshot so a later, worse (or
    # black) round can't be the one shipped. Falls back to the live scene only if the snapshot is missing.
    final_dir: str | None = None
    rendered = [it for it in iterations if it.render_path is not None]
    best = max(rendered, key=lambda it: (it.score, it.index)) if rendered else None
    if best is not None and best.score > 0:
        if best.blend_path:
            emit(f"opening best round's snapshot (round {best.index}, motion {best.score:.2f}) for the final render")
            try:
                bridge.open_blend(path=best.blend_path)
            except Exception:
                emit("open_blend failed — rendering from the live scene instead")
        else:
            emit(f"rendering final sequence from the live scene (best motion {best.score:.2f})")
        seq = bridge.render_sequence(start=1, end=frames, step=1, prefix="final", dir=str(out / "final"), resolution=list(resolution), samples=samples)
        final_dir = seq.get("dir")
    best_score = best.score if best else 0.0
    emit(f"done | rounds {len(iterations)} | best motion {best_score:.2f} | final={final_dir}")
    return AnimationResult(iterations=iterations, frames=frames, final_dir=final_dir, accept_score=accept_score)


# --- develop realizer: build_animation as the leaf for 3D/motion beats --------------


def beat_motion_brief(subject: str, vo: str = "", evidence: str = "") -> str:
    """A moving-shot brief from a beat's intent — a faithful reconstruction with a purposeful move."""
    parts = [f"Reconstruct a short cinematic MOVING shot of a real scene: {subject.strip()}."]
    if vo.strip():
        parts.append(f"It illustrates: {vo.strip()}.")
    parts.append(
        "Depict the scene faithfully with a clear subject, and give it a subtle, purposeful camera "
        "move across the shot (a slow push in, a reveal, or a drift) — not a static frame."
    )
    if evidence.strip():
        parts.append(f"Anchored on: {evidence.strip()}.")
    return " ".join(parts)


def _sequence_clip_frames(seq_dir: Path, frames: int) -> tuple[FrameSample, ...]:
    """Sample first/middle/last frames of the rendered sequence for the develop sighted critic."""
    out: list[FrameSample] = []
    for fnum in _strip_frame_numbers(frames):
        path = seq_dir / f"final_{fnum:04d}.jpg"
        if path.exists():
            out.append(FrameSample(timecode=f"f{fnum:04d}", seconds=float(fnum), jpeg_b64=base64.b64encode(path.read_bytes()).decode("ascii")))
    return tuple(out)


def make_animation_realizer(
    *,
    bridge,
    out_dir: str | Path,
    frames: int = 36,
    max_iters: int = 3,
    resolution: tuple[int, int] = (640, 360),
    samples: int = 24,
    refs_subdir: str = "refs",
    _build: Callable[..., Awaitable[AnimationResult]] = build_animation,
) -> Render3D:
    """A ``render_3d`` leaf that realizes a 3D/motion beat as an ANIMATION via :func:`build_animation`.

    The animation twin of ``make_scene_builder``: same seam, same Clip output, but the clip is a
    rendered sequence. Returns a Clip whose frames are the first/middle/last of the sequence (so the
    develop sighted critic can judge it) and whose ``render_meta`` carries the motion score + dir.
    Caller owns the bridge lifecycle."""

    async def render(entry: BeatEntry) -> Clip:
        beat_dir = Path(out_dir) / entry.id
        beat_dir.mkdir(parents=True, exist_ok=True)
        subject = entry.intent.subject or entry.intent.heading
        vo = entry.realization.vo if entry.realization else ""
        references = load_reference_images(beat_dir / refs_subdir)
        brief = beat_motion_brief(subject, vo, entry.intent.evidence)
        result = await _build(
            bridge=bridge, brief=brief, reference_images=references, subject=subject,
            out_dir=beat_dir, frames=frames, max_iters=max_iters, resolution=resolution, samples=samples,
        )
        best = result.best
        if best is None or best.render_path is None or result.final_dir is None:
            return Clip(licence="KNOWN", acquisition_gap=f"3D animation failed for {subject!r}")
        clip_frames = _sequence_clip_frames(Path(result.final_dir), frames)
        return Clip(
            fetched_path=Path(result.final_dir),
            licence="KNOWN",
            frames=clip_frames,
            render_meta={"kind": "animation", "frames": str(frames), "motion_score": f"{best.score:.2f}", "subject": subject},
        )

    return render
