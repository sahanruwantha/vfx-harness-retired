---
id: HIR-0169
title: Fenced work-unit attempts precede paid execution
status: accepted
introduced_in: unreleased
date: 2026-09-01
failure_class: unfenced_read_ready_then_spend_then_transition_race
mechanism: shot_wide_live_fence_and_digest_bound_unit_attempt_claims
adr: null
---

# Fenced work-unit attempts precede paid execution

## Observed failure

The builder selected a dependency-ready work unit from one durable-state read, then could run
due-debt checks, inspect or generate its just-in-time plan, and spend planner-model budget before
it attempted the unit's first durable lifecycle transition. Readiness and transition were
separate unit-state transactions. A second builder could therefore select and pay for the same
unit, while a concurrent replan could invalidate a producer or replace the unit generation
between the readiness read and the late `pending|blocked -> planning -> building` writes.

The late transition checked only the unit id and current lifecycle status. It did not prove the
exact selected plan/JIT heads, layer plan hash, work-unit digest, complete current DAG, eligible
passed-producer set, or the readiness result that authorized the spend. A stale consumer could
therefore attempt to start after the producer evidence on which it depended had been
superseded. A same-id unit, or a status sequence such as
`planning -> retryable -> planning`, could also return to a familiar visible value while naming
a different attempt. Status equality could not detect that ABA.

Per-layer state serialization was necessary but insufficient. Builder execution also uses
shot-root Blender, candidate, script, checkpoint, and journal surfaces. Even two independently
ready units in different layers are not safe concurrent mutation transactions merely because
their lifecycle rows live in different files.

## Root cause

Scheduling eligibility, durable attempt ownership, and live execution exclusion were three
different concerns, but the builder had only the first and part of the second. A read-only ready
set was treated as permission to spend. A generic lifecycle transition was treated as ownership.
Neither carried a monotone attempt identity, and no live shot-wide fence covered the shared
execution context.

The earliest owning defect was therefore the scheduling boundary, not planner quality or retry
policy. The system allowed work to begin before one transaction had revalidated the exact
authority generation and exclusively claimed the unit that would consume it.

## Decision criteria

- No planner-model spend, builder-model spend, or Blender candidate mutation begins before an
  exact work-unit attempt is durably claimed.
- Claiming reloads and validates the complete current WorkUnit DAG, layer plan hash, unit
  digests, passed-producer digests, optional eligible-passed narrowing, and dependency readiness
  under one per-layer exclusive state lock.
- The claim binds the exact revisioned two-head selected-authority token from HIR-0168. A
  semantic or byte-level A -> B -> A selection cannot revive an older attempt.
- Every attempt has a monotone per-unit revision and content-derived identity. A lifecycle status
  is never used as an attempt identity.
- Planning and building are separate phases of the same claim. Building requires the exact
  active planning claim and another readiness/authority validation; it does not infer ownership
  from `status == planning`.
- `retryable` is claimable only into a new planning attempt. It cannot skip ownership and move
  directly to building merely because an earlier plan file exists.
- The shot has one non-blocking live builder fence held across the complete `build_layer`
  invocation, including session startup, unit boundaries, and synthetic composition. Kernel
  release on normal close or process exit does not erase a durable unit claim.
- Lock acquisition order is fixed: shot-wide builder fence, selected-authority shared lock,
  then per-layer unit-state shared or exclusive lock. Inner locks are short: model calls,
  renders, Blender execution, provider calls, and large-file hashing/copying stage outside them,
  then exact pre/post checks or a bounded guarded publication decide whether staged work can bind.
- Retry after a crash is reviewed and explicit. The reviewer proves the prior process is gone,
  disposes of or preserves its outputs according to existing evidence authority, and releases
  or revokes the exact durable claim before a new revision can be minted.
- Replan, supersession, and dependency invalidation revoke and archive any active affected
  claim. A stale holder cannot publish through a later transition with its old token.
- Storage is fail-closed: malformed nested attempt records, substituted fence paths, symlinks,
  non-regular files, stale tokens, and lock-order violations never become retry permission.

## General mechanism

`vfx-harness.work-unit-attempt-claim/v1` is the closed ownership token for one exact unit
generation. It contains:

- `claim_id`, derived from the immutable attempt identity;
- positive monotone `attempt_revision`;
- `run_id`, `layer_id`, and `unit_id`;
- exact `unit_digest` and layer `plan_hash`;
- the complete `vfx-harness.authority-selection-token/v1`;
- closed `phase`, either `planning` or `building`; and
- `claimed_at` and `updated_at` audit timestamps.

Phase promotion changes `phase` and `updated_at` but retains the claim id and attempt revision.
The claim id is derived from schema, attempt revision, run, layer, unit, unit digest, plan hash,
and selection token, so timestamps and the planning-to-building promotion do not manufacture a
second attempt identity.

Each durable unit slot may add `attempt_revision`, one `active_attempt`, and an ordered
`attempt_history`. A present archive row has the closed
`vfx-harness.work-unit-attempt-archive/v1` shape: exact claim, disposition
`completed | released | revoked`, reason, and timestamp. Revisions across archived plus active
claims are unique and gap-free from one through the slot's current attempt revision. An active
planning claim is legal only with lifecycle `planning`; an active building claim owns
`building`, `frozen`, `evaluating`, or `repairing`. Exact-claim comparison, not a caller-provided
claim id alone, guards every attempt-owned mutation.

`claim_ready_unit_for_planning` accepts only a requested `pending`, `blocked`, or `retryable`
unit. While holding the layer state lock it reloads the file, validates the current DAG and every
digest, requires the expected plan hash, derives digest-matched passed producers, applies but
never broadens `eligible_passed`, proves that the named unit is currently ready, increments the
unit's attempt revision, installs the planning claim, and performs the lifecycle transition in
the same write. Failure leaves both state and spend unchanged.

`claim_ready_unit_for_build` accepts only `planning` with the exact active planning claim. It
repeats the authority, DAG, digest, producer, and readiness checks, then promotes that same claim
and revision to `building` in the same state write. It does not adopt an unclaimed planning row,
accept another run's claim, or let `retryable` bypass planning.

`release_unit_attempt` is the conditional exit boundary. It requires the complete current claim,
archives it with a typed disposition and reason, and moves the unit only to the reviewed next
state `retryable`, `blocked`, or `failed`. Successful terminal handling archives the active claim
as completed. Replan, supersession, and dependent blocking archive affected active claims as
revoked before they replace or invalidate those rows. All later attempt-owned transitions and
checkpoint publication require the exact active claim, so a released, revoked, or superseded
caller is stale even if the visible status later returns to planning or building.

The live exclusion boundary combines a normalized-shot-path System V semaphore using
`SEM_UNDO` with descriptor-relative locks on the real shot root and permanent
`state/builder-execution/fence.lock`. The kernel identity survives shot-root or descendant
rename-and-recreate, while the filesystem locks remain inspectable defense in depth. Every path
component is opened without following symlinks. Contention stops immediately and names reviewed
recovery; it does not wait, delete a file, replace an inode, or infer process death from age.
Normal close removes the owned semaphore set and releases file locks; abnormal process exit
releases the semaphore claim through `SEM_UNDO`. The durable attempt deliberately survives
process death, separating proof that no process is live from the authority decision about its
unfinished work.

The CLI performs only read-only environment/authority preparation before the fence. It then
acquires the shot-wide fence before creating the invocation run, initializing the bounded
construction namespace, or starting Blender, and calls an explicitly already-fenced internal
layer entry point. The public `build_layer` entry point retains the same fence boundary for
direct callers, so neither path nests or silently opens a second live fence. The builder retains
that one fence through the whole layer invocation. Inside that fence,
the planning claim is created before just-in-time plan generation. After a valid plan exists,
the builder reopens the exact selected-authority snapshot and promotes the exact claim to
building under the fixed authority-selection-to-unit-state lock order. The fence stays held
through the paid builder and shared shot-root mutation interval. A concurrent replan can revoke
the durable claim; subsequent exact-claim checks then refuse the stale attempt rather than
letting it freeze or accept work under replaced authority.

Every unit-owned external mutation is bound to a `UnitAttemptGuard` containing the exact claim,
unit digest, plan hash, and selected-authority token. Durable ledger saves, candidate-script
promotion, construction-pointer binding, context publication, checkpoint freeze, evaluation,
failure, falsification, and completion all revalidate that identity. Long Blender calls use
exact pre-check → unlocked confined execution → exact post-check; a concurrent replan can commit
while the call runs, and the stale result remains scratch-only. Native SDK `Write`/`Edit` cannot
reach the authoritative unit script: script work is mechanically limited to one claim/run-bound
scratch candidate and a trusted parent performs the guarded atomic promotion.

