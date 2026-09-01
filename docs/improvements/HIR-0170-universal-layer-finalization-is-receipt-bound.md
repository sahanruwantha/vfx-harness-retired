---
id: HIR-0170
title: Universal layer finalization is receipt-bound
status: proposed
introduced_in: unreleased
date: 2026-09-01
failure_class: unit_success_and_unclaimed_composition_could_publish_layer_authority
mechanism: claimed_layer_composition_group_replay_evaluation_and_terminal_receipt_state_machine
adr: null
---

# Universal layer finalization is receipt-bound

## Observed failure

Layer publication currently has two overlapping routes instead of one authoritative boundary.
A singleton layer asks `build_unit` to publish layer outcome and layer-ledger success while that
unit is still finalizing. The enclosing layer builder then normally composes and replays the
identity-derived unit script as a layer artifact anyway, because HIR-0126 no longer permits the
unit script to be the layer script. The first publication can therefore say that the layer
passed before unit completion and before the later empty-scene layer replay have established
the layer boundary.

A multi-unit layer avoids that singleton shortcut, but its synthetic composition is not owned by
a typed attempt. It writes the composed layer script, starts a generic layer-ledger milestone,
runs Blender and any independent critic, projects judgment-debt transitions and falsification
records, and finally writes a layer outcome and ledger result while guarded only by the selected
plan/JIT authority. Exact constituent completion receipts do not become a durable composition
claim, and no single immutable terminal receipt identifies the result from which those several
projections were derived.

This leaves both false-positive and duplicate-spend windows:

- unit success can be mistaken for layer success before the layer artifact has replayed from an
  empty scene;
- a replan, rematerialization, unit invalidation, or script substitution can make an in-flight
  composition stale while paid replay or critic work is running;
- a crash after judgment-debt or finding publication but before outcome or ledger publication
  can cause the same evidence to be judged and projected again on restart;
- a crash after outcome or ledger publication can leave those projections disagreeing with one
  another without a terminal source of truth; and
- downstream replay, revalidation, acceptance, and render paths can treat unit completion
  receipts plus `shot.json` layer status as sufficient even though no current layer-finalization
  transaction exists.

The unused singleton artifact-equality shortcut is not a harmless optimization. If it becomes
reachable again, it bypasses composed replay entirely and recreates the same publication gap.

## Root cause

Work-unit completion and layer publication are different ownership boundaries, but the runtime
lets one impersonate the other when a layer happens to contain one unit. At the same time,
synthetic multi-unit composition is treated as orchestration code rather than a durable claimed
transaction. The number of units therefore changes the authority path even though the layer is
always the public dependency and replay boundary.

The builder also projects one logical finalization result onto several mutable stores without
first committing an immutable result that can be reconciled. A ledger row, judgment-debt state,
finding, or outcome can consequently be the first durable assertion of success or failure. None
of those partial projections proves the exact composition attempt, replay inputs, evaluated
evidence, or independent judgment that authorized it.

The earliest owning defect is the layer-finalization boundary. It is not a critic-quality,
planner, retry, or ledger-format problem. Every layer needs the same claimed state machine after
all of its current units have independently completed.

## Decision criteria

- Every layer, including a one-unit layer, finalizes through one universal layer-composition
  state machine. Unit count selects only the closed mode
  `singleton_passthrough | multi_unit_fan_in`; it never selects a different authority path.
- A unit attempt may publish only its identity-derived unit script, checkpoint, independent
  evaluation receipt, completion receipt, and unit-scoped diagnostics. It cannot write a layer
  outcome, mark the layer ledger passed, or make the unit artifact the public layer artifact.
- Finalization begins only after the exact current completion receipt of every constituent unit
  is source-verified in stable dependency order. Passed lifecycle strings or ledger rows are not
  substitutes.
