---
id: HIR-0069
title: Black-frame probes must retire the diagnosed branch
status: proposed
introduced_in: unreleased
date: 2026-08-28
failure_class: repeated_mandatory_probe_loop
mechanism: measurement_keyed_guard_retirement
adr: null
---

# Black-frame probes must retire the diagnosed branch

## Observed failure

In run `20260827T213426Z-dd2f26`, logarithmic density sweeps at frame 150 repeatedly showed
candidate means below `0.32/255` against an `81.27/255` reference. After every permitted light
edit, the next black render nevertheless recreated the same mandatory density sweep. The run
entered `black render → density sweep → light edit → black render` without new information.

Run `20260827T232542Z-3c222e` proved that an advisory branch closure was still insufficient.
After both frame-specific density sweeps closed, the builder tried at least six distinct light
placements, spanning camera distances from roughly `452` to `8`, while frame 72 remained
`0/255`, 100% black. It then repeated a subset of the density sweep and continued coordinate
guessing until the operator interrupted the run after 15 minutes.

Run `20260828T001601Z-9ca8f6` then repeated the exact frame-150 density range after its
stored diagnosis already said `DENSITY HYPOTHESIS CLOSED`. Retiring the mandatory probe card
did not prevent the model from voluntarily purchasing the same experiment again.

## Root cause

The guard stored one arrival-ordered `black_frame_required_probe` card. A completed measurement
was recorded separately, but the guard path did not reconcile stale cards against that registry.
The numeric sweep also returned a lowest-MAE row without interpreting that every candidate was
still optically black, leaving the model to rediscover the next causal branch.

## General mechanism

Guard authority now resolves through the role+frame measurement registry. If the exact current
density was measured, any stale requirement is retired before mutation is considered. Authored
double-precision values and Blender float32 socket read-back use one `1e-6` relative / `1e-9`
absolute equality, so `0.05` and `0.050000000745…` are the same measurement rather than a new
candidate. The next
cause card removes its obsolete `NEXT MEASUREMENT` line and attaches the stored sweep diagnosis.
When all tested densities remain below ten percent of a non-dark reference, the diagnosis closes
the density hypothesis, forbids repeating the sweep, preserves the required energy schedule, and
routes to local-light contribution, coverage, or placement.
`probe_control` now refuses a World-density request when the role+frame diagnosis is closed
and every requested sample is already present in that measurement registry. A request that
adds a genuinely new sample remains measurable; an exact replay names the next legal actions.

Placement search is now bounded by measured scale coverage. Once the density branch is closed,
scene contracts pass, and three distinct local-light placements spanning at least a 4x
camera-distance range all retain a near-black beauty (`mean <= 3/255`, `black >= 85%`), the live
session records `LIGHT-PLACEMENT SEARCH CLOSED`. Further render, probe, or scene mutation calls
on that causal branch fail closed and name the exact `cannot_express_in_scope` invocation with
the frame's unpaid image-contract ids. This is an experiment-budget boundary, not a claim that
continuous 3D space was exhaustively searched: the legal alternatives are a different declared
control or typed abstention, not another unsupported coordinate.

## Rejected patch-level alternatives

Raising the black threshold only postpones the loop. Allowing every mutation after one sweep
would also permit unmeasured density commits; the independent measured-value guard remains.
Prompting the model to remember a prior table would make concurrency order and context retention
execution authority.
Likewise, merely printing "do not repeat" did not retire the broader placement branch; the
producing run ignored it repeatedly. A fixed turn or wall-clock timeout would not know whether
the work was converging and would discard useful searches alongside churn.

## Validation

`test_completed_density_measurement_retires_stale_black_frame_guard` pins registry-first guard
resolution including Blender's float32 read-back. `test_unmeasured_density_keeps_black_frame_guard_closed` preserves fail-closed
behavior. `test_density_probe_closes_a_non_explanatory_black_frame_branch` pins the causal
readback on the measurements observed in the producing run.
`test_repeated_black_placements_close_only_after_distinct_scale_coverage` pins the bounded
non-progress condition, while `test_black_placement_search_stays_open_when_signal_improves`
proves that an optically useful trial keeps the branch open.
`test_closed_density_branch_rejects_only_already_measured_repeat` pins exact-repeat refusal
without blocking a range that contributes a new value.

## Release and rollback

No persisted schema migration. Probe state remains scoped to one live unit session.
