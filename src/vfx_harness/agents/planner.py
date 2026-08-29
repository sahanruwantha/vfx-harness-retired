"""Stage 2 — the PLAN harness.

Standard flow is TWO-PASS, an A/B-tested division of labour:

  pass 1  DRAFT   (default claude-sonnet-5) — from-scratch forensics: deep scene
          read, research, spikes. Empirically the stronger cold-start discoverer.
  pass 2  VERIFY  (default claude-sonnet-5) — adversarial audit of the draft:
          frame claims re-derived, still↔source twins metric-matched, spike
          citations evidence-checked, gaps measured, missed prior work salvaged.
          Empirically the stronger reviewer. Writes the superseding plans/global.md.

  loop    REPAIR (--until-clean)              — the two passes are both model passes,
          so "solid" was the model's own opinion of its own work. --until-clean runs
          a DETERMINISTIC gate (eval.plan_gate) over the artifacts on disk after the
          verify pass and feeds its findings back as a narrow repair brief, until the
          gate clears, stops moving, or hits --max-rounds.

Why the loop is gated on a machine and not on another critique pass: every mechanism
built to make the planner self-correcting is self-certified, and measured across both
shots all of them read zero. `[unknown]` — which is what gates web research — was used
0 times in all four plan documents. `ask_supervisor` never fired. No plan contains a
single source URL, though step 6 requires them. The verify pass, whose instruction #2
is "measure-check every approval still", shipped 7 fingerprints that do not reproduce.
Meanwhile spike citations are precise and TRUE. The difference is that `spike` writes a
file to the lab and `WebSearch` writes nothing: evidence a tool physically deposits
survives, evidence the model is merely asked to record does not.

The draft is kept alongside (`plans/global.draft.md` + its lab dir) as the audit trail,
and each repair round snapshots its input under the active run's
`checkpoints/plans/snapshots/` directory.
`--single` runs one from-scratch pass (the pre-two-pass behavior);
`--verify-only` skips pass 1 and audits an existing draft.

Both passes run with the same tool surface the build harness deliberately lacks:
still forensics (measure_ref), a Blender spike lab, the cookbook, the open
web, and the one-shot headless Blender spike lab.

Usage:
    python -m vfx_harness.agents.planner <shot-folder>                     # two-pass
    python -m vfx_harness.agents.planner <shot-folder> --until-clean       # + repair loop
    python -m vfx_harness.agents.planner <shot-folder> --single [--model M]
    python -m vfx_harness.agents.planner <shot-folder> --verify-only
    common flags: [--draft-model M] [--verify-model M] [--blender BIN]
                  [--max-turns N] [--tag T] [--max-rounds N]
    python -m vfx_harness.agents.planner <shot-folder> --layer 2  # JIT layer plan

    vfx evals plan <shot-folder>        # run the gate alone — free, no model
    vfx evals plan --feedback           # the repair brief a round would receive
"""

from __future__ import annotations

import argparse
import json
import shutil
from dataclasses import dataclass
from pathlib import Path

import anyio
from claude_agent_sdk import ClaudeAgentOptions, query

from vfx_harness.agents.plan_guardrails import planner_hooks
from vfx_harness.agents.plan_tools import build_plan_tools
from vfx_harness.agents.prompts import (
    LAYER_PLANNER_ADDENDUM,
    PLANNER_SYSTEM,
    REPAIR_ADDENDUM,
    VERIFIER_ADDENDUM,
    layer_user_prompt,
    planner_user_prompt,
    repair_user_prompt,
    verifier_user_prompt,
)
from vfx_harness.agents.resilience import AgentSessionFailure, result_signal, run_session
from vfx_harness.domain.brief import load_shot
from vfx_harness.infrastructure.config import DEFAULT_EXECUTION_MODEL, Settings
from vfx_harness.knowledge.recipes import build_recipe_tools
from vfx_harness.observability import costlog, run_artifacts, transcript
from vfx_harness.observability.log import log, log_message
from vfx_harness.orchestration.layer_plans import (
    amendment_block,
    contract_gaps_block,
    global_plan_path,
    is_selected_bundle_member,
    prior_outcomes_block,
    stamp_work_unit_plan,
    validate_work_unit_plan_authority,
    work_unit_plan_authority_path,
    work_unit_plan_path,
)
from vfx_harness.orchestration.ledger import load_layers, load_layers_from_path

from .builder import _one_user_message

# Materialization authority is the selected bundle, the decision ledger, sealed
# outcomes, and the candidate file. Glob/Grep of historical bundles is not a repair
# instrument; Edit is JSON text-edit of the wrong document (HIR-0023). Task/Agent
# are not remat repair instruments: a spawned Explore burned remat4 reading denied
# paths, then the process died on a broken pipe (HIR-0026).
MATERIALIZATION_DENIED_TOOLS = [
    "Bash",
    "Edit",
    "Glob",
    "Grep",
    "Task",
    "Agent",
    "ListAgents",
    "ScheduleWakeup",
]


def mapping_expander(workspace: Path, registry, mapping_path: Path):
    """The warm authoring loop: each mapping write is validated with enumerated errors
    and, when valid, expanded into the full authority surface immediately — so the
    session's `run_gate` always measures fresh artifacts and `plans/global.md` exists
    exactly when the mapping is publishable."""

    def _expand_or_errors() -> list[str]:
        from vfx_harness.orchestration.plan_authoring import (
            expand_mapping,
            validate_mapping,
        )

        try:
            mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return [f"ownership_mapping.json is not readable JSON: {exc}"]
        errors = validate_mapping(mapping, registry, workspace / "refs")
        if errors:
            return errors
        try:
            expand_mapping(workspace, mapping)
        except (ValueError, OSError) as exc:
            return [str(exc)]
        return []

    return _expand_or_errors


def _with_target_feedback(hooks: dict, target: Path, validate) -> dict:
    """Append warm write-time validation of one target file to a planner hook set."""
    from vfx_harness.agents.plan_guardrails import target_validation_feedback

    hooks = dict(hooks)
    hooks["PostToolUse"] = [
        *hooks.get("PostToolUse", []),
        target_validation_feedback(target, validate),
    ]
    return hooks


# The unit-ticket tool exposes the closed WorkUnit schema. This remains only the outer
# materialization document shape — every value is a placeholder, no shot vocabulary.
_MATERIALIZATION_EXAMPLE = """{
 "schema": "<materialization schema id from your instructions>",
 "bundle_hash": "<selected bundle hash>",
 "layer": {
  "<structural fields copied verbatim from your global row>": "...",
  "execution": "ready",
  "stages": [{
   "id": "example_unit", "title": "Example unit", "plan": "plans/units/example_unit.md",
   "depends_on": [],
   "mutates": {"mode": "scoped", "roles": ["example_role.part"], "controls": ["example_control"],
               "control_roles": {"example_control": ["example_role.part"]},
               "script_spans": ["build/units/01/example_unit.py"]},
   "protects": {"selector": "all_active_upstream_interfaces",
                "resolve_to_explicit_ids_at": "freeze"},
   "look_capabilities": ["<the appearance families THIS unit answers for>"],
   "provides": ["<capabilities this unit gives the scene: camera, geometry>"],
   "evaluation": {"primary_judge": 1, "judge": [{"frame": 1, "ref": "refs/<a judge ref>.png"}],
                  "temporal_evidence": "none",
                  "claims": [{
                   "id": "example-claim", "proposition": "one testable sentence",
                   "axis": "<an axis this layer owns>", "property": "<contract kind>",
                   "subject_roles": ["example_role.part"], "subject_controls": [],
                   "moments": [1], "kind": "atomic", "required": true,
                   "authority": "executable_required", "repair_owner": "example_unit",
                   "asserts": "<scene|temporal|projected_composition|image|human>",
                   "evidence": [{"kind": "scene_contract", "id": "example-contract"}]}]},
   "completion": "all_required_claims_and_protected_contracts_pass"}]
 },
 "scene_contracts": [{
  "id": "example-contract", "kind": "<contract kind>", "owner_layer": "<this layer id>",
  "fault_owner": "<this layer id>", "activates_at": "<this layer id>", "lifecycle": "layer",
  "axis": "<an owned axis>", "op": "max", "hi": 0.01}],
 "image_contracts": [],
 "requirement_bindings": [
  {"requirement_id": "<owned id>", "contract_ids": ["example-contract"]},
  {"requirement_id": "<owned id>", "decision": {"statement": "one-sentence closure",
    "decision_strength": "approved_start"}}],
 "acceptance": []
}"""

_PUBLISH_CONSUME_EXAMPLE = """Typed predecessor handoff (exact field names):
producer `publishes`: [{"id":"target.publish","kind":"placement_control",
"schema":"vfx-harness.publish-interface/v1",
"exports":{"role":"target.role","control":"target.control"}}]
successor `depends_on`: ["target"]
successor `consumes`: [{"producer":"target","interface_id":"target.publish",
"kind":"placement_control"}]
The successor observes exported selectors read-only; it does not add them to `mutates`.
Omit `composition_context` when required claims directly bind the evidence. If present,
it needs non-empty `frames` and exactly one of `source_unit` or non-empty `contract_ids`.
"""


