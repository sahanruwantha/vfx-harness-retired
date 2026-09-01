---
id: HIR-0171
title: Atomic authority-state transitions preserve proven work
status: proposed
introduced_in: unreleased
date: 2026-09-01
failure_class: selected_view_transition_stranded_or_revoked_unchanged_terminal_receipts
mechanism: wal_bound_authority_state_transition_and_immediate_predecessor_receipt_adoption
adr: null
---

# Atomic authority-state transitions preserve proven work

## Observed failure

The public JIT pipeline exposes the boundary left open by HIR-0168 and HIR-0170.
Layer 1 can materialize, complete its units, and publish a receipt-backed terminal
finalization under JIT revision 1. Materializing Layer 2 then publishes JIT revision 2.
The Layer 1 row, unit DAG, scripts, checkpoints, replay evidence, and terminal decision may
all be unchanged, but Layer 2 cannot consume that proof.

The failure is deterministic:

1. `publish_materialization` installs an immutable cumulative view and replaces
   `state/jit-layers/current.json` while holding the authority-selection lock.
2. Work-unit state is not part of that publication. The planner later calls
   `_reconcile_materialized_layer_state` or `apply_replan` in a separate transaction.
3. Unit-attempt and layer-finalization claims bind the complete HIR-0168 selection token.
   Their durable state also binds `plan_hash`, currently the SHA-256 of the complete selected
   `layers.json` rather than the semantic identity of one layer or unit.
4. Layer 2 materialization changes both the JIT token and the combined `layers.json` hash,
   even when Layer 1 is byte-identical.
5. `terminal_layer_finalization_guard` correctly refuses Layer 1's revision-1 claim when it
   is presented directly as revision-2 authority.
6. Running the existing plan-identity adoption afterward is not preservation.
   `apply_replan` archives current layer finalization unconditionally and, when the combined
   plan hash changed, revokes every completion receipt and reopens passed units.

The same split leaves two crash states. A process can die after selecting the new plan/JIT
head but before moving durable state, or after moving one state record but before the selected
head and the remaining records agree. HIR-0133 makes one target layer recoverable after the
first form of crash, but it does not make the transition atomic and it cannot authorize an
unchanged predecessor receipt under the new selection.

The materialization gate also sees an incomplete projection. Candidate preview currently
copies and replans only the target layer's work-unit state. It does not project preservation,
revocation, or cross-layer invalidation for every state record affected by the proposed view.
The gate can therefore attest a view without attesting the exact state transition required to
make that view executable.

## Root cause

Selected authority and durable execution state have different publication owners and no shared
commit receipt. HIR-0168 makes plan/JIT pointer selection serializable and ABA-safe. HIR-0170
makes one fixed layer generation's finalization claimed and receipt-bound. Neither mechanism
states how an accepted terminal receipt moves from one selected generation to the next.

Three identities are currently conflated:

- the exact selection under which paid work executed;
- the semantic layer/unit authority that work proved; and
- the current selected generation allowed to consume that proof.

The execution token must remain immutable or an in-flight result could publish after its
authority changed. The semantic identity must be narrower than the whole cumulative view or a
sibling materialization destroys unrelated proof. Current authorization must be a separately
audited transition, because simply ignoring the old execution token would allow stale work to
revive after an A -> B -> A sequence.

Classification: an authority/provenance and transaction-boundary defect. It is not a Blender,
critic, retry, or planner-intelligence problem. The earliest owning fix is a single
authority-state publication protocol shared by plan and JIT producers.

## Decision criteria

- Plan/JIT selection and every affected per-layer authority binding form one logically atomic,
  crash-reconcilable transaction.
- The expensive plan/materialization gate attests the exact proposed state transition, not only
  the proposed authority documents.
- Immutable unit-completion and layer-finalization receipts are never rewritten or re-minted to
  make them appear current. Their original execution selection remains historical truth.
- A completed unit receipt becomes current in a successor selection only through an immutable
  immediate-predecessor transition effect preserving that exact unit binding and source closure.
  The containing layer may change because a sibling unit changed; the unit receipt does not.
- A terminal layer receipt becomes current in a successor selection only when the complete layer
  binding is unchanged, including every constituent unit receipt and predecessor terminal
  binding. Unit-level preservation must never be mistaken for layer-level preservation.
