---
id: HIR-0234
title: Shading is a light modifier, not a light source
status: accepted
introduced_in: unreleased
date: 2026-09-06
failure_class: a_shading_over_mesh_prefix_with_no_light_passed_both_bootstrap_gates_and_owed_unpayable_debt
mechanism: signal_sources_are_separated_from_modifiers_and_an_emissive_unit_declares_illumination
adr: null
---

# Shading is a light modifier, not a light source

## Observed failure

`hansa_silk_road` layer 2 owed four required image debts. **None could be paid**, and
`propose_checks` refused every one as NOT NECESSARY: the pre-unit adversary already read
`region_mean ~ 0.0835` and `frame_mean ~ 0.083`. The builder measured why with
`inspect_scene`:

```
world = None
light objects = 0        anywhere in the cumulative scene through layer 2
```

**Both bootstrap gates passed.** `image-signal-bootstrap` (HIR-0110) was satisfied because
`hero_facade` derives a `shading` write family. `image-subject-bootstrap` (HIR-0160) was
satisfied because `hero_mass_geometry` supplies the `mesh` carrier. So the gates told the
materializer the layer may owe image debt, and it authored unpayable contracts there
**twice, on two independent designs**.

The debt was unpayable in *both* directions, which is the signature: a darkness bound is
trivially met by a black adversary and refused as NOT NECESSARY, and a brightness bound
has nothing to illuminate the surface. There is no threshold in between.

The builder named no upstream fault owner, correctly. Camera and geometry are both right
at that DAG stage. It is a sequencing property of the plan.

## Root cause

`IMAGE_SIGNAL_FAMILIES` was `{light, shading, volume, compositor}` -- every family that
can affect pixels. But affecting pixels and *emitting* are different properties.
**Shading decides how a surface responds to light.** A material on an unlit surface
renders black however carefully it is authored.

Shading is a source in exactly one case: when it is emissive. And that is invisible where
the gate runs. `bvfx_emission`, `bvfx_emissive_windows`, `bvfx_emissive_from_texture` and
`bvfx_scatter_emissive` all resolve to the `shading` family, and **the script that would
call them does not exist when the unit is staged.** No authored field distinguishes an
emissive facade from a diffuse one.

So the gate could not have known -- and the fix is not a cleverer derivation but a typed
declaration, exactly as for camera (HIR-0098) and geometry.

## Decision

Sources are separated from modifiers, and an emissive unit says so:

```
IMAGE_SIGNAL_SOURCE_FAMILIES    = {light, volume, compositor}
IMAGE_SIGNAL_MODIFIER_FAMILIES  = {shading}
IMAGE_SIGNAL_FAMILIES           = sources | modifiers      # value unchanged
UNIT_PROVIDES                   = {camera, geometry, illumination}
```

A unit provides optical signal if its write cluster derives a **source** family, or if it
declares `provides: ["illumination"]`. That declaration is additive capability in exactly
the way `provides: ["geometry"]` already adds a `mesh` carrier in
`judgment_authority.py:96`; a role name or a look label never implies it.

`IMAGE_SIGNAL_FAMILIES` keeps its value because witness guidance is a different question:
all four families can alter a plate, and that list is what a materializer is shown when
asked which write kinds affect pixels. Only the *bootstrap* predicate narrows.

The refusal names both routes rather than only refusing:

> a unit that owes required image_contract debt needs a light SOURCE in its replay
> prefix: its own light, volume or compositor write family, a same-layer dependency that
> derives one, an earlier materialized layer that does, or a unit declaring
> `provides: ["illumination"]` because it is itself emissive. A shading cluster alone is
> not a source -- shading decides how a surface responds to light, and whether it emits is
> not visible when the unit is staged -- so a shading-over-mesh prefix with no light and no
> world renders black and its image debt is unpayable in both directions.

## Validation

`src/tests/unit/test_illumination_capability.py`: shading is a modifier and not a source;
the pixel-affecting set is unchanged **in value**, so witness guidance still names all
four; `illumination` sits beside `camera` and `geometry`; and the rule names the
declaration, says why shading is not enough, and still lists the other legal routes.

