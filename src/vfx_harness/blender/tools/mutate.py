"""Agent SDK tools over a warm BlenderSession.

`build_blender_tools(session)` returns an in-process MCP server and the list of
qualified tool names to put in `ClaudeAgentOptions.allowed_tools`. Render tools
return image content blocks, so the agent literally sees the frames it makes.
"""

from __future__ import annotations

import anyio
from claude_agent_sdk import tool

from vfx_harness.blender.black_frame_report import authored_density_values, same_density
from vfx_harness.blender.session import BlenderError
from vfx_harness.blender.tools.bbox_feasibility_gate import bbox_feasibility_block, record_bbox_failures
from vfx_harness.blender.tools.guards import (
    _pending_black_frame_probe,
    _run_bpy_instrument_hint,
    _schedule_override_without_keying,
    _scoped_renderer_write_error,
)
from vfx_harness.blender.tools.payment import _refresh_unpaid_image_debts, _run_bpy_write_family_error, _text
from vfx_harness.blender.tools.reports import (
    _ROLE_MANIFEST,
    _bound_static_frames,
    _deferred_subject_forecast_note,
    _scene_completion_state,
    _scope_offenders,
    _unpaid_image_debt_note,
    downstream_framing_note,
    followup_after_scene_contracts_pass,
)
from vfx_harness.evidence.scene_checks import functional_evidence, layer_evidence, load_rows
from vfx_harness.observability.log import log
from vfx_harness.observability.runlog import bump


async def _downstream_framing_probe(session, comparison_state: dict, contract_rows: list[dict]) -> str:
    """A camera unit sees after every mutation whether its deferred rows stay payable.

    Run 20260903T081518Z-9a32ab sealed a camera path under which no mass could satisfy the
    frame-1 height cap and frame-113 height floor the reference demands; the geometry
    layer discovered that by 46 rebuilds. The proxy solver answers it here, per later layer,
    with no scene object and no camera move (HIR-0184).
    """
    if not comparison_state.get("camera_provider"):
        return ""
    wanted = {str(item) for item in comparison_state.get("downstream_framing_ids") or []}
    if not wanted:
        return ""
    groups: dict[str, list[dict]] = {}
    for row in contract_rows:
        if str(row.get("id")) in wanted:
            groups.setdefault(str(row.get("activates_at") or ""), []).append(row)
    results: dict[str, dict] = {}
    for layer_id, rows in sorted(groups.items()):
        roles = sorted({str(role) for row in rows for role in row.get("roles") or []})
        payload = {
            "rows": [
                {key: row[key] for key in ("id", "kind", "frame", "op", "lo", "hi", "value", "tol") if key in row}
                for row in rows
            ],
            "roles": roles,
        }
        result = await anyio.to_thread.run_sync(lambda p=payload: session.check(kind="bbox_feasibility", **p))
        results[layer_id] = {
            "row_ids": [str(row.get("id")) for row in rows],
            "feasible": bool(result.get("feasible")),
            "binding": [str(item) for item in result.get("binding") or []],
            "box": result.get("best") or result.get("box") or {},
        }
    comparison_state["downstream_framing"] = results
    return downstream_framing_note(results)


