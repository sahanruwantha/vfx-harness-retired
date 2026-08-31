---
id: HIR-0168
title: Revisioned authority selection is ABA-safe
status: accepted
introduced_in: unreleased
date: 2026-09-01
failure_class: unversioned_two_head_authority_and_post_gate_recomposition
mechanism: revisioned_two_head_selection_cas_and_snapshot_bound_publication
adr: null
---

# Revisioned authority selection is ABA-safe

## Observed failure

HIR-0167 gave every producer one verified semantic selected-authority assertion, but it
deliberately kept raw pointer observations outside that semantic identity. Its bounded
read/verify/reread protocol could reject an ordinary concurrent change, yet it could not
distinguish an unchanged head from an A → B → A sequence that restored the same pointer bytes.
Plan and JIT publishers also had no shared lock or compare-and-swap token. Two candidates
designed from one selection could therefore both reach their publication boundary, and a stale
writer could replace newer authority after its expensive validation had already completed.

The JIT boundary had a second time-of-check/time-of-use gap. Terminal validation attested the
candidate revision and bundle, but publication could compose the consumer documents again from
whatever selected base existed later. A clean gate result did not identify the exact five
artifact byte streams that publication would select. Repeated per-artifact selection reads in a
consumer could likewise join files from different generations.

Unpublished revert overlays were addressed only by their semantic documents. Equal revert
documents derived from different selection heads could alias one directory and replace its base
metadata. Selecting a revert of the last materialized layer could remove the JIT pointer, turning
a state transition into absence rather than a monotone revision.

## Root cause

Semantic identity and transaction identity were conflated. Equal authority semantics should
remain equal for stop fingerprints and idempotency, but publication needs an exact observation
that changes on every committed head transition, including semantic A → B → A. Neither pointer
carried such a revision, and no shared serialization boundary covered the pair.

Workspace, materialization candidate, finalization, consumer snapshot, and unpublished overlay
contracts also failed to carry one exact two-head base through their full lifetime. Individual
checks could be locally correct while the overall transaction changed its authority base or
proposed bytes between design, gate, and selection.

## Decision criteria

- Plan and JIT pointers have closed v2 schemas with positive monotone revisions. A JIT pointer
  additionally binds the exact plan revision against which its view was produced.
- Plan and JIT heads, consumer-view markers, and materialization finalizations have one
  duplicate-free canonical UTF-8 JSON representation. Semantically equivalent alternative bytes
  are invalid authority rather than a second hash identity for the same record.
- One strict `vfx-harness.authority-selection-token/v1` binds both revisions and both exact raw
  pointer-byte SHA-256 values. Absence is represented only by revision zero plus a null digest.
- Readers hold one shared lock while reading and fully verifying both heads and their selected
  artifacts. Publishers hold the same lock exclusively while comparing and replacing a head.
- A publisher compares both heads even though it mutates only its owned head. It preserves the
  other head byte-for-byte and verifies that postcondition before returning.
- Monotone revisions make semantic A → B → A distinguishable. Restoring earlier semantic content
  never restores an earlier selection token.
- Repeating an identical global bundle and outcome is a true semantic no-op: pointer bytes and
  revision remain unchanged rather than manufacturing progress.
- Repeating an identical materialized JIT view is likewise a true semantic no-op: the existing
  head and revision remain unchanged.
- A JIT view is effective only when both its bundle digest and `plan_revision` match the selected
  plan. A verified old JIT pointer stays inert after global publication, including a semantic
  plan A → B → A.
- Planning workspace, materialization candidate, terminal finalization, consumer snapshot, and
  unpublished overlay contracts bind the exact selection token from which they were derived.
- Materialization finalization binds the exact candidate bytes, base token, proposed canonical
  view digest, every proposed artifact byte digest, and deterministic gate policy. Publication
  cannot substitute a recomposed view.
- Selection replacement is durable: write a unique file in the real parent, flush and `fsync`
  it, atomically rename it, then `fsync` the parent directory. Symlinked, non-regular, escaping,
  or ambiguous storage fails closed.
- A pointer may select an immutable bundle or JIT view only after every member, the immutable
  directory, its rename parent, and every ancestor through the shot root have crossed a durable
  flush barrier. First-use pointer parents are created with the same descriptor-relative,
  no-follow barrier. A retry verifies and reflushes an exact pre-existing orphan before it can
  publish the pointer.
- Revert selection always publishes another JIT pointer revision, including an empty
  materialized-layer set. It never unlinks authority to express a transition.

## General mechanism

`vfx-harness.plan-pointer/v2` adds `revision` to the closed selected-plan pointer.
`vfx-harness.jit-layer-view/v2` adds both `revision` and `plan_revision` to the closed selected
JIT pointer. Version-one pointer shapes are not inferred or upgraded in place.

`vfx-harness.authority-selection-token/v1` is the exact transaction precondition:

- `plan_revision` and `plan_pointer_sha256`;
- `jit_revision` and `jit_pointer_sha256`.

