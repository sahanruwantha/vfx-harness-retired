"""Agent SDK tools for the PLAN harness — scene forensics + the spike lab."""

from __future__ import annotations

import json
from collections.abc import Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

import anyio
from claude_agent_sdk import tool

import vfx_harness.orchestration.jit_materialization.gate_evidence as gate_evidence
from vfx_harness.agents.plan_tools.media import _text
from vfx_harness.domain.image_debts import payable_image_property_kinds
from vfx_harness.domain.judgment_debts import (
    JUDGMENT_DEBT_CLAIM_KINDS,
    JUDGMENT_DEBT_PROPERTIES,
    OBSERVATION_MEDIA,
    RENDERED_CARRIER_FAMILIES,
)
from vfx_harness.domain.work_units import (
    clustered_mutation_dialect,
    compile_clustered_mutation,
    compile_clustered_mutation_roles,
    work_unit_authoring_schema,
)
from vfx_harness.domain.work_units.parsing import STAGEABLE_CLAIM_AUTHORITIES
from vfx_harness.evaluation import plan_gate
from vfx_harness.evidence.checks import METRICS
from vfx_harness.orchestration.authority_selection import (
    ResolvedSelectedAuthority,
    SelectedAuthorityResolutionError,
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
    MaterializationInspection,
    apply_materialization_patches,
    finalize_materialization_candidate,
    inspect_materialization,
    materialization_candidate_revision,
    materialization_finalization_path,
    stage_materialization_unit,
    unstage_materialization_unit,
)
from vfx_harness.orchestration.jit_materialization.candidate import (
    load_materialization_candidate,
)
from vfx_harness.orchestration.jit_materialization.overlay_base import read_overlay_base
from vfx_harness.orchestration.jit_materialization.schema import (
    materialization_base_selection,
)
from vfx_harness.orchestration.plan_authority import prepare_consumer_view
from vfx_harness.orchestration.refobs import mint_refobs


@dataclass(frozen=True, slots=True)
class _MaterializationAuthorityInputs:
    selected: ResolvedSelectedAuthority
    bundle_root: Path
    bundle_hash: str
    base_layers: Path
    base_scene_checks: Path
    base_requirements: Path


def _require_same_selection(expected, observed, *, boundary: str) -> None:
    try:
        require_matching_authority_selection_token(expected, observed)
    except AuthoritySelectionConflict as exc:
        raise ValueError(f"{boundary}: {exc}") from exc



def _compiled_patch_value(pointer: str, value: object) -> object:
    """Compile a patched ``mutates`` written in the staging authoring dialect.

    ``stage_materialization_unit`` takes relative ``role_namespace``/``role_members``
    and compiles them; ``patch_materialization`` writes the same object. Passing the
    raw dialect through reached the durable parser, which held no such keys, so
    ``role_members`` was dropped and the unit landed mutating nothing (HIR-0217).
    Both tools now compile through one function.
    """
    if clustered_mutation_dialect(value) and pointer.endswith("/mutates"):
        return compile_clustered_mutation(value)  # type: ignore[arg-type]
    if isinstance(value, Mapping) and clustered_mutation_dialect(value.get("mutates")):
        return compile_clustered_mutation_roles(value)
    return value

def _materialization_authority_inputs(
    shot_folder: Path,
    candidate: Path,
    *,
    overlay_root: str | Path | None,
) -> _MaterializationAuthorityInputs:
    """Resolve every local-validation input from one exact selected generation."""

    try:
        selected = resolve_selected_authority(shot_folder)
    except SelectedAuthorityResolutionError as exc:
        raise ValueError(str(exc)) from exc
    if selected.plan is None or selected.assertion.effective_view is None:
        raise ValueError("materialization requires selected global plan authority")
    bundle = selected.plan.bundle
    payload = load_materialization_candidate(
        candidate,
        expected_bundle_hash=bundle.content_hash,
    )
    candidate_base = materialization_base_selection(payload)
    _require_same_selection(
        candidate_base,
        selected.selection_token,
        boundary="materialization candidate base selection is stale",
    )

    if overlay_root is None:
        try:
            layers = selected.artifact_paths["layers.json"]
            scene_checks = selected.artifact_paths["scene_checks.json"]
            requirements = selected.artifact_paths["requirements.json"]
        except KeyError as exc:
            raise ValueError(f"selected materialization authority omits {exc.args[0]!r}") from exc
    else:
        overlay = Path(overlay_root).resolve()
        overlay_bundle, overlay_base = read_overlay_base(overlay)
        if overlay_bundle != bundle.content_hash:
            raise ValueError("materialization overlay belongs to another global bundle")
        _require_same_selection(
            candidate_base,
            overlay_base,
            boundary="materialization overlay base selection is stale",
        )
        layers = overlay / "layers.json"
        scene_checks = overlay / "scene_checks.json"
        requirements = overlay / "requirements.json"

    return _MaterializationAuthorityInputs(
        selected=selected,
        bundle_root=bundle.root,
        bundle_hash=bundle.content_hash,
        base_layers=layers,
        base_scene_checks=scene_checks,
        base_requirements=requirements,
    )


