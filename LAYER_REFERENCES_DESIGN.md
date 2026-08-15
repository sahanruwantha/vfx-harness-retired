# Per-layer references and per-layer fingerprints

**Status:** design, not built. Evidence below is from real runs and four image-generation
probes on 2026-08-15.

## The problem

Every layer is judged against a **finished-shot reference**. Layer 2 builds towers and is
scored against `f440_final.jpg`, which contains the city, the sky, the green energy and
the grade — everything layers 3–8 add. The layer is asked "does this partial build look
like the finished shot?", and the honest answer is always no.

Three mechanisms exist only to work around that: `owns` (scope the critic by axis), the
`"n/a"` rule (absent-by-design must not score 0), and the scope block (prose telling the
critic what later layers deliver). All three are patches on a reference that is wrong for
the question being asked.

What it cost today, measured:

- Layer 2 was marked down at f440 for *"featureless flat-grey boxes"* — the background
  city, which is **layer 3's** work and had not been built.
- Layer 2's f45 was told, three canonical attempts running, to add fins, a stepped podium
  and crown mass. It does not model geometry; it shades. The podium it was asked for is
  already in the mesh (verified: 1.73x base flare vs the design plate's 1.85x) and is
  invisible at f45 because the plan puts its base at y0.95 — the bottom edge of frame.
- Three attempts at layer 2 cost roughly $37 and never cleared all four frames.

And a subtler one, found while probing this design:

- Layer 3's done-check reads *"at f100 the bottom band reads μ~74/σ~53"*. That number was
  measured from the **final graded frame** and handed to a layer that runs five stages
  before the grade. The plan is asking a pre-grade layer to hit a post-grade target.

## What the probes established

Four generations against `gpt-image-2` via the existing `codex_images.edit_image` path —
the same machinery that already produces asset isolation plates.

**Editing a real reference into a layer-appropriate one works.** The layer-3 plate
(city built, but no green, no storm sky, no grade) is a clean, photographic render of
exactly what `city_field_depth` describes: dense warm sprawl, varied window density,
thinning into haze with a light band at the skyline.

**Prompting is the hard part, and the failure modes are specific.** The first f440
attempt deleted the entire city. The cause was mine: at f440 the city exists *only* as a
field of lights, and I had written "no city lights". Two further faults — the model
"completed" frame-cropped towers into whole objects standing on a ground plane, and it
normalised their depth spacing. All three were fixed by naming them:

| fault | fix |
|---|---|
| city deleted | "where you see lights, place buildings; the city must not become empty ground" |
| cropped towers completed | "crops must stay cropped; do not stand them on a visible ground plane" |
| depth normalised | "near towers stay large, far ones stay small; do not space them evenly" |

**Passing the whole reference board helps.** With four other frames of the same world as
context, tower designs stayed faithful (the left tower's X-brace survived) where a
single-reference edit had reinvented them.

**Generated plates drift on framing, and we cannot currently detect it.** The layer-1
grey-box plates measured -8% and +24% on hero shaft width against the plan's fingerprints.
Worse, `measure_ref` returns exposure and structure only — there is **no instrument** that
measures "hero tower width as a fraction of frame". The plan's geometric fingerprints were
derived by the plan agent looking at images. I tried three times to improvise a detector
and produced three different answers for the same image; the attempt is not cheap and
should not be faked.

## The design

### 1. Generate a plate per layer per judge frame

At plan time, for each layer L and each frame it is judged at, edit the real reference
into "the shot as it looks after L, before L+1..N". Keep: camera, everything layers 1..L
built. Remove: everything layers L+1..N add.

Inputs: the target frame first, then up to four other reference frames as world context
(the `edit_image` cap is 5).

### 2. Derive that layer's fingerprints FROM the plate

This is the part that is easy to miss and not optional. The layer-3 plate measures
**μ35/σ37** in the bottom band where the real frame measures **μ74/σ53** — a real,
*correct* difference, because the plate is ungraded and the final frame is not. But the
plan's done-check for layer 3 currently says μ74.

If we adopt plates without re-deriving fingerprints, the picture says one thing and the
number says another, and the builder is handed a contradiction — strictly worse than
today, where image and number at least agree with each other.

So: generate the plate, run `_region_metrics` on it, and write **those** numbers as the
layer's targets. One source of truth, image and number agreeing by construction.

### 3. Gate by axis type, not by layer

| axis kind | reference | why |
|---|---|---|
| geometric / framing (`camera_framing`) | **real plate + plan fingerprints only** | generation drifts on framing and we cannot measure the drift |
| look / content (`hero_tower_read`, `city_field_depth`, `sky_and_palette`) | **generated per-layer plate** | "does this read correctly" survives a few percent of framing drift |

Layer 1 is explicitly excluded. Its only axis is framing, it already passes first time
under the current design, and a drifted plate there would teach a wrong camera to every
layer above it.

### 4. Keep acceptance as the composition check

Per-layer plates verify each layer against its own target. Nothing in that verifies the
layers still **compose** into the real final frame. Acceptance already judges the finished
chain against the real references and must keep doing so — it is the only defence against
a series of individually-correct layers drifting collectively away from the shot.

## Cost

Two plates per layer for an 8-layer shot is ~16 image edits at plan time, once. Against
layer builds that have cost $6.87–$23.20 each, this is not the expensive part. The
expensive part today is re-running layers because their reference was wrong.

## What we do not know

- **Whether plate quality holds across all eight layers.** Two worked well (layer-3 f100,
  layer-1 f100 grey-box), one failed badly and was recovered by prompt fixes (layer-1
  f440), one is unverified (layer-3 f440). That is not enough to predict layer 5's green
  charge or layer 6's blackout, both of which are far stranger edits.
- **Whether a layer that matches its plate composes correctly.** Untested; this is the
  drift risk, and only a full run answers it.
- **How to verify framing fidelity.** No instrument exists. Worth building regardless —
  it would also let us check the *builder's* framing directly instead of via a critic's eye.

## Pilot result (layer 3, run 2026-08-15)

**The thesis held.** Every critique stayed inside `city_field_depth` — six-for-six
in-scope issues, three of them citing the plate directly, and not one demand for sky,
green, grade or geometry the layer does not build. Compare layer 2 under finished-frame
references, which spent three attempts being told to add a stepped podium that was
already in the mesh.

**The derived fingerprints landed.** The builder converged on **mu35-39**, the plate's
measured target, rather than the plan's inherited **mu74** which only exists after layer 8
grades the shot. The critic then cited the derived number by name: *"verify bottom-band
statistics against the plate target (f440 mu~34/sigma~39)"*.

**The critique is better feedback.** "Add warm sodium street-lamp chains along a grid of
avenues converging toward camera" is buildable. Under the old reference that note competed
for space with complaints about a missing storm sky.

The layer still FAILED (both frames 2.0, $20.76) — but on its own axis, for real reasons,
after a repair round regressed it. That failure is about the repair loop, not the
references, and is fixed separately.

## Rollout

1. ~~Pilot on layer 3~~ — **done, thesis held.**
2. ~~Extend to layers 2 and 4~~ — **done.** Layer 2 has plates for all four judge frames,
   layer 4 for both of its. Fingerprints derived from each plate and written into
   `layers.json`. The layer-2 plates matter most: they show the two polarities that were
   built inverted three times (outer strips vs recessed core, dark sign panel vs glowing
   letters), so the target now *shows* the answer instead of describing it.
3. Build the framing measurer as separate work; only then consider layer 1.
4. Never generate for an axis whose fingerprint is geometric until step 3 lands.

## Caveat on the extension

The layer-2 and layer-4 plates were generated in a batch and spot-checked, not
individually verified frame by frame. The runtime safety net is `reference_usable` — the
critic must declare a reference unfit, and a false declaration fails the verdict rather
than silently grading against a bad plate. If one of these plates is wrong, that is where
it should surface.