The permanent lock lives at `state/authority-selection/selection.lock`. It is opened without
following symlinks and is never replaced. Selection resolution holds a shared `flock` across
both pointer reads, complete bundle/JIT verification, and construction of the returned snapshot.
Plan publication, JIT publication, and selected revert hold an exclusive lock across exact-token
comparison, owned-head replacement, and postcondition read-back. Each transaction writes one
head while proving that the other did not change, so no two-pointer replacement or transient
mixed pair is needed.

Durable pointer replacement uses descriptor-relative, no-follow filesystem operations. It
validates every existing parent and target, creates a unique same-directory temporary file,
writes canonical bytes, flushes and `fsync`s the file, performs atomic rename, and `fsync`s the
parent directory. A failed compare may leave an immutable candidate bundle or view orphan, but
it cannot select that candidate or damage the previously selected pair.

Content-addressed JIT publication has one immutable view-store transaction shared by ordinary
materialization and revert. It writes the complete member set into a private sibling directory,
flushes every file and the directory, renames to the digest, then flushes the complete ancestor
chain before pointer CAS. Unpublished revert base metadata is part of that same immutable member
set rather than a later side write. If a crash leaves the renamed directory visible before the
ancestor barrier completes, retry verifies its exact membership and bytes and reflushes the
whole tree. Plan and JIT pointer-parent creation uses the descriptor-relative durable directory
primitive, including when a visible directory is a pre-existing crash orphan.

Global planning uses `vfx-harness.plan-workspace/v3`. Workspace creation captures the complete
base selection token along with authored and decision inputs; reopening the workspace preserves
that original base rather than silently rebasing it. Publication freezes and verifies the
candidate bundle, then under the exclusive selection lock requires the exact original token.
It increments only the plan revision and preserves the observed JIT bytes. If content digest and
gate outcome already equal the selected plan, it returns the existing selection without changing
either head.

Preserving an old JIT pointer across global publication is intentional. Its recorded
`plan_revision` no longer equals the new selected plan revision, so the fully verified pointer is
semantically inert. If later global publications restore the same plan content, their higher
revision still prevents the old JIT view from reactivating.

JIT materialization uses `vfx-harness.jit-layer-materialization/v3`. The candidate embeds the
exact two-head base selection. Composition resolves one selected snapshot and derives all five
base artifacts from that snapshot. `vfx-harness.materialization-finalization/v3` then binds:

- the candidate byte revision;
- the exact base selection and bundle;
- the proposed canonical view hash;
- exact serialized hashes for `layers.json`, `scene_checks.json`, `checks.json`,
  `requirements.json`, and `acceptance.json`; and
- the exact full authored/decision input identity and the immutable consumer-marker digest; and
- the closed structural-authority gate policy.

The run-scoped consumer view copies `brief.md`, every reference, amendments, and the complete
resolution ledger into real snapshot files. The marker binds their exact hashes and sizes.
Candidate validation reads the copied resolution ledger, never the live shot ledger, and the
gate rechecks every copied byte before attestation. Finalization holds the candidate lock across
stage, deterministic gate, and attestation. It
deletes any receipt from an earlier attempt before staging, refuses candidate mutation,
staged-artifact mutation, consumer-marker mutation, or selection change during that interval,
and performs the final exact head and live planning-input comparison plus receipt write under
the shared selection lock.
The expensive gate runs outside that lock; a concurrent publication therefore invalidates the
attempt instead of being blocked for the duration of the gate.
Publication reacquires the candidate lock, recomposes under the candidate's exact base, and
requires every attested identity before writing immutable content-addressed view members. Under
the exclusive selection lock it then compares both heads, increments only the JIT revision,
copies the selected plan revision into the new JIT pointer, preserves the plan pointer bytes, and
fully resolves the tentative semantic authority. A failed postcondition restores the exact prior
JIT pointer bytes or exact absence before the exclusive lock is released. The postcondition also
rechecks the exact full planning-input identity, so an append-only decision suffix cannot evade
the plan provenance prefix rule. If the proposed view, materialized-layer set, hashes, plan revision, and
bundle already equal the selected JIT head, publication returns that head without changing its
bytes or revision. Any actual head change invalidates replay of the old finalization token.

Selected-authority resolution returns the verified plan, semantic assertion, exact selection
token, and one immutable artifact-path map. JIT composition and plan consumer preparation use
that single snapshot instead of resolving each artifact independently.
`vfx-harness.plan-consumer-view/v3` records the base token, effective source and digest, all five
overlay artifact hashes, and exact authored/full-decision input identities so downstream gate
readers can identify every planning byte they received.

An unpublished revert carries a strict `.authority-base.json` containing its bundle and exact
selection token. Its store identity hashes the reverted documents, bundle, and token, so
equal-content overlays from semantic A → B → A heads occupy distinct directories and cannot
overwrite each other's base metadata. A selected revert writes a new JIT v2 pointer even when no
materialized layers remain; pointer unlink is not a transition.

## Rejected patch-level alternatives

- Add only pointer-byte hashes to HIR-0167's semantic assertion: pointer formatting and location
  would pollute semantic stop identity, while a restored byte sequence would still permit ABA.
