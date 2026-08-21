# When to generate, and when not to

A decision rule for where AI image generation belongs in this pipeline. Derived from six
measured outcomes on 2026-08-15, not from taste.

## The wrong question and the right one

"Is AI good at this?" has no useful answer. The two that do:

1. **Does the requirement admit many answers, or exactly one?**
2. **Can we cheaply measure whether the output meets it?**

Generation produces something *plausible*. That is exactly right when many answers satisfy
the requirement, and exactly wrong when one does.

## The evidence

| generated | requirement | outcome |
|---|---|---|
| asset isolation plate → Meshy mesh | must be *this* tower | **worked** — mesh matches plate, 1.73x base flare vs 1.85x |
| layer-1 grey-box plate | shaft 0.105W, horizon y0.76 | **failed** — -8% and +24% on shaft width |
| layer-3 city plate | dense, warm, varied, receding | **worked** — critique stayed in scope, builder converged on plate targets |
| layer-2 tower plate | must be *this* tower's facade | **failed** — 2.0 on all four frames, worse than the 3/4 without it |
| projected city, ground/low massing | a plausible city | **worked** — held through 74 units of dolly and 35 degrees of yaw |
| projected city, tall towers | *those* four towers | **failed** — flat grey blanks, no texel density at that scale |

Every success is a distribution. Every failure is a specific value or a specific object.

## The rule

```
                    verifiable cheaply          not verifiable
distribution        GENERATE FREELY             generate + human review
                    city plates, projection      concept art, mood

specific value      GENERATE + GATE             DO NOT GENERATE
or object           asset -> fidelity check      layer-1 framing
```

> Generate where many answers are acceptable. Where exactly one is, generate only if you
> can measure the difference — and gate on that measurement. Where you cannot measure it,
> do not generate.

### Amendment 1 — weight by blast radius

A wrong city plate costs one layer. A wrong camera costs every layer stacked above it.
The gate must be strictest where the error propagates furthest, which is exactly layer 1 —
the place generation is most tempting and least trustworthy.

### Amendment 2 — verify the right quantity

Having a metric is not enough; it must be a metric OF THE THING. Layer 2's plates failed
not because the images were bad but because the fingerprint was derived from a frame BAND
while the layer owned a narrow SUBJECT. The band at that frame was almost entirely city —
content the layer does not own and had been deliberately suppressed in the plate — so the
target told the builder to make the bottom of frame nearly black. See #37.

## Applying it here

| use | verdict | gate |
|---|---|---|
| asset isolation plate (image -> 3D) | **generate** | `compare_to_plate` silhouette check at normalise time |
| far / ground city as a projected matte | **generate** | band statistics; subject and band coincide |
| sky canopy plate (layer 4) | **generate** | band statistics; sky fills the top band |
| hero + neighbour tower facades | **do not** | no reliable generated form; real geometry and shading |
| camera framing / any geometric fingerprint | **do not** | no instrument exists to measure the drift |

## The standing gap

The pipeline gets this right in exactly one place — asset isolation generates, then
`compare_to_plate` gates it — and nowhere else. Generalising that is the work:

**every generated artifact needs a named check and a named owner of the failure case.**

Without the check, a generated artifact is a confident fiction, and this pipeline's whole
premise is that claims are measured rather than asserted.