- Active unit attempts and active layer-finalization claims are never adopted across a selection
  change. They are revoked; their post-checks cannot publish.
- Unchanged units retain status, checkpoint, completion receipt, attempt lineage, and evidence.
  Unchanged layers retain their terminal finalization receipt and its outcome/ledger projections.
- Changed, removed, added, or incomparable units invalidate their replacement unit-DAG closure.
  A changed layer or predecessor finalization invalidates the dependent sparse-layer closure;
  independent branches remain intact.
- A receipt absent from the immediately preceding current binding cannot be recovered from
  history merely because later semantic bytes resemble an earlier generation. A -> B -> A never
  revives proof invalidated by B.
- A durable write-ahead intent exists before any selected pointer or live state mutation. While
  an intent is pending, every authority-bearing reader fails closed.
- Crash recovery performs exact deterministic roll-forward from already staged bytes. It never
  reruns a planner, gate, Blender, render, critic, or judgment.
- The commit is independently evaluated before it becomes readable current authority.
- Unknown fields, unsupported schemas, unclassifiable authority rows, incomparable digest
  generations, unsafe paths, or a live member matching neither recorded side stop the
  transaction without guessing.
- A true semantic no-op changes neither selection revision nor authority-state revision.

## Pre-change boundary audit

Before this mechanism, the implementation divided the intended transaction across these owners:

- `orchestration/plan_authority.py::publish_current` freezes and selects a plan pointer but does
  not reconcile any layer state.
- `orchestration/jit_materialization/publish.py::finalize_materialization_candidate` binds the
  base token, candidate revision, proposed view and artifact hashes, planning inputs, and gate
  result. It does not bind a live-state before image or proposed state diff.
- `orchestration/jit_materialization/publish.py::publish_materialization` installs the immutable
  view and replaces only the JIT pointer.
- `orchestration/jit_materialization/candidate_preview.py` projects only the target layer's
  `apply_replan` result into scratch.
- `agents/planner/generate.py::_reconcile_materialized_layer_state` and
  `agents/planner/rematerialize.py::_rematerialize_layer` move state after selection.
- `orchestration/unit_state.py::{initialize,apply_replan}` use the complete `layers.json` hash as
  plan identity. `apply_replan` cannot distinguish a sibling-view adoption from invalidating
  layer authority.
- `domain/unit_attempts.py` and `domain/unit_completion_receipts.py` carry the execution token
  inside accepted evidence.
- `domain/layer_finalization_claims.py` and
  `orchestration/layer_finalization_state.py` likewise carry and directly compare the original
  selection token.
- `orchestration/layer_publication.py` correctly funnels consumers through one verifier, but the
  verifier has no adoption lineage to prove that an older execution remains semantically
  current.

HIR-0171 changes those ownership boundaries rather than adding another post-publication repair.

## General mechanism

### Execution identity and current authorization are separate

Strict unit-attempt and unit-completion records plus HIR-0170's
`vfx-harness.layer-replay-receipt/v2`, `vfx-harness.layer-evaluation-receipt/v1`, and
`vfx-harness.layer-finalization-receipt/v2` remain immutable execution evidence. Their selection
token records the exact plan/JIT heads under which the work ran, and their `plan_hash` field now
receives the semantic layer-capsule digest from authority-bound state rather than the complete
cumulative `layers.json` digest. No record is rewritten or re-minted when a sibling view changes.

Current authorization is external to those receipts. Each evaluated authority-state head binds
the live selection to a `vfx-harness.layer-authority-binding/v1` for every live layer. The
binding names the layer and unit capsule digests, exact authorized completion receipts,
predecessor layer/finalization bindings, and optional authorized terminal finalization receipt.
A transition effect may carry the exact same unit receipt from its immediate predecessor only
when the before and after bindings preserve that unit's capsule, digest, receipt, and complete
source closure. This rule is deliberately narrower than layer equality: a changed layer can
preserve an unchanged unit while reopening changed siblings and revoking the old terminal layer
receipt. Carrying a terminal layer receipt requires the complete layer capsule, constituent
receipt set, predecessor terminal bindings, and layer source closure to remain unchanged.

