---
id: HIR-0181
title: A layer's own decision stays in its own capsule and reopened priors are built first
status: accepted
introduced_in: unreleased
date: 2026-09-03
failure_class: unchanged_layer_receipt_superseded_by_later_publication
mechanism: deferred_owner_capsule_attribution_and_driver_resume_of_reopened_priors
adr: null
---

# A layer's own decision stays in its own capsule and reopened priors are built first

## Observed failure

Run `20260903T053305Z-83f8e1` on `artifacts/room_1046_opening` (main `0c2c593`): layer 1
re-finalized and passed at 132 s (receipt `65ddae0e…`, claim revision 3). Layer 2's
materialization then published (authority head 5 → 6) and archived that receipt as
`superseded` ("authority selection changed during atomic authority-state transition"),
installing layer 1's binding with no finalization receipt although its layer row, units,
and contracts were byte-identical in both selected views. `vfx build --layer 2` refused
with "layer 1 has no current terminal finalization receipt" (exit 6, classified
`harness_defect` because the boundary returned without typed stop authority), 26 minutes
and $3.35 into the run.

Layer 1's capsule digest had changed (`9bd4a1ec…` → `b4754d10…`) because the layer-2 view
resolved requirement R51 as a pure human `decision` with no judgment-debt definition, and
`_requirement_owners` projects an ownerless decision into every layer's capsule as
"genuinely shot-wide".

## Root cause

1. Capsule attribution (domain): a decision a layer's materialization makes on a
   requirement the global bundle deferred to that layer was treated as ownerless and
   therefore shot-wide, so every earlier layer's capsule changed with it and the
   HIR-0171 preservation rule ("unchanged capsule keeps its terminal receipt") could not
   apply. The global row already names the owner (`deferred_owner.owner_layer`).
2. Driver: after a just-in-time publication the driver chained straight into the new
   layer's builder without re-deriving the accepted prefix, so a legitimately reopened
   lower layer surfaced as the builder's defensive UNACCEPTED PRIOR exit instead of being
   rebuilt first ("resume at the earliest legal unit").

## Decision criteria

- Unit and layer preservation are derived from exact capsules; a capsule must contain only
  the authority its layer is judged against.
- The driver owns the accepted prefix: a republication that reopens a lower layer is
  handled by building that layer first, not by stopping on the next builder's refusal.

## General mechanism

1. `domain/authority_capsules._requirement_owners` receives the global requirement rows: a
   `decision` with no debt definition belongs to the global row's `deferred_owner` layer
   when the bundle deferred the requirement, and only a decision on a never-deferred
   requirement remains shot-wide.
2. `application/run_shot._reopened_lower_layers` re-derives the receipt-backed prefix of
   every selected-DAG layer below the layer just materialized; each reopened layer is built
   and verified (`_build_layer_and_verify`, the same path the new layer uses) before the
   new layer's builder starts. The builder's exit 6 remains the defensive boundary.

## Rejected patch-level alternatives

- Preserving a terminal receipt across any transition whose layer row is unchanged
  (would hide genuine closure changes such as a rewritten owned contract).
- Restarting the run from the reopened layer by hand after the exit-6 stop (the defect
  this record removes).

## Validation

- `src/tests/unit/test_authority_capsules.py::test_a_layers_own_decision_never_changes_an_earlier_layers_capsule`
  and `::test_a_decision_on_a_never_deferred_requirement_stays_shot_wide`.
- `src/tests/unit/test_run_shot_stops.py::test_driver_builds_a_reopened_lower_layer_before_the_new_layer`.

## Release and rollback

Unreleased; capsule digests of layers whose requirements carry layer-owned decisions
change once, so the next transition re-derives effects from the corrected attribution.
Rolling back restores shot-wide attribution and the exit-6 stop.

## Remaining limitations

- A lower layer reopened by a later publication is rebuilt (its units are preserved, so
  only finalization re-runs); the run does not yet reason about whether the reopening was
  necessary.