- Before composed-script publication, Blender execution, raster capture, or critic spend, one
  durable layer-composition claim binds the selected authority token, layer plan digest, exact
  ordered constituent completion receipts, proposed layer script identity, run, mode, and
  monotone finalization revision.
- Every long operation uses exact claim pre-check, unlocked work, and exact post-check. Replan,
  rematerialization, unit invalidation, or source substitution makes the result ineligible for
  publication; it does not wait behind model, Blender, render, or large-file work.
- Both modes publish a distinct composed layer artifact and replay it from an empty scene using
  the same evaluated-state barriers, typed construction inputs, selected dependency prefix,
  worklist fan-in, and evidence rules. `singleton_passthrough` means one constituent, not
  replay bypass or byte-copy authority.
- Executable-only finalization reaches its deterministic verdict without raster or a critic.
  Raster and independent qualitative judgment occur only when typed evidence requires them, and
  every paid call is guarded by the exact layer claim. Empty camera or control plates are never
  invented.
- Every actual evaluation group publishes its own immutable replay receipt before independent
  judgment consumes that group's result. The group plan binds its index and total count,
  required claims and evidence ids, judge points, debt/payment generation when applicable, and
  its own closed evidence kind plus `solid | eevee` render mode and scale. Executable-only groups
  carry no render settings.
- The executed replay receipts form an ordered contiguous prefix starting at group zero. One
  immutable evaluation receipt binds that prefix and mechanically derives each group result and
  the layer status. A passing evaluation contains every planned group; a failed group terminates
  the prefix, and skipped, duplicate, reordered, or post-failure groups fail closed.
- Replay execution failure is evidence, not an exception-shaped verdict supplied by a caller.
  A failed group receipt carries one typed stage/message/digest and no unexecuted point,
  auxiliary, render, payment, or critic evidence.
- One immutable terminal finalization receipt is committed before any judgment-debt lifecycle,
  finding, schema-3 layer outcome, or layer-ledger result is projected. It binds the external
  evaluation-receipt locator, file digest, semantic digest, and exact content; its status,
  canonical rows, group rows, script identity, and projection digest are mechanical derivatives.
- Restart reconciles every missing or partial projection from that exact terminal receipt. It
  never reruns Blender, rerenders, repays the critic, changes a judgment, or consumes an
  unchanged finding merely because a derived projection is absent.
- Judgment-debt activation used to compile a paid observation is provisional until the terminal
  receipt commits. Persistent transitions remain the independent lifecycle
  `pending_not_due -> due -> satisfied | falsified` and are projected exactly once after the
  terminal receipt. A layer may pass executable finalization while carrying correctly
  `pending_not_due` debt to its compiled future provider.
- Every downstream consumer requires the exact current terminal finalization receipt and its
  closed projections. Unit receipts plus layer status, layer script presence, or a historical
  outcome never make a layer replayable or acceptable.
- Replan, rematerialization, supersession, dependency invalidation, and reviewed retirement
  revoke or archive the affected active claim and make its terminal receipt ineligible for the
  new generation. Same visible status after invalidation cannot revive an older revision.
- Malformed, missing, stale, non-canonical, path-substituted, or source-incomplete claim and
  receipt state fails closed before downstream spend or publication.

## General mechanism

### Universal claimed state

Each layer's durable unit-state record gains one nested finalization slot with a positive
monotone `attempt_revision`, at most one `active_claim`, ordered claim history, and at most one
current terminal receipt binding. Unit lifecycle rows remain independently authoritative; no
synthetic composition unit or broad mutation scope is added.

`vfx-harness.layer-composition-claim/v1` is the closed ownership token for the public layer
boundary. Its immutable identity includes:

- claim id and positive finalization revision;
- run id, layer id, exact layer-plan digest, and complete HIR-0168 selection token;
- closed composition mode;
- the complete stable dependency-ordered constituent set, with each unit id, unit digest,
  completion-receipt digest, canonical unit-script locator, and script SHA-256;
