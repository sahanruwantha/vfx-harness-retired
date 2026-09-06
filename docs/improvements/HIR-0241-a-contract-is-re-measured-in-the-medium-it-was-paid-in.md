---
id: HIR-0241
title: A contract is re-measured in the medium it was paid in
status: accepted
introduced_in: unreleased
date: 2026-09-06
failure_class: a_composed_group_re_measured_every_unit_contract_in_one_debts_declared_medium
mechanism: a_group_evaluates_only_the_claims_its_render_medium_can_certify
adr: null
---

# A contract is re-measured in the medium it was paid in

## Observed failure

`hansa_silk_road` layer 2, run `20260906T023403Z-ed877b`. One contract, one frame, two
answers:

```
2@hero_facade_canonical_f51.png               unit canonical    frame_detail 5.266  >= 2  PASS
2@f51_finalization_group_0_canonical_f51.png  composed group 0  frame_detail 1.826  >= 2  FAIL
```

Both plates were opened. The first is EEVEE: black ground, the emissive window grid
glowing, peak luminance 229. The second is Workbench solid: white ground, flat grey massing
with floor lines, peak 164. **The emissive pattern `frame_detail` measures does not exist in
the second image.** Not dimmer -- absent, because Workbench solid suppresses materials.

The composed failure is what the layer stopped on, and it is false. There is no content gap.

## Why it was allowed

The chain, every step read from the tree:

1. Layer 2 carries a judgment debt `jd-95b82d78…` for requirement `R15`, owner layer 2,
   property `subject_appearance`, declaring `observation_medium: workbench_solid`.
2. `_composition_judge_unit` sets the composed unit's `judgment_observation_medium` from
   that debt's decision.
3. `agents/builder/evidence.py:523 _unit_raster_mode` returns `"solid"` for
   `workbench_solid`, and `layer_composition_finalization.py:426` passes that as the group's
   `render_mode`.
4. The claims that group evaluates are, in `_composition_judge_unit`:

   ```python
   claims = (*unit_claims, *qualitative)
   ```

   **every unit's bound image contracts included.**

So one debt's declared medium decided the medium for every contract in the group, and a
threshold calibrated against an EEVEE payment was tested against a Workbench solid
measurement.

`_composition_judge_unit` already refuses to mix media:

```python
if len(media) > 1:
    raise ValueError("one composed judgment unit cannot mix observation media; ...")
```

The invariant is correct and the set is incomplete: a unit image claim's medium requirement
is implicit -- it is whatever `_unit_raster_mode` gave that unit when it paid -- and never
enters `media`. So two media were mixed and the guard could not see it.

AGENTS.md has stated the rule since HIR-0212's neighbourhood: *every evidence a layer binds
must be visible in the medium that layer is judged in*. No boundary called it. That is the
same shape as HIR-0238 -- a rule written and never wired -- and it is the third instance in
one day.

## The mechanism this does not use

A per-metric medium declaration in the registry, in the style of `KIND_VALUE_RANGE`
(HIR-0219), was considered and rejected. `frame_detail` is not beauty-only: it measures
something in solid, and that something is 1.826. A metric that returns a plausible number in
the wrong medium cannot be classified by metric identity. The property that is actually
wrong is the *comparison* -- a threshold calibrated in one medium against a measurement in
another -- and that is true whatever the metric.

## Mechanism

A contract's observation medium is part of its identity, so a claim is re-measured in the
medium it was paid in or not at all:

- each claim entering a composed group carries its required medium -- for a unit claim, the
  `_unit_raster_mode` of the unit that owns it; for a qualitative debt claim, the debt's
  declared `observation_medium`;
- a group evaluates only claims whose medium matches its own render mode;
- composition refuses when a required claim is covered by no group, so a medium mismatch
  produces an additional group rather than a silently skipped contract.

## Mechanism, as landed

A unit with a required claim bound to an `image_contract` contributes its own medium --
`unit_observation_medium(unit)` -- to the set `_composition_judge_unit` already checks. The
existing invariant then sees the mix and refuses, naming both sides:

```
one composed judgment unit cannot mix observation media (eevee, workbench_solid);
debt jd-95b82d78 declares workbench_solid; unit hero_facade pays hero_facade-claim in
eevee. Schedule each typed debt independently, or give the debt the medium its layer's
image contracts were paid in -- a contract is re-measured in the medium it was paid in
or not at all
```

