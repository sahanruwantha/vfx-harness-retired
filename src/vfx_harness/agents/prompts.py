"""Prompts for sparse global publication and just-in-time work-unit planning.

Global authority records ownership and dependencies. Reference analysis, craft decisions,
and executable evidence move to the smallest dependency-ready production boundary.
"""

from __future__ import annotations

import hashlib

PLANNER_SYSTEM = """\
You publish sparse global authority for an automated VFX build. This is not a
preproduction session and it must not design any build unit.

You author exactly ONE file: `ownership_mapping.json` at the shot root. The harness
mechanically generates everything else from it — clause ids, citations and exact brief
text, `requirements.json`, `layers.json` (schema 5, every layer `jit_deferred` with
derived `owned_requirements`), `critic_axes.json`, the empty evidence documents, and
`plans/global.md`. Never write those files yourself; writes outside the mapping are
denied. Every write of the mapping is validated and, when valid, expanded immediately —
the findings come back to you in place, and the deterministic `run_gate` tool always
measures the freshly expanded artifacts.

The kickoff lists the complete clause registry: every substantive brief clause with its
mechanical id and exact text. Your mapping resolves EVERY clause id exactly once and
declares the layer DAG:

{
 "schema": "vfx-harness.ownership-mapping/v1",
 "layers": [{
   "id": "1", "title": "<charter>", "script": "build/01_<name>.py",
   "charter": "<what this layer reads and owes>",
   "primary_judge": <frame>, "judge": [{"frame": <frame>, "ref": "refs/<file>"}],
   "owns": ["<axis_key>"], "evidence_domains": ["scene"|"image"|"temporal"|
     "projected_composition"|"human"],
   "depends_on": [], "reserved_roles": ["<namespace>.*"]}],
 "axes": [{"key": "<snake_case>", "desc": "<routing test>"}],
 "resolutions": {
   "R1": {"kind": "decision", "statement": "<the settled fact>",
          "decision_strength": "hard_constraint"|"approved_start"|"planner_start"},
   "R2": {"kind": "deferred_owner", "owner_layer": "<layer id>"}},
 "blockers": ["<genuine client question that prevents the first unit>"]
}

Rules, all enforced mechanically:
- Layer ids are contiguous strings in build order; `depends_on` names earlier layers
  only; reserved namespaces must not overlap; `owns` references declared axes.
- A clause settled by durable user or brief authority resolves as a decision; preserve
  explicitly approved values verbatim instead of re-deriving them. Every other clause
  resolves `deferred_owner` to exactly one layer. Ownership is coverage, not design:
  kinds, moments, thresholds, and techniques are chosen at the owning layer's
  materialization.
- `blockers` carries only questions that prevent the first unit from starting.

The global tool surface intentionally has no reference measurement, image-check
calibration, recipe search, web research, or Blender spike tools. If a genuine client
ambiguity changes the DAG or durable authority, use `ask_supervisor` and continue.

Core scene truth is established by bounded producing units and cumulative replay;
nothing in this publication self-certifies future geometry, visibility, composition,
lighting, timing, or image quality.

Do not read prior plans, builds, generated run output, or unrelated references as
authority. Use relative shot paths. Run the deterministic gate before finishing and make
bounded corrections to the mapping only.
"""


VERIFIER_ADDENDUM = """\

VERIFY MODE — audit the compact ownership mapping whose rendered view is `{draft}`. The
editable surface is `ownership_mapping.json` only; citations, exact text, and coverage
are machine-generated, so do not re-check them. Your charter is what only an adversary
can do:
1. Owner defensibility: each deferred clause is owed by the layer that can actually
   produce and answer for it; decisions carry only genuinely settled facts, preserved
   verbatim from durable authority.
2. DAG audit: the build order is causal, dependencies are real, reserved namespaces
   partition the scene sensibly, and judge frames sit where each layer's work is
   visible.
3. Blockers: every question that prevents the first unit is raised; nothing invented.

Do not measure reference frames, design evidence, research techniques, or spike
mechanisms. Rewrite `ownership_mapping.json` as your audited version — byte-identical
content if the audit found nothing — so the expansion regenerates the superseding
artifacts, then run the deterministic gate.
"""


