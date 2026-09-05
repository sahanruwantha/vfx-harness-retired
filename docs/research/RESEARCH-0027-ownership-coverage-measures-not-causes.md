---
id: RESEARCH-0027
title: Ownership coverage asks whether an owner can measure a requirement, never whether it can cause it
status: open
date: 2026-09-05
owner: unassigned
---

# Ownership coverage asks whether an owner can measure, never whether it can cause

## Why this is a note and not yet an HIR

The failure is reproduced and general. The *mechanism* is not chosen, and the obvious
mechanisms all require deciding what a prose requirement is "about", which this repo
correctly refuses to guess at. Recording the evidence rather than landing a heuristic.

## Observed failure

room_1046_opening layer 2 stopped at `hypothesis_falsified`, finding
`hf-2471bd031b5215b66ba1`, `fault_owner_units ["camera_path"]` — a unit sealed in layer 1.
The contract `facade-grid-parallax-f39-f76` (`parallax_displacement_profile`, `op min`,
`lo 1.02`, `owner_layer 2`) could not pass because the sealed layer-1 camera is
byte-identical at both judge frames: `location[0]`/`location[1]` LINEAR between equal
values f1→f113, both rotation channels flat.

That was diagnosed as a layer-1 authoring miss. It is not. **The fresh, independently
drafted plan reproduces the same structure**, so the drafting step produces it reliably.

## The structure

In room_1046_opening's current bundle
(`985ca934b702db7bd05daa55109740daa6180d5cf59770b073ee13162eb37cfc`):

```
layer 1: reserved=['camera.*']    provides={'camera': ['camera.*']}
layer 2: reserved=['building.*']  provides={}
layer 3: reserved=['interior.*']  provides={}

R20 "1.5–4.5 seconds — Approach: the camera advances while the building grows
     from miniature to frame-filling."
    resolution.kind        = deferred_owner
    resolution.owner_layer = "2"
    evidence_domains       = ["projected_composition", "temporal"]

R32 "Use slow movement initially, then a strong ease-in/acceleration after
     approximately 4.5 seconds."
    owner_layer = "1"   evidence_domains = ["temporal"]
```

Layer 2 owns a requirement whose grammatical subject is *the camera*, and layer 2 cannot
write `camera.*`. It can **measure** the projected growth — `bbox_*` over `building.*`
across two frames is a `projected_composition` reading it is entitled to take — but the
projected growth ratio between two frames over a rigid subject is a function of camera
translation alone. Scaling or moving the building scales both endpoints and leaves the
ratio at 1.0.

Layer 1's only clause over that window, R32, has **no lower bound**, so zero camera motion
satisfies it. Every gate passes and the shot stalls at the first build that measures.

## Why the gate accepts it

HIR-0124's coverage rule is logical AND over *domains*: the owner layer must declare every
`evidence_domain` the row declares. Layer 2 declares `projected_composition` and
`temporal`, so the row closes. Nothing asks whether the owner has mutation authority over
anything that could move the measured quantity.

Domains are typed; subjects are prose. The rule checks the half it can read.

## Generality

Ten rows across two of three independently drafted current bundles, camera as the explicit
subject in four of them:

| shot | row | owner | owner reserved | statement (truncated) |
|---|---|---|---|---|
| room | R20 | 2 | `building.*` | "**the camera advances** while the building grows…" |
| room | R21 | 2 | `building.*` | "Façade acceleration: perspective and window repetition emphasize speed" |
| room | R22 | 3 | `interior.*` | "**the camera centers** one dark/open window" |
| room | R23 | 3 | `interior.*` | "the window frame **passes camera**…" |
| room | R43 | 3 | `interior.*` | "Let the window frame briefly fill the image around 8 seconds" |
| room | R52 | 2 | `building.*` | "Window repetition produces convincing scale and acceleration during the push-in" |
| hansa | R23 | 6 | `secondary.*` | "Expansion: green-lit secondary structures appear around the hero building" |
| hansa | R24 | 6 | `secondary.*` | "Final skyline: multiple towers frame the hero tower and title" |
| hansa | R50 | 2 | `hero.*` | "remains identifiable and visually dominant throughout **the camera roll**" |
| hansa | R61 | 6 | `secondary.*` | "`frame_12s.jpg` — first secondary-tower expansion" |

