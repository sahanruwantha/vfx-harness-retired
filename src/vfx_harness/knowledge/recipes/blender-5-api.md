---
name: blender-5-api
tags: [api, blender5, engine, compositor, action, keyframes, eevee, gotcha]
blender: "5.2"
when: "bpy AttributeError/enum errors on engine, compositor (scene.node_tree), or action.fcurves"
verified: false
---
Blender 5.x moved several APIs the model reaches for by habit. The fixes:

- ENGINE enum: EEVEE is `'BLENDER_EEVEE'` — `'BLENDER_EEVEE_NEXT'` is NOT valid in 5.2
  (that was 4.2–4.x). The harness already sets it; don't touch `sc.render.engine`.
- COMPOSITOR: `scene.node_tree` is GONE. It's a node group on `scene.compositing_node_group`
  (build: CompositorNodeTree with a Group Output socket). Use `bvfx_glare_bloom(...)` to set
  and `inspect_nodes('compositor')` to read.
- GLARE: **every** Glare setting is an input SOCKET in 5.x — there are no `glare_type` /
  `quality` / `mix` / `size` attributes. `AttributeError: 'CompositorNodeGlare' object has no
  attribute 'glare_type'` means you wrote the 4.x form; retrying it fails identically.
  ```python
  glare.glare_type = 'BLOOM'; glare.size = 8          # 4.x — AttributeError in 5.x
  glare.inputs['Type'].default_value = 'Bloom'        # 5.x — menu socket, string value
  glare.inputs['Size'].default_value = 0.55           # float sockets, 0..1-ish, NOT the
  glare.inputs['Strength'].default_value = 0.30       # 4.x integer 'size' in pixels
  glare.inputs['Threshold'].default_value = 0.6
  glare.inputs['Quality'].default_value = 'High'      # menu socket too: 'High'/'Medium'/'Low'
  ```
  Sockets are addressable by name off `glare.inputs`; if a name lookup ever misses, build the
  map once (`{s.name: s for s in glare.inputs}`) rather than guessing indices.
  Type is a menu socket taking `'Bloom'` / `'Fog Glow'` / `'Streaks'` / `'Ghosts'` /
  `'Simple Star'` — title-case strings, not the 4.x SCREAMING_CASE enum. Because they are
  sockets, they are also KEYFRAMABLE: `glare.inputs['Size'].keyframe_insert('default_value',
  frame=f)` (see `blackout-beat`), which the 4.x attributes never were.
- MENU SOCKETS RETURN `str`: `TypeError: type str doesn't define __round__ method` means a
  generic loop did arithmetic on `default_value` and hit a MENU socket (Glare's `Type` /
  `Quality`) whose value is `'Bloom'`, not a float. Bites settle-assertion samplers, node
  dumps, and `value * DECAY` sweeps. Note `hasattr(v, '__len__')` does NOT protect you — a
  `str` has `__len__`, so the "it's a vector" branch iterates the characters and rounds `'B'`.
  Guard on the numeric type, and address Glare's float sockets by name or by their known
  indices (threshold/smooth/strength/size), never by sweeping all of `glare.inputs`:
  ```python
  v = sock.default_value
  if isinstance(v, str):            # menu socket — no arithmetic, keep as-is
      pass
  elif hasattr(v, "__len__"):       # colour/vector
      v = tuple(round(x, 3) for x in v)
  else:
      v = round(v, 3)
  ```
- CHANNELBAG INDEXING: `IndexError: list index out of range` on
  `ad.action.layers[0].strips[0]` — an action only grows a layer/strip once something has
  actually been keyed on it, so the index form raises on any ID you have not keyed yet (and on
  a legacy non-slotted action). ITERATE instead of indexing; this form is safe on every ID
  that has `animation_data`, and is a no-op when there is nothing to walk:
  ```python
  def all_fcurves(ad):
      if not ad or not ad.action:
          return
      for layer in ad.action.layers:
          for strip in layer.strips:
              for cb in strip.channelbags:      # all slots, no channelbag(slot) lookup
                  yield from cb.fcurves
  ```
  Same rule for COLOR RAMP stops (below): a fresh ramp has two elements, so `elements[2]`
  raises until you `.new()` — guard with `if len(cr.elements) < 3: cr.elements.new(pos)`.
- IDENTITY vs EQUALITY on bpy structs: `bpy_struct` wrappers are re-created on every attribute
  access, so `link.from_node is node`, `ob is bpy.data.objects['x']` and
  `sock in (a.inputs[0],)` are all unreliable — the wrapper you compare is a different Python
  object each time. bpy overloads `==` to compare the underlying data; use it everywhere.
  ```python
  [l.to_node for l in nt.links if l.from_node is node]   # always [] — different wrappers
  [l.to_node for l in nt.links if l.from_node == node]   # right
  ```
- BARE `StopIteration`: `next(x for x in nt.nodes if x.type == 'EMISSION')` raises a
  *message-less* `StopIteration` when nothing matches, which tells the next reader nothing.
  Always give `next()` a default and assert with the name you were looking for:
  `n = next((x for x in nt.nodes if x.type == T), None); assert n, f'no {T} node'`.
  Same for `next(l.from_node for l in nt.links if ...)` when walking a graph upstream.
- BLOOM: EEVEE-Next has no bloom toggle — it's the compositor Glare (see above).
- SLOTTED ACTIONS (4.4+): `action.fcurves` is gone. F-curves live in per-slot channelbags.
  Use the `list_keyframes` tool (handles both), or in bpy:
  ```python
  def action_fcurves(obj, action):
      fcs = getattr(action, "fcurves", None)
      if fcs and len(fcs): return list(fcs)   # legacy
      out = []; slot = obj.animation_data.action_slot
      for layer in action.layers:
          for strip in layer.strips:
              cb = strip.channelbag(slot) if slot else None
              out += list(cb.fcurves) if cb else [f for b in strip.channelbags for f in b.fcurves]
      return out
  ```
