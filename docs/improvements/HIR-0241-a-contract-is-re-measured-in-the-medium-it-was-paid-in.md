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

## What this does not fix

**A mixed-media layer is refused, not repaired.** `hansa_silk_road` layer 2 now gets an
explicit refusal naming both media instead of a false `frame_detail` failure, which is the
correct state -- a wrong answer became a stop -- but the layer still cannot compose until
its authority changes: either the `subject_appearance` debt declares `eevee`, or the debt
and the beauty contracts are scheduled in separate groups.

The full repair is one group per medium: unit claims evaluated in the medium they were paid
in, debt claims in theirs, and composition refusing when a required claim is covered by no
group. That restructures `decision_groups`, which currently produces one group per debt plus
optionally one look-less group, and a group for the units' own medium cannot be expressed in
that shape today. It is owed and is not attempted here.

**It is also not an authoring-time check.** A debt whose declared medium cannot certify the
image contracts its layer's units bind is knowable at materialization, before any spend. The
refusal above fires at finalization, after the layer has been built. Moving it earlier is
the same predicate at a cheaper boundary and is owed separately.

Found by the hansa_silk_road driver, who also retracted their own earlier reading of the
same number. HIR-0237's claim of a genuine content gap at 1.826 is retracted in that record.
