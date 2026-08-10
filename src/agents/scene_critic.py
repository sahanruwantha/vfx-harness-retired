"""Scene critic: judge a 3D render against a reference image by how closely it MATCHES it.

The sighted critic for reconstruction beats. Where the footage critic asks "does this clip show
the thing," this asks "does this render *look like the reference* — same composition, camera,
lighting, palette, mood." Because a 3D beat reconstructs a real thing with a real reference frame,
"does it match" is a measurable, gradeable question, not a taste call — the strongest calibration
anchor any critic in this pipeline gets (docs/3d-agent-architecture.md §8).

It is adversarial (find why the render FAILS to match; default to REJECT when unsure) and sighted
(handed the actual reference image and the actual render frames). A rejection names a **lever** so
the scheduler pulls the right layer — RE_SOURCE (push the scene closer: relight, reframe, regrade),
SWITCH_TO_MG (the reference look is atmospheric/graphic in a way 3D reconstruction cannot reach —
build it as motion graphics), or HUMAN_ACQUIRE (the reference/evidence cannot be built faithfully).
Same verdict contract and levers as the footage critic, so the existing routing is reused verbatim.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any, Literal

from claude_agent_sdk import (
    AgentDefinition,
    ClaudeAgentOptions,
    ResultMessage,
    query,
)
from _sdk import MAX_BUFFER_SIZE, VISUAL_EFFORT, VISUAL_MODEL

from production import FACELESS_ONELINE
from develop.ledger import Layer
from develop.verdict import Lever, Verdict
from footage.inspect import FrameSample

MODEL = VISUAL_MODEL
EFFORT = VISUAL_EFFORT

CRITIQUE_MAX_TURNS = 2
WRAP_UP_MAX_TURNS = 1
TASK_BUDGET_TOKENS = 24_000

# A reference image passed to the critic: its media type and base64 data.
ReferenceImage = tuple[str, str]

# Only scene-stage levers are valid here; anything else is coerced to a safe escalation.
_LEVER_BY_NAME: dict[str, Lever] = {
    Lever.RE_SOURCE.value: Lever.RE_SOURCE,
    Lever.SWITCH_TO_MG.value: Lever.SWITCH_TO_MG,
    Lever.HUMAN_ACQUIRE.value: Lever.HUMAN_ACQUIRE,
}

PROMPT = (
    "You are a scene-match critic for " + FACELESS_ONELINE + ".\n\n"
    """You are shown a REFERENCE image (the target look for one shot) and the ACTUAL RENDER frames
of a 3D scene built to match it. Your job is adversarial: find the reason the render FAILS to match
the reference. Judge only from the images you can see. When you are unsure, REJECT.

Compare the render to the reference on these axes:
- composition: subject placement, framing, scale in frame, negative space, foreground/background layering.
- camera: angle, height, and lens feel (wide vs tele) — does the render see the subject as the reference does?
- lighting: key direction, hardness, and contrast ratio — do the shadows and highlights fall as in the reference?
- palette: dominant colours, colour temperature, and overall grade/mood.
- subject_match: is the render actually depicting the same thing the reference depicts?

Then decide. If the render matches the reference well on all axes, PASS. Otherwise REJECT and choose
exactly one lever:
- "re_source": the render is close but off on one or more axes that a re-render can fix (relight,
  reframe, regrade, rescale) — push the scene closer to the reference.
- "switch_to_mg": the reference look is fundamentally atmospheric or graphic (volumetric fog, a sea
  of city lights, abstract glow) in a way a reconstructed 3D object cannot reach — stop and build it
  as motion graphics instead.
- "human_acquire": the reference cannot be reconstructed faithfully from what is available — escalate.

Also give a "match" score from 0.0 to 1.0 — overall how close the render is to the reference
(0 = unrelated, 0.5 = right idea wrong execution, 0.8 = clearly the same shot with minor gaps,
1.0 = indistinguishable). Be a calibrated grader on this number even when you REJECT: a render
that nails composition and palette but misses atmosphere is a high-0.6s, not a 0.2.

