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
from pathlib import Path

from PIL import Image, ImageChops, ImageStat

from vfx_harness.domain.contracts import active_for, load_document, validate_lifecycle

OBJECT_KINDS = {
    "bbox_width",
    "bbox_height",
    "bbox_center_x",
    "bbox_center_y",
    "bbox_top_y",
    "bbox_bottom_y",
    "object_count",
    "mesh_vertex_count",
    "smooth_fraction",
    "radial_inward_fraction",
    "object_property",
}
MATERIAL_KINDS = {"material_count", "material_user_count", "material_assignment_fraction"}
NODE_KINDS = {"node_count", "node_socket_value", "node_link_count"}
STATE_KINDS = {"animation_count", "compositor_enabled"}
TEMPORAL_KINDS = {
    "keyframe_schedule",
    "onset_order",
    "radial_distance_trend",
    "transform_return_delta",
}
WINDOW_KINDS = TEMPORAL_KINDS - {"keyframe_schedule"}
# Every key the evaluator, gate, and orchestration actually read off a contract row.
# A row carrying anything else is not "extra metadata" — it is a claim the harness
# silently ignores. Run 20260823T154920Z shipped `at_frame: 36`, nothing read it,
# `row.get("frame", 1)` defaulted to 1, and a sealed frame-1 reading was reported as a
# frame-36 retraction failure for a whole build. Unknown keys now fail closed.
KNOWN_ROW_KEYS = frozenset({
    "id", "kind", "axis", "op", "lo", "hi", "value", "unit",
    "owner_layer", "fault_owner", "activates_at", "lifecycle", "expires_at",
    "decision_id", "frame", "frames", "region", "component", "samples",
    "motion_epsilon", "property", "tol", "uniform_tol", "direction", "domain",
    "graph", "socket", "socket_index", "socket_direction", "from_socket", "to_socket",
    "probe_mode", "probe_scale", "probe_values", "response_metric",
    "roles", "control_roles", "material_roles", "compare_roles",
    "compare_control_roles", "node_roles", "node_group_roles",
    "from_node_roles", "to_node_roles",
})
# Scene state these kinds read changes with the frame, so a row that does not say
# WHICH frame it reads silently measures frame 1 via the historical default.
FRAME_SCOPED_KINDS = {
    "bbox_width",
    "bbox_height",
    "bbox_center_x",
    "bbox_center_y",
    "bbox_top_y",
    "bbox_bottom_y",
    "mesh_vertex_count",
    "radial_inward_fraction",
    "object_property",
}
FUNCTIONAL_KINDS = {"control_render_response", "frame_delta"}
SUPPORTED_KINDS = (
    OBJECT_KINDS | MATERIAL_KINDS | NODE_KINDS | STATE_KINDS | TEMPORAL_KINDS | FUNCTIONAL_KINDS
)
SUPPORTED_OPS = {"band", "eq", "min", "max"}

