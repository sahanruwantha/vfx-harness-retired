"""Stage 3 — the build + critic loop for one PLAN LAYER."""

from __future__ import annotations

import json
import re

from claude_agent_sdk import (
    ClaudeAgentOptions,
    query,
)

from vfx_harness.agents.build_prompts import (
    CRITIC_SYSTEM,
)
from vfx_harness.agents.builder.critic_focus import CRITIC_EFFORT
from vfx_harness.agents.builder.models import AXES_SYSTEM, DISTILL_SYSTEM, builder_model, critic_model, distiller_model
from vfx_harness.agents.builder.pkg import builder_package
from vfx_harness.agents.guardrails import distiller_hooks
from vfx_harness.domain.brief import Shot
from vfx_harness.infrastructure.config import (
    PROJECT_ROOT,
)
from vfx_harness.infrastructure.sandbox import sandbox_hooks
from vfx_harness.knowledge.recipes import RECIPES_DIR
from vfx_harness.observability import costlog
from vfx_harness.observability.log import (
    log,
    log_message,
)
from vfx_harness.observability.runid import RUN_ID
from vfx_harness.orchestration.ledger import Milestone, load_axes, load_layers
from vfx_harness.orchestration.plan_authority import selected_artifact_path


def _axes_options(shot: Shot) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        model=builder_model(),
        system_prompt=AXES_SYSTEM,
        cwd=str(shot.folder),
        hooks=sandbox_hooks(shot.folder, cwd=shot.folder),
        allowed_tools=["Read", "Glob"],
        disallowed_tools=["Write", "Edit", "Bash", "Grep", "WebFetch", "WebSearch", "Task", "Agent", "NotebookEdit"],
        permission_mode="bypassPermissions",
        max_buffer_size=32 * 1024 * 1024,
        setting_sources=[],
        max_turns=6,
        effort="low",  # one cheap classification
    )


def _critic_schema(
    axes: list[tuple[str, str]],
    *,
    allow_na: bool = True,
    focus_frames: list[int] | None = None,
) -> dict:
    """Force the verdict shape instead of regex-scraping the last {...} out of prose.
    Layer builds pass only their owned axes, so scope is no longer a model decision there.
    Full-rubric/acceptance calls may still need n/a for beat-specific axes."""
    numeric = {"type": "integer", "minimum": 0, "maximum": 5}
    score = {"anyOf": [numeric, {"type": "string", "enum": ["n/a"]}]} if allow_na else numeric
    return {
        "type": "object",
        "properties": {
            "scores": {
                "type": "object",
                "properties": {k: score for k, _ in axes},
                "required": [k for k, _ in axes],
                "additionalProperties": False,
            },
            "observations": {
                "type": "array",
                "maxItems": 4,
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "kind": {"type": "string", "enum": ["qualitative", "measurable"]},
                        "axis": {"type": "string", "enum": [k for k, _ in axes]},
                        "property": {"type": "string"},
                        "observation": {"type": "string"},
                        "action": {"type": "string"},
                        "moment": {
                            "type": "integer",
                            **({"enum": sorted(set(focus_frames))} if focus_frames else {"minimum": 1}),
                        },
                        "roles": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 1,
                        },
                        "claim_id": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                        "check_ids": {"type": "array", "items": {"type": "string"}},
                        "panel_ids": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": [
                        "id",
                        "kind",
                        "axis",
                        "property",
                        "observation",
                        "action",
                        "moment",
                        "roles",
                        "claim_id",
                        "check_ids",
                        "panel_ids",
                    ],
                    "additionalProperties": False,
                },
                "description": "One typed observation for each axis scored below 3. "
                "Bind planned defects to one exact claim and its evidence ids. Use a null "
                "claim_id only for a coverage defect absent from the supplied claim manifest.",
            },
            "focus_requests": {
                "type": "array",
                "maxItems": 2,
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "short stable id"},
                        "axis": {"type": "string", "enum": [k for k, _ in axes]},
                        "source": {
                            "type": "string",
                            "enum": ["candidate_frame", "motion_strip"],
                            "description": "coordinate space used by region",
                        },
                        "source_frame": {
                            "type": "integer",
                            **({"enum": sorted(set(focus_frames))} if focus_frames else {"minimum": 1}),
                            "description": "shot frame whose detail must be rerendered",
                        },
                        "region": {
                            "type": "array",
                            "items": {"type": "number", "minimum": 0, "maximum": 1},
                            "minItems": 4,
                            "maxItems": 4,
                        },
                        "reason": {"type": "string"},
                    },
                    "required": ["id", "axis", "source", "source_frame", "region", "reason"],
                    "additionalProperties": False,
                },
                "description": "At most two normalized TOP-LEFT regions needed to resolve "
                "a material below-3/uncertain visual decision. source=candidate_frame "
                "uses coordinates local to source_frame; source=motion_strip uses global "
                "strip coordinates and must remain inside that frame's one panel. Empty "
                "when the supplied images are sufficient.",
            },
            # Asked EXPLICITLY because the critic will otherwise mention a bad reference
            # in `issues` and score anyway: handed a render of a night city against a
            # green meadow, it wrote "cannot be the shot's look reference" and returned
            # camera_framing=4, PASS — silently grading the frame against the brief's
            # prose instead of an image. Ref-relative scoring is the whole premise, so
            # this has to be a first-class field, not a remark.
            "reference_usable": {
                "type": "boolean",
                "description": "false if the REFERENCE image is not a plausible target "
                "for this candidate at all (wrong shot, wrong beat, "
                "corrupt, blank). Absent-by-design content that a LATER "
                "layer adds does NOT make a reference unusable.",
            },
            "reference_note": {"type": "string", "description": "one line; required when unusable"},
        },
        "required": ["scores", "observations", "focus_requests", "reference_usable", "reference_note"],
        "additionalProperties": False,
    }