def _binding_decisions_block(shot_folder: Path, layer, bundle_hash: str) -> str:
    """Compile the structured decisions this layer must copy — not the whole ledger.

    Remat5 copied a prior generation's A2 spine because the prompt said to scan
    ``state/plan-resolutions.jsonl``. The selected bundle is the key; other
    generations are inert (HIR-0028).
    """
    from vfx_harness.domain.plan_records import (
        load_active_structured_decisions,
        roles_match_reserved,
    )

    reserved = tuple(
        str(pattern)
        for pattern in (getattr(getattr(layer, "jit", None), "reserved_roles", None) or [])
    )
    active = load_active_structured_decisions(
        shot_folder / "state" / "plan-resolutions.jsonl",
        bundle_hash=bundle_hash,
    )
    binding = [
        {
            "id": decision.id,
            "decision": decision.decision,
            "values": {"contract": decision.contract},
        }
        for decision in active.values()
        if roles_match_reserved(
            [str(role) for role in (decision.contract.get("roles") or [])],
            reserved,
        )
    ]
    return (
        "Binding structured decisions for this layer on the selected bundle "
        "(copy each verbatim into scene_contracts with decision_id; ledger rows "
        "keyed to another generation, or retired by a later superseded/falsified "
        f"row, are inert):\n{json.dumps(binding, indent=1)}\n"
    )


_TWO_SIDED_CONTRACT_BINDING = (
    "Two-sided scene contracts (path_clearance_min, parallax_displacement_profile, "
    "onset_order): bind the contract on the unit that mutates the primary `roles`; "
    "`compare_roles` may name other plan-declared namespaces. visible_fraction is "
    "repaired by a unit that provides camera or mutates/dresses every named role "
    "on that row — not by any unit that can see a plan-declared selector. "
    "A volume-only unit cannot bind mesh vis as required repair.\n"
    "Each unit is one derived write-cluster (role-namespace × host class × instrument "
    "family). Do not author family/mutation_family/coherent_family fields. Dressing, "
    "visibility observation, bounded coordination, and consumed assembly interfaces "
    "are typed exceptions. Required claims share one repair_owner. Publish interfaces "
    "export only roles, controls, or sealed contract ids.\n"
)


def _frame_authority_block(global_row: dict) -> str:
    """Compile this layer's judge subset rule — remat6 invented extra judge frames."""
    from vfx_harness.domain.work_units import compile_frame_authority

    return (
        "Frame authority compiled from this layer's global row (do not invent "
        "frames; do not Read a prior materialization to guess them):\n"
        f"{json.dumps(compile_frame_authority(global_row), indent=1)}\n"
    )


def _sealed_outcomes_block(shot_folder: Path, layer, global_row: dict) -> str:
    """Compile only dependency status and explicitly required evidence (HIR-0054)."""
    jit_row = global_row.get("jit") if isinstance(global_row.get("jit"), dict) else {}
    depends: list[str] = []
    required: list[dict[str, str]] = []
    layer_jit = getattr(layer, "jit", None)
    if layer_jit is not None:
        depends = [str(item) for item in (getattr(layer_jit, "depends_on_layers", None) or [])]
        required = [
            {"kind": str(kind), "id": str(oid)}
            for kind, oid in (getattr(layer_jit, "required_outcomes", None) or [])
        ]
    if not depends:
        depends = [str(item) for item in (jit_row.get("depends_on_layers") or [])]
    if not required:
        for item in jit_row.get("required_outcomes") or []:
            if isinstance(item, dict) and item.get("kind") and item.get("id"):
                required.append({"kind": str(item["kind"]), "id": str(item["id"])})
    if not depends and not required:
        return (
            "Sealed upstream outcomes: none. This layer is a dependency root "
            "(empty depends_on_layers and required_outcomes).\n"
        )
    required_ids = {row["id"] for row in required}
    outcomes: list[dict] = []
    for dep in depends:
        try:
            name = f"{int(dep):02d}.json"
        except (TypeError, ValueError):
            continue
        path = shot_folder / "plans" / "outcomes" / name
        if not path.is_file():
            outcomes.append({"layer": dep, "status": "missing"})
            continue
        value = json.loads(path.read_text(encoding="utf-8"))
        matched: list[dict] = []

        def collect(node, sink=matched) -> None:
            if isinstance(node, dict):
                if str(node.get("id") or "") in required_ids:
                    sink.append({
                        key: node.get(key)
                        for key in (
                            "id", "kind", "value", "target", "pass", "error",
                            "note", "source", "owner_layer", "fault_owner",
                        )
                        if node.get(key) not in (None, "")
                    })
                for child in node.values():
                    collect(child)
            elif isinstance(node, list):
                for child in node:
                    collect(child)

        collect(value)
        unique = {json.dumps(row, sort_keys=True): row for row in matched}
        outcomes.append({
            "layer": dep,
            "status": value.get("status"),
            "script": value.get("script"),
            "required_evidence": list(unique.values()),
        })
    card = {
        "depends_on_layers": depends,
        "required_outcomes": required,
        "outcomes": outcomes,
    }
    return (
        "Compiled sealed upstream outcome card (this is the complete dependency input; "
        "do not read outcome files):\n"
        f"{json.dumps(card, indent=1)}\n"
    )


def _owned_requirements_block(bundle_root: Path, global_row: dict) -> str:
    owned = {
        str(value)
        for value in ((global_row.get("jit") or {}).get("owned_requirements") or [])
    }
    if not owned:
        return "Compiled requirements owned by this layer: none.\n"
    document = json.loads((bundle_root / "requirements.json").read_text(encoding="utf-8"))
    rows = [
        row
        for row in document.get("requirements") or []
        if isinstance(row, dict) and str(row.get("id") or "") in owned
    ]
    return (
        "Compiled requirements owned by this layer (complete; do not read the global "
        f"requirements register):\n{json.dumps(rows, indent=1)}\n"
    )


def _upstream_interfaces_block(
    shot_folder: Path,
    global_row: dict,
    bundle_hash: str,
    *,
    overlay_root: str | Path | None = None,
) -> str:
    """Compile dependency grants without exposing all selected layer claims."""
    from vfx_harness.orchestration.jit_materialization import selected_view_artifact

    dependencies = {
        str(value)
        for value in ((global_row.get("jit") or {}).get("depends_on_layers") or [])
    }
    if not dependencies:
        return "Compiled upstream semantic interfaces: none (dependency root).\n"
    path = selected_view_artifact(
        shot_folder, "layers.json", bundle_hash, overlay_root=overlay_root
    )
    if path is None:
        from vfx_harness.orchestration.plan_authority import resolve_current

        path = resolve_current(shot_folder).root / "layers.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    interfaces: list[dict] = []
    for row in document.get("layers") or []:
        if not isinstance(row, dict) or str(row.get("id") or "") not in dependencies:
            continue
        interfaces.append({
            "id": str(row.get("id")),
            "title": row.get("title"),
            "dressable": list(row.get("dressable") or []),
            "units": [
                {
                    "id": unit.get("id"),
                    "provides": list(unit.get("provides") or []),
                    "roles": list((unit.get("mutates") or {}).get("roles") or []),
                    "dressable": list(unit.get("dressable") or []),
                }
                for unit in row.get("stages") or []
                if isinstance(unit, dict)
            ],
        })
    return (
        "Compiled upstream semantic interfaces and owner-granted dressable selectors "
        f"(complete; do not read the layer catalog):\n{json.dumps(interfaces, indent=1)}\n"
    )


def _materialization_kickoff(
    shot_folder: Path,
    layer,
    bundle,
    rel_target: str,
    replacing: str | None = None,
    *,
    overlay_root: str | Path | None = None,
) -> str:
    """The session must copy its global layer row exactly and close owned requirements,
    so the kickoff carries the row verbatim and the READABLE paths that hold the rest.
    Run 20260823T125746Z-9cd0b8 got only the bundle hash: it probed six plausible bundle
    locations, was denied by the path scope, reconstructed the row from prose, and
    failed structural validation on every field."""
    rows = json.loads((bundle.root / "layers.json").read_text(encoding="utf-8"))
    global_row = next(
        row for row in rows.get("layers", []) if str(row.get("id")) == str(layer.id)
    )
    # A replacement designed in ignorance of why its predecessor was discarded repeats
    # the predecessor's mistakes: the first re-materialization of layer 1 put the camera
    # last and left the faceted housing unowned, both defects the operator was replacing.
    replacement = (
        f"REPLACING A DISCARDED MATERIALIZATION. The previous design of this layer was "
        f"rejected. Reason and requirements from the operator:\n{replacing}\n"
        f"Your design must satisfy those requirements explicitly; do not reproduce the "
        f"structure being replaced.\n\n"
        if replacing
        else ""
    )
    return (
        f"{replacement}"
        f"Materialize deferred layer {layer.id} ({layer.title}).\n"
        f"Selected bundle hash: {bundle.content_hash}\n"
        f"This kickoff is the complete compiled authority card. Do not read the brief, "
        f"global registers, layer catalogs, decision ledger, or prior materializations.\n"
        f"Your exact global layer row — copy the structural fields verbatim into the "
        f"replacement layer:\n{json.dumps(global_row, indent=1)}\n"
        f"{_owned_requirements_block(bundle.root, global_row)}"
        f"{_upstream_interfaces_block(shot_folder, global_row, bundle.content_hash, overlay_root=overlay_root)}"
        f"{_frame_authority_block(global_row)}"
        f"{_TWO_SIDED_CONTRACT_BINDING}"
        f"{_binding_decisions_block(shot_folder, layer, bundle.content_hash)}"
        f"{_sealed_outcomes_block(shot_folder, layer, global_row)}"
        f"{_PUBLISH_CONSUME_EXAMPLE}"
        f"Document shape (generic minimal-valid example — replace every placeholder, "
        f"add stages/contracts/claims as the layer needs):\n{_MATERIALIZATION_EXAMPLE}\n"
        f"Output: {rel_target}"
    )


