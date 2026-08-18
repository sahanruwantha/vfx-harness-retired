"""Stage 3 — the build + critic loop for one PLAN LAYER.

A BUILD agent drives a warm Blender session (run_bpy / render_*) to implement one
layer from plan.md (its tickets are the spec), iterating against a reference-scored
CRITIC until the layer's judge frame clears the bar. On pass it persists the layer's
deterministic delta script (build/NN_<layer>.py); the harness re-runs the whole chain
from an empty scene to confirm it reproduces — the scripts, not the live scene, are
the artifact of record.

Usage:
    python -m pipeline.build_agent <shot-folder> --layer <id> [--rounds 2]
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import shutil
import time
from pathlib import Path

import anyio
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    query,
)

from .blender.session import BlenderError, BlenderSession
from .blender.tools import build_blender_tools
from .brief import Shot, load_shot
from .layer_state import record_round as state_round, start as state_start
from .log import (
    TOOL_USE, _result_text, log, log_message, reset_tool_use, tool_use_summary,
)
from .preflight import empty_success, warn_if_broken
from .build_prompts import (
    CRITIC_SYSTEM,
    builder_kickoff,
    recurring_complaints,
    builder_system,
    canonical_repair_prompt,
    critic_prompt,
    finalize_prompt,
    revision_prompt,
)
from .ledger import Ledger, Milestone, load_axes, load_layers, plan_strips
from .recipes import RECIPES_DIR, build_recipe_tools, log_recipe_use, recipe_index
from .approach import review as approach_review, revision_from_review
from .escalate import load as load_questions
from .guardrails import builder_hooks, distiller_hooks
from .sandbox import sandbox_hooks
from .provenance import check as provenance_check
from .runid import RUN_ID
from .runlog import bump, reset_counts, summary as run_summary, write as write_run
from .shot_context import clear_layer_context, write_layer_context
from . import transcript

MODEL = "claude-opus-5"
# The critic scores renders and runs 3-4x per layer to the builder's one session, so it
# dominates layer cost. It was fable-5 on that reasoning; it is opus-5 now because the
# verdict is the pipeline's only measure of quality and a cheaper judge is a false economy
# when every downstream decision rests on it.
#
# NOTE the calibration below: _JUDGE_SD = 0.603 and the adjudication band derived from it
# were MEASURED ON FABLE-5. They are the wrong constants for this judge until re-measured
# (`python -m pipeline.evals variance <shot>`). Until then the panel is being convened on
# a noise estimate that belongs to a different model.
CRITIC_MODEL = "claude-opus-5"

AXES_SYSTEM = """\
You define the CRITIC RUBRIC for one VFX shot. Read brief.md and the reference images,
then output the 5-7 look axes a VFX supervisor would score a render on against THESE
references — the dimensions this specific shot's look lives or dies by (e.g. composition,
atmosphere, the hero subject's detail, environment, palette, finish/grade). Make them
specific to this shot's content and style, not generic. Return ONLY a JSON array of
{"key": "snake_case", "desc": "one concrete line"} and nothing else.
"""

DISTILL_SYSTEM = """\
You harvest REUSABLE Blender recipes from a build that just passed its critic. Read the
build script. Identify 0-2 GENERAL techniques worth reusing on other shots (volumetrics,
materials, compositor/grade, instancing) — NOT shot-specific values or trivia. For each,
Write pipeline/recipes/<slug>.md with frontmatter (name, tags, blender: "5.2+", when,
verified: false), a short GOTCHAS note, and a parameterized code snippet. If a similar
recipe already exists, improve it instead of duplicating. If nothing is general enough,
write nothing and say so.

ALWAYS write `verified: false`. You are not able to verify anything — you are reading a
script, not running one. `verified: true` is set ONLY by pipeline.verify_recipes, and only
after the snippet has been EXECUTED in a headless Blender and the top-level callables it
defines actually INVOKED, with what changed and a sha256 of the exact code that ran
recorded as evidence in pipeline/recipes/_verified.json. `--audit` fails on any recipe
claiming verification without a matching spike entry, and the test suite asserts it — so
declaring it yourself does not merely lie to every future build, it breaks the build.

Verification is earned, by running:

    python -m pipeline.verify_recipes --name <slug> --sync

which promotes the flag only if the spike actually passes.

Write your snippet so it CAN be verified: put the technique in a top-level function with
plain, defaulted arguments. Code that only runs inside a larger shot-specific block cannot
be proved to work — one recipe's shader function was never called, so verification proved
only that an unrelated loop beneath it ran, and a Blender-4 call inside that function
would have gone undetected.

If the snippet needs scaffolding before it can run — a named material or object it assumes
a real shot provides, a placeholder constant, or arguments the verifier cannot guess —
also Write pipeline/recipes/_spikes/<slug>.py supplying them. Read
pipeline/recipes/_spikes/README.md first; it documents the SPIKE_ARGS convention. Keep
that scaffolding OUT of the .md: find_recipe hands the recipe body to a builder verbatim,
and test scaffolding in there gets pasted straight into a shot.
"""

# Layer: the render passes when every axis clears PASS_MIN and the mean clears
# PASS_MEAN (both on the critic's 0–5 scale). Calibrated from data: across 4 builds the
# best/canonical band is 3.1-3.3, and coupled global axes REDISTRIBUTE score under
# revision — 3.3 sat inside that band and was missed by ≤0.16 four times straight.
#
# The "~±0.15 critic noise" this once claimed was WRONG, and wrong in the dangerous
# direction. Measured directly: the same render against the same reference on one axis
# scored 4.0, 3.0, 3.0, 2.0 across four repeats — a 2-point spread that flipped the
# verdict. On a one- or two-axis layer the mean IS that single number, so a lone verdict
# near the line is close to a coin flip. Hence _judge() below, which buys a second and
# third opinion exactly where the decision is uncertain.
PASS_MIN = 2
PASS_MEAN = 3.1

# Circuit breaker. Turns are a poor proxy for what we actually care about — a layer
# needing 200 cheap turns is fine, one burning $40 in 40 turns is not — so cap SPEND
# and leave turns as loose headroom. Observed: BR C $10.69 passing, BR G $15.95 while
# truncated at 120 turns. Both default to unlimited in the SDK.
MAX_BUDGET_USD = 25.0
MAX_TURNS = 400
MAX_CONTINUES = 3  # turn-cap nudges before we call the build truncated

_REPO = Path(__file__).resolve().parent.parent

_RESET = "import bpy\nbpy.ops.wm.read_factory_settings(use_empty=True)\n"

# Terminations that mean "the builder never finished", as opposed to "it finished badly".
_TRUNCATED = {"error_max_turns", "error_max_budget_usd"}


class BuildTruncated(RuntimeError):
    """The builder ran out of budget mid-build — no verdict is meaningful."""


class UnpassedPrior(RuntimeError):
    """A layer below this one was never accepted — building on it would compound it."""


def _prior_layer_paths(shot: Shot, layer, *, force: bool = False) -> list[Path]:
    """Layer scripts that must run before this layer: all EXISTING build/NN_*.py with a
    lower numeric prefix, in order. Each layer stacks on the ones before it.

    Raises UnpassedPrior unless every one of them is recorded 'passed'."""
    def num(p: Path) -> int:
        m = re.match(r"(\d+)", p.name)
        return int(m.group(1)) if m else 10_000
    mine = num(Path(layer.script))
    build_dir = shot.folder / "build"
    if not build_dir.is_dir():
        return []
    found = sorted((p for p in build_dir.glob("[0-9]*.py") if num(p) < mine), key=num)
    # The build dir is a glob, the ledger is the record of what was ACCEPTED. An
    # interrupted layer leaves a script that would silently join the chain (a killed
    # seam layer left an un-critiqued 40_seam.py queued for the two after it).
    try:
        ledger, layers = Ledger(shot), load_layers(shot)
    except Exception as e:
        log(f"! chaining WITHOUT the ledger cross-check: {str(e)[:70]}")
        return found
    by_script = {Path(g.script).name: g for g in layers.values()}
    keep, unpassed = [], []
    for p in found:
        g = by_script.get(p.name)
        if g is None:
            log(f"! {p.name} matches no layer in layers.json — SKIPPING (orphan)")
            continue
        st = ledger.status(g.as_milestone())
        if st != "passed":
            unpassed.append(f"layer {g.id} ({p.name}) is '{st}'")
        else:
            # 'passed' is a verdict on a SCRIPT, not on a layer id. Editing the script
            # afterwards leaves the pass in place describing code that no longer exists,
            # and every layer above then builds on renders of the old version. Treated
            # exactly like an unpassed prior, because that is what it is.
            why = ledger.stale(g.as_milestone())
            if why:
                unpassed.append(why)
        keep.append(p)
    # FAIL CLOSED. This used to warn and chain anyway, so a layer could be built on top of
    # a predecessor whose content was never accepted — every judgement above it then rests
    # on unreviewed geometry. The protection previously lived in the shell script that
    # drove a full run, which meant running a single layer by hand silently bypassed it.
    if unpassed and not force:
        raise UnpassedPrior(
            f"refusing to build layer {layer.id} on unaccepted work: "
            + "; ".join(unpassed)
            + ". Re-run those layers, or pass --force to chain anyway (debugging only).")
    if unpassed:
        log(f"! --force: chaining {len(unpassed)} unaccepted prior(s) — "
            + "; ".join(unpassed))
    return keep


class ChainBroken(RuntimeError):
    """A prior layer script no longer composes — the chain must be repaired first."""


def _run_prior_paths(session: BlenderSession, paths: list[Path]) -> list[str]:
    """Replay the accepted chain. A failure here is NOT this layer's fault: layer scripts
    reference each other's objects by name (30_purple.py does D.objects['tower_dot']
    from 20_green.py), so re-running an early layer can invalidate every later one and
    the break only surfaces now. Say so plainly instead of leaking a raw bpy KeyError."""
    names = []
    for p in paths:
        log(f"running prior layer script {p.name}")
        try:
            session.run(p.read_text(encoding="utf-8"))
        except BlenderError as e:
            first = str(e).strip().splitlines()[0]
            raise ChainBroken(
                f"{p.name} no longer composes onto the scene built by the layers before "
                f"it: {first}\n  The chain is broken, not this layer. Re-run {p.name}'s "
                f"layer (or restore the prior script it was authored against) before "
                f"building further.") from e
        names.append(p.name)
    return names


def _plan_layer_excerpt(shot: Shot, layer) -> str:
    """The layer's own section of plan.md — its tickets ARE the build instructions."""
    plan = shot.folder / "plan.md"
    if not plan.is_file():
        return ""
    text = plan.read_text(encoding="utf-8")
    script_name = Path(layer.script).name
    lines = text.splitlines()
    id_pat = re.compile(rf"(?:LAYER\s+{re.escape(layer.id)}\b|\b{re.escape(layer.id)}\s*·)")
    start = None
    for i, ln in enumerate(lines):
        if not ln.startswith("### "):
            continue
        # script name is unambiguous; a bare id needs word-ish boundaries
        if script_name in ln or id_pat.search(ln) or (layer.title and layer.title in ln):
            start = i
            break
    if start is None:
        return ""
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].startswith("### ") or lines[j].startswith("## "):
            end = j
            break
    return "\n".join(lines[start:end]).strip()