An active claim remains valid only against its exact execution token and is revoked on any
selection change. An immutable unit receipt can be consumed under a later token only after the
shared lineage verifier proves a contiguous sequence of independently evaluated edges, each of
which preserves its exact unit binding. Those edges may change the containing layer through
other units. A terminal layer receipt requires a contiguous sequence of edges that preserve the
complete layer binding. Thus existing schemas retain one meaning: the record says where work
executed; coordinator lineage says whether the current generation is authorized to consume it.

### Schema-closed authority capsules

A dependency-free compiler produces
`vfx-harness.authority-capsule-set/v1` from one fully verified selected or proposed consumer
view. It records provenance for the complete bundle/view, then publishes semantic capsules whose
digests deliberately exclude unrelated sibling bytes.

Each unit capsule contains:

- the exact WorkUnit authority row;
- all referenced scene, image, semantic-diff, qualification, construction, and interface rows;
- every requirement and claim binding consumed by that unit;
- applicable durable shot-wide constraints; and
- exact producer/interface bindings on which the unit depends.

Each layer capsule contains:

- its exact sparse global row and effective materialized row;
- its stable dependency declarations and judge/ownership policy;
- its ordered unit-capsule digests;
- layer-owned evidence and exported interface authority;
- its judgment-debt definitions; and
- predecessor layer-generation bindings.

The dependency-free capsule compiler owns a closed, schema-specific projection for every
supported plan artifact. The projection is semantic, not a scan for familiar field names.
Adding or changing an authority field without updating the compiler's explicit field closure
makes publication fail closed. Moving those projections into their individual domain schemas
would be a future ownership refinement; this record does not claim that boundary exists today.

Debt activation demonstrates why generic owner-field fan-out is wrong. A definition belongs to
its semantic/fault owner and changes that owner's capsule. An activation compiled for a later
payer changes the payer's capsule; it does not invalidate an earlier camera finalization that
correctly carried the definition as `pending_not_due`. Final acceptance still refuses until the
payer resolves persistent debt.

### Durable layer-state binding

The strict work-unit state document remains schema 1 with digest schema 4. Its `plan_hash` is
the selected semantic layer-capsule digest, and its unit slots remain the durable lifecycle
source used by existing state mutations. The complete-view `layers.json` hash is no longer an
acceptance identity.

The atomic coordinator stages the exact state bytes as an
`vfx-harness.authority-state-member-image/v1` and binds them externally with
`vfx-harness.layer-authority-binding/v1`. The binding contains:

- current authority-state transition revision and proposal digest;
- current exact selection token;
- layer id and layer-generation digest;
- sorted unit ids and unit-generation digests;
- exact predecessor layer-generation/finalization bindings;
- exact currently authorized unit-completion receipt digests;
- an optional currently authorized layer-finalization receipt digest; and
- a semantic binding digest.

The member image additionally binds the exact live locator, staged byte SHA-256, and state
revision. This keeps the existing lifecycle record focused while making selection, semantic
identity, accepted receipts, and exact state bytes one evaluated transition member.

Ordinary unit-state mutations retain the selected semantic capsule identity. Active attempt
guards require the current execution selection. Completed-receipt and finalization guards use
the external current binding and, for historical tokens, its verified preservation lineage;
they do not pretend the historical execution token was minted in the successor generation.

### Transition records

The durable store lives below `state/authority-state/` and has one permanent lock lineage shared
with the existing authority-selection lock.

`vfx-harness.authority-state-head/v1`, selected by
`state/authority-state/current.json`, contains exactly:

- positive monotone transition revision;
- predecessor head digest, or null only for genesis;
- exact selected plan/JIT token;
- commit receipt locator, file SHA-256, and semantic digest; and
- independent evaluation locator, file SHA-256, and semantic digest.

`vfx-harness.authority-state-transition-intent/v1` is the write-ahead source of truth. Its
identity includes:

- transaction id, proposed transition revision, and predecessor head;
- exact before selection token;
- exact proposed plan and JIT pointer bytes/hashes and resulting token;
- producing plan-gate or materialization-finalization reference;
- capsule-set locator, SHA-256, and digest;
- for every existing or proposed layer state: before locator/hash/revision/binding and exact
  proposed locator/hash/revision/binding;
- sorted per-layer preservation and invalidation effects;
- preserved completion and terminal receipt digests;
- invalidation seeds plus intra-layer and cross-layer downstream closures; and
- a digest of every immutable staged member.

Audit time does not participate in transaction semantic identity.

