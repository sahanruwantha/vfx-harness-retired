"""Hooks that ENFORCE what prompts only request.

Observed, repeatedly: a rule stated in the system prompt AND in two recipes still gets
ignored. `bvfx_glare_bloom` was in the builder's system prompt and documented in both
`cinematic-grade` and `blender-5-api`, and the builder hand-rolled a 4.x
`CompositorNodeGlare` twice inside 54 seconds. Recipes have to be *fetched* to help;
prompt lines get skimmed and, on a long build, compacted away.

A PreToolUse hook runs in our process before the call executes, costs no context, and is
not optional. That is the right home for "never do X" — the model finds out at the moment
it matters, with the replacement named.

`api_guardrails()` blocks known-broken Blender-4 idioms in run_bpy.
`metrics_feedback()` appends objective ref-deltas to every render so the builder gets
numbers without asking (the critic never once noticed barrel_roll M1 was 54% hot).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from claude_agent_sdk import HookMatcher

from .layer_state import NAME as LAYER_STATE
from .log import log
from .runlog import bump

# pattern -> what to do instead. Each of these was hit for real during a build.
_BANNED: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\.glare_type\s*="),
     "Glare settings are INPUT SOCKETS in 5.x — `.glare_type` does not exist and "
     "retrying it fails identically. Call bvfx_glare_bloom(threshold=, size=, strength=)."),
    (re.compile(r"scene\.node_tree|sc\.node_tree|\bbpy\.context\.scene\.node_tree"),
     "scene.node_tree is GONE in 5.x. The compositor is scene.compositing_node_group; "
     "use bvfx_glare_bloom(...) to set it and inspect_nodes('compositor') to read it."),
    (re.compile(r"\.use_bloom\s*="),
     "EEVEE-Next has no use_bloom. Bloom is a compositor Glare node — bvfx_glare_bloom(...)."),
    (re.compile(r"file_format\s*=\s*['\"]FFMPEG['\"]"),
     "file_format is stills-only here ('FFMPEG' is not in the 5.x enum). Video muxing is "
     "the harness's job (pipeline.render_shot), not a layer's. Use 'PNG'."),
    (re.compile(r"\.layers\[0\]\.strips\[0\]"),
     "An action only grows a layer/strip once something is keyed on it, so [0][0] raises "
     "on any un-keyed ID. Iterate: for layer in act.layers: for strip in layer.strips: ..."),
    (re.compile(r"ShaderNodeVolumeEmission"),
     "ShaderNodeVolumeEmission does not exist in 5.2. Use ShaderNodeVolumePrincipled "
     "(it has Emission Strength / Emission Color inputs)."),
]

_CODE_KEYS = ("script", "code")

# Where a builder may look things up. Unrestricted web mid-build is a rabbit-hole
# generator and the builder has the least budget discipline of any agent here; but with
# NO lookup it guesses at an API, fails, and guesses identically again (glare_type, twice
# in 54 seconds). Docs only.
_WEB_ALLOW = ("docs.blender.org", "blender.stackexchange.com",
              "developer.blender.org", "projects.blender.org")


def web_allowlist() -> HookMatcher:
    """Permit WebFetch/WebSearch only against Blender documentation."""
    async def _check(inp, tool_use_id, ctx) -> dict:
        tool = inp.get("tool_name", "") if isinstance(inp, dict) else getattr(inp, "tool_name", "")
        if tool not in ("WebFetch", "WebSearch"):
            return {}
        args = (inp.get("tool_input") if isinstance(inp, dict) else getattr(inp, "tool_input", {})) or {}
        target = str(args.get("url") or args.get("query") or "")
        if tool == "WebFetch" and not any(d in target for d in _WEB_ALLOW):
            log(f"⛔ web: {target[:60]} is outside the docs allowlist", 1)
            bump("web_blocked")
            return {"hookSpecificOutput": {
                "hookEventName": "PreToolUse", "permissionDecision": "deny",
                "permissionDecisionReason":
                    f"Only Blender documentation is reachable from a build: "
                    f"{', '.join(_WEB_ALLOW)}. For anything else, use find_recipe, or "
                    f"prove it with run_bpy instead of reading about it."}}
        return {}
    return HookMatcher(matcher=None, hooks=[_check])


def api_guardrails() -> HookMatcher:
    """Deny run_bpy payloads containing Blender-4 idioms that are known to fail."""
    async def _check(inp: Any, tool_use_id: str | None, ctx: Any) -> dict:
        tool = inp.get("tool_name", "") if isinstance(inp, dict) else getattr(inp, "tool_name", "")
        if not tool.endswith("run_bpy"):
            return {}
        args = (inp.get("tool_input") if isinstance(inp, dict) else getattr(inp, "tool_input", {})) or {}
        code = next((str(args[k]) for k in _CODE_KEYS if args.get(k)), "")
        for pat, fix in _BANNED:
            if pat.search(code):
                log(f"⛔ guardrail: blocked `{pat.pattern}` — {fix[:60]}…", 1)
                bump("api_guardrail_blocked")
                return {"hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": f"BLOCKED (known-broken in Blender 5.x). {fix}",
                }}
        return {}
    return HookMatcher(matcher=None, hooks=[_check])


def metrics_feedback(shot_folder: str | Path, ref_rel: str | None) -> HookMatcher:
    """After every render, append objective ref-deltas to the tool result.

    The builder otherwise has to *decide* to measure. Pushing the numbers in makes
    'you are 54% over-exposed' unmissable instead of something a critic might mention.
    """
    folder = Path(shot_folder)

    async def _after(inp: Any, tool_use_id: str | None, ctx: Any) -> dict:
        tool = inp.get("tool_name", "") if isinstance(inp, dict) else getattr(inp, "tool_name", "")
        # render_frame ONLY. compare_frame now computes and prints the same signed gap
        # itself, against the reference the builder actually named — appending a second
        # near-identical report (and against a possibly DIFFERENT ref, the layer's
        # primary one) is noise that makes the builder reconcile two sets of numbers.
        # render_frame takes no reference, so it is the case that still needs pushing.
        if not tool.endswith("render_frame") or not ref_rel:
            return {}
        ref = folder / ref_rel
        if not ref.is_file():
            return {}
        try:
            from .metrics import compare, look_pair, report
            # Build renders land in .artifacts/ (the warm session's dir); only the
            # critic's stashed copies go to renders/. Looking in one place made this
            # hook a silent no-op for the entire build phase — exactly when the
            # feedback is worth having.
            cands = [p for d in (".artifacts", "renders")
                     for p in (folder / d).glob("*.png")]
            latest = max(cands, key=lambda p: p.stat().st_mtime, default=None)
            if latest is None:
                return {}
            d = compare(*look_pair(str(latest), str(ref)))
            if not d:
                return {}
            bump("metric_feedback")
            return {"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                           "additionalContext": report(d)}}
        except Exception as e:
            # never break a render on feedback — but a hook that dies quietly is exactly
            # how this one stayed a silent no-op for an entire build phase
            log(f"! metric feedback unavailable: {str(e)[:70]}", 1)
            return {}
    return HookMatcher(matcher=None, hooks=[_after])


def compaction_notice(shot_folder: str | Path) -> HookMatcher:
    """Make compaction VISIBLE, and point the builder at its durable state.

    A long layer gets compacted mid-build and nothing recorded it: the builder silently
    lost the middle of its own reasoning, and afterwards the only symptom was work being
    repeated. Compaction is also exactly when the layer's conclusions need to be somewhere
    other than the transcript — layer_state.json holds the measured state per judge frame
    and what has already been ruled out.
    """
    async def _pre(inp, tool_use_id, ctx):
        bump("compaction")
        trigger = (inp or {}).get("trigger", "?")
        log(f"⚠ CONTEXT COMPACTED mid-layer (trigger={trigger}) — the transcript is being "
            f"summarised; conclusions live in logs/{LAYER_STATE}")
        block = ""
        try:
            from .layer_state import as_prompt_block
            block = as_prompt_block(shot_folder)
        except Exception as e:
            log(f"! layer state unavailable at compaction: {str(e)[:70]}")
        if not block:
            return {}
        return {"hookSpecificOutput": {"hookEventName": "PreCompact",
                                       "additionalContext": block}}

    return HookMatcher(matcher=None, hooks=[_pre])


def recipe_write_guard() -> HookMatcher:
    """Force `verified: false` on any recipe the distiller writes, and keep spike
    scaffolding out of the recipe body.

    Both rules are already in DISTILL_SYSTEM, and the top of this module exists because a
    rule stated in a system prompt AND two recipes still got ignored twice in 54 seconds.
    Prompt-only, the failure lands late and confusingly: the recipe is written, the run
    finishes, and the NEXT test-suite invocation fails on a file nobody in that session
    deliberately wrote. `verified: true` now means an executed, hash-pinned spike — the
    harvester is reading a script, not running one, and cannot know it.
    """
    async def _check(inp, tool_use_id, ctx) -> dict:
        tool = inp.get("tool_name", "") if isinstance(inp, dict) else getattr(inp, "tool_name", "")
        if tool not in ("Write", "Edit"):
            return {}
        args = (inp.get("tool_input") if isinstance(inp, dict) else getattr(inp, "tool_input", {})) or {}
        target = str(args.get("file_path") or "")
        if "/recipes/" not in target.replace("\\", "/") or not target.endswith(".md"):
            return {}
        body = str(args.get("content") or args.get("new_string") or "")
        if re.search(r"^verified:\s*true\s*$", body, re.MULTILINE | re.IGNORECASE):
            bump("recipe_verified_blocked")
            log(f"⛔ recipe write: {Path(target).name} claimed `verified: true`", 1)
            return {"hookSpecificOutput": {
                "hookEventName": "PreToolUse", "permissionDecision": "deny",
                "permissionDecisionReason":
                    "Write `verified: false`. That flag is set ONLY by "
                    "pipeline.verify_recipes after the snippet has been EXECUTED in a "
                    "headless Blender and its top-level callables invoked, with a sha256 "
                    "of the code recorded as evidence. Claiming it here fails the audit "
                    "and the test suite. Earn it with: "
                    "python -m pipeline.verify_recipes --name <slug> --sync"}}
        if "SPIKE_ARGS" in body:
            bump("recipe_scaffold_blocked")
            return {"hookSpecificOutput": {
                "hookEventName": "PreToolUse", "permissionDecision": "deny",
                "permissionDecisionReason":
                    "Spike scaffolding must NOT go in the recipe body — find_recipe hands "
                    "that text to a builder verbatim, so SPIKE_ARGS would be pasted into "
                    "a real shot. Put it in pipeline/recipes/_spikes/<slug>.py instead "
                    "(see the README there)."}}
        return {}
    return HookMatcher(matcher=None, hooks=[_check])


def distiller_hooks(*roots, cwd: str | Path | None = None) -> dict:
    """Sandbox + the recipe-frontmatter guard, for the harvesting agent."""
    from .sandbox import path_sandbox
    return {"PreToolUse": [path_sandbox(*roots, cwd=cwd), recipe_write_guard()]}


def builder_hooks(shot_folder: str | Path, roots: list, ref_rel: str | None = None) -> dict:
    """PreToolUse: path sandbox + API guardrails. PostToolUse: metric feedback."""
    from .sandbox import path_sandbox
    return {
        "PreToolUse": [path_sandbox(*roots, cwd=shot_folder), api_guardrails(),
                       web_allowlist()],
        "PostToolUse": [metrics_feedback(shot_folder, ref_rel)],
        "PreCompact": [compaction_notice(shot_folder)],
    }
