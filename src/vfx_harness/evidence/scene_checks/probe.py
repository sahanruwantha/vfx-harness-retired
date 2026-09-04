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

from vfx_harness.domain.evidence_kinds import PROJECTED_ORIGIN_KINDS as PROJECTED_ORIGIN_KINDS
from vfx_harness.evidence.scene_checks.kinds import KIND_DEFINITIONS, PATH_CLEARANCE_UNMEASURED
from vfx_harness.evidence.scene_checks.validate import (
    _argmax_span,
    _derivative_segments,
    _holds,
    _optional_hi,
    _target,
    curve_derivative_note,
    validate_row,
    visible_fraction_note,
)


def _blender_probe(rows: list[dict], frame: int) -> str:
    payload = json.dumps(rows)
    return f"""\
import bpy, fnmatch, json, math
import checks as _checks  # worker sibling — the ONE projection implementation (ADR-0003)
_rows=json.loads({json.dumps(payload)}); _scene=bpy.context.scene; _FRAME={int(frame)}
_scene.frame_set(_FRAME); _camera=_scene.camera; _out=[]
def _p(row,key):
    v=row.get(key) or []
    return [v] if isinstance(v,str) else v
_m=_checks.match_semantic
def _objects(row):
    return sorted([o for o in _scene.objects
                   if (not _p(row,'roles') or _m(o.get('bvfx_role'),_p(row,'roles')))
                   and (not _p(row,'control_roles') or
                        _m(o.get('bvfx_control'),_p(row,'control_roles')))],key=lambda o:o.name)
def _materials(row):
    return sorted([m for m in bpy.data.materials
                   if _m(m.get('bvfx_role'),_p(row,'material_roles'))],key=lambda m:m.name)
def _nr(n): return n.get('bvfx_control') or n.get('bvfx_role') or ''
def _nm(n,pats):
    # Either-of, never precedence: with `control or role` a control-tagged node's ROLE
    # was unreachable by any selector — run 20260825 tagged AtmosphereVolume with both,
    # and its role selector silently resolved to the wrong node ("matched 1").
    return _m(n.get('bvfx_control'),pats) or _m(n.get('bvfx_role'),pats)
def _graphs(row):
    if row.get('graph')=='material': return [(m.name,m.node_tree) for m in _materials(row) if m.node_tree]
    if row.get('graph')=='compositor':
        ng=getattr(_scene,'compositing_node_group',None); return [('compositor',ng)] if ng else []
    nt=_scene.world.node_tree if _scene.world and _scene.world.use_nodes else None
    return [('world',nt)] if nt else []
def _projected(objects,dg):
    # Evaluated meshes are clipped against the camera frustum as EDGES before the
    # perspective divide (checks.frustum_union_ndc), so the union is the visible
    # portion and every coordinate is inside [0,1] by construction. The previous
    # vertex-projection accepted off-frustum blowup: a camera facing away from its
    # subject read bbox_height 1132.53608 "normalized" and PASSED >= 0.25
    # (run 20260824T103842Z-afec73). `points` also used to leak across iterations:
    # an object whose to_mesh() failed reused the PREVIOUS object's vertices.
    _V=__import__('mathutils').Vector
    _mvp=_checks.camera_clip_matrix(_scene,dg)
    clip=[]; edges=[]; base=0; empty=0; hidden=[]
    for obj in objects:
        ev=obj.evaluated_get(dg); mesh=None; points=[]; pairs=[]
        # A bbox_* row measures the RENDERED subject, so an object hidden from render
        # contributes nothing. Counting it made the metric unable to answer the ablation
        # a builder performs to find its own contribution (HIR-0196).
        if not _checks.renders_in_frame(ev):
            hidden.append(obj.name); continue
        if ev.type=='MESH':
            try:
                mesh=ev.to_mesh()
                points=[ev.matrix_world@v.co for v in mesh.vertices]
                pairs=[(e.vertices[0],e.vertices[1]) for e in mesh.edges]
            except Exception:
                points=[ev.matrix_world@_V(c) for c in ev.bound_box]; pairs=list(_checks.BOX_EDGES)
            finally:
                if mesh is not None: ev.to_mesh_clear()
        else:
            points=[ev.matrix_world@_V(c) for c in ev.bound_box]; pairs=list(_checks.BOX_EDGES)
        if not points: empty+=1
        clip.extend(tuple(_mvp@p.to_4d()) for p in points)
        edges.extend((base+a,base+b) for a,b in pairs)
        base+=len(points)
    return _checks.frustum_union_ndc(clip,edges),empty,tuple(hidden)
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
def _path_aliases(path):
    p=str(path)
    if p.startswith('[') or p.startswith('data.'):
        return (p,)
    return (p, 'data.'+p)
def _eval_property(target,path):
    last=None
    for alias in _path_aliases(path):
        try: return _raw_property(target, alias)
        except Exception as exc: last=exc
    raise last
def _readable(target,path):
    try: _raw_property(target,path); return True
    except Exception: return False
def _carriers(objects,paths):
    # A `data.*` alias names a DATA-BLOCK property. A host with no data-block at all (an
    # Empty pivot in a camera rig, a control marker) cannot carry it and is typed out of
    # the closure; a host WITH a data-block that lacks the attribute remains a failing
    # measurement. The closure must keep at least one carrier — the caller fails closed
    # otherwise — so a typed-out host can never hide a subject (HIR-0174).
    aliases=[a for path in paths for a in _path_aliases(path)]
    if not any(str(a).startswith('data.') for a in aliases): return list(objects),[]
    typed_out=[o for o in objects if getattr(o,'data',None) is None
               and not any(not str(a).startswith('data.') and _readable(o,a) for a in aliases)]
    return [o for o in objects if o not in typed_out],typed_out
def _nocarrier(row,paths,typed_out):
    return ('roles '+repr(_p(row,'roles'))+' matched only hosts without a data-block for '
            'data-block path(s) '+repr(sorted(str(p) for p in paths))+': '
            +', '.join(o.name+' ['+o.type+']' for o in typed_out)
            +'; tag the data-block host (Camera/Light/Mesh) with the role or bind the row on '
            'its role')
def _typed_out_note(typed_out):
    return ('no data-block, not a carrier: '
            +', '.join(o.name+' ['+o.type+']' for o in typed_out)) if typed_out else ''
def _host_fcurves(o):
    rows=[]
    for fc in _fcurves(o):
        rows.append(('object', fc.data_path, fc))
    data=getattr(o,'data',None)
    if data is not None:
        for fc in _fcurves(data):
            rows.append(('data', fc.data_path, fc))
    return rows
def _schedule_frames(o, path):
    aliases=set(_path_aliases(path)); frames=set()
    for kind, dp, fc in _host_fcurves(o):
        if kind=='object' and dp in aliases:
            frames.update(int(round(k.co.x)) for k in fc.keyframe_points)
        elif kind=='data' and (dp in aliases or ('data.'+dp) in aliases):
            frames.update(int(round(k.co.x)) for k in fc.keyframe_points)
    return frames
def _present_paths(o):
    out=[]
    for kind, dp, _fc in _host_fcurves(o):
        out.append(dp if kind=='object' else (dp if str(dp).startswith('data.') else 'data.'+dp))
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
def _seen_tags(row):
    tags=[]
    for _g,_nt in _graphs(row):
        for n in _nt.nodes:
            tags += [str(n.get('bvfx_control') or ''), str(n.get('bvfx_role') or '')]
    return sorted(set(t for t in tags if t))[:16]
def _seen_object_roles():
    return sorted(set(str(o.get('bvfx_role')) for o in bpy.data.objects if o.get('bvfx_role')))[:24]
def _missobj(row):
    # A bare "matched no objects" left probing the live scene as the only way to learn
    # what WAS tagged; the miss must name both sides or every selector typo costs a session.
    return ('roles '+repr(_p(row,'roles'))+' / control_roles '+repr(_p(row,'control_roles'))+
            ' matched no objects; object roles present: '+(', '.join(_seen_object_roles()) or '(none)'))
for row in _rows:
    kind=row['kind']; value=None; error=''; note=''; segments=[]; role_fractions={{}}
    # Every row measures its DECLARED frame with its own depsgraph. Temporal kinds
    # (keyframe_schedule, onset_order, …) excurse to other frames and never restored
    # the batch frame, so every later row silently measured whatever frame the previous
    # row parked the scene at — run 20260824T103842Z-afec73 sealed frame-240 readings
    # as f1 and f36 evidence. Ambient shared state is not an instrument.
    _scene.frame_set(_FRAME)
    _row_dg=bpy.context.evaluated_depsgraph_get()
    objects=(
        _objects(row) if row.get('roles') or row.get('control_roles') else [])
    materials=_materials(row) if row.get('material_roles') else []; matched=[]
    try:
        if kind=='object_count':
            value=len(objects)
            if not objects and (row.get('roles') or row.get('control_roles')): note=_missobj(row)
            else:
                literal=[r for r in _p(row,'roles') if not any(c in r for c in '*?[')]
                below=[o for o in objects if o.get('bvfx_role') not in literal
                       and any(str(o.get('bvfx_role','')).startswith(r+'.') for r in literal)]
                if below:
                    note=('literal selector(s) '+repr(literal)+' also match dotted descendants '
                          '(HIR-0147): '+', '.join(o.name+'('+str(o.get('bvfx_role'))+')' for o in below)
                          +'; count a leaf role to count one host')
        elif kind.startswith('bbox_'):
            if not objects: raise ValueError(_missobj(row))
            rec,empty,hidden=_projected(objects,_row_dg)
            if rec is None:
                _hid=(' '+str(len(hidden))+' hidden from render: '+', '.join(hidden)
                      if hidden else '')
                raise ValueError(
                    f'none of {{len(objects)}} selected object(s) intersects the camera frustum '
                    f'at frame {{_FRAME}} ({{empty}} contributed no points){{_hid}}')
            x0,y0,x1,y1=rec['bbox']
            value={{'bbox_width':x1-x0,'bbox_height':y1-y0,'bbox_center_x':(x0+x1)/2,'bbox_center_y':(y0+y1)/2,'bbox_top_y':y0,'bbox_bottom_y':y1}}[kind]
        elif kind in ('projected_origin_x','projected_origin_y'):
            if not objects: raise ValueError(_missobj(row))
            if len(objects)!=1:
                raise ValueError(
                    f'{{kind}} requires exactly one selected object origin; matched '
                    f'{{len(objects)}} objects: '+', '.join(o.name for o in objects))
            if _scene.camera is None: raise ValueError('scene has no camera at the declared frame')
            point=objects[0].evaluated_get(_row_dg).matrix_world.translation.to_4d()
            clip=_checks.camera_clip_matrix(_scene,_row_dg)@point
            if clip.w<=0: raise ValueError('selected object origin is behind the active camera')
            nx=clip.x/clip.w; ny=clip.y/clip.w
            value=(nx+1.0)/2.0 if kind=='projected_origin_x' else (1.0-ny)/2.0
        elif kind=='visible_fraction':
            # Of EACH named role's ON-SCREEN surface samples, the fraction whose camera
            # ray reaches that role before anything else. A pooled union hid a failing
            # core behind passing rings (HIR-0051): the scalar is min(per-role), and a
            # named role with no samples reads 0.0. Zero on-screen samples is a failing
            # measurement, not an instrument error (HIR-0019).
            if not objects: raise ValueError(_missobj(row))
            if _scene.camera is None: raise ValueError('scene has no camera at the declared frame')
            def _vis_frac(sel):
                if not sel: return 0.0
                return _checks.surface_visible_fraction(
                    _scene,_row_dg,_scene.camera,sel)['visible_fraction']
            named=_p(row,'roles')
            if named:
                for role in named:
                    role_fractions[str(role)]=_vis_frac(_objects({{**row,'roles':[role]}}))
                value=min(role_fractions.values()) if role_fractions else 0.0
                note='per-role '+', '.join(r+'='+('%.6g'%role_fractions[r]) for r in named)
            else:
                value=_vis_frac(objects)
        elif kind=='mesh_vertex_count':
            value=sum(len(o.evaluated_get(_row_dg).data.vertices)
                      for o in objects if o.type=='MESH')
        elif kind=='smooth_fraction':
            ps=[p for o in objects if o.type=='MESH' for p in o.data.polygons]
            value=sum(1 for p in ps if p.use_smooth)/len(ps)
        elif kind=='radial_inward_fraction':
            tested=[]
            for o in objects:
                if o.type!='MESH': continue
                ev=o.evaluated_get(_row_dg); mw=ev.matrix_world
                for p in ev.data.polygons:
                    c=mw@p.center
                    n=(mw.to_3x3()@p.normal).normalized()
                    r=(c.x*c.x+c.y*c.y)**.5
                    if r>=1e-9 and abs(n.z)<=.9: tested.append((n.x*c.x+n.y*c.y)/r<=0)
            if not tested: raise ValueError('no radial faces to test on the selection')
            value=sum(tested)/len(tested)
        elif kind=='object_property':
            if not objects: raise ValueError(_missobj(row))
            carriers,typed_out=_carriers(objects,[row['property']])
            if not carriers: raise ValueError(_nocarrier(row,[row['property']],typed_out))
            note=_typed_out_note(typed_out)
            vs=[_property(o,row['property']) for o in carriers]
            if max(vs)-min(vs)>float(row.get('uniform_tol',1e-6)):
                raise ValueError('selected objects do not share one property value')
            value=sum(vs)/len(vs)
        elif kind=='material_count': value=len(materials)
        elif kind=='material_user_count': value=sum(m.users for m in materials)
        elif kind=='material_assignment_fraction':
            wanted=_p(row,'material_roles')
            if not objects: raise ValueError(_missobj(row))
            # only material-capable members are judged: an Empty marker counted as
            # "unassigned" makes the metric unsatisfiable over any mixed selection
            # (run 20260826: the dressed collective tier role includes layer 1's
            # Empties and the honest 3/3-mesh dressing read 0.5 forever)
            capable=[o for o in objects if hasattr(o.data,'materials') if o.data is not None]
            if not capable:
                raise ValueError('selected roles contain no material-capable objects '
                                 '(types: '+', '.join(sorted(set(o.type for o in objects)))+')')
            good=0
            for o in capable:
                assigned=[s.material for s in o.material_slots if s.material]
                good+=int(bool(assigned) and all(_m(m.get('bvfx_role'),wanted) for m in assigned))
            value=good/len(capable)
        elif kind in ('node_count','node_socket_value'):
            wanted=_p(row,'node_roles')
            for graph,nt in _graphs(row):
                for node in nt.nodes:
                    if _nm(node,wanted): matched.append((graph,node))
            if kind=='node_count':
                value=len(matched)
                if not matched and wanted:
                    note=('node_roles '+repr(wanted)+' matched 0 nodes in '+repr(row.get('graph'))+
                          ' graph(s); semantic tags present: '+(', '.join(_seen_tags(row)) or '(none)'))
            else:
                if len(matched)!=1:
                    raise ValueError('node_roles '+repr(wanted)+' matched '+str(len(matched))+' nodes'
                                     +' in '+repr(row.get('graph'))+' graph(s); semantic tags present: '
                                     +(', '.join(_seen_tags(row)) or '(none)'))
                node=matched[0][1]; sockets=node.inputs if row.get('direction','input')=='input' else node.outputs
                socket=(sockets[int(row['socket_index'])] if row.get('socket_index') is not None
                        else sockets.get(row['socket']))
                if socket is None:
                    raise ValueError('semantic node '+node.bl_idname+' has no requested socket '
                                     +repr(row.get('socket'))+'; available '
                                     +row.get('direction','input')+' sockets: '
                                     +(', '.join(s.name for s in sockets) or '(none)'))
                raw=socket.default_value; comp=row.get('component')
                # authors write channels as letters; run d2ea42 authored component 'B'
                # and int('B') killed the row as a binding defect
                if comp is not None:
                    comp={{'R':0,'G':1,'B':2,'A':3}}.get(str(comp).upper().strip(),comp)
                value=float(raw[int(comp)]) if comp is not None else float(raw)
        elif kind=='node_link_count':
            value=0
            for graph,nt in _graphs(row):
                for link in nt.links:
                    if (not _nm(link.from_node,_p(row,'from_node_roles'))
                            or not _nm(link.to_node,_p(row,'to_node_roles'))):
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
            if not objects: raise ValueError(_missobj(row))
            samples=row['samples']; expected_frames={{int(s['frame']) for s in samples}}
            paths=set(samples[0]['values']); deltas=[]; miss=[]
            miss_floor=float(row.get('hi') or 0)+1.0
            carriers,typed_out=_carriers(objects,paths)
            if not carriers: raise ValueError(_nocarrier(row,paths,typed_out))
            for o in carriers:
                present=_present_paths(o)
                frames_ok=True
                for path in paths:
                    actual_frames=_schedule_frames(o, path)
                    if actual_frames!=expected_frames:
                        frames_ok=False
                        miss.append(
                            o.name+' '+repr(path)+' aliases '+repr(list(_path_aliases(path)))+
                            ': keyframes '+repr(sorted(actual_frames))+' != '+repr(sorted(expected_frames))+
                            '; fcurve data_paths present: '+
                            (', '.join(repr(p) for p in present) or '(none)'))
                for sample in samples:
                    _scene.frame_set(int(sample['frame'])); dg=bpy.context.evaluated_depsgraph_get()
                    ev=o.evaluated_get(dg)
                    for path,expected in sample['values'].items():
                        try:
                            deltas.append(_delta(_eval_property(ev,path),expected))
                        except Exception as exc:
                            frames_ok=False
                            miss.append(
                                o.name+' '+repr(path)+' unreadable: '+str(exc)[:160]+
                                '; fcurve data_paths present: '+
                                (', '.join(repr(p) for p in present) or '(none)'))
                            deltas.append(miss_floor)
                if not frames_ok:
                    deltas.append(miss_floor)
            notes=([_typed_out_note(typed_out)] if typed_out else [])+miss
            if notes:
                note='; '.join(notes)[:400]
            value=max(deltas) if deltas else 0.0
        elif kind=='onset_order':
            other=_objects({{**row,
                'roles':_p(row,'compare_roles'),
                'control_roles':_p(row,'compare_control_roles')}})
            if not objects or not other: raise ValueError('onset selector matched no objects')
            a,b=row['frames']; epsilon=float(row.get('motion_epsilon',1e-5))
            value=_onset(other,a,b,epsilon)-_onset(objects,a,b,epsilon)
        elif kind=='radial_distance_trend':
            if not objects: raise ValueError(_missobj(row))
            a,b=row['frames']; samples=[]
            for f in range(int(a),int(b)+1):
                _scene.frame_set(int(f)); dg=bpy.context.evaluated_depsgraph_get()
                samples.append(sum((o.evaluated_get(dg).matrix_world.translation.x**2+
                                    o.evaluated_get(dg).matrix_world.translation.y**2)**.5
                                   for o in objects)/len(objects))
            xs=list(range(len(samples))); xm=sum(xs)/len(xs); ym=sum(samples)/len(samples)
            value=sum((x-xm)*(y-ym) for x,y in zip(xs,samples))/max(sum((x-xm)**2 for x in xs),1e-12)
        elif kind=='transform_return_delta':
            if not objects: raise ValueError(_missobj(row))
            a,b=row['frames']; first=_transforms(objects,a); second=_transforms(objects,b)
            component=row.get('component','location'); deltas=[]
            for name in first:
                if component=='location': deltas.append((second[name][0]-first[name][0]).length)
                elif component=='scale': deltas.append((second[name][2]-first[name][2]).length)
                else: deltas.append(first[name][1].rotation_difference(second[name][1]).angle)
            value=max(deltas)
        elif kind=='curve_derivative_max':
            if not objects: raise ValueError(_missobj(row))
            a,b=row['frames']; path=row.get('property') or 'location'
            deltas=[]; prev=None; prev_f=None
            for f in range(int(a),int(b)+1):
                _scene.frame_set(int(f)); dg=bpy.context.evaluated_depsgraph_get()
                cur=[_eval_property(o.evaluated_get(dg),path) for o in objects]
                if prev is not None:
                    pair=None
                    for before,after in zip(prev,cur):
                        bv=before if isinstance(before,tuple) else (before,)
                        av=after if isinstance(after,tuple) else (after,)
                        step=max(abs(x-y) for x,y in zip(av,bv))
                        pair=step if pair is None or step>pair else pair
                    if pair is not None:
                        deltas.append(pair)
                        segments.append([int(prev_f), int(f), pair])
                prev=cur; prev_f=f
            if not deltas: raise ValueError('frame window has no adjacent frame pair')
            value=max(deltas)
        elif kind=='path_clearance_min':
            if not objects: raise ValueError(_missobj(row))
            a,b=row['frames']; step=int(row.get('frame_step') or 1)
            obstacle_sel=_p(row,'compare_roles'); best=None; saw_obstacle=False
            for f in range(int(a),int(b)+1,step):
                _scene.frame_set(int(f)); dg=bpy.context.evaluated_depsgraph_get()
                obstacles=[o for o in _scene.objects
                           if o.type=='MESH' and _m(o.get('bvfx_role'),obstacle_sel)]
                if obstacles: saw_obstacle=True
                points=[o.evaluated_get(dg).matrix_world.translation.copy() for o in objects]
                for obstacle in obstacles:
                    ev=obstacle.evaluated_get(dg)
                    try: inverse=ev.matrix_world.inverted()
                    except Exception: continue
                    for point in points:
                        try: hit,local,_normal,_index=ev.closest_point_on_mesh(inverse@point)
                        except Exception: continue
                        if hit:
                            distance=((ev.matrix_world@local)-point).length
                            best=distance if best is None or distance<best else best
            if best is None:
                mesh_roles=sorted({{str(o.get('bvfx_role')) for o in _scene.objects
                    if o.type=='MESH' and o.get('bvfx_role')}})[:24]
                if not saw_obstacle:
                    raise ValueError(
                        'compare_roles '+repr(obstacle_sel)+
                        ' matched no mesh obstacles; mesh roles present: '+
                        (', '.join(mesh_roles) or '(none)')+
                        ' — empty obstacle selection is not clearance')
                raise ValueError(
                    'compare_roles '+repr(obstacle_sel)+
                    ' matched mesh obstacles but closest_point_on_mesh produced no distance')
            value=best
        elif kind=='parallax_displacement_profile':
            far_group=_objects({{**row,'roles':_p(row,'compare_roles'),'control_roles':[]}})
            if not objects or not far_group:
                raise ValueError('parallax selector matched no objects on one side')
            a,b=row['frames']
            def _centroid(items,f):
                _scene.frame_set(int(f)); dg=bpy.context.evaluated_depsgraph_get()
                mvp=_checks.camera_clip_matrix(_scene,dg); xs=[]; ys=[]
                for o in items:
                    v=mvp@o.evaluated_get(dg).matrix_world.translation.to_4d()
                    if v.w>1e-9: xs.append(v.x/v.w); ys.append(v.y/v.w)
                if not xs: raise ValueError('a parallax group has no object in front of the camera')
                return sum(xs)/len(xs), sum(ys)/len(ys)
            n1=_centroid(objects,a); n2=_centroid(objects,b)
            f1=_centroid(far_group,a); f2=_centroid(far_group,b)
            near_move=((n2[0]-n1[0])**2+(n2[1]-n1[1])**2)**.5
            far_move=((f2[0]-f1[0])**2+(f2[1]-f1[1])**2)**.5
            if near_move<1e-6 and far_move<1e-6:
                raise ValueError('neither group displaces on screen between the frames')
            value=1e9 if far_move<1e-6 else near_move/far_move
    # 400, not 160: the miss diagnostics carry the selector AND the tags present,
    # and a truncated enumeration reads as a complete one.
    except Exception as exc: value=None; error=str(exc)[:400]
    _out.append({{'id':row['id'],'value':value,'objects':[o.name for o in objects],
      'roles':[str(o.get('bvfx_role','')) for o in objects],'materials':[m.name for m in materials],
      'controls':[str(o.get('bvfx_control')) for o in objects if o.get('bvfx_control')],
      'selector_roles':list(_p(row,'roles')),'selector_control_roles':list(_p(row,'control_roles')),
      'material_roles':[str(m.get('bvfx_role','')) for m in materials],
      'nodes':[n.name for g,n in matched],'error':error,'note':note,'segments':segments,
      'role_fractions':role_fractions}})
RESULT=_out
"""


