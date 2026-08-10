"""Scene supervisor — the VFX supe who decides a shot's METHODOLOGY before any work starts.

Every prior stage assumed the answer was "build it in 3D." But the Silk Road runs proved procedural
geometry plateaus on atmospheric shots (~0.57) — those want an AI plate, not more geometry. A real
supervisor makes that call up front: is this shot full-CG, a matte-painting/plate, or a hybrid? This
agent is that call. Given a beat's intent (and reference, sighted), it returns a :class:`Methodology`
the router in :mod:`scene.supervisor` dispatches on — sending atmospheric shots to a plate instead of
grinding the department pipeline to the ceiling.

Bounded, tool-less, JSON out, with a fail-safe default (3D) so a bad reply never blocks a beat.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from agents.jsonparse import loads_json

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query
from _sdk import MAX_BUFFER_SIZE, VISUAL_EFFORT, VISUAL_MODEL

MODEL = VISUAL_MODEL
EFFORT = VISUAL_EFFORT
DECIDE_MAX_TURNS = 2
TASK_BUDGET_TOKENS = 20_000

VALID_KINDS = ("3D", "plate", "hybrid")
# The disciplines a shot element can be assigned to — a real supe's turnover tags each element with
# the department that realises it. "fx" is the one that adds a department to the 3D pipeline.
VALID_DISCIPLINES = ("3d", "fx", "plate", "environment")
ReferenceImage = tuple[str, str]

RECOVERABLE_SUBTYPES = {"error_max_turns", "error_max_budget_usd", "error_max_structured_output_retries"}

PROMPT = """You are a VFX SUPERVISOR breaking down ONE faceless-documentary shot before any work
starts. Decide the METHODOLOGY that will realise it best — the way a real supe assigns a shot to a
pipeline:

- "3D": full CG built in Blender. Best for DISCRETE objects and constructed scenes with clear,
  directable geometry you must control precisely — a workstation, a server room, a labelled tower,
  a diagram made physical.
- "plate": a single AI-generated cinematic IMAGE used as the shot. Best for ATMOSPHERIC or photoreal
  environments that procedural geometry cannot reach — volumetric storm skies, weather, haze, fire,
  painterly light, vast cityscapes or landscapes. If the shot is mostly MOOD and ATMOSPHERE rather
  than a specific directable object, choose plate.
- "hybrid": an AI atmosphere plate with a directable 3D subject composited over it. Choose it only
  when you genuinely need BOTH an unreachable atmosphere AND a specific element you must direct.

Also decide whether the shot MOVES (a camera move or animation) or is a STILL.

Then BREAK THE SHOT DOWN into its distinct visual ELEMENTS — the way a supe's turnover lists what
has to be built and by which department. Tag each element with the discipline that realises it:
- "3d": a directable built object or set (the hero geometry, a structure, a prop).
- "fx": a SIMULATED or VOLUMETRIC element — a storm, smoke, fire, haze, dust, clouds, particles,
  atmospheric density. Anything that is a *phenomenon* rather than a solid object. Tag it "fx" so the
  FX/simulation department builds it as a real volume, instead of the modeling desk faking it with a
  lumpy mesh. Be honest: if the shot has a storm sky or drifting haze, it HAS an fx element.
- "plate": an element best served by an AI-generated image (an unreachable distant environment/sky).
- "environment": the ground/world/backdrop context the hero sits in.
Most shots have 1-3 elements. A clean product-style shot may be a single "3d" element with no fx.

Think about what the shot actually needs, then output ONLY a JSON object, nothing else:
{"kind": "3D" | "plate" | "hybrid", "motion": true | false,
 "rationale": "one sentence on why this methodology",
 "elements": [{"name": "short label", "discipline": "3d"|"fx"|"plate"|"environment", "note": "what it is"}],
 "challenges": ["short", "risks"]}"""


@dataclass(frozen=True)
class ShotElement:
    """One element of a shot's breakdown — a thing to build and the department that builds it."""

    name: str
    discipline: str  # one of VALID_DISCIPLINES
    note: str = ""


@dataclass(frozen=True)
class Methodology:
    kind: str  # "3D" | "plate" | "hybrid"
    motion: bool
    rationale: str = ""
    elements: tuple[ShotElement, ...] = ()
    challenges: tuple[str, ...] = ()
    raw: str = ""

    @property
    def routing_key(self) -> str:
        """The realizer key the router dispatches on."""
        if self.kind == "plate":
            return "plate"
        if self.kind == "hybrid":
            return "hybrid"
        return "3d_motion" if self.motion else "3d_still"

    @property
    def needs_fx(self) -> bool:
        """Does the shot contain a simulated/volumetric element — i.e. does the 3D pipeline need the
        FX department inserted (``scene.departments.with_fx``)? The per-element decision the supe
        makes so a storm is built as a real volume, not faked by the modeling desk."""
        return any(el.discipline == "fx" for el in self.elements)

    @property
    def fx_elements(self) -> tuple[ShotElement, ...]:
        """The fx-tagged elements — their notes brief the FX desk on what to simulate."""
        return tuple(el for el in self.elements if el.discipline == "fx")