async def _materialize_deferred_layer(
    shot,
    layer,
    *,
    model: str,
    blender: str,
    max_turns: int,
    replacing: str | None = None,
    overlay_root: str | Path | None = None,
) -> None:
    """Close one layer's owned requirements with concrete authority, then select its view.

    ``overlay_root`` is an unpublished reverted overlay used as the remat design base.
    Publication is the only select; a crash must not have already moved the live pointer.
    """
    from vfx_harness.orchestration.jit_materialization import (
        MATERIALIZATION_SCHEMA,
        publish_materialization,
        seed_materialization_candidate,
    )
    from vfx_harness.orchestration.plan_authority import resolve_current

    bundle = resolve_current(shot.folder)
    layout = run_artifacts.ensure(shot.folder, command="plan-layer")
    target = layout.scratch / f"jit-layer-{layer.id}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    rel_target = target.relative_to(shot.folder).as_posix()
    seed_materialization_candidate(
        bundle.root,
        target,
        layer_id=str(layer.id),
        bundle_hash=bundle.content_hash,
    )

    def _validate_target() -> list[str]:
        from vfx_harness.orchestration.jit_materialization import (
            inspect_materialization,
            selected_view_artifact,
        )
        from vfx_harness.orchestration.plan_authority import artifact_path

        # Mirror publication EXACTLY: the base must be the view-resolved layers, not the
        # bundle's sparse rows — other layers' materialized declarations (ADR-0007
        # dressable grants) live only in their overlays, and the sparse base made the
        # write hook refuse dresses the publication path would accept (run bm9og09xw).
        try:
            base_layers = selected_view_artifact(
                shot.folder, "layers.json", bundle.content_hash, overlay_root=overlay_root
            ) or artifact_path(shot.folder, "layers.json")
            base_requirements = selected_view_artifact(
                shot.folder,
                "requirements.json",
                bundle.content_hash,
                overlay_root=overlay_root,
            ) or artifact_path(shot.folder, "requirements.json")
            findings, _materialized = inspect_materialization(
                bundle.root,
                target,
                expected_bundle_hash=bundle.content_hash,
                base_layers_path=base_layers,
                resolutions_path=shot.folder / "state" / "plan-resolutions.jsonl",
                base_requirements_path=base_requirements,
            )
            return findings
        except OSError as exc:
            return [str(exc)]
    system = f"""You materialize exactly one deferred VFX build layer at its dependency boundary.
The harness has already seeded `{rel_target}` with schema `{MATERIALIZATION_SCHEMA}`, bundle
identity, exact global layer structure, and empty collections. Do not generate or Write the whole
document or Read the seeded file; `materialization_status` supplies compact progress if needed.
Call `stage_materialization_unit` for one independently bounded unit, then wait for its
result before authoring the next unit. Never issue several staging calls in one assistant
turn. Include only that unit, its scene contracts, and the owned requirement bindings it
closes. After all units are
staged, call `finalize_materialization`; repair its complete findings with
`patch_materialization`, then finalize again. The completed candidate must contain non-empty
bounded stages and close every
globally owned requirement exactly once, either with one or more concrete contract ids or an
explicit decision carrying `statement` and `decision_strength`. Preserve global layer structure
exactly. Mutated roles must stay inside reserved namespaces. Every scene contract must be
required evidence of a materialized producing claim.
A unit that must ASSIGN materials to another layer's geometry declares `mutates.dresses`:
selectors the OWNING layer's row lists under `dressable` (granted by the owner, never
taken; assignment-only authority — geometry stays protected by the owner's contracts).
A layer whose proxies later layers must dress declares those selectors under `dressable`
on its layer row; dressed roles need required claims exactly like mutation roles. Choose unit structure, scene-truth
contracts, reference fingerprints, and techniques now from authored references plus sealed
upstream outcomes. Copy each binding structured decision listed in the kickoff
verbatim into `scene_contracts` — exact contract fields plus `decision_id` — bound
to a required claim. Ledger rows keyed to another generation, or retired by a later
superseded or falsified row, are inert: do not copy them.
The kickoff compiles this layer's judge frames and extra-frame binding rule. Unit
evaluation.judge, claim.moments, and composition_context.frames stay inside that
list. Scene contracts may measure other frames; bind those ids through
composition_context.contract_ids without adding the extra frames to the judge lists.
Each stage declares `provides`: the scene capabilities it makes available. Declare
`camera` if the unit creates the camera a dependent's framing evidence projects through,
and `geometry` if objects under its roles carry polygons — mesh metrics (smooth_fraction,
mesh_vertex_count, radial_inward_fraction) may only target roles a geometry provider
owns, because on an empty or a camera they read None forever.
Cross-unit observation is explicit. When a camera claim binds `projected_origin_x/y`
to a target produced by another unit, the target publishes a typed `placement_control`
and the camera unit declares both `depends_on` and the exact `consumes` row. The target
selector stays read-only; never add it to the camera unit's mutation roles or controls.
Every required claim declares `asserts`: the evidence domain its proposition lives in.
A metric may only close a claim it can support — a count proves existence, not sequence;
geometry proves position, not appearance. Bind a temporal metric for behaviour over
time, a projected_composition metric for framing, a scene metric for structure. Claims
that assert `image` or `human` are proved after a candidate exists, at build time.
Each stage declares `look_capabilities`: the appearance families that unit is answerable
for, a subset of detail, material, color, exposure, lighting, emission, atmosphere,
motion, grade. Declare exactly what the unit's own judged frames require — this decides
the image feedback its builder receives, and an appearance-owning unit that declares
none will be told surface quality is out of scope. A unit that genuinely changes no
appearance declares an empty list — composed canonical then fans in those
executable claims instead of a critic look vote.
Every unit `evaluation.judge` frame must appear in at least one required claim's
`moments`. A judge frame with no required claim is a contract_gap, not a critic look
vote. Extra-frame scene contracts still bind through `composition_context.contract_ids`.
A unit that declares `look_capabilities` must cover every judge frame with a required
claim that asserts `image` and binds `image_contract` (or qualification / human_decision).
Scene counts cannot certify appearance; that hole is a contract_gap, not a 5.0
executable seal and not a critic look vote.
Image checks are candidate-sensitive: the builder proposes them only after
this unit mutates the cumulative scene, so `image_contracts` must remain empty here. Claim-closure
counts the bound `image_contract` ids as producers; missing `checks.json` rows are debts, not
`does not exist`. Those ids are builder-owed `propose_checks` payments (exact id, frame,
property kind, axis); candidate freeze refuses while any remain unpaid without a typed
`unpaid_image_debt` abstention. Every write
of the completed candidate is checked by `finalize_materialization`, which returns every
collectable finding as `{{json_pointer}}: {{message}}` in one report. When a finding names a pointer, call
`patch_materialization`; group independent repairs into its `patches` array so they commit
atomically and trigger one re-validation. The tool returns remaining findings or VALIDATION
PASSED. Generic Write is unavailable because schema wrappers, paths, and cross-unit assembly are
harness work. Call `evidence_vocabulary` BEFORE authoring contracts — it enumerates every
contract kind, its evidence domain, and required fields. When no kind can express a claim,
call `escalate_vocabulary_gap` (typed durable record) and close the requirement with an
explicit decision resolution referencing the gap id — never pad with a trivially-satisfiable
contract (vacuous shapes are rejected at validation). After VALIDATION PASSED, call
`gate_preview` once: it applies the exact terminal gate to the resulting consumer view, and
a finding fixed here costs one patch instead of a retracted generation. Do not edit
global authority, create unit state, write prose, or write another file."""
    kickoff = _materialization_kickoff(
        shot.folder,
        layer,
        bundle,
        rel_target,
        replacing,
        overlay_root=overlay_root,
    )
    lab_dir = layout.scratch / "plan-lab" / f"layer-{int(layer.id):02d}-materialize"
    pserver, pnames = build_plan_tools(
        shot.folder,
        blender=blender,
        lab_dir=lab_dir,
        measure_ref_paths=tuple(ref for _frame, ref in layer.judges),
        enabled_tools=frozenset({
            "measure_ref", "spike", "ask_supervisor", "evidence_vocabulary",
            "gate_preview", "escalate_vocabulary_gap", "stage_materialization_unit",
            "materialization_status", "finalize_materialization", "patch_materialization",
        }),
        candidate_materialization=target,
        overlay_root=overlay_root,
    )
    rserver, rnames = build_recipe_tools()
    materialization_tools = _phase_tools(
        pnames, "measure_ref", "spike", "ask_supervisor", "evidence_vocabulary",
        "gate_preview", "escalate_vocabulary_gap", "stage_materialization_unit",
        "materialization_status", "finalize_materialization", "patch_materialization",
    )
    options = ClaudeAgentOptions(
        model=model,
        system_prompt=system,
        cwd=str(shot.folder),
        mcp_servers={"plan": pserver, "recipes": rserver},
        allowed_tools=[*materialization_tools, *rnames],
        disallowed_tools=[*MATERIALIZATION_DENIED_TOOLS, "Write"],
        permission_mode="bypassPermissions",
        max_buffer_size=32 * 1024 * 1024,
        setting_sources=[],
        max_turns=max_turns,
        effort="high",
        hooks=_with_target_feedback(
            planner_hooks(
                shot.folder,
                writable_files=(target,),
                strict_reads=True,
                completion_gate=False,
            ),
            target,
            _validate_target,
        ),
    )

    async def _attempt() -> str:
        said: list[str] = []
        async for message in query(prompt=kickoff, options=options):
            log_message(message)
            for block in getattr(message, "content", None) or []:
                if value := getattr(block, "text", None):
                    said.append(value)
            if signal := result_signal(message):
                said.append(signal)
        return "\n".join(said)[-4000:]

    costlog.bind(shot.folder, role="plan:materialize", model=model, tag=str(layer.id))
    tpath = transcript.bind(shot.folder, "plan", label=f"materialize-layer-{layer.id}")
    if tpath:
        log(f"transcript → {tpath.relative_to(shot.folder)}", 1)
    transcript.prompt(
        kickoff,
        role="kickoff",
        mode="PLAN_MATERIALIZE",
        model=model,
        layer=layer.id,
        replacing=replacing,
        max_turns=max_turns,
    )
    try:
        await run_session(
            _attempt,
            succeeded=lambda: target.is_file() and not _validate_target(),
            label=f"materialize layer {layer.id}",
        )
    except Exception as exc:
        log(f"! materialize session died: {str(exc)[:200]}")
        transcript.event("died", error=str(exc)[:2000])
        raise
    finally:
        transcript.unbind()
        costlog.unbind()
    publish_materialization(shot.folder, target, overlay_root=overlay_root)
    log(f"deferred layer {layer.id} materialized against bundle {bundle.content_hash[:12]}")