caesar_curia has **zero** instances, which matters: the shape is not an artifact of the
rule being vacuous, and three drafts of the same planner differ on it.

Note that several rows are legitimately satisfiable by the owner — hansa R23/R24 are about
structures *appearing*, which `secondary.*` genuinely causes. The defect is not "a
non-camera layer may not own a projected row". It is that nothing distinguishes those from
R20.

## Candidate mechanisms, none selected

1. **Refuse a `projected_composition`+`temporal` row on a layer that provides no camera.**
   Rejected as drafted: it would refuse hansa R23/R24, which the owner can cause. Turns one
   true refusal into several spurious ones.
2. **Require the camera owner to co-witness the window.** When such a row is deferred to a
   non-camera layer, the camera layer must carry a persistent obligation over the same
   frames — the pattern HIR-0184 already uses to make a camera layer author downstream
   `bbox_*` obligations it cannot otherwise be held to. Most promising: it reuses a proven
   mechanism and adds an obligation rather than a refusal. Open question: what the camera
   layer's obligation *is* when the statement does not bound camera motion numerically.
3. **Give the requirement a typed subject.** Make drafting emit which reserved namespace(s)
   a statement is about, and check owner authority against it. Strongest in principle,
   largest change, and it moves a prose-comprehension step into the planner where it can be
   wrong silently.
4. **Bound R32-shaped clauses.** "Slow movement initially" with no lower bound is a clause
   that zero satisfies. A separate, smaller defect worth its own record: a temporal clause
   whose only operator is an upper bound cannot establish that anything moved.

## Update 2026-09-05: currently covered by HIR-0184, pending keyframes

room_1046_opening's current run at `09daa94` shows the composition-coverage rule firing
hard on the camera layer, which the archived runs never did:

```
/scene_contracts: downstream subject coverage: judge f76 is shared with layer 2
(building.*) but no camera-authored persistent bbox_* row over that namespace activates
at layer 2; measure the target on refs/frame_3s.jpg and author it here.
```
(same for f113 → `frame_4p5s.jpg`, f151 → `frame_6s.jpg`)

The archived layer-1 materializations contain **zero** occurrences of "downstream subject
coverage" and authored no `cam-bbox-*-building` row. The current run has four, bound into
R33. Because the reference stills show the building growing, those targets increase across
frames, and the camera unit must prove them jointly feasible under its own path before it
freezes — which a camera held static from f1 to f113 cannot do.

So the ownership rule is still loose, but it is **not currently load-bearing**: a different
mechanism catches the same failure one layer earlier, at the layer that can actually cause
it. Do not open an ADR on this until the sealed layer-1 location curve is available.

## Update: three instances in one layer of one plan

room_1046_opening layer 2 owns **R20, R21 and R52**, verified from the selected bundle:

| id | owner | reserved | statement |
|---|---|---|---|
| R20 | 2 | `building.*` | "the camera advances while the building grows…" |
| R21 | 2 | `building.*` | "Façade acceleration: perspective and window repetition emphasize speed" |
| R52 | 2 | `building.*` | "Window repetition produces convincing scale and acceleration during the push-in" |

All three declare `[projected_composition, temporal]`; all three are about camera-induced
motion; none can be caused by a layer restricted to `building.*`. Each independently
reached the point of attempting a padding closure, which HIR-0218 then refused.

This moves the finding from "a requirement was misplaced" to **"one layer received three
camera-motion clauses it cannot cause, and the coverage rule accepted all three"**. The
count matters: a single misassignment is a planner slip, three in one layer is the rule
admitting a class. It does not change the disposition — the mechanism candidates still all
either over-refuse or require prose-subject inference — but it raises the priority.

## What would decide it

room_1046_opening's current run reaches layer 2 and either its layer-1 camera animates the
approach anyway (mechanism 2 or 4 suffices; the coverage rule is loose but not
load-bearing) or it does not (the coverage rule is load-bearing and mechanism 3 is owed).
Do not choose before that.

## Evidence

- Archived falsification: `artifacts/_archive/room_1046_opening-20260905T055632Z/`,
  finding `hf-2471bd031b5215b66ba1`.
- Current bundles read directly, 2026-09-05, for all three shots.