- the exact accepted prior-layer finalization bindings required by the selected layer DAG;
- the canonical layer-script locator and prepared source SHA-256; and
- audit timestamps that do not participate in semantic attempt identity.

Claim publication reloads the current selected authority and per-layer state under the existing
lock order, source-verifies every unit and prior-layer receipt, proves that no current claim or
terminal receipt already owns the generation, increments the revision, and installs the claim
in one state write. The composed layer script is prepared outside the locks and committed only
through a claim guard that rechecks its inode, parent, bytes, selection, unit state, and all
source receipts. A stale prepared file remains inert.

All unit builds use unit-specific ledger slots. The layer milestone may enter `in_progress` only
under the layer claim and must carry that claim identity. It cannot become `passed` or `failed`
until a terminal receipt exists.

### Replay and judgment receipts

Both modes execute the published layer script through the same fresh empty-scene replay
boundary. The replay prefix is compiled from the selected DAG and exact current predecessor
finalization receipts, not from directory contents or ledger ordering. Constituent scripts and
construction inputs retain their existing descriptor-pinned source capture and evaluated-state
barriers.

`vfx-harness.layer-replay-receipt/v2` is an immutable run artifact for one actual evaluation
group. Its typed `vfx-harness.layer-replay-observation/v1` binds the claim, composed-script and
ordered replay-prefix inputs, dependency-ordered unit receipts, exact group plan, and actual
execution result. A successful group records every declared judge point in order, the exact
reference locator and SHA-256, harness-authoritative deterministic evidence rows and their
derived missing/failed sets, and, for a rendered group, the render locator/SHA-256 plus canonical
capture receipt. Motion montages and other declared auxiliary captures are separate typed
locator/SHA-256/frame bindings. Replay-input scripts, construction/dependency files,
references, renders, and auxiliary captures are descriptor-read and retained as causal source
bindings across receipt publication.

The group plan owns its evidence mode. `executable_only` forbids raster settings and qualitative
debt. `render` binds that group's `solid | eevee` mode and scale; a later group in the same layer
may legitimately use a different mode. A critic, when required, consumes only an observation
request bound to that exact group receipt digest, replay-prefix and parent-chain digests,
reference digest, render settings, activation, definition, and payment generation. Claim checks
bracket replay, render, receipt publication, and critic calls, so a revoked attempt cannot
publish their outputs.

Replay failures are sealed through the same v2 receipt instead of being converted into an
unbound orchestration error. The observation records one closed failure stage
(`reset | preamble | predecessor_replay | payer_replay | scope_validation`), message, and
derived failure digest; it forbids point, render, auxiliary-capture, payment, and critic rows.

`vfx-harness.layer-evaluation-receipt/v1` binds the external locator, file SHA-256, and embedded
canonical content of every executed replay receipt. Those receipts must be the exact ordered
contiguous prefix `0..n-1`, share the same claim and replay prefix, and agree on the planned group
count. Each evaluation group exactly partitions the sealed point observations. Deterministic
and qualitative verdicts must join the group that produced their evidence; payment failures
likewise bind the group's request and replay identity. Results and final status are mechanically
derived. Passing requires all planned groups. A non-passing result may appear only on the final
executed group, so early termination is explicit without allowing skipped work to pass.

Debt observation compilation is split from durable debt mutation. The compiler may derive that
a selected definition would be due in the claim's exact replay prefix and issue a bound
observation request without first writing `due`. That provisional calculation has no acceptance
authority. After terminal commit, deterministic reconciliation applies the same activation and
resolution to durable state. This removes the current need to mutate debt state merely to
authorize a paid critic call.

### Terminal commit before projections

