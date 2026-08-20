"""Write the per-shot CLAUDE.md that survives compaction.

The kickoff carries the complete execution ticket, while this file carries the small set
of durable pointers and invariants needed to resume safely after compaction.  It is not a
second copy of the plan: duplicated plans drift, and a stale monolithic plan was the
actual source of Layer 1 scope drift in the observed run.

The SDK docs are explicit about the remedy: "Persistent rules belong in CLAUDE.md (loaded
via settingSources) rather than in the initial prompt, because CLAUDE.md content is
re-injected on every request." The compactor also reads CLAUDE.md, so a summary-
instructions section steers what it keeps.

This writes a short, durable file per layer run: the layer's contract (owned axes, every
frame it answers for, measured targets) plus the rules that must never be summarised out.
"""

from __future__ import annotations

from pathlib import Path

from .brief import Shot
from .escalate import answers_block, open_block
from .layer_plans import layer_plan_path

_HEADER = "<!-- generated per layer run by bambi_vfx.shot_context — safe to overwrite -->"


def write_layer_context(shot: Shot, layer, axes: list[tuple[str, str]],
                       fingerprints: dict[int, str] | None = None) -> Path:
    """Write shots/<id>/CLAUDE.md for this layer. Returns the path."""
    fingerprints = fingerprints or {}
    list(layer.owns) or ["(not declared — judge on the layer's scope)"]
    judge_rows = "\n".join(
        f"- **f{f}** vs `{r}`" + (f" — target: {fingerprints[f]}" if f in fingerprints else "")
        for f, r in layer.judges)
    axis_rows = "\n".join(f"- `{k}` — {d}" for k, d in axes if k in layer.owns) or \
                "\n".join(f"- `{k}` — {d}" for k, d in axes)

    sup = "\n\n".join(x for x in (answers_block(shot.folder), open_block(shot.folder)) if x)
    supervisor = (sup + "\n\n") if sup else ""
    # Carry forward what a PREVIOUS attempt at this layer already established — a resume
    # after a crash, or a rebuild triggered by acceptance repair, otherwise starts by
    # re-deriving (and often re-trying) approaches that were already measured and rejected.
    # Written only here, at layer start: CLAUDE.md sits in the cached prompt prefix, so
    # rewriting it every round would invalidate the cache and cost more than it saves.
    prior_state = ""
    try:
        from .layer_state import as_prompt_block
        prior_state = as_prompt_block(shot.folder)
    except Exception as e:                       # never block a build on context assembly
        print(f"! prior layer state unavailable: {e}", flush=True)
    if prior_state:
        supervisor = prior_state + "\n" + supervisor
    try:
        from .layer_plans import amendment_block, prior_outcomes_block
        hierarchical = "\n\n".join(
            block for block in (
                amendment_block(shot.folder, str(layer.id)),
                prior_outcomes_block(shot.folder, str(layer.id)),
            ) if block
        )
        if hierarchical:
            supervisor = hierarchical + "\n\n" + supervisor
    except Exception as e:
        raise RuntimeError(f"hierarchical plan feedback is invalid: {e}") from e
    plan_path = layer_plan_path(shot.folder, layer)
    plan_rel = plan_path.relative_to(shot.folder).as_posix()
    body = f"""{_HEADER}
# Layer {layer.id} — {layer.title}

You are building ONE layer of shot `{shot.id}` ({shot.frames}f @ {shot.fps}fps).
This file is re-injected on every request: if the conversation is summarised, THESE
facts remain true and authoritative.

## Current mode and source authority
- MODE is `LIVE_BUILD`. Change the warm Blender scene only through `run_bpy`.
- The only execution plan is `{plan_rel}`. Re-read that exact file after any context
  reset. `plans/global.md` is dependency context only, never an execution checklist.
- Shot-root `plan.md` is an unsupported legacy artifact and must never be read or treated
  as authority. There is no monolithic-plan fallback.
- Do not call `Write` or `Edit` in this mode. The harness journals successful `run_bpy`
  mutations and later starts a separate `FINALIZE_SCRIPT` session to publish `{layer.script}`.

## What this layer must deliver
{layer.reads}

Delta script: `{layer.script}` — reproduce only THIS layer's changes; earlier layer scripts
run before yours and their objects already exist.

## Frames you answer for
The finished script is rendered and scored at EVERY frame below and passes only if all
of them clear. A change that fixes one and breaks another is not a fix.

{judge_rows}

## Axes you are scored on
Only these are sent to the layer critic, and every one is scored numerically. Elements
that are correctly absent because another layer adds or removes them must not depress
these axes.

{axis_rows}

## Rules that do not change
- Use the `bvfx_*` helpers over raw bpy where one exists. Hand-rolled equivalents use
  Blender-4 APIs that fail on 5.2, and a PreToolUse guardrail will block the known ones.
- `find_recipe` before inventing a technique; the cookbook has verified, measured code.
- Build LIVE in the session and verify with renders. Script publication is a later harness
  phase with a different tool surface; do not try to anticipate or perform it here.
- Objective metrics beat opinion. If a render's measured gap to the reference is reported
  to you, treat it as fact and fix the number.
- Scene contracts prefer semantic roles over object names. Tag owned objects with
  `bvfx_role(obj, "department.subject.part", owner_layer="{layer.id}")`; names are labels,
  while `bvfx_role` is the stable interface that survives renames.

{supervisor}## Summary instructions
When summarising this conversation, ALWAYS preserve:
- MODE `LIVE_BUILD`, authoritative plan `{plan_rel}`, layer id, owned axes, and every
  judge frame listed above
- semantic `bvfx_role` values and material interfaces created so far
- measured values already converged on, and values already ruled out with their measurement
- which of the judge frames currently pass and which do not
"""
    path = shot.folder / "CLAUDE.md"
    path.write_text(body, encoding="utf-8")
    return path


def clear_layer_context(shot: Shot) -> None:
    """Remove a generated CLAUDE.md (never delete a hand-written one)."""
    p = shot.folder / "CLAUDE.md"
    if p.is_file() and p.read_text(encoding="utf-8").startswith(_HEADER):
        p.unlink()
