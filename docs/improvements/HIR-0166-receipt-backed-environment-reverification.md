---
id: HIR-0166
title: Environment reverification is the first receipt-backed transaction
status: accepted
introduced_in: unreleased
date: 2026-08-31
failure_class: receipt_shaped_claims_could_not_prove_transaction_progress
mechanism: immutable_key_addressed_receipts_and_independent_environment_reverification
adr: null
---

# Environment reverification is the first receipt-backed transaction

## Observed failure

HIR-0164 closed stop classification and defined a receipt schema name, idempotency-key
derivation, and postcondition-evaluation shape. No code could produce or parse that receipt,
persist a phase transition, reconcile an authoritative commit after a crash, or prove that the
receipt reference in an evaluation named real bytes. A fabricated `StopEvidenceRef` therefore
had the same pure shape as a future earned receipt, while none of the seven stop actions was safe
to dispatch.

Strict preflight was the narrowest useful vertical slice. Its failure producer already bound a
secret-free typed result, exact failed checks, a versioned probe surface, and the sole legal
`recover_environment` action. Recovery itself remains external: the harness can safely observe
whether the operator fixed credentials, configuration, or Blender, but must never edit those
things on the operator's behalf.

## Root cause

The action contract and the execution protocol stopped at different boundaries. An action named
an idempotency key and declared a postcondition, but there was no write-ahead state machine or
immutable commit identity between them. Historical environment results also re-derived their
probe specification from current code, so a future probe change could silently reinterpret old
evidence with the same check IDs.

## Decision criteria

- A receipt embeds the complete `StopAction` and recomputes its key from the action's semantic
  identity, exact authoritative-before digest, attempt evidence, and receipt schema. Two source
  runs carrying the same typed evidence bytes therefore converge on one transaction even though
  their evidence locators differ; relocation is not a second side effect.
- Receipt phases are immutable, contiguous revisions: `prepared -> running -> terminal`.
  Running revisions may only append execution identity or spend, or narrow recovery policy.
- The store is shot-state authority, not prior-run output: immutable content-addressed receipt
  files plus one atomic selected pointer under `state/transactions/<key>/`.
- Every selected read verifies canonical bytes, file SHA-256, receipt digest, the complete
  predecessor chain, and parent paths. Missing, corrupt, ambiguous, symlinked, or branched state
  fails closed.
- A transaction publishes `prepared` before its first probe. Concurrent exact calls converge on
  one chain; competing successors do not overwrite one another.
- Environment recovery means repeatable read-only reverification after external repair. A still
  failing probe leaves the receipt `running`; it does not mutate the environment or spend model
  budget. Full success requires every strict check, including every originally failed check, to
  pass under the exact original probe specification.
- `EnvironmentResult` v2 embeds `EnvironmentProbeSpec` v2, including its explicit probe revision,
  and the environment digest binds that specification.
- The environment commit binds the exact action, running receipt revision/digest, failed before
  result and evidence, passing after result and evidence, and changed environment digest. The
  commit is published before the terminal receipt and carries the same idempotency key.
- Postcondition evaluation is independent: it re-reads the terminal chain, result, and commit,
  verifies every join, and cites the exact selected terminal receipt bytes. The executor does not
  self-certify.
- Only `recover_environment` is receipt-capable. The other six typed actions remain proposals,
  not dispatch authority.

## General mechanism

`vfx-harness.transaction-receipt/v1` contains the exact action and derived transaction,
precondition, postcondition, and evaluator identities; authoritative before and attempt digests;
adapter; revision, phase, predecessor; closed recovery disposition; typed operation references;
cumulative integer spend; and terminal result, after-state, result evidence, and commit marker.
A committed terminal receipt requires changed authoritative state and an exact authority-record
marker. Failed or indeterminate receipts cannot claim a commit, and a terminal receipt has no
successor.