- COLOR RAMP: the stops are NOT on the node. `AttributeError: 'ShaderNodeValToRGB' object
  has no attribute 'elements'` means you skipped the `color_ramp` struct — it lives one level
  down, and the same applies to `interpolation` / `color_mode`:
  ```python
  ramp = nt.nodes.new('ShaderNodeValToRGB')
  ramp.elements[0].position = 0.4                     # WRONG — AttributeError
  ramp.color_ramp.elements[0].position = 0.4          # right
  ramp.color_ramp.elements[1].color = (0.03, 0.11, 0.06, 1.0)   # RGBA, 4 floats
  ramp.color_ramp.elements.new(0.95)                  # add a stop (returns the new element)
  ramp.color_ramp.interpolation = 'LINEAR'
  ```
  A fresh ramp has exactly TWO stops (index 0 and 1) — `elements[2]` raises until you `.new()`.
- VOLUME emission: no `ShaderNodeVolumeEmission` — use `ShaderNodeVolumePrincipled`
  (Emission Strength/Color inputs). Use `bvfx_volume(...)` and you never touch this.
- FILE FORMAT IS STILLS-ONLY: `render.image_settings.file_format = 'FFMPEG'` raises
  `TypeError: bpy_struct: item.attr = val: enum "FFMPEG" not found in (...)`. The 5.x enum is
  image codecs ONLY — verbatim: `'AVIF'`, `'JPEG'`, `'OPEN_EXR'`, `'PNG'`, `'WEBP'`, `'BMP'`,
  `'CINEON'`, `'DPX'`, `'IRIS'`, `'JPEG2000'`, `'HDR'`, `'TARGA'`, `'TARGA_RAW'`, `'TIFF'`.
  There is no `AVI_JPEG` / `AVI_RAW` / `FFMPEG` member any more, so `scene.render.ffmpeg.*`
  is not reachable from a layer either.
  ```python
  sc.render.image_settings.file_format = 'FFMPEG'   # TypeError in 5.x — no such enum member
  sc.render.image_settings.file_format = 'PNG'      # right: render a frame SEQUENCE
  sc.render.image_settings.color_mode = 'RGB'
  sc.render.filepath = os.path.join(outdir, 'f')    # -> f0001.png, f0002.png, …
  bpy.ops.render.render(animation=True)
  ```
  **A layer should not be reaching for video at all.** Muxing to mp4 is the harness's job and
  happens OUTSIDE Blender (`vfx_harness/application/render_shot.py` renders the PNG sequence, then shells out
  to `ffmpeg -framerate <fps> -i seq -c:v libx264 -pix_fmt yuv420p -crf 18`). If you want to
  check the grade in motion, build a contact sheet from low-res PNG stills in-process instead —
  and save/restore `file_format` around it (see `warm-session-probe-loop`).
- RENDER PASSES need an INTERFACE SOCKET, not just a link. Routing an isolated pass
  (`Diffuse Direct`, `Emission`, `Shadow`, `AO`, `Normal`, `Depth`) to the output fails
  SILENTLY in the worst way: `CompositorNodeComposite` does not exist in 5.x, the
  compositor is a node GROUP, and a fresh `NodeGroupOutput` has only a virtual socket. Link
  to `inputs[0]` on a group whose interface is empty and the group outputs nothing, Blender
  falls back to the raw Combined pass, and you get a render **bit-identical to beauty** —
  no error, no warning, just a diagnostic image that is secretly the beauty frame. Declare
  the interface socket FIRST. Note the socket names are `'Diffuse Direct'` / `'Emission'`,
  NOT the `'DiffDir'` / `'Emit'` of older docs, and they only appear AFTER the pass is
  enabled on the view layer.
  ```python
  vl = sc.view_layers[0]
  vl.use_pass_diffuse_direct = True                 # the socket does not exist until this
  ng = bpy.data.node_groups.new("route", "CompositorNodeTree")
  ng.interface.new_socket(name="Image", in_out="OUTPUT", socket_type="NodeSocketColor")
  rl, go = ng.nodes.new("CompositorNodeRLayers"), ng.nodes.new("NodeGroupOutput")
  ng.links.new(rl.outputs["Diffuse Direct"], go.inputs["Image"])
  sc.compositing_node_group = ng
  sc.render.use_compositing = True
  ```
  Verified: an emissive body reads 180/255 in beauty and 0.1 in `Diffuse Direct`. If your
  "pass" render matches beauty, the route is not connected — check `ng.links` is non-empty
  and that `ng.interface.items_tree` has an OUTPUT. Use `render_pass(frame, pass=...)`
  and you never touch this.
- LIGHT GROUPS ARE CYCLES-ONLY, and `lightgroup` is on the OBJECT, not the light data
  (`AttributeError: 'AreaLight' object has no attribute 'lightgroup'` means you wrote
  `light.data.lightgroup`). On EEVEE the render layer exposes only `Image` and `Alpha` with
  a light group assigned, so there is no per-group pass to route and assigning groups does
  **nothing** to the render. To see one lamp's contribution, hide the others
  (`obj.hide_render = True`) — that EEVEE does perform. `render_pass(light='Key')` does
  exactly this and reports which lights it hid.
- MOTION BLUR: it's on `scene.render`, NOT `scene.eevee` (legacy). Set
  `scene.render.use_motion_blur = True`, `scene.render.motion_blur_shutter = <0.5-2.0>`,
  `scene.render.motion_blur_position` ('CENTER'/'START'/'END'). `scene.eevee.use_motion_blur`
  raises AttributeError in 5.x. (The harness preamble already enables render motion blur.)