**One existing test asserted the defect and is superseded here.**
`test_image_debt_may_be_paid_by_own_shading_family_or_earlier_layer` read
`image_signal_provider_ids((surface,), (material_row,)) == {"surface"}` -- a material
assignment on a product shell, no light anywhere, treated as sufficient signal. That is
hansa's shape exactly. Its replacement asserts more than it did: shading alone yields a
gap naming the unit, and the same unit declaring `illumination` yields none. Reverting
`src/vfx_harness` fails it on behaviour:

```
E   AssertionError: assert frozenset({'surface'}) == frozenset()
```

## Two fixtures were relying on the defect

The full suite surfaced two, and both are informative rather than incidental:

- `test_materialization_requirement_binding_accepts_required_image_debt` builds a unit
  that assigns a material and owes beauty debt over a mesh carrier **with no light
  anywhere** -- `hansa_silk_road` layer 2's exact shape. Its stated subject is HIR-0122
  (an empty `image_contracts` list is intentional), so the signal gate was incidental to
  it and it passed on the defect. It now declares `provides: ["illumination"]`, which is
  what a material unit that is itself the light should say.
- `test_camera_global_layer_refuses_geometry_proxy_before_candidate_write` asserted the
  non-camera allowed set exhaustively as `{"geometry"}`. Its subject is HIR-0128 -- that a
  camera layer cannot manufacture subject form -- and that assertion is one line below.

Neither was pinning the rule this change alters. Both are updated with the reason at the
line.

## What this does not fix

It establishes that *something* in the prefix can emit. It does not establish that the
emission is enough, aimed at the subject, or at a level any particular threshold can use
-- that is a measurement, and it belongs to the builder's adversary comparison.

It also does not help a plan whose DAG has no illuminating layer at all. hansa's four
layers declare `scene`, `image`, `human`, `temporal` and `projected_composition` domains
and **no layer declares an illumination capability**, because until this change none
could. Whether a *sparse* layer should declare it -- so the plan gate can refuse an
unlightable DAG before any materialization spends -- is the capability ADR, which now has
three findings behind it.

## Correction: the black-prefix sentence was too strong (2026-09-06)

This record justified the modifier/source split partly with "a shading-over-mesh prefix
with no light and no world renders black". That is true only of a *non-emissive* shading
prefix, and the shot this record was written about contains the counterexample.

`artifacts/hansa_silk_road/build/units/02/hero_facade.py`, the accepted script:

```python
emit = nt.nodes.new("ShaderNodeEmission")
emit.inputs["Strength"].default_value = 3.5
nt.links.new(emit.outputs["Emission"], mix.inputs[2])
```

and in run `20260906T023403Z-ed877b`, `reports/layers/layer-2.hero_facade.json`:

```
id: hero-facade-appearance-debt   metric: frame_detail
frame 1   value 5.135   target >= 2   pass
frame 51  value 5.266   target >= 2   pass
frame 151 value 5.14    target >= 2   pass
```

An emission shader needs no light and no world. So a shading cluster can be the only
optical signal in a prefix, and this record's own example shot proves it.

**The mechanism is unchanged and the correction strengthens it.** The argument was never
"shading cannot light"; it is that `bvfx_emission` and its siblings resolve to `shading`
and *the script does not exist when the unit is staged*, so the gate cannot tell an
emissive prefix from a dark one. Both prefixes exist in this one shot. That is precisely
why the answer is a typed declaration (`provides: ["illumination"]`) rather than a
classification the gate infers -- the same shape as HIR-0098 for camera.

The unpayable-debt evidence in the sections above came from an earlier attempt of that
layer whose design bound `region_mean` / `frame_mean` debts, not the `frame_detail` design
above. Both attempts are real; the record previously read as though one prefix were the
whole story.

Found while reviewing the caesar_curia driver's `RESEARCH-0029`, which retracts a related
claim of its own ("emission is not a light source") on the same measurement. Recorded here
rather than left in that note, because the sentence being corrected is in this record and
in AGENTS.md.
