"""Authoritative Blender-state and cross-layer interface contracts.

``scene_checks.json`` is a strict schema-2 document. Contracts address objects,
materials, shader controls and compositor nodes by semantic custom properties, never by
datablock names. Object selectors distinguish ``bvfx_role`` from ``bvfx_control`` so a
planner cannot put control ids in a role field and publish an unresolvable contract. Their
lifecycle decides which prior-layer guarantees remain active for the layer currently being
built.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path

from PIL import Image, ImageChops, ImageStat

from vfx_harness.domain.contracts import active_for, validate_lifecycle
from vfx_harness.domain.evidence_kinds import PROJECTED_ORIGIN_KINDS as PROJECTED_ORIGIN_KINDS
from vfx_harness.evidence.scene_checks.deferred_subject import deferred_subject_composition_activation_ids, load_rows
from vfx_harness.evidence.scene_checks.kinds import FUNCTIONAL_KINDS, KIND_DEFINITIONS
from vfx_harness.evidence.scene_checks.probe import _blender_probe, _evidence
from vfx_harness.evidence.scene_checks.validate import _holds, _target, validate_row
from vfx_harness.orchestration.plan_authority import selected_artifact_path


def layer_evidence(shot_folder: str | Path, layer_id: str, *, frame: int, session) -> list[dict]:
    """Evaluate every lifecycle-active contract, including persistent prior interfaces."""
    path = selected_artifact_path(shot_folder, "scene_checks.json")
    if not path.is_file():
        return []
    try:
        all_rows = load_rows(shot_folder)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return [
            {
                "id": "scene-contract-document",
                "axis": "",
                "metric": "schema",
                "value": None,
                "target": "schema=2",
                "pass": False,
                "origin": "planner",
                "source": "interface_contract",
                "authoritative": True,
                "error": str(exc)[:160],
            }
        ]
    rows = [r for r in all_rows if isinstance(r, dict) and active_for(r, layer_id, frame)]
    static = [r for r in rows if r.get("kind") not in FUNCTIONAL_KINDS]
    invalid = [r for r in static if validate_row(r)]
    runnable = [r for r in static if not validate_row(r)]
    raw = []
    if runnable:
        result = session.run(_blender_probe(runnable, frame), journal=False)
        raw = result.get("result") or []
        if not isinstance(raw, list):
            raw = []
    return _evidence(invalid, []) + _evidence(runnable, raw)


def _control_script(row: dict, value=None) -> str:
    spec = json.dumps(
        {
            key: row.get(key)
            for key in (
                "graph",
                "material_roles",
                "node_roles",
                "socket",
                "socket_index",
                "socket_direction",
            )
        }
    )
    return f"""\
import bpy, json
import checks as _checks
spec=json.loads({json.dumps(spec)})
match=_checks.match_semantic
graphs=[]
if spec['graph']=='material':
    mats=[m for m in bpy.data.materials if match(m.get('bvfx_role'),spec['material_roles'])]
    graphs=[m.node_tree for m in mats if m.node_tree]
elif spec['graph']=='compositor':
    ng=getattr(bpy.context.scene,'compositing_node_group',None); graphs=[ng] if ng else []
else:
    nt=bpy.context.scene.world.node_tree if bpy.context.scene.world and bpy.context.scene.world.use_nodes else None
    graphs=[nt] if nt else []
nodes=[n for nt in graphs for n in nt.nodes
       if match(n.get('bvfx_control'),spec['node_roles'])
       or match(n.get('bvfx_role'),spec['node_roles'])]
if len(nodes)!=1:
    seen=sorted(set(str(t) for nt in graphs for n in nt.nodes
                    for t in (n.get('bvfx_control'),n.get('bvfx_role')) if t))[:16]
    raise ValueError("semantic control "+repr(spec['node_roles'])+" matched "+str(len(nodes))
                     +" nodes in "+str(len(graphs))+" "+str(spec['graph'])+" graph(s);"
                     +" semantic tags present: "+(", ".join(seen) or "(none)"))
direction=spec.get('socket_direction') or 'auto'
collections=(
    [('input',nodes[0].inputs)] if direction=='input' else
    [('output',nodes[0].outputs)] if direction=='output' else
    [('input',nodes[0].inputs),('output',nodes[0].outputs)]
)
socket=None; resolved_direction=None
for candidate_direction,sockets in collections:
    try:
        candidate=(sockets[int(spec['socket_index'])] if spec.get('socket_index') is not None
                   else sockets.get(spec.get('socket') or 'Value'))
    except IndexError:
        candidate=None
    if candidate is not None:
        socket=candidate; resolved_direction=candidate_direction; break
