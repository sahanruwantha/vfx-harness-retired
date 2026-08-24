---
id: HIR-0017
title: The composed authority lifecycle had never passed, and agents lacked the instruments to see why
status: accepted
introduced_in: unreleased
date: 2026-08-24
failure_class: seam_defects_between_individually_correct_mechanisms
mechanism: lifecycle_aware_rules_composed_fixture_and_decision_grade_agent_tools
adr: ADR-0004
---

# The composed authority lifecycle had never passed, and agents lacked the instruments to see why

## Observed failure

Run `20260824T153427Z-91b7c1` — the first materialization under the first fully clean
sparse bundle (`088b4a7c…`), with honest instruments (HIR-0015) and transactional
publication (HIR-0016) already in place — still could not publish a unit plan:

1. The deterministic gate blocked the generation's own materialization: a ready layer
   and its concrete contracts in the consumer view were flagged as
   `global-preproduction` debt, and decision A2's `cam_rig` reservation could not be
   satisfied because the reserving layer, having materialized, was no longer deferred.
   The same misfires had killed run `20260824T103842Z-afec73`'s unit-2 generation that
   morning. Combined with the (now sealed) plan leak, **the materialize→gate→publish
   path had never once passed end-to-end** — every unit plan a build ever consumed had
   arrived through the leak.
2. The materializer burned 8 validator rounds trying to express camera clearance with a
   vocabulary that had no kind for it, then padded: R29 bound to an invented
   builder-writable custom property (self-certification) and R4 to a `bbox_center_x`
   with `lo: -1.0` — a bound the [0,1] metric satisfies unconditionally.
3. The session had an escalation tool and did not use it; padding was representable
   and cheaper than honesty.
4. Underlying, from `afec73`: canonical repair and finalize sessions operate blind —
   they edit scripts whose rebuilt consequences they cannot observe — and the rig-aim
   ownership lesson that would have prevented both broken repairs lived only in a
   worker docstring no session reads.

## Root cause

Two classes:

- **Lifecycle blindness.** The gate's `global-preproduction` and `decision-adoption`
  rules encode the PRE-materialization reading of a schema-5 generation and were never
  taught its post-materialization state, although that state is produced by the
  harness's own verified transaction. Every seam defect of 2026-08-22..24 (leak, bundle
  membership, stale view, inexpressible supersession, these rules) shares one property:
  each mechanism was individually tested, and the COMPOSED path ran only inside paid
  production runs.
- **Instrument-poor agents.** Deterministic information the harness already held —
  the contract vocabulary, the terminal gate's verdict, the rebuilt artifact's evidence
  and transforms, the cookbook's lessons — reached agents only as one error per model
  turn, a retracted artifact, or not at all. Padding, blind repairs, and 8-round
  validator walks are the rational behavior of a capable model inside that instrument
  set.

## Mechanism

Lifecycle:

- `_materialized_view` (plan_gate): exemptions gated on integrity — view pointer schema,
  bundle-hash match with the consumer-view marker, and staged overlay bytes matching the
  pinned hashes. Ready layers and contracts owned by verifiably materialized layers are
  the designed shape, not preproduction debt; a decision is adopted when a pinned
  materialized contract carries its `decision_id` with the exact approved values.
  Anything unverifiable falls back to the strict reading. Validated against the real
  shot: 4 blocking → 2, both survivors legitimate.
- `tests/integration/test_lifecycle_fixture.py`: one hermetic generation (no model, no
  Blender) walks publish → materialize → gate → two-phase unit-plan attestation →
  seal → republication → supersession, asserting every seam that failed this week.
  The composed path is now suite-guarded at sub-second cost.

Vocabulary and honesty:

- Three evidence kinds close the gaps that forced padding: `curve_derivative_max`
  (per-frame smoothness bound; catches a one-frame 0.8 rad jump endpoint deltas miss),
  `path_clearance_min` (ray-measured clearance, `lifecycle: persistent` so it
  re-evaluates as obstacle geometry arrives; empty selection reads vacuously clear),
  `parallax_displacement_profile` (screen-space displacement ratio between role
  groups). Real-Blender fixture: 0.8 exact, 0.117 through an obstacle, 6.56× near/far.
- `validate_row` refuses vacuous shapes: projected bounds outside the normalized frame,
  non-measured `object_property` paths (self-certification), overlapping two-sided
  selectors. Padding is now unrepresentable, not discouraged.
- `escalate_vocabulary_gap` records a typed durable gap (requirement, claim, every
  attempted kind and why it cannot certify) under `state/plan-escalations/`; the
  requirement closes with an explicit decision referencing the gap id.

Instruments:

- `evidence_vocabulary`: the full kind registry (definition, domain, required fields)
  in one call, for materialization and unit-plan sessions.
- `gate_preview`: the exact terminal gate against the current consumer view, in-session,
  capped and plateau-detected — findings become fixes instead of retracted generations.
- `probe_candidate`: repair/finalize sessions rebuild their CURRENT artifact in a
  disposable worker and receive the authoritative evidence rows, evaluated camera/role
  world transforms, the `rig_contract` check, and a solid render per judge frame.
  Either misdirected `afec73` repair dies in its first turn against this output.
- `check_rig_contract` (self-tested on a known-bad fixture) fails closed on X/Y
  rotation keys or tracking constraints on a rig-parented camera;
  `harness-lesson-rig-aim-ownership` carries the distilled lesson as a retrievable
  recipe, and repair sessions now carry the recipe tools.
- Failed executable contract rows name the exact local invocation that re-measures them.

## Validation

- Real shot: `vfx evals plan` 4 blocking → 2 (composition-coverage and the retracted
  unit plan — both true); the leaked artifact and both gate misfires are inert.
- Hermetic lifecycle fixture passes end-to-end; suite 283, ruff clean, `vfx --help`
  healthy.
- New-kind measurements pinned against seeded ground truth in real Blender 5.2.
- Vacuity linting reproduces on the run's own padded contracts (both rejected).

## Consequences

The shot's layer-1 materialization (sealed under the pre-lint vocabulary) contains the
two padded contracts and no projected context at f1; it must be reverted and
re-materialized under the new vocabulary — the composition-coverage finding already
demands this, and `revert_materialization` is the designed route. Not closed here:
`_rematerialize_layer`'s ready-era transaction still predates two-phase attestation
(noted in HIR-0016), and the vocabulary will lag new claim types by design — the gap
record is the backlog mechanism, not a fix for it.