def _critic_options(
    shot: Shot,
    axes: list[tuple[str, str]] | None = None,
    *,
    allow_na: bool = True,
    focus_frames: list[int] | None = None,
) -> ClaudeAgentOptions:
    # NO TOOLS. The images arrive attached to the request (see _critique), so the critic
    # has nothing to fetch and cannot score a frame it never saw. This deleted three
    # layers of machinery that existed only to police the old tool loop: the sandbox
    # redirect for critic reads, the request/result id pairing, and the blind-critic guard.
    return ClaudeAgentOptions(
        model=critic_model(),
        system_prompt=CRITIC_SYSTEM,
        cwd=str(shot.folder),
        allowed_tools=[],
        disallowed_tools=[
            "Read",
            "Glob",
            "Write",
            "Edit",
            "Bash",
            "Grep",
            "WebFetch",
            "WebSearch",
            "Task",
            "Agent",
            "NotebookEdit",
        ],
        permission_mode="bypassPermissions",
        max_buffer_size=32 * 1024 * 1024,  # a multi-image request exceeds the 1MB default
        setting_sources=[],
        # Structured output can require a final protocol turn after the model has
        # finished reasoning. In the real Layer 1 run, both first attempts exhausted a
        # two-turn ceiling and both retries succeeded. Three turns are protocol headroom,
        # not an invitation to loop: the critic has no tools and only one user message.
        max_turns=3,
        # The judgement everything depends on — but "xhigh" here was an assertion, never a
        # measurement, and it is the single largest cost in the pipeline. Measured on layer
        # 1: the critic produced 5-12k output per session for $4.18-11.31, i.e. $0.63-1.28
        # per 1k output, against the builder's $0.07-0.09 — 9-18x more per token, while
        # reading HALF the cache. Extended thinking is billed as output and does not appear
        # in the output field, which is where the money goes. Configurable so the claim can
        # be tested with `evals variance` instead of argued about.
        effort=CRITIC_EFFORT,
        # Validated at the tool layer with automatic retries, instead of scraping the
        # last {...} out of free text — one critic already returned nothing parseable.
        output_format=(
            {
                "type": "json_schema",
                "schema": _critic_schema(axes, allow_na=allow_na, focus_frames=focus_frames),
            }
            if axes
            else None
        ),
    )


