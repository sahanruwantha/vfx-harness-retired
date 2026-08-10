"""The review/handoff loop for one shot: spawn the desk, render it, run dailies, hand notes back.

Give it a brief ("what needs to happen") and reference image(s); it spawns the agent-driven desk
(:func:`agents.scene_builder.build_scene`), which builds the shot in the live Blender and submits.
The orchestrator then renders the *authoritative* deliverable frame, has the scene critic score it
against the reference (dailies), and — if it isn't there yet — resumes the SAME desk session with
the critic's notes for a revision. This is the division a real pipeline has: the desk is the artist
(hands + its own eyes), the critic is the independent supervisor, and this loop is the dailies cycle
that makes them add up to a finished shot.

The desk owns building and self-correction (it has live tools and works incrementally), so this
loop no longer execs code or self-heals tracebacks — it reviews. The feedback handed back is the
critic's per-axis pass/fail plus a 0-1 match score, so the desk keeps what matches and fixes only
what fails; convergence is a *threshold* on that score, not the critic's strict binary pass. Builder
and critic are injected; the loop is plain, testable control flow. A near-black deliverable is a
pipeline failure — the critic is skipped and the desk gets a symbolic diagnostic to fix instead.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from agents.desk_log import make_message_logger
from agents.desk_memory import LessonBook, ShotContext, default_lessons_path
from agents.scene_builder import DeskResult, ReferenceImage, build_scene
from agents.art_director import critique_craft
from agents.scene_critic import SceneCritique, critique_scene
from contracts.ledger import Layer
from contracts.verdict import Lever, Verdict
from contracts.ledger import FrameSample
from scene.diagnostics import black_frame_hint, is_black, scene_digest

Progress = Callable[[str], None]
DeskBuilder = Callable[..., Awaitable[DeskResult]]
Critic = Callable[..., Awaitable[SceneCritique]]

_DEFAULT_ACCEPT_SCORE = 0.8


@dataclass
class Iteration:
    index: int
    code: str
    render_path: str | None  # None when the code errored before a render
    render_frame: FrameSample | None
    verdict: Verdict | None
    score: float = 0.0
    error: str | None = None
    builder_raw: str = ""  # the builder's full reply (its reasoning + the code) — the reasoning log
    critic_raw: str = ""  # the critic's full reply (its reasoning + the verdict JSON)
    feedback_out: str | None = None  # the revision instruction handed to the next iteration
    blend_path: str | None = None  # the .blend snapshot of this round's scene (persistent handoff)

    @property
    def passed(self) -> bool:
        return self.verdict is not None and self.verdict.passed


def _dims_line(verdict: Verdict | None) -> str:
    if verdict is None or not verdict.dimensions:
        return ""
    short = {"composition": "comp", "camera": "cam", "lighting": "light", "palette": "pal", "subject_match": "subj"}
    return " ".join(f"{short.get(k, k)}={'OK' if v else 'FIX'}" for k, v in verdict.dimensions.items())


@dataclass
class ShotResult:
    iterations: list[Iteration]
    accept_score: float = _DEFAULT_ACCEPT_SCORE

    @property
    def best(self) -> Iteration | None:
        """The highest-scoring iteration that actually rendered (ties → latest)."""
        rendered = [it for it in self.iterations if it.render_path is not None]
        if not rendered:
            return self.iterations[-1] if self.iterations else None
        return max(rendered, key=lambda it: (it.score, it.index))

    @property
    def converged(self) -> bool:
        best = self.best
        return best is not None and (best.passed or best.score >= self.accept_score)


def format_feedback(critique: SceneCritique, accept_score: float) -> str:
    """Turn a critique into actionable feedback: the score, which axes hold, and what to fix."""
    verdict = critique.verdict
    axes = ", ".join(f"{name}={'OK' if ok else 'FIX'}" for name, ok in verdict.dimensions.items())
    parts = [f"Match score {critique.score:.2f}/1.0 (need >= {accept_score:.2f} to finish)."]
    if axes:
        parts.append(f"Per-axis: {axes}. Keep every axis marked OK; change only the ones marked FIX.")
    parts.append(f"Critic note: {verdict.reason}")
    return " ".join(parts)


async def build_shot(
    *,
    bridge,
    brief: str,
    reference_images: list[ReferenceImage],
    subject: str,
    out_dir: str | Path,
    max_rounds: int = 3,
    accept_score: float = _DEFAULT_ACCEPT_SCORE,
    resolution: tuple[int, int] = (768, 432),
    samples: int = 64,
    builder: DeskBuilder = build_scene,
    critic: Critic = critique_craft,
    prepare: Callable[[], None] | None = None,
    on_progress: Progress | None = None,
) -> ShotResult:
    """Run desk→render→dailies→revise up to *max_rounds* times, stopping when the match score reaches
    *accept_score* (or the critic passes). Round 0 spawns a fresh desk; later rounds resume the same
    desk session with the critic's notes so it revises the live scene it already built.

    ``prepare`` (optional) runs ONCE before round 0 to set up a scene the desk then builds ON TOP of —
    e.g. a plate backdrop + camera for a hybrid shot. When given, round 0 does NOT clean-slate (the
    desk builds on the prepared scene), so ``prepare`` owns clearing/setup."""
    log = on_progress or (lambda _m: None)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    trace = out / "trace.log"

    def emit(line: str) -> None:
        """A monitorable one-liner: appended to trace.log (for Monitor to tail) and to on_progress."""
        with trace.open("a") as fh:
            fh.write(line + "\n")
        log(line)

    def reasoning_md(i: int, desk_note: str, critic_raw: str, feedback: str | None) -> None:
        body = [f"# round {i}", "", "## desk submission", "", desk_note or "(none)", ""]
        if critic_raw:
            body += ["## dailies — critic reasoning + verdict", "", critic_raw, ""]
        if feedback:
            body += ["## notes handed back to the desk for the next round", "", feedback, ""]
        (out / f"round_{i:02d}.reasoning.md").write_text("\n".join(body))

    ctx = ShotContext(path=out / "context.md")
    ctx.set_intent(brief)
    lessons = LessonBook(path=default_lessons_path())
    if prepare is not None:
        prepare()  # set up the scene the desk builds on (e.g. plate backdrop + camera)
    iterations: list[Iteration] = []
    session: str | None = None
    last_submitted = True
    feedback: str | None = None

    for i in range(max_rounds):
        emit(f"round {i} | " + ("building at desk (first pass)" if i == 0 else "revising from dailies"))
        logger = make_message_logger(out / f"round_{i:02d}.transcript.md", out / f"round_{i:02d}.messages.jsonl",
                                     title=f"scene desk — round {i}")
        # Don't resume a wrapped-up session (its history says "tools removed" and the revise no-ops);
        # start fresh on the existing scene instead. Only round 0 clears the scene (fresh=True).
        resume = session if (session and last_submitted) else None
        desk = await builder(
            bridge=bridge, brief=brief, reference_images=reference_images,
            feedback=feedback, resume=resume, fresh=(i == 0 and prepare is None),
            context=ctx.render(), lessons=lessons.render(), on_message=logger,
        )
        session = desk.session_id
        last_submitted = desk.submitted
        note = (desk.note or "").strip()
        ctx.add_decision(f"round {i}", note)
        lessons.harvest_and_add(note)
        code_log = "\n\n".join(desk.bpy_log)
        (out / f"round_{i:02d}.bpy.py").write_text(code_log)
        if note:
            emit(f"round {i} | desk: {note[:200]}" + ("" if desk.submitted else " [wrapped up — not a clean submit]"))

        # Snapshot this round's scene to a versioned .blend — the persistent handoff: the round's
        # state is recoverable (repro, best-round selection, later department stages), not just its jpg.
        blend_path: str | None = str(out / f"round_{i:02d}.blend")
        try:
            bridge.save_blend(path=blend_path)
        except Exception:
            blend_path = None

        # Render the AUTHORITATIVE deliverable (the desk's own renders were preview-quality, for its
        # eyes only). Then inspect the scene + frame before judging — symbolic ground truth the desk
        # is blind to across the handoff. A near-black deliverable is a pipeline failure: skip the
        # (wasted) critic and hand the desk the digest so it can diagnose instead of guessing.
        path = out / f"round_{i:02d}.jpg"
        shot = bridge.render(path=str(path), format="JPEG", resolution=list(resolution), samples=samples, return_base64=True)
        frame = FrameSample(timecode="00:00", seconds=0.0, jpeg_b64=shot.get("image_b64", ""))
        digest = scene_digest(bridge.get_scene_graph())
        if is_black(bridge.image_stats(path=str(path))):
            hint = black_frame_hint(digest)
            verdict = Verdict(beat="shot", layer=Layer.CLIP, passed=False, lever=Lever.RE_SOURCE,
                              reason="render is nearly black", confidence=0.9)
            iterations.append(Iteration(i, code_log, str(path), frame, verdict, score=0.0,
                                        builder_raw=note, feedback_out=hint, blend_path=blend_path))
            reasoning_md(i, note, "(critic skipped — black render)", hint)
            emit(f"round {i} | BLACK render — skipping critic | {digest}")
            feedback = hint
            continue

        critique = await critic(
            beat_id="shot", subject=subject, reference_images=reference_images, render_frames=[frame],
        )
        verdict = critique.verdict
        done = verdict.passed or critique.score >= accept_score
        next_feedback = None if done else f"{format_feedback(critique, accept_score)} | {digest}"
        iterations.append(Iteration(
            i, code_log, str(path), frame, verdict, score=critique.score,
            builder_raw=note, critic_raw=critique.raw, feedback_out=next_feedback, blend_path=blend_path,
        ))
        reasoning_md(i, note, critique.raw, next_feedback)
        emit(f"round {i} | score {critique.score:.2f} | {_dims_line(verdict)} | {'PASS' if done else 'REVISE'}: {verdict.reason[:130]}")
        if done:
            break
        feedback = next_feedback

    emit(f"done | rounds {len(iterations)} | converged {ShotResult(iterations, accept_score).converged} | best score {max((it.score for it in iterations), default=0):.2f}")
    return ShotResult(iterations=iterations, accept_score=accept_score)