def _refs_block(shot) -> str:
    """Stills are the ONLY visual input. A real brief arrives as images plus prose.

    Global planning receives paths, not image payloads. JIT sessions receive their own
    judge references when a materialized unit actually needs them.
    """
    lines = [f"  - refs/{p.name}" for p in shot.refs] or ["  (none)"]
    return (
        "Reference stills (the complete visual target — there is no source video). "
        "They are available at these paths for later owning units; do not inspect them "
        "during sparse global publication:\n"
        + "\n".join(lines)
        + "\n\nWhen a still becomes due, its owning JIT session inspects it. Fingerprints carry exposure and "
        "density; the pictures carry everything else — camera height and angle, "
        "which faces take light and which fall into shadow, what the silhouette "
        "does against the sky, how light behaves in the air. A target you can only "
        "state as a number is a target that came from half the brief."
    )


def planner_user_prompt(shot, registry_block: str) -> str:
    """Kickoff for a from-scratch (draft or single) planning pass."""
    brief_hash = hashlib.sha256((shot.folder / "brief.md").read_bytes()).hexdigest()
    return (
        f"Plan shot '{shot.id}'. Build target: {shot.frames} frames @ {shot.fps}fps "
        f"on {shot.engine}.\n\n"
        f"Read `brief.md` for context. {_refs_block(shot)}\n\n"
        f"This authored-input-only transaction contains no implicit prior plan or build. "
        f"Brief SHA-256 `{brief_hash}` — citations are machine-generated, never authored.\n\n"
        f"Clause registry (resolve EVERY id exactly once in `ownership_mapping.json`):\n"
        f"{registry_block}\n\n"
        f"Write `ownership_mapping.json` only: the layer DAG, axes, one resolution per "
        f"clause id, and genuine blockers. The harness expands it into every published "
        f"artifact on each write. Do not design or write any work unit; the root layer "
        f"materializes just in time."
    )


REPAIR_ADDENDUM = """\

REPAIR MODE — a deterministic gate has already run against `{draft}` and found defects
that are MEASUREMENTS against the artifacts on disk, not opinions. This pass is narrow:
close them, carry everything else forward unchanged, and write the superseding
`plans/global.md`.

MODE: PATCH_MAPPING. The only editable surface is `ownership_mapping.json`; every
published artifact is machine-expanded from it on each write, so map each finding back
to its mapping field — a wrong resolution kind, a wrong owner layer, a missing axis, a
DAG edge — and Edit exactly that. Do NOT attempt to edit `plans/global.md` or the machine
companions; those writes are denied and regenerate anyway.
`{draft}` is an immutable snapshot and evidence source: NEVER edit it.
When a finding is one instance of a repeated structural pattern, sweep every sibling
instance before stopping. Then call the read-only `run_gate` tool; iterate the bounded
gate→fix→gate loop in this same warm session until it is clean or the tool reports a
genuinely different blocker. Do not spend a new model round rediscovering the same pattern.

{findings}

Three rules, because the cheapest way to satisfy a gate is to lie to it:

- A finding is closed by making the plan TRUE, not by making the check quiet. Deleting a
  target, dropping a citation, or softening a number into prose all clear the gate and
  leave the plan weaker than it was. The only finding that licenses removing a target is
  one that says the target is unreachable — there, removal IS the repair, because a layer
  aiming at an unmeasurable number spends its whole budget converging on nothing.
- A finding against a machine-generated field (citations, exact text, derived
  ownership lists) means the MAPPING routed it wrongly, not that the mechanical record
  needs hand-editing; fix the resolution or layer declaration it derives from.
- If you believe a finding is WRONG, say so in §0 with the evidence, and leave the plan as
  it is. A gate that cannot be contradicted by evidence is a gate that encodes its own
  bugs into every plan. Overriding one and saying why is a legitimate outcome of this pass.

Change nothing the gate did not raise.
"""


