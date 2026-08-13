---
name: mix-node-sockets
tags: [shader, nodes, material, mix, blender5, api]
blender: "5.2+"
when: "mixing two colours/values in a shader graph, or a KeyError on a Mix node socket"
verified: true
---
`ShaderNodeMix` is the modern mix node. Its sockets are **`Factor`, `A`, `B`** with output
**`Result`**. There is no socket called `Mix`, `Fac`, `Color1`, or `Color2` — reaching for
any of those raises
`KeyError: bpy_prop_collection[key]: key "Mix" not found`.

GOTCHAS:
- **`data_type` defaults to `'FLOAT'`.** For colour mixing you must set
  `n.data_type = 'RGBA'` FIRST, before assigning `A`/`B`, or you are writing scalars into
  a float mix and the colours never appear.
- The node carries a full set of A/B sockets per data type, so `node.inputs` looks
  alarming: `['Factor','Factor','A','B','A','B','A','B','A','B']` (FLOAT, VECTOR, RGBA,
  ROTATION). **Name lookup is still safe** — Blender resolves `inputs['A']` to the socket
  enabled for the current `data_type` (index 2 under FLOAT, index 6 under RGBA), verified
  on 5.2.0 LTS. Do NOT hand-roll index arithmetic to "fix" this; you'll pick the wrong one.
- The legacy `ShaderNodeMixRGB` still exists in 5.2 and uses the OLD names
  (`Factor`, `Color1`, `Color2`). If you copy a snippet from an old tutorial, it will
  reference those — either keep the legacy node or rename the sockets, don't mix the two
  conventions.

```python
import bpy
nt = mat.node_tree
mix = nt.nodes.new('ShaderNodeMix')
mix.data_type = 'RGBA'                 # BEFORE touching A/B — changes which sockets are live
mix.inputs['Factor'].default_value = 0.5
mix.inputs['A'].default_value = (1.0, 0.0, 0.0, 1.0)
mix.inputs['B'].default_value = (0.0, 0.0, 1.0, 1.0)
nt.links.new(mix.outputs['Result'], bsdf.inputs['Base Color'])
```
