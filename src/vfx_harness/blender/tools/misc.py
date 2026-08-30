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
from vfx_harness.blender.tools.guards import _candidate_for_proposed_check
from vfx_harness.blender.tools.payment import _merge_worklist_items, _refresh_unpaid_image_debts, _sha256_file, _text
from vfx_harness.blender.tools.reports import CANNOT_EXPRESS_DESCRIPTION, CANNOT_EXPRESS_SCHEMA, record_cannot_express
from vfx_harness.domain.image_debts import (
    debts_from_dicts,
    normalize_evidence_id,
    reject_proposed_image_check,
    unpaid_image_contract_debts,
)
from vfx_harness.domain.work_units import read_document
from vfx_harness.evidence.checks import (
    IMAGE_PAYMENT_SCHEMA,
    METRICS,
    Check,
    load_image_contract_payment_rows,
    verify_necessity,
)
from vfx_harness.observability.provenance import atomic_write
from vfx_harness.observability.worklists import load_unit_worklist, write_unit_worklist
from vfx_harness.orchestration.escalate import ask as _ask
from vfx_harness.orchestration.plan_authority import selected_artifact_path
from vfx_harness.orchestration.script_map import find_lines as _find_lines
from vfx_harness.orchestration.script_map import outline as _outline


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
):
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
        qid = _ask(
            shot_dir,
            layer=layer_id or "?",
            question=args["question"],
            assumption=args["assumption"],
            why_it_matters=args.get("why_it_matters", ""),
            affected_layers=args.get("affected_layers") or [],
            affected_axes=args.get("affected_axes") or [],
            global_decision=bool(args.get("global_decision")),
        )
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

        try:
            wl, state = load_unit_worklist(
                shot_dir,
                layer_id=layer_part,
                unit_id=active_unit_id,
                unit_hash=active_unit_hash,
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            return _text(f"worklist refused: {exc}", is_error=True)
        if args.get("items"):
            # A new attempt may restate its tickets, but it cannot erase an unresolved
            # item discovered by the previous attempt.  Carry those forward until they
            # are explicitly completed; this is the durable feedback loop across both
            # context compaction and full process restarts.
            _merge_worklist_items(state, list(args["items"]))
        for d in args.get("done", []):
            if d not in state["done"]:
                state["done"].append(d)
        if args.get("note"):
            state["notes"].append(args["note"])
        write_unit_worklist(wl, state)
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

    @tool(
        "propose_checks",
        "Record what you learned about VERIFYING this layer, as executable checks. You are "
        "the only stage with the built scene. Runtime evidence is evaluation-only and is "
        "not execution authority; do not read runtime_checks.json. Propose only an "
        "evidence gap you actually discovered.\n"
        "Each check must PASS on your render and FAIL on the state before your unit ran. "
        "The harness captures that adversary before the unit starts; you cannot select "
        "or manufacture it.\n"
        "after_handle is the IMAGE EVIDENCE HANDLE returned by render_frame or an "
        "uncropped compare_frame at the owed frame. Raw paths are intentionally not "
        "accepted. For a multi-frame batch, put after_handle on each check; a batch-level "
        "after_handle is shorthand only when every check uses the same frame. Render at "
        "mode='eevee', scale=0.5 so it is settings-identical to the "
        "harness adversary. Survivors are appended to the "
        "runtime_checks.json evidence ledger; planner contracts remain immutable in "
        "checks.json. Propose few and real. When the active unit owes image-contract "
        "debts, each kept row must use an owed id with matching frame, property kind, "
        "and axis; a different id while debts remain is rejected naming requested vs owed.",
        {
            "type": "object",
            "properties": {
                "checks": {
                    "type": "array",
                    "maxItems": 20,
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "metric": {"type": "string", "enum": sorted(METRICS)},
                            "op": {"type": "string", "enum": [">=", "<=", "band"]},
                            "lo": {"type": "number"},
                            "hi": {"type": "number"},
                            "frame": {"type": "integer"},
                            "axis": {"type": "string"},
                            "stage": {"type": "string", "enum": ["pre_grade", "post_grade", "any"]},
                            "ref": {"type": "string"},
                            "regions": {
                                "type": "object",
                                "description": (
                                    "Literal metric operands: every region_mean/min/p5/max/"
                                    "sigma/lit_pct/green_excess/lit_variance check requires "
                                    "exactly key 'r', e.g. {'r':[x0,y0,x1,y1]}; "
                                    "region_ratio requires keys 'a' and 'b'. Labels such as "
                                    "'target', 'region', or a subject name are not operands."
                                ),
                                "additionalProperties": {
                                    "type": "array",
                                    "minItems": 4,
                                    "maxItems": 4,
                                    "items": {"type": "number"},
                                },
                            },
                            "note": {"type": "string"},
                            "after_handle": {
                                "type": "string",
                                "description": (
                                    "Frame-local current-run immutable candidate handle; "
                                    "overrides the batch-level shorthand"
                                ),
                            },
                        },
                        "required": ["id", "metric", "op"],
                        "additionalProperties": False,
                    },
                },
                "after_handle": {
                    "type": "string",
                    "description": "Current-run immutable handle returned by render_frame/compare_frame",
                },
            },
            "required": ["checks"],
            "additionalProperties": False,
        },
    )
    async def propose_checks(args):

        if not shot_dir:
            return _text("propose_checks needs a shot dir", is_error=True)
        root = Path(shot_dir)
        registry = comparison_state.get("image_artifacts") or {}
        # Every check must name the plate it is about, or the gate cannot re-run it. The
        # first version of this tool took `after`/`before` renders and never populated
        # `ref`, so four good builder checks landed in the runtime evidence ledger and all
        # four failed validation on plumbing rather than on merit.
        judge: dict[int, str] = {}
        first_ref = ""
        try:

            for lay in read_document(selected_artifact_path(root, "layers.json")):
                if str(lay.get("id")) != str(layer_id):
                    continue
                js = lay.get("judge") or []
                judge = {int(j["frame"]): j["ref"] for j in js if j.get("ref")}
                primary = lay.get("primary_judge")
                first_ref = next((j.get("ref", "") for j in js if j.get("frame") == primary), "")
        except Exception as e:
            return _text(
                f"could not read judge refs from selected layers.json: {str(e)[:100]}",
                is_error=True,
            )
        kept, lines = [], []
        debts = debts_from_dicts(comparison_state.get("image_debts"))
        unpaid = unpaid_image_contract_debts(debts, load_image_contract_payment_rows(root)) if debts else ()
        for d in list(args.get("checks") or [])[:20]:
            cid = normalize_evidence_id(d.get("id", "?"))
            debt_reject = reject_proposed_image_check(
                d,
                debts,
                unpaid=unpaid,
                registry=METRICS,
            )
            if debt_reject:
                lines.append(f"  REJECTED {cid:10} {debt_reject}")
                continue
            d = {**d, "id": cid}
            after_handle, after_record, handle_error = _candidate_for_proposed_check(
                d, args.get("after_handle"), registry
            )
            d.pop("after_handle", None)
            if handle_error or not isinstance(after_record, dict):
                lines.append(f"  REJECTED {cid:10} {handle_error}")
                continue
            after = root / str(after_record["path"])
            if not after.is_file() or _sha256_file(after) != after_record.get("sha256"):
                lines.append(
                    f"  REJECTED {cid:10} candidate handle {after_handle!r} no longer matches its immutable artifact"
                )
                continue
            try:
                check_frame = int(d["frame"])
            except (KeyError, TypeError, ValueError):
                lines.append(f"  REJECTED {cid:10} frame is required for a runtime image payment")
                continue
            adversary_record = (comparison_state.get("image_adversaries") or {}).get(check_frame)
            if not isinstance(adversary_record, dict):
                lines.append(f"  REJECTED {cid:10} harness captured no pre-unit adversary at f{check_frame}")
                continue
            before = root / str(adversary_record["path"])
            if not before.is_file() or _sha256_file(before) != adversary_record.get("sha256"):
                lines.append(f"  REJECTED {cid:10} pre-unit adversary artifact is missing or changed")
                continue
            if int(after_record.get("frame", -1)) != check_frame:
                lines.append(
                    f"  REJECTED {cid:10} candidate handle is f{after_record.get('frame')}, "
                    f"but this debt is f{check_frame}"
                )
                continue
            settings = ("mode", "scale", "resolution")
            mismatch = [key for key in settings if after_record.get(key) != adversary_record.get(key)]
            if mismatch:
                lines.append(
                    f"  REJECTED {cid:10} candidate/adversary settings differ in "
                    + ", ".join(mismatch)
                    + "; render_frame(mode='eevee', scale=0.5)"
                )
                continue
            try:
                ref_rel = (
                    d.get("ref") or judge.get(int(d["frame"])) if d.get("frame") is not None else d.get("ref")
                ) or first_ref
                d = {**d, "ref": ref_rel}
                c = Check.from_dict(
                    {
                        **d,
                        "layer": str(layer_id or d.get("layer", "")),
                        "lo": d.get("lo", float("-inf")),
                        "hi": d.get("hi", float("inf")),
                    }
                )
                if not ref_rel:
                    lines.append(f"  REJECTED {cid:10} no judge frame to name as its ref")
                    continue
                v = verify_necessity(c, after, before)
            except Exception as e:
                lines.append(f"  REJECTED {cid:10} {str(e)[:80]}")
                continue
            if v.ok:
                # Free-form builder notes are never persisted as future execution
                # authority. Keep only executable fields plus harness-generated proof.
                safe = {k: value for k, value in d.items() if k != "note"}
                kept.append(
                    {
                        **safe,
                        "layer": str(layer_id or d.get("layer", "")),
                        # Record the render this was proven against. A later attempt
                        # replaces the renders, and a proof that does not say which
                        # picture it came from cannot be told apart from a wrong one.
                        "proof": {
                            "ref": round(v.ref_value, 4),
                            "adversary": [round(x, 4) for x in v.bad_values[:1]],
                            "on": after_record["path"],
                        },
                        "payment": {
                            "schema": IMAGE_PAYMENT_SCHEMA,
                            "run_id": after_record["run_id"],
                            "unit_id": str(comparison_state.get("unit_id") or ""),
                            "unit_hash": str(comparison_state.get("unit_hash") or ""),
                            "parent_chain_hash": str(comparison_state.get("parent_chain_hash") or ""),
                            "candidate": {
                                key: after_record[key]
                                for key in ("path", "sha256", "frame", "mode", "scale", "resolution")
                            },
                            "adversary": {
                                key: adversary_record[key]
                                for key in (
                                    "path",
                                    "sha256",
                                    "frame",
                                    "mode",
                                    "scale",
                                    "resolution",
                                    "parent_chain_hash",
                                )
                            },
                        },
                        "origin": "builder",
                        # No prior layer means no adversary — the FIRST layer's checks
                        # are the least verified in the system, and saying so is the
                        # point. Silence here would let them count as adversaried.
                        **({"note": "[no prior-layer adversary: first layer]"} if not v.bad_values else {}),
                    }
                )
                lines.append(
                    f"  KEPT     {cid:10} {c.metric} {c.target()} · after "
                    f"{v.ref_value:.4g}" + (f" · before {v.bad_values[0]:.4g}" if v.bad_values else "")
                )
            else:
                lines.append(f"  REJECTED {cid:10} {v.reasons[0][:120]}")
        if kept:
            spec = root / "runtime_checks.json"
            cur = json.loads(spec.read_text()) if spec.is_file() else []
            replacement_keys = {(row.get("layer"), row.get("id")) for row in kept}
            cur = [row for row in cur if (row.get("layer"), row.get("id")) not in replacement_keys]
            cur.extend(kept)

            atomic_write(spec, json.dumps(cur, indent=1) + "\n")
        _refresh_unpaid_image_debts(comparison_state, root)
        remaining = comparison_state.get("unpaid_image_debts") or []
        tail = ""
        if remaining:
            tail = (
                "\nStill unpaid: "
                + ", ".join(str(row.get("id")) for row in remaining)
                + ". Candidate freeze will refuse until these ids are paid or "
                "cannot_express_in_scope records unpaid_image_debt."
            )
        return _text(f"{len(kept)} check(s) added to runtime_checks.json.\n" + "\n".join(lines) + tail)

    @tool(
        "cannot_express_in_scope",
        CANNOT_EXPRESS_DESCRIPTION,
        CANNOT_EXPRESS_SCHEMA,
    )
    async def cannot_express_in_scope(args):
        return record_cannot_express(comparison_state, args)

    return script_map, find_in_script, worklist, cannot_express_in_scope, measure_regions, propose_checks