Canonical replay-script promotion follows the same short-lock boundary. The parent reads,
hashes, copies, and fsyncs the exact run/claim-bound candidate into an unreferenced same-parent
temporary file without holding selection or unit-state locks. The guarded commit only rechecks
the prepared inode and destination-directory identity, atomically renames it, and fsyncs that
directory. Revocation during preparation discards the temporary file and never binds the stale
script path.

Scratch candidate writes and edits use exact pre-check -> unlocked durable scratch mutation ->
exact post-check. Those bytes are never executable authority by location, so revocation may leave
only inert run/claim-bound scratch. Unit context generation similarly performs its potentially
large source reads and generated-context write outside selection/state locks, then rechecks the
claim before any model can consume it and clears the generated context on failure.

`shot.json` publication is a two-transaction compare-and-swap. Preparation holds ledger EX only
while it rereads and merges current rows, writes and fsyncs an unreferenced same-parent inode, and
binds that inode to the exact prior ledger identity plus selected-authority and active-attempt
identity. Commit first holds the shared shot-authority writer fence, then the short attempt guard,
then the ordered real ledger EX lock. It rechecks the live writer, prior target, prepared inode,
parent, and binding, performs the atomic rename plus directory fsync, and reads back the exact
digest. A concurrent ledger writer yields a typed `LedgerSaveConflict`; the stale temp is
discarded and no automatic retry can invisibly reinterpret the owning builder operation. This is
a guarded legacy builder projection, not a strict `shot-ledger/v2` accepted-build root.

Shot-bound Blender workers run under mandatory bubblewrap confinement with a synthetic filesystem
view. The worker receives explicit system-runtime roots and a descriptor-pinned harness runtime;
declared refs, assets, and construction inputs or an exact current-run final-render snapshot are
read-only; and only the exact active run's `scratch/blender` root is writable. Original shot
authority, prior and sibling run scratch, undeclared host paths, and inherited environment
variables are absent. Network/process syscalls are denied through a TSYNC libseccomp filter.
Checkpoints are parent-promoted after read-back; artifact replay also passes closed AST teaching
validation, but the OS boundary—not that denylist—is the authority confinement. Strict preflight
boots a real confined Blender worker and executes its deterministic self-test before paid work.

Generate-construction provider calls, GLB hashing, witness hashing, and content-addressed CAS
staging also run outside selection/state locks. CAS bytes are inert until a short guarded commit
rechecks run, claim, unit, GLB, witness, and prior-pointer identities and publishes the bounded
unit pointer. Reuse follows the same large-read preparation plus short identity bind. A replan
therefore never waits behind Meshy, a render, or a large GLB copy.

Replay inputs are compiled from the selected dependency DAG, not from a directory scan. Each
prefix receipt seals the ordered source scripts plus every typed construction pointer and GLB on
which those scripts depend. The trusted parent opens and verifies those files through held
descriptor-relative roots, captures the exact bytes before worker launch, and exposes GLB bytes
to confined Blender through a held anonymous descriptor. Replay therefore executes the bytes
named by the receipt even if a host path is substituted later; a missing, extra, reordered, or
digest-mismatched dependency fails before execution or payment.

Executable acceptance and judgment debt remain two state machines. A passing evaluator first
stages the canonical verdict rows, ledger outcome, canonical script digest, and candidate digest
outside the selection and unit-state locks. A short exact-attempt commit rechecks their file
identities and publishes one immutable independent-evaluation receipt. State completion consumes
that receipt, verifies it against the frozen checkpoint, and atomically publishes `passed` plus
an immutable completion receipt. Only then may completion-receipt-guarded resolution satisfy
judgment debt. A crash can leave accepted executable work with due debt, and restart reconciles
that exact receipt before scheduling. It never changes passed work back into an unclaimed
planning row.

Standalone `vfx plan --unit` is retired. `vfx plan --layer` materializes/reconciles only; paid
unit planning begins after `vfx build` owns an exact planning claim and continues by promoting
that same claim to building. Legacy recipe distillation queues and direct distiller writers are
inert until a separate receipt-bound staged-diff protocol exists.

