---
id: HIR-0014
title: Instruments that cannot lie
status: proposed
introduced_in: unreleased
date: 2026-08-23
failure_class: confident_evidence_that_could_not_support_its_claim
mechanism: typed_declarations_and_fail_closed_readings
---

# Instruments that cannot lie

## Observed failure

Build run `20260823T154920Z-6d4022` produced the project's first work-unit build: 45
minutes, ~$12.9, 53 objects, two rounds plus a bounded repair, rollback-to-best-state,
a 58-call journal, and a published unit script. It failed with `iris_mechanism=failed`,
`camera_ingress_take=blocked`, reporting `2/4 scene contracts pass`.

Every one of those numbers was untrustworthy. Reviewing the artifacts found six
independent defects, none of which any test or gate detected:

1. **Unknown contract keys were ignored silently.** A retraction contract declared
   `at_frame: 36`; nothing reads that key, `row.get("frame", 1)` defaulted to 1, and a
   sealed frame-1 reading was reported as a frame-36 failure for the entire build.
2. **`onset_order` accepted overlapping selectors.** The stagger contract compared the
   combined lead/trail selector against itself, computing `onset(X) - onset(X) = 0` —
   a contract that can never pass and never fails honestly.
3. **Metrics certified claims they cannot support.** "Warning lights chase around the
   rim" was closed by an `object_count` of 24 (a count has no time in it); "the seal
   reads as layered machined metal" was closed by radial closure (geometry cannot see
   appearance). Both metrics measured correctly. Both claims were unproven.
4. **Look scope was inferred from axis identifiers.** `axis_feedback_groups` scanned
   axis *names* for words like material/surface/light. `iris_seal_readability` contains
   none, so a unit whose own plan owned layered metal, seams, fasteners and
   close-range readability was told appearance was out of scope — and built to the
   mechanical contracts it could see.
5. **Required evidence could pass by absence.** Sealing asked "did something pass and
   did nothing fail?" A contract never evaluated — selector matching nothing, probe
   error, frame group never run — is neither, so it could not block anything.
6. **The finalized script did not describe the selected checkpoint.** Finalization
   restored round 1, then dumped the *whole* journal, publishing round 2's rejected key
   light and tunnel taper into `build/units/01/iris_mechanism.py`.

A seventh, latent: mutation scope was checked at canonical replay (correctly, against
the active unit) but the revalidate fast path resolved the unit as "the only stage", so
scope went unchecked for every multi-unit layer — and violations surfaced only at the
end, after the budget was spent.

## Root cause

Each defect is the same shape: **a reading that looked authoritative while being
unable to mean what it claimed.** The harness had strong machinery for deciding things
by evidence and almost no machinery for asking whether a given piece of evidence could
decide the thing it was attached to. Silence — an ignored key, an unevaluated contract,
an unscanned axis name — consistently read as consent.

## Mechanism

Typed declaration replaces inference; absence fails closed.

- Contract rows reject unknown keys with the accepted set enumerated, and
  frame-sensitive kinds must declare `frame` rather than inheriting the silent
  default of 1.
- `onset_order` requires disjoint selectors, naming the shared roles.
- Every metric kind declares the evidence domain it can certify (`scene`, `temporal`,
  `projected_composition`, `image`); every required claim declares the domain its
  proposition lives in; materialization rejects a claim whose bound evidence cannot
  certify its domain. An honest existence claim backed by a count still passes.
- Work units declare `look_capabilities` from a closed vocabulary; the builder resolves
  the declaration to image-feedback families. Identifier scanning survives only for
  schema-4 layers with no declaring unit. Declared appearance ownership additionally
  requires candidate-bound image evidence before sealing — the case no plan-time
  binding can cover, since image contracts are candidate-bound by design.
- Sealing takes the contracts bound to the active unit's required claims and blocks on
  anything never produced, naming the absent ids and their usual causes.
- Finalization bounds the journal by the selected checkpoint's write-ahead
  `journal_index` — a value snapshots already returned, never wired to finalize.
- Scope is enforced against the active unit on the revalidate path (which now refuses
  the fast path when it cannot verify scope) and reported live on the `run_bpy` call
  that creates an out-of-scope object, using a rule pinned by test to match the
  canonical replay rule exactly.

## Validation

Offline replay of the failed run's real artifacts against the fixed stack, no model or
Blender spend — 13/13 confirmations:

| Confirmation | Result |
|---|---|
| `at_frame` contracts rejected (frame-1 reading sold as frame 36) | detected |
| self-comparing `onset_order` rejected | detected, names both shared roles |
| honest contracts still accepted | 2/6 accepted, unchanged |
| required claims declaring no asserted domain | 6 of 6 now fail closed |
| chase/readability claims rest on scene-domain metrics only | confirmed |
| materialized units declaring no `look_capabilities` | 2 of 2 now rejected |
| old keyword gate gave the built layer's axes no look feedback | reproduced exactly |
| declaring material/detail restores it | confirmed |
| unevaluated required evidence blocks sealing | confirmed |
| published script creates roles outside declared scope | `iris_housing`, `reactor_core_target` |
| live scope rule agrees with canonical rule | on every created role |
| journal carried every round's calls | 58 calls, untruncated |
| rejected round-2 work reached the published script | key light present |

Repository verification: ruff clean, 246 tests, `vfx --help` healthy.

## Consequences

The failed unit must not be retried under its current authority: its contracts are
defective, its script violates its own declared scope, and its claims are proxied.
Layer 1 requires re-materialization through the existing replan transaction, not
`vfx units retry`. The rebuilt DAG should put the camera unit at the root so the
approved spine meets executable evidence first.

This HIR does not claim the integrity class is closed — only that every defect this run
exposed is now detected on the run's own artifacts. Further classes will surface the
same way: by building something and auditing what the instruments claimed.
