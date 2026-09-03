---
id: ADR-0010
title: vfx run dispatches receipt-backed transactions until a shot closes
status: proposed
date: 2026-09-03
supersedes: null
---

# vfx run dispatches receipt-backed transactions until a shot closes

## Context

`vfx run` today executes one pass: plan-gated materialization, build, accept, render, and
it stops at the first unaccepted boundary with a closed stop envelope (HIR-0164). The
envelope proposes exactly one typed transaction from a closed vocabulary
(`retry_exact_unit`, `publish_validated_amendment`, `apply_revision_checked_replan`,
`route_engineering`, `recover_environment`, `resume_checkpointed_session`,
`escalate_question`), with preconditions, a postcondition, an idempotency key, and
convergence on semantic evidence identity (HIR-0166). Only `recover_environment` has a
receipt-backed public adapter; the standing rule is "there is no automatic controller"
and the other six kinds are non-dispatchable proposals.

The first real v2 runs on `artifacts/room_1046_opening` (2026-09-02/03) showed what that
costs. Every operator action between builds was mechanical and fully determined by a
typed record: `vfx units retry` after a dead attempt (HIR-0174), `vfx plan --layer N
--rematerialize --evidence <finding>` after `hypothesis_falsified` (HIR-0175), `vfx run
--from N` after that, `vfx reconcile --run-id` after a lost owner (HIR-0172). None
required judgment. Each required a human to read a record and type the command it
named, and each wait between stages left model sessions idle. The reason the controller
was forbidden was never that dispatch is unsafe in principle; it was that six transaction
kinds had no producer, commit protocol, independent evaluator, or crash fixtures, so a
loop could not prove that a transaction ran (HIR-0166, remaining limitations).

The same runs also showed the failure modes a loop must refuse: a materialization that
re-authors the same misunderstanding (the camera-layer judgment property, HIR-0175 §5),
a repeated identical finding that must converge rather than retry (HIR-0166), and
authority that a vocabulary retirement turned into a prior generation, which no
republication may migrate (HIR-0174, strict migration).

## Decision

`vfx run` becomes the normal operation that runs a shot to closure. After each unaccepted
boundary it reads the terminal stop envelope and dispatches the one transaction the
envelope names, if and only if that transaction kind has a receipt-backed adapter whose
independent evaluator proves the commit, then continues from the earliest legal unit.
The rule "there is no automatic controller" is replaced by: **a controller may dispatch
only receipt-backed transactions, and every dispatch is itself a receipt in the run's
ledger.**

Scope of automatic dispatch, in delivery order:

1. `recover_environment` (already receipt-backed): after a strict-preflight stop, once
   the environment re-verifies.
2. `retry_exact_unit`: the audited retry transition through the builder execution fence,
   only from `UNCLAIMED_RETRY_STATES` and only when the fence proves no live owner.
3. Owner-loss reconciliation for a run left `running` by a dead owner (the reconciler
   is already fence-proving); the controller invokes it before starting a successor run.
4. `publish_validated_amendment` and `apply_revision_checked_replan` at the
   materialization boundary: a `hypothesis_falsified` finding whose `fault_owner_units`
   lie inside one layer dispatches that layer's rematerialization with the finding as
   evidence, then resumes from that layer. A finding that names out-of-layer owners or
   changes a hard constraint is not dispatched.
5. `resume_checkpointed_session` only when the phase-specific immutable receipt that
   HIR-0164 requires exists; until then it remains non-dispatchable.

What the controller never does:

- Dispatch `escalate_question`, a hard-constraint amendment, or any transaction whose
  envelope names a human decision; the run ends with that envelope.
- Replan the sparse global authority (`vfx plan --until-clean` over an existing shot):
  global republication stays a reviewed operator transaction in this ADR.
- Migrate, wipe, or move prior-generation authority. A strict-migration rejection ends
  the run with a typed stop naming the archival step.
- Retry a finding whose semantic evidence identity equals a finding already dispatched
  in this shot; identical typed stops converge and the run stops (HIR-0166).
- Exceed the caps: a dollar ceiling per shot and per layer, an attempt cap per unit, and
  a dispatch cap per run. Cap exhaustion is a typed `budget_exhausted` stop.

Every dispatch writes a controller ledger row under the run (`reports/controller/`)
binding the consumed envelope digest, the adapter's receipt digest, the evaluator's
verdict, and the resumed unit, and the run summary lists the rows. The manual commands
(`vfx plan`, `vfx units`, `vfx reconcile`, `vfx recover-environment`) remain the reviewed
operator surfaces and share the adapters; the controller adds no second implementation of
any transaction.

## Consequences

- One command closes a shot when nothing needs a human, and the run's ledger shows every
  transaction it took. Idle time between stages disappears.
- Each newly dispatchable kind needs its receipt-backed adapter, independent evaluator,
  and crash and fork fixtures before the controller may call it; this ADR does not make
  a kind dispatchable by naming it.
- Spend becomes a controller responsibility: caps and convergence are part of the
  mechanism, not operator vigilance. A run that keeps re-authoring the same defect stops
  on identity, not on money alone.
- AGENTS.md's controller rule, HIR-0164's and HIR-0166's "no controller" limitations, and
  the operations guide change in the same change that lands step 1; each later step
  updates the rules it enables.
- Compatibility: none. Prior-generation runs stay readable through their own records;
  the controller ledger is a new run-owned record family.

## Rejected alternatives

- **Keep the operator as the transport.** Correct records, mechanical actions, and idle
  sessions; the cost is paid on every stop and the goal ("the real shot closes smoothly")
  is unreachable by design.
- **A heuristic controller that infers the next action from status, exit code, or
  transcript prose.** Rejected by HIR-0164: dispatch authority is the closed envelope
  only; anything inferred is a guess.
- **Automatic global replanning on any finding.** Rejected: global authority changes what
  "correct" means for every layer; the loop would happily rewrite the plan around a
  builder's misunderstanding. Global republication stays reviewed until a finding class
  proves it can be dispatched with the same receipt discipline.
- **A separate `vfx loop` command.** Rejected: two entry points invite running the
  single-pass form by habit; `vfx run` is the normal operation and the single pass is a
  flag (`--single-pass`) for debugging.

## Validation and review trigger

Accept this ADR when step 1 and step 2 land with: a controller ledger row per dispatch, a
crash fixture that kills the process between the adapter commit and the ledger write
and proves the next run neither repeats nor loses the transaction, a convergence fixture
where an identical finding stops the run, and a cap fixture that stops on a spent
budget. Steps 3 to 5 each add their own fixtures.

Review or reverse this ADR if a dispatched transaction is ever found to have committed
without a verifiable receipt, if convergence fails to stop a repeated finding within one
extra dispatch, or if any adapter gains a second implementation inside the controller.
Implementation starts after `artifacts/room_1046_opening` closes on the current single-pass
operation, so the loop is validated against real stops rather than designed blind.