A scene-bound claim is computed from the scene rather than the plate, so it does not
constrain the medium: a look-less geometry unit beside a `workbench_solid` debt -- the
normal arrangement -- still composes. Three tests assert that, so the guard cannot widen
into refusing every layer.

The change also collapses three derivations of the medium-to-render-mode mapping into
`RENDER_MODE_BY_MEDIUM` in `domain/judgment_debt_models.py`:
`JudgmentObservationRequest`'s `expected_mode`, the builder's `_unit_raster_mode`, and the
composed group's `render_mode` now read one table, with a test asserting the builder and
the domain agree on every unit shape.

## Validation

`src/tests/unit/test_composed_group_medium.py`. The behavioural discriminator imports the
domain module rather than any new name, so on the pre-fix tree it fails on the refusal not
firing:

```
E   Failed: DID NOT RAISE ValueError
```

Three of the six pass on both trees by design -- they are the over-refusal guards.

## The repair: one group per medium

The refusal above was correct and insufficient. It stopped the false failure and left the
layer unable to compose at all, and hansa's authority was not wrong: R15's statement is
*"hero windows should come from a repeatable façade module or shader mask rather than
individually modeled rooms"*, a construction-method proposition whose `carrier_families`
is `['mesh']`. **`workbench_solid` is exactly the medium that discriminates it** -- windows
visible in solid mean modeled rooms, windows absent mean a shader mask. Moving that debt to
`eevee` would make it unfalsifiable while looking like a repair, since in beauty both render
as windows. The layer genuinely carries two media and owes two plates.

`composed_group_plans(layer, decisions)` returns one group per debt plus one per unit medium
no debt group covers, and refuses when an image contract is covered by no group at all. A
group compiled for an explicit medium:

- keeps only the image-bound claims whose unit medium matches its plate, so a contract is
  re-measured where it was paid;
- carries no qualitative claim and declares `look_capabilities: ()`, so it takes no look
  vote -- it exists to measure executable contracts, not to judge appearance;
- still rasters, because `_unit_requires_raster` returns true for a required claim bound to
  an `image_contract` rather than a `scene_contract`.

Medium-free claims -- scene contracts, which are computed from the scene rather than the
plate -- stay in every group exactly as before, so the split changes nothing for a layer
that carries one medium.

The mixed-media raise stays for two typed debts in one group, which `decision_groups` has
never produced. It is no longer the mechanism: filtering is.

## Validation

`src/tests/unit/test_composed_group_medium.py`, nine tests. The one that carries the
property is `test_every_image_contract_lands_in_exactly_one_group`: it walks every plan,
compiles each group, and asserts each unit contract appears exactly once across all of them
-- not that one group has it, and not that another group lacks it. A split that dropped a
contract or double-counted it fails there.

Three tests remain over-refusal guards, passing on both trees by design.

**One test was changed rather than added, and that is a ratchet decision.**
`test_a_solid_debt_cannot_re_measure_an_eevee_image_contract` asserted the refusal; it now
asserts the placement, under the name `..._does_not_re_measure_...`. The refusal it pinned
was this record's own interim mechanism, superseded here by the repair it named as owed. The
behaviour it protected -- an EEVEE contract is not measured on a solid plate -- is asserted
more strongly than before, since the test now also says where the contract went.

## What this does not fix

**It is not an authoring-time check.** Which media a layer's plates must cover is knowable
at materialization, before any spend; the split is computed at finalization, after the layer
has been built. Nothing is measured in the wrong medium either way, so this is a cost
question rather than a correctness one -- but a layer that will owe two plates could say so
before it is paid for. Owed separately.

**It does not decide `JUDGMENT_DEBT_PROPERTIES`.** R15 is labelled `subject_appearance`
because that vocabulary has three members -- `camera_framing`, `reference_identity`,
`subject_appearance` -- and none expresses a construction-method proposition. The label is a
forced choice rather than a mistake, and refusing the `subject_appearance` +
`workbench_solid` pair would leave no legal way to author R15 at all. Extending the
vocabulary is a schema decision, as that module's own comment says.

Found by the hansa_silk_road driver, who also retracted their own earlier reading of the
same number. HIR-0237's claim of a genuine content gap at 1.826 is retracted in that record.
