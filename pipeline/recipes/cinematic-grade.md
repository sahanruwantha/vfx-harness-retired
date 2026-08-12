---
name: cinematic-grade
tags: [grade, color, tonemap, filmic, contrast, finish, compositor, bloom]
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

```python
import bpy
sc = bpy.context.scene
# 1) tonemap: soft highlight rolloff, cinematic contrast
sc.view_settings.view_transform = 'Filmic'      # or 'AgX' in newer builds
sc.view_settings.look = 'Medium High Contrast'  # UNPREFIXED in 5.x
assert sc.view_settings.look == 'Medium High Contrast', sc.view_settings.look
# 2) bloom on emission (compositor Glare — see bvfx_glare_bloom)
bvfx_glare_bloom(threshold=0.6, size=0.8, strength=0.7)
```
