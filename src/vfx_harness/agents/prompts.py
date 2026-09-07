"""Prompts for sparse global publication and just-in-time work-unit planning.

Global authority records ownership and dependencies. Reference analysis, craft decisions,
and executable evidence move to the smallest dependency-ready production boundary.
"""

from __future__ import annotations

import json


def _refs_block(shot) -> str:
    """Stills staged in this workspace are the visual target for global publication.

    Global planning receives paths, not image payloads. JIT sessions receive their own
    judge references when a materialized unit actually needs them. Video tools register
    only for clips inside ``refs/``; a clip outside this workspace is not planning input.
    """
    lines = [f"  - refs/{p.name}" for p in shot.refs] or ["  (none)"]
    return (
        "This workspace stages reference stills as the visual target. Video measurement "
        "tools register only for clips inside refs/; a clip outside this workspace is "
        "not planning input. Stills are available at these paths for later owning units; "
        "do not inspect them during sparse global publication:\n"
        + "\n".join(lines)
        + "\n\nWhen a still becomes due, its owning JIT session inspects it. Fingerprints carry exposure and "
        "density; the pictures carry everything else — camera height and angle, "
        "which faces take light and which fall into shadow, what the silhouette "
        "does against the sky, how light behaves in the air. A target you can only "
        "state as a number is a target that came from half the brief."
    )










LAYER_PLANNER_ADDENDUM = """\

JUST-IN-TIME WORK-UNIT MODE — plan exactly Layer {layer_id}: {layer_title},
unit {unit_id}: {unit_title}.

The global dependency map and machine contracts already exist. Earlier layer outcomes are
sealed facts, and approved amendments are explicit changes to the specification. The kickoff
is the complete compiled authority card for this unit; there is no raw Read surface. Do not
audit broad files or copy transcripts into the plan. Publish the finished content through
`publish_unit_plan`; the harness fixes its only target to `{target}`. Do not edit the global plan or any
machine contract in this mode. If those artifacts conflict, stop and report the conflict;
the correct repair is an approved amendment or global re-plan, not a hidden local override.

The plan is an execution index, not an evidence archive: maximum 160 lines. Include scope
and explicit non-scope; dependencies and sealed interfaces; owned axes and judge frames;
one compact ticket per independently controllable value; semantic `bvfx_role` and
`bvfx_control` values it creates/reads; contract IDs instead of copied check prose;
comparison settings locked for the round; the relevant failed approach in at most five
lines; and an automatic stop clause once authoritative checks pass and no evidence-backed
owned-axis defect remains. Runtime check thresholds are evaluation-only and may never be promoted to
hard plan requirements. Executable `image_contract` ids on required look
claims ARE the stop clause: name those exact ids as builder-owed
`propose_checks` work (id, frame, property kind, axis). Do not describe them
as critic-only judge comparison. Do not ask a layer to repair controls owned
by another layer.

After publishing `{target}`, call `gate_preview` once: it applies the exact terminal
deterministic gate to the staged consumer view. A finding fixed here costs one edit; the
same finding after this session ends costs a retracted plan and a fresh generation. A
finding you cannot fix from this plan (another layer's authority, a missing
materialization) is not yours — finish and report it.
"""


def layer_user_prompt(
    shot,
    layer,
    unit,
    target: str,
    feedback: str,
    *,
    unit_card: dict,
    predecessor_cards: list[dict],
) -> str:
    """Kickoff for a just-in-time plan that consumes prior measured outcomes."""
    return (
        f"Plan only Layer {layer.id} — {layer.title} — unit {unit.id}: {unit.title} "
        f"for shot '{shot.id}'. "
        f"Publish through `publish_unit_plan`; its fixed target is `{target}`.\n\n"
        f"This is the complete compiled unit authority. Do not read the brief, layer/contract "
        f"catalogs, scripts, amendments, decisions, or prior plans. "
        f"{_refs_block(shot)}\n\n"
        f"Layer contract: judges={list(layer.judges)}, owns={list(layer.owns)}, "
        f"script=`{layer.script}`.\n"
        f"Active unit card (exact):\n{json.dumps(unit_card, indent=1)}\n\n"
        f"Passed predecessor interfaces (compiled exports, sealed ids, and typed "
        f"publish_interfaces only; no producer scripts or catalogs):\n"
        f"{json.dumps(predecessor_cards, indent=1)}\n\n"
        f"{feedback or 'No prior outcome/amendment feedback.'}"
    )
