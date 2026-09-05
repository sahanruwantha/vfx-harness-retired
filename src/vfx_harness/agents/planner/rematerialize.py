"""Stage 2 — the PLAN harness."""

from __future__ import annotations

import json
from pathlib import Path

from claude_agent_sdk import query

from vfx_harness.agents.model_stream import with_idle_deadline
from vfx_harness.agents.plan_guardrails import planner_hooks
from vfx_harness.agents.plan_tools import build_plan_tools
from vfx_harness.agents.planner import rematerialization_evidence
from vfx_harness.agents.planner.budget import materialization_turn_budget
from vfx_harness.agents.planner.kickoff import (
    _materialization_kickoff,
    _with_target_feedback,
)
from vfx_harness.agents.planner.materialization_stop import publish_materialization_stop
from vfx_harness.agents.planner.pkg import planner_package
from vfx_harness.agents.planner.types import MATERIALIZATION_DENIED_TOOLS, _phase_tools
from vfx_harness.agents.resilience import AgentSessionFailure, result_signal, run_session
from vfx_harness.agents.sdk_options import sdk_options
from vfx_harness.infrastructure.config import DEFAULT_EXECUTION_MODEL
from vfx_harness.knowledge.recipes import build_recipe_tools
from vfx_harness.observability import costlog, run_artifacts, transcript
from vfx_harness.observability.log import log, log_message
from vfx_harness.orchestration import plan_authority, unit_state
from vfx_harness.orchestration.authority_capsule_resolution import (
    selected_layer_capsule_digest,
)
from vfx_harness.orchestration.authority_selection import (
    ResolvedSelectedAuthority,
    resolve_selected_authority,
)
from vfx_harness.orchestration.authority_selection_heads import (
    AuthoritySelectionHeadError,
    read_authority_selection_heads,
)
from vfx_harness.orchestration.authority_selection_transaction import (
    AuthoritySelectionConflict,
    authority_selection_lock,
    require_matching_authority_selection_token,
)
from vfx_harness.orchestration.jit_materialization import (
    MATERIALIZATION_SCHEMA,
    inspect_materialization,
    materialization_finalization_current,
    publish_materialization,
    revert_materialization,
    seed_materialization_candidate,
)
from vfx_harness.orchestration.layer_outcome_paths import layer_identity_segment
from vfx_harness.orchestration.unit_state_lock import unit_state_lock


def _owned_requirement_count(layers_path: Path, layer_id: str) -> int:
    """Owned requirements of one sparse layer row; the budget input, never the design."""

    document = json.loads(Path(layers_path).read_text(encoding="utf-8"))
    for row in document.get("layers") or []:
        if isinstance(row, dict) and str(row.get("id")) == layer_id:
            return len((row.get("jit") or {}).get("owned_requirements") or [])
    return 0


