"""Per-department critics — each department reviewed by its own domain-expert eye, not a generic one.

A real studio doesn't review a modeling pass and a lighting pass with the same reviewer asking the
same question. Reusing one match/craft critic across stages is the root cause of mid-pipeline noise:
the match critic penalises a greybox modeling render for missing lighting/atmosphere that later
departments add. The fix isn't to tell a generic critic to "ignore" things — it's to give each
department a reviewer whose entire frame of reference IS that discipline, so out-of-scope concerns
never enter the judgment:

* :func:`critique_layout`  — LAYOUT/blocking: staging, composition, camera, silhouette/scale/place.
* :func:`critique_lookdev` — LOOK-DEV: materials, shading, palette, surface believability.
* :func:`critique_fx`      — FX/SIM: does the effect read as the phenomenon — density, turbulence, scale.
* :func:`critique_lighting`— LIGHTING/DP: key/fill/rim, contrast, motivation, mood, volumetrics.

(Comp/final keeps the art-director :func:`agents.art_director.critique_craft`.) Each emits the same
SceneCritique contract (dimensions + 0-1 score) so it drops into the department pipeline's critic
seam unchanged; only the reviewer's expertise differs. Reuses the match critic's JSON parsers.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

from claude_agent_sdk import AgentDefinition, ClaudeAgentOptions, ResultMessage, query
from _sdk import MAX_BUFFER_SIZE, VISUAL_EFFORT, VISUAL_MODEL

from production import FACELESS_ONELINE
from develop.ledger import Layer
from develop.verdict import Lever, Verdict
from footage.inspect import FrameSample
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
TASK_BUDGET_TOKENS = 24_000

_JSON_CONTRACT = (
    """Return ONE JSON object and nothing else:
{"passed": true|false, "lever": "re_source"|"switch_to_mg"|"human_acquire"|null,
 "reason": "<the specific, actionable note to fix, or why it is good>",
 "match": 0.0-1.0,
 "confidence": 0.0-1.0,
 "dimensions": {"composition": true|false, "camera": true|false, "lighting": true|false,
                "palette": true|false, "subject_match": true|false}}
"match" is your 0-1 quality score for THIS department's work. Mark a dimension true when it meets a
passing standard for this stage, false when it needs work. "lever" is null iff passed is true; reject
with "re_source" for fixable notes. Only judge and score what THIS department owns."""
)

LAYOUT_PROMPT = (
    "You are a LAYOUT / blocking supervisor reviewing a MODELING pass for " + FACELESS_ONELINE + ".\n\n"
    """You are shown the REFERENCE (target staging) and the RENDER of a blocking/greybox pass. Judge
ONLY the layout craft:
- composition: staging, framing, balance, depth, negative space, where the subject sits in frame.
- camera: angle, height, lens feel (wide/tele) — does it see the subject as the reference does?
- subject_match: does the built geometry read as the intended subject — silhouette, scale, proportion,
  massing, placement?
This is a BLOCKING pass: it is intentionally GREYBOX — unshaded, placeholder materials, no scene
lighting. That is CORRECT, not a fault. Do NOT comment on or score colour, materials, lighting,
bloom or atmosphere — later departments add those. Score = how well the shot is BLOCKED to the
reference's staging. Set the lighting/palette dimensions to true (out of scope).\n\n"""
    + _JSON_CONTRACT
)

LOOKDEV_PROMPT = (
    "You are a LOOK-DEV / shading supervisor reviewing a MATERIALS pass for " + FACELESS_ONELINE + ".\n\n"
    """The geometry and camera are already blocked. You are shown the REFERENCE (target look) and the
RENDER. Judge ONLY the shading craft:
- palette: dominant colours, colour temperature, saturation, contrast of the materials/shading vs the
  reference; do the emissive elements read the right hue and strength (not blown to white)?
- subject_match: do the materials make the subject read as the intended thing (surface, emission,
  roughness) — is it believable and consistent?
The scene may be UNLIT (no scene lights yet) — that is expected; judge the MATERIALS, not the
lighting or final atmosphere. Score = material/palette quality vs the reference. Set the composition,
camera and lighting dimensions to true (out of scope).\n\n"""
    + _JSON_CONTRACT
)

LIGHTING_PROMPT = (
    "You are a LIGHTING supervisor / DP reviewing a LIGHTING pass for " + FACELESS_ONELINE + ".\n\n"
    """The geometry and materials are built. You are shown the REFERENCE (target light and mood) and
the RENDER. Judge ONLY the lighting craft:
- lighting: key/fill/rim direction and balance, contrast ratio, motivation, mood, volumetrics/haze,
  exposure — does the light fall and read as the reference's, and is it evocative? Flag artefacts
  (clipping to white, flatness, fireflies/noise).
- palette: only insofar as the LIGHTING grade shifts the colour/mood toward the reference.
Assume geometry and materials are as intended — do not re-litigate them. Score = lighting quality vs
the reference. Set the composition, camera and subject_match dimensions to true (out of scope).\n\n"""
    + _JSON_CONTRACT
)