The store writes canonical receipt bytes durably, links the immutable content-addressed record,
then atomically selects it with a digest-bearing pointer. A crash before pointer selection leaves
an orphan. The next explicit transaction invocation may select exactly one already-written direct
successor before making a new observation; no action is redispatched and no candidate is
reconstructed from changed external state. Competing, ambiguous, or far-future unselected
branches stop recovery. Read-only key and chain queries never create state.
`transaction_receipt_evidence_ref` returns a reference only while those exact bytes remain the
verified selected head.

`vfx recover-environment <shot> --run-id <id> --idempotency-key <key>` resolves the exact source
run's status-selected envelope and evidence. It refuses implicit latest-run selection, another
action, another key, changed probe identity, or inconsistent failed checks. It publishes
`prepared`, then `running`, and records each distinct secret-free observation. A failed
observation remains safely re-probeable. Before probing, the adapter reconciles one exact direct
receipt orphan, so a pointer-write crash cannot make a later changed observation strand the
transaction. A passing observation publishes an immutable
`EnvironmentRecoveryCommit`, then a committed terminal receipt. If the process dies between
those writes, the next exact invocation reconciles the commit without probing again. A terminal
invocation returns the existing receipt unchanged. A semantically identical stop from another
run reuses that same terminal transaction rather than failing on its run-local locator.

The deterministic evaluator separately resolves the receipt's predecessor chain and the commit's
running receipt, re-parses the after result, invokes the commit's exact-match contract, and only
then publishes `PostconditionEvaluation(result="satisfied")`. A receipt never exempts a later
production run from its own strict preflight.

## Rejected patch-level alternatives

- Accept a receipt-shaped reference without reading bytes: this preserves the original
  self-certification gap.
- Store one mutable receipt file: a crash or second writer could erase the history needed to
  reconcile which side effect happened.
- Treat a new log row or probe call as progress: only a passing changed environment state and its
  commit satisfy the declared postcondition.
- Automatically edit credentials, configuration, or Blender paths: those are external operator
  authority, not harness mutation scope.
- Dispatch replanning, retry, resume, engineering handoff, or human escalation because the generic
  store now exists: none has its required atomic producer, exact commit, and evaluator.
- Build the controller around the one adapter: transaction-local idempotency does not supply the
  controller journal, budgets, or safe actions for the rest of the taxonomy.

## Validation

Pure tests cover strict receipt/result/commit round trips, embedded probe revisions, stale and
unknown fields, illegal phase transitions, key/action substitution, append-only execution
identity, monotone spend and recovery policy, committed/failed/indeterminate invariants, and
factory-level joins across action, running receipt, and before/after results.

Store tests cover read-only absence, canonical pointer and full-chain verification, immutable
terminal replay, missing/skipped phases, pointer and receipt tampering, unique direct crash-orphan
selection, competing and far-future branches, leaf and parent symlink substitution, exact receipt
references, and concurrent identical and competing writers. Adapter tests cover still-failing
then passing recovery, crash after running, crash after an observation record but before pointer
selection, crash after commit, terminal no-reprobe behavior, semantically identical stops from
different runs, concurrent same-key calls, probe-spec change, wrong keys, tampered commits, and
secret exclusion.

## Release and rollback

Environment results use strict v2 migration. A v1 result is historical evidence and is not
silently interpreted as v2; rerun strict preflight to mint current probe identity. Removing the
adapter leaves its immutable receipts and commits as audit state but removes the only
receipt-capable action. It does not authorize falling back to prose, exit codes, mutable receipts,
or automatic recovery.

## Remaining limitations

`recover_environment` requires explicit operator invocation after external repair. HIR-0186
adds the `vfx run` controller for layer-view `publish_validated_amendment`, with its own commit
record, evaluator, ledger rows, and crash fixtures on this receipt store. Retry, revision-checked
replan, engineering route, checkpointed resume, and human decision remain non-dispatchable until
each has its own producer, commit protocol, independent evaluator, and crash fixtures. A safe
receipt store is necessary infrastructure; it does not make an action safe by itself.