DRAFT_MODEL = DEFAULT_EXECUTION_MODEL
# Draft and verify deliberately share the configured planner model. Their independence
# comes from distinct sessions and an adversarial contract, not from pretending two calls
# to one model are statistically independent. CLI flags can still create a mixed-model
# lane when an experiment needs it.
VERIFY_MODEL = DEFAULT_EXECUTION_MODEL
MODEL = VERIFY_MODEL  # single-pass default


@dataclass(frozen=True, slots=True)
class PlanLoopResult:
    """Terminal authority from the deterministic until-clean loop."""

    path: Path
    outcome: str
    blocking_count: int
    plan_pointer: str | None = None
    plan_bundle: str | None = None
    plan_content_hash: str | None = None

    @property
    def clean(self) -> bool:
        return self.outcome in {
            "clean", "clean_with_assumptions", "clean_with_deferred"
        } and self.blocking_count == 0


class PlanGateFailure(SystemExit):
    """Exit 3 while preserving a useful run-status detail instead of a traceback."""

    def __init__(self, result: PlanLoopResult):
        self.detail = (
            f"plan loop {result.outcome} with {result.blocking_count} blocking "
            f"finding(s) remaining in {result.path}"
        )
        self.run_metadata = {
            "outcome": result.outcome,
            "blocking_count": result.blocking_count,
            "plan_gate_report": "reports/plan_gate.json",
            **({"plan_pointer": result.plan_pointer} if result.plan_pointer else {}),
            **({"plan_bundle": result.plan_bundle} if result.plan_bundle else {}),
            **({"plan_content_hash": result.plan_content_hash} if result.plan_content_hash else {}),
        }
        super().__init__(3)

    def __str__(self) -> str:
        return self.detail


@dataclass(frozen=True, slots=True)
class PlanRoleCapabilities:
    """Testable workflow contract for one global planning role."""

    role: str
    verbs: frozenset[str]
    allowed_tools: frozenset[str]
    denied_tools: frozenset[str]
    include_gate: bool


def plan_role_capabilities(role: str) -> PlanRoleCapabilities:
    """Return the declared verbs and concrete affordances for a global plan role."""
    if role not in {"draft", "verify", "repair"}:
        raise ValueError(f"unknown global plan role: {role!r}")
    denied = {"Bash"}
    if role == "repair":
        denied.update({"Task", "Agent"})
    return PlanRoleCapabilities(
        role=role,
        verbs=frozenset({"author", "patch", "gate", "escalate"}),
        allowed_tools=frozenset({"Edit"}),
        denied_tools=frozenset(denied),
        include_gate=True,
    )


def _phase_tools(names: list[str], *short_names: str) -> list[str]:
    """Expose only tools that belong to the current authority boundary."""
    suffixes = tuple(f"__{name}" for name in short_names)
    return [name for name in names if name.endswith(suffixes)]


def _planner_tool_policy(repair: bool) -> tuple[list[str], list[str]]:
    """Compatibility adapter for callers that predate explicit role manifests."""
    capabilities = plan_role_capabilities("repair" if repair else "draft")
    return sorted(capabilities.allowed_tools), sorted(capabilities.denied_tools)


# The reference board is the visual half of the brief, and the harness used to hand over
# a list of FILENAMES. `_refs_block`'s own docstring said "stills are the ONLY visual
# input" and then emitted ten lines of text. Nothing else supplied a picture either:
# measure_ref returned numbers, and contact_sheet/extract_frames — the tools that DO
# return images — were defined but never registered, so they were uncallable on every
# shot (now fixed, and registered when the shot has video). Whether the planner ever saw
# the thing it was planning came down to whether it happened to try Read on a .jpg.
#
# It did, in the run that produced barrel_roll — all ten. And the plan it wrote still
# scored 26 mentions of `halation` against ZERO for camera angle, shadow side or solid
# form. So attaching these is not sufficient on its own; it is the half that can be
# guaranteed, and an unguaranteed input is the wrong thing to be debugging around later.
# The critic has had this guarantee for a while — it "cannot score a frame it never saw"
# — and there is no argument for holding the stage that WRITES the targets to a lower bar
# than the stage that checks them.
_KICKOFF_MAX_PX = 1568  # same budget the critic uses; ~1600 tokens per still


def _kickoff_blocks(text: str, shot, *, refs=None) -> list[dict]:
    """Kickoff prose plus the explicitly due reference stills, in shot order."""
    from .builder import _image_block

    blocks: list[dict] = [{"type": "text", "text": text}]
    for p in shot.refs if refs is None else refs:
        try:
            blocks.append(_image_block(p, _KICKOFF_MAX_PX))
        except Exception as e:  # a corrupt plate must not cost the whole pass
            log(f"! could not attach {p.name}: {str(e)[:120]}", 1)
    return blocks