Return ONE JSON object and nothing else, in exactly this shape:

{"passed": true|false, "lever": "re_source"|"switch_to_mg"|"human_acquire"|null,
 "reason": "<one sentence naming the specific axis that fails and how, or why it matches>",
 "match": 0.0-1.0,
 "confidence": 0.0-1.0,
 "dimensions": {"composition": true|false, "camera": true|false, "lighting": true|false,
                "palette": true|false, "subject_match": true|false}}

"lever" MUST be null when passed is true, and a non-null lever when passed is false."""
)

WRAP_UP_PROMPT = """Output your verdict now as the single JSON object specified — no prose. \
If you could not judge the match from the images, reject with "re_source"."""

RECOVERABLE_SUBTYPES = {
    "error_max_turns",
    "error_max_budget_usd",
    "error_max_structured_output_retries",
}

SCENE_CRITIC = AgentDefinition(
    description="Adversarially judges how closely a 3D render matches a reference image.",
    prompt=PROMPT,
    model=MODEL,
    effort=EFFORT,
    tools=[],
    maxTurns=CRITIQUE_MAX_TURNS,
    permissionMode="dontAsk",
)
AGENTS = {"scene-critic": SCENE_CRITIC}


@dataclass(frozen=True)
class SceneCritique:
    verdict: Verdict
    outcome: Literal["complete", "wrapped_up", "no_frames", "no_reference"]
    stop_reason: str
    turns: int
    cost_usd: float
    score: float = 0.0  # 0-1 how close the render is to the reference (for threshold + tracking)
    raw: str = ""


# --- pure helpers (tested without the SDK) ------------------------------------------


def _clamp01(value: object, default: float = 0.5) -> float:
    try:
        return max(0.0, min(1.0, float(value)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _extract_json(text: str) -> dict[str, Any]:
    """Pull the JSON object out of the model's reply — see agents.jsonparse.loads_json (tolerates
    fences, prose, nested objects, and the odd stray-';'/trailing-comma glitch)."""
    from agents.jsonparse import loads_json

    return loads_json(text)


def parse_score(text: str, default: float = 0.0) -> float:
    """Pull the 0-1 ``match`` score out of the critic's reply, clamped; default on any failure."""
    try:
        return _clamp01(_extract_json(text).get("match"), default)
    except (ValueError, json.JSONDecodeError):
        return default


def parse_verdict(text: str, beat_id: str) -> Verdict:
    """Turn the critic's JSON reply into a Verdict. Unparseable / malformed → RE_SOURCE.

    Fails safe toward re-rendering, not escalation: a garbled scene critic should make the loop try
    again, never launder a mismatched render into the film.
    """
    try:
        data = _extract_json(text)
    except (ValueError, json.JSONDecodeError):
        return Verdict(
            beat=beat_id,
            layer=Layer.CLIP,
            passed=False,
            lever=Lever.RE_SOURCE,
            reason="scene critic returned unparseable output",
            confidence=0.5,
        )
    dims = {str(k): bool(v) for k, v in (data.get("dimensions") or {}).items()}
    confidence = _clamp01(data.get("confidence"))
    if data.get("passed") is True:
        return Verdict(beat=beat_id, layer=Layer.CLIP, passed=True, dimensions=dims, confidence=confidence)
    lever = _LEVER_BY_NAME.get(str(data.get("lever", "")).strip().lower(), Lever.RE_SOURCE)
    reason = str(data.get("reason", "")).strip() or "render does not match the reference"
    return Verdict(
        beat=beat_id,
        layer=Layer.CLIP,
        passed=False,
        lever=lever,
        reason=reason,
        dimensions=dims,
        confidence=confidence,
    )