KIND_DEFINITIONS = {
    "bbox_width": "projected union width in normalized camera coordinates",
    "bbox_height": "projected union height in normalized camera coordinates",
    "bbox_center_x": "projected union horizontal centre; 0=left, 1=right",
    "bbox_center_y": "projected union vertical centre; 0=top, 1=bottom",
    "bbox_top_y": "top of projected union; normalized top-left coordinates",
    "bbox_bottom_y": "bottom of projected union; normalized top-left coordinates",
    "object_count": "number of objects whose bvfx_role matches roles",
    "mesh_vertex_count": "evaluated mesh vertex total across matched object roles",
    "smooth_fraction": "fraction of matched mesh polygons using smooth shading",
    "radial_inward_fraction": "fraction where radial XY normal dot face centre <= 0 (inward)",
    "object_property": "numeric property read from every semantically selected object",
    "material_count": "number of materials whose bvfx_role matches material_roles",
    "material_user_count": "total Blender users of matched semantic materials",
    "material_assignment_fraction": "fraction of selected objects assigned a matching material role",
    "node_count": "number of shader/compositor nodes matching node_roles",
    "node_socket_value": "numeric socket value on one semantic shader/compositor node",
    "node_link_count": "number of links between semantic nodes and optional sockets",
    "animation_count": "animation datablocks on the selected semantic state",
    "compositor_enabled": "1 when compositing and a semantic compositor group exist",
    "control_render_response": "pixel response when a semantic numeric control is swept low to high",
    "onset_order": (
        "comparison-role onset frame minus selected-role onset frame; positive means selected roles start first"
    ),
    "radial_distance_trend": "least-squares slope of mean XY distance from origin across a frame window",
    "transform_return_delta": "selected transform-component delta between two declared frames",
    "keyframe_schedule": (
        "maximum property error against an exact semantic keyframe schedule; any missing or "
        "extra keyed frame fails the contract"
    ),
    "frame_delta": "mean absolute rendered-pixel delta between two declared frames",
}


def _target(row: dict) -> str:
    op = row.get("op", "band")
    if op == "band":
        return f"{row.get('lo')}..{row.get('hi')}"
    if op == "eq":
        return f"= {row.get('value')} ± {row.get('tol', 0)}"
    if op == "min":
        return f">= {row.get('lo')}"
    if op == "max":
        return f"<= {row.get('hi')}"
    return str(op)


def _holds(row: dict, value) -> bool:
    if value is None:
        return False
    try:
        value = float(value)
        op = row.get("op", "band")
        if op == "band":
            return float(row["lo"]) <= value <= float(row["hi"])
        if op == "eq":
            return abs(value - float(row["value"])) <= float(row.get("tol", 0))
        if op == "min":
            return value >= float(row["lo"])
        if op == "max":
            return value <= float(row["hi"])
    except (TypeError, ValueError):
        return False
    return False


def _selectors(row: dict, key: str) -> list[str]:
    value = row.get(key)
    if isinstance(value, str):
        value = [value]
    return value if isinstance(value, list) else []