async def generate_plan(
    folder: str | Path,
    *,
    model: str | None = None,
    blender: str = "blender",
    max_turns: int = 100,
    tag: str | None = None,
    verify_draft: str | None = None,
    repair: tuple[str, int] | None = None,
    workspace: str | Path | None = None,
) -> Path:
    """Run ONE global planning session. With `tag`, outputs are isolated:
    plans/global.md → plans/global.<tag>.md, lab artifacts → active run scratch/plan-lab/.
    With `verify_draft`, the session runs in VERIFY MODE against that draft file.
    With `repair=(findings, round)`, it runs in REPAIR MODE against `verify_draft`."""
    source_shot = load_shot(folder)
    layout = run_artifacts.ensure(source_shot.folder, command="plan")
    if workspace is None:
        from vfx_harness.orchestration.plan_authority import prepare_staging

        workspace = prepare_staging(layout)
    workspace = Path(workspace).resolve()
    shot = load_shot(workspace)
    model = model or Settings.from_environment(load_dotenv_file=False).planner_model
    plan_path = global_plan_path(shot.folder)
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    lab_dir = layout.scratch / "plan-lab" / (tag or "global")

    from vfx_harness.orchestration.plan_authoring import (
        clause_registry,
        registry_prompt_block,
    )

    registry = clause_registry(workspace / "brief.md")
    mapping_path = workspace / "ownership_mapping.json"

    if repair:
        findings, rnd = repair
        system = PLANNER_SYSTEM + REPAIR_ADDENDUM.format(draft=verify_draft, findings=findings)
        kickoff = repair_user_prompt(shot, verify_draft, rnd)
        mode = f"REPAIR round {rnd} (against {verify_draft})"
        role = "repair"
    elif verify_draft:
        system = PLANNER_SYSTEM + VERIFIER_ADDENDUM.format(draft=verify_draft)
        kickoff = verifier_user_prompt(shot, verify_draft)
        mode = f"VERIFY (auditing {verify_draft})"
        role = "verify"
    else:
        system = PLANNER_SYSTEM
        kickoff = planner_user_prompt(shot, registry_prompt_block(registry))
        mode = "PLAN (from scratch)"
        role = "draft"

    _expand_mapping_or_errors = mapping_expander(workspace, registry, mapping_path)

    capabilities = plan_role_capabilities(role)
    pserver, pnames = build_plan_tools(
        shot.folder,
        blender=blender,
        lab_dir=lab_dir,
        include_gate=capabilities.include_gate,
        run_layout=layout,
        enabled_tools=frozenset({"ask_supervisor", "run_gate"}),
    )
    # Every global role authors the same transaction and therefore needs the same patch and
    # validation verbs. Repair additionally loses delegation so a bounded mechanical patch
    # cannot escape into an agent that lacks its exact context or tools.
    options = ClaudeAgentOptions(
        model=model,
        system_prompt=system,
        cwd=str(shot.folder),
        mcp_servers={"plan": pserver},
        allowed_tools=[
            "Read", "Glob", "Grep", "Write", *sorted(capabilities.allowed_tools),
            *_phase_tools(pnames, "ask_supervisor", "run_gate"),
        ],
        disallowed_tools=sorted(capabilities.denied_tools),
        permission_mode="bypassPermissions",
        max_buffer_size=32 * 1024 * 1024,  # sheets/frames as base64 image blocks
        setting_sources=[],  # isolate from user/project settings
        max_turns=max_turns,
        effort="high",
        hooks=_with_target_feedback(
            planner_hooks(
                workspace,
                readable_files=(verify_draft,) if repair and verify_draft else (),
                # The mapping is the ONLY model-authored surface; every published
                # artifact is machine-expanded from it, so other writes are denied
                # rather than merely discouraged.
                writable_files=(mapping_path,),
            ),
            mapping_path,
            _expand_mapping_or_errors,
        ),
    )

    stills = [p.name for p in shot.refs]
    videos = sorted(p.name for p in (shot.folder / "refs").glob("*.mp4"))
    log(
        f"plan agent [{mode}]: shot '{shot.id}' ({shot.frames}f @ {shot.fps}fps, "
        f"{shot.engine}), model {model}" + (f", tag '{tag}'" if tag else "")
    )
    log(f"refs: {len(stills)} stills {stills} + {len(videos)} videos {videos}", 1)
    log(
        f"workspace: {workspace.relative_to(source_shot.folder)}/ · "
        f"lab: {lab_dir.relative_to(source_shot.folder)}/ · "
        f"global tools: ownership, escalation, and deterministic gate only · max_turns {max_turns}",
        1,
    )

    costlog.bind(source_shot.folder, role="plan:" + mode.split()[0].lower(), model=model, tag=tag)
    tpath = transcript.bind(source_shot.folder, "plan", label=tag or mode)
    if tpath:
        log(f"transcript → {tpath.relative_to(source_shot.folder)}", 1)
    transcript.prompt(
        kickoff, role="kickoff", mode=mode, model=model, tag=tag, refs=stills, videos=videos, max_turns=max_turns
    )
    # Attaching every future approval frame recreated whole-shot visual preproduction.
    # Ready-unit references are requested explicitly after the sparse DAG exists.
    blocks = _kickoff_blocks(kickoff, shot, refs=())
    log("kickoff: reference stills available on demand; none attached globally", 1)
    # The post-condition, not the absence of an exception. Two repair rounds were lost to a
    # session that raised "error result: success" at $0.0007 having written nothing, and the
    # real cause ("Repeated 529 Overloaded errors") was only in its assistant text — which is
    # why one attempt collects that text and hands it to the classifier.
    before = plan_path.stat().st_mtime_ns if plan_path.is_file() else -1

    async def _attempt() -> str:
        said: list[str] = []
        async for message in query(prompt=_one_user_message(blocks), options=options):
            log_message(message)
            for blk in getattr(message, "content", None) or []:
                text = getattr(blk, "text", None)
                if text:
                    said.append(text)
            if signal := result_signal(message):
                said.append(signal)
        return "\n".join(said)[-4000:]

    def _wrote() -> bool:
        return plan_path.is_file() and plan_path.stat().st_mtime_ns != before

    try:
        await run_session(_attempt, succeeded=_wrote, label=f"plan {mode}")
    except Exception as e:
        log(f"! plan session died: {str(e)[:200]}")
        transcript.event("died", error=str(e)[:2000])
        raise
    finally:
        transcript.unbind()
        costlog.unbind()
    if tag:
        final = plan_path.with_name(f"global.{tag}.md")
        plan_path.rename(final)
        plan_path = final
    lines = plan_path.read_text(encoding="utf-8").count("\n")
    log(f"plan written: {plan_path.relative_to(shot.folder)} ({lines} lines)")
    return plan_path


async def _rematerialize_layer(
    shot, layer, authority: tuple[str, str, list[str], bool], *, model, blender, max_turns
):
    """Replace a materialized layer view and move durable unit state through apply_replan.

    Materialization is a decision, and a decision proven wrong must be replaceable —
    layer 1 of run 20260823T154920Z shipped defective contracts, proxied claims, and a
    unit whose script escaped its own scope. This does NOT add a supersession authority:
    the view is republished through `publish_materialization` and durable unit state
    moves through `apply_replan`. Matching unit digests stay, including accepted
    checkpoints. Changed, removed, or downstream-invalidated units are superseded
    even if they had passed — that is a DAG amendment, not a discard. `--discard-accepted`
    remains the heavier act: accepted orphans, and wiping state when the replan base
    is unusable (HIR-0052).
    """
    from vfx_harness.orchestration.plan_authority import (
        resolve_current,
    )
    from vfx_harness.orchestration.unit_state import (
        apply_replan,
        supersede_layer_units,
        validate_current,
    )
    from vfx_harness.orchestration.unit_state import load as load_unit_state

    owner, trigger, evidence, discard_accepted = authority
    layer_id = str(layer.id)
    state = load_unit_state(shot.folder, layer_id)
    accepted = sorted(
        uid
        for uid, row in (state.get("units") or {}).items()
        if row.get("status") == "passed"
    )
    if accepted and discard_accepted:
        log(
            f"discard-accepted: accepted unit(s) {', '.join(accepted)} may retire "
            "if the replacement orphans them or the replan base is unusable",
            1,
        )
    elif accepted:
        log(
            f"accepted unit(s) {', '.join(accepted)} stay unless the replacement "
            "DAG invalidates them",
            1,
        )

    def _plan_hash() -> str:
        from vfx_harness.orchestration.plan_authority import active_plan_hash

        return active_plan_hash(shot.folder)

    old_units, old_plan_hash = layer.stages, _plan_hash()
    state_backed_base = False
    if state:
        # Durable state is the accepted base identity. Global republication can make
        # the prior JIT view inert before remat starts, and a sibling materialization
        # can change the combined layers.json hash without changing this layer's DAG.
        # Use the state hash in both cases. If the currently selected layer no longer
        # matches those stored unit identities, apply_replan performs a digest-backed
        # state diff instead of pretending the new sparse/ready row is the old DAG.
        old_plan_hash = str(state.get("plan_hash") or old_plan_hash)
        try:
            validate_current(state, layer_id, old_units)
        except ValueError:
            old_units = ()
            state_backed_base = True
            log(
                "selected layer no longer reconstructs the durable replan base; "
                "using digest-bound work-unit state",
                1,
            )
    bundle = resolve_current(shot.folder)
    deferred = load_layers_from_path(bundle.root / "layers.json")[layer_id]
    log(
        f"re-materializing layer {layer_id}: replacing {len(old_units)} unit(s) "
        f"({', '.join(u.id for u in old_units) or 'none'}) — {trigger}"
    )
    # Design against global authority, not against the view being replaced: otherwise
    # the discarded register (where this layer's owned requirements were already
    # resolved) is the base, and the replacement trips owned-means-owed.
    from vfx_harness.orchestration.jit_materialization import revert_materialization

    overlay = revert_materialization(shot.folder, layer_id, select=False)
    if overlay is not None:
        log(
            f"designing replacement against unpublished overlay {overlay.name}; "
            "live pointer stays until publication",
            1,
        )
    await _materialize_deferred_layer(
        shot,
        deferred,
        model=model,
        blender=blender,
        max_turns=max_turns,
        replacing=trigger,
        overlay_root=overlay,
    )
    refreshed = load_layers(shot)[layer_id]
    if state:
        try:
            apply_replan(
                shot.folder,
                layer_id,
                old_units,
                refreshed.stages,
                old_plan_hash=old_plan_hash,
                new_plan_hash=_plan_hash(),
                owner=owner,
                trigger=trigger,
                evidence=evidence,
                discard_accepted=discard_accepted,
                state_backed_base=state_backed_base,
            )
        except ValueError as exc:
            # The replan base can be unreconstructable — a prior partial transaction
            # left state naming a DAG that no longer exists, or its digests predate a
            # WorkUnit schema change. Wiping accepted checkpoints is --discard-accepted;
            # otherwise fail closed with the view already published and state unmoved
            # (HIR-0052).
            if accepted and not discard_accepted:
                raise
            log(f"replan base unusable ({str(exc)[:90]}); superseding layer units", 1)
            supersede_layer_units(
                shot.folder,
                layer_id,
                owner=owner,
                trigger=trigger,
                evidence=evidence,
                plan_hash=_plan_hash(),
                allow_accepted=discard_accepted,
            )
        log(
            f"work-unit state superseded → {', '.join(u.id for u in refreshed.stages)}",
            1,
        )
    return refreshed


