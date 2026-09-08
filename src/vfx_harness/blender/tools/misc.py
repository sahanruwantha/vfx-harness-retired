"""Agent SDK tools over a warm BlenderSession.

`build_blender_tools(session)` returns an in-process MCP server and the list of
qualified tool names to put in `ClaudeAgentOptions.allowed_tools`. Render tools
return image content blocks, so the agent literally sees the frames it makes.
"""

from __future__ import annotations

import json
from pathlib import Path

from claude_agent_sdk import tool
from PIL import Image

from vfx_harness.blender.session import BlenderError
from vfx_harness.blender.tools.payment import _merge_worklist_items, _text
from vfx_harness.blender.tools.reports import CANNOT_EXPRESS_DESCRIPTION, CANNOT_EXPRESS_SCHEMA, record_cannot_express
from vfx_harness.evidence import image_check_operation
from vfx_harness.observability import prepared_publication, worklists
from vfx_harness.orchestration import escalate
from vfx_harness.orchestration.script_map import find_lines as _find_lines
from vfx_harness.orchestration.script_map import outline as _outline
from vfx_harness.orchestration.selected_authority_guard import commit_selected_authority


def register_misc(
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
    attempt_guard=None,
):
    def publication_binding() -> str:
        if attempt_guard is not None:
            return f"work-unit-attempt:{attempt_guard.claim.claim_id}"
        if selected_authority is not None:
            token = selected_authority.selection_token.to_dict()
            return "selected-authority:" + json.dumps(
                token,
                sort_keys=True,
                separators=(",", ":"),
            )
        return f"unbound-tool:{Path(shot_dir).absolute() if shot_dir else 'no-shot'}"

    def publish(operation, mutation):
        if attempt_guard is not None:
            return attempt_guard.publish(operation, mutation)
        if selected_authority is None:
            return mutation()
        return commit_selected_authority(
            shot_dir,
            selected_authority,
            operation=operation,
            mutation=mutation,
        )

    def prepare_and_publish(operation, prepare):
        binding = publication_binding()
        if attempt_guard is not None:
            attempt_guard.check(f"start {operation} preparation")
        update = prepare(binding)
        publication = update.publication
        if publication is None:
            if attempt_guard is not None:
                attempt_guard.check(f"finish {operation} no-op")
            return update.result
        try:
            publish(
                operation,
                lambda: prepared_publication.commit_prepared_file(
                    publication,
                    authority_binding=binding,
                ),
            )
        except BaseException:
            prepared_publication.discard_prepared_file(publication)
            raise
        return update.result

    @tool(
        "script_map",
        "STRUCTURAL INDEX of a build script — functions, sections, and which lines create "
        "or reference each named object/material. Use this INSTEAD of reading the whole "
        "file: a 536-line script maps to ~380 tokens. Then Read just that span and Edit "
        "it. Never rewrite a script you only need to change in one place.",
        {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    )
    async def script_map(args):
        p = Path(args["path"])
        if not p.is_absolute() and shot_dir:
            p = shot_dir / p
        return {"content": [{"type": "text", "text": _outline(p)}]}

    @tool(
        "find_in_script",
        "Locate a name/value inside a build script with surrounding context, so you can "
        "Read the right span instead of the whole file. Give the object name, material "
        "name, or literal you want to change.",
        {
            "type": "object",
            "properties": {"path": {"type": "string"}, "needle": {"type": "string"}},
            "required": ["path", "needle"],
        },
    )
    async def find_in_script(args):
        p = Path(args["path"])
        if not p.is_absolute() and shot_dir:
            p = shot_dir / p
        return {"content": [{"type": "text", "text": _find_lines(p, args["needle"])}]}

    @tool(
        "ask_supervisor",
        "Raise a question you cannot resolve from the brief, the stills or the plan — an "
        "ambiguity, a contradiction, or a judgement call that is genuinely the client's. "
        "This does NOT block: state the assumption you will proceed on and keep building. "
        "Use it INSTEAD of guessing silently, and instead of tuning against a target you "
        "are not sure about. Do not use it for things you could measure or spike.",
        {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "assumption": {"type": "string"},
                "why_it_matters": {"type": "string"},
                "affected_layers": {"type": "array", "items": {"type": "string"}},
                "affected_axes": {"type": "array", "items": {"type": "string"}},
                "global_decision": {"type": "boolean"},
            },
            "required": ["question", "assumption", "affected_layers"],
        },
    )
    async def ask_supervisor(args):
        if not shot_dir:
            return {"content": [{"type": "text", "text": "no shot folder — cannot ask"}]}

        binding = publication_binding()
        if attempt_guard is not None:
            attempt_guard.check("start builder question preparation")
        prepared = escalate.prepare_question(
            shot_dir,
            layer=layer_id or "?",
            question=args["question"],
            assumption=args["assumption"],
            why_it_matters=args.get("why_it_matters", ""),
            affected_layers=args.get("affected_layers") or [],
            affected_axes=args.get("affected_axes") or [],
            global_decision=bool(args.get("global_decision")),
            authority_binding=binding,
        )
        try:
            publication = prepared.update.publication
            if publication is None:
                raise prepared_publication.FilePublicationConflict(
                    "prepared supervisor question lacks an authoritative CAS publication"
                )
            publish(
                f"record builder question for layer {layer_id}",
                lambda: prepared_publication.commit_prepared_file(
                    publication,
                    authority_binding=binding,
                ),
            )
        except BaseException:
            escalate.discard_prepared_question(prepared)
            raise
        qid = escalate.log_committed_question(prepared)
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Recorded as Q{qid}. Continue on your stated assumption: {args['assumption']}",
                }
            ]
        }

    @tool(
        "worklist",
        "Your build checklist ON DISK — it survives context compaction and process death, "
        "which your memory does not. Call with items=[...] to (re)write it, or done=[...] "
        "to tick things off; call with neither to read it back. Write it once at the start "
        "from your layer's tickets, then tick as you go. Layer G was killed at turn 121 with "
        "the work half-finished and no record of what remained.",
        {
            "type": "object",
            "properties": {
                "items": {"type": "array", "items": {"type": "string"}},
                "done": {"type": "array", "items": {"type": "string"}},
                "note": {"type": "string"},
            },
            "required": [],
        },
    )
    async def worklist(args):
        if not shot_dir:
            return {"content": [{"type": "text", "text": "no shot folder"}]}
        layer_part = str(layer_id or "layer")
        active_unit_id = str(comparison_state.get("unit_id") or "")
        active_unit_hash = str(comparison_state.get("unit_hash") or "")
        if not active_unit_id or not active_unit_hash:
            return _text(
                "worklist requires the active unit id and digest; layer-only worklists "
                "cannot authorize another unit generation",
                is_error=True,
            )

        mutation_requested = any(args.get(field) for field in ("items", "done", "note"))
        if not mutation_requested:
            try:
                if attempt_guard is not None:
                    attempt_guard.check(f"start unit {layer_part}.{active_unit_id} worklist read")
                _worklist_path, state = worklists.load_unit_worklist(
                    shot_dir,
                    layer_id=layer_part,
                    unit_id=active_unit_id,
                    unit_hash=active_unit_hash,
                )
                if attempt_guard is not None:
                    attempt_guard.check(f"finish unit {layer_part}.{active_unit_id} worklist read")
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                return _text(f"worklist refused: {exc}", is_error=True)
        else:

            def update_worklist(current):
                if args.get("items"):
                    # A new attempt may restate its tickets, but it cannot erase an
                    # unresolved item discovered by the previous attempt. Carry those
                    # forward until explicitly completed.
                    _merge_worklist_items(current, list(args["items"]))
                for done_item in args.get("done", []):
                    if done_item not in current["done"]:
                        current["done"].append(done_item)
                if args.get("note"):
                    current["notes"].append(args["note"])

            try:
                state = prepare_and_publish(
                    f"write unit {layer_part}.{active_unit_id} worklist",
                    lambda binding: worklists.prepare_unit_worklist_update(
                        shot_dir,
                        layer_id=layer_part,
                        unit_id=active_unit_id,
                        unit_hash=active_unit_hash,
                        update=update_worklist,
                        authority_binding=binding,
                    ),
                )
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                return _text(f"worklist refused: {exc}", is_error=True)
        left = [i for i in state["items"] if i not in state["done"]]
        body = (
            "\n".join(f"  [x] {i}" for i in state["items"] if i in state["done"])
            + "\n"
            + "\n".join(f"  [ ] {i}" for i in left)
        ).strip()
        return {
            "content": [
                {"type": "text", "text": f"{len(state['done'])}/{len(state['items'])} done, {len(left)} left\n{body}"}
            ]
        }

    @tool(
        "measure_regions",
        "Measure named RECTANGLES of a rendered frame and compare them. Use this to "
        "prove a structural claim numerically instead of eyeballing it — 'the outer "
        "window strips are brighter than the recessed core', 'the sign LETTERS are "
        "brighter than the panel behind them'. Regions are in NORMALISED frame "
        "coordinates [x0,y0,x1,y1], each 0..1, origin TOP-LEFT. Returns mean/σ/max/lit%% "
        "per region plus every pairwise brightness ratio, so you never slice pixels "
        "yourself.",
        {
            "type": "object",
            "properties": {
                "frame": {"type": "integer"},
                "regions": {
                    "type": "object",
                    "description": "name -> [x0,y0,x1,y1] in 0..1, e.g. "
                    '{"left_strip":[0.42,0.2,0.46,0.8], '
                    '"core":[0.47,0.2,0.53,0.8]}',
                    "additionalProperties": {"type": "array", "items": {"type": "number"}},
                },
                "mode": {"type": "string", "enum": ["solid", "wire", "draft", "eevee"]},
                "scale": {"type": "number"},
            },
            "required": ["frame", "regions"],
        },
    )
    async def measure_regions(args):
        """Exists because the builder was writing its own measurement rig every layer.

        Asked to prove 'the outer quarters are brighter than the central half', it had no
        tool for it, so it hand-rolled `bpy.ops.render.render(write_still=True)` plus numpy
        pixel slicing INSIDE run_bpy — which produced two distinct crashes in one layer
        (a zero-size reduction and a 28-vs-31 concatenation), bypassed the session's render
        path so the metrics hook never saw those frames, and mutated
        scene.render.resolution_* on the live scene, where an exception between set and
        restore leaves the deliverable rendering at the wrong size.
        """
        stop = _black_search_stop()
        if stop:
            return _text(stop, is_error=True)
        regions = args.get("regions") or {}
        if not regions:
            return _text("no regions given", is_error=True)
        bad = [
            n
            for n, r in regions.items()
            if not (
                isinstance(r, list)
                and len(r) == 4
                and all(isinstance(v, (int, float)) and 0.0 <= v <= 1.0 for v in r)
                and r[0] < r[2]
                and r[1] < r[3]
            )
        ]
        if bad:
            return _text(f"regions must be [x0,y0,x1,y1] in 0..1 with x0<x2 and y0<y1; bad: {bad}", is_error=True)
        try:
            r = await _call(
                "render", frame=int(args["frame"]), mode=args.get("mode", "eevee"), scale=float(args.get("scale", 0.5))
            )
        except BlenderError as e:
            return _text(str(e), is_error=True)

        im = Image.open(r["image_path"]).convert("RGB")
        g = im.convert("L")
        W, H = g.size
        out = {}
        for name, (x0, y0, x1, y1) in regions.items():
            box = (
                max(0, int(x0 * W)),
                max(0, int(y0 * H)),
                min(W, max(int(x1 * W), int(x0 * W) + 1)),
                min(H, max(int(y1 * H), int(y0 * H) + 1)),
            )
            px = list(g.crop(box).getdata())
            n = len(px) or 1
            mean = sum(px) / n
            sd = (sum((p - mean) ** 2 for p in px) / n) ** 0.5
            out[name] = {
                "mean": round(mean, 1),
                "sd": round(sd, 1),
                "max": max(px),
                "lit_pct": round(100 * sum(1 for p in px if p >= 120) / n, 1),
                "px": n,
            }
        lines = [f"frame {r['frame']} ({r['mode']}) — {W}x{H}"]
        for name, v in out.items():
            lines.append(
                f"  {name:<16} mean {v['mean']:>5} · σ {v['sd']:>5} · "
                f"max {v['max']:>3} · lit {v['lit_pct']:>5}% · {v['px']}px"
            )
        names = list(out)
        if len(names) > 1:
            lines.append("  ratios (a/b by mean brightness):")
            for i, a in enumerate(names):
                for b in names[i + 1 :]:
                    ma, mb = out[a]["mean"], out[b]["mean"]
                    rel = ma / mb if mb > 0.5 else float("inf")
                    verdict = "BRIGHTER" if ma > mb * 1.05 else "DARKER" if mb > ma * 1.05 else "about EQUAL"
                    lines.append(f"    {a} is {verdict} than {b}  ({ma} vs {mb}, ×{rel:.2f})")
        for name, v in out.items():
            if v["px"] < 64:
                lines.append(
                    f"  ⚠ {name} is only {v['px']}px — too small to measure reliably; widen the region or raise scale"
                )
        return {"content": [{"type": "text", "text": "\n".join(lines)}]}

    @tool("propose_checks", image_check_operation.DESCRIPTION, image_check_operation.SCHEMA)
    async def propose_checks(args):
        result = image_check_operation.propose_checks(
            args, shot_dir=shot_dir, layer_id=layer_id, comparison_state=comparison_state,
            selected_authority=selected_authority, prepare_and_publish=prepare_and_publish,
        )
        return _text(result.message, is_error=result.is_error)

    @tool(
        "cannot_express_in_scope",
        CANNOT_EXPRESS_DESCRIPTION,
        CANNOT_EXPRESS_SCHEMA,
    )
    async def cannot_express_in_scope(args):
        return record_cannot_express(comparison_state, args)

    return script_map, find_in_script, worklist, cannot_express_in_scope, measure_regions, propose_checks