## Rejected patch-level alternatives

- Move only `pending | blocked -> planning` before plan generation: this still leaves
  `retryable -> building`, unclaimed planning recovery, producer invalidation, and status ABA
  without ownership. It can also strand a planning status after a crash without identifying who
  owned it.
- Hold only the per-layer state lock while the model runs: this serializes unrelated state writes
  for an unbounded paid interval and still permits another layer to mutate the shared Blender and
  shot-root context.
- Treat `planning` or `building` as an implicit mutex: status has no owner, generation, run, or
  authority token, and can repeat across retries.
- Re-read readiness immediately before the old generic transition: another writer can still
  invalidate it between the read and write, and the transition still cannot fence later spend or
  publication.
- Let retryable units jump directly to building when a plan file exists: file existence is not
  gated authority, and a retry is a new attempt whose producer and selected-head prerequisites
  must be re-earned.
- Delete a fence file after a crash or infer death from age: replacing the inode can split
  cooperating processes across different locks, while elapsed time is not proof that the owning
  process stopped.
- Automatically reclaim a durable claim when the kernel fence is free: process death proves only
  that live execution ended. It does not classify candidate bytes, checkpoints, journals, model
  spend, or whether retry is the legal next action.
- Keep only `run_id` or a random lock owner in the lifecycle row: neither binds the exact unit,
  plan, selected heads, nor monotone attempt generation, so it cannot reject ABA.

## Validation

Acceptance required deterministic fixtures for all of the following:

- two processes and two threads contend for one shot-wide fence; exactly one proceeds and the
  loser fails without waiting or spending;
- independently ready units in different layers still share the same shot fence;
- two contenders target the same pending unit; exactly one planning claim and lifecycle write is
  published;
- a producer is invalidated or replanned while a consumer claim competes: either invalidation
  commits first and readiness fails, or the claim commits first and is deterministically revoked;
- a retryable unit receives a new planning claim and cannot transition directly to building;
- release and re-claim return to the same visible status but increment the attempt revision, and
  the prior claim fails promotion, transition, checkpoint, and completion checks;
- planning promotion retains the same claim identity and revalidates exact authority and
  producers before building;
- selected-authority and same-id WorkUnit A -> B -> A cases reject stale selection tokens,
  plan hashes, and unit digests;
- normal exception unwinding and hard process exit release the kernel fence without replacing
  its permanent inode, while the durable active claim remains;
- reviewed release of the exact orphaned claim permits one new revision; absent, wrong, or
  already archived claims cannot release another attempt;
- replan, supersession, and dependent blocking revoke affected claims and stale holders cannot
  publish later state;
- symlink, directory, FIFO, malformed claim, skipped revision, duplicate identity, and
  non-canonical nested-state substitutions fail closed; and
- a builder integration fixture proves no paid planning call occurs before the planning claim,
  no Blender/build call occurs before build promotion, and every normal failure path archives or
  preserves an exact reviewable claim;
- shot-root, fence-directory, and fence-file rename-and-recreate cannot split the live fence;
- replan commits while a render, canonical-script copy, construction provider, large CAS read,
  or independent-evaluation receipt preparation is blocked, after which the stale attempt refuses
  every publication;
- the CLI acquires exactly one live fence before run creation, construction-namespace setup, and
  Blender session startup, while direct `build_layer` callers retain the same outer protection;
- two ledger writers prepare from one generation, exactly one CAS commit succeeds, the loser
  reports `LedgerSaveConflict` without retry or overwrite, and replan does not wait behind ledger
  merge/temp fsync or scratch/context preparation;
- missing, dangling, symlinked, changed-witness, changed-GLB, and pointer-fsync construction
  fixtures fail closed without external spend or reusable pointer authority;
- a committed falsification remains terminal when its derived JSON projection fails, and both
  stop compilation and `vfx units replan --falsification RECORD_ID` consume authoritative state;
- passed executable work publishes before completion-debt resolution, restart reconciles its
  immutable receipt, and invalidation makes old receipt-bound resolutions inert; and
- standalone unit planning, legacy resume, and both legacy distillation entry points refuse
  before model, Blender, or repository-write spend.

Final deterministic validation completed on 2026-09-01:

```text
.venv/bin/ruff check src
# All checks passed

.venv/bin/python -m pytest -q \
  src/tests/unit/test_generate_construction.py \
  src/tests/unit/test_unit_evaluation_builder.py \
  src/tests/unit/test_judgment_debt_state.py \
  src/tests/unit/test_judgment_debt_builder_scheduling.py \
  src/tests/integration/test_judgment_debt_public_pipeline.py \
  src/tests/unit/test_selected_layer_chain.py \
  src/tests/unit/test_acceptance_stop.py \
  src/tests/unit/test_unit_state_claims.py \
  src/tests/unit/test_unit_completion_guard_scope.py \
  src/tests/unit/test_render_acceptance_gate.py \
  src/tests/architecture/test_staged_architecture.py \
  -k 'not hierarchy_gate_does_not_treat_passed_unit_as_published_layer'
# 185 passed, 1 deselected

.venv/bin/python -m pytest -q \
  src/tests/unit/test_generate_construction.py::test_confined_blender_imports_glb_through_held_memfd
# 1 passed; real confined Blender 5.2 imported the captured GLB through /proc/self/fd/N

.venv/bin/vfx preflight --strict
# ok: true; all five strict checks passed, including the real confined-worker self-test

.venv/bin/python -m pytest -q src/tests
# 1439 passed, 3 known workspace-only failures

git diff --check
# clean
```

The focused and full fixtures cover the complete matrix above, including competing processes and
threads, cross-layer exclusion, retry ABA, producer invalidation, claim revocation, exact
pre/post-spend guards, prepared-publication substitution races, ledger CAS, executable and
judgment-debt receipt ordering, and real confined construction replay. The three full-suite
failures are pre-existing workspace fixtures, not mechanism regressions: `.cursor/rules` exists
despite the one-instruction-authority architecture fixture, and this local checkout omits
`shots/beacon_wake` and `shots/barrel_roll`. Every discoverable independent test passed.

## Release and rollback

The top-level durable unit-state schema remains version 1 for this unreleased change, but the new
attempt fields are an explicit optional extension. Complete absence is legal for historical
rows; once any attempt field is present, the nested v1 contracts, gap-free history, lifecycle
compatibility, and exact current unit/plan identity are mandatory. Malformed or partially
present attempt state is rejected rather than repaired heuristically.

Existing `pending`, `blocked`, and `retryable` rows acquire their first current-schema attempt
through the new claim transaction. Historical unclaimed `planning` or later spend-bearing rows
are not adopted as live work and are not automatically resumed. They require reviewed retirement
or migration based on their exact existing evidence. There is no compatibility path that lets
the public builder use generic unclaimed transitions for new work.

Rolling back only the claim checks would allow already claimed rows to be mutated by stale
callers. Rolling back only the live fence would restore concurrent mutation of shared shot state.
The safe operational fallback is to retain strict parsing and refuse new builder execution while
repairing the mechanism; do not erase attempt history, reset revisions, delete the fence file, or
reinterpret a claim as a checkpoint-resume receipt.

## Remaining limitations

This mechanism prevents duplicate live builder execution and makes one unit attempt's ownership
and generation explicit. It is not an automatic recovery controller. It neither classifies a
crashed attempt as `local_implementation_miss` nor authorizes retry, and it does not add the
phase-specific immutable checkpoint/session receipt required for automatic builder resume.
Reviewed release or revocation remains an operator-owned action until a separately specified
receipt-backed adapter and independent evaluator exist.

The selected plan/JIT heads and durable per-layer unit state are still separate state surfaces.
The fixed lock order prevents a live claim from mixing their generations, but it is not the
commit/reconciliation receipt needed to make authority selection plus `apply_replan` one atomic,
crash-recoverable transaction. That authority-to-state reconciliation protocol is separate next
work and must not be hidden inside this builder fence.

The shot-wide fence intentionally serializes all paid builder execution and Blender mutation for
one shot. It does not claim safe parallel mutation within one Blender scene, safe cross-host
distributed locking, controller dispatch, amendment execution, or automatic session resume.

Synthetic multi-unit composition is currently a separate layer-level transaction under the live
shot fence, not a unit claim and never an accidental continuation of the last unit attempt. A
typed layer-composition attempt/receipt remains required before the authority/state
reconciliation receipt can call this entire layer boundary atomic. That next mechanism is outside
the accepted unit-attempt boundary recorded here.