`vfx-harness.authority-state-transition-commit/v1` binds:

- exact intent locator, file SHA-256, and digest;
- predecessor and successor head revisions/digests;
- observed exact after-selection token;
- every installed state hash/revision/binding;
- the complete effect digest; and
- commit time outside semantic identity.

`vfx-harness.authority-state-transition-evaluation/v1` is produced by a separate deterministic
evaluator. It reopens the intent, commit, selected heads, capsules, and installed state, verifies
every join, and publishes a closed `satisfied | failed` result. The executor's own commit record
cannot self-certify.

`state/authority-state/pending.json` is a canonical pointer to exactly one durable intent. Its
presence means no current authority-state pair is consumable, even if individual files happen to
look mutually consistent.

### Deterministic preservation and invalidation

The transition compiler compares the immediate committed predecessor with the proposed capsule
set and derives one closed effect record.

For each layer:

1. Revoke every active unit attempt and active layer-finalization claim on any selection change.
   Prepared artifacts remain inert and cannot pass their old post-check.
2. Seed unit invalidation with added, removed, changed, or digest-incomparable units.
3. Expand seeds through the replacement same-layer unit DAG.
4. Preserve a completed unit only when its unit capsule is identical, every required producer is
   preserved, its completion receipt was authorized by the immediate predecessor binding, and
   its complete script/evaluator/replay source closure still verifies. The containing layer may
   otherwise change; the transition effect must name the exact same unit binding on both sides.
5. Preserve a terminal layer finalization only when its layer capsule is identical, every
   constituent completion receipt is preserved unchanged, every predecessor terminal receipt is
   preserved unchanged, and its composed script, external evaluation receipt, complete ordered
   v2 replay-receipt prefix, replay inputs and dependencies, reference/render/auxiliary captures,
   outcome, and ledger source closure still verify.
6. A changed or removed layer, invalidated terminal receipt, or changed predecessor terminal
   binding seeds the dependent sparse-layer closure. Initially that conservatively invalidates
   every unit and finalization in a descendant layer. Independent DAG branches remain eligible
   for preservation.
7. Preserve non-terminal lifecycle state only when its unit capsule is identical; any active
   ownership token is still revoked. Changed identities start from `pending` with their prior
   slots retained in supersession history.
8. Retain monotone unit-attempt and layer-finalization revision lineages. Reusing a visible id or
   status never reuses an earlier ownership generation.

Adoption is immediate-predecessor only. The new binding may name a receipt only when the prior
binding named that exact receipt and the transition effect proves equality. Historical receipt
or archive searches are forbidden. Therefore semantic A -> B -> A produces three different
transition revisions, and a receipt invalidated by B is absent from the only legal source for
the later A transition.

### Plan and JIT publication own the same transaction

Materialization finalization advances to a strict schema that additionally binds:

- the exact live-state before map;
- proposed capsule-set digest;
- proposed authority-state transition intent digest; and
- deterministic preservation/invalidation effects.

Candidate preview snapshots every affected layer state and the authority-state head into its
isolated consumer view. The plan gate therefore evaluates the exact post-publication authority
and state projection. It independently recompiles the effects and successor state from the live
predecessor capsules, current coordinator bindings, and candidate capsules; prepared effects,
member bytes, or bindings are evidence to compare, never an authorization source. The verifier
also reopens every JIT manifest target, staged immutable member, and producer record before it
mints the distinct preview authorization. A target-only scratch `apply_replan` is insufficient
and is retired.

`publish_materialization` becomes the sole owner of JIT selection plus state movement. The
post-publication `_reconcile_materialized_layer_state` and rematerialization `apply_replan` paths
are removed. A normal first materialization, rematerialization, and crash recovery all pass
through the same commit.

Global plan publication uses the same protocol. Selecting a sparse global generation does not
silently keep an old JIT materialization current. A prior state may be preserved only when the
new proposal contains complete comparable capsules and a validated current materialized
authority for it; otherwise it becomes dormant or is invalidated by the recorded effect. A later
materialization cannot revive a dormant historical receipt except through the same
immediate-predecessor rules.

An identical plan or JIT proposal remains a semantic no-op. It publishes no intent, changes no
head, and adopts nothing because current authority already matches.

### Lock order and bounded commit

The existing authority-selection lock remains the outer serialization boundary. The order is:

1. producing workspace or materialization-candidate lock;
2. authority-selection lock, exclusive for a transition;
3. every existing and proposed per-layer state lock, exclusive and sorted by canonical path; and
4. descriptor-relative immutable-store and same-parent replacement operations.

Every ordinary state mutation must acquire authority-selection shared before its layer-state
lock. This makes state-file enumeration stable while a transition holds selection exclusive.
Unknown state files or state files for unknown layers fail closed; they are not ignored by
directory naming convention.

No model, Blender, render, critic, large evidence read, or gate runs while these locks are held.
The producer prepares and hashes external sources first. Under the locks it performs only exact
re-read, pure diff, small durable writes, and postverification.

Commit ordering is:

1. re-read exact heads, authority-state head, and every affected state;
2. recompute the capsule set and transition; require the gate-attested proposal exactly;
3. durably install all immutable proposed state members, pointer bytes, capsules, intent, and
   supporting records;
4. atomically select `pending.json` before changing any live member;
5. replace proposed live layer-state files;
6. replace the plan and/or JIT pointer using the HIR-0168 exact-byte CAS;
7. publish the immutable commit receipt;
8. run and publish independent deterministic evaluation;
9. replace `state/authority-state/current.json` with the evaluated successor head;
10. durably remove `pending.json`; and
11. release locks.

The last durable removal is the logical visibility point. During an ordinary commit readers wait
on the selection lock. After a process death they see pending and refuse. They can never accept a
mixed pair as current.

### Crash recovery

Once pending is durable, the transition is fully authorized and every target byte is already in
immutable storage. Recovery acquires the same locks and rolls forward; it does not reconstruct a
candidate from mutable state.

Whole-file state replacement prepares bytes only under the closed shot-local
`state/work-unit-state-staging/` namespace on the same filesystem, then atomically renames them
into `state/work-units/`. Prepared names never coexist in the authoritative live-member
namespace. Descriptor identity, source-name CAS, destination identity, and both directory fsyncs
close the rename. A process death before replacement may leave an inert staging orphan; exact
live-member enumeration and the next state write ignore it without weakening validation.

For every live pointer and state member, recovery accepts exactly two identities: the intent's
recorded before bytes or its recorded after bytes. It installs missing after bytes, verifies the
target selection and binding set, publishes any missing commit/evaluation/head records, and then
removes pending.

If a member matches neither side, an immutable target is missing or corrupt, the pending pointer
is ambiguous, the predecessor head is not exact, or independent evaluation fails, recovery stops
as `harness_defect` and leaves pending selected. It does not roll a plausible subset backward,
choose a recent file, widen authority, or rerun paid work.

The supported operator boundary is `vfx recover-authority-state <shot>`. It invokes only this
deterministic roll-forward protocol and emits one strict
`vfx-harness.authority-state-recovery-result/v1` joining the transition intent, coordinator head,
selection token, revision, and affected state-member ids. When no WAL is pending, the command
re-verifies the current evaluated head and returns `already_current` without changing state.
Planner, Blender, render, critic, judgment, and model entry points are outside this boundary.

`already_current` is not whole-state SHA equality with the commit image: ordinary lifecycle
progress may legally change those bytes. It requires the exact live layer-id namespace, strict
current-schema state, the current layer/unit/predecessor capsule generations and coordinator
binding, current active-claim selection, and source-verified completion/finalization lineage.
Missing, extra, or generation-substituted state therefore fails while legal attempt and receipt
progress remains recoverable as a no-op.

The implemented live-member resolver is shared by pre-WAL validation, post-install validation,
independent evaluation, and recovery. It descriptor-reads the exact coordinator-declared live
namespace; an extra live state file, missing member, capsule/plan/unit substitution, symlink, or
parent-directory replacement cannot be accepted by a looser recovery-only path. Whole-file state
bytes are prepared only under sibling `state/work-unit-state-staging/`, never under the live
`state/work-units/` namespace, so a process death before atomic replacement leaves an inert
staging orphan rather than an ambiguous live member.

Focused recovery fixtures exercise strict `already_current` validation, before/after live-member
roll-forward, hostile malformed/conflicting pending state, missing staged objects, corrupt commit
or selected head, symlinked members and pointers, parent substitution, and an actual subprocess
death before state replacement. Those results establish the implemented WAL/live-state
boundaries; they do not establish process-death recovery at every commit step. The complete
subprocess crash-boundary matrix below, full suite, and confined real replay remain required, so
this record stays proposed.