async def generate_layer_plan(
    folder: str | Path,
    layer_id: str,
    *,
    unit_id: str | None = None,
    model: str | None = None,
    blender: str = "blender",
    max_turns: int = 24,
    rematerialize: tuple[str, str, list[str], bool] | None = None,
) -> Path:
    """Generate one work-unit plan after its declared dependencies have sealed outcomes.

    This is intentionally a separate session and output contract. It cannot mutate the
    global plan or machine contracts, and there is no monolithic-plan fallback.
    """
    shot = load_shot(folder)
    model = model or Settings.from_environment(load_dotenv_file=False).planner_model
    global_path = global_plan_path(shot.folder)
    if not global_path.is_file():
        raise FileNotFoundError(f"{global_path} missing — generate and gate the strict global plan first")
    layers = load_layers(shot)
    try:
        layer = layers[str(layer_id)]
    except KeyError as exc:
        raise KeyError(f"unknown layer {layer_id!r}; available: {', '.join(layers)}") from exc
    if rematerialize is not None:
        # A prior remat that selected a hole then crashed leaves this layer
        # jit_deferred in the live view. The replacement still needs the unpublished
        # overlay and apply_replan — unpacking a 4-tuple as 3 and skipping unit-state
        # movement is how remat5 would publish then die (HIR-0026). Accepted units
        # are not a door refuse; apply_replan preserves matching digests (HIR-0052).
        layer = await _rematerialize_layer(
            shot, layer, rematerialize, model=model, blender=blender, max_turns=max_turns
        )
    elif layer.execution == "jit_deferred":
        await _materialize_deferred_layer(
            shot,
            layer,
            model=model,
            blender=blender,
            max_turns=max_turns,
        )
        layers = load_layers(shot)
        layer = layers[str(layer_id)]
    from vfx_harness.domain.work_units import ready_units
    from vfx_harness.orchestration.plan_authority import active_plan_hash
    from vfx_harness.orchestration.unit_state import digest_matched_passed
    from vfx_harness.orchestration.unit_state import initialize as initialize_unit_state

    layers_hash = active_plan_hash(shot.folder)
    state = initialize_unit_state(
        shot.folder, str(layer.id), layer.stages, plan_hash=layers_hash
    )
    passed = {
        uid for uid, row in (state.get("units") or {}).items() if row.get("status") == "passed"
    }
    ready = ready_units(
        layer.stages, passed, sealed_producers=digest_matched_passed(state, layer.stages)
    )
    if unit_id is not None:
        selected = next((unit for unit in layer.stages if unit.id == unit_id), None)
        if selected is None:
            raise KeyError(
                f"unknown unit {unit_id!r} in layer {layer.id}; available: "
                + ", ".join(unit.id for unit in layer.stages)
            )
        missing = sorted(set(selected.depends_on) - passed)
        if missing:
            raise ValueError(
                f"layer {layer.id} unit {selected.id} is blocked by unpassed dependencies: "
                + ", ".join(missing)
            )
    else:
        if not ready:
            raise ValueError(
                f"layer {layer.id} has no plannable unit; all units passed or dependencies are blocked"
            )
        selected = ready[0]
    target = work_unit_plan_path(shot.folder, selected)
    if is_selected_bundle_member(shot.folder, target):
        validate_work_unit_plan_authority(shot.folder, target)
        log(
            f"unit plan already frozen in selected bundle; model-free reuse: "
            f"{target.relative_to(shot.folder)}"
        )
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    rel_target = target.relative_to(shot.folder).as_posix()
    feedback = "\n\n".join(
        x
        for x in (
            prior_outcomes_block(shot.folder, str(layer.id)),
            amendment_block(shot.folder, str(layer.id)),
            contract_gaps_block(shot.folder, str(layer.id), selected.id),
        )
        if x
    )
    # Do not carry the global planner's monolithic output contract into a layer session.
    # The layer doctrine is intentionally self-contained and much smaller.
    system = LAYER_PLANNER_ADDENDUM.format(
        layer_id=layer.id,
        layer_title=layer.title,
        unit_id=selected.id,
        unit_title=selected.title,
        target=rel_target,
    )
    from vfx_harness.agents.unit_scope import compile_scope_with_predecessors
    from vfx_harness.evidence.scene_checks import load_rows

    contract_rows = load_rows(shot.folder)
    unit_card = compile_scope_with_predecessors(
        unit=selected,
        layer_id=str(layer.id),
        contracts=contract_rows,
        units=layer.stages,
        durable_state=state,
        helpers=(),
    )
    predecessor_cards = list(unit_card.get("predecessor_interfaces") or [])
    kickoff = layer_user_prompt(
        shot,
        layer,
        selected,
        rel_target,
        feedback,
        unit_card=unit_card,
        predecessor_cards=predecessor_cards,
    )
    layout = run_artifacts.ensure(shot.folder, command="plan-layer")
    lab_dir = layout.scratch / "plan-lab" / f"layer-{int(layer.id):02d}"
    pserver, pnames = build_plan_tools(
        shot.folder,
        blender=blender,
        lab_dir=lab_dir,
        enabled_tools=frozenset({
            "measure_ref",
            "spike",
            "ask_supervisor",
            "gate_preview",
            "publish_unit_plan",
        }),
        unit_plan_target=target,
    )
    rserver, rnames = build_recipe_tools()
    unit_plan_tools = _phase_tools(
        pnames,
        "measure_ref",
        "spike",
        "ask_supervisor",
        "gate_preview",
        "publish_unit_plan",
    )
    options = ClaudeAgentOptions(
        model=model,
        system_prompt=system,
        cwd=str(shot.folder),
        mcp_servers={"plan": pserver, "recipes": rserver},
        allowed_tools=[*unit_plan_tools, *rnames],
        disallowed_tools=["Bash", "Edit", "Write"],
        permission_mode="bypassPermissions",
        max_buffer_size=32 * 1024 * 1024,
        setting_sources=[],
        max_turns=max_turns,
        effort="high",
        hooks=planner_hooks(
            shot.folder,
            writable_files=(target,),
            strict_reads=True,
            completion_gate=False,
        ),
    )
    before = target.stat().st_mtime_ns if target.is_file() else -1
    judge_names = {Path(ref).name for _frame, ref in layer.judges}
    blocks = _kickoff_blocks(
        kickoff, shot, refs=tuple(ref for ref in shot.refs if ref.name in judge_names)
    )
    costlog.bind(shot.folder, role="plan:layer", model=model, tag=str(layer.id))
    tpath = transcript.bind(shot.folder, "plan", label=f"layer-{layer.id}")
    if tpath:
        log(f"transcript → {tpath.relative_to(shot.folder)}", 1)
    transcript.prompt(
        kickoff, role="kickoff", mode="PLAN_LAYER", model=model, layer=layer.id, refs=[p.name for p in shot.refs]
    )

    async def _attempt() -> str:
        said: list[str] = []
        async for message in query(prompt=_one_user_message(blocks), options=options):
            log_message(message)
            for blk in getattr(message, "content", None) or []:
                if text := getattr(blk, "text", None):
                    said.append(text)
            if signal := result_signal(message):
                said.append(signal)
        return "\n".join(said)[-4000:]

    def _wrote() -> bool:
        return target.is_file() and target.stat().st_mtime_ns != before

    # Materialization is a transaction: the shot may keep this plan ONLY if the
    # deterministic gate accepts the resulting consumer view. Run 20260824T103842Z-afec73
    # wrote its generated plan, failed the gate in the caller, and left the file behind —
    # the next build trusted its existence and built a unit on gate-failed authority.
    from vfx_harness.observability.provenance import atomic_write

    authority_path = work_unit_plan_authority_path(target)
    prior_plan = target.read_text(encoding="utf-8") if target.is_file() else None
    prior_authority = authority_path.read_text(encoding="utf-8") if authority_path.is_file() else None

    def _rollback() -> None:
        for path, prior in ((target, prior_plan), (authority_path, prior_authority)):
            if prior is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write(path, prior)

    try:
        log(f"plan agent [LAYER {layer.id} · UNIT {selected.id}]: {selected.title} → {rel_target}")
        await run_session(_attempt, succeeded=_wrote, label=f"plan layer {layer.id} unit {selected.id}")
    finally:
        transcript.unbind()
        costlog.unbind()
    try:
        text = target.read_text(encoding="utf-8")
        if len(text.strip()) < 200:
            raise ValueError(f"{target} is too small to be an executable layer plan")
        if text.count("\n") + 1 > 160:
            raise ValueError(
                f"{target} has {text.count(chr(10)) + 1} lines; layer plans are capped at 160. "
                "Keep evidence in machine contracts/outcomes and rewrite this as an execution index"
            )
        # integrity stamp first — the gate validates it, then a clean result earns the
        # gate attestation consumers require
        stamp_work_unit_plan(shot.folder, target)
        from vfx_harness.evaluation.plan_gate import report as gate_report
        from vfx_harness.evaluation.plan_gate import run as run_plan_gate
        from vfx_harness.orchestration.plan_authority import prepare_consumer_view

        gated = run_plan_gate(prepare_consumer_view(layout))
        # scoped: another layer's stuck STATE belongs to that layer's own transaction
        # and must not block this layer's plan (run bwng97m5n: layer 1's amendment died
        # on layer 2's 'no ready unit' finding)
        if not gated.clean_for(str(layer.id)):
            raise RuntimeError(
                f"generated unit plan {layer.id}.{selected.id} failed the deterministic gate:\n"
                + gate_report(gated)
            )
        stamp_work_unit_plan(
            shot.folder, target, gate={"clean": True, "blocking": 0, "run_id": layout.run_id}
        )
    except BaseException:
        _rollback()
        log(f"unit plan retracted: {rel_target} did not pass the deterministic gate")
        raise
    log(f"unit plan published through a clean gate: {rel_target} ({text.count(chr(10))} lines)")
    return target


