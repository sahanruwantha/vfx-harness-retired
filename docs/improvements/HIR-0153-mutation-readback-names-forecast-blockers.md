---
id: HIR-0153
title: Mutation readback names irreversible forecast blockers immediately
status: accepted
introduced_in: unreleased
date: 2026-08-30
failure_class: live_tool_teaches_blocking_evidence_as_diagnostic
mechanism: blocker_aware_mutation_readback
adr: ADR-0006
---

# Mutation readback names irreversible forecast blockers immediately

## Observed failure

Bounded Layer 2 run `20260830T115340Z-a73924` evaluated the camera-owned deferred
subject rows after every massing mutation. The first tower already read
`building_miniature_start bbox_height=0.2564` against `<= 0.15`. HIR-0152 made that
an irreversible freeze blocker because later union geometry cannot reduce height, but
the live mutation tool still labelled every row `DIAGNOSTIC ONLY`. The builder therefore
continued through six mutations, a render, a comparison, and a terminal handoff before
the verdict revealed the same row as required. A repair session then proved the conflict
was owned by the sealed earlier-layer camera.

Layer 3 was never opened.

## Root cause

The freeze verdict and the live tool rendered the same typed forecast evidence through
different classifications. The verdict called
`irreversible_deferred_subject_forecast_failures`; mutation readback formatted the raw
forecast list without that classifier. The tool therefore taught a legal conclusion that
the acceptance boundary had already made false.

## Decision

- Every post-mutation forecast readback runs the same registry-backed irreversible-union
  classifier used by live, canonical, and revalidation verdicts.
- An irreversible miss is labelled `REQUIRED BEFORE FREEZE` immediately and names the
  two legal actions: repair the current producer or call `cannot_express_in_scope`.
- The row remains nonpayment for the owner contract; its blocker status is current-unit
  required evidence, not transferred ownership.
- Repairable-side misses remain explicitly diagnostic.
- `contract_result` returns the same blocker classification for a requested forecast id.
  It may not re-label an irreversible miss as `diagnostic_only`.

## General mechanism

`_deferred_subject_forecast_note` accepts both the compiled contract rows and fresh
evidence, partitions them with
`irreversible_deferred_subject_forecast_failures`, and renders blocker and diagnostic
sections separately. The automatic mutation probe retains the loaded contract rows for
that classification. The exact-id `contract_result` path applies the same classifier to
its freshly measured row.

## Rejected alternatives

- Make the prompt emphasize the terminal gate: the contradictory statement came from a
  trusted tool after each mutation.
- Promote every forecast to a blocker: a successor may legitimately repair a lower-bound
  union miss.
- Stop immediately on any blocker without model involvement: the active producer may be
  able to repair it, and abstention must remain available when it cannot.

## Validation

The builder-instrument regression carries one upper-bound height miss and one lower-bound
height miss in the same readback. It requires the former to appear under `REQUIRED BEFORE
FREEZE` with the legal abstention action, and the latter to remain `DIAGNOSTIC ONLY`.

## Release and rollback

No persisted schema change. Rollback restores contradictory live teaching and is unsafe.