A crash before pending selection leaves only unselected immutable orphans and changes no live
authority. Content-addressed garbage collection is separate from recovery.

### Receipt-lineage verification

Unit consumers do not treat a durable `passed` row or source-valid completion receipt as current
authorization by itself. `authorize_completed_units_for_layer` snapshots the selected coordinator
head and produces an exact typed, state-bound authorization. For a historical execution token it delegates to
`require_preserved_unit_completion_authorization`, which walks every contiguous predecessor
effect and requires that edge's `preserved_units` row to name the exact same unit capsule,
generation, and receipt. Successor scheduling, predecessor-interface exposure, replay-prefix
construction, and finalization inputs retain that typed authorization rather than erasing it into
a caller-supplied receipt map. The authorization binds the exact selection, coordinator head,
semantic layer generation, and completion-relevant state projection. Candidate preview uses a
different typed authorization bound to its content-addressed preview reference; live claim APIs
reject that preview type.

`require_current_layer_publication` remains the singular downstream boundary. It adds these
checks before accepting the HIR-0170 terminal receipt:

1. no pending authority-state transition exists;
2. the selected authority-state head's token exactly equals the live plan/JIT heads;
3. the independent transition evaluation is `satisfied` and names those exact bytes;
4. current layer and unit capsules recomputed from selected authority equal the durable binding;
5. the current binding names the exact unit completion and terminal finalization receipts;
6. the contiguous immediate-predecessor adoption chain reaches each receipt's immutable
   execution head: every unit-receipt edge preserves the exact unit binding, while every terminal
   layer-receipt edge preserves the complete layer binding;
7. unit scripts and evaluator receipts plus the terminal's external evaluation receipt, complete
   ordered v2 replay-receipt prefix, composed script, replay dependencies,
   reference/render/auxiliary captures, and predecessor receipts still match their source
   hashes; and
8. outcome bytes and ledger projection close on the same terminal receipt.

The verifier does not require a historical receipt's execution token to equal the live token
directly. For a unit receipt it requires the stronger statement that every contiguous,
independently evaluated edge carried the exact unit binding, even if sibling changes made the
layer generation different. For a terminal layer receipt every edge must carry the exact complete
layer binding. In both cases no intermediate generation may omit or invalidate the receipt;
semantic A -> B -> A resemblance cannot revive it.

Prior replay, successor scheduling, JIT materialization, revalidation, acceptance, and final
render continue to consume only this shared verifier.

### Derived projections

Layer outcome and ledger rows remain HIR-0170 projections, not transaction authority. When a
transition invalidates a finalization, old outcome or `passed` ledger bytes may remain briefly as
audit evidence, but the current state binding makes them immediately non-consumable. A
deterministic postcommit reconciler may archive an outcome and reset the ledger projection from
the transition receipt. Missing projection reconciliation cannot reopen the authority commit or
rerun evidence.

## Implemented decomposition

The unreleased implementation introduces cohesive modules:

- `domain/authority_capsules.py`: pure capsule and impact-layer compiler;
- `domain/authority_state_records.py` and its dependency-free leaf modules: closed head,
  proposal, intent, commit, binding, and evaluation contracts;
- `orchestration/authority_state_effects.py`: pure preservation and invalidation closure over
  verified durable state and capsules;
- `orchestration/authority_state_store.py`: descriptor-relative immutable members, pending and
  current pointers;
- `orchestration/authority_state_context.py`: independently resolved coordinator context and
  exact record joins;
- `orchestration/authority_state_transaction.py`: lock acquisition, exact CAS, commit ordering,
  and plan/JIT publisher adapter;
- `orchestration/authority_state_recovery.py`: exact before/after roll-forward;
- `orchestration/authority_receipt_lineage.py`: immediate-predecessor preservation proof for
  historical receipts; and
- `evaluation/authority_state_transition.py`: independent postcondition evaluation.

It refactors the existing owners to:

- make `authority_state_effects` the sole selected-authority state transform and retire public
  `vfx units replan` / `unit_state.initialize` adoption as state-movement paths; a selected
  capsule mismatch outside the coordinator fails closed;