@contextmanager
def _current_selection_guard(
    shot_folder: Path,
    selected: ResolvedSelectedAuthority,
) -> None:
    """Hold the shared head lock only across one candidate's durable replace."""

    with authority_selection_lock(shot_folder, exclusive=False):
        try:
            observed = read_authority_selection_heads(shot_folder)
            require_matching_authority_selection_token(
                selected.selection_token,
                observed.token,
            )
        except (AuthoritySelectionConflict, AuthoritySelectionHeadError) as exc:
            raise ValueError(f"materialization selection changed before candidate mutation: {exc}") from exc
        yield


def register_materialize_tools(**closed):

    shot_folder = closed["shot_folder"]
    _resolve = closed["_resolve"]
    _keep = closed["_keep"]
    candidate_materialization = closed["candidate_materialization"]
    overlay_root = closed["overlay_root"]
    materialization_write_lock = closed["materialization_write_lock"]
    layout = closed["layout"]
    ns = closed["ns"]
    materialization_revision_token = ns.materialization_revision_token
    materialization_axis_ids = ns.materialization_axis_ids
    materialization_layer_id = ns.materialization_layer_id
    materialization_allowed_provides = ns.materialization_allowed_provides
    materialization_requirement_statements = ns.materialization_requirement_statements

    @tool(
        "stage_materialization_unit",
        "Stage exactly one bounded work unit plus the scene contracts and requirement "
        "bindings it owns into the harness-seeded candidate. There is no path argument. "
        "Issue one staging call, wait for its result, then author the next unit. The unit "
        "parameter is a closed schema: use producer/interface_id/kind for consumes, one "
        "of none/keyframes/motion for temporal_evidence, and place composition_context "
        "under evaluation (evaluation.composition_context, never at the unit top level), "
        "omitting it unless it has non-empty frames plus exactly one source_unit or "
        "contract_ids. "
        "Every claim.axis enumerates the active layer's exact owned axis ids. "
        "Mutation roles are cluster-shaped: choose one two-token "
        "mutates.role_namespace and list only relative role_members (`$self` means the "
        "namespace tag). Absolute mutates.roles is not in the schema, so one unit cannot "
        "mix write namespaces. mutates.control_roles uses that same relative notation and "
        "its values must come from role_members; a unit with no role_members maps no "
        "control. "
        "script_spans contains exactly the identity-derived unit file enumerated by the "
        "schema under build/units/<layer>/; layer scripts and #fragments are invalid. "
        "After all units, call finalize_materialization. This is unpublished scratch "
        "state; duplicate unit, contract, or requirement ids are refused.",
        {
            "type": "object",
            "properties": {
                "unit": work_unit_authoring_schema(
                    image_property_kinds=payable_image_property_kinds(METRICS),
                    axis_ids=materialization_axis_ids,
                    layer_id=materialization_layer_id,
                    allowed_provides=materialization_allowed_provides,
                    clustered_mutation_roles=True,
                    # Authored claims cannot cite a qualification artifact: only the harness
                    # mints qualified judgment (provisional judgment debt). Three layer-2
                    # sessions of run 1b6807's lineage each paid a rejection to learn that;
                    # enumerate legality instead (HIR-0177).
                    stageable_authorities=STAGEABLE_CLAIM_AUTHORITIES,
                ),
                "scene_contracts": {"type": "array", "items": {"type": "object"}},
                "requirement_bindings": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "requirement_id": {"type": "string"},
                            "contract_ids": {
                                "type": "array",
                                "items": {"type": "string"},
                                "uniqueItems": True,
                            },
                            "decision": {
                                "type": "object",
                                "properties": {
                                    "statement": {
                                        "type": "string",
                                        "enum": sorted(set(materialization_requirement_statements.values())),
                                        "description": (
                                            "exact authored statement for this requirement; "
                                            "validation rejects a statement belonging to "
                                            "another id or any rewritten/meta proposition"
                                        ),
                                    },
                                    "decision_strength": {
                                        "type": "string",
                                        "enum": ["approved_start", "planner_start"],
                                    },
                                    "judgment": {
                                        "type": "object",
                                        "description": (
                                            "Typed qualitative debt subject and owner-time "
                                            "judgment scope. Required when this decision pays "
                                            "an image domain; omit it for human-only debt. The "
                                            "harness compiles activates_at from the selected "
                                            "DAG; do not submit a layer id."
                                        ),
                                        "properties": {
                                            "claim_kind": {
                                                "type": "string",
                                                "enum": sorted(JUDGMENT_DEBT_CLAIM_KINDS),
                                            },
                                            "property": {
                                                "type": "string",
                                                "enum": sorted(JUDGMENT_DEBT_PROPERTIES),
                                            },
                                            "fault_owner": {
                                                "type": "string",
                                                "minLength": 1,
                                                "description": (
                                                    "Exact work-unit id authorized to repair "
                                                    "this proposition if judgment falsifies it."
                                                ),
                                            },
                                            "subject_roles": {
                                                "type": "array",
                                                "items": {"type": "string", "minLength": 1},
                                                "minItems": 1,
                                                "uniqueItems": True,
                                            },
                                            "axes": {
                                                "type": "array",
                                                "items": {
                                                    "type": "string",
                                                    "enum": list(materialization_axis_ids or ()),
                                                },
                                                "minItems": 1,
                                                "uniqueItems": True,
                                            },
                                            "moments": {
                                                "type": "array",
                                                "items": {"type": "integer", "minimum": 1},
                                                "minItems": 1,
                                                "uniqueItems": True,
                                            },
                                            "carrier_families": {
                                                "type": "array",
                                                "items": {
                                                    "type": "string",
                                                    "enum": sorted(RENDERED_CARRIER_FAMILIES),
                                                },
                                                "minItems": 1,
                                                "uniqueItems": True,
                                            },
                                            "observation_medium": {
                                                "type": "string",
                                                "enum": sorted(OBSERVATION_MEDIA),
                                            },
                                            "lifecycle": {
                                                "type": "string",
                                                "enum": ["layer", "window", "persistent"],
                                            },
                                        },
                                        "required": [
                                            "claim_kind",
                                            "property",
                                            "fault_owner",
                                            "subject_roles",
                                            "axes",
                                            "moments",
                                            "carrier_families",
                                            "observation_medium",
                                            "lifecycle",
                                        ],
                                        "additionalProperties": False,
                                    },
                                },
                                "required": [
                                    "statement",
                                    "decision_strength",
                                ],
                                "additionalProperties": False,
                            },
                        },
                        "required": ["requirement_id"],
                        "anyOf": [
                            {"required": ["contract_ids"]},
                            {"required": ["decision"]},
                        ],
                        "additionalProperties": False,
                    },
                },
                "layer_updates": {
                    "type": "object",
                    "properties": {
                        "dressable": {"type": "array", "items": {"type": "string"}},
                    },
                    "additionalProperties": False,
                },
            },
            "required": ["unit", "scene_contracts", "requirement_bindings"],
            "additionalProperties": False,
        },
    )
    async def stage_materialization_unit_tool(args):
        nonlocal materialization_revision_token
        candidate = Path(candidate_materialization) if candidate_materialization else None
        if candidate is None:
            return _text(
                "stage_materialization_unit is only available during layer materialization",
                is_error=True,
            )

        try:
            compiled_unit = compile_clustered_mutation_roles(args.get("unit") or {})
            async with materialization_write_lock:
                authority = _materialization_authority_inputs(
                    shot_folder,
                    candidate,
                    overlay_root=overlay_root,
                )
                staged = await anyio.to_thread.run_sync(
                    lambda: stage_materialization_unit(
                        candidate,
                        unit=compiled_unit,
                        scene_contracts=args.get("scene_contracts") or [],
                        requirement_bindings=args.get("requirement_bindings") or [],
                        layer_updates=args.get("layer_updates"),
                        allowed_provides=materialization_allowed_provides,
                        expected_revision=materialization_revision_token,
                        shot_folder=layout.shot,
                        candidate_write_guard=lambda: _current_selection_guard(
                            shot_folder,
                            authority.selected,
                        ),
                        inspection=MaterializationInspection(
                            global_root=authority.bundle_root,
                            shot_folder=layout.shot,
                            expected_bundle_hash=authority.bundle_hash,
                            base_layers_path=authority.base_layers,
                            base_scene_checks_path=authority.base_scene_checks,
                            resolutions_path=shot_folder / "state" / "plan-resolutions.jsonl",
                            base_requirements_path=authority.base_requirements,
                        ),
                    )
                )
                materialization_revision_token = materialization_candidate_revision(candidate)
                payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            return _text(f"unit staging refused: {exc}", is_error=True)
        remaining = list(staged.remaining_findings)
        open_findings = (
            "\nOpen layer-level findings finalize will still refuse "
            f"({len(remaining)}; resolve them with the units that own them):\n- "
            + "\n- ".join(remaining[:12])
            if remaining
            else ""
        )
        return _text(
            f"STAGED unit {compiled_unit.get('id', '<missing>')}: "
            f"candidate now has {len(payload['layer']['stages'])} unit(s), "
            f"{len(payload['scene_contracts'])} contract(s), and "
            f"{len(payload['requirement_bindings'])} requirement binding(s). "
            "Stage the next independent unit, or call finalize_materialization."
            + open_findings
        )

    @tool(
        "unstage_materialization_unit",
        "Remove exactly one previously staged unit from unpublished materialization "
        "scratch when later validation proves the decomposition wrong. The locked, "
        "revision-checked transaction also removes contracts no surviving unit binds "
        "and prunes requirement bindings that become empty. It refuses while a "
        "surviving unit depends on or consumes the target; unstage in reverse dependency "
        "order or patch those exact references first. This never changes selected "
        "authority or durable work-unit state.",
        {
            "type": "object",
            "properties": {"unit_id": {"type": "string", "minLength": 1}},
            "required": ["unit_id"],
            "additionalProperties": False,
        },
    )
    async def unstage_materialization_unit_tool(args):
        nonlocal materialization_revision_token
        candidate = Path(candidate_materialization) if candidate_materialization else None
        if candidate is None:
            return _text(
                "unstage_materialization_unit is only available during layer materialization",
                is_error=True,
            )

        try:
            async with materialization_write_lock:
                authority = _materialization_authority_inputs(
                    shot_folder,
                    candidate,
                    overlay_root=overlay_root,
                )
                result = await anyio.to_thread.run_sync(
                    lambda: unstage_materialization_unit(
                        candidate,
                        unit_id=args.get("unit_id"),
                        expected_revision=materialization_revision_token,
                        shot_folder=layout.shot,
                        candidate_write_guard=lambda: _current_selection_guard(
                            shot_folder,
                            authority.selected,
                        ),
                    )
                )
                materialization_revision_token = materialization_candidate_revision(candidate)
                payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            return _text(f"unit unstaging refused: {exc}", is_error=True)
        return _text(
            f"UNSTAGED unit {result.unit_id}: removed contract ids "
            f"{list(result.removed_contract_ids)} and empty requirement bindings "
            f"{list(result.removed_requirement_ids)}; candidate now has "
            f"{len(payload['layer']['stages'])} unit(s), "
            f"{len(payload['scene_contracts'])} contract(s), and "
            f"{len(payload['requirement_bindings'])} requirement binding(s)."
        )

    @tool(
        "mint_refobs",
        "Crop a refs/ still into a generate-construction witness and return a stable "
        "refobs-* id. Call this before staging construction.witnesses. box is "
        "normalised [x0, y0, x1, y1] from the top-left; a whole-frame box is refused. "
        "The crop persists under shot-root state/refobs/. Text-only generate and "
        "prose isolate_regen are not witnesses.",
        {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "description": "relative refs/ path inside this workspace",
                    "minLength": 1,
                },
                "box": {
                    "type": "array",
                    "items": {"type": "number"},
                    "minItems": 4,
                    "maxItems": 4,
                    "description": "normalised [x0, y0, x1, y1] crop, origin top-left",
                },
            },
            "required": ["source", "box"],
            "additionalProperties": False,
        },
    )
    async def mint_refobs_tool(args):
        if not candidate_materialization:
            return _text(
                "mint_refobs is only available during layer materialization",
                is_error=True,
            )
        source_rel = str(args.get("source") or "").replace("\\", "/").lstrip("./")
        try:
            path = _resolve(source_rel)
            token = await anyio.to_thread.run_sync(
                lambda: mint_refobs(layout.shot, path, args.get("box"), source_rel=source_rel)
            )
        except (OSError, ValueError, TypeError) as exc:
            return _text(f"mint_refobs refused: {exc}", is_error=True)
        return _text(
            f"MINTED {token} from {source_rel}. Use this exact id in "
            "construction.witnesses on a mesh-family generate unit."
        )

    @tool(
        "materialization_status",
        "Return compact harness-owned progress for the seeded materialization candidate: "
        "staged unit ids and counts only. Use this instead of Read; raw candidate JSON is "
        "not a model context surface.",
        {"type": "object", "properties": {}, "additionalProperties": False},
    )
    async def materialization_status(args):
        candidate = Path(candidate_materialization) if candidate_materialization else None
        if candidate is None:
            return _text(
                "materialization_status is only available during layer materialization",
                is_error=True,
            )
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
            revision = materialization_candidate_revision(candidate)
            units = [
                str(row.get("id"))
                for row in ((payload.get("layer") or {}).get("stages") or [])
                if isinstance(row, dict)
            ]
            contracts = payload.get("scene_contracts") or []
            requirements = payload.get("requirement_bindings") or []
        except (OSError, ValueError, AttributeError, json.JSONDecodeError) as exc:
            return _text(f"materialization status unavailable: {exc}", is_error=True)
        return _text(
            json.dumps(
                {
                    "staged_units": units,
                    "unit_count": len(units),
                    "scene_contract_count": len(contracts),
                    "requirement_binding_count": len(requirements),
                    "revision": revision,
                    "session_revision_matches": revision == materialization_revision_token,
                }
            )
        )

    @tool(
        "finalize_materialization",
        "Validate the complete incrementally staged candidate against global authority "
        "and run the exact deterministic terminal gate against the post-publication "
        "consumer view. Call after every unit and owned requirement has been staged, "
        "and call it again after any patch. A clean result attests the exact candidate "
        "revision for publication; otherwise it returns all local JSON-pointer or gate "
        "findings for repair. This is the sole terminal materialization operation.",
        {"type": "object", "properties": {}, "additionalProperties": False},
    )
    async def finalize_materialization(args):
        candidate = Path(candidate_materialization) if candidate_materialization else None
        if candidate is None:
            return _text(
                "finalize_materialization is only available during layer materialization",
                is_error=True,
            )

        try:
            authority = _materialization_authority_inputs(
                shot_folder,
                candidate,
                overlay_root=overlay_root,
            )
            findings, _materialized = await anyio.to_thread.run_sync(
                lambda: inspect_materialization(
                    authority.bundle_root,
                    candidate,
                    expected_bundle_hash=authority.bundle_hash,
                    base_layers_path=authority.base_layers,
                    base_scene_checks_path=authority.base_scene_checks,
                    resolutions_path=shot_folder / "state" / "plan-resolutions.jsonl",
                    base_requirements_path=authority.base_requirements,
                )
            )
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            return _text(str(exc), is_error=True)
        if not findings:
            try:
                view = await anyio.to_thread.run_sync(
                    lambda: prepare_consumer_view(
                        layout,
                        selected_authority=authority.selected,
                    )
                )
                result = await anyio.to_thread.run_sync(
                    lambda: finalize_materialization_candidate(
                        shot_folder,
                        candidate,
                        view,
                        overlay_root=overlay_root,
                        selected_authority=authority.selected,
                    )
                )
            except (ValueError, OSError, json.JSONDecodeError) as exc:
                # An absent record is itself typed as a harness defect by the outer
                # boundary.  Preserve the original tool failure if evidence storage is
                # also unavailable; never infer repair authority from either exception.
                with suppress(OSError, RuntimeError, ValueError, json.JSONDecodeError):
                    gate_evidence.write_materialization_gate_evidence(
                        layout,
                        candidate=candidate,
                        bundle_digest=authority.bundle_hash,
                        layer_id=str(materialization_layer_id),
                        gate_issue="terminal_gate_execution_failed",
                    )
                return _text(f"terminal materialization gate failed: {exc}", is_error=True)
            result.shot = shot_folder.name
            gate_evidence.write_materialization_gate_evidence(
                layout,
                candidate=candidate,
                bundle_digest=authority.bundle_hash,
                layer_id=str(materialization_layer_id),
                gate_result=result,
            )
            # Scoped: a finding another layer owns belongs to that layer's own
            # transaction. This session cannot repair layer 1's camera authority, and
            # blocking on it burns turns against a constraint it has no scope to satisfy
            # (rematerialization 20260903T211552Z-8ad44d) (HIR-0189).
            owned = plan_gate.scoped_to_layer(result, str(materialization_layer_id))
            if not owned.clean:
                body = plan_gate.report(owned)
                repair = plan_gate.feedback(owned)
                return _text(
                    body + (f"\n\nREPAIR BRIEF\n{repair}" if repair else ""),
                    is_error=True,
                )
            attestation = materialization_finalization_path(candidate)
            return _text(
                f"FINALIZATION ATTESTED AND TERMINAL GATE CLEAN for {candidate.name} "
                f"at the current revision ({attestation.name}). The outer transaction "
                "may publish even if this call consumes the final model turn."
            )
        gate_evidence.write_materialization_gate_evidence(
            layout,
            candidate=candidate,
            bundle_digest=authority.bundle_hash,
            layer_id=str(materialization_layer_id),
            local_findings=findings,
        )
        return _text(
            "VALIDATION FAILED. Remaining findings:\n" + "\n".join(f"- {item}" for item in findings),
            is_error=True,
        )

    @tool(
        "patch_materialization",
        "Atomically set one or several RFC 6901 JSON Pointers on the candidate "
        "materialization file, then re-validate once. Group independent findings in "
        "`patches`; use `pointer` + `value` for one repair. Every value is JSON-encoded. "
        "A list token may be the row's stable id (`/scene_contracts/id=<row id>/hi`) "
        "instead of a guessed index; `-` appends. "
        "Cannot replace the document root. Returns VALIDATION PASSED or all remaining "
        "findings; if any pointer is invalid, no patch is written.",
        {
            "type": "object",
            "properties": {
                "pointer": {
                    "type": "string",
                    "description": "JSON Pointer such as /scene_contracts/2/owner_layer",
                },
                "value": {
                    "type": "string",
                    "description": "JSON-encoded replacement at that pointer",
                },
                "patches": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "pointer": {"type": "string"},
                            "value": {
                                "type": "string",
                                "description": "JSON-encoded replacement value",
                            },
                        },
                        "required": ["pointer", "value"],
                        "additionalProperties": False,
                    },
                },
            },
            "oneOf": [
                {"required": ["pointer", "value"]},
                {"required": ["patches"]},
            ],
            "additionalProperties": False,
        },
    )
    async def patch_materialization(args):
        nonlocal materialization_revision_token
        candidate = Path(candidate_materialization) if candidate_materialization else None
        if candidate is None:
            return _text(
                "patch_materialization is only available during layer materialization",
                is_error=True,
            )
        raw_patches = args.get("patches")
        if raw_patches is None:
            raw_patches = [{"pointer": args.get("pointer"), "value": args.get("value")}]
        patches: list[tuple[str, object]] = []
        try:
            for index, row in enumerate(raw_patches):
                pointer = str((row or {}).get("pointer") or "")
                if not pointer:
                    return _text(f"patches[{index}].pointer is required", is_error=True)
                value = json.loads(str((row or {}).get("value")))
                patches.append((pointer, _compiled_patch_value(pointer, value)))
        except (TypeError, json.JSONDecodeError) as exc:
            return _text(f"value must be JSON-encoded: {exc}", is_error=True)
        except ValueError as exc:
            return _text(str(exc), is_error=True)

        try:
            async with materialization_write_lock:
                authority = _materialization_authority_inputs(
                    shot_folder,
                    candidate,
                    overlay_root=overlay_root,
                )
                findings = await anyio.to_thread.run_sync(
                    lambda: apply_materialization_patches(
                        authority.bundle_root,
                        candidate,
                        patches,
                        expected_bundle_hash=authority.bundle_hash,
                        base_layers_path=authority.base_layers,
                        base_scene_checks_path=authority.base_scene_checks,
                        resolutions_path=(shot_folder / "state" / "plan-resolutions.jsonl"),
                        base_requirements_path=authority.base_requirements,
                        expected_revision=materialization_revision_token,
                        shot_folder=layout.shot,
                        candidate_write_guard=lambda: _current_selection_guard(
                            shot_folder,
                            authority.selected,
                        ),
                    )
                )
                materialization_revision_token = materialization_candidate_revision(candidate)
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            return _text(str(exc), is_error=True)
        if not findings:
            return _text(f"VALIDATION PASSED for {candidate.name}.")
        detail = "\n".join(f"- {item}" for item in findings)
        return _text(
            f"VALIDATION FAILED for {candidate.name}. Remaining findings:\n{detail}",
            is_error=True,
        )

    return (
        stage_materialization_unit_tool,
        unstage_materialization_unit_tool,
        mint_refobs_tool,
        materialization_status,
        finalize_materialization,
        patch_materialization,
    )