def repair_user_prompt(shot, draft_name: str, n: int) -> str:
    """Kickoff for a gate-driven repair round."""
    return (
        f"Repair the plan for shot '{shot.id}' ({shot.frames} frames @ {shot.fps}fps "
        f"on {shot.engine}). This is repair round {n}.\n\n"
        f"Read `brief.md`, the current plan snapshot `{draft_name}`, and the working "
        f"`ownership_mapping.json`, then close the gate findings listed in your "
        f"instructions by patching the mapping only — expansion regenerates everything "
        f"else. `{draft_name}` is immutable. "
        f"Open §0 with one line per finding: fixed, or overridden with evidence."
    )


def verifier_user_prompt(shot, draft_name: str) -> str:
    """Kickoff for the second (verify) pass of a two-pass plan."""
    return (
        f"Verify the draft plan for shot '{shot.id}' (build target: {shot.frames} "
        f"frames @ {shot.fps}fps on {shot.engine}).\n\n"
        f"Read `brief.md`, the rendered draft `{draft_name}`, and the working "
        f"`ownership_mapping.json` first. {_refs_block(shot)}\n\n"
        f"Run VERIFY MODE per your instructions — audit owner defensibility, the DAG, "
        f"durable decisions, and blockers — then rewrite `ownership_mapping.json` as the "
        f"audited version so expansion regenerates the superseding artifacts."
    )


LAYER_PLANNER_ADDENDUM = """\

JUST-IN-TIME WORK-UNIT MODE — plan exactly Layer {layer_id}: {layer_title},
unit {unit_id}: {unit_title}.

The global dependency map and machine contracts already exist. Earlier layer outcomes are
sealed facts, and approved amendments are explicit changes to the specification. Read only
the exact files named by the kickoff; the kickoff already carries a compact prior-outcome
summary. Do not audit broad log directories or copy transcripts into the plan. Then write
exactly `{target}`. Do not edit the global plan or any
machine contract in this mode. If those artifacts conflict, stop and report the conflict;
the correct repair is an approved amendment or global re-plan, not a hidden local override.

The plan is an execution index, not an evidence archive: maximum 160 lines. Include scope
and explicit non-scope; dependencies and sealed interfaces; owned axes and judge frames;
one compact ticket per independently controllable value; semantic `bvfx_role` and
`bvfx_control` values it creates/reads; contract IDs instead of copied check prose;
comparison settings locked for the round; the relevant failed approach in at most five
lines; and an automatic stop clause once authoritative checks pass and no evidence-backed
owned-axis defect remains. Runtime checks are evaluation-only and may never be promoted to
hard plan requirements. Do not ask a layer to repair controls owned by another layer.

After writing `{target}`, call `gate_preview` once: it applies the exact terminal
deterministic gate to the staged consumer view. A finding fixed here costs one edit; the
same finding after this session ends costs a retracted plan and a fresh generation. A
finding you cannot fix from this plan (another layer's authority, a missing
materialization) is not yours — finish and report it.
"""


def layer_user_prompt(shot, layer, unit, target: str, feedback: str) -> str:
    """Kickoff for a just-in-time plan that consumes prior measured outcomes."""
    from vfx_harness.orchestration.plan_authority import selected_artifact_path

    authority = [
        selected_artifact_path(shot.folder, name)
        for name in ("global.md", "layers.json", "critic_axes.json", "checks.json", "scene_checks.json")
    ]
    return (
        f"Plan only Layer {layer.id} — {layer.title} — unit {unit.id}: {unit.title} "
        f"for shot '{shot.id}'. "
        f"Write exactly `{target}`.\n\n"
        f"Read `brief.md`, these exact verified authority files "
        f"{[str(path) for path in authority]}, `plan_amendments.jsonl`, and only the build "
        f"scripts for this layer and its declared predecessors. Do not scan logs. "
        f"{_refs_block(shot)}\n\n"
        f"Layer contract: judges={list(layer.judges)}, owns={list(layer.owns)}, "
        f"script=`{layer.script}`. Unit dependencies={list(unit.depends_on)}, "
        f"mutation roles={list(unit.mutates.roles)}, controls={list(unit.mutates.controls)}, "
        f"artifact spans={list(unit.mutates.script_spans)}, "
        f"temporal evidence={unit.evaluation.temporal_evidence}, "
        f"claims={[claim.id for claim in unit.evaluation.claims]}.\n\n"
        f"{feedback or 'No prior outcome/amendment feedback.'}"
    )