`vfx-harness.layer-finalization-receipt/v2` is the immutable source of truth for one completed
attempt. It binds the complete claim and the exact external
`vfx-harness.layer-evaluation-receipt/v1` locator, file SHA-256, semantic digest, and embedded
content. The terminal's status, group rows, canonical rows, script identity, and projection
digest are re-derived from that evaluation receipt; a caller cannot independently assert them.
Terminal preparation and commit reopen the evaluation receipt, every bound group receipt, every
replay-input/dependency byte, and every reference, render, and auxiliary capture. Terminal commit
then rechecks the active claim and those retained source identities, archives that claim as
completed, and installs the exact receipt in nested layer-finalization state before writing any
projection.

The strict order is:

1. prepare deterministic composed bytes and claim the exact layer generation;
2. guarded layer-script publication;
3. for each planned group in order, guarded fresh replay and one immutable v2 group receipt;
4. guarded group-specific independent judgment when that group's typed authority requires it,
   stopping the executed prefix on the first non-passing result;
5. one immutable v1 evaluation receipt over the ordered contiguous replay-receipt prefix;
6. one immutable v2 terminal finalization receipt committed to current layer state;
7. judgment-debt activation/resolution or preserved pending state;
8. typed falsification/finding publication when the terminal result requires it;
9. schema-3 layer outcome publication for an accepted layer; and
10. final layer-ledger projection.

Steps 7 through 10 are deterministic projections, not new decisions. Each is idempotent against
the exact terminal receipt and refuses conflicting existing bytes. If a crash occurs anywhere
after step 6, the next invocation enters reconciliation before scheduling or spending. If a
crash occurs earlier, the active claim and immutable evidence remain reviewable; no automatic
retry or session resume is inferred.

### Reviewed pre-terminal release

A process can die after claiming finalization but before committing a terminal receipt. Restart
must initially fail closed because the durable active claim still owns the layer. The separate
operator boundary

```bash
vfx finalizations release <shot> --layer <layer-id> --claim-id <lfc-...> \
  --reason "<reviewed reason>" --evidence <shot-relative-file> [--evidence ...]
```

is the only finalization-only escape hatch. It is not a unit retry, invalidation, semantic
replacement, checkpoint resume, or finding-consumption alias. The command requires an exclusive
live builder fence, the exact full active claim, its current selected layer generation, the
current coordinator head, the dependency-ordered source-authorized unit completion closure, and
non-empty descriptor-read review evidence. Before release publication, every reviewed source
byte is copied create-only to its SHA-256-derived locator under
`state/layer-finalization-releases/evidence/`; conflicting bytes at that locator fail closed. The
request retains both the original source locator and the immutable snapshot locator/digest.

If the claim has already published replay receipts, release must capture the complete existing
ordered contiguous prefix starting at group zero. Every
`vfx-harness.layer-finalization-release-evidence/v2` replay row binds its group index, common
planned-group count, canonical locator, file SHA-256, v2 replay schema, semantic digest, and exact
claim. A gap, duplicate, non-canonical or out-of-range locator, inconsistent planned count, or
receipt from another claim fails closed. It is legal for a crash before the first replay receipt
to produce an empty replay prefix; it is not legal to capture only group zero when later group
receipts exist. A terminal finalization is never releasable.

`vfx-harness.layer-finalization-release-request/v2` binds those replay-prefix rows, the v2
content-addressed review-evidence rows, the reviewed reason, and the fixed judgment disposition
`unsealed_not_reusable`. `vfx-harness.layer-finalization-release-receipt/v2` is published
immutably before the state mutation. Under the existing selection-then-state lock order, the
finalization-state facade
rechecks every authority input, archives the claim with disposition `released` and the complete
content-addressed evidence closure, then clears only `active_claim`. It does not change any unit
row, completion receipt, checkpoint, attempt history, mutation authority, or selected plan/JIT
state. A later builder may therefore mint a fresh claim at the next monotone finalization
revision while reusing the unchanged accepted unit receipts. Before incrementing, ordinary claim
publication source-verifies every prior `released` archive against its exact immutable release
receipt locator, file SHA-256, semantic digest, claim, reason, timestamp, and archive evidence;
it also reopens and hashes every content-addressed review snapshot. Missing or altered release
authority or immutable evidence leaves the layer unclaimed.