- preserve strict state schema 1/digest schema 4 bytes, change `plan_hash` production to the
  semantic layer-capsule digest, and move current-generation authorization into external typed
  member bindings;
- keep HIR-0170's exact current claim, replay v2, evaluation v1, and terminal v2 records immutable
  and add coordinator-lineage authorization at their singular consumer boundary;
- migrate every state mutation entry point to the selection -> state lock order;
- extend materialization proposal/finalization and consumer preview to bind all affected state;
- route `plan_authority.publish_current` and `publish_materialization` through the shared
  transaction;
- remove planner post-publication reconciliation; and
- extend `layer_publication` with transition-lineage verification rather than adding another
  consumer-specific fast path.

Architecture tests must prove no production writer can replace plan/JIT heads or authority-bound
work-unit state outside this transaction boundary.

## Rejected patch-level alternatives

- Compare only the Layer 1 row after JIT revision 2: referenced contracts, requirements,
  interfaces, constraints, or debt definitions can change while the row stays equal.
- Ignore the receipt's old selection token when unit digests match: this silently changes the v1
  contract and permits A -> B -> A resurrection.
- Rewrite the selection token or `plan_hash` inside an accepted receipt: that manufactures a new
  claim for evidence that did not execute under it.
- Mint a new completion or finalization receipt without replay: a new evidence digest would imply
  a new evaluation or judgment that never occurred.
- Run current `apply_replan` after pointer publication: it retains the crash window and archives
  proof merely because a sibling changed the combined view hash.
- Move state before selecting the view: an unpublished or gate-failing candidate cannot retire
  accepted work, and this only reverses the same split transaction.
- Hold selection/state locks during the materialization gate, Blender, raster, or critic:
  unbounded work must remain optimistic and postchecked.
- Treat `pending.json` as success and let readers inspect whichever side looks complete: pending
  means the transaction has no visible committed side.
- Recover by choosing the newest timestamp, revision-looking file, or matching semantic view:
  only exact before/after identities in the selected intent are legal.
- Include the complete JIT view hash in every layer capsule: this recreates sibling invalidation
  under a different field name.
- Preserve every layer when the plan bundle digest is unchanged: one materialization can replace
  contracts or a unit DAG inside the same bundle generation.
- Let a later A-like generation search receipt history after B invalidated it: current immediate
  predecessor state is the only adoption source.
- Add outcome/ledger to the commit as alternative acceptance authority: they remain projections
  of terminal receipt state and cannot repair a missing binding.

## Validation required

### Semantic preservation and invalidation fixtures

- Camera singleton Layer 1 finalizes under JIT revision 1. Matching form Layer 2 materialization
  selects revision 2, adopts the exact Layer 1 unit and finalization receipts once, and consumes
  them without replay, raster, critic, or outcome rewrite.
- An independent sibling branch materializes while an unrelated finalized branch remains
  byte-identical and current.
- A rematerialized layer contains one unchanged accepted unit, one changed unit, and one dependant.
  The unchanged unit receipt survives the changed layer transition through its exact unit binding;
  the changed and downstream units reopen, and the old terminal layer receipt is revoked.
- The same unchanged unit crosses two successive layer-changing transitions only when both
  immediate-predecessor effects name its exact binding; omission or substitution on either edge
  makes the historical receipt unusable.
- An upstream layer change invalidates every dependent sparse-layer finalization and unit closure
  while preserving an independent branch.
- A later payer activation for existing camera debt preserves the earlier camera finalization;
  changing the debt definition invalidates its owner.
- An active unit attempt and active layer-finalization claim are revoked by a selection change;
  their stale post-checks cannot publish. Terminal receipts over identical capsules can be
  adopted.
- A plan/JIT semantic A -> B -> A sequence proves that a receipt invalidated by B never returns
  to the current authorized set.
- A true identical plan/JIT publication is a no-op for both selection and authority-state heads.

### Crash and recovery fixtures

Inject process death:

- before pending publication;
- immediately after pending publication;
- after the first of several state replacements;
- after all state replacements;
- after plan or JIT pointer replacement;
- after commit-receipt publication;
- after independent evaluation;
- after current-head replacement; and
- immediately before pending removal.

Every post-pending fixture must recover the identical target once without invoking planner,
Blender, render, critic, or judgment. Repeated recovery is an exact no-op.