def _preamble(shot: Shot) -> str:
    """Deterministic scene setup the build script may assume is already applied."""
    return (
        "import bpy\n"
        "sc = bpy.context.scene\n"
        # resolve the EEVEE engine name for this build (5.2 = BLENDER_EEVEE, not …_NEXT)
        "_av = {e.identifier for e in bpy.types.RenderSettings.bl_rna.properties['engine'].enum_items}\n"
        "sc.render.engine = 'BLENDER_EEVEE_NEXT' if 'BLENDER_EEVEE_NEXT' in _av else 'BLENDER_EEVEE'\n"
        f"sc.render.fps = {shot.fps}\n"
        f"sc.render.resolution_x = {shot.resolution[0]}\n"
        f"sc.render.resolution_y = {shot.resolution[1]}\n"
        "sc.render.resolution_percentage = 100\n"
        "sc.frame_start = 1\n"
        f"sc.frame_end = {shot.frames}\n"
        "sc.render.use_motion_blur = True\n"
    )


# --------------------------------------------------------------------------- #
# Agent plumbing                                                               #
# --------------------------------------------------------------------------- #
def _builder_options(shot: Shot, mcp_servers: dict, tool_names: list[str],
                     axes: list[tuple[str, str]],
                     ref_rel: str | None = None) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        model=MODEL,
        system_prompt=builder_system(axes, recipe_index()),
        cwd=str(shot.folder),
        hooks=builder_hooks(shot.folder, [shot.folder, RECIPES_DIR], ref_rel=ref_rel),
        mcp_servers=mcp_servers,
        # Edit was never advertised, so the obvious way to change one value in a 23KB
        # script was to Write the whole thing again.
        allowed_tools=["Read", "Edit", "Write", "Glob", "Grep", "WebFetch", *tool_names],
        # allowed_tools is an AUTO-APPROVE list, not a whitelist: under bypassPermissions
        # every unlisted tool still runs. Deny explicitly or it is available.
        # WebFetch is allowed but hook-restricted to Blender docs (see guardrails):
        # with no lookup at all the builder re-guesses a failing API verbatim.
        disallowed_tools=["Bash", "Task", "Agent", "NotebookEdit", "KillShell",
                          "BashOutput"],
        permission_mode="bypassPermissions",
        max_buffer_size=32 * 1024 * 1024,  # renders/read-images can exceed the 1MB default
        # "project" loads shots/<id>/CLAUDE.md on EVERY request, so the layer contract
        # survives compaction — the kickoff message does not.
        setting_sources=["project"],
        max_turns=MAX_TURNS,      # headroom only — MAX_BUDGET_USD is the real stop
        max_budget_usd=MAX_BUDGET_USD,
        effort="high",
    )


def _axes_options(shot: Shot) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        model=MODEL, system_prompt=AXES_SYSTEM, cwd=str(shot.folder),
        hooks=sandbox_hooks(shot.folder, cwd=shot.folder),
        allowed_tools=["Read", "Glob"],
        disallowed_tools=["Write", "Edit", "Bash", "Grep", "WebFetch", "WebSearch",
                          "Task", "Agent", "NotebookEdit"],
        permission_mode="bypassPermissions", max_buffer_size=32 * 1024 * 1024,
        setting_sources=[], max_turns=6, effort="low",   # one cheap classification
    )


def _critic_schema(axes: list[tuple[str, str]]) -> dict:
    """Force the verdict shape instead of regex-scraping the last {...} out of prose.
    Each axis is a 0-5 integer OR the string "n/a" for out-of-scope/absent-by-design."""
    score = {"anyOf": [{"type": "integer", "minimum": 0, "maximum": 5},
                       {"type": "string", "enum": ["n/a"]}]}
    return {
        "type": "object",
        "properties": {
            "scores": {"type": "object",
                       "properties": {k: score for k, _ in axes},
                       "required": [k for k, _ in axes],
                       "additionalProperties": False},
            "issues": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
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
            "reference_note": {"type": "string",
                               "description": "one line; required when unusable"},
        },
        "required": ["scores", "issues", "reference_usable", "reference_note"],
        "additionalProperties": False,
    }