- Lock plan and JIT independently: a reader could still observe one head before and the other
  after a competing transaction.
- Increment a revision without comparing exact pointer bytes: unrecognized or corrupted bytes
  could be overwritten merely because they carried a plausible integer.
- Rewrite both pointers on every transaction: this creates an unnecessary multi-file commit gap.
  One owned-head write plus exact preservation of the other is sufficient under the shared lock.
- Treat a matching bundle digest as sufficient to reactivate JIT after plan A → B → A: the old
  view was not gated against the later plan generation.
- Let publication rerun composition after a clean gate and attest only the candidate: the selected
  base or serialized overlay can change without changing candidate bytes.
- Reuse one equal-content unpublished overlay and replace `.authority-base.json`: mutable base
  metadata destroys the exact precondition needed for later CAS.
- Delete `state/jit-layers/current.json` when the last materialized layer is reverted: absence has
  no successor revision and cannot prove a committed transition.

## Validation

Final deterministic validation completed on 2026-09-01:

```text
.venv/bin/ruff check src
# All checks passed

# Exact authority/materialization/state/final-render regression set
.venv/bin/python -m pytest -q \
  src/tests/unit/test_authority_selection_transaction.py \
  src/tests/unit/test_jit_view_pointer_v2.py \
  src/tests/unit/test_jit_view_store.py \
  src/tests/unit/test_authority_selection.py \
  src/tests/unit/test_plan_authority.py \
  src/tests/unit/test_plan_records.py \
  src/tests/unit/test_plan_consumer_view.py \
  src/tests/unit/test_materialization_stop.py \
  src/tests/unit/test_materialization_snapshot_consumers.py \
  src/tests/unit/test_read_only_snapshot_consumers.py \
  src/tests/unit/test_jit_acceptance_selection.py \
  src/tests/unit/test_unit_state_transactions.py \
  src/tests/unit/test_unit_admin.py \
  src/tests/unit/test_work_unit_plan_transaction.py \
  src/tests/unit/test_final_render_snapshot.py \
  src/tests/unit/test_render_acceptance_gate.py \
  src/tests/integration/test_materialization_selection_race.py \
  src/tests/integration/test_lifecycle_fixture.py
# 389 passed

.venv/bin/python -m pytest -q
# 1217 passed, 3 known workspace-only failures

git diff --check
# clean
```

The focused fixtures cover strict schema migration, revision/digest combinations, lock
serialization and filesystem substitution, durable write ordering, semantic no-op, competing
same-base workspaces, stale JIT inertness, semantic plan ABA, exact finalization, selection
change after gate, cumulative materialization publication, snapshot consumers, selected and
unselected revert behavior, crash-after-view-rename recovery, first-use pointer-parent ordering,
equal-content overlays with different base tokens, immutable gate inputs, decision append races,
tentative-head rollback, serialized unit-state transactions, and final-render attribution.

The three full-suite failures are pre-existing workspace fixtures, not mechanism regressions:
`.cursor/rules` exists despite the one-instruction-authority architecture fixture, and the local
checkout omits `shots/beacon_wake` and `shots/barrel_roll`. All 1217 discoverable independent
tests passed; the exact 389-test regression set is green.

## Release and rollback

Plan pointer v1, JIT pointer v1, plan workspace v2, materialization candidate v2,
materialization finalization v1/v2, and plan consumer view v1/v2 are obsolete and strictly rejected.
Because a v1 selected head cannot supply a valid CAS base, the new publisher does not overwrite
or silently "republish over" it. This is an unreleased strict cutover: fixtures and disposable
shots recreate authority through the current public boundaries, while any durable historical
shot requires an explicit reviewed migration or retirement before execution. Readers and writers
never synthesize revisions or tokens for historical bytes.

Rolling back only the readers would make current v2 authority unreadable. Rolling back only the
writers would restore unversioned selection and permit stale publication. The safe fallback is
to retain strict resolution and disable new publication while repairing the current mechanism;
there is no compatibility mode that treats revisionless authority as current.

## Remaining limitations

The selected authority pair is now serializable and ABA-safe, but selecting a plan or JIT head
and moving durable work-unit state are not one atomic transaction. Materialization publication
and revision-checked unit-state reconciliation still have a crash boundary, and the shared
selection lock is not a receipt or recovery journal for that second state surface. Closing this
gap requires one explicit commit/reconciliation protocol rather than broadening the pointer lock.

This mechanism does not execute an amendment. No public amendment adapter binds one already
authored successor, exact candidate, gate attestation, finding consumption, authority selection,
unit-state move, immutable commit, transaction receipt, and independent evaluator. There is also
no dispatch controller, persistent controller journal, or automatic loop. HIR-0166's explicitly
invoked environment recovery remains the only receipt-backed transaction.

Cryptographic authenticity, signatures, and hard-link substitution defense remain separate
concerns. The lock and durable replacement mechanisms establish local filesystem transaction
identity and crash durability; they do not claim trust against a privileged hostile operator.