async def _materialize_deferred_layer(
    shot,
    layer,
    *,
    model: str,
    blender: str,
    max_turns: int,
    replacing: str | None = None,
    overlay_root: str | Path | None = None,
    selected_authority: ResolvedSelectedAuthority | None = None,
) -> None:
    """Close one layer's owned requirements with concrete authority, then select its view.

    ``overlay_root`` is an unpublished reverted overlay used as the remat design base.
    Publication is the only select; a crash must not have already moved the live pointer.
    """

    selected_authority = (
        resolve_selected_authority(shot.folder)
        if selected_authority is None
        else selected_authority
    )
    if selected_authority.plan is None:
        raise ValueError("cannot materialize without selected global plan authority")
    bundle = selected_authority.plan.bundle
    owned_requirements = _owned_requirement_count(bundle.root / "layers.json", str(layer.id))
    max_turns = materialization_turn_budget(max_turns, owned_requirements)
    layout = run_artifacts.ensure(shot.folder, command="plan-layer")
    identity_segment = layer_identity_segment(str(layer.id))
    target = layout.scratch / f"jit-{identity_segment}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    rel_target = target.relative_to(shot.folder).as_posix()
    seed_materialization_candidate(
        bundle.root,
        target,
        layer_id=str(layer.id),
        bundle_hash=bundle.content_hash,
        base_selection=selected_authority.selection_token,
    )

    def _validate_target() -> list[str]:

        # Mirror publication EXACTLY: the base must be the view-resolved layers, not the
        # bundle's sparse rows — other layers' materialized declarations (ADR-0007
        # dressable grants) live only in their overlays, and the sparse base made the
        # write hook refuse dresses the publication path would accept (run bm9og09xw).
        try:
            def _base(name: str) -> Path:
                if overlay_root is None:
                    return selected_authority.artifact_paths[name]
                path = Path(overlay_root) / name
                if path.is_symlink() or not path.is_file():
                    raise OSError(
                        f"materialization overlay base is missing real artifact {name}"
                    )
                return path

            findings, _materialized = inspect_materialization(
                bundle.root,
                target,
                expected_bundle_hash=bundle.content_hash,
                shot_folder=shot.folder,
                base_layers_path=_base("layers.json"),
                base_scene_checks_path=_base("scene_checks.json"),
                resolutions_path=shot.folder / "state" / "plan-resolutions.jsonl",
                base_requirements_path=_base("requirements.json"),
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
closes. If a later cross-unit finding proves a staged decomposition wrong, call
`unstage_materialization_unit` in reverse dependency order; never replace
`/layer/stages` through `patch_materialization`. A unique source mesh may set
`construction.route` generate only after `mint_refobs` on a refs/ crop (not a whole
frame); retrieve is not wired. After all units are
staged, call `finalize_materialization`; repair its complete findings with
`patch_materialization`, then finalize again. The completed candidate must contain non-empty
bounded stages and close every
globally owned requirement exactly once. Every declared structural evidence domain needs one or
more same-domain contract ids. Every image/human domain not paid by a candidate-bound contract
needs an explicit provisional decision carrying `statement` and `decision_strength`; mixed-domain
requirements therefore bind contracts AND a decision in the same row. The harness retains this
domain-to-binding map in the selected view and refuses partial coverage. Preserve global layer
structure exactly. Mutated roles must stay inside reserved namespaces. Every scene contract must be
required evidence of a materialized producing claim.
A unit that must ASSIGN materials to another layer's geometry declares `mutates.dresses`:
selectors the OWNING layer's row lists under `dressable` (granted by the owner, never
taken; assignment-only authority — geometry stays protected by the owner's contracts).
Same-layer mutation roles cannot be dressed: drop those `dresses` and assign through this
unit's own mutation or material_roles, or move look to a later layer. This layer's
`dressable` / `layer_updates.dressable` grants downstream layers only (HIR-0161).
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
Each stage declares `provides` from the exact layer-specific enum compiled into the
staging tool. Declare `camera` only when sparse global authority grants it, and
`geometry` only when that enum permits rendered form. Mesh metrics (smooth_fraction,
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
`finalize_materialization` again: it applies the exact deterministic gate to the resulting
consumer view and attests only that clean candidate revision. It is the sole terminal action;
do not finish after `patch_materialization` says VALIDATION PASSED. A gate finding fixed here
costs one patch instead of a retracted generation. Do not edit
global authority, create unit state, write prose, or write another file."""
    kickoff = _materialization_kickoff(
        shot.folder,
        layer,
        bundle,
        rel_target,
        replacing,
        overlay_root=overlay_root,
        selected_authority=selected_authority,
    )
    lab_dir = layout.scratch / "plan-lab" / f"{identity_segment}-materialize"
    pserver, pnames = build_plan_tools(
        shot.folder,
        blender=blender,
        lab_dir=lab_dir,
        measure_ref_paths=tuple(ref for _frame, ref in layer.judges),
        enabled_tools=frozenset(
            {
                "measure_ref",
                "spike",
                "ask_supervisor",
                "evidence_vocabulary",
                "escalate_vocabulary_gap",
                "stage_materialization_unit",
                "unstage_materialization_unit",
                "mint_refobs",
                "materialization_status",
                "finalize_materialization",
                "patch_materialization",
            }
        ),
        candidate_materialization=target,
        overlay_root=overlay_root,
    )
    rserver, rnames = build_recipe_tools()
    materialization_tools = _phase_tools(
        pnames,
        "measure_ref",
        "spike",
        "ask_supervisor",
        "evidence_vocabulary",
        "escalate_vocabulary_gap",
        "stage_materialization_unit",
        "unstage_materialization_unit",
        "mint_refobs",
        "materialization_status",
        "finalize_materialization",
        "patch_materialization",
    )
    options = sdk_options(
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
        async for message in with_idle_deadline(
            query(prompt=kickoff, options=options),
            label="materialization",
        ):
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
            succeeded=lambda: materialization_finalization_current(
                shot.folder,
                target,
                bundle_hash=bundle.content_hash,
            ),
            label=f"materialize layer {layer.id}",
            accept_max_turns_if_succeeded=True,
        )
    except AgentSessionFailure as exc:
        log(f"! materialize session died: {str(exc)[:200]}")
        transcript.event("died", error=str(exc)[:2000])
        envelope = publish_materialization_stop(
            layout,
            bundle=bundle,
            layer_id=str(layer.id),
            candidate=target,
            overlay_root=overlay_root,
        )
        raise run_artifacts.TypedStop(
            3, envelope, terminal_cause="materialization_failed"
        ) from exc
    finally:
        transcript.unbind()
        costlog.unbind()
    try:
        publish_materialization(shot.folder, target, overlay_root=overlay_root)
    except (OSError, TypeError, ValueError, plan_authority.PlanPublicationError) as exc:
        envelope = publish_materialization_stop(
            layout,
            bundle=bundle,
            layer_id=str(layer.id),
            candidate=target,
            overlay_root=overlay_root,
        )
        raise run_artifacts.TypedStop(
            3, envelope, terminal_cause="materialization_failed"
        ) from exc
    log(f"deferred layer {layer.id} materialized against bundle {bundle.content_hash[:12]}")


DRAFT_MODEL = DEFAULT_EXECUTION_MODEL
# Draft and verify deliberately share the configured planner model. Their independence
# comes from distinct sessions and an adversarial contract, not from pretending two calls
# to one model are statistically independent. CLI flags can still create a mixed-model
# lane when an experiment needs it.
VERIFY_MODEL = DEFAULT_EXECUTION_MODEL
MODEL = VERIFY_MODEL  # single-pass default


async def _rematerialize_layer(shot, layer, authority: tuple[str, str, list[str], bool], *, model, blender, max_turns):
    """Replace one materialized layer through the atomic authority-state publisher.

    Materialization is a decision, and a decision proven wrong must be replaceable —
    layer 1 of run 20260823T154920Z shipped defective contracts, proxied claims, and a
    unit whose script escaped its own scope. This does NOT add a supersession authority:
    `publish_materialization` owns pointer selection and every affected durable state
    move in one transition. Matching semantic capsules retain accepted checkpoints;
    changed and downstream-invalidated units retire in that same commit.
    """

    _owner, trigger, evidence, discard_accepted = authority
    layer_id = str(layer.id)
    base_authority = resolve_selected_authority(shot.folder)
    if base_authority.plan is None:
        raise ValueError("cannot rematerialize without selected global plan authority")
    selected_layers = planner_package().load_layers(
        shot,
        replacing_layer_id=layer_id,
        selected_authority=base_authority,
    )
    try:
        selected_layer = selected_layers[layer_id]
    except KeyError as exc:
        raise ValueError(f"selected authority has no layer {layer_id!r}") from exc
    if selected_layer != layer:
        raise ValueError(
            f"rematerialization layer {layer_id} is stale relative to selected authority"
        )
    state = unit_state.load(shot.folder, layer_id)
    accepted = sorted(uid for uid, row in (state.get("units") or {}).items() if row.get("status") == "passed")
    if accepted and discard_accepted:
        log(
            f"discard-accepted: accepted unit(s) {', '.join(accepted)} may retire "
            "if the replacement orphans them or the replan base is unusable",
            1,
        )
    elif accepted:
        log(
            f"accepted unit(s) {', '.join(accepted)} retain digest-matched checkpoints, "
            "but plan-bound completion authority reopens unless reconciled",
            1,
        )

    old_units = layer.stages
    bundle = base_authority.plan.bundle
    deferred = planner_package().load_layers_from_path(bundle.root / "layers.json")[layer_id]
    log(
        f"re-materializing layer {layer_id}: replacing {len(old_units)} unit(s) "
        f"({', '.join(u.id for u in old_units) or 'none'}) — {trigger}"
    )
    # Design against global authority, not against the view being replaced: otherwise
    # the discarded register (where this layer's owned requirements were already
    # resolved) is the base, and the replacement trips owned-means-owed.

    overlay = revert_materialization(
        shot.folder,
        layer_id,
        select=False,
        selected_authority=base_authority,
    )
    if overlay is not None:
        log(
            f"designing replacement against unpublished overlay {overlay.name}; live pointer stays until publication",
            1,
        )
    # The materializer designs against what the discarded view could not satisfy, not
    # only against the operator's sentence about it: a replacement camera that never saw
    # the geometry finding re-authors the same defect (HIR-0191).
    evidence_block = rematerialization_evidence.replacement_evidence_block(
        shot.folder, tuple(evidence)
    )
    await planner_package()._materialize_deferred_layer(
        shot,
        deferred,
        model=model,
        blender=blender,
        max_turns=max_turns,
        replacing=f"{trigger}\n\n{evidence_block}" if evidence_block else trigger,
        overlay_root=overlay,
        selected_authority=base_authority,
    )
    published_authority = resolve_selected_authority(shot.folder)
    try:
        with authority_selection_lock(shot.folder, exclusive=False):
            require_matching_authority_selection_token(
                published_authority.selection_token,
                read_authority_selection_heads(shot.folder).token,
            )
            refreshed = planner_package().load_layers(
                shot,
                selected_authority=published_authority,
            )[layer_id]
            expected_digest = selected_layer_capsule_digest(
                shot.folder,
                layer_id,
                published_authority,
            )
            with unit_state_lock(shot.folder, layer_id, exclusive=False):
                refreshed_state = unit_state.load(shot.folder, layer_id)
                if not refreshed_state:
                    raise ValueError(
                        "materialization publication did not create authority-bound "
                        f"work-unit state for layer {layer_id}"
                    )
                unit_state.validate_current(
                    refreshed_state,
                    layer_id,
                    refreshed.stages,
                )
                if refreshed_state.get("plan_hash") != expected_digest:
                    raise ValueError(
                        f"rematerialized layer {layer_id} state does not bind its "
                        "semantic capsule"
                    )
    except (AuthoritySelectionConflict, AuthoritySelectionHeadError) as exc:
        raise ValueError(
            "selected authority changed before rematerialization state verification"
        ) from exc
    log(
        f"atomic authority-state transition selected {len(refreshed.stages)} unit(s) "
        f"for layer {layer_id}",
        1,
    )
    return refreshed