async def generate_plan_two_pass(
    folder: str | Path,
    *,
    draft_model: str | None = None,
    verify_model: str | None = None,
    blender: str = "blender",
    max_turns: int = 100,
    tag: str | None = None,
    verify_only: bool = False,
    workspace: str | Path | None = None,
) -> Path:
    """The standard flow: draft from scratch, then adversarially verify.
    Keeps the draft (plan.<tag->draft.md + its lab) as the audit trail."""
    shot = load_shot(folder)
    layout = run_artifacts.ensure(shot.folder, command="plan")
    if workspace is None:
        from vfx_harness.orchestration.plan_authority import prepare_staging

        workspace = prepare_staging(layout)
    workspace = Path(workspace).resolve()
    configured_settings = Settings.from_environment(load_dotenv_file=False)
    configured = configured_settings.planner_model
    draft_model = draft_model or configured
    verify_model = verify_model or configured
    dtag = f"{tag}-draft" if tag else "draft"
    draft_path = global_plan_path(workspace).with_name(f"global.{dtag}.md")

    if verify_only:
        if not draft_path.is_file():
            raise FileNotFoundError(f"--verify-only needs an existing {draft_path.name}")
        log(f"two-pass: reusing existing draft {draft_path.name}")
    else:
        log(f"══ two-pass 1/2 · DRAFT · {draft_model} ══")
        await generate_plan(
            folder,
            model=draft_model,
            blender=blender,
            max_turns=max_turns,
            tag=dtag,
            workspace=workspace,
        )

    # VERIFY audits the immutable draft path, while its write contract and run_gate tool
    # operate on plans/global.md. Seed that canonical candidate with the exact draft bytes
    # so the verifier's first gate measures the artifact it was assigned instead of
    # reporting a synthetic "global.md missing" blocker.
    verify_candidate = global_plan_path(workspace)
    verify_candidate.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(draft_path, verify_candidate)
    log(f"══ two-pass 2/2 · VERIFY · {verify_model} · auditing {draft_path.name} ══")
    verify_turns = min(
        max_turns,
        getattr(configured_settings, "plan_verify_max_turns", 6),
    )
    try:
        final = await generate_plan(
            folder,
            model=verify_model,
            blender=blender,
            max_turns=verify_turns,
            tag=tag,
            verify_draft=draft_path.relative_to(workspace).as_posix(),
            workspace=workspace,
        )
    except AgentSessionFailure as failure:
        if failure.terminal_cause != "max_turns_exhausted":
            raise
        # The until-clean loop converges against the DETERMINISTIC gate, not the verify
        # critique. An audit that exhausts its budget must not discard a viable draft:
        # the canonical candidate was seeded from the exact draft bytes above, any
        # artifact repairs the dying verifier landed are on disk, and the gate re-measures
        # all of it from scratch. Runs 1c18c2, 7040c2, and 270652 each aborted here with a
        # converging candidate (27→7 findings in 270652) the repair rounds never saw.
        log(
            f"! verify exhausted its {verify_turns}-turn budget — handing the on-disk "
            f"candidate to the deterministic gate and repair rounds instead of discarding it"
        )
        final = verify_candidate
    log(f"two-pass complete → {final.name} (draft kept: {draft_path.name})")
    return final


async def generate_plan_until_clean(
    folder: str | Path,
    *,
    draft_model: str | None = None,
    verify_model: str | None = None,
    blender: str = "blender",
    max_turns: int = 100,
    tag: str | None = None,
    verify_only: bool = False,
    max_rounds: int = 3,
) -> PlanLoopResult:
    """Draft → verify → GATE → repair → gate → … until the plan clears or stops moving.

    The loop exists because "solid" was previously a model's own opinion of its own work,
    and every mechanism built to make it more than that was self-certified: the planner
    marked `[unknown]` zero times across four documents (which is what gates research), it
    never once called `ask_supervisor`, and the verify pass — whose instruction #2 is
    "measure-check every approval still" — shipped seven fingerprints that do not
    reproduce. So the thing this loop converges against is a DETERMINISTIC gate over the
    artifacts on disk, not another critique pass. A model cannot talk its way past it.

    Three ways to stop, and two of them are failures that must be reported as failures:

      clean    no blocking findings — the plan aims at things that are actually there
      stalled  a round produced the SAME findings as the one before it. The repair pass
               is not converging, and paying for another identical answer helps nobody
      budget   max_rounds reached with findings outstanding

    Returns the plan path and deterministic terminal outcome either way. The caller keeps
    the dirty plan as useful evidence but must publish a non-zero terminal result; what
    must never happen is a dirty plan looking clean.
    """
    from vfx_harness.evaluation import plan_gate
    from vfx_harness.orchestration import plan_authority

    configured = Settings.from_environment(load_dotenv_file=False).planner_model
    draft_model = draft_model or configured
    verify_model = verify_model or configured

    shot = load_shot(folder)
    layout = run_artifacts.ensure(shot.folder, command="plan")
    workspace = plan_authority.prepare_staging(layout)
    final = await generate_plan_two_pass(
        folder,
        draft_model=draft_model,
        verify_model=verify_model,
        blender=blender,
        max_turns=max_turns,
        tag=tag,
        verify_only=verify_only,
        workspace=workspace,
    )
    plan_name = final.relative_to(workspace).as_posix()

    prev_sig, outcome = None, "budget"
    for rnd in range(1, max_rounds + 1):
        # New plans use the current five-artifact contract.  The standalone gate keeps
        # missing scene checks warning-only for legacy shots, but a planner running now
        # must not claim CLEAN while leaving numeric scene facts to the vision judge.
        res = plan_gate.run(workspace, plan_name, require_scene_checks=True)
        res.shot = shot.id
        log(f"══ gate {rnd}/{max_rounds} ══")
        log(plan_gate.report(res), 1)
        if res.clean:
            outcome = res.publishable_outcome
            break
        sig = res.signature()
        if sig == prev_sig:
            outcome = "stalled"
            log(
                f"! gate findings are unchanged from round {rnd - 1} — the repair pass is "
                f"not converging. Stopping rather than paying for the same answer again."
            )
            break
        prev_sig = sig
        # The repair input is immutable evidence owned by this run. Shot-global round names
        # let a later invocation overwrite the only record of what an earlier repair saw.
        snap = plan_authority.snapshot_repair_input(layout, rnd, final)
        snap_rel = snap.relative_to(shot.folder).as_posix()
        log(
            f"══ repair {rnd}/{max_rounds} · {verify_model} · {len(res.blocking)} "
            f"blocking finding(s) → {snap_rel} ══"
        )
        final = await generate_plan(
            folder,
            model=verify_model,
            blender=blender,
            max_turns=max_turns,
            tag=tag,
            verify_draft=str(snap),
            repair=(plan_gate.feedback(res), rnd),
            workspace=workspace,
        )
        plan_name = final.relative_to(workspace).as_posix()
    else:
        res = plan_gate.run(workspace, plan_name, require_scene_checks=True)
        res.shot = shot.id
        log("══ gate (final) ══")
        log(plan_gate.report(res), 1)
        outcome = res.publishable_outcome if res.clean else "budget"

    n = len(res.blocking)
    report_path = layout.write_report("plan_gate", res.to_dict(outcome=outcome))
    log(f"gate authority → {report_path.relative_to(shot.folder)}", 1)
    published_pointer = published_bundle = published_hash = None
    if outcome in {"clean", "clean_with_assumptions", "clean_with_deferred"} and tag is None:
        bundle = plan_authority.publish_current(
            shot.folder,
            layout,
            outcome=outcome,
            plan_path=final,
            source_root=workspace,
        )
        pointer_rel = plan_authority.POINTER.as_posix()
        published_pointer = pointer_rel
        published_bundle = bundle.root.relative_to(shot.folder).as_posix()
        published_hash = bundle.content_hash
        layout.terminal_metadata.update(
            {
                "plan_pointer": pointer_rel,
                "plan_bundle": bundle.root.relative_to(shot.folder).as_posix(),
                "plan_content_hash": bundle.content_hash,
            }
        )
        log(f"plan authority → {pointer_rel} ({bundle.content_hash[:16]})", 1)
    elif outcome in {"clean", "clean_with_assumptions", "clean_with_deferred"} and tag is not None:
        log("tagged plan is gated evidence only; it does not replace plans/current.json", 1)
    log(
        f"plan loop {outcome.upper()}: {final.name}"
        + (
            ""
            if outcome in {"clean", "clean_with_assumptions", "clean_with_deferred"}
            else f" — {n} blocking finding(s) REMAIN. `vfx evals plan {shot.folder}` lists "
            f"them; building on this plan means building toward them."
        )
    )
    return PlanLoopResult(
        final,
        outcome,
        n,
        plan_pointer=published_pointer,
        plan_bundle=published_bundle,
        plan_content_hash=published_hash,
    )