def _extract_json_list(text: str) -> list:
    """Pull the last JSON array out of a reply (fenced or bare)."""
    fenced = re.findall(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
    candidates = fenced or re.findall(r"(\[.*\])", text, re.DOTALL)
    for chunk in reversed(candidates):
        try:
            return json.loads(chunk)
        except json.JSONDecodeError:
            continue
    raise ValueError("no parseable JSON array")


async def ensure_axes(shot: Shot, verbose: bool = True) -> list[tuple[str, str]]:
    """The critic rubric for this shot.

    Written by the PLAN stage (critic_axes.json): the planner has the deepest scene read
    AND knows the layer breakdown, so it is the only stage that can guarantee every axis
    has an owning layer. Missing axes are a migration failure, not an invitation for the
    builder to invent a different rubric.
    """

    path = selected_artifact_path(shot.folder, "critic_axes.json")
    if path.is_file():
        return load_axes(shot)
    raise FileNotFoundError(
        f"{path} missing — legacy builder-side rubric derivation has been removed; "
        "generate and gate the strict global plan"
    )


def _warn_unowned_axes(shot: Shot, axes: list[tuple[str, str]]) -> None:
    """Audit the axis↔layer mapping in BOTH directions — each catches a real bug we hit.

    axis with no layer  → unearnable: nobody can ever score it.
    layer with no axis  → unjudgeable: the layer is scored purely on OTHER layers' work,
                         so its own contribution is invisible and its revisions polish
                         someone else's layer (SH G60 "ENVIRONMENT" scored only on
                         typography and palette until `environment_depth` was added).

    An axis owned only by the LAST layer is fine and deliberately NOT flagged: with
    `owns` in force the earlier layers simply aren't judged on it, which is the point.
    """
    layers = load_layers(shot)
    if not any(g.owns for g in layers.values()):
        raise ValueError("strict layers.json contract requires owned axes; legacy unscoped judging is not supported")
    keys = {k for k, _ in axes}
    orphan = sorted(keys - {a for g in layers.values() for a in g.owns})
    if orphan:
        log(f"! axes owned by NO layer — unearnable: {orphan}")
    mute = sorted(g.id for g in layers.values() if not g.owns)
    if mute:
        log(f"! layers owning NO axis — judged only on other layers' work: {mute}")
    unknown = sorted({a for g in layers.values() for a in g.owns} - keys)
    if unknown:
        log(f"! layers claim axes not in the rubric: {unknown}")


def _owned_axes(axes: list[tuple[str, str]], layer) -> list[tuple[str, str]]:
    """Deterministically scope a layer before prompting or schema construction."""
    owned = set(getattr(layer, "owns", ()) or ())
    return [row for row in axes if row[0] in owned]


_MOTION_AXIS_WORDS = (
    "motion",
    "animation",
    "continuity",
    "timing",
    "trajectory",
    "interpolation",
    "velocity",
    "monotonic",
    "easing",
)


def _axes_need_motion(axes: list[tuple[str, str]]) -> bool:
    """Legacy-free helper for unlayered acceptance/eval calls only.

    Staged layers use their explicit ``temporal_evidence`` policy.  This heuristic remains
    for acceptance milestones, which do not have work-unit manifests.
    """
    text = " ".join(f"{key} {description}" for key, description in axes).lower()
    return any(word in text for word in _MOTION_AXIS_WORDS)


def _layer_needs_motion(layer) -> bool:
    return getattr(layer, "temporal_evidence", None) == "motion"


def _evidence_convergence_stop(layer, verdict: dict) -> bool:
    """Stop revisions when executable owned evidence has nothing left to repair.

    This does not manufacture a PASS: canonical verification may still record a judge
    conflict.  It prevents an evidence-free low score from sending the builder into more
    geometry edits after every authoritative contract is green.
    """
    if layer is None or verdict.get("pass"):
        return False
    authoritative = [row for row in (verdict.get("evidence") or []) if row.get("authoritative")]
    layer_id = str(getattr(layer, "id", ""))
    owned = [
        row for row in authoritative if str(row.get("fault_owner") or row.get("owner_layer") or layer_id) == layer_id
    ]
    return bool(
        owned
        and all(row.get("pass") for row in authoritative)
        and not verdict.get("issues")
        and not verdict.get("reference_unusable")
    )


def _builder_ticket_context(plan_excerpt: str, scope: str | None, axes: list[tuple[str, str]], layer=None) -> str:
    """Local retrieval query for prompt modules; never sent as extra prompt text.

    The full excerpt contains gotchas and regression notes about other departments. Feeding
    all of that to retrieval makes a finish ticket look like camera+city+asset work again.
    Headings, scope/done clauses, approach lines and explicit recipe calls express what this
    layer can actually change; the builder still receives the complete excerpt at kickoff.
    """
    ticket_lines = []
    for line in plan_excerpt.splitlines():
        stripped = line.strip()
        if (
            stripped.startswith(("### ", "**Scope", "**Done", "**G"))
            or "build/approach:" in stripped
            or "find_recipe(" in stripped
        ):
            ticket_lines.append(stripped)
    layer_bits = [str(getattr(layer, key, "") or "") for key in ("title", "reads", "script")]
    axis_bits = [f"{name}: {description}" for name, description in axes]
    return "\n".join(x for x in ("\n".join(ticket_lines), scope or "", *layer_bits, *axis_bits) if x)


async def distill_recipe(
    shot: Shot, m: Milestone, verbose: bool = True, script_rel: str | None = None, errors: list[str] | None = None
) -> None:
    """Harvest reusable recipes into the cookbook (best-effort).

    Two sources, either of which is enough to be worth a pass: a build script that
    PASSED (proven technique) and API errors the builder hit and worked around (proven
    gotcha). The second used to be discarded entirely.
    """
    script_path = shot.folder / (script_rel or f"build/{m.id.lower()}.py")
    if not script_path.is_file() and not errors:
        return
    what = []
    if script_path.is_file():
        what.append("the passing build")
    if errors:
        what.append(f"{len(errors)} self-corrected error(s)")
    log(f"distilling reusable recipes from {' + '.join(what)}…")
    repo = PROJECT_ROOT
    options = ClaudeAgentOptions(
        model=distiller_model(),
        system_prompt=DISTILL_SYSTEM,
        cwd=str(repo),
        hooks=distiller_hooks(RECIPES_DIR, shot.folder, cwd=repo),
        allowed_tools=["Read", "Write", "Glob"],
        # Grep is why the distiller walked out to ~/.claude and read this session's
        # transcript looking for context on an error message.
        disallowed_tools=["Bash", "Grep", "WebFetch", "WebSearch", "Task", "Agent"],
        permission_mode="bypassPermissions",
        max_buffer_size=32 * 1024 * 1024,
        setting_sources=[],
        max_turns=16,
        effort="medium",
    )
    parts = [
        f"Layer {m.id} of shot '{shot.id}' just finished. Existing recipes are in "
        f"`{RECIPES_DIR}` — improve one rather than duplicating it."
    ]
    if script_path.is_file():
        parts.append(f"It PASSED: read its build script `{script_path}` and harvest 0-2 general, reusable techniques.")
    if errors:
        joined = "\n".join(f"  - {e}" for e in errors[:8])
        parts.append(
            f"It also hit these API errors and worked around them:\n{joined}\n"
            f"Each cost the builder turns and will cost the next builder the same. For "
            f"any that is a GENERAL Blender-5 gotcha (not a shot-specific typo), record "
            f"the correct usage. VERIFY the correct form against the recipes or the "
            f"script before writing it — a confidently wrong recipe is worse than none, "
            f"so if you cannot confirm the fix, write nothing for that error."
        )
    prompt = " ".join(parts)

    async def _run_distiller():
        async for message in query(prompt=prompt, options=options):
            if verbose:
                log_message(message)
            elif isinstance(message, builder_package().ResultMessage):
                costlog.record(message)

    if costlog.is_bound():
        with costlog.scoped(role="distiller", phase="distill"):
            await _run_distiller()
    else:
        costlog.bind(shot.folder, role="distiller", phase="distill", layer=m.id, run_id=RUN_ID)
        try:
            await _run_distiller()
        finally:
            costlog.unbind()
