"""Art-director critic: judge a render by CRAFT QUALITY, not pixel-match to the reference.

The match critic (:mod:`agents.scene_critic`) asks "does this reproduce the reference frame" — the
right question for reconstruction, but the wrong yardstick once the craft chain (Cycles + real
materials + comp) starts producing genuinely good frames: the depth-first tower looked dramatically
better yet still scored ~0.55, because it isn't a pixel copy of frame_00366. This critic is the
art director at dailies: shown the reference as the target STYLE and INTENT (not a pixel target), it
asks "is this a compelling, well-crafted, finished shot of this KIND" and returns specific,
actionable craft notes.

It emits the SAME verdict contract as the match critic (same SceneCritique, same dimension keys, the
0-1 score carried in ``match``) so it drops into the department pipeline's critic seam unchanged —
only the rubric differs. Reuses the match critic's JSON parsers.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

from claude_agent_sdk import AgentDefinition, ClaudeAgentOptions, ResultMessage, query
from _sdk import MAX_BUFFER_SIZE, VISUAL_EFFORT, VISUAL_MODEL

from production import FACELESS_ONELINE
from develop.ledger import Layer
from develop.verdict import Lever, Verdict
from develop.ledger import FrameSample
from agents.scene_critic import (
    RECOVERABLE_SUBTYPES,
    ReferenceImage,
    SceneCritique,
    _describe,
    parse_score,
    parse_verdict,
)

MODEL = VISUAL_MODEL
EFFORT = VISUAL_EFFORT
CRITIQUE_MAX_TURNS = 2
WRAP_UP_MAX_TURNS = 1
TASK_BUDGET_TOKENS = 26_000

PROMPT = (
    "You are a VFX ART DIRECTOR / DP reviewing a rendered frame at DAILIES for " + FACELESS_ONELINE + ".\n\n"
    """You are shown a REFERENCE (the target STYLE, mood and INTENT for the shot) and the RENDER to
review. Judge whether the render is a COMPELLING, BELIEVABLE, well-crafted shot IN THE REFERENCE'S
STYLE and genre — NOT a pixel-for-pixel copy. Exact reproduction of the reference's proportions,
camera, or layout is NOT required and is NOT the goal; capturing its mood, craft quality and intent
IS. Judge from the images you can see.

Review on craft:
- composition: is the framing strong and deliberate — staging, balance, depth, negative space?
- camera: is the lens/angle/height a good choice for this shot?
- lighting: believable and evocative — motivated key, shaping, contrast, mood — and free of artefacts
  (clipping to white, flatness, fireflies/noise)?
- palette: cohesive and on-genre — colour temperature, contrast, saturation, grade?
- subject_match: does the subject read clearly as the intended thing?
Also weigh FINISH: real atmosphere, bloom/glow where warranted, believable materials — does it look
like a FINISHED shot, not a grey preview?

Score 0.0-1.0 on overall CRAFT QUALITY — "would this pass in a real dailies review as a strong shot
of this kind": 0 = broken/preview, 0.5 = competent but rough, 0.75 = a good shot with minor notes,
0.9+ = excellent, finished. Grade QUALITY, not similarity to the reference frame.

Give SPECIFIC, ACTIONABLE art-director notes — name the concrete craft fixes, e.g. "key falloff too
hard, soften it and add a rim to separate the subject", "emission clipping to white — pull it down
and let comp carry the bloom", "fog reads milky — thin it and raise contrast". That note is what the
artist acts on next.

Return ONE JSON object and nothing else, in exactly this shape:

{"passed": true|false, "lever": "re_source"|"switch_to_mg"|"human_acquire"|null,
 "reason": "<the specific craft note(s) to act on, or why it is strong>",
 "match": 0.0-1.0,
 "confidence": 0.0-1.0,
 "dimensions": {"composition": true|false, "camera": true|false, "lighting": true|false,
                "palette": true|false, "subject_match": true|false}}

