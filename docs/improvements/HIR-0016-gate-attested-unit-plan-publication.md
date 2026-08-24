---
id: HIR-0016
title: A failed plan gate left its materialization behind as build authority
status: accepted
introduced_in: unreleased
date: 2026-08-24
failure_class: failed_transaction_left_durable_authority
mechanism: gate_attested_two_phase_unit_plan_publication
adr: ADR-0004
---

# A failed plan gate left its materialization behind as build authority

## Observed failure

Run `20260824T103842Z-afec73` generated the JIT plan for unit `1.iris_mechanism_detail`,
wrote it to `plans/units/iris_mechanism_detail.md`, stamped its authority sidecar, and
then failed the deterministic gate with three blocking findings. The run terminated —
but the written plan and sidecar stayed on the shot.

The next build (`20260824T140241Z-10489f`) reached the unit, found the file, **skipped
both generation and the gate** (the gate ran only inside the generation branch), and
started building the unit on gate-failed authority. Caught only because a human asked
why the gate "passed" this time; the run was stopped mid-unit. Byte identity between the
shot file and the failed run's consumer-view copy (md5 `68f31a35…`, mtime inside the
failed run) is the preserved evidence.

A second latent instance: `vfx plan --layer` generated and stamped unit plans with **no
gate at all** — only the builder path gated, and only when generating.

## Root cause

The materialization transaction ended at the wrong boundary. Write → stamp → *return*
was the transaction; the gate was a separate, caller-owned step. Two consequences:

1. A gate failure after publication had nothing to roll back — the shot already held
   the artifact.
2. Consumers equated file existence (plus a bundle/bytes integrity stamp) with
   authority. The v1 sidecar could not represent the difference between "published
   through a clean gate" and "left behind by a failed transaction", so absence of the
   gate read as consent — the HIR-0014 shape at the transaction layer, and precisely
   the class ADR-0004 exists to close for global plans.

## Mechanism

Publication is two-phase and the gate is inside the transaction:

- `stamp_work_unit_plan` still writes an integrity stamp (bundle hash, plan bytes,
  path) — schema bumped to `vfx-harness.unit-plan-authority/v2`. A `gate=` argument
  adds the attestation fields (`gate_clean`, `gate_blocking`, `gate_run_id`) and
  refuses to stamp anything but a clean result.
- `generate_layer_plan` owns the whole transaction: agent writes → size checks →
  integrity stamp → consumer view → deterministic gate. A clean gate re-stamps with the
  attestation; anything else — dirty gate, crash, size violation — **rolls the shot
  back** to the prior plan/sidecar bytes (or removes them) and raises with the gate
  report. Both callers (builder JIT path and `vfx plan --layer`) inherit this, which
  also closes the ungated CLI path.
- `validate_work_unit_plan_authority` requires the clean-gate attestation by default.
  The gate pipeline itself (consumer-view staging, the gate's hierarchy check) passes
  `require_gate=False` — it checks integrity only, because it runs before the
  attestation it produces can exist.
- The builder treats an existing plan without valid gated authority as **absent** and
  regenerates through the transactional path, logging why. Regeneration is the designed
  recovery for a derived artifact; it can only replace the file with a gated one, so no
  state is hand-edited and no invalid file can wedge a shot.

Bundle-frozen unit plans are untouched: bundle membership is already gate-attested by
`--until-clean` publication and hash-verified on resolution.

Rejected alternative: hard refusal (stop the build, tell the operator to delete the
file). Deleting authority by hand is exactly the operation the unit-state design
refuses to normalize; regeneration-through-the-gate expresses the same decision inside
the transaction system.

## Validation

- The real leaked artifact fails closed: `validate_work_unit_plan_authority` on the
  shot's `plans/units/iris_mechanism_detail.md` refuses it (v1 sidecar → stale
  authority) at both strengths; the build loop therefore regenerates instead of
  trusting it. No hand cleanup required or performed.
- New tests: an integrity-only stamp satisfies `require_gate=False` and is refused as
  build authority; a forged `gate_clean`/`gate_blocking` attestation is refused; a
  dirty gate result cannot be stamped; v1 sidecars fail closed; consumer-view pair
  staging and bundle-member flows keep passing with attested stamps.
- Repository verification: ruff clean; `pytest -q` 271 passed; `vfx --help` healthy.

## Consequences

Existing shot-root JIT plans stamped under v1 stop being build authority everywhere;
each regenerates through the gate on next consumption (bounded model spend per plan).
For the current shot this is the desired outcome: the leaked plan is inert, and unit
2's next generation runs against whatever global authority is then selected — its gate
findings (global schema-5 sparsity violations) are plan-content work for the pending
replan, not covered by this record.

Not closed here: `_materialize_deferred_layer` / `_rematerialize_layer` write other
materialization artifacts (layers refresh, contracts) with their own validation but
without this two-phase gate attestation; if a failure-to-mechanism case surfaces there,
extend the same transaction shape rather than adding a second one.