def main() -> None:
    settings = Settings.from_environment()
    ap = argparse.ArgumentParser(description="Plan a shot. Default: two-pass (draft → adversarial verify).")
    ap.add_argument("folder", help="shot folder (contains brief.md, refs/)")
    ap.add_argument("--layer", help="generate only this layer's just-in-time plan")
    ap.add_argument("--unit", help="with --layer, generate this ready work unit instead of the first ready unit")
    ap.add_argument(
        "--rematerialize",
        action="store_true",
        help="with --layer, replace the layer's materialized view and design it again "
        "from global authority; apply_replan preserves units whose digests still match",
    )
    ap.add_argument(
        "--discard-accepted",
        action="store_true",
        help="with --rematerialize, permit retiring accepted orphans and wiping state "
        "when the replan base is unusable; matching digests still stay through apply_replan",
    )
    ap.add_argument("--owner", help="authority applying a --rematerialize transaction")
    ap.add_argument("--trigger", help="why the materialized view is being replaced")
    ap.add_argument(
        "--evidence",
        action="append",
        default=[],
        help="evidence locator for --rematerialize; repeat for each item",
    )
    ap.add_argument("--single", action="store_true", help="one from-scratch pass with --model (no verify)")
    ap.add_argument(
        "--verify-only", action="store_true", help="skip drafting; audit the existing plans/global.<tag->draft.md"
    )
    ap.add_argument("--model", default=settings.planner_model, help="model for --single or --layer runs")
    ap.add_argument("--draft-model", default=settings.planner_model)
    ap.add_argument("--verify-model", default=settings.planner_model)
    ap.add_argument(
        "--blender", default=settings.blender_bin, help="blender executable for the spike lab"
    )
    ap.add_argument(
        "--max-turns", type=int, default=None, help="turn cap per pass (default: 24 for --layer, 100 globally)"
    )
    ap.add_argument("--tag", default=None,
                    help="isolate plan output and its active-run plan-lab artifacts")
    ap.add_argument(
        "--until-clean",
        action="store_true",
        help="after the two passes, run the deterministic plan gate and "
        "repair until it clears, stalls, or hits --max-rounds",
    )
    ap.add_argument("--max-rounds", type=int, default=3, help="repair rounds for --until-clean (default 3)")
    ap.add_argument(
        "--promote-run",
        metavar="RUN_ID",
        help="model-free: revalidate and publish a retained gate-clean planning candidate",
    )
    args = ap.parse_args()

    if args.unit and not args.layer:
        ap.error("--unit requires --layer")
    if args.layer and (args.single or args.verify_only or args.until_clean or args.tag):
        ap.error("--layer is a dedicated JIT pass; do not combine it with global-pass flags")
    if args.rematerialize and not (args.layer and args.owner and args.trigger and args.evidence):
        ap.error(
            "--rematerialize replaces published authority: it needs --layer, --owner, "
            "--trigger, and at least one --evidence"
        )
    if args.promote_run and (
        args.layer or args.single or args.verify_only or args.until_clean or args.tag
    ):
        ap.error("--promote-run is a dedicated model-free transaction")

    shot = load_shot(args.folder)
    command = "plan-layer" if args.layer else ("plan-promote" if args.promote_run else "plan")
    loop_result: PlanLoopResult | None = None
    with run_artifacts.invocation(shot.folder, command, shot_id=shot.id) as layout:
        if args.promote_run:
            from vfx_harness.orchestration.plan_authority import promote_candidate

            bundle, gate_result, workspace = promote_candidate(
                shot.folder, args.promote_run, layout
            )
            plan_path = workspace / "plans" / "global.md"
            loop_result = PlanLoopResult(
                plan_path,
                gate_result.publishable_outcome,
                0,
                plan_pointer="plans/current.json",
                plan_bundle=bundle.root.relative_to(shot.folder).as_posix(),
                plan_content_hash=bundle.content_hash,
            )
            log(f"promoted candidate from run {args.promote_run}")
            log(f"plan authority → plans/current.json ({bundle.content_hash[:16]})", 1)
        elif args.layer:
            plan_path = anyio.run(
                lambda: generate_layer_plan(
                    args.folder,
                    args.layer,
                    unit_id=args.unit,
                    model=args.model,
                    blender=args.blender,
                    max_turns=args.max_turns or max(24, settings.plan_max_turns // 4),
                    rematerialize=(
                        (args.owner, args.trigger, list(args.evidence), args.discard_accepted)
                        if args.rematerialize
                        else None
                    ),
                )
            )
        elif args.single:
            plan_path = anyio.run(
                lambda: generate_plan(
                    args.folder, model=args.model, blender=args.blender,
                    max_turns=args.max_turns or settings.plan_max_turns, tag=args.tag
                )
            )
        elif args.until_clean:
            loop_result = anyio.run(
                lambda: generate_plan_until_clean(
                    args.folder,
                    draft_model=args.draft_model,
                    verify_model=args.verify_model,
                    blender=args.blender,
                    max_turns=args.max_turns or settings.plan_max_turns,
                    tag=args.tag,
                    verify_only=args.verify_only,
                    max_rounds=args.max_rounds,
                )
            )
            plan_path = loop_result.path
        else:
            plan_path = anyio.run(
                lambda: generate_plan_two_pass(
                    args.folder,
                    draft_model=args.draft_model,
                    verify_model=args.verify_model,
                    blender=args.blender,
                    max_turns=args.max_turns or settings.plan_max_turns,
                    tag=args.tag,
                    verify_only=args.verify_only,
                )
            )
        if args.layer:
            log(f"unit plan ready: {plan_path}")
            validate_work_unit_plan_authority(shot.folder, plan_path)
            if is_selected_bundle_member(shot.folder, plan_path):
                log("provenance → verified member of the selected immutable bundle")
            else:
                log(f"provenance → {plan_path.with_name(plan_path.name + '.authority.json')}")
        else:
            log(f"wrote {plan_path}")
            if loop_result is not None and loop_result.clean:
                log("provenance → content-addressed member of the published plan bundle")
            else:
                log("provenance → not published for this unaccepted candidate")
        if loop_result is not None and not loop_result.clean:
            raise PlanGateFailure(loop_result)
        if loop_result is not None:
            layout.terminal_metadata.update(
                {
                    "outcome": loop_result.outcome,
                    "blocking_count": loop_result.blocking_count,
                    "plan_gate_report": "reports/plan_gate.json",
                    **({"plan_pointer": loop_result.plan_pointer} if loop_result.plan_pointer else {}),
                    **({"plan_bundle": loop_result.plan_bundle} if loop_result.plan_bundle else {}),
                    **(
                        {"plan_content_hash": loop_result.plan_content_hash}
                        if loop_result.plan_content_hash
                        else {}
                    ),
                }
            )


if __name__ == "__main__":
    main()
