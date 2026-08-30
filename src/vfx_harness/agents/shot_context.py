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

from vfx_harness.agents.unit_scope import compile_scope_with_predecessors, format_unit_scope_card, helper_inventory
from vfx_harness.domain.brief import Shot
from vfx_harness.evidence.scene_checks import load_rows
from vfx_harness.orchestration.escalate import answers_block, open_block
from vfx_harness.orchestration.layer_plans import amendment_block, prior_outcomes_block, work_unit_plan_path
from vfx_harness.orchestration.layer_state import as_prompt_block
from vfx_harness.orchestration.unit_state import load as load_unit_state

_HEADER = "<!-- generated per layer run by vfx_harness.agents.shot_context — safe to overwrite -->"


def write_layer_context(
    shot: Shot,
    layer,
    axes: list[tuple[str, str]],
    fingerprints: dict[int, str] | None = None,
    *,
    unit=None,
    layer_units=None,
) -> Path:
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
        prior_state = as_prompt_block(shot.folder)
    except Exception as e:                       # never block a build on context assembly
        print(f"! prior layer state unavailable: {e}", flush=True)
    if prior_state:
        supervisor = prior_state + "\n" + supervisor
    try:
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
    if unit is None:
        if len(layer.stages) != 1:
            raise ValueError(
                f"layer {layer.id} declares {len(layer.stages)} units; context generation "
                "requires the active unit explicitly"
            )
        unit = layer.stages[0]
    plan_path = work_unit_plan_path(shot.folder, unit)
    plan_rel = plan_path.relative_to(shot.folder).as_posix()
    dependency_rows = ", ".join(unit.depends_on) or "none"
    mutation_rows = "\n".join(
        (
            f"- semantic roles: {', '.join(unit.mutates.roles) or 'none'}",
            f"- semantic controls: {', '.join(unit.mutates.controls) or 'none'}",
            f"- artifact spans: {', '.join(unit.mutates.script_spans) or 'none'}",
        )
    )
    claim_rows = "\n".join(
        f"- `{claim.id}` ({claim.authority}, repair `{claim.repair_owner}`): {claim.proposition}"
        for claim in unit.evaluation.claims
    )
    try:
        contracts = load_rows(shot.folder)
    except (OSError, ValueError):
        contracts = []
    try:
        durable_state: dict = {}
        try:
            durable_state = load_unit_state(shot.folder, str(layer.id))
        except (OSError, ValueError):
            durable_state = {}
        scope_card = format_unit_scope_card(
            compile_scope_with_predecessors(
                unit=unit,
                layer_id=str(layer.id),
                contracts=contracts,
                helpers=helper_inventory(),
                units=tuple(layer_units or getattr(layer, "stages", ()) or (unit,)),
                durable_state=durable_state,
            )
        )
    except ValueError as exc:
        raise RuntimeError(f"unit scope card is invalid: {exc}") from exc
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

## Active work unit — {unit.id}: {unit.title}
- Dependencies: {dependency_rows}
- Explicit primary frame: f{unit.evaluation.primary_judge}
- Temporal evidence policy: `{unit.evaluation.temporal_evidence}`
- Completion: `{unit.completion}`

Permitted mutation surface:
{mutation_rows}

Required and advisory claims:
{claim_rows}

## Compiled unit scope
{scope_card}

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
  `bvfx_role(obj, "department.subject.part", owner_layer="{layer.id}")` — one dotted token
  per host, commas are not membership. Names are labels, while `bvfx_role` is the stable
  interface that survives renames. inspect_scene / check_scene / list_keyframes take `role=`.

{supervisor}## Summary instructions
When summarising this conversation, ALWAYS preserve:
- MODE `LIVE_BUILD`, authoritative plan `{plan_rel}`, layer id, owned axes, and every
  judge frame listed above; active unit `{unit.id}` and its mutation surface
- the compiled unit-scope card (roles, bound contracts, helpers); query `unit_scope`
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
