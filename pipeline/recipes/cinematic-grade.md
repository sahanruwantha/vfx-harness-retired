---
name: cinematic-grade
tags: [grade, color, tonemap, filmic, contrast, finish, compositor, bloom, halation, exposure]
blender: "5.2+"
when: "the render looks flat / washed / video-ish and the ref is punchy and cinematic"
verified: true
---
Two cheap moves get most of the cinematic finish: a filmic view transform (so highlights
roll off instead of clipping) and a compositor bloom on emissive elements.

GOTCHAS:
- EEVEE-Next has NO bloom toggle — bloom is a compositor Glare node (use `bvfx_glare_bloom`).
- In 5.x `scene.node_tree` is GONE — the compositor is `scene.compositing_node_group`. Don't
  read/poke `scene.node_tree` (AttributeError). To inspect the bloom graph, call
  `inspect_nodes('compositor')` instead of hand-writing bpy against the compositor.
- The DEFAULT view transform in recent Blender is **AgX, which desaturates strongly** and can
  kill a saturated hero colour (e.g. a vivid green tower goes grey). Switch to **Filmic** when
  the ref is punchy/saturated. (Learned the hard way on barrel_roll.)
- Filmic/AgX crush blacks nicely but can dull colour — nudge world/emission strength up a bit
  after applying it.
- The `look` enum is **UNPREFIXED** in 5.x: `'Medium High Contrast'`, NOT
  `'Filmic - Medium High Contrast'` (the prefixed form raises). Never wrap it in a bare
  `try/except` — that swallows the error and leaves `look` at `'None'`, so you ship an
  ungraded render that looks merely "a bit flat" and burn a critic round finding it.
  **Assert the value instead.**
- **AgX → Filmic lifts the mids.** Every already-tuned frame comes back too bright on the raw
  switch (12–26 mean on server_to_hansa). Do NOT re-key emitters to compensate — that unpicks
  work the critic already scored. Pull ONE global stop offset (`view_settings.exposure`, about
  −0.6 to −1.0) and the whole fingerprint set lands again.
- **Halation is halo size RELATIVE to the emissive core, not frame brightness.** Shrinking the
  Glare to fix a hot halation reading makes it WORSE (measured 9.6 → 10.4). The lever is a
  HOTTER CORE: raise the emitter's strength so the core clips harder than its own halo.
  Lower `Threshold` = more halation, so never raise it to chase brightness.
- Glare `Size` is a fraction of the frame, not pixels: 0.8 is enormous (a post-process wash
  across the floor); 0.1–0.3 is the usable cinematic range. If ONE beat blooms into a wash,
  key `Size` down over just that beat instead of lowering it globally — `Size` is a socket and
  therefore keyframable (see `blender-5-api`, `blackout-beat`), and every other beat keeps the
  global value untouched. Force `LINEAR` interpolation on those keys or bezier overshoot makes
  the grade non-idempotent across re-runs.

```python
import bpy
sc = bpy.context.scene
# 1) tonemap: soft highlight rolloff, cinematic contrast
sc.view_settings.view_transform = 'Filmic'      # or 'AgX' in newer builds
sc.view_settings.look = 'Medium High Contrast'  # UNPREFIXED in 5.x
assert sc.view_settings.view_transform == 'Filmic', sc.view_settings.view_transform
assert sc.view_settings.look == 'Medium High Contrast', sc.view_settings.look
sc.view_settings.exposure = EXPOSURE_OFFSET     # ~ -0.85 when moving AgX -> Filmic

# 2) bloom on emission (compositor Glare — see bvfx_glare_bloom)
bvfx_glare_bloom(threshold=0.6, size=0.25, strength=0.7)

# 3) localised bloom dip over one beat (leaves all other beats byte-identical)
def dip_glare_size(glare, beat, normal=0.25, tight=0.11):
    """beat = (ramp_in, hold_in, hold_out, ramp_out) frames."""
    sock = glare.inputs['Size']
    for f, v in zip(beat, (normal, tight, tight, normal)):
        sock.default_value = v
        sock.keyframe_insert('default_value', frame=f)
    # iterate layers/strips — layers[0].strips[0] IndexErrors before anything is keyed
    ad = glare.id_data.animation_data
    for layer in ad.action.layers:
        for strip in layer.strips:
            for cb in strip.channelbags:
                for fc in cb.fcurves:
                    for kp in fc.keyframe_points:
                        kp.interpolation = 'LINEAR'   # no bezier overshoot on a re-run
                    fc.update()
    sock.default_value = normal
```