def _critic_options(shot: Shot, axes: list[tuple[str, str]] | None = None) -> ClaudeAgentOptions:
    # NO TOOLS. The images arrive attached to the request (see _critique), so the critic
    # has nothing to fetch and cannot score a frame it never saw. This deleted three
    # layers of machinery that existed only to police the old tool loop: the sandbox
    # redirect for critic reads, the request/result id pairing, and the blind-critic guard.
    return ClaudeAgentOptions(
        model=CRITIC_MODEL,
        system_prompt=CRITIC_SYSTEM,
        cwd=str(shot.folder),
        allowed_tools=[],
        disallowed_tools=["Read", "Glob", "Write", "Edit", "Bash", "Grep", "WebFetch",
                          "WebSearch", "Task", "Agent", "NotebookEdit"],
        permission_mode="bypassPermissions",
        max_buffer_size=32 * 1024 * 1024,  # a multi-image request exceeds the 1MB default
        setting_sources=[],
        max_turns=1,
        # the judgement everything depends on; xhigh where the model supports it
        # (degrades to high elsewhere)
        effort="xhigh",
        # Validated at the tool layer with automatic retries, instead of scraping the
        # last {...} out of free text — one critic already returned nothing parseable.
        output_format=({"type": "json_schema", "schema": _critic_schema(axes)}
                       if axes else None),
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
    has an owning layer. The derive-from-refs path below is a fallback for shots planned
    before that, and cannot produce `owns`.
    """
    path = shot.folder / "critic_axes.json"
    if path.is_file():
        return load_axes(shot)
    log("no critic_axes.json from the plan — falling back to deriving from brief + refs")
    ref_list = "\n".join(f"  - {p.name}" for p in shot.refs) or "  (none)"
    prompt = (f"Define the critic rubric for shot '{shot.id}'. Read `brief.md` and these "
              f"reference images, then output the JSON axes array:\n{ref_list}")
    text = ""
    async for message in query(prompt=prompt, options=_axes_options(shot)):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    text += block.text
    try:
        data = _extract_json_list(text)
        clean = [{"key": a["key"], "desc": a["desc"]} for a in data if a.get("key") and a.get("desc")]
        if clean:
            path.write_text(json.dumps(clean, indent=2) + "\n")
    except Exception as e:  # fall back to defaults
        log(f"axes derive failed ({str(e)[:80]}); using defaults")
    axes = load_axes(shot)
    log(f"critic axes: {', '.join(k for k, _ in axes)}")
    return axes


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
    try:
        layers = load_layers(shot)
    except FileNotFoundError:
        return
    if not any(g.owns for g in layers.values()):
        return  # plan predates ownership — the scope block still narrows the rubric
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


async def distill_recipe(shot: Shot, m: Milestone, verbose: bool = True,
                         script_rel: str | None = None,
                         errors: list[str] | None = None) -> None:
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
    repo = Path(__file__).resolve().parent.parent
    options = ClaudeAgentOptions(
        model=MODEL, system_prompt=DISTILL_SYSTEM, cwd=str(repo),
        hooks=distiller_hooks(RECIPES_DIR, shot.folder, cwd=repo),
        allowed_tools=["Read", "Write", "Glob"],
        # Grep is why the distiller walked out to ~/.claude and read this session's
        # transcript looking for context on an error message.
        disallowed_tools=["Bash", "Grep", "WebFetch", "WebSearch", "Task", "Agent"],
        permission_mode="bypassPermissions", max_buffer_size=32 * 1024 * 1024,
        setting_sources=[], max_turns=16, effort="medium",
    )
    parts = [f"Layer {m.id} of shot '{shot.id}' just finished. Existing recipes are in "
             f"`{RECIPES_DIR}` — improve one rather than duplicating it."]
    if script_path.is_file():
        parts.append(f"It PASSED: read its build script `{script_path}` and harvest 0-2 "
                     f"general, reusable techniques.")
    if errors:
        joined = "\n".join(f"  - {e}" for e in errors[:8])
        parts.append(
            f"It also hit these API errors and worked around them:\n{joined}\n"
            f"Each cost the builder turns and will cost the next builder the same. For "
            f"any that is a GENERAL Blender-5 gotcha (not a shot-specific typo), record "
            f"the correct usage. VERIFY the correct form against the recipes or the "
            f"script before writing it — a confidently wrong recipe is worse than none, "
            f"so if you cannot confirm the fix, write nothing for that error.")
    prompt = " ".join(parts)
    async for message in query(prompt=prompt, options=options):
        if verbose:
            log_message(message)


# API errors the builder hit and worked around. The distiller only ever harvested from
# PASSING builds, so a gotcha the builder fumbled and recovered from left no trace —
# four distinct Blender-5 errors self-corrected in one run and taught the cookbook
# nothing. These are the highest-value recipes precisely because they cost turns.
_ERRORS: list[str] = []
_RECIPES_USED: list[str] = []
_JOURNAL_INFO: dict = {}


# The builder's own statement of what it is going to do, captured once per layer. The
# journal records the run_bpy calls it made; nothing recorded the reasoning that produced
# them, so whether the recipe index actually changed what it reached for was unanswerable
# without reading raw SDK transcripts.
_APPROACH: dict = {}


def _collect_approach(message) -> None:
    if _APPROACH.get("text") or not isinstance(message, AssistantMessage):
        return
    for b in message.content:
        if not isinstance(b, TextBlock):
            continue
        for line in b.text.splitlines():
            if line.strip().upper().startswith("APPROACH:"):
                _APPROACH["text"] = line.split(":", 1)[1].strip()[:400]
                log(f"approach: {_APPROACH['text'][:120]}", 1)
                return


def _collect_errors(message) -> None:
    """Reuse log's extractor: a tool result's content may be a str, a list of dicts, or a
    list of BLOCK OBJECTS. My first version only handled the first two, so object-shaped
    results extracted to "" and were dropped — it collected 1 of 2 errors on BR layer S."""
    for b in getattr(message, "content", []) or []:
        if not getattr(b, "is_error", False):
            continue
        txt = _result_text(b)
        head = txt.strip().splitlines()[0][:200] if txt.strip() else ""
        if head and head not in _ERRORS:
            _ERRORS.append(head)


async def _drain_once(client: ClaudeSDKClient, verbose: bool) -> dict:
    """Consume one builder response; report HOW it ended.

    The SDK signals a truncated loop with subtype='error_max_turns' on the final
    ResultMessage. Discarding it (as this used to) means a build that was cut off
    mid-scene is indistinguishable from one that finished — BR layer G was critiqued,
    scored and recorded 'failed' while half-built. Always look at the subtype.
    """
    info = {"subtype": "unknown", "turns": 0, "cost": 0.0, "session_id": None,
            "tokens": {}}
    async for message in client.receive_response():
        if verbose:
            log_message(message)
        _collect_errors(message)
        _collect_approach(message)
        if isinstance(message, ResultMessage):
            # Token economics lived ONLY in the SDK's session JSONL, outside the repo, so
            # "which layer burned the budget, and was the stable prefix actually cached?"
            # could not be answered from anything the pipeline writes. A collapsing
            # cache-hit rate is the early warning that the cached prefix has been broken.
            u = getattr(message, "usage", None)
            info = {
                "session_id": getattr(message, "session_id", None),
                "subtype": getattr(message, "subtype", "unknown"),
                "turns": getattr(message, "num_turns", 0) or 0,
                # total_cost_usd is cumulative for the session and Optional on error paths
                "cost": getattr(message, "total_cost_usd", None) or 0.0,
                "tokens": {k: (u.get(k) or 0) for k in
                           ("input_tokens", "output_tokens",
                            "cache_read_input_tokens", "cache_creation_input_tokens")
                           } if isinstance(u, dict) else {},
            }
    return info


async def _drain(client: ClaudeSDKClient, verbose: bool, *,
                 continues: int = MAX_CONTINUES) -> dict:
    """Drain a builder response, nudging it onward if it hit the turn cap.

    A streaming-input session SURVIVES error_max_turns — the builder is still there
    with its full context, and each new message starts a fresh turn budget. So an
    exhausted cap is a pause, not a death: tell it how much it has spent and let it
    finish. (Single-shot query() raises instead; that is why this only works here.)
    """
    info = await _drain_once(client, verbose)
    for i in range(continues):
        if info["subtype"] != "error_max_turns":
            break
        log(f"⏸ builder hit the turn cap ({info['turns']} turns, ${info['cost']:.2f}) — "
            f"continuing {i + 1}/{continues}")
        await client.query(
            f"You have hit a turn checkpoint: {info['turns']} turns and "
            f"${info['cost']:.2f} spent on this layer so far, out of a ${MAX_BUDGET_USD:.0f} "
            f"budget. You have NOT been reset — the scene and your context are intact. "
            f"Continue from exactly where you stopped, but start converging: finish the "
            f"work in progress and prefer landing the layer over further refinement.")
        info = await _drain_once(client, verbose)
    if info["subtype"] == "error_max_turns":
        log(f"! builder still truncated after {continues} continuations "
            f"({info['turns']} turns, ${info['cost']:.2f})")
    return info


def _extract_json(text: str) -> dict:
    """Pull the last JSON object out of the critic's reply (fenced or bare)."""
    fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidates = fenced or re.findall(r"(\{.*\})", text, re.DOTALL)
    for chunk in reversed(candidates):
        try:
            return json.loads(chunk)
        except json.JSONDecodeError:
            continue
    raise ValueError("critic returned no parseable JSON scorecard")


def _verdict(verdict: dict) -> dict:
    """Compute pass/mean from the critic's IN-SCOPE axis scores. A scaffolding stage
    marks axes a later stage delivers as "n/a" — absent-by-design must not drag the
    mean (judging layer L on emission scored it 1.25 while its own axis scored 4)."""
    raw = verdict.get("scores", {})
    scores, na = {}, []
    for k, v in raw.items():
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            scores[k] = float(v)
        else:  # "n/a", "N/A", null … — out of this stage's scope
            na.append(k)
    n = len(scores)
    mean = round(sum(scores.values()) / n, 2) if n else 0.0
    verdict["mean"] = mean
    verdict["scored_axes"] = sorted(scores)
    verdict["na_axes"] = sorted(na)
    # A verdict measured against the wrong plate is not a verdict. The critic reports
    # this itself; before it was asked directly it would note the mismatch in `issues`
    # and pass regardless. No score can rescue this — the plan's ref path is wrong.
    if verdict.get("reference_usable") is False:
        verdict["pass"] = False
        verdict["reference_unusable"] = True
        return verdict
    # Thresholds must be GRANULARITY-AWARE. mean = sum/n, so one axis point of judge
    # noise moves the mean by 1/n: 0.125 across 8 axes but 1.0 across one. A scoped
    # layer with 1-2 in-scope axes must not face a harsher bar than a full acceptance
    # layer (PASS_MEAN 3.1 on a single axis silently demands a 4).
    if n and n <= 2:
        verdict["pass"] = min(scores.values()) >= 3
    else:
        verdict["pass"] = bool(scores) and mean >= PASS_MEAN and min(scores.values()) >= PASS_MIN
    return verdict


# A canonical render may sit slightly under the live best and still be the same picture —
# judge noise is real. But 1/n was derived from "one axis point moves the mean by 1/n",
# which at n=1 licenses a FULL POINT: a 4 becoming a 3 counted as "reproduced". That is a
# different verdict, not noise, and on a one-axis layer it is the entire verdict.
_REPRO_TOL_MAX = 0.5


def _repro_tolerance(n_scored: int) -> float:
    """How far a canonical re-render may fall below the live best and still count as
    'the script reproduces it'. Widens as the axis count shrinks (one axis point moves
    the mean by 1/n), but never far enough to absorb a whole grade."""
    return min(_REPRO_TOL_MAX, max(0.3, 1.0 / n_scored)) if n_scored else 0.3


def _repair_delta(pre: list, post: list) -> dict:
    """What a canonical repair round actually achieved, per judged frame.

    Two questions, and the loop used to get the second one wrong.

    `broke` — frames that PASSED before the repair and do not now. A repair that trades
    a passing frame for a failing one is not a fix, and the caller reverts on it.

    `progressed` — whether the round moved toward a pass at all. This used to compare the
    SUM of the failing frames' means, which is the wrong statistic for a conjunctive
    requirement: the layer passes only when EVERY judged frame clears the bar, so the
    binding constraint is the worst frame, while a sum rises whenever any one frame does.
    A four-frame layer going 1.0→2.0 on one frame and holding the rest scored as
    improvement and bought another round that could not lead to a pass. Layer 2 spent five
    attempts in that state — the sum crept up while the minimum sat still. Progress means
    the worst failing frame improved, or there are fewer failing frames than before.
    """
    was = {f: v.get("mean") for (f, _r), v in pre}
    now = {f: v.get("mean") for (f, _r), v in post}
    was_pass = {f for (f, _r), v in pre if v.get("pass")}
    failed = [f for (f, _r), v in pre if not v.get("pass")]
    still = [f for (f, _r), v in post if not v.get("pass")]
    # Only frames judged BOTH times can be compared. A frame missing from `post` has no
    # score to improve on, and treating its absence as 0 would read as a regression.
    common = [f for f in failed if f in now]
    was_worst = min((was.get(f) or 0) for f in common) if common else None
    now_worst = min((now.get(f) or 0) for f in common) if common else None
    progressed = (
        (was_worst is not None and now_worst > was_worst)
        or len(still) < len(failed)
    )
    return {
        "was": was, "now": now,
        "broke": sorted(f for (f, _r), v in post if f in was_pass and not v.get("pass")),
        "was_worst": was_worst, "now_worst": now_worst,
        "was_failing": len(failed), "now_failing": len(still),
        "progressed": progressed,
    }


# Claude's long-edge sweet spot. Beyond this an image costs tokens without adding
# discriminable detail, and the critic scores several images per call.
_CRITIC_MAX_PX = 1568


def _image_block(path: Path, max_px: int = _CRITIC_MAX_PX) -> dict:
    """A base64 JPEG content block, downscaled to the useful maximum.

    Verified against the live API before adopting: three images attached with positional
    labels came back mapped correctly (reference/candidate/motion strip), and a control
    request with no image attached correctly reported that it had none.
    """
    from PIL import Image
    im = Image.open(path).convert("RGB")
    if max(im.size) > max_px:
        im.thumbnail((max_px, max_px), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=90)
    return {"type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg",
                       "data": base64.b64encode(buf.getvalue()).decode()}}