if socket is None:
    raise ValueError("semantic control node "+nodes[0].bl_idname+" has no requested "+direction
                     +" socket "+repr(spec.get('socket') or 'Value')
                     +"; inputs="+repr([s.name for s in nodes[0].inputs][:12])
                     +" outputs="+repr([s.name for s in nodes[0].outputs][:12])
                     +". With no 'socket' declared the sweep resolves a socket literally named"
                     +" 'Value': tag a ShaderNodeValue that drives the target property,"
                     +" or declare 'socket' in the contract row.")
_tree=nodes[0].id_data
_ad=getattr(_tree,'animation_data',None)
_dpath=socket.path_from_id('default_value')
if _ad and any(d.data_path==_dpath for d in (_ad.drivers or [])):
    # A driver re-evaluates the socket every depsgraph update, so the sweep's write is
    # silently clobbered and the measured response is always 0.0 — run 20260825
    # (17581c) burned both repairs proving a beautiful frame-driven look that no
    # contract could ever measure. Fail loudly with the workable rig instead.
    raise ValueError("semantic control socket '"+socket.name+"' on "+nodes[0].name
                     +" is DRIVER-OWNED: the sweep writes default_value and the driver"
                     +" overwrites it at evaluation, so the measured response is always"
                     +" 0. Keep the swept control FREE (e.g. a tagged ShaderNodeValue)"
                     +" and combine it with the animated quantity via a Math node;"
                     +" drive the Math operand, never the tagged control itself.")
before=float(socket.default_value)
new={value!r}
if new is not None: socket.default_value=float(new)
RESULT={{'before':before,'after':float(socket.default_value),'node':nodes[0].name,
        'socket':socket.name,'socket_direction':resolved_direction}}
