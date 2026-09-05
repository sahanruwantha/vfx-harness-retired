---
id: HIR-0219
title: A metric declares the values it can produce
status: accepted
introduced_in: unreleased
date: 2026-09-05
failure_class: a_threshold_wholly_outside_a_metrics_achievable_range_cleared_every_gate
mechanism: the_canonical_kind_registry_declares_each_metrics_range_and_validation_refuses_disjoint_bounds
adr: ADR-0003
---

# A metric declares the values it can produce

## Observed failure

hansa_silk_road layer 1 falsified on two scene contracts that no scene can satisfy:

```json
{"id":"cam-roll-return-251","kind":"transform_return_delta","frames":[201,251],
 "op":"band","lo":-0.4,"hi":-0.05,"component":"rotation"}
{"id":"cam-roll-return-401","kind":"transform_return_delta","frames":[251,401],
 "op":"band","lo":-0.3,"hi":-0.02,"component":"rotation"}
```

`transform_return_delta` is computed in `evidence/scene_checks/probe.py` as

```python
if component=='location': deltas.append((second[name][0]-first[name][0]).length)
elif component=='scale':  deltas.append((second[name][2]-first[name][2]).length)
else:                     deltas.append(first[name][1].rotation_difference(second[name][1]).angle)
value=max(deltas)
```

`Vector.length` is non-negative, `Quaternion.rotation_difference(...).angle` is
non-negative, and `max` preserves it. Both bands are wholly negative. Measured values
across the builder's attempts were 0.052355, 0.245004, 0.591759, 0.364 — all positive,
as they must be.

The builder behaved correctly and expensively: six direct probes, then
`cannot_express_in_scope`, then `hypothesis_falsified hf-511203cb3d8be38729d3`. The
ADR-0010 controller then dispatched a rematerialization unassisted, and the materializer
authored satisfiable replacements — `[-0.4,-0.05]` became `[0.08,0.22]`, `[-0.3,-0.02]`
became `[0,0.15]` — re-expressing the directional intent as a magnitude with direction
implied by which frames are compared. **The loop worked end to end. It cost $3.50 of
builder budget plus a full rematerialization to learn what authoring could have refused
for $0.00.**

## Root cause

Not a missing check — a check that existed four times and could not be inherited.

`evidence/scene_checks/validate.py` already performed range-aware threshold validation,
hand-written per kind:

```
339  "the metric can only read [0,1], so this target is vacuous"   (projected kinds)
379  render_region_stat min with lo<=0 passes any frame — vacuous
381  render_region_stat max with hi>=255 passes any frame — vacuous
385  visible_fraction   min with lo<=0 passes even when fully occluded — vacuous
387  visible_fraction   max with hi>=1 passes even when fully visible — vacuous
454  path_clearance_min min with lo<=0 passes any measured distance — vacuous
467  path_clearance_min max with hi>=1e9 passes any measured distance — vacuous
```

Each branch restates, in prose, one kind's known range. The range is knowledge the
**metric** has; the validator re-derived it per kind, so a kind added later inherits
nothing. `transform_return_delta` is the fifth kind that needed a range and the first
to get none, and the same is true of `curve_derivative_max`,
`parallax_displacement_profile` and every count.

The earliest owning decision is ADR-0003's: it made metric identity canonical — one
registry, one implementation, producers and consumers calling the same code — and
`kinds.py` already keeps `KIND_DOMAINS`, `CAMERA_REQUIRED_KINDS` and
`FRAME_SCOPED_KINDS` beside it for exactly that reason. The achievable range is the same
class of fact and was left in the consumer.

A contributing cause is the registry's own wording. `KIND_DEFINITIONS` described the
metric as *"selected transform-component delta between two declared frames"*. "Delta"
reads as signed, and the planner authored a signed band in good faith, while
`radial_distance_trend` says "least-squares slope" and correctly signals that it is
signed. A non-negative magnitude described as a delta is its own trap.

## Decision

- `KIND_VALUE_RANGE` in `evidence/scene_checks/kinds.py` declares the interval each
  metric can physically produce, `None` meaning unbounded on that side. It sits beside
  the domain and camera-capability declarations, on the same reasoning.
- `unsatisfiable_bound(row)` decides **disjointness only**: a `band` whose whole
  interval misses the range, a `min` above the ceiling, a `max` below the floor, an `eq`
  outside. A bound that merely touches the edge is *vacuous*, which is a different rule
  that several kinds already state in their own words.
- It runs **last** in `validate_row`, so a kind with a bespoke branch answers first and
  keeps its exact wording. This is a backstop for kinds that declare a range and have no
  branch, not a rewrite of the four that do.
- **No default.** A metric with no entry gets no range check. `radial_distance_trend` is
  a least-squares slope and `onset_order` a difference of frame indices; both are
  legitimately signed, and a non-negative default would refuse them. A guessed default
  is the same error as the missing declaration, failing in the other direction on a
  different shot.
- `transform_return_delta`'s definition now names it a magnitude and says it is never
  negative, with direction expressed by which frames are compared.

## General mechanism

Ranges are derived from the implementation, not assumed: counts are `len(...)`,
fractions divide a subset by its population, `transform_return_delta` /
`curve_derivative_max` / `path_clearance_min` are magnitudes, and
`parallax_displacement_profile` is a ratio of two on-screen displacements. Projected
kinds are deliberately absent: their bespoke branch enforces a *tolerance* band of
`[-0.25, 1.25]` around the normalized frame rather than a strict `[0,1]`, and moving
that into a strict declaration would refuse rows it currently allows.

## Validation

`src/tests/unit/test_evidence_vocabulary.py`:

- `test_a_band_outside_a_metrics_range_is_refused_at_authoring` — both hansa rows refused
  naming `[0, inf]`; the rematerialization's real replacements still accepted; `min`
  above the ceiling and `max` below the floor refused; a band that *overlaps* the range
  still accepted.
- `test_a_signed_metric_still_accepts_a_negative_band` — `radial_distance_trend` and
  `onset_order` accept `[-30,-5]`, and an undeclared kind is untouched, proving no
  implicit default.
- `test_every_declared_range_names_a_supported_kind_and_orders_its_bounds` — the
  declaration cannot drift from the kind vocabulary.
- `test_the_magnitude_definition_does_not_invite_a_signed_bound`.
- `test_kinds_that_state_their_own_range_keep_their_wording` — the subsumption guard.
  **This one passes with and without the mechanism, deliberately**: it asserts that the
  four existing refusals are unchanged, so it guards against this change rather than
  testing it.

The other four fail without the mechanism and pass with it; run both ways.

## Rejected alternatives

- **Refuse every negative bound on every metric.** Refuses `radial_distance_trend` and
  `onset_order`, which are correctly signed. The failure mode of over-refusal is a shot
  paying to discover that a legal contract is unauthorable — the same cost as this
  defect, inverted.
- **Add a fifth bespoke branch for `transform_return_delta`.** Closes this row and
  leaves the next kind with nothing, which is the defect.
- **Fold the four vacuity branches into the range declaration.** They encode
  edge-touching and tolerance semantics that disjointness does not express, and
  rewriting four working messages closes no defect.
- **Refuse at evaluation instead of authoring.** The builder already discovers it, by
  probing, for $3.50.

## Reconsider when

A metric's range becomes conditional on its row — a socket whose range depends on the
socket type, say. The declaration is a constant per kind and would need to become a
function of the row, which is a larger change than adding an entry.
