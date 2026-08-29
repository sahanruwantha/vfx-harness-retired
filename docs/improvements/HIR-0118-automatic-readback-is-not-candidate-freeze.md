---
id: HIR-0118
title: Automatic read-back is not candidate freeze
status: accepted
introduced_in: unreleased
date: 2026-08-30
failure_class: intermediate_structural_pass_froze_executable_only_mutation
mechanism: terminal_handoff_freezes_executable_only_candidates
adr: null
---

# Automatic read-back is not candidate freeze

## Observed failure

Room 1046 held-out Layer 2 run `20260829T194427Z-04d002`, unit
`roofline_signage`, created a temporary eight-vertex marker to read back the measured
tower-top placement before constructing its multi-part parapet, pavilions, and sign. The
automatic post-mutation probe reported every bound executable row passing because the
unit's structural floor was `mesh_vertex_count >= 8`. That probe set
`scene_contracts_passed`, and the pre-tool convergence guard denied the next three legal
mutations, including removal of the marker and the complete authored replacement. The
session was stopped before finalization rather than knowingly publishing the placeholder.

The preceding `tower_massing` unit showed the same mechanism at lower severity. Its first
two-box mass satisfied the vertex floor; after read-only diagnostics identified an owned
scale concern, the guard refused three corrective calls. Across the two units the live
run recorded six `convergence_mutation_blocked` denials after intermediate structural
passes.

## Root cause

The runtime collapsed two different state transitions:

- post-mutation read-back answers whether the current scene satisfies the bound rows;
- candidate freeze declares that the builder has finished every authored ticket and that
  this exact state is ready for isolated evaluation.

`run_bpy` performed the first transition but the guard treated it as the second. A weak
existence or structural floor can become true before a multi-step unit is complete, so
the first passing mutation could mechanically strand unfinished work. This contradicted
the staged runtime's explicit `building -> frozen -> evaluating` state machine and made a
temporary role-tagged probe indistinguishable from a terminal candidate.

## Decision criteria

- Automatic executable evidence remains immediate read-back after every mutation.
- An executable-only unit stays mutable for the rest of its live builder session; the
  model's terminal handoff is the freeze boundary, after which the harness independently
  evaluates, distills, and replays the artifact.
- An image-bound unit retains the existing compare-before-more-mutation lock: once scene
  interfaces pass, the exact current scene must pay and compare immutable image evidence.
- Look work with no bound image contract and audited judgment retries retain their
  existing HIR-0044 and HIR-0021 behavior.
- No scene name, role name, frame, coordinate, metric threshold, or department is inferred.

## General mechanism

`builder_phase_guard` now arms convergence mutation denial only for units with typed
`image_evidence_required`. Scene-contract feedback still updates
`scene_contracts_passed`, but for executable-only work that flag is observation state,
not freeze authority. The outer builder session already supplies the durable freeze
boundary: when the model returns, the harness snapshots the scene, evaluates the active
unit's exact evidence closure, distills the journal, and proves the script through
empty-scene replay.

The post-mutation tool response now says explicitly that a passing structural floor is
read-back rather than candidate freeze and directs the builder to complete and observe
every authored ticket before terminal handoff. This is feedback for the mechanical state
transition, not a prompt-only substitute for enforcement.

## Rejected alternatives

- Allow one or N extra mutations after the first pass: an arbitrary retry allowance is a
  stopgap and still truncates units needing N+1 edits.
- Raise the vertex floor for this shot: that overfits one roofline and does not distinguish
  a finished asset from any sufficiently dense placeholder.
- Tell builders never to use temporary geometry: intermediate authored states occur in
  every domain, and prompt wording cannot override a mechanical deny.
- Remove convergence locking globally: image-contract units need an immutable candidate
  between scene convergence and comparison.
- Accept and repair the placeholder downstream: downstream compensation would promote an
  unfinished upstream artifact into authority.

## Validation

- `tests/integration/test_harness.py` pins both sides of the boundary: an executable-only
  structural pass keeps `run_bpy` legal, while an image-bound pass still denies further
  mutation until comparison. The new executable-only assertion fails against the prior
  guard and passes with this mechanism.
- Static verification: `.venv/bin/ruff check src tests` passed; the focused integration
  harness passed every check; `.venv/bin/python -m pytest -q` passed 587 tests; and
  `.venv/bin/vfx --help` passed.
- Held-out producing rerun `20260829T201315Z-59c634` rebuilt `roofline_signage` from
  clean priors. The formerly denied full replacement executed after the first structural
  pass and produced one 616-vertex / 462-face roofline containing the cornice, merlons,
  five pavilions, and sign. The report records zero `convergence_mutation_blocked`
  hooks versus the interrupted baseline's three denials on this unit. The live phase
  completed in 28 turns, 534.8 seconds, and $1.737. Canonical evaluation then failed
  closed on the separate protected `hero-window-visible-f38` row and published typed
  finding `hf-117eeea1918a7055124e`; that downstream failure does not reuse or obscure
  the producing proof for this mutation-boundary mechanism.

Implementation commit: pending.

## Release and rollback

No persisted schema change. Interrupted run `20260829T194427Z-04d002` remains audit
evidence and must not be resumed as proof of the fix. The fault-owning unit is retried
through the audited lifecycle and rebuilt under the new guard. Rollback restores the
premature freeze and is unsafe.

## Remaining limitations

An executable-only unit can still publish a poor terminal candidate if its authored
contracts do not cover the claim. That is a separate plan/evidence defect and must fail at
materialization or evaluation through its own mechanism; this change only ensures the
builder can finish the work its current authority already permits.