FX_PROMPT = (
    "You are an FX / SIMULATION supervisor reviewing an EFFECTS pass for " + FACELESS_ONELINE + ".\n\n"
    """The hero geometry, camera and materials are already built. You are shown the REFERENCE (the
target look of the effect) and the RENDER of the FX pass — a volumetric (storm/cloud/smoke/haze),
a particle system, or atmospheric density. Judge ONLY the effect's believability, reported on the
subject_match dimension:
- Does the effect read as the intended PHENOMENON, not as geometry pretending to be one? A real
  storm/cloud has soft, wispy density falloff and internal turbulence; the classic failure is a
  solid, tarry, lumpy blob — a sculpted mesh instead of a volume. Flag that hard.
- Scale and distribution: does the effect sit at the right scale in the world and disperse the way
  the reference's does (billowing, layered, dissipating at edges), or is it a uniform slab?
- Motion/turbulence read: even in a still, does the density suggest movement and detail at multiple
  scales, or is it a single smooth gradient?
The effect may be lit only by ambient/world light (final lighting comes next) — judge its FORM and
DENSITY, not the final key light or grade. Score = how believably the effect reads as the phenomenon
vs the reference. Report your verdict on subject_match; set composition, camera, palette and lighting
to true (out of scope for FX).\n\n"""
    + _JSON_CONTRACT
)

WRAP_UP_PROMPT = """Output your verdict now as the single JSON object specified — no prose. \
If you could not judge from the images, reject with "re_source"."""


def _build_content(*, discipline: str, subject: str, reference_images: list[ReferenceImage],
                   render_frames: list[FrameSample]) -> list[dict[str, Any]]:
    brief = (f"SHOT SUBJECT: {subject.strip()}\n\nReview this {discipline} pass. First the REFERENCE "
             f"(target), then the RENDER to judge — score only the {discipline} craft.")
    blocks: list[dict[str, Any]] = [{"type": "text", "text": brief}]
    if reference_images:
        blocks.append({"type": "text", "text": f"REFERENCE ({len(reference_images)} image(s)):"})
        for media_type, data in reference_images:
            blocks.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}})
    blocks.append({"type": "text", "text": f"RENDER — the {discipline} output to judge ({len(render_frames)} frame(s)):"})
    for frame in render_frames:
        blocks.append({"type": "text", "text": f"Render at {frame.timecode}:"})
        blocks.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": frame.jpeg_b64}})
    blocks.append({"type": "text", "text": "Return your verdict as the single JSON object specified."})
    return blocks


async def _stream_one(blocks: list[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
    yield {"type": "user", "session_id": "", "message": {"role": "user", "content": blocks}, "parent_tool_use_id": None}


def _make_critic(*, prompt: str, agent_name: str, discipline: str) -> Callable[..., Any]:
    agents = {agent_name: AgentDefinition(description=f"{discipline} department critic.", prompt=prompt,
                                          model=MODEL, effort=EFFORT, tools=[], maxTurns=CRITIQUE_MAX_TURNS,
                                          permissionMode="dontAsk")}

    async def _run(prompt_in: Any, max_turns: int, resume: str | None, on_message) -> ResultMessage:
        options = ClaudeAgentOptions(
            max_buffer_size=MAX_BUFFER_SIZE, model=MODEL, effort=EFFORT, system_prompt=prompt,
            tools=[], allowed_tools=[], max_turns=max_turns, permission_mode="dontAsk",
            setting_sources=[], agents=agents, resume=resume,
            task_budget={"total": TASK_BUDGET_TOKENS} if resume is None else None,
        )
        terminal: ResultMessage | None = None
        try:
            async for message in query(prompt=prompt_in, options=options):
                if on_message is not None:
                    on_message(message)
                if isinstance(message, ResultMessage):
                    terminal = message
        except Exception:
            if terminal is None:
                raise
        if terminal is None:
            raise RuntimeError(f"{discipline}-critic ended without returning a result")
        return terminal

    async def critique(*, beat_id: str, subject: str, reference_images: list[ReferenceImage],
                       render_frames: list[FrameSample], on_message: Callable[[object], None] | None = None) -> SceneCritique:
        if not render_frames:
            return SceneCritique(
                verdict=Verdict(beat=beat_id, layer=Layer.CLIP, passed=False, lever=Lever.RE_SOURCE,
                                reason="no render frames to judge; re-render the scene", confidence=0.6),
                outcome="no_frames", stop_reason="no_frames", turns=0, cost_usd=0.0, score=0.0)
        blocks = _build_content(discipline=discipline, subject=subject, reference_images=reference_images, render_frames=render_frames)
        result = await _run(_stream_one(blocks), CRITIQUE_MAX_TURNS, None, on_message)
        if not result.is_error:
            return SceneCritique(verdict=parse_verdict(result.result or "", beat_id), outcome="complete",
                                 stop_reason=result.terminal_reason or "completed", turns=result.num_turns,
                                 cost_usd=result.total_cost_usd or 0.0, score=parse_score(result.result or ""), raw=result.result or "")
        if result.subtype not in RECOVERABLE_SUBTYPES:
            raise RuntimeError(f"{discipline}-critic failed: {_describe(result)}")
        wrap = await _run(WRAP_UP_PROMPT, WRAP_UP_MAX_TURNS, result.session_id, on_message)
        if wrap.is_error:
            raise RuntimeError(f"{discipline}-critic wrap-up failed: {_describe(wrap)}")
        return SceneCritique(verdict=parse_verdict(wrap.result or "", beat_id), outcome="wrapped_up",
                             stop_reason=result.terminal_reason or result.subtype, turns=result.num_turns + wrap.num_turns,
                             cost_usd=(result.total_cost_usd or 0.0) + (wrap.total_cost_usd or 0.0),
                             score=parse_score(wrap.result or ""), raw=wrap.result or "")

    return critique


critique_layout = _make_critic(prompt=LAYOUT_PROMPT, agent_name="layout-critic", discipline="layout")
critique_lookdev = _make_critic(prompt=LOOKDEV_PROMPT, agent_name="lookdev-critic", discipline="look-dev")
critique_fx = _make_critic(prompt=FX_PROMPT, agent_name="fx-critic", discipline="fx")
critique_lighting = _make_critic(prompt=LIGHTING_PROMPT, agent_name="lighting-critic", discipline="lighting")
