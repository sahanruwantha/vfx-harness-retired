---
id: HIR-0152
title: Irreversible partial-union forecasts block producer freeze
status: accepted
introduced_in: unreleased
date: 2026-08-30
failure_class: diagnostic_forecast_allows_mathematically_doomed_checkpoint
mechanism: monotonic_partial_union_freeze_gate
adr: ADR-0006
---

# Irreversible partial-union forecasts block producer freeze

## Observed failure

After HIR-0151, bounded Layer 2 build `20260830T112336Z-57f889` showed the mass
producer all three camera-owned bbox forecasts after every mutation. The terminal mass
candidate read `building_miniature_start bbox_height=0.4019` against `<= 0.15` while
its seven local f39 rows passed. The tool correctly called the forecast diagnostic, so
the executable-only unit was still free to freeze. Adding the later roof subject cannot
make a projected union height smaller. Waiting for the roof payer would therefore repeat
the already-proven unrepairable downstream failure.

The run was interrupted before checkpoint freeze. Layer 3 was never opened.

## Root cause

HIR-0151 separated observation from payment, but treated every partial-union miss as
equally provisional. Projected union metrics carry directional facts independent of the
unknown successor geometry. Width, height, and bottom cannot decrease as more selected
geometry is added; top cannot increase. A miss on the irreversible side is not a model
forecast or a premature full-subject payment. It is executable proof that the current
producer has already made the final contract impossible.

## Decision

- Deferred forecasts remain nonpayable by default.
- For projected union `bbox_width`, `bbox_height`, and `bbox_bottom_y`, a partial value
  already above an upper bound is a freeze blocker because successor geometry can only
  preserve or increase it.
- For projected union `bbox_top_y`, a partial value already below a lower bound is a
  freeze blocker because successor geometry can only preserve or decrease it.
- `band` and `eq` use their upper/lower tolerance edges. A miss on the repairable side
  remains diagnostic. Bbox centres are not monotonic and never use this gate.
- A blocker is required executable evidence for the current producer only at candidate
  evaluation and empty-scene replay. It does not transfer contract ownership, claim
  closure, or payment authority.
- The rejection names the monotonic direction and legal outcomes: repair the current
  producer or call `cannot_express_in_scope`. The complete-subject payer still evaluates
  the final union under HIR-0134.

## General mechanism

`irreversible_deferred_subject_forecast_failures` joins compiled forecast rows to fresh
evidence and classifies only mathematically irreversible sides. The builder schedules
the producer forecasts at their declared owner frames during live/canonical evidence,
adds only those blocker ids to the unit's extra required set, and leaves every other
forecast on HIR-0151's diagnostic channel.

## Rejected alternatives

- Require every partial producer to pass every deferred bbox: later subject pieces may
  legitimately repair a lower extent miss.
- Trust the model to notice the word "diagnostic": the observed agent acknowledged the
  readings and still prepared to freeze because its formal rows passed.
- Let the roof shrink or move mass: that violates write-cluster and predecessor-interface
  authority.
- Parse camera or subject coordinates from scripts: the union monotonicity is a general
  metric invariant and needs no shot geometry heuristic.

## Validation

The evidence-vocabulary fixture proves an over-height partial union blocks an upper
height bound, an under-height minimum remains diagnostic, a too-high top-coordinate
minimum blocks, and a centre miss remains diagnostic. The builder fixture proves the
early producer receives the blocker at the contract's declared frame while the
dependency-complete payer does not receive a forecast blocker.

Validation on 2026-08-30: the focused evidence-vocabulary and builder-instrument
suites passed 46 tests in 2.89 seconds. `.venv/bin/ruff check src tests` passed, the
full repository suite passed 661 tests in 48.69 seconds, and `.venv/bin/vfx --help`
exited successfully.

## Release and rollback

No persisted schema change. Rollback permits a checkpoint that deterministic union
monotonicity already proves no successor can repair and is unsafe.