def _coerce_kind(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    return {"3d": "3D", "plate": "plate", "hybrid": "hybrid"}.get(v)


def _parse_elements(value: Any) -> tuple[ShotElement, ...]:
    """Coerce the ``elements`` array → ShotElements; skip malformed entries, drop unknown disciplines
    to the safe default '3d' (an unknown tag must never silently invent an fx pass)."""
    if not isinstance(value, list):
        return ()
    out: list[ShotElement] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        discipline = str(item.get("discipline", "")).strip().lower()
        if not name:
            continue
        if discipline not in VALID_DISCIPLINES:
            discipline = "3d"
        out.append(ShotElement(name=name, discipline=discipline, note=str(item.get("note", "")).strip()))
    return tuple(out)


def parse_methodology(text: str, *, default_motion: bool = True) -> Methodology:
    """Parse the supe's JSON reply → Methodology. Unparseable / invalid kind → fail-safe 3D.

    Uses the lenient loader so a single delimiter glitch (a stray ``;`` for a ``,``) doesn't discard
    the whole per-element breakdown and silently drop the shot back to a default with no FX."""
    try:
        data = loads_json(text or "")
        kind = _coerce_kind(data.get("kind"))
        if kind:
            challenges = data.get("challenges") or []
            if not isinstance(challenges, list):
                challenges = [str(challenges)]
            return Methodology(
                kind=kind,
                motion=bool(data.get("motion", default_motion)),
                rationale=str(data.get("rationale", "")).strip(),
                elements=_parse_elements(data.get("elements")),
                challenges=tuple(str(c).strip() for c in challenges if str(c).strip()),
                raw=text,
            )
    except (ValueError, AttributeError):
        pass
    return Methodology(kind="3D", motion=default_motion,
                       rationale="fail-safe default (supervisor reply unparseable)", raw=text)


def _content(subject: str, vo: str, evidence: str, reference_images: list[ReferenceImage]) -> list[dict[str, Any]]:
    lines = [f"SHOT INTENT — subject: {subject.strip()}"]
    if vo.strip():
        lines.append(f"voiceover it illustrates: {vo.strip()}")
    if evidence.strip():
        lines.append(f"anchored on: {evidence.strip()}")
    blocks: list[dict[str, Any]] = [{"type": "text", "text": "\n".join(lines)}]
    if reference_images:
        blocks.append({"type": "text", "text": f"REFERENCE — what the shot should look like ({len(reference_images)} image(s)):"})
        for media_type, data in reference_images:
            blocks.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}})
    blocks.append({"type": "text", "text": "Decide the methodology. Output ONLY the JSON."})
    return blocks


async def _stream_one(blocks: list[dict[str, Any]]):
    yield {"type": "user", "session_id": "", "message": {"role": "user", "content": blocks}, "parent_tool_use_id": None}


async def _run(prompt: Any, on_message) -> ResultMessage:
    options = ClaudeAgentOptions(
        max_buffer_size=MAX_BUFFER_SIZE, model=MODEL, effort=EFFORT, system_prompt=PROMPT,
        tools=[], allowed_tools=[], max_turns=DECIDE_MAX_TURNS, permission_mode="dontAsk",
        setting_sources=[], task_budget={"total": TASK_BUDGET_TOKENS},
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
        raise RuntimeError("scene-supervisor ended without returning a result")
    return terminal


async def decide_methodology(
    *,
    subject: str,
    vo: str = "",
    evidence: str = "",
    reference_images: list[ReferenceImage] | None = None,
    default_motion: bool = True,
    on_message: Callable[[object], None] | None = None,
) -> Methodology:
    """Decide 3D / plate / hybrid (+ motion) for one shot. Sighted when a reference is given; a
    recoverable limit or unparseable reply falls back to 3D so a beat is never blocked."""
    blocks = _content(subject, vo, evidence, reference_images or [])
    run = await _run(_stream_one(blocks), on_message)
    if run.is_error and run.subtype not in RECOVERABLE_SUBTYPES:
        raise RuntimeError(f"scene-supervisor failed: {run.subtype}")
    return parse_methodology(run.result or "", default_motion=default_motion)
