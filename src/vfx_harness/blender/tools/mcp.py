"""Assemble the Blender MCP tool server from clustered registrations."""

from __future__ import annotations

from pathlib import Path

import anyio
from claude_agent_sdk import create_sdk_mcp_server

from vfx_harness.blender.black_frame_report import effective_volume_span, same_density, summarize_black_placement_search
from vfx_harness.blender.session import BlenderError, BlenderSession
from vfx_harness.blender.tools.compare import register_compare
from vfx_harness.blender.tools.contracts import register_contracts
from vfx_harness.blender.tools.guards import _black_search_stop_message, _pending_black_frame_probe
from vfx_harness.blender.tools.images import _load
from vfx_harness.blender.tools.inspect import register_inspect
from vfx_harness.blender.tools.misc import register_misc
from vfx_harness.blender.tools.mutate import register_mutate
from vfx_harness.blender.tools.payment import _capture_image_artifact, _payment_eligible_candidate
from vfx_harness.blender.tools.probe import register_probe
from vfx_harness.blender.tools.render import register_render
from vfx_harness.blender.tools.reports import SERVER_NAME, _layer_feedback_policy
from vfx_harness.evidence.metrics import look_vector


def build_blender_tools(
    session: BlenderSession,
    assets_dir: str | Path | None = None,
    shot_dir: str | Path | None = None,
    layer_id: str | None = None,
    comparison_state: dict | None = None,
    feedback_groups: list[str] | None = None,
    mutation_roles: tuple[str, ...] | None = None,
    scope_baseline: set[str] | None = None,
    unit_scope: dict | None = None,
):
    """Wire the warm session as SDK tools. `assets_dir` enables `import_asset`;
    `shot_dir` enables `compare_frame` to resolve reference paths (e.g. refs/…).

    `feedback_groups` carries the active unit's DECLARED look capability resolved to
    metric families. When present it IS the policy; the layer-axis derivation is the
    legacy path for schema-4 layers with no declaring work unit."""
    assets_dir = Path(assets_dir) if assets_dir else None
    shot_dir = Path(shot_dir) if shot_dir else None
    comparison_state = comparison_state if comparison_state is not None else {"round": 1}
    comparison_locks: dict = {}
    feedback_policy = (
        {
            "axes": [],
            "groups": sorted(feedback_groups),
            "look_actions": bool(feedback_groups),
            "source": "declared_unit_capabilities",
        }
        if feedback_groups is not None
        else _layer_feedback_policy(shot_dir, layer_id)
    )

    async def _call(cmd, **args):
        return await anyio.to_thread.run_sync(lambda: session.call(cmd, **args))

    async def _black_frame_note(rendered: dict) -> str:
        """Return typed scene causes only for a nearly black look render."""
        if str(rendered.get("mode")) not in {"draft", "eevee"}:
            return ""

        metrics = look_vector(_load(rendered["image_path"]))
        if float(metrics.get("black_pct", 0.0)) <= 85.0:
            return ""
        try:
            context = await _call("black_context", frame=int(rendered["frame"]))
        except BlenderError as exc:
            return f"\n⚠ black-frame scene diagnosis unavailable: {str(exc)[:100]}"
        frame = int(rendered["frame"])
        clip_end = context.get("camera_clip_end")

        sampled_span = effective_volume_span(
            clip_end,
            context.get("volumetric_start"),
            context.get("volumetric_end"),
        )
        high_rows = [
            row
            for row in context.get("volume_rows") or []
            if not row.get("linked")
            and row.get("density") is not None
            and sampled_span is not None
            and float(row["density"]) * float(sampled_span) >= 1.0
            and str(row.get("role") or "").strip()
        ]
        if high_rows:
            row = max(high_rows, key=lambda item: float(item["density"]))
            role = str(row["role"])
            density = float(row["density"])
            probed = comparison_state.setdefault("world_density_probes", {}).get(f"{role}@{frame}", [])

            already_measured = any(same_density(value, density) for value in probed)
            if not already_measured:
                comparison_state["black_frame_required_probe"] = {
                    "role": role,
                    "frame": frame,
                    "density": density,
                }
        text = str(context.get("text") or "").strip()
        if high_rows and already_measured:
            # The worker cause card cannot see session probe history. Retire a stale
            # prescription explicitly; concurrent frame renders must not resurrect it.
            text = "\n".join(line for line in text.splitlines() if "NEXT MEASUREMENT:" not in line)
            _pending_black_frame_probe(comparison_state)
            probe_key = f"{role}@{frame}"
            diagnosis = str((comparison_state.get("world_density_probe_diagnoses") or {}).get(probe_key, "")).strip()
            text += "\n  " + (
                diagnosis
                if diagnosis
                else "DENSITY ALREADY MEASURED at this frame; repeating the same "
                "sweep is not a legal next step. Test a different causal variable."
            )
            if diagnosis.startswith("DENSITY HYPOTHESIS CLOSED"):

                for light in context.get("lights") or []:
                    light_role = str(light.get("role") or "").strip()
                    distance = light.get("camera_distance")
                    if not light_role or distance is None:
                        continue
                    search_key = f"{light_role}@{frame}"
                    trials = comparison_state.setdefault("black_placement_trials", {}).setdefault(search_key, [])
                    trials.append(
                        {
                            "camera_distance": distance,
                            "mean": metrics.get("exposure_mean", 0.0),
                            "black_pct": metrics.get("black_pct", 0.0),
                        }
                    )
                    closure = summarize_black_placement_search(trials)
                    if closure and comparison_state.get("scene_contracts_passed"):
                        contract_ids = [
                            str(card.get("id"))
                            for card in comparison_state.get("image_debts") or []
                            if int(card.get("frame") or -1) == frame and card.get("id")
                        ]
                        comparison_state["black_frame_search_exhausted"] = {
                            "frame": frame,
                            "role": light_role,
                            "contract_ids": contract_ids,
                            "reason": closure,
                        }
                        text += "\n  " + closure
        return "\n" + text if text else ""

    def _black_search_stop() -> str:
        return _black_search_stop_message(comparison_state)

    def _register_candidate(rendered: dict) -> str | None:
        if not shot_dir or not comparison_state.get("image_debts"):
            return None
        # A runtime payment must be directly comparable with the harness-captured
        # adversary.  Do not mint opaque handles for diagnostic previews: they
        # cannot pass the v2 provenance/settings check and advertising them teaches
        # the builder a dead-end action.
        if not _payment_eligible_candidate(rendered):
            return None
        record = _capture_image_artifact(
            shot_dir=shot_dir,
            source=rendered["image_path"],
            frame=int(rendered["frame"]),
            mode=str(rendered["mode"]),
            scale=float(rendered.get("scale", 0.5)),
            resolution=rendered.get("resolution"),
            role="live_candidate",
            unit_id=str(comparison_state.get("unit_id") or "unit"),
            parent_chain_hash=str(comparison_state.get("parent_chain_hash") or ""),
        )
        comparison_state.setdefault("image_artifacts", {})[record["handle"]] = record
        return str(record["handle"])

    closed = {
        "session": session,
        "_call": _call,
        "comparison_state": comparison_state,
        "comparison_locks": comparison_locks,
        "shot_dir": shot_dir,
        "layer_id": layer_id,
        "assets_dir": assets_dir,
        "feedback_policy": feedback_policy,
        "mutation_roles": mutation_roles,
        "scope_baseline": scope_baseline,
        "unit_scope": unit_scope,
        "_black_frame_note": _black_frame_note,
        "_black_search_stop": _black_search_stop,
        "_register_candidate": _register_candidate,
    }
    run_bpy = register_mutate(**closed)
    unit_scope_tool, inspect_scene, inspect_nodes, list_keyframes = register_inspect(**closed)
    render_frame, inspect_view, render_pass = register_render(**closed)
    check_scene, contract_result = register_contracts(**closed)
    diff_frames, verify_change, compare_frame, render_frames, import_asset = register_compare(**closed)
    probe_control = register_probe(**closed)
    script_map, find_in_script, worklist, cannot_express_in_scope, measure_regions, propose_checks = register_misc(
        **closed
    )
    tools = [
        run_bpy,
        unit_scope_tool,
        inspect_scene,
        inspect_nodes,
        list_keyframes,
        render_frame,
        inspect_view,
        render_frames,
        render_pass,
        check_scene,
        contract_result,
        diff_frames,
        verify_change,
        probe_control,
    ]
    if shot_dir is not None:
        tools.append(compare_frame)
    if assets_dir is not None and str(
        ((unit_scope or {}).get("construction") or {}).get("route") or "procedural"
    ) != "generate":
        tools.append(import_asset)
    tools = [*tools, script_map, find_in_script, worklist, cannot_express_in_scope, measure_regions, propose_checks]
    server = create_sdk_mcp_server(name=SERVER_NAME, version="0.1.0", tools=tools)
    names = [f"mcp__{SERVER_NAME}__{t.name}" for t in tools]
    return server, names
