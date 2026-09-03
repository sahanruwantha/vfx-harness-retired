---
id: HIR-0186
title: vfx run dispatches receipt-backed layer amendments instead of waiting for an operator
status: accepted
introduced_in: unreleased
date: 2026-09-03
failure_class: mechanical_operator_step_between_typed_stop_and_its_named_transaction
mechanism: receipt_backed_run_controller_with_identity_convergence_and_caps
adr: ADR-0010
---

# vfx run dispatches receipt-backed layer amendments instead of waiting for an operator

## Observed failure

Run `20260903T100335Z-fa5dbb` on `artifacts/room_1046_opening` stopped at the layer-2
builder boundary with a closed `authority_defect` envelope: `streetlight_flicker` had
scheduled `data.energy` on mesh fixtures with no Light carrier, the builder abstained with
`cannot_express_in_scope`, and the envelope named exactly one legal transaction,
`publish_validated_amendment` for layer 2 with finding `hf-26516d9cbbf07fe51c84` as
evidence. Nothing further happened for the rest of the day. The operator had to read the
record, type `vfx plan artifacts/room_1046_opening --layer 2 --rematerialize --owner
operator --trigger ... --evidence state/hypothesis-falsifications/hf-26516d9c....json`,
wait, then type `vfx run --from 2`. The same shape recurred on every one of the day's
stops (HIR-0174 retry, HIR-0175 rematerialization, HIR-0181 reopened lower layers):
mechanical actions fully determined by a typed record, each paid for in idle hours.

## Root cause

ADR-0010 records the decision: `vfx run` was a single pass by design because six of the
seven transaction kinds had no receipt-backed adapter, and a controller that inferred
its next action from status, exit code, or prose was rightly forbidden (HIR-0164). That
rule was never "dispatch is unsafe"; it was "nothing yet proves that a transaction ran".
For layer-view amendments the proof already existed in pieces: the stop envelope binds
the exact finding and the selected authority digest it observed, the rematerialization
transaction publishes through the atomic authority-state publisher and re-verifies its
state binding (HIR-0171), the receipt store provides prepared → running → terminal
chains with crash-orphan reconciliation (HIR-0166), and the selected authority resolver
yields a canonical after-state digest. The missing mechanism was the adapter that ties
them together under one idempotency key, plus convergence and caps so a loop cannot
re-author the same defect at model prices.

## Decision criteria

- A controller may dispatch only a transaction whose commit an independent evaluator
  proves from durable records; everything else stays the run's terminal stop.
- The controller adds no second implementation of any transaction: it runs the same
  `vfx plan --layer N --rematerialize` stage an operator would, as a child process under
  the same run id, and proves the result from selected authority.
- Identity converges before money bounds: a finding whose cause fingerprint was already
  dispatched in this shot stops the run, whatever the caps say.
- Every dispatch is itself a record in the run: the consumed envelope, the receipt chain,
  the commit, the evaluation, and the ledger row all bind each other by digest.
- Refusals leave the envelope exactly where the single-pass driver left it.

## General mechanism

1. `application/run_controller.RunController.dispatch(envelope)` accepts only
   `publish_validated_amendment` with `scope == "layer_view"`. It refuses, with a closed
   reason, a global amendment (`not_dispatchable`), a finding whose `fault_owner_units`
   lie outside the target layer's stages or that changes a hard constraint
   (`out_of_layer_owner`, `hard_constraint`), an unreadable or tampered source finding
   (`evidence_unavailable`), an identical cause fingerprint already dispatched in the shot
   or an unsatisfied repetition per `classify_stop` (`repeated_finding`), and any exhausted
   cap (`budget_exhausted`: dispatches per run, rematerializations per layer across the
   shot's durable attempts, and an optional USD ceiling read from the run's cost log).
2. The transaction runs under `state/recovery/controller/locks/<key>.lock` with the key
   `action_idempotency_key(action, before, attempt)`. The receipt chain is
   prepared → running (`query_external`) → terminal. Before the child stage runs, the
   consumed envelope is archived as `reports/controller-consumed-stop-NN.json` and the
   run's stop-envelope slot is cleared so the child can publish its own typed stop. The
   child is `python -m vfx_harness.agents.planner <shot> --layer L --rematerialize --owner
   <owner_authority_id> --trigger <finding ids and found text> --evidence <locator>` where
   the locator is the source finding named by the builder's stop audit (hash-verified) or,
   for a materialization stop, the envelope's own evidence record.