def validate_row(row: dict) -> str | None:
    if not isinstance(row, dict):
        return "record must be an object"
    if not row.get("id"):
        return "missing id"
    if any(key in row for key in ("objects", "materials", "nodes")):
        return "datablock-name selectors are removed; use semantic role selectors"
    life = validate_lifecycle(row)
    if life:
        return life
    kind = str(row.get("kind", ""))
    if kind not in SUPPORTED_KINDS:
        # Sessions authoring contracts are workspace-confined: this message is their
        # only route to the registry, and an unnamed enum invites invented kinds.
        return (
            f"unsupported kind {kind!r}; supported kinds: "
            + ", ".join(sorted(SUPPORTED_KINDS))
        )
    if kind in OBJECT_KINDS and not (
        _selectors(row, "roles") or _selectors(row, "control_roles")
    ):
        return "object contract requires non-empty roles or control_roles"
    if kind in MATERIAL_KINDS - {"material_assignment_fraction"} and not _selectors(row, "material_roles"):
        return "material contract requires non-empty material_roles"
    if kind == "material_assignment_fraction" and (
        not _selectors(row, "roles") or not _selectors(row, "material_roles")
    ):
        return "material_assignment_fraction requires roles and material_roles"
    if kind in NODE_KINDS:
        if row.get("graph") not in {"material", "compositor", "world"}:
            return "node contract graph must be material, compositor, or world"
        if row.get("graph") == "material" and not _selectors(row, "material_roles"):
            return "material node contract requires material_roles"
        if kind != "node_link_count" and not _selectors(row, "node_roles"):
            return f"{kind} requires node_roles"
    if kind == "control_render_response":
        if row.get("graph") not in {"material", "compositor", "world"}:
            return "control_render_response graph must be material, compositor, or world"
        if row.get("graph") == "material" and not _selectors(row, "material_roles"):
            return "material control response requires material_roles"
        if not _selectors(row, "node_roles"):
            return "control_render_response requires node_roles"
        values = row.get("probe_values")
        if not isinstance(values, list) or len(values) != 2:
            return "control_render_response requires two probe_values"
        region = row.get("region")
        if (
            not isinstance(region, list)
            or len(region) != 4
            or not all(isinstance(v, (int, float)) for v in region)
            or not all(0 <= float(v) <= 1 for v in region)
            or not (region[0] < region[2] and region[1] < region[3])
        ):
            return "control_render_response requires a normalized TOP-LEFT region"
        if row.get("response_metric", "mean_delta") not in {"mean_delta", "mae"}:
            return "control_render_response metric must be mean_delta or mae"
        if row.get("socket_direction", "auto") not in {"auto", "input", "output"}:
            return "control_render_response socket_direction must be auto, input, or output"
    if kind in WINDOW_KINDS | {"frame_delta"}:
        frames = row.get("frames")
        if (
            not isinstance(frames, list)
            or len(frames) != 2
            or any(isinstance(value, bool) or not isinstance(value, int) or value < 1 for value in frames)
            or frames[0] >= frames[1]
        ):
            return f"{kind} requires two increasing positive integer frames"
    if kind in TEMPORAL_KINDS and not (
        _selectors(row, "roles") or _selectors(row, "control_roles")
    ):
        return f"{kind} requires non-empty roles or control_roles"
    if kind == "keyframe_schedule":
        samples = row.get("samples")
        if not isinstance(samples, list) or len(samples) < 2:
            return "keyframe_schedule requires at least two samples"
        seen_frames: set[int] = set()
        paths: set[str] | None = None
        for index, sample in enumerate(samples):
            if not isinstance(sample, dict):
                return f"keyframe_schedule samples[{index}] must be an object"
            frame = sample.get("frame")
            if isinstance(frame, bool) or not isinstance(frame, int) or frame < 1:
                return f"keyframe_schedule samples[{index}].frame must be a positive integer"
            if frame in seen_frames:
                return "keyframe_schedule sample frames must be unique"
            seen_frames.add(frame)
            values = sample.get("values")
            if not isinstance(values, dict) or not values:
                return f"keyframe_schedule samples[{index}].values must be a non-empty object"
            sample_paths = set(values)
            if paths is None:
                paths = sample_paths
            elif sample_paths != paths:
                return "keyframe_schedule samples must declare the same property paths"
            for path, expected in values.items():
                if not isinstance(path, str) or not path.strip():
                    return "keyframe_schedule property paths must be non-empty strings"
                numeric = expected if isinstance(expected, list) else [expected]
                if not numeric or any(
                    isinstance(value, bool) or not isinstance(value, (int, float))
                    for value in numeric
                ):
                    return "keyframe_schedule values must be numeric scalars or vectors"
    unknown = sorted(set(row) - KNOWN_ROW_KEYS)
    if unknown:
        return (
            "unknown contract key(s) " + ", ".join(unknown)
            + " — the harness would ignore them silently; accepted keys are "
            + ", ".join(sorted(KNOWN_ROW_KEYS))
        )
    if kind in FRAME_SCOPED_KINDS:
        frame = row.get("frame")
        if isinstance(frame, bool) or not isinstance(frame, int) or frame < 1:
            return (
                f"{kind} must declare `frame` as a positive integer: this reading "
                "changes with the frame, and an undeclared frame silently measures "
                "frame 1"
            )
    if kind == "onset_order":
        primary = set(_selectors(row, "roles")) | set(_selectors(row, "control_roles"))
        compare = set(_selectors(row, "compare_roles")) | set(
            _selectors(row, "compare_control_roles")
        )
        if not compare:
            return "onset_order requires compare_roles or compare_control_roles"
        # The metric is onset(compare) - onset(roles). Overlapping selectors compare a
        # set against itself, which is 0 by construction — a contract that can never
        # pass and never fails honestly. Run 20260823T154920Z burned a build on one.
        shared = sorted(primary & compare)
        if shared:
            return (
                "onset_order selectors must be disjoint; "
                + ", ".join(shared)
                + " appears on both sides, which forces the difference to 0 regardless "
                "of the scene"
            )
    if kind == "transform_return_delta" and row.get("component", "location") not in {
        "location",
        "rotation",
        "scale",
    }:
        return "transform_return_delta component must be location, rotation, or scale"
    if kind == "frame_delta":
        region = row.get("region")
        if region is not None and (
            not isinstance(region, list)
            or len(region) != 4
            or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in region)
            or not all(0 <= float(v) <= 1 for v in region)
            or not (region[0] < region[2] and region[1] < region[3])
        ):
            return "frame_delta region must be a normalized TOP-LEFT box"
    if kind == "node_socket_value" and (
        not row.get("socket") or row.get("direction", "input") not in {"input", "output"}
    ):
        return "node_socket_value requires socket and input/output direction"
    if row.get("socket_index") is not None and (
        not isinstance(row.get("socket_index"), int)
        or isinstance(row.get("socket_index"), bool)
        or row["socket_index"] < 0
    ):
        return "socket_index must be a non-negative integer"
    if kind == "node_link_count" and (not _selectors(row, "from_node_roles") or not _selectors(row, "to_node_roles")):
        return "node_link_count requires from_node_roles and to_node_roles"
    if kind == "object_property" and not row.get("property"):
        return "object_property requires a numeric property path"
    if kind == "animation_count" and row.get("domain", "all") not in {
        "all",
        "objects",
        "materials",
        "node_trees",
        "world",
        "scene",
    }:
        return "animation_count domain is invalid"
    op = str(row.get("op", "band"))
    if op not in SUPPORTED_OPS:
        return f"unsupported op {op!r}"
    try:
        if op == "band":
            if row.get("lo") is None or row.get("hi") is None:
                return "band requires lo and hi"
            if float(row["lo"]) > float(row["hi"]):
                return "band lo exceeds hi"
        elif op == "eq":
            float(row["value"])
            float(row.get("tol", 0))
        elif op == "min":
            float(row["lo"])
        else:
            float(row["hi"])
    except (KeyError, TypeError, ValueError):
        return f"{op} threshold must be numeric"
    return None


