---
id: HIR-0164
title: Closed typed stop envelopes precede recovery dispatch
status: accepted
introduced_in: unreleased
date: 2026-08-31
failure_class: untyped_terminal_state_authorized_repeated_recovery
mechanism: content_addressed_stop_envelope_and_transaction_progress_contracts
adr: null
---

# Closed typed stop envelopes precede recovery dispatch

## Observed failure

The HIR-0163 exposing run reached a useful camera checkpoint with qualitative debt
that was not yet payable. Runtime flattened that boundary into the same
`contract_gap` channel used for defects, then proposed global replanning even though
neither plan authority nor the accepted artifact had changed. Other stage exits likewise
exposed an integer and human-readable detail without a closed record proving which
authority failed, which evidence classified the failure, or what state a legal recovery
must change. An unchanged finding could therefore consume another cycle without any
machine-checkable progress.

## Root cause

Terminal reporting and recovery authority were the same informal channel. Exit codes,
exception types, and prose described symptoms but did not bind an exact selected bundle,
view, layer, unit, candidate, checkpoint, settings generation, or evidence record. A
purported next action named neither resolvable preconditions nor a typed domain
postcondition, so a controller could not distinguish a valid retry from an authority
change, environment recovery, human decision, or harness defect. It also could not prove
that a prior action committed progress before seeing the same cause again.

## Decision criteria

- Terminal dispatch authority is a strict, content-addressed record. Exit codes and
  human-readable detail remain diagnostics only.
- `evidence_not_due` is successful continuation, not a stop class.
- Stop classification is closed to `local_implementation_miss`, `authority_defect`,
  `harness_defect`, `infrastructure_failure`, and `human_decision_required`.
- Each stop contains exactly one typed transaction. Digests authenticate complete
  payloads; an opaque digest never stands in for a target, precondition, or
  postcondition.
- A legal action binds its exact evidence and before-state, declares one evaluator and
  receipt schema, and names the authoritative domain state that must change.
- Future repeated-fingerprint routing must be receipt-backed. A caller-supplied Boolean or
  another occurrence of the same finding is not evidence that the previous action progressed;
  the current classifier detects the repetition and refuses to route it.
- No action may imply `--force`, skipped gates, widened mutation authority, informal plan
  edits, discarded accepted work, or safe session resume without its full typed state.
- A dispatcher does not ship until every advertised automatic transaction has an atomic
  producer, immutable receipt, postcondition evaluator, and crash reconciliation path.

## General mechanism

`vfx-harness.stop-envelope/v1` seals the stage and stop class; exact run, authority,
layer/unit, candidate, checkpoint, settings, and debt identities where applicable; a
stable causal fingerprint; attempt, classification, artifact-state, and authoritative
before-state digests; exact evidence references; budget identity; and one
`vfx-harness.stop-action/v1`.

The action vocabulary is closed to `retry_exact_unit`,
`publish_validated_amendment`, `apply_revision_checked_replan`, `route_engineering`,
`recover_environment`, `resume_checkpointed_session`, and `escalate_question`. The target
type derives its dispatch mode (`automatic`, `external_recovery`, `human_handoff`, or
`terminal_route`) and its state assertions. The matching postcondition type is likewise
closed: selected-authority amendment committed, revision-checked replan committed, exact
unit attempt advanced, environment reverified, checkpointed session advanced, human
decision committed, or engineering route committed. Target and postcondition identities
must agree structurally.

Every dispatchable execution must produce `vfx-harness.transaction-receipt/v1`.
The evaluation contract binds an exact-schema, content-addressed receipt reference,
action, evaluator, authoritative before/after digests, and an idempotency key derived from the
action, before-state, attempt evidence, and receipt schema. A satisfied evaluation must claim
changed authoritative domain state. HIR-0166 implements the strict parser/store and the first
producer/evaluator for `recover_environment`; no other transaction inherits that capability by
proximity. The domain helper
defines a future `repeated-dispatch-defect/v1` payload binding the full stable current stop,
prior attempt, evaluation, and its one exact-schema receipt reference. Because no verified
receipt/defect consumer exists, the classifier currently stops an unchanged repetition by
refusing to route it; it does not manufacture an engineering envelope from unverified refs.

The common run publisher writes one immutable `reports/stop-envelope.json`, reads it back,
and makes `status.json` select it by exact digest. The run driver consumes that selected
child envelope, not the child exit digit or status detail. Strict producers at preflight,
planning/plan gate, materialization, hypothesis falsification, and finished-chain
acceptance bind the evidence they actually read. An unclassified terminal exception may
fail closed as `harness_defect`; that generic envelope never invents local retry,
authority-repair, or resume permission.

## Rejected patch-level alternatives

- Rename `contract_gap` or add more exit codes: neither binds authority, evidence, or a
  legal state transition.
- Map exception strings to recovery actions: prose is unstable and cannot prove exact
  ownership or preconditions.
- Build the controller first: it would automate the existing replan loop before progress
  could be evaluated.
- Store only precondition and postcondition digests: an executor cannot resolve or validate
  the payload those digests allegedly authenticate.
- Accept a caller-supplied `progress=True`: the transaction must earn progress through a
  receipt and an independent domain evaluator.
- Treat `evidence_not_due` as retryable failure: the passed executable checkpoint should be
  preserved while the DAG advances to the compiled provider.
- Turn every builder truncation into resume or local retry: budget exhaustion, provider
  failure, invalid authority, and missing durable session state are different causes.

## Validation

Pure-domain tests cover strict round trips, stale and unknown fields, the stop-class /
stage / transaction matrix, exact unit-identity closure, target-to-postcondition binding,
evidence and state assertions, derived dispatch modes, receipt-bound postcondition
evaluation schema, exact-one receipt-reference validation, idempotency, `EvidenceNotDue`, and
fail-closed unchanged-fingerprint detection pending a verified routing boundary. Run
artifact tests cover immutable publication, status-pointer digest verification, run-id
binding, and stable generic defect identity across restarts until authoritative input
changes. Focused producer tests cover strict preflight, rejected global planning,
materialization failure, builder hypothesis falsification, composition-stage attribution,
and failed full acceptance.

These are contract and boundary fixtures. No recovery controller, automatic transaction,
or chamber run was used to claim this HIR.

## Release and rollback

Terminal machine consumers must resolve the selected v1 envelope and fail closed on an
unknown schema, stale digest, mismatched run, malformed evidence, or illegal
class/stage/action combination. Legacy exit codes and `status.json.detail` remain useful
for operators but do not regain dispatch authority. Rollback removes the new machine
authority and therefore also removes any basis for automatic dispatch; it must not be
paired with an informal controller.

## Remaining limitations

The typed action, target, state-assertion, postcondition, evaluation, idempotency, and
fail-closed loop-detection scaffolding is implemented. HIR-0166 adds a durable receipt store,
commit reconciliation, and explicit public execution for `recover_environment` only. A recovery
controller is not implemented. There is no controller journal, automatic dispatch, safe
resume-record producer, or producer that can yet prove `local_implementation_miss`. The other
six transaction types remain non-dispatchable even when an envelope names one; their typed action
is a closed proposal rather than evidence that a transaction ran.

HIR-0163 supplies the separate non-stop `EvidenceNotDue` classifier value. Runtime carries
that successful continuation through debt state and ordinary scheduling; no run summary or
status emits `EvidenceNotDue` yet. HIR-0165 seals finished-chain acceptance before
deliverable rendering; it does not add a recovery dispatcher.