Exception-injection fixtures exercise the deterministic roll-forward points in-process. They do
not substitute for the complete subprocess-death fixtures that prove staged files, directory
fsync, and every pointer/replacement boundary survive interpreter loss. Focused hostile-state
and pre-replacement subprocess coverage is already part of the regression suite, but this record
remains proposed until the complete crash-boundary matrix and broader validation pass.

Inject a live member matching neither before nor after, a missing staged member, conflicting
pending pointer, corrupted commit, failed evaluator, symlinked state/head/member, and parent
directory substitution. Each leaves pending selected and fails closed as a harness defect.

### Concurrency and migration fixtures

- Two candidates finalized from one base serialize; one commits and the other fails its exact
  precondition without mutating state.
- A unit state mutation racing publication either commits entirely before the transition or sees
  the successor binding; no mixed state is possible.
- State-file creation racing enumeration cannot escape the selection -> state lock order.
- Pre-coordinator state or receipts without an exact current member binding and contiguous
  preservation lineage are rejected as current authority; current HIR-0170 replay v2,
  evaluation v1, and terminal v2 receipts remain usable only as immutable historical execution
  evidence until an evaluated lineage authorizes them.
- Missing authority-state head alongside selected current-schema plan/JIT heads is not
  auto-bootstrapped.
- Unknown capsule schema fields and authority rows without a total impact projection fail before
  pending publication.
- Outcome and ledger bytes alone cannot keep an invalidated layer current.

The key acceptance test is:

> Layer 1's immutable terminal evidence is produced once under JIT revision 1. Publishing Layer
> 2 under revision 2 commits one independently evaluated authority-state transition whose
> immediate-predecessor adoption preserves Layer 1 byte-for-byte. Layer 2 consumes that proof,
> Layer 1 incurs no replay or judgment spend, and a crash at every multi-file boundary converges
> on the same committed state.

## Release and migration

This is a strict unreleased migration.

- Existing state/receipt bytes are not reinterpreted as current merely because their semantic
  capsule matches. Current use requires an evaluated coordinator member binding and, when the
  execution token is historical, an exact contiguous preservation lineage.
- Selected plan/JIT heads without a matching authority-state head are incomplete current
  authority after cutover.
- Disposable fixtures and local shots recreate authority through the public plan/materialization
  boundaries.
- Durable historical shots require an explicit reviewed migration transaction. That migration
  may preserve evidence only when current schemas can prove complete capsules, sources, and an
  immediate predecessor binding; otherwise it archives or retires the evidence. It never
  synthesizes a receipt from status, script presence, ledger rows, timestamps, or transcript
  history.
- No compatibility mode falls back to post-publication `_reconcile_materialized_layer_state` or
  direct `apply_replan` for selected-authority movement.

There is no selected coordinator head when plan and JIT selection are both absent and there is
no authority-bound layer state. A genesis proposal records predecessor revision zero and the
first plan publication selects coordinator revision one. An existing selected pair is not
silently declared genesis.

## Rollback

Rolling back only publishers would strand external state bindings behind pointer-only selection.
Rolling back only readers would allow selected heads and state to disagree without the pending
barrier. Reinterpreting any retired receipt schema, or treating a current immutable execution
receipt as selected authority without its evaluated preservation lineage, would erase the
distinction between historical execution and current authorization.

The safe operational fallback is to retain strict readers, pending recovery, and coordinator
bindings while disabling new authority publication until the transaction implementation is
repaired.
Do not downgrade records, remove pending manually, restore post-publication reconciliation, or
reinterpret historical receipts.

## Remaining limitations

This mechanism is a local-filesystem logical transaction, not a cross-host distributed commit or
cryptographic signature system. It assumes every authority-bearing local writer obeys the shared
lock and storage boundary; architecture tests make violations visible.

HIR-0171 does not authorize automatic replanning, finding consumption, accepted-work discard,
checkpointed model-session resume, controller dispatch, or human decisions. It moves only an
already validated exact authority candidate and its mechanically derived state effects. Any
change requiring new judgment must produce new authority and new evidence through its owning
public transaction. The old `vfx units replan` command is retired rather than allowed to replay
the same invalidation after publication. A future exact finding-consumption adapter must verify
the immutable transition before-image and effects and publish its own receipt without mutating
state again.