def _blender_probe(rows: list[dict], frame: int) -> str:
    payload = json.dumps(rows)
    return f"""\
import bpy, fnmatch, json, math
from bpy_extras.object_utils import world_to_camera_view
_rows=json.loads({json.dumps(payload)}); _scene=bpy.context.scene; _scene.frame_set({int(frame)})
_dg=bpy.context.evaluated_depsgraph_get(); _camera=_scene.camera; _out=[]
def _p(row,key):
    v=row.get(key) or []
    return [v] if isinstance(v,str) else v
def _m(v,pats): return any(fnmatch.fnmatchcase(str(v or ''),p) for p in pats)
def _objects(row):
    return sorted([o for o in _scene.objects
                   if (not _p(row,'roles') or _m(o.get('bvfx_role'),_p(row,'roles')))
                   and (not _p(row,'control_roles') or
                        _m(o.get('bvfx_control'),_p(row,'control_roles')))],key=lambda o:o.name)
def _materials(row):
    return sorted([m for m in bpy.data.materials
                   if _m(m.get('bvfx_role'),_p(row,'material_roles'))],key=lambda m:m.name)
def _nr(n): return n.get('bvfx_control') or n.get('bvfx_role') or ''
def _graphs(row):
    if row.get('graph')=='material': return [(m.name,m.node_tree) for m in _materials(row) if m.node_tree]
    if row.get('graph')=='compositor':
        ng=getattr(_scene,'compositing_node_group',None); return [('compositor',ng)] if ng else []
    nt=_scene.world.node_tree if _scene.world and _scene.world.use_nodes else None
    return [('world',nt)] if nt else []
def _projected(objects):
    coords=[]
    for obj in objects:
        ev=obj.evaluated_get(_dg); mesh=None
        if ev.type=='MESH':
            try: mesh=ev.to_mesh(); points=[ev.matrix_world@v.co for v in mesh.vertices]
            finally:
                if mesh is not None: ev.to_mesh_clear()
        else: points=[ev.matrix_world@__import__('mathutils').Vector(c) for c in ev.bound_box]
        for point in points:
            ndc=world_to_camera_view(_scene,_camera,point)
            if ndc.z>0: coords.append((float(ndc.x),float(1-ndc.y)))
    return coords
def _property(target,path):
    value=target
    for token in str(path).split('.'):
        value=value[int(token)] if token.isdigit() else getattr(value,token)
    return float(value)
def _raw_property(target,path):
    value=target
    for token in str(path).split('.'):
        value=value[int(token)] if token.isdigit() else getattr(value,token)
    try: return tuple(float(v) for v in value)
    except TypeError: return float(value)
def _delta(actual,expected):
    if isinstance(expected,list):
        actual=tuple(actual)
        if len(actual)!=len(expected): raise ValueError('scheduled vector length differs')
        return max(abs(float(a)-float(b)) for a,b in zip(actual,expected))
    return abs(float(actual)-float(expected))
def _animated(v): return int(bool(getattr(v,'animation_data',None)))
def _fcurves(target):
    ad=getattr(target,'animation_data',None)
    if not ad or not ad.action: return []
    legacy=getattr(ad.action,'fcurves',None)
    if legacy and len(legacy): return list(legacy)
    out=[]; slot=getattr(ad,'action_slot',None)
    for layer in getattr(ad.action,'layers',[]):
        for strip in getattr(layer,'strips',[]):
            bags=[]
            if slot is not None and hasattr(strip,'channelbag'):
                try:
                    cb=strip.channelbag(slot)
                    if cb is not None: bags=[cb]
                except Exception: bags=[]
            if not bags: bags=list(getattr(strip,'channelbags',[]))
            for cb in bags: out.extend(cb.fcurves)
    return out
def _state(items,frame):
    _scene.frame_set(int(frame)); dg=bpy.context.evaluated_depsgraph_get(); out=[]
    for item in items:
        ev=item.evaluated_get(dg)
        out.append((item.name,tuple(float(v) for row in ev.matrix_world for v in row),
                    bool(ev.hide_render),bool(ev.hide_viewport)))
    return out
def _onset(items,start,end,epsilon):
    base=_state(items,start)
    for frame in range(int(start)+1,int(end)+1):
        current=_state(items,frame)
        for before,after in zip(base,current):
            if before[0]!=after[0] or before[2:]!=after[2:]: return frame
            if max(abs(a-b) for a,b in zip(before[1],after[1]))>epsilon: return frame
    raise ValueError('selector has no evaluated transform/visibility onset in frame window')
def _transforms(items,frame):
    _scene.frame_set(int(frame)); dg=bpy.context.evaluated_depsgraph_get(); out={{}}
    for item in items:
        loc,rot,scale=item.evaluated_get(dg).matrix_world.decompose()
        out[item.name]=(loc.copy(),rot.copy(),scale.copy())
    return out
for row in _rows:
    kind=row['kind']; value=None; error=''; objects=(
        _objects(row) if row.get('roles') or row.get('control_roles') else [])
    materials=_materials(row) if row.get('material_roles') else []; matched=[]
    try:
        if kind=='object_count': value=len(objects)
        elif kind.startswith('bbox_'):
            pts=_projected(objects)
            if not pts: raise ValueError('no selected geometry is in front of the camera')
            xs=[p[0] for p in pts]; ys=[p[1] for p in pts]; x0,x1,y0,y1=min(xs),max(xs),min(ys),max(ys)
            value={{'bbox_width':x1-x0,'bbox_height':y1-y0,'bbox_center_x':(x0+x1)/2,'bbox_center_y':(y0+y1)/2,'bbox_top_y':y0,'bbox_bottom_y':y1}}[kind]
        elif kind=='mesh_vertex_count':
            value=sum(len(o.evaluated_get(_dg).data.vertices)
                      for o in objects if o.type=='MESH')
        elif kind=='smooth_fraction':
            ps=[p for o in objects if o.type=='MESH' for p in o.data.polygons]
            value=sum(1 for p in ps if p.use_smooth)/len(ps)
        elif kind=='radial_inward_fraction':
            tested=[]
            for o in objects:
                if o.type!='MESH': continue
                ev=o.evaluated_get(_dg); mw=ev.matrix_world
                for p in ev.data.polygons:
                    c=mw@p.center
                    n=(mw.to_3x3()@p.normal).normalized()
                    r=(c.x*c.x+c.y*c.y)**.5
                    if r>=1e-9 and abs(n.z)<=.9: tested.append((n.x*c.x+n.y*c.y)/r<=0)
            if not tested: raise ValueError('no radial faces to test on the selection')
            value=sum(tested)/len(tested)
        elif kind=='object_property':
            vs=[_property(o,row['property']) for o in objects]
            if not vs: raise ValueError('selector matched no objects')
            if max(vs)-min(vs)>float(row.get('uniform_tol',1e-6)):
                raise ValueError('selected objects do not share one property value')
            value=sum(vs)/len(vs)
        elif kind=='material_count': value=len(materials)
        elif kind=='material_user_count': value=sum(m.users for m in materials)
        elif kind=='material_assignment_fraction':
            wanted=_p(row,'material_roles')
            if not objects: raise ValueError('selector matched no objects')
            good=0
            for o in objects:
                assigned=[s.material for s in o.material_slots if s.material]
                good+=int(bool(assigned) and all(_m(m.get('bvfx_role'),wanted) for m in assigned))
            value=good/len(objects)
        elif kind in ('node_count','node_socket_value'):
            wanted=_p(row,'node_roles')
            for graph,nt in _graphs(row):
                for node in nt.nodes:
                    if _m(_nr(node),wanted): matched.append((graph,node))
            if kind=='node_count': value=len(matched)
            else:
                if len(matched)!=1: raise ValueError(f'node selector matched {{len(matched)}} nodes')
                node=matched[0][1]; sockets=node.inputs if row.get('direction','input')=='input' else node.outputs
                socket=(sockets[int(row['socket_index'])] if row.get('socket_index') is not None
                        else sockets.get(row['socket']))
                if socket is None: raise ValueError('semantic node has no requested socket')
                raw=socket.default_value; comp=row.get('component')
                value=float(raw[int(comp)]) if comp is not None else float(raw)
        elif kind=='node_link_count':
            value=0
            for graph,nt in _graphs(row):
                for link in nt.links:
                    if (not _m(_nr(link.from_node),_p(row,'from_node_roles'))
                            or not _m(_nr(link.to_node),_p(row,'to_node_roles'))):
                        continue
                    if row.get('from_socket') and link.from_socket.name!=row['from_socket']: continue
                    if row.get('to_socket') and link.to_socket.name!=row['to_socket']: continue
                    value+=1
        elif kind=='compositor_enabled':
            ng=getattr(_scene,'compositing_node_group',None); wanted=_p(row,'node_group_roles')
            value=int(bool(_scene.render.use_compositing and ng and (not wanted or _m(ng.get('bvfx_role'),wanted))))
        elif kind=='animation_count':
            domain=row.get('domain','all'); values=[]
            if domain in ('all','objects'): values+=objects
            if domain in ('all','materials'): values+=materials
            if domain in ('all','node_trees'): values += [m.node_tree for m in materials if m.node_tree]
            if domain in ('all','world') and _scene.world: values += [_scene.world,_scene.world.node_tree]
            if domain in ('all','scene'): values += [_scene]
            value=sum(_animated(v) for v in values if v is not None)
        elif kind=='keyframe_schedule':
            if not objects: raise ValueError('selector matched no objects')
            samples=row['samples']; expected_frames={{int(s['frame']) for s in samples}}
            paths=set(samples[0]['values']); deltas=[]
            for o in objects:
                action=getattr(getattr(o,'animation_data',None),'action',None)
                if action is None: raise ValueError('scheduled object has no action')
                for path in paths:
                    actual_frames={{int(round(k.co.x)) for fc in _fcurves(o)
                                   if fc.data_path==path for k in fc.keyframe_points}}
                    if actual_frames!=expected_frames:
                        raise ValueError(f'{{path}} keyframes {{sorted(actual_frames)}} != {{sorted(expected_frames)}}')
                for sample in samples:
                    _scene.frame_set(int(sample['frame'])); dg=bpy.context.evaluated_depsgraph_get()
                    ev=o.evaluated_get(dg)
                    for path,expected in sample['values'].items():
                        deltas.append(_delta(_raw_property(ev,path),expected))
            value=max(deltas) if deltas else 0.0
        elif kind=='onset_order':
            other=_objects({{**row,
                'roles':_p(row,'compare_roles'),
                'control_roles':_p(row,'compare_control_roles')}})
            if not objects or not other: raise ValueError('onset selector matched no objects')
            a,b=row['frames']; epsilon=float(row.get('motion_epsilon',1e-5))
            value=_onset(other,a,b,epsilon)-_onset(objects,a,b,epsilon)
        elif kind=='radial_distance_trend':
            if not objects: raise ValueError('selector matched no objects')
            a,b=row['frames']; samples=[]
            for f in range(int(a),int(b)+1):
                _scene.frame_set(int(f)); dg=bpy.context.evaluated_depsgraph_get()
                samples.append(sum((o.evaluated_get(dg).matrix_world.translation.x**2+
                                    o.evaluated_get(dg).matrix_world.translation.y**2)**.5
                                   for o in objects)/len(objects))
            xs=list(range(len(samples))); xm=sum(xs)/len(xs); ym=sum(samples)/len(samples)
            value=sum((x-xm)*(y-ym) for x,y in zip(xs,samples))/max(sum((x-xm)**2 for x in xs),1e-12)
        elif kind=='transform_return_delta':
            if not objects: raise ValueError('selector matched no objects')
            a,b=row['frames']; first=_transforms(objects,a); second=_transforms(objects,b)
            component=row.get('component','location'); deltas=[]
            for name in first:
                if component=='location': deltas.append((second[name][0]-first[name][0]).length)
                elif component=='scale': deltas.append((second[name][2]-first[name][2]).length)
                else: deltas.append(first[name][1].rotation_difference(second[name][1]).angle)
            value=max(deltas)
    except Exception as exc: value=None; error=str(exc)[:160]
    _out.append({{'id':row['id'],'value':value,'objects':[o.name for o in objects],
      'roles':[str(o.get('bvfx_role','')) for o in objects],'materials':[m.name for m in materials],
      'controls':[str(o.get('bvfx_control','')) for o in objects],
      'material_roles':[str(m.get('bvfx_role','')) for m in materials],
      'nodes':[n.name for g,n in matched],'error':error}})
RESULT=_out
"""


