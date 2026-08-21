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

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from claude_agent_sdk import HookMatcher

from vfx_harness.observability import run_artifacts
from vfx_harness.observability.log import log
from vfx_harness.observability.runlog import bump

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
     "the harness's job (vfx_harness.application.render_shot), not a layer's. Use 'PNG'."),
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


def _injected_scope() -> set[str]:
    """Exactly what a run_bpy call can see. Read from worker.py's own _HELPERS so a new
    bvfx_* helper can never become a false positive here."""
    import builtins
    names = {"bpy", "math", "mathutils", "Vector"} | set(dir(builtins))
    try:
        package = Path(__file__).resolve().parents[1]
        src = (package / "blender" / "worker.py").read_text(encoding="utf-8")
        names |= set(re.findall(r'"(bvfx_\w+)":', src))
    except OSError as e:
        # Fall back to blocking nothing rather than blocking everything.
        log(f"! could not read worker helpers, undefined-name check disabled: {e}")
        return set()
    return names


def _undefined_names(tree) -> set[str]:
    """Names this script CALLS but nothing defines — the fresh-namespace trap.

    Deliberately under-reports. A false positive here blocks legitimate work, which is
    far worse than missing a NameError the builder will see anyway, so this only flags a
    bare `name(...)` call where `name` is bound nowhere in the script and is not in the
    injected scope. Anything dynamic (globals(), exec, star-import, getattr) disables the
    check entirely rather than risking a wrong deny.
    """
    import ast
    scope = _injected_scope()
    if not scope:
        return set()
    src_names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    if {"globals", "locals", "exec", "eval", "vars"} & src_names:
        return set()                       # dynamic binding: cannot reason statically

    bound: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(n.name)
            bound |= {a.arg for a in n.args.args + n.args.kwonlyargs + n.args.posonlyargs}
            for a in (n.args.vararg, n.args.kwarg):
                if a is not None:
                    bound.add(a.arg)
        elif isinstance(n, ast.Lambda):
            bound |= {a.arg for a in n.args.args}
        elif isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            bound.add(n.id)
        elif isinstance(n, ast.alias):
            bound.add((n.asname or n.name).split(".")[0])
            if n.name == "*":
                return set()               # star-import: unknowable
        elif isinstance(n, ast.ExceptHandler) and n.name:
            bound.add(n.name)
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            bound |= set(n.names)

    called = {n.func.id for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    return called - bound - scope


def script_sanity() -> HookMatcher:
    """Parse every run_bpy payload before it executes: syntax, and bare `next(...)`.

    An error HINT teaches one session. Every layer is a separate process with a fresh SDK
    session, so a shot with 8 layers gets 8 independent chances to make the same mistake —
    which is why bare `next(...)` raised StopIteration in one run, got a good hint, was
    absorbed, and then raised again in the next run's layer 2. Reactive help cannot
    accumulate across a boundary the pipeline deliberately creates. A PreToolUse deny can.

    Detection is by AST, not regex: nested parens defeat any pattern, and `next(gen, None)`
    (the safe form) differs from `next(gen)` only by an argument count. Zero false
    positives by construction.

    Parsing also gives a free syntax check. A typo currently costs a full round-trip into
    Blender to discover.
    """
    import ast as _ast

    async def _check(inp, tool_use_id, ctx) -> dict:
        tool = inp.get("tool_name", "") if isinstance(inp, dict) else getattr(inp, "tool_name", "")
        if not tool.endswith("run_bpy"):
            return {}
        args = (inp.get("tool_input") if isinstance(inp, dict) else getattr(inp, "tool_input", {})) or {}
        code = next((str(args[k]) for k in _CODE_KEYS if args.get(k)), "")
        if not code.strip():
            return {}
        try:
            tree = _ast.parse(code)
        except SyntaxError as e:
            bump("syntax_blocked")
            log(f"⛔ run_bpy has a syntax error at line {e.lineno} — blocked before "
                f"executing", 1)
            return {"hookSpecificOutput": {
                "hookEventName": "PreToolUse", "permissionDecision": "deny",
                "permissionDecisionReason":
                    f"SyntaxError on line {e.lineno}: {e.msg}\n"
                    f"    {(e.text or '').rstrip()}\n"
                    f"Fix it and resend — this never reached Blender."}}

        undefined = _undefined_names(tree)
        if undefined:
            bump("undefined_name_blocked")
            names = ", ".join(sorted(undefined)[:4])
            log(f"⛔ run_bpy calls undefined name(s): {names} — blocked", 1)
            return {"hookSpecificOutput": {
                "hookEventName": "PreToolUse", "permissionDecision": "deny",
                "permissionDecisionReason":
                    f"BLOCKED: this script uses {names}, which nothing defines.\n"
                    f"EVERY run_bpy call is a FRESH NAMESPACE — it cannot see anything "
                    f"you defined in a previous call. In scope you have only: bpy, math, "
                    f"mathutils, Vector, the bvfx_* helpers, and Python builtins.\n"
                    f"Either inline the definition in THIS call, or put it in the build "
                    f"script where it will be defined at module scope when the script "
                    f"runs."}}

        bare = [n.lineno for n in _ast.walk(tree)
                if isinstance(n, _ast.Call) and isinstance(n.func, _ast.Name)
                and n.func.id == "next" and len(n.args) == 1 and not n.keywords]
        if bare:
            bump("bare_next_blocked")
            log(f"⛔ run_bpy uses bare next(...) at line(s) {bare} — blocked", 1)
            return {"hookSpecificOutput": {
                "hookEventName": "PreToolUse", "permissionDecision": "deny",
                "permissionDecisionReason":
                    f"BLOCKED: bare `next(...)` at line(s) {', '.join(map(str, bare))}. "
                    f"When the generator matches nothing it raises StopIteration, which "
                    f"carries NO message — you would get a bare 'StopIteration:' and "
                    f"nothing to act on. Write `x = next((... for ... if ...), None)` and "
                    f"handle `x is None`, or call inspect_nodes(...) first to see which "
                    f"node types actually exist. For the usual targets the bvfx_* helpers "
                    f"already handle the miss."}}

        indexed = []
        ensured: dict[tuple[str, str], list[int]] = {}
        for node in _ast.walk(tree):
            if (isinstance(node, _ast.Call)
                    and isinstance(node.func, _ast.Attribute)
                    and node.func.attr == "ensure_lookup_table"
                    and isinstance(node.func.value, _ast.Attribute)
                    and isinstance(node.func.value.value, _ast.Name)
                    and node.func.value.attr in {"faces", "verts", "edges"}):
                key = (node.func.value.value.id, node.func.value.attr)
                ensured.setdefault(key, []).append(node.lineno)
            if not isinstance(node, _ast.Subscript) or not isinstance(node.value, _ast.Attribute):
                continue
            owner = node.value.value
            seq = node.value.attr
            if isinstance(owner, _ast.Name) and seq in {"faces", "verts", "edges"}:
                indexed.append((owner.id, seq, node.lineno))
        missing_lookup = []
        for owner, seq, line in indexed:
            if not any(ensure_line < line for ensure_line in ensured.get((owner, seq), [])):
                missing_lookup.append(f"{owner}.{seq}[...] at line {line}")
        if missing_lookup:
            bump("bmesh_lookup_blocked")
            return {"hookSpecificOutput": {
                "hookEventName": "PreToolUse", "permissionDecision": "deny",
                "permissionDecisionReason":
                    "BLOCKED before partial mesh mutation: indexed bmesh access requires "
                    "the matching lookup table in this same run_bpy call. Before "
                    + ", ".join(missing_lookup)
                    + ", call `<bm>.<faces|verts|edges>.ensure_lookup_table()`."
            }}
        return {}
    return HookMatcher(matcher=None, hooks=[_check])


def metrics_feedback(shot_folder: str | Path, ref_rel: str | None, *,
                     look_actions: bool = True) -> HookMatcher:
    """After owned-look renders, append objective ref-deltas to the tool result.

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
        if not look_actions or not tool.endswith("render_frame") or not ref_rel:
            return {}
        ref = folder / ref_rel
        if not ref.is_file():
            return {}
        try:
            from vfx_harness.evidence.metrics import compare, look_pair, report
            layout = run_artifacts.ensure(folder, command="metrics-feedback")
            cands = [
                *layout.scratch.joinpath("blender").glob("*.png"),
                *layout.evidence.joinpath("renders").glob("*.png"),
            ]
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
        bump("compaction_started")
        trigger = (inp or {}).get("trigger", "?")
        from vfx_harness.orchestration.layer_state import path_for
        state_rel = path_for(shot_folder).relative_to(Path(shot_folder)).as_posix()
        log(f"⚠ CONTEXT COMPACTION STARTING (trigger={trigger}) — checkpointing "
            f"conclusions in {state_rel}")
        block = ""
        try:
            from vfx_harness.observability import transcript
            from vfx_harness.orchestration.layer_state import as_prompt_block, checkpoint
            checkpoint(shot_folder, trigger=trigger)
            block = as_prompt_block(shot_folder)
            transcript.event("pre_compact", trigger=trigger, state_file=state_rel)
        except Exception as e:
            log(f"! layer state unavailable at compaction: {str(e)[:70]}")
        capsule = (
            "## Pre-compaction continuation capsule\n"
            "- MODE remains LIVE_BUILD: mutate only the warm scene with run_bpy; never "
            "Write or Edit the build script.\n"
            "- Re-read the authoritative layer plan named in CLAUDE.md; never use "
            "shot-root plan.md.\n"
            "- Continue from durable measured state below; do not retry ruled-out work.\n"
        ) + (block or "- No judged round has been recorded yet.\n")
        return {"hookSpecificOutput": {"hookEventName": "PreCompact",
                                       "additionalContext": capsule}}

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
                    "vfx_harness.knowledge.verify_recipes after the snippet has been EXECUTED in a "
                    "headless Blender and its top-level callables invoked, with a sha256 "
                    "of the code recorded as evidence. Claiming it here fails the audit "
                    "and the test suite. Earn it with: "
                    "python -m vfx_harness.knowledge.verify_recipes --name <slug> --sync"}}
        if "SPIKE_ARGS" in body:
            bump("recipe_scaffold_blocked")
            return {"hookSpecificOutput": {
                "hookEventName": "PreToolUse", "permissionDecision": "deny",
                "permissionDecisionReason":
                    "Spike scaffolding must NOT go in the recipe body — find_recipe hands "
                    "that text to a builder verbatim, so SPIKE_ARGS would be pasted into "
                    "a real shot. Put it in vfx_harness/knowledge/recipes/_spikes/<slug>.py instead "
                    "(see the README there)."}}
        return {}
    return HookMatcher(matcher=None, hooks=[_check])


def distiller_hooks(*roots, cwd: str | Path | None = None) -> dict:
    """Sandbox + the recipe-frontmatter guard, for the harvesting agent."""
    from vfx_harness.infrastructure.sandbox import path_sandbox
    return {"PreToolUse": [path_sandbox(*roots, cwd=cwd), recipe_write_guard()]}


def completion_gate(shot_folder: str | Path, script_rel: str | None,
                    phase: dict[str, str] | None = None) -> HookMatcher:
    """Stop: a layer has not finished until its ARTIFACTS exist.

    "Write the delta script before you finish" was prompt text, and prompt text is what
    reads zero in this pipeline: `[unknown]` was used 0 times across four plan documents,
    `ask_supervisor` never fired, no plan carried a source URL. Every one of those was
    asked for and none was required. A Stop hook is the same instruction expressed as a
    condition the harness evaluates, so "finished" stops being the model's opinion of its
    own work.

    Blocking here returns the agent to work with the reason, rather than failing the layer
    — the session is still warm and the missing artifact is usually one Write away.
    """
    folder = Path(shot_folder)

    async def _check(inp, tool_use_id, ctx):
        if not script_rel:
            return {}
        # LIVE_BUILD must be allowed to end before the harness can switch the shared
        # phase to FINALIZE_SCRIPT. Requiring the script here while the phase guard
        # forbids writing it creates an infinite Stop/deny loop.
        if (phase or {}).get("mode", "finalize") == "live":
            return {}
        target = folder / script_rel
        if target.is_file() and target.stat().st_size > 0:
            return {}
        return {"decision": "block",
                "reason": (f"This layer has not published {script_rel}. The live scene is "
                           f"not the deliverable — the delta script that rebuilds it from "
                           f"empty is. Write it, then finish.")}
    return HookMatcher(matcher=None, hooks=[_check])


def failure_recorder(shot_folder: str | Path) -> HookMatcher:
    """PostToolUseFailure: keep a durable record of every tool call that raised.

    A failed run_bpy can leave the live scene half-mutated while the model's account of
    what happened lives only in a context window that compaction will discard. Appending
    to disk means a post-mortem can ask "what was Blender actually asked to do before it
    broke" without replaying the whole session.
    """
    folder = Path(shot_folder)

    async def _record(inp, tool_use_id, ctx):
        try:
            rec = {"at": datetime.now(UTC).isoformat(timespec="seconds"),
                   "tool": inp.get("tool_name"),
                   "error": str(inp.get("error") or inp.get("tool_response"))[:1500],
                   "input": json.dumps(inp.get("tool_input"))[:4000]}
            out = run_artifacts.logs_dir(folder) / "tool_failures.jsonl"
            out.parent.mkdir(parents=True, exist_ok=True)
            with out.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec) + "\n")
        except Exception as e:
            # A recorder that breaks the run is worse than no recorder — but a SILENT one
            # is how you later believe there were no failures because the log is empty.
            log(f"! could not record tool failure: {str(e)[:120]}", 1)
        return {}
    return HookMatcher(matcher=None, hooks=[_record])


def builder_phase_guard(phase: dict[str, Any], script_rel: str | None) -> HookMatcher:
    """Keep live search, first publication, and canonical repair from bleeding together.

    The old system prompt contained both "write the script once at finalize" and "Edit the
    script through script_map". Both were true, in different phases, but the tool surface
    never changed. This guard makes the mode header executable for the one artifact whose
    mutation matters: live work cannot publish it, and repair cannot replace it wholesale.
    """
    target = Path(script_rel).as_posix() if script_rel else ""

    async def _check(inp, tool_use_id, ctx) -> dict:
        tool = inp.get("tool_name", "") if isinstance(inp, dict) else getattr(inp, "tool_name", "")
        mode = phase.get("mode", "live")
        if (mode == "live" and phase.get("scene_contracts_passed")
                and tool.endswith("run_bpy")):
            bump("convergence_mutation_blocked")
            if phase.get("image_evidence_required") and not phase.get("pixel_contracts_passed"):
                reason = (
                    "AUTHORITATIVE SCENE CONTRACTS ALREADY PASS. Further speculative "
                    "geometry mutation is blocked. Call one FULL-FRAME compare_frame now "
                    "to evaluate this unit's explicitly bound image contracts."
                )
            else:
                reason = (
                    "ACTIVE UNIT CONTRACTS ALREADY PASS. Further speculative mutation is "
                    "blocked; this unit binds no unresolved image evidence. Finish required "
                    "read-only diagnostics and hand off without another beauty comparison."
                )
            return {"hookSpecificOutput": {
                "hookEventName": "PreToolUse", "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }}
        if not target:
            return {}
        if tool not in ("Write", "Edit"):
            return {}
        args = (inp.get("tool_input") if isinstance(inp, dict) else getattr(inp, "tool_input", {})) or {}
        raw = str(args.get("file_path") or args.get("path") or "").replace("\\", "/")
        if not (raw == target or raw.endswith("/" + target)):
            return {}
        reason = ""
        if mode == "live":
            reason = (
                f"MODE LIVE_BUILD: change the warm scene with run_bpy; do not write or edit "
                f"{target}. The harness will request FINALIZE_SCRIPT after the best scene "
                f"has been selected."
            )
        elif mode == "finalize" and tool == "Edit":
            reason = (
                f"MODE FINALIZE_SCRIPT: publish {target} once from the accepted run_bpy "
                f"journal with Write. Local Edit belongs to REPAIR_SCRIPT after canonical "
                f"replay identifies a defect."
            )
        elif mode == "repair" and tool == "Write":
            reason = (
                f"MODE REPAIR_SCRIPT: do not replace all of {target}. Use Grep, Read "
                f"the smallest span, then Edit that span."
            )
        if not reason:
            return {}
        bump("phase_write_blocked")
        log(f"⛔ {tool} blocked for {target} in {mode} mode", 1)
        return {"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }}

    return HookMatcher(matcher=None, hooks=[_check])


def execution_authority_guard(shot_folder: str | Path,
                              phase: dict[str, Any]) -> HookMatcher:
    """Keep evaluation ledgers from becoming live build instructions."""
    root = Path(shot_folder).resolve()
    runtime = (root / "runtime_checks.json").resolve()

    async def _check(inp, tool_use_id, ctx) -> dict:
        if phase.get("mode", "live") != "live":
            return {}
        tool = inp.get("tool_name", "") if isinstance(inp, dict) else getattr(inp, "tool_name", "")
        if tool not in {"Read", "Grep", "Edit", "Write"}:
            return {}
        args = (inp.get("tool_input") if isinstance(inp, dict) else getattr(inp, "tool_input", {})) or {}
        raw = str(args.get("file_path") or args.get("path") or "")
        if not raw and tool != "Grep":
            return {}
        candidate = Path(raw or ".")
        candidate = (candidate if candidate.is_absolute() else root / candidate).resolve()
        exposes_runtime = candidate == runtime
        if tool == "Grep":
            # A recursive grep of the shot root is also a read of runtime_checks.json.
            exposes_runtime = runtime.is_relative_to(candidate)
        if not exposes_runtime:
            return {}
        bump("runtime_authority_blocked")
        return {"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "deny",
            "permissionDecisionReason":
                "runtime_checks.json is evaluation-only evidence from earlier attempts; "
                "it is not live execution authority and may contain stale observations. "
                "Use the current layer plan, scene_checks contracts, check_scene, and the "
                "current render instead. propose_checks is the only supported writer."
        }}

    return HookMatcher(matcher=None, hooks=[_check])


def builder_hooks(shot_folder: str | Path, roots: list, ref_rel: str | None = None,
                  script_rel: str | None = None,
                  phase: dict[str, Any] | None = None) -> dict:
    """PreToolUse: path sandbox + API guardrails. PostToolUse: metric feedback.
    PostToolUseFailure: durable failure log. Stop: the artifacts must exist."""
    from vfx_harness.infrastructure.sandbox import path_sandbox
    active_phase = phase or {"mode": "live"}
    return {
        "PreToolUse": [path_sandbox(*roots, cwd=shot_folder), api_guardrails(),
                       script_sanity(), web_allowlist(),
                       execution_authority_guard(shot_folder, active_phase),
                       builder_phase_guard(active_phase, script_rel)],
        "PostToolUse": [metrics_feedback(
            shot_folder, ref_rel,
            look_actions=bool(active_phase.get("look_actions", True)))],
        "PostToolUseFailure": [failure_recorder(shot_folder)],
        "Stop": [completion_gate(shot_folder, script_rel, phase)],
        "PreCompact": [compaction_notice(shot_folder)],
    }
