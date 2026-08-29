---
id: HIR-0108
title: Finalization attests the last model turn
status: proposed
introduced_in: unreleased
date: 2026-08-29
failure_class: successful_finalization_misclassified_as_turn_exhaustion
mechanism: revision_bound_materialization_finalization_attestation
adr: ADR-0006
---

# Finalization attests the last model turn

## Observed failure

Four Layer 2 rematerializations reached a structurally valid candidate at their budget
boundary. Runs `20260829T094605Z-a1b1cb`, `20260829T101336Z-ac6aa6`, and
`20260829T113343Z-d8aa13` validated a final patch and exhausted before another
finalize/gate turn. Run `20260829T114846Z-103e62` went further: after the typed
geometry-cycle finding drove a complete reverse-order redesign, it staged the final
three-unit DAG and `finalize_materialization` returned `VALIDATION PASSED` at 740.1
seconds. The SDK then reported `error_max_turns`, so HIR-0027 discarded the explicitly
finalized revision.

## Root cause

`run_session` correctly treated every max-turn result as terminal before consulting the
caller's post-condition. That ordering fixed HIR-0027's dirty-candidate publication bug,
but the materialization post-condition was only “the file currently validates.” It did
not distinguish a candidate that happened to exist at exhaustion from one the model had
explicitly submitted through the dedicated finalization boundary on its last turn.

The terminal action and candidate revision were not durably linked.

## Decision criteria

- Candidate existence or incidental validation never overrides turn exhaustion.
- Only a successful `finalize_materialization` call may create terminal authority.
- The terminal record binds the selected global bundle hash and exact candidate byte
  revision.
- Any subsequent stage, unstage, patch, truncation, or external write invalidates the
  record without cleanup or heuristic timestamps.
- Materialization is the only session type that opts into accepting max-turn exhaustion
  with a valid terminal record; every other caller retains HIR-0027 semantics.
- Publication still runs the normal materialization transaction and can fail closed on
  current authority, replan, or write errors.

## General mechanism

Successful `finalize_materialization` writes a run-local
`vfx-harness.materialization-finalization/v1` record beside the candidate. It contains
the selected bundle hash and SHA-256 revision of the exact validated bytes. The outer
materialization session now defines success as that record matching the current bundle
and candidate revision, not merely as a parsable file.

`run_session` has a default-off `accept_max_turns_if_succeeded` policy. Materialization
enables it with the attestation predicate. When the SDK reports max turns after the
finalizer tool result, the outer flow may continue only if that exact predicate passes.
All other max-turn results—including a last-turn patch that says validation passed but
was never finalized—remain failed transactions.

## Rejected alternatives

- Raising the materialization turn budget would defer the boundary and violate the
  bounded-unit scaling rule.
- Checking generic candidate validity before max-turn classification would recreate the
  dirty-publication defect fixed by HIR-0027.
- Treating the finalizer's console text as authority would not bind the result to bytes
  and would be lost or forgeable through transcript truncation.
- Automatically promoting every failed run with a valid scratch file would bypass the
  explicit terminal action and current-bundle verification.

## Validation

Resilience fixtures prove that max turns remains terminal when a generic success
predicate is true, and is accepted only when the caller explicitly opts into the
attested path. Materialization fixtures prove bundle mismatch or any candidate byte
change invalidates a finalization record. Planner policy fixtures prove the JIT session
uses the attestation predicate rather than candidate existence.

Producing-run evidence and broad regression results will be added after a
materialization finishes on its last model turn or completes normally with the same
attested post-condition.

## Release and rollback

No published authority schema change. The attestation is disposable run-local evidence;
selected authority and durable unit state retain their existing transactions. Rollback
would again discard an explicitly finalized revision solely because the SDK had no turn
left for narration, so rollback is unsafe.