def _evidence(rows: list[dict], raw: list[dict]) -> list[dict]:
    readings = {str(item.get("id")): item for item in raw if isinstance(item, dict)}
    out = []
    for row in rows:
        reading = readings.get(str(row.get("id")), {})
        error = validate_row(row) or str(reading.get("error") or "")
        value = reading.get("value")
        if isinstance(value, float):
            value = round(value, 6)
        out.append(
            {
                "id": str(row.get("id") or "<missing>"),
                "axis": str(row.get("axis") or ""),
                "metric": str(row.get("kind") or "scene_contract"),
                "definition": KIND_DEFINITIONS.get(str(row.get("kind") or ""), ""),
                "value": value,
                "target": _target(row),
                "pass": not error and _holds(row, reading.get("value")),
                "origin": "planner",
                "source": "interface_contract",
                "authoritative": True,
                "owner_layer": str(row.get("owner_layer") or ""),
                "fault_owner": str(row.get("fault_owner") or ""),
                "activates_at": str(row.get("activates_at") or ""),
                "lifecycle": str(row.get("lifecycle") or ""),
                "objects": list(reading.get("objects") or []),
                "roles": list(reading.get("roles") or []),
                "controls": list(reading.get("controls") or []),
                "materials": list(reading.get("materials") or []),
                "material_roles": list(reading.get("material_roles") or []),
                "nodes": list(reading.get("nodes") or []),
                **({"error": error} if error else {}),
            }
        )
    return out