async def _one_user_message(blocks: list[dict]):
    """Stream exactly one multimodal user message; the SDK ends input when we return."""
    yield {"type": "user", "session_id": "",
           "message": {"role": "user", "content": blocks},
           "parent_tool_use_id": None}


def _structured_or_text(message, acc: dict) -> None:
    """Collect the verdict however the SDK delivers it.

    With output_format=json_schema the answer arrives as a StructuredOutput TOOL USE
    block, NOT as text — a request that scored three images perfectly returned no
    TextBlock at all. Reading only text made the verdict depend on the model ALSO
    volunteering prose JSON, which it is under no obligation to do.
    """
    if not isinstance(message, AssistantMessage):
        return
    for block in message.content:
        if isinstance(block, ToolUseBlock) and block.name == "StructuredOutput":
            if isinstance(block.input, dict):
                acc["structured"] = block.input
        elif isinstance(block, TextBlock):
            acc["text"] = acc.get("text", "") + block.text


async def _critique(shot: Shot, m: Milestone, candidate_rel: str,
                    axes: list[tuple[str, str]], session: BlenderSession, verbose: bool,
                    scope: str | None = None, prior_rel: str | None = None,
                    prior_mean: float | None = None) -> dict:
    motion_rel, motion_frames = None, None
    if shot.frontmatter.get("type") == "motion" and shot.frames > 1:
        try:  # a motion strip so motion/finish axes are judged across frames, not a still
            motion_rel, motion_frames = _stash_motion_strip(session, shot, m, candidate_rel.split("/")[-1].split(".")[0])
        except Exception as e:
            log(f"motion strip skipped: {str(e)[:80]}", 1)
    log(f"critic[{CRITIC_MODEL}]: scoring {candidate_rel} vs {m.ref}"
        + (f" (+motion {motion_frames})" if motion_rel else ""), 1)
    prompt = critic_prompt(shot, m, candidate_rel, axes, motion_rel, motion_frames, scope)

    # ATTACH the images instead of asking an agent to fetch them. A missing file is now a
    # loud failure here rather than a confident score on a frame that was never seen.
    ref_abs, cand_abs = shot.folder / m.ref, shot.folder / candidate_rel
    for p, what in ((ref_abs, "reference"), (cand_abs, "candidate render")):
        if not p.is_file():
            raise BlenderError(f"critic cannot score {m.id}: {what} missing at {p}")
    blocks = [{"type": "text", "text": prompt},
              {"type": "text", "text": "FIRST — the REFERENCE:"},
              _image_block(ref_abs),
              {"type": "text", "text": "SECOND — the CANDIDATE render:"},
              _image_block(cand_abs)]
    if motion_rel and (shot.folder / motion_rel).is_file():
        blocks += [{"type": "text",
                    "text": f"THIRD — the MOTION STRIP, frames {motion_frames}:"},
                   _image_block(shot.folder / motion_rel)]
    # The previous best, so the critic can judge DIRECTION of travel and not only
    # absolute state. Explicitly framed as context: it must score the candidate.
    if prior_rel and (shot.folder / prior_rel).is_file():
        blocks += [{"type": "text",
                    "text": f"CONTEXT ONLY — the best PREVIOUS attempt at this frame, "
                            f"which scored {prior_mean}. Do NOT score this image. Use it "
                            f"to say whether the candidate improved or regressed, and "
                            f"note in `issues` anything the previous attempt got right "
                            f"that the candidate has lost:"},
                   _image_block(shot.folder / prior_rel)]

    # Still retried: the critic is a transient-failure choke point — an SDK hiccup here
    # once killed a layer AFTER it had passed at 4.0 and written its script. Scoring is
    # idempotent. What is gone is retrying because the critic never opened its images.
    acc: dict = {}
    for attempt in range(1, 4):
        acc = {}
        try:
            async for message in query(prompt=_one_user_message(blocks),
                                       options=_critic_options(shot, axes)):
                _structured_or_text(message, acc)
            if acc.get("structured") or acc.get("text", "").strip():
                break
            log(f"critic returned nothing (attempt {attempt}/3) — retrying", 1)
        except Exception as e:
            if attempt == 3:
                raise
            log(f"critic error (attempt {attempt}/3): {str(e)[:90]} — retrying", 1)
    if not (acc.get("structured") or acc.get("text", "").strip()):
        raise BlenderError(f"critic returned no verdict for {m.id} after 3 attempts")
    verdict = _verdict(acc.get("structured") or _extract_json(acc["text"]))
    if verdict.get("reference_unusable"):
        log(f"✗ critic says the REFERENCE is unusable for {m.id}: "
            f"{verdict.get('reference_note', '(no note)')} — fix {m.ref} in the plan; "
            f"no score is meaningful against it", 1)
    scores = ", ".join(f"{k}={v}" for k, v in verdict.get("scores", {}).items())
    na = verdict.get("na_axes") or []
    log(f"critic: {scores} | mean {verdict['mean']} (over {len(verdict.get('scored_axes', []))} "
        f"in-scope axes{f'; n/a: {len(na)}' if na else ''}) | "
        f"{'PASS ✅' if verdict['pass'] else 'REVISE ✎'}", 1)
    for issue in verdict.get("issues", [])[:6]:
        log(f"· fix: {issue}", 2)
    # The judge's answer is what every control decision downstream hangs on, and it was
    # the one output with no durable home: `_critique` drains its own stream and never
    # calls log_message, so nothing but this console line recorded WHICH image scored
    # what against which reference. Recorded with the inputs beside it, because a score
    # without its render/reference pair cannot be re-checked.
    transcript.event("critic",
                     milestone=m.id, frame=m.frame,
                     candidate=candidate_rel, ref=m.ref,
                     model=CRITIC_MODEL,
                     mean=verdict.get("mean"),
                     verdict="pass" if verdict.get("pass") else "revise",
                     scores=verdict.get("scores", {}),
                     scored_axes=verdict.get("scored_axes", []),
                     na_axes=na,
                     borderline=_borderline(verdict),
                     reference_unusable=bool(verdict.get("reference_unusable")),
                     issues=verdict.get("issues", []),
                     scope="layer" if scope else "full-rubric",
                     motion_strip=motion_rel)
    return verdict


# How close to the pass line counts as "noise could flip this".
#
# MEASURED (N=12, same render vs same reference, layer-1 scope, one axis):
#   scores [2,4,3,2,3,3,3,3,3,3,4,3] → median 3.0, spread 2.0, sd 0.603,
#   and 2 of 12 draws flipped the verdict — a 17% flip rate on an unchanged image.
#
# A FIXED band was the wrong shape. Noise in the MEAN falls as 1/sqrt(n), so one constant
# is simultaneously too narrow on a 3-axis layer and wasteful on an 8-axis one — the same
# mistake _repro_tolerance and the granularity-aware PASS_MEAN already corrected. Two
# sigma of the mean at this sd: n=3 → 0.70, n=4 → 0.60, n=6 → 0.49, n=8 → 0.43. The old
# flat 0.4 was only defensible at n≈8, and most layers here are narrower than that.
#
# STALE AS OF THE SWITCH TO OPUS-5. Every number above was measured on FABLE-5. Judge
# noise is a property of the judge, so both the sd and the flip rate belong to a model
# that is no longer scoring anything here. The band may now be too wide (paying for
# panels that were never in doubt) or too narrow (passing verdicts that a second opinion
# would have flipped) — and which of those it is, is not currently known.
# Re-measure before trusting the adjudication economics: python -m pipeline.evals variance <shot>
_JUDGE_SD = 0.603     # fable-5 measurement; see above

# How many times a canonical failure may be handed back before we stop paying for it.
# Two, plus a no-improvement break: a repair that moved nothing will not move anything
# next time, and the money is better spent on a human reading the critique.
MAX_CANON_REPAIRS = 2


def _adjudicate_band(n_scored: int) -> float:
    """2σ of the mean for this many axes, clamped to a sane range."""
    if n_scored <= 0:
        return 0.4
    return min(0.8, max(0.4, 2 * _JUDGE_SD / (n_scored ** 0.5)))