"""


def functional_evidence(
    shot_folder: str | Path, layer_id: str, *, session, rows: list[dict] | None = None
) -> list[dict]:
    """Render transactional control sweeps or deterministic two-frame deltas."""
    selected = (
        rows
        if rows is not None
        else [
            row
            for row in load_rows(shot_folder)
            if isinstance(row, dict)
            and row.get("kind") in FUNCTIONAL_KINDS
            and active_for(row, layer_id, int(row.get("frame", 1)))
        ]
    )
    out = []
    for row in selected:
        error = validate_row(row)
        value = None
        original = None
        try:
            if error:
                raise ValueError(error)
            images = []
            if row.get("kind") == "frame_delta":
                probe_values = row["frames"]
            elif row.get("kind") == "render_region_stat":
                probe_values = [row["frame"]]
            else:
                initial = session.run(_control_script(row), journal=False).get("result") or {}
                original = float(initial["before"])
                probe_values = row["probe_values"]
            for probe_value in probe_values:
                if row.get("kind") == "control_render_response":
                    session.run(_control_script(row, float(probe_value)), journal=False)
                    render_frame = int(row.get("frame", 1))
                else:
                    render_frame = int(probe_value)
                rendered = session.render_full(
                    frame=render_frame,
                    mode=str(row.get("probe_mode", "eevee")),
                    scale=float(row.get("probe_scale", 0.5)),
                )
                with Image.open(rendered["image_path"]) as source:
                    image = source.convert("RGB")
                    region = row.get("region")
                    if region:
                        x0, y0, x1, y1 = region
                        image = image.crop(
                            (
                                int(image.width * x0),
                                int(image.height * y0),
                                max(int(image.width * x0) + 1, int(image.width * x1)),
                                max(int(image.height * y0) + 1, int(image.height * y1)),
                            )
                        )
                    images.append(image)
            if row.get("kind") == "render_region_stat":
                stat_name = str(row.get("stat"))
                if stat_name in {"mean_r", "mean_g", "mean_b"}:
                    # hue is contractable: the luminance-only anchors passed a frame
                    # whose RGB spread was 15 against the ref's 57 (run af3084 —
                    # "bright but nearly colorless")
                    channel = {"mean_r": 0, "mean_g": 1, "mean_b": 2}[stat_name]
                    value = ImageStat.Stat(images[0].convert("RGB")).mean[channel]
                else:
                    stats = ImageStat.Stat(images[0].convert("L"))
                    value = stats.mean[0] if stat_name == "mean" else stats.stddev[0]
            elif row.get("kind") == "frame_delta" or row.get("response_metric", "mean_delta") == "mae":
                low, high = images
                if low.size != high.size:
                    raise ValueError("rendered frames have different dimensions")
                value = ImageStat.Stat(ImageChops.difference(low, high).convert("L")).mean[0]
            else:
                low, high = images
                low_mean = ImageStat.Stat(low.convert("L")).mean[0]
                high_mean = ImageStat.Stat(high.convert("L")).mean[0]
                value = high_mean - low_mean
        except Exception as exc:
            # 400, not 160: control-resolution misses enumerate the tags/sockets present,
            # and a truncated enumeration reads as a complete one.
            error = str(exc)[:400]
        finally:
            if original is not None:
                try:
                    session.run(_control_script(row, original), journal=False)
                except Exception as exc:
                    error = f"restore failed: {exc}"[:160]
        # The instrument itself is part of the reading: a failing 0.0 with no metric,
        # region, or frame named sent two builds hunting the control instead of the
        # measurement (run 17581c: mean_delta is luminance-only, so a hue-swap palette
        # control reads ~0; the row also measured the silent-default frame).
        if row.get("kind") == "render_region_stat":
            instrument = (
                f"measured as luminance {row.get('stat')} (0-255) over region "
                f"{row.get('region')} at frame {row.get('frame')}"
            )
        else:
            instrument = (
                f"measured as {row.get('response_metric', 'mean_delta')}"
                + (" (luminance-only: a pure hue shift reads ~0 — palette/tint semantics"
                   " need response_metric: mae)"
                   if row.get("kind") == "control_render_response"
                   and row.get("response_metric", "mean_delta") == "mean_delta"
                   else "")
                + f" over region {row.get('region')}"
                + (f" at frame {row.get('frame')}" if row.get("frame") is not None else "")
            )
        out.append(
            {
                "id": str(row.get("id") or "<missing>"),
                "axis": str(row.get("axis") or ""),
                "metric": str(row.get("kind") or "control_render_response"),
                "definition": KIND_DEFINITIONS[str(row.get("kind") or "control_render_response")],
                "value": round(value, 4) if isinstance(value, (int, float)) else None,
                "target": _target(row),
                "note": instrument,
                "pass": not error and _holds(row, value),
                "origin": "planner",
                "source": "interface_contract",
                "authoritative": True,
                "owner_layer": str(row.get("owner_layer") or ""),
                "fault_owner": str(row.get("fault_owner") or ""),
                "activates_at": str(row.get("activates_at") or ""),
                "lifecycle": str(row.get("lifecycle") or ""),
                **({"error": error} if error else {}),
            }
        )
    return out


def prior_interface_evidence(shot_folder: str | Path, layer_id: str, *, session) -> list[dict]:
    """Revalidate every active interface owned by an earlier layer before mutation.

    Contracts retain their own judge frame, so this cannot accidentally validate a rest
    transform at the current layer's primary action frame.  A failed prior interface is
    attributed to ``fault_owner`` and stops before a downstream builder is asked to work
    around corrupt input.
    """
    path = selected_artifact_path(shot_folder, "scene_checks.json")
    if not path.is_file():
        return []
    rows = load_rows(shot_folder)
    selected = list(prior_interface_rows(rows, layer_id))
    out = []
    by_frame: dict[int, list[dict]] = {}
    for row in selected:
        by_frame.setdefault(int(row.get("frame", 1)), []).append(row)
    for frame, frame_rows in sorted(by_frame.items()):
        functional = [r for r in frame_rows if r.get("kind") in FUNCTIONAL_KINDS]
        static = [r for r in frame_rows if r.get("kind") not in FUNCTIONAL_KINDS]
        invalid = [r for r in static if validate_row(r)]
        runnable = [r for r in static if not validate_row(r)]
        raw = []
        if runnable:
            result = session.run(_blender_probe(runnable, frame), journal=False)
            raw = result.get("result") or []
            if not isinstance(raw, list):
                raw = []
        out.extend(_evidence(invalid, []))
        out.extend(_evidence(runnable, raw))
        out.extend(functional_evidence(shot_folder, layer_id, session=session, rows=functional))
    return out


def prior_interface_rows(
    rows: Sequence[Mapping[str, object]], layer_id: str | int
) -> tuple[dict, ...]:
    """Earlier-layer rows testable before this layer mutates (HIR-0134)."""
    current = int(layer_id)
    future_subject_ids = set(deferred_subject_composition_activation_ids(rows, current))
    return tuple(
        r
        for r in rows
        if isinstance(r, dict)
        and not validate_lifecycle(r)
        and int(r["owner_layer"]) < current
        and active_for(r, current)
        and str(r.get("id") or "") not in future_subject_ids
    )