def build_content(
    *,
    subject: str,
    reference_images: list[ReferenceImage],
    render_frames: list[FrameSample],
) -> list[dict[str, Any]]:
    """The user message: the subject, the reference image(s), then the render frame(s), then the ask."""
    brief = (
        f"SUBJECT: {subject.strip()}\n\n"
        "You are matching a RENDER to a REFERENCE. First the REFERENCE (the target look for this "
        "shot), then the RENDER frames (the 3D output to judge). Score how closely the render "
        "matches the reference."
    )
    blocks: list[dict[str, Any]] = [{"type": "text", "text": brief}]
    blocks.append({"type": "text", "text": f"REFERENCE — the target look ({len(reference_images)} image(s)):"})
    for media_type, data in reference_images:
        blocks.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}})
    blocks.append({"type": "text", "text": f"RENDER — the 3D output to judge ({len(render_frames)} frame(s)):"})
    for frame in render_frames:
        blocks.append({"type": "text", "text": f"Render frame at {frame.timecode}:"})
        blocks.append(
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": frame.jpeg_b64}}
        )
    blocks.append({"type": "text", "text": "Return your verdict as the single JSON object specified."})
    return blocks


async def _stream_one(blocks: list[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
    yield {
        "type": "user",
        "session_id": "",
        "message": {"role": "user", "content": blocks},
        "parent_tool_use_id": None,
    }


def _describe(message: ResultMessage) -> str:
    errors = message.errors or []
    detail = "\n" + "\n".join(errors) if errors else ""
    return f"{message.subtype}{detail}"


async def _run(prompt: Any, max_turns: int, resume: str | None, on_message) -> ResultMessage:
    options = ClaudeAgentOptions(
        max_buffer_size=MAX_BUFFER_SIZE,
        model=MODEL,
        effort=EFFORT,
        system_prompt=PROMPT,
        tools=[],
        allowed_tools=[],
        max_turns=max_turns,
        permission_mode="dontAsk",
        setting_sources=[],
        agents=AGENTS,
        resume=resume,
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
        raise RuntimeError("scene-critic ended without returning a result")
    return terminal


async def critique_scene(
    *,
    beat_id: str,
    subject: str,
    reference_images: list[ReferenceImage],
    render_frames: list[FrameSample],
    on_message: Callable[[object], None] | None = None,
) -> SceneCritique:
    """Judge one render against its reference. No render → RE_SOURCE; no reference → skip, no call."""
    if not render_frames:
        return SceneCritique(
            verdict=Verdict(
                beat=beat_id, layer=Layer.CLIP, passed=False, lever=Lever.RE_SOURCE,
                reason="no render frames to judge; re-render the scene", confidence=0.6,
            ),
            outcome="no_frames", stop_reason="no_frames", turns=0, cost_usd=0.0, score=0.0,
        )
    if not reference_images:
        return SceneCritique(
            verdict=Verdict(beat=beat_id, layer=Layer.CLIP, passed=True, confidence=0.5),
            outcome="no_reference", stop_reason="no_reference", turns=0, cost_usd=0.0, score=1.0,
        )

    blocks = build_content(subject=subject, reference_images=reference_images, render_frames=render_frames)
    result = await _run(_stream_one(blocks), CRITIQUE_MAX_TURNS, None, on_message)

    if not result.is_error:
        return SceneCritique(
            verdict=parse_verdict(result.result or "", beat_id),
            outcome="complete",
            stop_reason=result.terminal_reason or "completed",
            turns=result.num_turns,
            cost_usd=result.total_cost_usd or 0.0,
            score=parse_score(result.result or ""),
            raw=result.result or "",
        )
    if result.subtype not in RECOVERABLE_SUBTYPES:
        raise RuntimeError(f"scene-critic failed: {_describe(result)}")

    wrap = await _run(WRAP_UP_PROMPT, WRAP_UP_MAX_TURNS, result.session_id, on_message)
    if wrap.is_error:
        raise RuntimeError(f"scene-critic wrap-up failed: {_describe(wrap)}")
    return SceneCritique(
        verdict=parse_verdict(wrap.result or "", beat_id),
        outcome="wrapped_up",
        stop_reason=result.terminal_reason or result.subtype,
        turns=result.num_turns + wrap.num_turns,
        cost_usd=(result.total_cost_usd or 0.0) + (wrap.total_cost_usd or 0.0),
        score=parse_score(wrap.result or ""),
        raw=wrap.result or "",
    )