def load_rows(shot_folder: str | Path) -> list[dict]:
    from vfx_harness.orchestration.plan_authority import selected_artifact_path

    return load_document(selected_artifact_path(shot_folder, "scene_checks.json"), "contracts")


def layer_evidence(shot_folder: str | Path, layer_id: str, *, frame: int, session) -> list[dict]:
    """Evaluate every lifecycle-active contract, including persistent prior interfaces."""
    from vfx_harness.orchestration.plan_authority import selected_artifact_path

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
import bpy, fnmatch, json
spec=json.loads({json.dumps(spec)})
def match(value, patterns): return any(fnmatch.fnmatchcase(str(value or ''), p) for p in patterns)
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
       if match(n.get('bvfx_control') or n.get('bvfx_role'),spec['node_roles'])]
if len(nodes)!=1: raise ValueError(f"semantic control matched {{len(nodes)}} nodes")
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
if socket is None: raise ValueError(f"semantic control has no requested {{direction}} socket")
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
            else:
                initial = session.run(_control_script(row), journal=False).get("result") or {}
                original = float(initial["before"])
                probe_values = row["probe_values"]
            for probe_value in probe_values:
                if row.get("kind") != "frame_delta":
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
            low, high = images
            if row.get("kind") == "frame_delta" or row.get("response_metric", "mean_delta") == "mae":
                if low.size != high.size:
                    raise ValueError("rendered frames have different dimensions")
                value = ImageStat.Stat(ImageChops.difference(low, high).convert("L")).mean[0]
            else:
                low_mean = ImageStat.Stat(low.convert("L")).mean[0]
                high_mean = ImageStat.Stat(high.convert("L")).mean[0]
                value = high_mean - low_mean
        except Exception as exc:
            error = str(exc)[:160]
        finally:
            if original is not None:
                try:
                    session.run(_control_script(row, original), journal=False)
                except Exception as exc:
                    error = f"restore failed: {exc}"[:160]
        out.append(
            {
                "id": str(row.get("id") or "<missing>"),
                "axis": str(row.get("axis") or ""),
                "metric": str(row.get("kind") or "control_render_response"),
                "definition": KIND_DEFINITIONS[str(row.get("kind") or "control_render_response")],
                "value": round(value, 4) if isinstance(value, (int, float)) else None,
                "target": _target(row),
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
    from vfx_harness.orchestration.plan_authority import selected_artifact_path

    path = selected_artifact_path(shot_folder, "scene_checks.json")
    if not path.is_file():
        return []
    rows = load_rows(shot_folder)
    current = int(layer_id)
    selected = [
        r
        for r in rows
        if isinstance(r, dict)
        and not validate_lifecycle(r)
        and int(r["owner_layer"]) < current
        and active_for(r, current)
    ]
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