def register_mutate(
    session,
    _call,
    comparison_state,
    comparison_locks,
    shot_dir,
    layer_id,
    assets_dir,
    feedback_policy,
    mutation_roles,
    scope_baseline,
    unit_scope,
    _black_frame_note,
    _black_search_stop,
    _register_candidate,
    selected_authority=None,
):
    @tool(
        "run_bpy",
        "Execute Python (with `bpy` in scope) against the live scene — the hands. "
        "Model, shade, key, set render config. Set `RESULT` to return JSON data; "
        "print()s are captured. Returns TIMING + scene-delta so you can feel cost. "
        "Pre-injected helpers (use them, don't hand-roll slow loops): "
        "bvfx_scatter_emissive(count,...) for light/greeble carpets (ONE instanced "
        "object, fast for 1000s); bvfx_volume(center,size,density,color,...) for a "
        "bounded volumetric domain (clouds/nebula/fog); bvfx_volumetric_world(...) for "
        "a tinted haze sky; bvfx_glare_bloom(...) for EEVEE-Next bloom; "
        "bvfx_vector_blur(...) for a wired Blender-5 compositor node; "
        "bvfx_light(...) for type-safe POINT/AREA/SPOT creation or conversion; "
        "bvfx_emission(name,color,strength). To DEBUG a material/world, use inspect_nodes "
        "instead of rendering repeatedly to guess. Tag every contract-facing datablock "
        "with bvfx_role(target,'material.floor.worn',owner_layer='2') — one dotted "
        "token per host, commas are not membership — and every shader/"
        "compositor control with bvfx_control(node,'control.orb.gain',owner_layer='2'); "
        "semantic roles survive renames and are the only supported contract interface.",
        {"type": "object", "properties": {"script": {"type": "string"}}, "required": ["script"]},
    )
    async def run_bpy(args):
        family_error = _run_bpy_write_family_error(str(args.get("script") or ""), unit_scope)
        if family_error:
            return _text(family_error, is_error=True)
        stop = _black_search_stop()
        if stop:
            return _text(stop, is_error=True)
        feasibility_block = bbox_feasibility_block(comparison_state)
        if feasibility_block:
            return _text(feasibility_block, is_error=True)
        required_probe = _pending_black_frame_probe(comparison_state)
        if isinstance(required_probe, dict):
            role = str(required_probe.get("role") or "")
            frame = int(required_probe.get("frame") or 0)
            density = float(required_probe.get("density") or 0.0)
            return _text(
                "BLOCKED: the last look render was nearly black with an unmeasured "
                f"World density {density:g} on {role!r} at f{frame}. Call the "
                "cause-card probe_control density sweep before another free-form "
                "scene mutation; a ceiling is not a target.",
                is_error=True,
            )
        if comparison_state.get("scene_contracts_passed") and shot_dir:

            active_ids = comparison_state.get("active_evidence_ids")
            schedule_rows = [
                row
                for row in load_rows(shot_dir, selected_authority)
                if row.get("kind") == "keyframe_schedule" and (active_ids is None or str(row.get("id")) in active_ids)
            ]
            protected_paths = {
                str(path)
                for row in schedule_rows
                for sample in row.get("samples") or []
                for path in (sample.get("values") or {})
            }
            violations = _schedule_override_without_keying(str(args.get("script") or ""), protected_paths)
            if violations:
                ids = [
                    str(row.get("id"))
                    for row in schedule_rows
                    if any(
                        str(path) in violations
                        for sample in row.get("samples") or []
                        for path in (sample.get("values") or {})
                    )
                ]
                protected = sorted(violations)
                return _text(
                    "BLOCKED: required exact schedule(s) already pass: "
                    + ", ".join(ids)
                    + ". This payload would disable or override protected animated path(s) "
                    + ", ".join(protected)
                    + " without keying a legal schedule. Legal next actions, in order: "
                    "re-key the protected schedule in this same payload — keyframe_insert on "
                    f"{protected[0] if protected else 'the protected path'} at every frame the "
                    "contract samples — and then interpolate, which is the same-transaction "
                    "rekeying this guard asks for; or scope the call so the schedule is not "
                    f"touched, bvfx_interp(host, mode=..., exclude_paths={protected!r}) or "
                    "data_paths=['rotation_euler']; or, if neither expresses the edit you "
                    "measured, call cannot_express_in_scope naming the schedule conflict. Do "
                    "not use authored state as a diagnostic, and do not mute or override a "
                    "passing schedule to observe it.",
                    is_error=True,
                )
        renderer_error = _scoped_renderer_write_error(str(args.get("script") or ""), mutation_roles)
        if renderer_error:
            return _text(renderer_error, is_error=True)

        proposed_densities = authored_density_values(str(args.get("script") or ""))
        if proposed_densities:
            tested = {
                float(value)
                for values in (comparison_state.get("world_density_probes") or {}).values()
                for value in values
            }
            unmeasured = [
                value
                for value in proposed_densities
                if tested and not any(same_density(value, prior) for prior in tested)
            ]
            if unmeasured:
                return _text(
                    "BLOCKED: World Density value(s) "
                    + ", ".join(f"{value:g}" for value in unmeasured)
                    + " were not measured by probe_control. Commit a tested value "
                    + "or include the new candidate in a density sweep first.",
                    is_error=True,
                )
        try:
            r = await _call("run", code=args["script"], transactional=True)
        except BlenderError as e:
            return _text(_run_bpy_instrument_hint(str(args.get("script") or ""), str(e)), is_error=True)
        # A successful script may have changed pixels even when this layer has no scene
        # completion contract. Never carry an earlier comparison verdict across it.
        comparison_state["pixel_contracts_passed"] = False
        comparison_state["mutation_serial"] = int(comparison_state.get("mutation_serial", 0)) + 1
        out = r.get("stdout", "")
        res = r.get("result")
        el, oa, va = r.get("elapsed_s"), r.get("objects_added"), r.get("verts_added")
        sc = r.get("scene", {})
        body = (out + (f"\nRESULT: {res}" if res is not None else "")).strip() or "ok"
        meta = (
            f"\n⏱ {el}s · +{oa} objects · +{va} verts · "
            f"scene now {sc.get('objects', '?')} objs / {sc.get('tris', '?')} tris"
        )
        warn = ""
        if isinstance(el, (int, float)) and el > 20:
            warn += (
                f"\n⚠ that took {el}s — too slow. Never create repeated elements in a "
                f"Python loop; use bvfx_scatter_emissive / instancing / bmesh."
            )
        if isinstance(oa, int) and oa > 200:
            warn += (
                f"\n⚠ +{oa} objects — collapse to ONE instanced mesh "
                f"(bvfx_scatter_emissive) instead of per-object creation."
            )
        # Scope is checked at canonical replay, which is AFTER the build spends its
        # whole budget: run 20260823T154920Z created camera, housing, tunnel and light
        # objects outside its declared roles and learned nothing until the end. Surface
        # the violation on the call that caused it, while the fix is one edit away.
        # Checked on EVERY successful call, not only when objects_added > 0: helpers
        # create objects through paths whose reported delta cannot be trusted, and the
        # cam_rig_spine build proved the point — two calls reported +2 objects, the live
        # check never spoke, and canonical replay found an untagged curve at the end.
        # Objects are re-examined until they are in scope, because a builder may create
        # first and tag second.
        if mutation_roles:
            try:
                manifest = (await _call("run", code=_ROLE_MANIFEST, journal=False)).get("result") or {}
                offenders = _scope_offenders(manifest, mutation_roles, scope_baseline)
                comparison_state["scope_offenders"] = list(offenders)
                if offenders:
                    log(f"scope: {len(offenders)} object(s) outside declared roles", 1)
                    warn += (
                        "\n⚠ SCOPE VIOLATION — this unit may only create objects in "
                        + ", ".join(mutation_roles)
                        + ": "
                        + "; ".join(offenders[:6])
                        + "\nCanonical replay rejects these deterministically. Delete "
                        "them or tag them with a role inside your declared scope."
                    )
            except BlenderError as exc:
                # A silent probe failure is the same lie as a silent violation.
                log(f"scope probe unavailable ({str(exc)[:70]})", 1)
        # Scene contracts are the live execution authority. Evaluate them immediately
        # after every mutation so convergence is a state transition, not a suggestion the
        # model may overlook for another 80 turns. Pixel checks still happen after the
        # required comparison render.
        contract_note = ""
        if shot_dir and layer_id:
            try:
                active_ids = comparison_state.get("active_evidence_ids")
                diagnostic_ids = set(comparison_state.get("diagnostic_evidence_ids") or [])
                scheduled_ids = None if active_ids is None else set(active_ids) | diagnostic_ids

                contract_rows = load_rows(shot_dir, selected_authority)
                frames = _bound_static_frames(
                    contract_rows,
                    scheduled_ids,
                    int(comparison_state.get("frame", 1)),
                )
                evidence = []
                for evidence_frame in frames:
                    evidence.extend(
                        await anyio.to_thread.run_sync(
                            lambda frame=evidence_frame: layer_evidence(
                                shot_dir,
                                str(layer_id),
                                frame=frame,
                                session=session,
                                selected_authority=selected_authority,
                            )
                        )
                    )
                if scheduled_ids is not None:
                    evidence = [row for row in evidence if str(row.get("id")) in scheduled_ids]
                evidence = list({str(row.get("id")): row for row in evidence}.values())
                diagnostic_evidence = [row for row in evidence if str(row.get("id")) in diagnostic_ids]
                active_evidence = [row for row in evidence if str(row.get("id")) not in diagnostic_ids]
                authoritative = [row for row in active_evidence if row.get("authoritative")]
                if authoritative and all(row.get("pass") for row in authoritative):

                    active_evidence.extend(
                        await anyio.to_thread.run_sync(
                            lambda: functional_evidence(
                                shot_dir,
                                str(layer_id),
                                session=session,
                                selected_authority=selected_authority,
                            )
                        )
                    )
                    if active_ids is not None:
                        active_evidence = [row for row in active_evidence if str(row.get("id")) in active_ids]
                    active_evidence = list({str(row.get("id")): row for row in active_evidence}.values())
                    authoritative = [row for row in active_evidence if row.get("authoritative")]
                state = _scene_completion_state(active_evidence, str(layer_id), active_ids)
                _refresh_unpaid_image_debts(
                    comparison_state,
                    shot_dir,
                    selected_authority=selected_authority,
                )
                authoritative = state["authoritative"]
                passed = [row for row in authoritative if row.get("pass")]
                failed = state["failures"]
                record_bbox_failures(comparison_state, failed)
                if authoritative:

                    bump("automatic_scene_contract_probe")
                    contract_note = f"\nAUTHORITATIVE SCENE CONTRACTS: {len(passed)}/{len(authoritative)} pass"
                    if state["missing"]:
                        # Unevaluated required SCENE evidence used to read as silence.
                        # Image-contract debts are a separate card (HIR-0048).
                        comparison_state["scene_contracts_passed"] = False
                        comparison_state["scene_interfaces_ready"] = False
                        contract_note += (
                            " · REQUIRED SCENE EVIDENCE NOT PRODUCED: "
                            + ", ".join(state["missing"][:6])
                            + "\n  These scene contracts are bound to required claims but "
                            "were never evaluated — usually a selector matching no object, "
                            "or a frame group that never ran. They cannot pass by absence."
                        )
                    if failed:
                        comparison_state["scene_interfaces_ready"] = False
                        comparison_state["scene_contracts_passed"] = False
                        comparison_state["current_scene_contracts_present"] = bool(state["current"])
                        contract_note += " · failing:\n" + "\n".join(
                            f"  {row.get('id', '?')}: {row.get('metric')}="
                            f"{row.get('value')} target {row.get('target')} — "
                            f"{row.get('definition', 'see scene_checks contract')}"
                            + (f"\n    note: {row['note']}" if row.get("note") else "")
                            for row in failed[:6]
                        )
                    elif state["may_seal"]:
                        comparison_state["scene_interfaces_ready"] = True
                        comparison_state["current_scene_contracts_present"] = True
                        comparison_state["scene_contracts_passed"] = True
                        if not comparison_state.get("look_unsettled") and not comparison_state.get(
                            "image_evidence_required"
                        ):
                            comparison_state["pixel_contracts_passed"] = True
                        contract_note += followup_after_scene_contracts_pass(comparison_state)
                    else:
                        comparison_state["scene_interfaces_ready"] = True
                        comparison_state["current_scene_contracts_present"] = False
                        comparison_state["scene_contracts_passed"] = False
                        contract_note += (
                            "\nINHERITED INTERFACES PASS, but this layer owns no active "
                            "scene completion contract. They prove healthy inputs, not "
                            "that the current layer is finished."
                        )
                        unpaid_note = _unpaid_image_debt_note(comparison_state)
                        if unpaid_note:
                            contract_note += unpaid_note
                        else:
                            contract_note += " Live mutation remains open until the builder hands its scoped work off."
                contract_note += _deferred_subject_forecast_note(
                    diagnostic_evidence,
                    contract_rows,
                    comparison_state.get("deferred_subject_sharing") or {},
                )
                contract_note += await _downstream_framing_probe(
                    session, comparison_state, contract_rows
                )
            except Exception as exc:
                contract_note = (
                    f"\n⚠ automatic scene-contract probe unavailable: {type(exc).__name__}: {str(exc)[:100]}"
                )
        if comparison_state.get("scope_offenders"):
            # A candidate with an unowned object is not converged even if its numeric
            # rows happen to pass. Keep the corrective mutation window open; canonical
            # replay will reject this exact state.
            comparison_state["scene_contracts_passed"] = False
            comparison_state["pixel_contracts_passed"] = False
            contract_note += (
                "\nSCOPE CLEANUP REQUIRED: convergence remains open until every newly "
                "created object is deleted or assigned a declared semantic role."
            )
        return _text(body + meta + warn + contract_note)

    return run_bpy
