---
name: emissive-halation
tags: [bloom, glare, compositor, emission, finish, halation, night]
blender: "5.2+"
when: "lights render as crisp pixels; the reference's lights bleed into the air around them"
verified: false
---
Reference night footage halates — bright sources spill into surrounding pixels, which is
what makes them read as *photographed* rather than *rendered*. Crisp emissive dots are the
second-biggest amateur tell after a flat city (see `night-city-field`).

EEVEE-Next has NO bloom toggle. Bloom is a compositor **Glare** node, and in 5.x every
Glare setting is an **input socket**, not an attribute. Socket names and types verified on
5.2.0 LTS:

| socket | type | value |
|---|---|---|
| `Type` | MENU | `'Bloom'` / `'Fog Glow'` / `'Streaks'` / `'Ghosts'` / `'Simple Star'` (title-case `str`) |
| `Quality` | MENU | `'High'` / `'Medium'` / `'Low'` |
| `Threshold` | VALUE | float — sources above this halate |
| `Strength` | VALUE | float |
| `Size` | VALUE | float (NOT the 4.x integer pixel size) |

GOTCHAS:
- `glare.glare_type = 'BLOOM'` raises `AttributeError` — that is the 4.x form and retrying
  it fails identically. Prefer the helper `bvfx_glare_bloom(threshold, size, strength)`.
- `scene.node_tree` is GONE; the compositor is `scene.compositing_node_group`. Read it with
  `inspect_nodes('compositor')`, don't poke it by hand.
- Because they are sockets they are **keyframable**:
  `glare.inputs['Size'].keyframe_insert('default_value', frame=f)` — the 4.x attributes
  never were. Useful for ramping halation through a beat.
- Menu sockets return `str`. Any generic loop doing arithmetic over `node.inputs` will hit
  `TypeError: type str doesn't define __round__`, and `hasattr(v,'__len__')` does NOT
  protect you (a `str` has one). Guard with `isinstance(v, str)` first.
- Set the threshold JUST UNDER your dimmest intended source. Too high and only the hero
  sign blooms while the city stays crisp — which is exactly the look you are trying to fix.

```python
bvfx_glare_bloom(threshold=0.55, size=0.60, strength=0.35)   # preferred

# equivalent by hand, if you must:
g.inputs['Type'].default_value      = 'Bloom'
g.inputs['Quality'].default_value   = 'High'
g.inputs['Threshold'].default_value = 0.55
g.inputs['Size'].default_value      = 0.60
g.inputs['Strength'].default_value  = 0.35
```

Pair with a filmic view transform (`cinematic-grade`); halation on a clipped highlight just
makes a white blob.