3. Proof: after the child exits 0, the selected authority must be `selected`, its effective
   view `jit`, its assertion digest different from the envelope's
   `authoritative_before_digest`, and the JIT head must list the layer. Then
   `domain/controller_commits.RematerializationCommit` binds the running receipt, the
   before digest, and the after assertion; it is published immutably, the terminal
   `committed` receipt names it as commit marker, and
   `evaluate_rematerialization` independently re-reads the commit, chain, and authority,
   publishes the `PostconditionEvaluation` and a `PriorDispatchAttempt`, and only then the
   ledger row `reports/controller-dispatch-NN.json` (`vfx-harness.controller-dispatch/v1`)
   is written. A child exit code other than 0 or an unpublished replacement ends the chain
   `failed` with a typed adapter-failure report as result evidence; the child's own stop
   becomes the run's terminal envelope.
4. `application/run_shot` builds a `RunController` unless `--single-pass` or `--dry-run`
   is set, passes it to every builder-boundary stop, raises `_ContinueRun` on a dispatch,
   re-derives the receipt-backed prefix from the selected DAG, and continues the layer loop
   (a rematerialized layer is unpassed, so it and any reopened lower layers rebuild). The
   run summary carries `controller.dispatches` (the ledger rows) and
   `controller.refusal` when a refusal selected the terminal stop.
5. Crash reconciliation: a death between the terminal receipt and the ledger row leaves a
   committed chain and no attempt record; the next dispatch of the same envelope finds the
   committed head, evaluates it, writes the row, and runs no child. A death between the
   child's publication and the terminal receipt leaves a running receipt whose
   `query_external` disposition is answered by re-resolving authority on the next call.
6. Shared primitives moved to `orchestration/immutable_records` (canonical bytes,
   immutable publish, exact read-back, evidence refs, key locks); the environment recovery
   store imports them instead of carrying its own copies.
7. `run_shot._needs_global_plan` reads the selected authority; when the plan pointer is
   absent (malformed authority still fails closed), `vfx run` runs the global plan loop
   (`python -m vfx_harness.agents.planner <shot> --until-clean`) as a child stage under the
   same run id before resolving layers, and a non-zero exit is consumed through the same
   typed-stop boundary as every other stage. A selected bundle is never redrafted by the
   driver; global republication stays reviewed (ADR-0010). A dry run without a plan explains
   instead of drafting.

## Rejected patch-level alternatives

- A shell loop re-running `vfx run --from N` after reading `status.json` `detail`: dispatch
  from prose, forbidden by HIR-0164.
- Dispatching inside the builder process when it records the falsification: the builder
  would mutate plan authority from within a build, breaking the one-writer boundary.
- Retrying without identity convergence and relying on a dollar cap: the loop would pay
  for the same misunderstanding until money ran out (HIR-0175 §5).
- Per-attempt child runs: a new run id per dispatch splits one shot closure into unrelated
  runs, the exact defect the driver was created to remove.

## Validation

- `src/tests/unit/test_run_controller.py`: a dispatch runs the exact rematerialization
  stage, produces a prepared/running/committed chain with the commit as marker, publishes
  the commit, evaluation, and attempt, archives the consumed envelope, and writes the ledger
  row; a failed child ends the chain `failed` without retry; the same cause fingerprint
  converges instead of dispatching again; dispatch, per-layer, and USD caps refuse before
  spend; out-of-layer owners and hard constraints are refused; a crash between commit and
  ledger row is reconciled without a second child run; an `escalate_question` stays terminal.
- `src/tests/unit/test_run_shot_controller.py`: the driver continues after a dispatch,
  records a refusal in the summary and selects the consumed envelope, selects the failed
  adapter's own stop, replans and rebuilds the replaced layer inside one run to a passed
  terminal status, and `--single-pass` builds no controller.
- `src/tests/unit/test_run_shot_global_plan.py`: an absent plan pointer runs the global
  plan loop before the first layer and the run passes; a selected plan is never redrafted; a
  failed global plan is a typed stop with the planner's exit code before any layer; a dry run
  without a plan explains instead of drafting.
- Existing driver, environment recovery, and receipt store suites pass unchanged.

## Release and rollback

Lands with ADR-0010 step 4 and the `--single-pass` debugging flag. Rollback is running with
`--single-pass`; the controller's durable records under `state/recovery/controller/` are
inert to every other reader.

## Remaining limitations

`retry_exact_unit`, `apply_revision_checked_replan` (already-selected amended authority),
`resume_checkpointed_session`, and `recover_environment` remain non-dispatchable from the
controller: the first has no producer, the third no complete resume receipt (HIR-0164),
and the environment adapter evaluates a terminal run and requires external repair. Global
plan amendments stay reviewed operator transactions. Materialization-stage amendment stops
carry the envelope's own evidence as the rematerialization evidence; a layer never
materialized cannot be rematerialized, so a first-materialization structural rejection is
still a terminal stop. Consumed envelopes are archived by bytes and digest, but the
evidence files they cite share report names with the next stage's evidence and may be
overwritten; the ledger row and receipt chain, not the archived locators, are the audit
authority for a consumed stop.
