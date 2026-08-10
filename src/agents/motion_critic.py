"""Motion critic: judge whether a start / middle / end strip achieves a described motion ARC.

The scene critic matches one frame to one reference — wrong for a moving shot, where the middle is
*supposed* to differ (a blackout, an inversion, a dissolve) and the end is a new pose. This judges
the three sampled frames of an animation as a motion: does the start show the start pose, the middle
the turning point, the end the end pose, and do they read as one continuous move? Same verdict
contract and levers as the other critics, so the animation loop reuses the routing.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any, Literal

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query
from _sdk import MAX_BUFFER_SIZE, VISUAL_EFFORT, VISUAL_MODEL

from production import FACELESS_ONELINE
from develop.ledger import Layer
from develop.verdict import Lever, Verdict
from develop.ledger import FrameSample

MODEL = VISUAL_MODEL
EFFORT = VISUAL_EFFORT
CRITIQUE_MAX_TURNS = 2
TASK_BUDGET_TOKENS = 24_000
RECOVERABLE_SUBTYPES = {"error_max_turns", "error_max_budget_usd", "error_max_structured_output_retries"}
_LEVER_BY_NAME = {Lever.RE_SOURCE.value: Lever.RE_SOURCE, Lever.HUMAN_ACQUIRE.value: Lever.HUMAN_ACQUIRE}

PROMPT = (
    "You are a motion critic for " + FACELESS_ONELINE + ".\n\n"
    """You are shown the START, MIDDLE and END frames sampled from a short animation, plus a
description of the MOTION it must achieve. Judge whether the animation ARC reads correctly — the
middle and end are SUPPOSED to differ from the start, so do NOT penalise change; penalise a motion
that does not match the description. When unsure, REJECT.

Test:
- start_pose: does the START frame show the described starting state/pose?
- transition: does the MIDDLE frame show the described mid-point — the turning point, blackout,
  inversion, or dissolve the description calls for?
- end_pose: does the END frame show the described ending state/pose?
- continuity: do the three read as ONE continuous move (consistent world/subject logic), not three
  unrelated images?

Give a "match" score 0.0-1.0 = how fully the sequence achieves the described motion (0 = unrelated,
0.8 = clearly the described move with minor gaps, 1.0 = exactly it). Return ONE JSON object:

{"passed": true|false, "lever": "re_source"|"human_acquire"|null,
 "reason": "<one sentence on the specific arc beat that works or fails>",
 "match": 0.0-1.0, "confidence": 0.0-1.0,
 "dimensions": {"start_pose": true|false, "transition": true|false, "end_pose": true|false, "continuity": true|false}}

"lever" MUST be null when passed is true, and non-null when passed is false."""
)

WRAP_UP_PROMPT = "Output your verdict now as the single JSON object specified — no prose."


@dataclass(frozen=True)
class MotionCritique:
    verdict: Verdict
    outcome: Literal["complete", "wrapped_up", "no_frames"]
    score: float
    cost_usd: float
    raw: str = ""


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
    try:
        return _clamp01(_extract_json(text).get("match"), default)
    except (ValueError, json.JSONDecodeError):
        return default


def parse_verdict(text: str, beat_id: str) -> Verdict:
    """Parse the motion critic's reply. Unparseable → RE_SOURCE (retry the animation, don't escalate)."""
    try:
        data = _extract_json(text)
    except (ValueError, json.JSONDecodeError):
        return Verdict(beat=beat_id, layer=Layer.CLIP, passed=False, lever=Lever.RE_SOURCE,
                       reason="motion critic returned unparseable output", confidence=0.5)
    dims = {str(k): bool(v) for k, v in (data.get("dimensions") or {}).items()}
    confidence = _clamp01(data.get("confidence"))
    if data.get("passed") is True:
        return Verdict(beat=beat_id, layer=Layer.CLIP, passed=True, dimensions=dims, confidence=confidence)
    lever = _LEVER_BY_NAME.get(str(data.get("lever", "")).strip().lower(), Lever.RE_SOURCE)
    reason = str(data.get("reason", "")).strip() or "motion does not match the description"
    return Verdict(beat=beat_id, layer=Layer.CLIP, passed=False, lever=lever, reason=reason,
                   dimensions=dims, confidence=confidence)


def build_content(*, brief: str, strip_frames: list[FrameSample]) -> list[dict[str, Any]]:
    """The message: the motion description, then the START / MIDDLE / END frames, then the ask."""
    labels = ["START", "MIDDLE", "END"]
    blocks: list[dict[str, Any]] = [{"type": "text", "text": f"MOTION the animation must achieve:\n{brief.strip()}"}]
    for idx, frame in enumerate(strip_frames):
        label = labels[idx] if idx < len(labels) else f"frame {idx}"
        blocks.append({"type": "text", "text": f"{label} frame:"})
        blocks.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": frame.jpeg_b64}})
    blocks.append({"type": "text", "text": "Return your verdict as the single JSON object specified."})
    return blocks


async def _stream_one(blocks: list[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
    yield {"type": "user", "session_id": "", "message": {"role": "user", "content": blocks}, "parent_tool_use_id": None}


async def _run(prompt: Any, max_turns: int, resume: str | None, on_message) -> ResultMessage:
    options = ClaudeAgentOptions(
        max_buffer_size=MAX_BUFFER_SIZE, model=MODEL, effort=EFFORT, system_prompt=PROMPT,
        tools=[], allowed_tools=[], max_turns=max_turns, permission_mode="dontAsk",
        setting_sources=[], agents={}, resume=resume,
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
        raise RuntimeError("motion-critic ended without a result")
    return terminal


async def critique_motion(
    *,
    beat_id: str,
    brief: str,
    strip_frames: list[FrameSample],
    on_message: Callable[[object], None] | None = None,
) -> MotionCritique:
    """Judge the start/middle/end strip against the described motion. <2 frames → RE_SOURCE, no call."""
    if len(strip_frames) < 2:
        return MotionCritique(
            verdict=Verdict(beat=beat_id, layer=Layer.CLIP, passed=False, lever=Lever.RE_SOURCE,
                            reason="not enough frames to judge motion", confidence=0.6),
            outcome="no_frames", score=0.0, cost_usd=0.0,
        )
    result = await _run(_stream_one(build_content(brief=brief, strip_frames=strip_frames)), CRITIQUE_MAX_TURNS, None, on_message)
    if not result.is_error:
        return MotionCritique(verdict=parse_verdict(result.result or "", beat_id), outcome="complete",
                              score=parse_score(result.result or ""), cost_usd=result.total_cost_usd or 0.0, raw=result.result or "")
    if result.subtype not in RECOVERABLE_SUBTYPES:
        raise RuntimeError(f"motion-critic failed: {result.subtype}")
    wrap = await _run(WRAP_UP_PROMPT, 1, result.session_id, on_message)
    return MotionCritique(verdict=parse_verdict(wrap.result or "", beat_id), outcome="wrapped_up",
                          score=parse_score(wrap.result or ""), cost_usd=(result.total_cost_usd or 0.0) + (wrap.total_cost_usd or 0.0), raw=wrap.result or "")