An ordinary critic output written before the crash is review evidence only. Without a sealed
terminal or separately specified immutable judgment-phase receipt, release does not claim that
the judgment can be reused or that critic spend was exact-once; the fresh claim may need to judge
again. Repeating the exact command reconciles from the archived immutable release receipt and
review snapshots without rereading mutable original files. A different reason, original evidence
locator set, claim, active claim, selection, or receipt identity fails closed. An immutable
release file left before the state write can be reconciled only by the exact same request.

Failed qualitative judgment preserves concrete qualified observations and derives the HIR-0139
producer closure from the terminal receipt. Finding publication remains separate from replan:
recording the exact finding does not revoke accepted units, widen mutation authority, or consume
the finding. An evidence-unavailable terminal result likewise preserves debt according to its
typed lifecycle and cannot be converted into success or a guessed local retry.

### Receipt-bound layer outcome and consumers

`vfx-harness.layer-outcome/v3` replaces schema 2. A passing record embeds or exactly references
the current `vfx-harness.layer-finalization-receipt/v2` by locator, file SHA-256, and receipt
digest, and repeats only fields whose equality is mechanically verified: layer id and digest,
selected authority, composed script locator and SHA-256, canonical evidence, interfaces,
revalidation data, and terminal disposition. A pure parser validates the closed v3 shape and
digests. A shot-aware reader additionally joins the referenced terminal to its external v1
evaluation receipt and every external v2 replay-group receipt, then reopens their replay scripts,
dependencies, point references, rendered PNGs, auxiliary captures, composed layer script,
predecessor outcomes, revalidation/runtime inputs, selected plan files, and harness identity.
Missing, altered, aliased, or unjoined bytes fail before the outcome can be current even when an
embedded receipt or duplicated digest still parses.

The former `unit_attempt | composition` outcome-publication authority is retired. There is one
`finalization` authority, and it is the exact terminal receipt. Schema-2 outcomes are historical
artifacts and are never interpreted as receipt-backed v3 records.

Every consumer uses one shared verifier rather than independently reconstructing readiness:

- prior-layer replay and successor-unit scheduling;
- builder advancement after a layer invocation;
- JIT materialization and rematerialization inputs;
- revalidation eligibility and publication;
- acceptance chain construction and completion checks; and
- final deliverable render gating.

The verifier requires the selected DAG's exact layer generation, source-verified terminal,
evaluation, and complete replay-receipt chain, successful closed projections, composed script
bytes, observation sources, and predecessor receipt closure. A ledger `passed` row is then an
audit/read-model projection, never the source of layer acceptance.

Finished-chain capture moves to `vfx-harness.acceptance-authoritative-before/v2` and
`vfx-harness.acceptance-chain/v2`: every layer row binds the terminal finalization receipt,
the exact v3 outcome bytes, and each unit completion receipt. Final-media capture moves to
`vfx-harness.final-render-replay-snapshot/v2`, carries those outcome files as immutable source
bindings, and rechecks the full shared verifier after the expensive render before tentative
publication. Its locked rename/postcondition phase then retains the captured layer-state,
ledger, outcome, script, and dependency bytes; any drift restores the exact predecessor media.

## Rejected patch-level alternatives

- Special-case singleton layers by copying their unit outcome: one unit does not remove the
  public empty-scene replay boundary or prove layer-level provisional decisions.
- Delete only the obsolete singleton early return: multi-unit composition would remain
  unclaimed and crash-inconsistent.
- Treat the last unit attempt as the composition attempt: its authority names one unit script
  and mutation scope, not the layer fan-in, predecessor closure, or composed artifact.
- Mark the layer passed when every unit is passed: independent unit evidence does not prove
  composed replay, interfaces, worklist fan-in, or provisional judgment.