def _borderline(verdict: dict) -> bool:
    """Could judge noise flip this verdict?"""
    scores = [v for v in verdict.get("scores", {}).values()
              if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if not scores:
        return False
    if len(scores) <= 2:
        # With one or two axes the mean IS a single integer score and the measured spread
        # is a full 2 points, so every value near the line is a coin flip — even a lone 4
        # carried a 7.4% chance a panel would have said REVISE. Two extra critic calls
        # (~$0.06) against a ~$6 layer that seven more layers get stacked on: always
        # convene the panel here rather than guess which single scores are safe.
        return True
    return (abs(verdict.get("mean", 0.0) - PASS_MEAN) <= _adjudicate_band(len(scores))
            or min(scores) == PASS_MIN)


async def _judge(shot: Shot, m: Milestone, candidate_rel: str,
                 axes: list[tuple[str, str]], session: BlenderSession, verbose: bool,
                 scope: str | None = None, **kw) -> dict:
    """Score the frame, buying extra opinions ONLY where the decision is uncertain.

    One critic call decided every layer until now. That is fine when a verdict is far
    from the line and indefensible when it is near it: the identical render/reference
    pair scored 4.0, 3.0, 3.0, 2.0 on repeats, so a single 3 was deciding whether the
    whole chain proceeded. Here a borderline verdict goes to best-of-three on the
    pass/fail question, which is where the noise actually hurts.
    """
    first = await _critique(shot, m, candidate_rel, axes, session, verbose, scope, **kw)
    if first.get("reference_unusable") or not _borderline(first):
        return first
    log(f"borderline verdict (mean {first['mean']}, "
        f"{len(first.get('scored_axes', []))} axis/axes) — seeking a second opinion", 1)
    panel = [first]
    for extra in range(2, 4):
        v = await _critique(shot, m, candidate_rel, axes, session, verbose, scope, **kw)
        panel.append(v)
        votes = [p["pass"] for p in panel]
        if len(panel) == 2 and votes[0] == votes[1]:
            break                          # unanimous; a third cannot change it
        if len(panel) == 3:
            break
    votes = [p["pass"] for p in panel]
    means = sorted(p["mean"] for p in panel)
    agreed = sum(votes) > len(votes) / 2
    out = dict(panel[0])
    out["pass"] = agreed
    out["mean"] = means[len(means) // 2]    # median resists the outlier
    out["panel"] = [{"mean": p["mean"], "pass": p["pass"]} for p in panel]
    # Take the issues from a judge that agrees with the panel, so the builder is not
    # handed fixes derived from the verdict that lost the vote.
    for p in panel:
        if p["pass"] == agreed:
            out["issues"], out["scores"] = p.get("issues", []), p.get("scores", {})
            break
    log(f"panel of {len(panel)}: means {[p['mean'] for p in panel]} · "
        f"votes {['PASS' if v else 'REVISE' for v in votes]} → "
        f"{'PASS ✅' if agreed else 'REVISE ✎'} (median {out['mean']})", 1)
    return out


def _stash_render(session: BlenderSession, shot: Shot, m: Milestone, tag: str,
                  scale: float = 0.5) -> str:
    """Render the judge frame (eevee) and copy it into the shot for the critic.
    Returns the path relative to the shot folder."""
    src = session.render(frame=m.frame, mode="eevee", scale=scale)
    dest_dir = shot.folder / "renders"
    dest_dir.mkdir(exist_ok=True)
    dest = dest_dir / f"{m.id}_{tag}.png"
    shutil.copyfile(src, dest)
    return f"renders/{dest.name}"


def _stash_motion_strip(session: BlenderSession, shot: Shot, m: Milestone, tag: str,
                        span: int = 6, scale: float = 0.4):
    """Montage a few frames around the judge frame so the critic can judge MOTION — a
    single still can't show blur or continuity. Returns (rel_path, frames).

    Prefers the PLAN's strip for this moment (acceptance.json), which the planner chose
    to cover the beat with a stated max gap. The old default only stepped FORWARD from
    the judge frame, so a botched approach was structurally invisible: barrel_roll's M2
    was judged at f20 (correctly near-black, scored 4.0) while f16-f18 sat at mean ~57
    with the world-swap in full view — and the strip [12,16,18,20,22] that would have
    caught it was parsed, stored, and never used.
    """
    from PIL import Image
    if m.strip:
        frames = sorted({f for f in m.strip if 1 <= f <= shot.frames})
    else:
        frames = sorted({m.frame, min(shot.frames, m.frame + span),
                         min(shot.frames, m.frame + 2 * span)})
    MAX = 6                  # keep the montage readable and the render cheap
    if len(frames) > MAX:
        # Thin the middle, but the JUDGE FRAME is never droppable — it is the frame the
        # verdict is about. (A naive sorted(...)[:MAX] silently cut f72 off SH's M1.)
        others = [f for f in frames if f != m.frame]
        step = max(1, round(len(others) / (MAX - 1)))
        thinned = others[::step][: MAX - 1]
        if others and others[-1] not in thinned:   # always keep the far end of the beat
            thinned = thinned[: MAX - 2] + [others[-1]]
        frames = sorted({*thinned, m.frame})
    ims = [Image.open(session.render(frame=f, mode="eevee", scale=scale)).convert("RGB") for f in frames]
    h = min(im.height for im in ims)
    ims = [im.resize((max(1, round(im.width * h / im.height)), h)) for im in ims]
    sheet = Image.new("RGB", (sum(im.width for im in ims), h), (10, 10, 12))
    x = 0
    for im in ims:
        sheet.paste(im, (x, 0)); x += im.width
    dest = shot.folder / "renders" / f"{m.id}_{tag}_motion.png"
    dest.parent.mkdir(exist_ok=True)
    sheet.save(dest)
    return f"renders/{dest.name}", frames


# --------------------------------------------------------------------------- #
# The loop                                                                     #
# --------------------------------------------------------------------------- #
async def build_unit(shot: Shot, m: Milestone, script_rel: str, prior_paths: list[Path],
                     session: BlenderSession, *, rounds: int = 2, verbose: bool = True,
                     plan_excerpt: str = "", scope: str | None = None,
                     layer=None, resume_ok: bool = False) -> Ledger:
    """The build+critic engine for ONE unit of work. Iteration is judged at m.frame vs
    m.ref; the canonical check covers every frame `layer` claims (see _verify_script)."""
    ledger = Ledger(shot)
    ledger.begin(m)

    axes = await ensure_axes(shot, verbose)  # per-shot critic rubric (from the plan)
    _warn_unowned_axes(shot, axes)
    bserver, bnames = build_blender_tools(session, assets_dir=shot.folder / "assets",
                                          shot_dir=shot.folder, layer_id=getattr(layer, 'id', m.id))
    rserver, rnames = build_recipe_tools(
        on_use=lambda names: (log_recipe_use(shot.folder, names),
                              _RECIPES_USED.extend(names)))
    mcp_servers = {"blender": bserver, "recipes": rserver}
    tool_names = bnames + rnames

    # Deterministic base + prior delta scripts (a unit extends the existing scene).
    session.run(_RESET)
    session.run(_preamble(shot))
    resume = ledger.get_resume(m) if resume_ok else None
    if resume:
        log(f"↻ resuming layer {m.id} from round {resume['round']}: restoring scene "
            f"+ replaying journal[{resume['journal_index']}:]")
        session.restore(resume["blend"])
        try:
            n = session.replay(resume["journal_index"]).get("replayed", 0)
            log(f"  replayed {n} journalled call(s) — scene matches the session", 1)
        except BlenderError as e:
            log(f"  ! replay failed ({str(e)[:60]}) — continuing from the checkpoint", 1)
        priors = [Path(p).name for p in prior_paths]
    else:
        priors = _run_prior_paths(session, prior_paths)

    t_layer = time.monotonic()
    # Per-layer, not per-process: the counts are attributed to one layer's report.
    reset_tool_use()
    reset_counts()
    # The durable record of this layer's conversation. Bound BEFORE the kickoff so the
    # very first thing in the file is the prompt the builder was given — a transcript of
    # answers to an unrecorded question cannot be audited, and the contract is exactly
    # what changes between the runs we want to compare.
    _tpath = transcript.bind(shot.folder, "build", label=f"layer{getattr(layer, 'id', m.id)}")
    if _tpath:
        log(f"transcript → {_tpath.relative_to(shot.folder)}", 1)
    # Conclusions that outlive the transcript: a compaction or a crash-resume costs the
    # conversation, not the measured state of each judge frame or what has been ruled out.
    state_start(shot.folder, getattr(layer, "id", m.id),
                list(getattr(layer, "judges", None) or [(m.frame, m.ref)]))
    canon_verdicts: list = []
    passed = False
    reviewed = False          # one approach review per layer; a second plateau stops
    best = {"mean": -1.0, "round": 0, "render": None, "verdict": None}
    prev_mean = None
    opts = _builder_options(shot, mcp_servers, tool_names, axes, ref_rel=m.ref)
    if resume and resume.get("session_id"):
        opts.resume = resume["session_id"]      # SDK restores the CONVERSATION
    async with ClaudeSDKClient(options=opts) as builder:
        also = [(f, r) for f, r in (layer.judges if layer else ()) if f != m.frame]
        # What earlier ATTEMPTS at this layer were told and kept being told. Without
        # this each attempt starts blind to the last one's corrections: layer 5's second
        # attempt rebuilt a six-light rig not knowing the first had twice been told the
        # hero was not light-linked and the podium was overlit.
        hist = recurring_complaints(shot, m)
        if hist:
            log(f"prior attempts: surfacing {hist.count('    - ')} recurring "
                f"complaint(s) to the builder", 1)
        _kickoff = builder_kickoff(shot, m, priors=priors,
                                   script_rel=script_rel,
                                   plan_excerpt=plan_excerpt,
                                   also_judged=also or None,
                                   history=hist)
        transcript.prompt(_kickoff, role="kickoff", layer=getattr(layer, "id", m.id),
                          judges=[[f, r] for f, r in (layer.judges if layer else ())],
                          owns=list(getattr(layer, "owns", ())),
                          model=MODEL, system_prompt_chars=len(opts.system_prompt or ""))
        await builder.query(_kickoff)
        _tools_before = sum(TOOL_USE.values())
        info = last_info = await _drain(builder, verbose)
        # A layer that spent nothing and touched no tool did not build anything, whatever
        # the result subtype claims. Caught here rather than after the critic, because the
        # next thing this function does is pay a vision model to look at an empty scene.
        _why = empty_success(info, sum(TOOL_USE.values()) - _tools_before)
        if _why:
            log(f"✗ build DID NOTHING: {_why}")
            transcript.event("empty_success", why=_why, **info)
            ledger.mark(m, "failed", best=None)
            raise BuildTruncated(f"layer {m.id}: {_why}")
        if info["subtype"] in _TRUNCATED:
            # Scoring a half-built scene produces a "failed" that says nothing about the
            # look, buys a misleading ledger entry, and pays the critic to judge it.
            log(f"✗ build TRUNCATED ({info['subtype']}, {info['turns']} turns, "
                f"${info['cost']:.2f}) — not critiquing an unfinished scene")
            ledger.mark(m, "truncated", best=None)
            raise BuildTruncated(
                f"layer {m.id}: {info['subtype']} after {info['turns']} turns "
                f"(${info['cost']:.2f}); raise MAX_BUDGET_USD or split the layer")

        for rnd in range(1, rounds + 1):
            log(f"── round {rnd}/{rounds} — rendering + critiquing frame {m.frame} ──")
            t_round = time.monotonic()
            render_rel = _stash_render(session, shot, m, f"r{rnd}")
            snap = session.snapshot(f"{m.id}_r{rnd}")  # {blend, journal_index}
            # Resume point: the SDK restores the CONVERSATION, the snapshot+journal
            # restores the SCENE. Both are needed or a resumed layer reasons about a
            # world that no longer exists.
            ledger.set_resume(m, session_id=last_info.get("session_id"),
                              blend=snap["blend"], journal_index=snap["journal_index"],
                              round=rnd)
            # Show the critic the previous best. Judging each round in isolation, it
            # re-derives an absolute verdict every time and cannot tell a round that
            # IMPROVED things from one that made them worse — which is also part of why
            # the same render scored 4.0/3.0/3.0/2.0 across repeats. Attaching one more
            # image is nearly free now that images are attached rather than fetched.
            prior = (best.get("render") if best.get("render")
                     and best["render"] != render_rel else None)
            verdict = await _judge(shot, m, render_rel, axes, session, verbose, scope,
                                   prior_rel=prior, prior_mean=best["mean"])
            verdict["round_s"] = round(time.monotonic() - t_round, 1)
            ledger.record_round(m, kind="iter", index=rnd, render=render_rel, verdict=verdict)
            state_round(shot.folder, frame=m.frame, mean=verdict["mean"],
                        passed=verdict["pass"], scores=verdict.get("scores"),
                        issues=verdict.get("issues"), approach=_APPROACH.get("text"))
            # best-of-N: keep the highest-scoring round (render AND scene snapshot)
            if verdict["mean"] > best["mean"]:
                best = {"mean": verdict["mean"], "round": rnd, "render": render_rel,
                        "verdict": verdict, "snap": snap}
                shutil.copyfile(shot.folder / render_rel, shot.folder / "renders" / f"{m.id}_best.png")
            if verdict["pass"]:
                passed = True
                break
            # A plateau is where a professional CHANGES TECHNIQUE, not where they stop.
            # This used to `break`: layer G tuned to 2.83 twice while city_texture sat at
            # 2 in every round, because nothing ever asked if instanced boxes with a
            # regular window grid could reach the reference at all. They cannot.
            plateaued = prev_mean is not None and verdict["mean"] <= prev_mean
            prev_mean = verdict["mean"]
            if rnd >= rounds:
                break
            if plateaued and layer is not None and not reviewed:
                reviewed = True
                log(f"plateau ({verdict['mean']} ≤ prev) — escalating to APPROACH REVIEW")
                out = await approach_review(shot, layer, render_rel, verdict, script_rel,
                                            metric_report=_metric_report(shot, render_rel,
                                                                         layer.judge_ref),
                                            verbose=verbose)
                if not out["text"]:
                    break                       # review unavailable: old behaviour
                ledger.record_review(m, rnd, out)
                await builder.query(revision_from_review(layer, out))
            elif plateaued:
                log(f"plateau again after review ({verdict['mean']}) — stopping revisions")
                break
            else:
                await builder.query(revision_prompt(m, verdict, render_rel))
            last_info = await _drain(builder, verbose)
        log(f"best round: r{best['round']} mean {best['mean']} → renders/{m.id}_best.png")

        # finalize from the BEST round's scene, not the last one — a regressed revision
        # must not be what gets written into build/<m>.py (cost build8 the layer).
        if best.get("snap") and best["round"] != rnd:
            log(f"restoring best round r{best['round']} scene state before finalize")
            session.restore(best["snap"]["blend"])
            await builder.query(
                f"NOTE: the scene has been RESTORED to your round-{best['round']} state "
                f"(the best-scoring round, mean {best['mean']}) — your later revision "
                f"scored worse and was discarded. Write the build script to reproduce "
                f"THIS restored scene.")
            await _drain(builder, verbose)

        # Persist the deterministic recipe regardless — it's the artifact of record.
        journal_rel = None
        try:  # a transcript to prune beats re-authoring 20KB+ from memory
            jrel = f"logs/journals/{m.id}.py"
            (shot.folder / "logs" / "journals").mkdir(parents=True, exist_ok=True)
            info = session.journal(path=str(shot.folder / jrel))
            if info.get("calls"):
                journal_rel = jrel
                _JOURNAL_INFO.clear(); _JOURNAL_INFO.update(info)
                log(f"journal: {info['calls']} accepted run_bpy calls "
                    f"({info['chars'] // 1024}KB) → {jrel}")
        except Exception as e:  # never block finalize on a nicety
            log(f"journal unavailable ({str(e)[:60]})")
        await builder.query(finalize_prompt(shot, m, priors=priors, script_rel=script_rel,
                                            journal_rel=journal_rel))
        fin = await _drain(builder, verbose)
        if fin["subtype"] in _TRUNCATED:
            # The script is probably half-written; verifying it would record a look
            # failure for a budget problem (the same lie truncation told at kickoff).
            log(f"✗ finalize TRUNCATED ({fin['subtype']}, ${fin['cost']:.2f}) — "
                f"{script_rel} may be incomplete; not scoring it")
            ledger.mark(m, "truncated", best=best)
            raise BuildTruncated(
                f"layer {m.id}: finalize hit {fin['subtype']} (${fin['cost']:.2f}); "
                f"raise MAX_BUDGET_USD or split the layer")

        # Confirm the written script REPRODUCES the unit from an empty scene. This is a
        # reproduction check, NOT a second quality layer — with judge noise, requiring two
        # consecutive layer-clears at the boundary is double jeopardy (cost M3 a pass).
        #
        # Done INSIDE the builder session so a canonical failure can be handed back. It
        # used to run after the session closed, so the critique was unreachable and the
        # layer simply died with it. Both barrel_roll layer-2 attempts produced ~24
        # specific, actionable fixes this way ("brightest strip runs down the centre where
        # the reference has two bright OUTER strips", "sign cabinet inverted: panel glows,
        # text dark") and discarded every one. Closing that gap by hand — read the log,
        # diagnose, edit the plan, re-run — cost $16.52 and two rounds of human attention
        # for feedback the pipeline was already holding.
        canonical = await _verify_script(shot, m, script_rel, prior_paths, session, axes,
                                         ledger, verbose, live_best_mean=best["mean"],
                                         scope=scope, layer=layer,
                                         out_verdicts=canon_verdicts)

        # Repair rounds, bounded. The target here is the SCRIPT's output from an empty
        # scene — not the live scene the builder has been tuning, which is why it must be
        # re-verified canonically or the loop would keep re-passing live and failing here.
        for attempt in range(1, MAX_CANON_REPAIRS + 1):
            if canonical != "failed":
                break
            failed = [(f, v) for (f, _r), v in (canon_verdicts or []) if not v.get("pass")]
            # The frames that currently PASS are constraints, not background. Feeding back
            # only the failures produced textbook whack-a-mole: repair 1 fixed f440 and
            # left f45 broken, repair 2 fixed f45 and BROKE f440. Every frame passed at
            # some point; never all at once. The builder was told what was wrong and
            # nothing about what it must not break, so trading one for the other looked
            # like progress. builder_kickoff already says "a change that fixes f{frame}
            # and breaks another of your frames is not a fix" — that instruction just
            # never made it into the repair path.
            holding = [(f, v) for (f, _r), v in (canon_verdicts or []) if v.get("pass")]
            if not failed:
                break
            log(f"canonical failed on {len(failed)} frame(s) — repair {attempt}/"
                f"{MAX_CANON_REPAIRS}, feeding the critique back"
                + (f" (protecting {len(holding)} passing frame(s))" if holding else ""))
            # Keep the script we are about to modify, plus the verdicts that describe it,
            # so a repair that makes things worse can be undone rather than merely regretted.
            backup = shot.folder / "logs" / f"{m.id}_prerepair{attempt}.py"
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(shot.folder / script_rel, backup)
            pre_verdicts = list(canon_verdicts or [])
            pre_canonical = canonical
            await builder.query(canonical_repair_prompt(m, failed, script_rel,
                                                        holding=holding))
            # last_info, NOT a throwaway: the layer report reads cost/turns from it, so
            # assigning to a local under-reported this layer as $14.64/37 turns when it
            # had actually spent $23.20 across five drains — within $1.80 of the cap,
            # invisible in the record.
            last_info = await _drain(builder, verbose)
            if last_info["subtype"] in _TRUNCATED:
                log(f"✗ canonical repair TRUNCATED ({last_info['subtype']}) — stopping "
                    f"here", 1)
                break
            # Re-verify, then compare against EVERY frame's pre-repair score rather than
            # only the failing ones — see _repair_delta, which owns both judgements (did
            # this break a passing frame, and did it move toward a pass at all).
            canon_verdicts.clear()
            canonical = await _verify_script(shot, m, script_rel, prior_paths, session,
                                             axes, ledger, verbose,
                                             live_best_mean=best["mean"], scope=scope,
                                             layer=layer, out_verdicts=canon_verdicts)
            delta = _repair_delta(pre_verdicts, canon_verdicts or [])
            was, now, broke = delta["was"], delta["now"], delta["broke"]
            if broke:
                # ENFORCE it. Telling the builder "these frames already pass, a trade is
                # not a fix" is a request, and it was ignored: the layer-3 pilot went
                # f100 3.0 PASS / f440 2.0 into a repair and came out 2.0 / 2.0 — the
                # repair broke the good frame and fixed nothing. This module's own
                # preamble says a rule stated in a prompt gets ignored; I wrote the
                # instruction anyway instead of the guard. Revert the script and stop:
                # the pre-repair version is strictly better than what we now hold.
                shutil.copyfile(backup, shot.folder / script_rel)
                log(f"✗ repair {attempt} REGRESSED f{', f'.join(map(str, broke))} "
                    f"({ {f: (was[f], now.get(f)) for f in broke} }) — reverting "
                    f"{script_rel} to its pre-repair state and stopping", 1)
                canon_verdicts.clear()
                canon_verdicts.extend(pre_verdicts)
                canonical = pre_canonical
                break
            if canonical == "failed" and not delta["progressed"]:
                log(f"repair {attempt} moved neither the worst failing frame "
                    f"({delta['was_worst']} → {delta['now_worst']}) nor the count of "
                    f"failing frames ({delta['was_failing']} → {delta['now_failing']}) "
                    f"— stopping rather than paying for another identical round", 1)
                break
    # The CANONICAL render is the deliverable — it is what the chain re-runs. If it
    # clears the bar the unit passed, whatever the live search scored on the way (with
    # finalize-from-best-snapshot the clean rebuild often outscores every live round:
    # SH G20 rounds 2.67/3.00, canonical 3.67 — previously recorded as a failure).
    ok = canonical == "passed" or (passed and canonical == "reproduced")
    ledger.mark(m, "passed" if ok else "failed", best=best)
    if ok and layer is not None:
        abl = await _ablate(shot, layer, prior_paths, script_rel, session)
        ledger.record_ablation(m, abl)
        if abl.get("moved"):
            log("ablation: " + " · ".join(f"{k}{v:+.0%}" for k, v in abl["moved"].items()), 1)
        elif abl.get("note"):
            log(f"ablation: {abl['note']}", 1)      # never silent — a skip is a result
        if not abl["ok"]:
            log(f"! {abl['note']}")
    if ok:
        v = ledger.snapshot_scripts(m, "pass")
        if v:
            log(f"chain snapshot → {Path(v).name} (revert point)", 1)
    if ok or _ERRORS:
        # QUEUED, not run here. Distillation writes recipes for FUTURE layers; nothing
        # downstream in this run needs them, yet the run used to sit and wait for a model
        # to finish writing prose before the next layer could start. Drain the queue with
        # `python -m pipeline.distill <shot>` after the run, or set BVFX_DISTILL_INLINE=1.
        req = {"milestone": m.id, "script_rel": script_rel if ok else None,
               "errors": list(_ERRORS), "run_id": RUN_ID}
        if os.environ.get("BVFX_DISTILL_INLINE") == "1":
            try:
                await distill_recipe(shot, m, verbose,
                                     script_rel=script_rel if ok else None,
                                     errors=list(_ERRORS))
            except Exception as e:
                log(f"distill skipped: {str(e)[:80]}")
        else:
            try:
                q = shot.folder / "logs" / "distill_queue.jsonl"
                q.parent.mkdir(parents=True, exist_ok=True)
                with q.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(req) + "\n")
                log(f"distillation queued ({len(_ERRORS)} error(s)) → "
                    f"logs/distill_queue.jsonl; drain with "
                    f"`python -m pipeline.distill {shot.folder}`", 1)
            except OSError as e:
                log(f"! could not queue distillation, running it inline: {e}")
                try:
                    await distill_recipe(shot, m, verbose,
                                         script_rel=script_rel if ok else None,
                                         errors=list(_ERRORS))
                except Exception as e2:
                    log(f"distill skipped: {str(e2)[:80]}")
    # One report per layer: everything that previously took six greps, plus what the
    # HOOKS did — a hook that never fires is silent by accident and invisible otherwise.
    try:
        slot = ledger._slot(m)
        rec_path = write_run(
            shot.folder, layer if layer is not None else m, status=slot.get("status", "?"),
            rounds=[{"kind": r.get("kind"), "mean": r.get("mean"), "pass": r.get("pass")}
                    for r in slot.get("rounds", []) if r.get("kind") != "canonical"],
            canonical=[{"frame": f, "mean": v["mean"], "pass": v["pass"]}
                       for (f, _r), v in (canon_verdicts or [])],
            ablation=slot.get("ablation", {}), reviews=slot.get("reviews", []),
            recipes=_RECIPES_USED, journal=_JOURNAL_INFO,
            cost=last_info.get("cost", 0.0), turns=last_info.get("turns", 0),
            seconds=time.monotonic() - t_layer,
            tokens=last_info.get("tokens", {}),
            approach=_APPROACH.get("text"),
            extra={"run_id": RUN_ID, "attempt": slot.get("attempt"),
                   "session_id": last_info.get("session_id"),
                   # WHICH tools the builder reached for. The four layers that passed
                   # barrel_roll called compare_frame 7-41 times; the one that failed
                   # three times called it 3-5 and measured 17-44 instead. Recovering
                   # that took grepping a console log that no longer exists.
                   "tools": tool_use_summary()})
        import json as _json
        _rec = _json.loads(rec_path.read_text())
        log("\n" + run_summary(_rec))
        # The layer's OUTCOME as the transcript's last word, so one file answers "what was
        # it asked, what did it do, what did the judge say, how did it end" without
        # joining across three artifacts.
        transcript.event("layer_end", status=_rec.get("status"),
                         layer=_rec.get("layer"),
                         cost_usd=_rec.get("cost_usd"), turns=_rec.get("turns"),
                         rounds=_rec.get("rounds"), canonical=_rec.get("canonical"),
                         tools=_rec.get("tools"), hooks=_rec.get("hooks"),
                         report=str(rec_path))
    except Exception as e:
        log(f"! run report unavailable: {str(e)[:80]}")
    transcript.unbind()
    _ERRORS.clear()
    _RECIPES_USED.clear()
    _APPROACH.clear()
    return ledger


async def build_layer(shot: Shot, layer, session: BlenderSession, *,
                     rounds: int = 2, verbose: bool = True,
                     resume_ok: bool = False, force: bool = False) -> Ledger:
    """Build one PLAN LAYER: chain lower-numbered layer scripts, implement this layer's
    tickets (its plan.md section is the spec), judge at its primary frame/ref."""
    excerpt = _plan_layer_excerpt(shot, layer)
    # EVERY layer is one layer of many, so every layer gets a scope block. Judging any
    # layer on the whole rubric scores it for work later layers do (BR layer G: floor on
    # emissive_finish, which layer F delivers 4 layers later — 2.83 ceiling in two
    # independent builds) and, worse, penalises elements a layer correctly REMOVED
    # (SH G50 @ f300 scored typography_legibility=0; the type hides at f197 by design).
    done = "\n".join(ln for ln in excerpt.splitlines()
                      if ln.startswith(("**Scope", "**Judge artifact", "**Done")))
    owned = (f"  THIS LAYER OWNS: {', '.join(layer.owns)}.\n"
             f"  Score ONLY those axes; every other axis is \"n/a\"."
             if layer.owns else
             f"  Score only what THIS layer's scope covers; everything else is \"n/a\".")
    # Name what the LATER layers deliver, by title. `owns` scopes the critic by AXIS, and
    # that is not fine-grained enough: layer 2 owns hero_tower_read, so every tower-shaped
    # thing in a wide frame counts against it — including the background city, which is
    # layer 3's job and has not been built yet. It was marked down at f440 for
    # "featureless flat-grey boxes" that were never its work. An axis can be owned and
    # still have parts of its subject produced elsewhere.
    later = [f"layer {g.id} ({g.title})" for g in sorted(
        load_layers(shot).values(), key=lambda g: str(g.script))
        if str(g.script) > str(layer.script)]
    not_yet = (f"  STILL TO COME, and therefore NOT this layer's to deliver or be marked "
               f"down for: {'; '.join(later)}. Judge the SUBJECT this layer built. If a "
               f"weakness in an owned axis comes from something a later layer delivers, "
               f"say so in `issues` and do NOT let it depress the score.\n"
               if later else "")
    scope = (f"  Layer {layer.id} — {layer.title} (one build stage of many; later layers "
             f"add the rest of the look).\n  {layer.reads}\n{done}\n{owned}\n{not_yet}"
             f"  Elements that are correctly ABSENT at this frame (they appear or "
             f"disappear in other layers) are \"n/a\", never 0.").strip()
    if len(layer.judges) > 1:
        log(f"layer {layer.id} answers for {len(layer.judges)} frames: "
            + ", ".join(f"f{f} vs {r}" for f, r in layer.judges))
    try:  # compaction-proof contract, re-injected on every request
        fps = {}
        try:
            from .ledger import load_milestones
            fps = {m.frame: m.fingerprint for m in load_milestones(shot).values() if m.fingerprint}
        except Exception as e:
            log(f"! no measured fingerprints in the layer contract: {str(e)[:60]}", 1)
        p = write_layer_context(shot, layer, load_axes(shot), fps)
        log(f"layer context → {p.relative_to(shot.folder)} (loaded every request)", 1)
    except Exception as e:
        log(f"layer context skipped: {str(e)[:70]}", 1)
    strips = plan_strips(shot)
    return await build_unit(shot, layer.as_milestone(strips), layer.script,
                            _prior_layer_paths(shot, layer, force=force), session,
                            rounds=rounds, verbose=verbose,
                            plan_excerpt=excerpt, scope=scope, layer=layer,
                            resume_ok=resume_ok)


async def _ablate(shot: Shot, layer, prior_paths: list[Path], script_rel: str,
                  session: BlenderSession) -> dict:
    """Does this layer's script actually CHANGE its judge frames?

    The pipeline already uses ablation inside a build — `warm-session-probe-loop` proved
    Glare strength was not driving halation and that DOF was a silent no-op. The same
    question one level up has never been asked: a layer can pass on axes it does not own
    (SH G60 scored 2.67 on typography and palette, neither of which is its work) while
    contributing nothing measurable at all.

    Render the primary judge frame WITHOUT this layer's script, then WITH it, and compare.
    Two renders, no model.
    """
    from .metrics import look_vector
    frame, _ref = layer.judges[0]
    try:
        session.run(_RESET); session.run(_preamble(shot))
        _run_prior_paths(session, prior_paths)
        try:
            without = look_vector(session.render(frame=frame, mode="eevee", scale=0.4))
        except Exception as e:
            # The FIRST layer has no priors, so "without it" is an empty scene with no
            # camera. That is not a skip — it is the strongest possible result: nothing
            # renders at all until this layer runs.
            if "no camera" in str(e).lower():
                session.run((shot.folder / script_rel).read_text(encoding="utf-8"))
                session.render(frame=frame, mode="eevee", scale=0.4)   # must now work
                return {"ok": True, "frame": frame, "moved": {},
                        "note": "scene cannot render at all without this layer "
                                "(no camera) — it establishes the spine"}
            raise
        session.run((shot.folder / script_rel).read_text(encoding="utf-8"))
        with_ = look_vector(session.render(frame=frame, mode="eevee", scale=0.4))
    except Exception as e:
        return {"ok": True, "note": f"ablation INCONCLUSIVE: {str(e)[:70]}"}
    moved = {}
    for k, v in with_.items():
        base = without.get(k, 0.0)
        denom = max(abs(base), 1e-6)
        if abs(v - base) > max(0.02 * denom, 1e-6):
            moved[k] = round((v - base) / denom, 3)
    top = sorted(moved.items(), key=lambda kv: -abs(kv[1]))[:4]
    changed = bool(top) and max(abs(v) for _k, v in top) > 0.05
    return {"ok": changed, "moved": dict(top), "frame": frame,
            "note": ("" if changed else
                     f"f{frame} is essentially IDENTICAL with and without "
                     f"{Path(script_rel).name} — this layer may be a no-op")}


def _metric_report(shot: Shot, render_rel: str, ref_rel: str) -> str:
    """Objective ref-deltas for the reviewer — technique problems show up as structural
    metrics (points, detail) rather than exposure."""
    try:
        from .metrics import compare, look_pair, report
        d = compare(*look_pair(str(shot.folder / render_rel),
                               str(shot.folder / ref_rel)))
        return report(d) if d else ""
    except Exception as e:
        log(f"! metric report unavailable for the review: {str(e)[:70]}", 1)
        return ""


async def _verify_script(shot: Shot, m: Milestone, script_rel: str,
                         prior_paths: list[Path], session: BlenderSession,
                         axes: list[tuple[str, str]], ledger: Ledger, verbose: bool,
                         live_best_mean: float | None = None,
                         scope: str | None = None,
                         layer=None, out_verdicts: list | None = None) -> str:
    """-> "passed" (canonical clears the bar itself) | "reproduced" (matches the live
    best within noise) | "failed".

    Scores EVERY frame the layer answers for, not just the primary. Iteration renders one
    frame for speed; the deliverable has to hold at all of them, and a layer is only as
    good as its worst claimed frame.
    """
    script_path = shot.folder / script_rel
    if not script_path.is_file():
        log(f"! builder never wrote {script_rel}")
        return "failed"
    log(f"verifying {script_path.name} reproduces from an empty scene…")
    try:
        session.run(_RESET)
        session.run(_preamble(shot))
        _run_prior_paths(session, prior_paths)  # deltas assume priors ran first
        session.run(script_path.read_text(encoding="utf-8"))
    except BlenderError as e:
        log(f"! build script failed: {str(e)[:200]}")
        ledger.record_round(
            m, kind="canonical", index=0, render="",
            verdict=_verdict({"scores": {}, "issues": [f"script error: {e}"]}))
        return "failed"
    judges = list(layer.judges) if layer is not None else [(m.frame, m.ref)]
    # Render serially (one Blender session), then score CONCURRENTLY — the critic calls
    # are independent judgements of already-written PNGs, and multi-frame judging tripled
    # the pass count on server_to_hansa (8 -> 18).
    shots_ = []
    for frame, ref in judges:
        m_i = (m if len(judges) == 1
               else layer.milestone_at(frame, ref, plan_strips(shot)))
        render_rel = _stash_render(session, shot, m_i, f"canonical_f{frame}"
                                   if len(judges) > 1 else "canonical")
        shots_.append((frame, ref, m_i, render_rel))
    results: list = [None] * len(shots_)

    async def _score(i, m_i, render_rel):
        results[i] = await _judge(shot, m_i, render_rel, axes, session, verbose, scope)

    if len(shots_) == 1:
        await _score(0, shots_[0][2], shots_[0][3])
    else:
        async with anyio.create_task_group() as tg:
            for i, (_f, _r, m_i, rr) in enumerate(shots_):
                tg.start_soon(_score, i, m_i, rr)
    verdicts = []
    for i, (frame, ref, _m_i, render_rel) in enumerate(shots_):
        v = results[i]
        ledger.record_round(m, kind="canonical", index=i, render=render_rel, verdict=v)
        verdicts.append(((frame, ref), v))
    if out_verdicts is not None:
        out_verdicts.extend(verdicts)      # the run report needs the per-frame results
    if len(judges) > 1:
        log("canonical per-frame: " + " · ".join(
            f"f{f}:{v['mean']}{'✅' if v['pass'] else '✗'}" for (f, _), v in verdicts), 1)
    # weakest claimed frame decides — that is the entire point of listing them
    (worst_frame, _), verdict = min(verdicts, key=lambda kv: kv[1]["mean"])
    if all(v["pass"] for _, v in verdicts):
        log(f"canonical clears every claimed frame (worst f{worst_frame} "
            f"{verdict['mean']})", 1)
        return "passed"
    # reproduction tolerance: within judge noise of the live best still counts as
    # "the script reproduces what passed" — widened for low-axis-count layers, where a
    # single point of variance swings the mean by 1/n (SH G10: 4.0 → 3.0 on one axis).
    tol = _repro_tolerance(len(verdict.get("scored_axes", [])))
    if live_best_mean is not None and verdict["mean"] >= live_best_mean - tol:
        log(f"canonical {verdict['mean']} within noise (±{tol:.2f}) of live best "
            f"{live_best_mean} — reproduction verified")
        return "reproduced"
    return "failed"


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #
async def _run(folder: str, layer_id: str, rounds: int, blender: str,
               resume_ok: bool = False, force: bool = False) -> None:
    # Cheapest possible check, first: a credential in a variable nothing reads costs a
    # whole layer to discover otherwise, and it does not fail loudly when it happens.
    warn_if_broken()
    shot = load_shot(folder)
    # Is the plan still a plan for THIS brief? Editing brief.md leaves the plan stale with
    # nothing recording the divergence, and every layer below is then built to a spec that
    # no longer exists. An edited INPUT refuses; an edited artifact only warns, since
    # hand-tuning layers.json is a legitimate thing to do mid-build.
    stale = provenance_check(shot.folder)
    for s in stale:
        log(f"! plan provenance: {s}")
    if any("CHANGED since the plan" in s for s in stale) and not force:
        log("   re-plan with `python -m pipeline.plan_agent <folder>`, or --force")
        raise SystemExit(8)

    # Questions are asked at PLAN time and must be settled BEFORE any layer runs. Building
    # on an unanswered assumption is how barrel_roll ended up 16:9 against 2:1 references
    # — by the time a later layer could notice, the camera had been committed three layers
    # earlier and every composition score was measured against the wrong crop.
    unanswered = [q for q in load_questions(shot.folder) if not q.get("answer")]
    if unanswered and not force:
        log(f"✗ {len(unanswered)} unanswered question(s) from the plan — answer them "
            f"before building (or pass --force to build on the assumptions):")
        for q in unanswered:
            log(f"   Q{q['id']}: {q['question']}", 1)
            log(f"        assuming: {q['assumption']}", 1)
        log(f"   answer with: python -m pipeline.escalate {folder} --answer <id> \"...\"")
        raise SystemExit(5)
    if unanswered:
        log(f"! building with {len(unanswered)} question(s) unanswered (--force)")
    session = BlenderSession(blender=blender, blend_file=None,
                             assets_dir=shot.folder / "assets",
                             cwd=shot.folder).start()
    try:
        layers = load_layers(shot)
        g = layers.get(layer_id) or layers.get(layer_id.upper())
        if g is None:
            raise SystemExit(f"unknown layer {layer_id!r}; known: {', '.join(layers)}")
        # Name EVERY judge frame. The banner used to print only the first, while the very
        # next line said "answers for 4 frames" — and single-frame judging is precisely
        # the bug that let a blacked-out stretch of barrel_roll through, so a banner that
        # under-reports the judge list is the wrong thing to get wrong.
        judged = " · ".join(f"f{f} vs {r}" for f, r in g.judges) or "no judge frame"
        log(f"build agent: shot '{shot.id}' LAYER {g.id} — {g.title} "
            f"(judges: {judged}) → {g.script}, model {MODEL}")
        ledger = await build_layer(shot, g, session, rounds=rounds, resume_ok=resume_ok,
                                   force=force)
        log(f"{g.id}: {ledger.status(g.as_milestone())}  →  {ledger.path}")
    finally:
        session.close()
        # This was imported and never called. write_layer_context() overwrites CLAUDE.md
        # per layer, so nothing leaked BETWEEN layers — but the last layer's contract was
        # left behind in the shot folder, where any later project-scoped session would
        # silently load it as if it were current. Generated context should not outlive
        # the layer that generated it. (Only removes a file it wrote; never a hand-written
        # CLAUDE.md.)
        clear_layer_context(shot)


def main() -> None:
    ap = argparse.ArgumentParser(description="Build one plan layer with the critic loop.")
    ap.add_argument("folder", help="shot folder (contains brief.md + refs/)")
    ap.add_argument("--layer", required=True,
                    help="layer id from layers.json — 1, 2, 3 …")
    ap.add_argument("--rounds", type=int, default=2, help="max build↔critic rounds")
    ap.add_argument("--blender", default="blender", help="blender executable")
    ap.add_argument("--force", action="store_true",
                    help="override the pre-build refusals: unanswered plan questions "
                         "(uses the assumptions) and unaccepted prior layers. Debugging "
                         "only — anything built this way rests on unreviewed work.")
    ap.add_argument("--resume", action="store_true",
                    help="continue a crashed/truncated run: restore its scene checkpoint, "
                         "replay the journal, and resume the same SDK session")
    args = ap.parse_args()
    try:
        anyio.run(_run, args.folder, args.layer, args.rounds, args.blender, args.resume, args.force)
    except BuildTruncated as e:
        log(f"BUILD TRUNCATED — {e}")
        raise SystemExit(3)  # distinct from a crash (1) so drivers can tell them apart
    except ChainBroken as e:
        log(f"CHAIN BROKEN — {e}")
        raise SystemExit(4)  # the chain needs repair; building on is pointless
    except UnpassedPrior as e:
        log(f"UNACCEPTED PRIOR — {e}")
        raise SystemExit(6)  # a lower layer must pass first


if __name__ == "__main__":
    main()