The "match" field is the CRAFT QUALITY score (named "match" only for pipeline compatibility). Each
dimension is true when that aspect is at a passing craft standard, false when it needs work. PASS
only a strong, finished shot of this kind; otherwise reject with "re_source" (craft fixes). Reserve
"switch_to_mg"/"human_acquire" for when this approach fundamentally cannot make the shot work.
"lever" MUST be null when passed is true, and non-null when passed is false."""
)

WRAP_UP_PROMPT = """Output your dailies verdict now as the single JSON object specified — no prose. \
If you could not judge from the images, reject with "re_source"."""

ART_DIRECTOR = AgentDefinition(
    description="Judges a render's craft quality (dailies), giving actionable notes — not pixel-match.",
    prompt=PROMPT, model=MODEL, effort=EFFORT, tools=[], maxTurns=CRITIQUE_MAX_TURNS, permissionMode="dontAsk",
)
AGENTS = {"art-director": ART_DIRECTOR}


def build_content(
    *,
    subject: str,
    reference_images: list[ReferenceImage],
    render_frames: list[FrameSample],
) -> list[dict[str, Any]]:
    """The dailies message: the subject/intent, the reference as a STYLE target, then the render."""
    brief = (
        f"SHOT INTENT: {subject.strip()}\n\n"
        "Review this RENDER at dailies. The REFERENCE below is the target STYLE and mood, NOT a pixel "
        "target — judge whether the render is a strong, finished shot of this kind and give craft notes."
    )
    blocks: list[dict[str, Any]] = [{"type": "text", "text": brief}]
    if reference_images:
        blocks.append({"type": "text", "text": f"REFERENCE — target style/mood ({len(reference_images)} image(s)):"})
        for media_type, data in reference_images:
            blocks.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}})
    blocks.append({"type": "text", "text": f"RENDER — the shot to review ({len(render_frames)} frame(s)):"})
    for frame in render_frames:
        blocks.append({"type": "text", "text": f"Render frame at {frame.timecode}:"})
        blocks.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": frame.jpeg_b64}})
    blocks.append({"type": "text", "text": "Return your dailies verdict as the single JSON object specified."})
    return blocks


async def _stream_one(blocks: list[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
    yield {"type": "user", "session_id": "", "message": {"role": "user", "content": blocks}, "parent_tool_use_id": None}


async def _run(prompt: Any, max_turns: int, resume: str | None, on_message) -> ResultMessage:
    options = ClaudeAgentOptions(
        max_buffer_size=MAX_BUFFER_SIZE, model=MODEL, effort=EFFORT, system_prompt=PROMPT,
        tools=[], allowed_tools=[], max_turns=max_turns, permission_mode="dontAsk",
        setting_sources=[], agents=AGENTS, resume=resume,
        task_budget={"total": TASK_BUDGET_TOKENS} if resume is None else None,
    )
    terminal: ResultMessage | None = None
    try:
        async for message in query(prompt=prompt, options=options):
            if on_message is not None:
                on_message(message)
            if isinstance(message, ResultMessage):
                terminal = message
    except Exception:
        if terminal is None:
            raise
    if terminal is None:
        raise RuntimeError("art-director ended without returning a result")
    return terminal


async def critique_craft(
    *,
    beat_id: str,
    subject: str,
    reference_images: list[ReferenceImage],
    render_frames: list[FrameSample],
    on_message: Callable[[object], None] | None = None,
) -> SceneCritique:
    """Judge one render's craft quality at dailies. Drop-in for the department critic seam. No render
    → RE_SOURCE. Unlike the match critic, a missing reference does NOT skip — craft is still judged."""
    if not render_frames:
        return SceneCritique(
            verdict=Verdict(beat=beat_id, layer=Layer.CLIP, passed=False, lever=Lever.RE_SOURCE,
                            reason="no render frames to judge; re-render the scene", confidence=0.6),
            outcome="no_frames", stop_reason="no_frames", turns=0, cost_usd=0.0, score=0.0,
        )
    blocks = build_content(subject=subject, reference_images=reference_images, render_frames=render_frames)
    result = await _run(_stream_one(blocks), CRITIQUE_MAX_TURNS, None, on_message)
    if not result.is_error:
        return SceneCritique(
            verdict=parse_verdict(result.result or "", beat_id), outcome="complete",
            stop_reason=result.terminal_reason or "completed", turns=result.num_turns,
            cost_usd=result.total_cost_usd or 0.0, score=parse_score(result.result or ""), raw=result.result or "",
        )
    if result.subtype not in RECOVERABLE_SUBTYPES:
        raise RuntimeError(f"art-director failed: {_describe(result)}")
    wrap = await _run(WRAP_UP_PROMPT, WRAP_UP_MAX_TURNS, result.session_id, on_message)
    if wrap.is_error:
        raise RuntimeError(f"art-director wrap-up failed: {_describe(wrap)}")
    return SceneCritique(
        verdict=parse_verdict(wrap.result or "", beat_id), outcome="wrapped_up",
        stop_reason=result.terminal_reason or result.subtype, turns=result.num_turns + wrap.num_turns,
        cost_usd=(result.total_cost_usd or 0.0) + (wrap.total_cost_usd or 0.0),
        score=parse_score(wrap.result or ""), raw=wrap.result or "",
    )