def _evidence(rows: list[dict], raw: list[dict]) -> list[dict]:
    readings = {str(item.get("id")): item for item in raw if isinstance(item, dict)}
    out = []
    for row in rows:
        reading = readings.get(str(row.get("id")), {})
        error = validate_row(row) or str(reading.get("error") or "")
        value = reading.get("value")
        if (
            str(row.get("kind")) == "path_clearance_min"
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
            and float(value) >= PATH_CLEARANCE_UNMEASURED
        ):
            error = error or (
                "path_clearance_min empty obstacle selection is not clearance "
                "(the 1e9 sentinel never PASSes)"
            )
            value = None
        if isinstance(value, float):
            value = round(value, 6)
        note = str(reading.get("note") or "")
        extra: dict = {}
        if str(row.get("kind")) == "curve_derivative_max":
            parsed = _derivative_segments(reading.get("segments"))
            formatted = curve_derivative_note(parsed, hi=_optional_hi(row))
            if formatted:
                note = formatted
            span = _argmax_span(parsed)
            if span is not None:
                extra["argmax_frames"] = [span[0], span[1]]
                extra["argmax_delta"] = round(span[2], 6)
        role_fracs = reading.get("role_fractions")
        if (
            str(row.get("kind")) == "visible_fraction"
            and isinstance(role_fracs, dict)
            and role_fracs
        ):
            extra["role_fractions"] = {
                str(key): round(float(frac), 6) for key, frac in role_fracs.items()
            }
            if not note:
                note = visible_fraction_note(extra["role_fractions"])
        out.append(
            {
                "id": str(row.get("id") or "<missing>"),
                "axis": str(row.get("axis") or ""),
                "metric": str(row.get("kind") or "scene_contract"),
                "definition": KIND_DEFINITIONS.get(str(row.get("kind") or ""), ""),
                "value": value,
                "target": _target(row),
                "pass": not error and _holds(
                    row, value, role_fractions=extra.get("role_fractions")
                ),
                "note": note,
                "origin": "planner",
                "source": "interface_contract",
                "authoritative": True,
                "owner_layer": str(row.get("owner_layer") or ""),
                "fault_owner": str(row.get("fault_owner") or ""),
                "activates_at": str(row.get("activates_at") or ""),
                "lifecycle": str(row.get("lifecycle") or ""),
                "objects": list(reading.get("objects") or []),
                "roles": list(reading.get("roles") or []),
                "selector_roles": list(reading.get("selector_roles") or []),
                "controls": list(reading.get("controls") or []),
                "selector_control_roles": list(reading.get("selector_control_roles") or []),
                "materials": list(reading.get("materials") or []),
                "material_roles": list(reading.get("material_roles") or []),
                "nodes": list(reading.get("nodes") or []),
                **extra,
                **({"error": error} if error else {}),
            }
        )
    return out