- Guard composition only with the selected plan/JIT token: unit completion, script, checkpoint,
  or finalization revision can change without changing that token.
- Write debt state before payment and use it as the attempt marker: debt lifecycle is evidence
  authority, not execution ownership, and it cannot bind the rest of the layer result.
- Let each projection carry its own decision and retry behavior: that recreates partial
  publication, duplicate critic spend, and conflicting judgments after a crash.
- Hold selection and unit-state locks during Blender or critic calls: this blocks unrelated
  authority publication for an unbounded paid interval and still does not create a replay or
  terminal receipt.
- Accept schema-2 outcomes when their script and ledger look current: this silently invents the
  missing finalization transaction and violates strict migration.
- Repair a missing projection by rerunning finalization: once a terminal receipt exists,
  reconciliation must reproduce only its exact deterministic projections.

## Validation required

Acceptance requires deterministic fixtures proving at least the following:

- a heterogeneous singleton executable-only layer enters `singleton_passthrough`, publishes a
  distinct composed script, replays from empty without raster or critic, and becomes visible to
  successors only after its terminal receipt and projections;
- a singleton layer with provisional qualitative debt uses the same claimed path and pays the
  critic exactly once;
- a heterogeneous multi-unit DAG enters `multi_unit_fan_in`, preserves stable dependency order,
  fans in every exact unit completion receipt and worklist, and executes evaluated-state barriers;
- unit completion, unit ledger success, layer script presence, or an injected layer-ledger
  `passed` row cannot independently make either mode downstream-ready;
- missing, stale, tampered, reordered, duplicated, or source-incomplete unit and predecessor
  receipts fail before claim, Blender, raster, or critic spend;
- mode/unit-count mismatch and attempts to alias a unit script as the layer script fail closed;
- selected authority, layer materialization, unit state, completion receipt, composed script, or
  predecessor finalization changes before publication, during replay, during render, during
  critic judgment, and before terminal commit invalidate the stale claim and every staged output;
- a claim revision is monotone, a released or revoked claim cannot publish, and an apparent
  finalization-status A -> B -> A cannot revive the earlier attempt;
- each actual evaluation group publishes one source-verified v2 replay receipt; an
  executable-only group invokes no raster or critic, while a rendered group and any critic input
  bind that exact group's receipt, render mode/scale, source-closed reference/render/auxiliary
  captures, and immutable payment generation;
- multi-group evaluation accepts only the ordered contiguous receipt prefix from group zero;
  full pass requires every planned group, a typed replay failure seals its exact stage and stops
  the prefix without critic spend, and skip/duplicate/reorder/post-failure groups are rejected;
- camera/control-only layers with no optical debt never render an empty plate;
- deferred camera-owned framing debt remains `pending_not_due` through its owner's accepted
  finalization, activates only at the compiled matching carrier prefix, and is judged once;
- incorrect ownership and no reachable future provider still fail at planning/publication rather
  than being hidden by finalization or debt activation;
- a composed qualitative failure commits one terminal receipt, then publishes the exact debt
  resolution and HIR-0139 finding once without changing accepted unit state or automatically
  replanning;
- crashes after any replay-group receipt, after critic output, after terminal commit, and after
  each debt, finding, outcome, and ledger projection are recovered. No post-terminal case reruns
  Blender or spends critic budget, and conflicting pre-existing projection bytes fail closed;
- process death after any replay-group receipt and after ordinary critic output leaves restart
  blocked on the exact active claim; reviewed v2 finalization release captures the complete
  available contiguous replay-receipt prefix plus content-addressed review evidence, preserves
  every unit row and source byte, permits one higher-revision fresh claim, makes the stale
  claim/replay prefix ineligible to publish, and either reconciles an exact repeat or clearly
  rejects a conflicting one without widening authority;
- deleting or rewriting the original reviewed files after commit does not block exact
  reconciliation or a fresh claim because immutable snapshots retain their bytes; deleting or
  tampering a selected snapshot does block both repeat verification and the next claim;
- injected death after immutable v2 release-receipt commit but before state write leaves one exact
  reconcilable receipt orphan, while death after state write but before command return leaves one
  release history row; exact retry converges in both windows without changing any unit,
  checkpoint, completion-receipt, or script bytes;
- layer-outcome schema 2, unknown v3 fields, wrong terminal/evaluation/group-receipt
  locator/hash/digest, altered embedded receipt, missing or changed reference/render/auxiliary
  source, mismatched script/evidence/interfaces, and a terminal receipt not selected in current
  nested state are rejected;
- prior replay, successor scheduling, JIT materialization, revalidation, acceptance, and final
  render all reject a layer lacking the exact current successful finalization receipt; and
- replan, rematerialization, supersession, and dependency invalidation retire the affected
  finalization generation and make all old outcomes and ledger projections inert.

Focused validation must include unit, contract, architecture, and integration coverage for the
state machine and every consumer. Full validation still requires Ruff across `src`, the complete
test suite, strict preflight, and a real confined Blender replay fixture before this record can
move from proposed to accepted.

## Release and rollback

This is a strict unreleased migration. Layer outcome v2 and the retired
`vfx-harness.layer-composition-replay-receipt/v1`,
`vfx-harness.layer-finalization-receipt/v1`,
`vfx-harness.layer-finalization-release-request/v1`,
`vfx-harness.layer-finalization-release-receipt/v1`, and
`vfx-harness.layer-finalization-release-evidence/v1` records are not current authority. Neither
are singleton unit-published layer outcomes, unclaimed composition results, or layer-ledger
success without a current terminal receipt. Disposable fixtures republish through universal
finalization; durable historical shots require an explicit reviewed migration or retirement.
Readers never synthesize a claim, replay group, evaluation receipt, or terminal receipt from a
script, ledger row, unit completion set, historical run, or schema-2 outcome.

The nested finalization slot may be introduced as a closed extension of current per-layer unit
state only if complete absence has the single meaning `not finalized`. Once any finalization
field is present, its revision, claim/history, receipt binding, lifecycle compatibility, and
source closure are mandatory. Partially present or malformed state is not repaired
heuristically.

Rolling back only consumers would allow current v3 outcomes to be misread or bypassed. Rolling
back only publishers would strand receipt-aware consumers behind unclaimed layer rows. The safe
fallback is to retain strict readers and refuse new layer publication while repairing the
mechanism; never downgrade v3 bytes or reinterpret a unit receipt as a layer receipt.

## Remaining limitations and HIR-0171 boundary

HIR-0170 makes one fixed selected layer generation's finalization claimed, replay-bound,
terminal, and crash-reconcilable. Proposed HIR-0171 now has an implementation of the
cross-surface authority-state commit: exact proposed head, semantic capsule/state diff,
write-ahead intent, commit ordering, crash recovery, independent evaluation, and contiguous
receipt-preservation lineage. It consumes HIR-0170's active and terminal identities when
retiring or preserving layer generations, so selected plan/JIT movement no longer depends on a
post-publication replan/rematerialization state repair.

Both records remain proposed until their combined heterogeneous, full-suite, strict-preflight,
and confined real-replay validation is complete. HIR-0170 must continue to fail closed if the
coordinator head, live state, semantic capsule, or historical receipt lineage is absent or
inexact; finalization reconciliation cannot substitute for authority publication.

This record also does not add an automatic controller, amendment executor, local-retry
classifier, checkpointed model-session resume, cross-host distributed lock, or permission to
discard accepted work. A failed or orphaned active finalization claim remains blocked until an
operator invokes the exact reviewed `vfx finalizations release` transaction; that transaction
abandons only finalization ownership and cannot change semantic authority or accepted unit work.
