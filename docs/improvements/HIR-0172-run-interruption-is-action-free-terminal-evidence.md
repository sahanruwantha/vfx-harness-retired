---
id: HIR-0172
title: Run interruption is action-free terminal evidence
status: proposed
introduced_in: unreleased
date: 2026-09-01
failure_class: interrupted_direct_invocation_remained_running_or_invented_failure_dispatch
mechanism: fenced_run_owner_and_strict_action_free_interruption_receipt
adr: null
---

# Run interruption is action-free terminal evidence

## Observed failure

A direct global-plan invocation for the synthetic two-layer judgment-debt fixture created run
`20260901T140547Z-3db678`, published its manifest, running status, and the opening plan transcript,
then emitted no model response. The operator interrupted the process. After the process had exited,
the selected run still said `running`; `reports/summary.json` was absent, `artifacts.json` still
described only the initial manifest and status, and the transcript ended after `open` and `prompt`
without a `close` event.

The existing unit fixture did not exercise that process boundary. It raises
`KeyboardInterrupt` inside `run_artifacts.invocation`, so ordinary context-manager unwinding
publishes an `interrupted` status. A real signal delivered while AnyIO and the Agent SDK own the
response stream can terminate or cancel outside that cooperative path. Direct invocations have no
root-owner fence or process-exit reconciler; only `vfx run` installs an `atexit` callback.

The cooperative path is also semantically wrong. It converts an operator interruption into the
generic HIR-0164 missing-boundary stop: `harness_defect` with one `route_engineering` action. The
whole-shot driver's `atexit` path does the same. Stopping a process is evidence that an invocation
did not finish. It is not evidence that engineering was routed, that authority is defective, that
an exact unit may retry, or that a model session may resume.

## Root cause

Run creation and terminal publication are a lexical context-manager convention rather than a
durable ownership lifecycle. `status.json: running` does not name a live process instance or a
fence that another process can use to prove owner loss. SIGINT handling depends on Python stack
unwinding, SIGTERM has no controlled run-terminal path, and `atexit` cannot cover every signal,
runtime abort, or machine loss.

The terminal schema also conflates two different facts:

- a failure is a classified unaccepted boundary with exactly one HIR-0164 transaction proposal;
- an interruption is a cessation observation with no transaction proposal.

Forcing both through `vfx-harness.stop-envelope/v1` either invents recovery authority or leaves a
run permanently live. This is an observability ownership and terminal-state defect, not a planner,
model-quality, retry, or authority-repair defect.

## Decision criteria

- HIR-0164 remains unchanged: every classified failure stop contains exactly one typed action.
- An interruption is represented by a separate strict receipt with no stop class, `StopAction`,
  target, postcondition, receipt schema, budget key, retryability claim, or dispatch mode.
- Every root invocation acquires one durable owner claim and a non-inheritable OS-backed exclusive
  fence before publishing `running`, and holds it until a terminal status is read back.
- A PID, timestamp, heartbeat age, missing SDK event, or absent shell process by itself never proves
  owner loss. Reconciliation requires exclusive acquisition of the exact recorded fence.
- SIGINT and SIGTERM have closed, distinct semantics. Signal handlers request cancellation; they do
  not serialize JSON, hash files, take application locks, or publish authority.
- The terminalizer records the exact run evidence that exists. It does not append a fictional
  transcript close, invent a model result, or call a partially written session resumable.
- Interruption terminalization changes only run-scoped generated output. It neither edits nor rolls
  back selected plan authority, accepted build state, durable unit state, or judgment debt.
- An interrupted status selects the receipt and an independent evaluation by exact locator and
  digest. Proximity, exit code, status detail, and an unselected receipt are not authority.
- Terminal selection is exactly once. A valid failed or passed commit is never replaced by an
  interruption, and two signal deliveries cannot mint two terminal meanings.
- Root-owner loss after a hard kill is deterministically reconcilable without dispatching a
  controller, rerunning a model, or consuming a finding.

## General mechanism

### Current foundation is deliberately non-publishable

The current foundation defines the strict records, the namespace-bound owner-fence contract, the
non-integrated `vfx-harness.shot-ledger/v2` value schema, and a partially integrated shot-authority lease
over the permanent selection lock. That lease is process-, thread-, shot-root-, and lock-inode-bound;
uses a refcounted per-inode process mutex, a POSIX record lock, and an armed crash-safe live-identity
claim; and records each inherited descriptor with its captured file identity. Managed acquisition,
registration, handoff, and cleanup transactions close the call-to-return interruption gaps. Cleanup
atomically shrinks both fork-visible and local rows to the exact still-live identity subset before
closing neutralized slots, so a closed-and-reused descriptor number is never touched. Direct
regressions cover the pre-registration fork window, process death, same-shot serialization,
distinct-shot concurrency, physical path aliases, foreign-thread context unwind, unexpected
same-process descriptor closure/reuse, partial cleanup, acquisition interruption, and both directions
of the legacy `flock` transition. The same managed fork-acquisition primitive now owns root-run fence
opens and handoff as well. The shot-authority lease is now installed at the final mutation boundary
for the layer-finalization artifact/replay/evaluation/outcome chain and every canonical legacy
`Ledger` commit. The latter is a physical transport boundary only: it does not derive or emit
strict accepted-build authority. Other sanctioned writers are not all migrated to their actual
inner lock and sink. The foundation now implements the strict ledger derivation boundary, the
fence-held capture into a run-owned archive, the independent source-verifying evaluator, and the
exactly-once terminal commit path with its source-verifying interrupted reader, and the
root-owner boundary that mints every public run as the v2 generation and terminalizes recorded
SIGINT/SIGTERM intent, and the model-free owner-loss reconciler that proves loss only by
acquiring the exact recorded fence; capability-bound issuance, authored/refobs capture, and the
process-level signal and fork matrices remain unimplemented.
The two selected record locators are already closed to
`reports/interruption-receipt.json` and
`reports/interruption-receipt-evaluation.json`; alternate nearby files have no authority.

This intermediate state fails closed. `InterruptionReceiptEvaluation` has no public factory that
lets a caller choose `satisfied`, and its pure receipt-binding helper derives identity fields only
— never status or issues. `RunStatusV2` selects `interrupted` only for the typed receipt, a
satisfied evaluation that binds that exact receipt, the exact owner claim the receipt binds, an
evaluation time not after selection, and the receipt-derived exit code; the terminalizer is the
only production minter, and the only authoritative reader re-evaluates the archive before it
returns a status.

Before that refusal may be removed, the evaluator must be able to prove the complete graph rather
than only rehash caller-enumerated files. In particular, the strict ledger value must be emitted by
one fenced writer which derives explicit complete ledger-to-member edges from selected authority;
authored intent and selected construction witnesses must be enumerated from their owning namespaces;
optional durable context references
need a closed schema/parser-to-semantic-digest registry (or a narrower typed reference union);
current and pending authority need independently rooted heterogeneous graphs; and the before/after
authority capture must bind one descriptor-stable shot-authority fence across the terminalization
window. Without those prerequisites, a caller could omit a source, alias an owner namespace,
self-assert signal or owner-loss authority, or hide a change-and-revert window and still
manufacture an apparently `satisfied` row.

#### Bounded layer-finalization writer migration

HIR-0172's second prerequisite now has one bounded production migration. The generic prepared-file
transaction retains the exact shot and destination, predecessor, staged inode, publication-lock
generation, and prepared bytes. It stores its commit policy at preparation, requires the matching
commit authorization, rechecks the staged generation after policy execution, performs one CAS
rename, rehashes the held published inode, and fsyncs the parent before reporting success.
Descriptor and temporary ownership is process- and thread-bound, fork-safe, and
interruption-cleaned; symlink, FIFO, substituted-lock, predecessor, staged-byte, post-rename-byte,
and parent-directory identity replacement or rebinding fails closed.
The generic API reserves composed `build/**/*.py` targets (excluding `build/units/`), the
layer-finalization receipt namespace, and sealed outcome namespace. Before even reading a protected
target or accepting a no-op, its typed owner must spend a process- and thread-bound, exact-shot,
exact-path, one-shot staging authorization; protected staging also requires a stored commit policy.
Public immediate publication, caller-selected subroot or ancestor re-rooting, normalized
`..`/absolute aliases, physical symlink aliases, namespace squatting, and
cross-path/family/shot/thread/fork authorization reuse fail closed.

The first typed sink family using that substrate is the composed artifact, per-group replay
receipt, aggregate evaluation receipt, and terminal layer outcome. Staging remains outside the
short joint publication guard. At the mutation boundary the permanent selection-lock writer lease
is outermost, the exact claim or terminal-receipt guard is actively held, and the builder guard
privately issues one process-, thread-, shot-, selected-authority-, guard-, and hold-bound
authorization for the exact opaque prepared transaction. The stored sink policy reopens its causal
source closure and consumes that
authorization immediately before rename; exact no-op paths consume the same authority before
reporting success. Each typed adapter proves that its guard invoked and completed that exact
mutation once before retiring the preparation or returning success. Typed preparations are opaque
exact-object capabilities, so callers cannot
transplant a generic publication, source audit, stored policy, no-op binding, or equal-but-distinct
guard. Raw writer capabilities, copied, expired, or transferred authorizations, cross-shot targets,
changed sources, wrong-sink token transplants, and forged payload bindings do not publish. Typed
registries quiesce their locks before fork so clearing a payload proof cannot run a weak-reference
cleanup through an inherited foreign-thread lock, and fallible post-commit readback retains cleanup
authority without masking its primary failure.

This is not completion of prerequisite 2. Other sanctioned authority writers and their real inner
locks are not yet migrated, and there is still no fenced `shot-ledger/v2` writer, complete
authored/refobs/current/pending source graph, run-owned archive, capability-bound interruption
issuer, independent evaluator, terminalizer, public signal integration, owner-loss reconciler, or
authoritative `interrupted` reader/status. The HIR remains proposed and non-publishable; focused
publication regressions prove only this sink family, not the heterogeneous/crash matrix or the
fresh real-model seal.

#### Bounded legacy shot-ledger transport migration

The canonical `<shot>/shot.json` physical sink now has one typed transport. Preparation rereads
the current generation under the real ledger lock, merges the legacy mapping, and returns an
opaque exact-object capability bound to the creator process, thread, shot, predecessor, staged
inode, payload digest, and authority binding. Generic prepared-file and durable replace/remove
APIs reserve every `shot.json` and `shot.json.lock` destination, including re-rooted paths.
Isolated plan-consumer and unpublished-candidate projections must prove that their destination is
not the live shot namespace before writing their non-authoritative copy.

Commit acquires the shared shot-authority writer fence first. Builder publication then proves its
exact attempt/finalization guard, while acceptance proves its selected-authority and replay-input
generation. The typed sink finally enters the ordered `shot_ledger` rank, acquires the actual
nonblocking ledger EX lock, revalidates the live writer, performs the prepared CAS, and reads back
the exact digest. Preparation and blocking reads never retain the process-wide fork-acquisition
mutex; pending, active, validation, handoff, and cleanup descriptors remain fork-visible and
identity-bound throughout their lifetime. A replaced root, lock, predecessor, staging name,
parent, payload, writer capability, thread, or process fails closed.

This migration does **not** close accepted-build semantics. `_merge_payload` still accepts the
legacy `milestones`/`acceptance`/`runs` projection and arbitrary changed top-level mappings, and
public base `Ledger.save()` has no unit, finalization, or acceptance owner. An authority-binding
string and a successful physical fence are not `shot-ledger/v2` evidence. The next ledger step is
one semantic writer that derives the complete accepted member graph from selected authority and
terminal receipts; callers must not supply those rows.

#### Derived accepted-build index

That writer now exists. `shot.json` keeps its legacy milestone log, but its `accepted_build`
member is a `vfx-harness.shot-ledger/v2` value that only `shot_ledger_v2_derivation` derives.
Accepted layers are the leading layers of the selected DAG's stable topological order whose
current terminal receipt is `passed` and whose sealed outcome, composed script bytes, and legacy
ledger projection verify through the existing publication verifier; each row binds the
coordinator head's `layer_generation_digest`, the exact receipt digest, the composed script
locator and SHA-256, and the canonical sealed-outcome locator and SHA-256. The index binds the
coordinator head reference and the exact selection token, and refuses a head that does not
authorize that selection or lacks a binding for an accepted layer. Its `accepted_chain_digest`
is the same `acceptance-chain/v2` digest acceptance judges: both now derive their rows through
one `accepted_chain` module, so the ledger cannot carry a second projection of the chain.
Acceptance is bound only when a passing typed outcome names this exact bundle, view, and chain
and every moment has a durable evidence record whose canonical digest is the outcome's
evidence digest; acceptance publishes those records under the run's `evidence/acceptance/`
tree and names them in its ledger record. An outcome for another chain, a missing record, or a
partial prefix leaves `acceptance` null; a record that does not hash to its evidence digest is a
conflict.

The typed transport reserves the member: only an opaque index minted by the derivation writer
may replace it, hand-authored rows are refused at preparation, and ordinary publications carry
the stored member forward unchanged. Layer finalization derives the index in the same ledger
publication that projects its slot, taking the finalizing layer's row from its durable receipt
and sealed outcome because that slot is still in flight. Every layer with a durable terminal
receipt contributes that receipt-bound chain row whatever its status: a failed receipt binds
through the same receipt, predecessor-prefix, and sealed-outcome verification with the expected
status made explicit, so the chain digest never depends on the ledger slot the same publication
rewrites and a crash-resume derivation reproduces the first one byte for byte. The accepted
prefix ends in front of the first non-passed receipt. A preserved receipt reconciling under a
successor selection binds through the same contiguous lineage authorization the publication
verifier requires. A selected authority without an evaluated coordinator head cannot derive the
member and fails closed; fixtures below the coordinator boundary stub the derivation rather than
publish an index they cannot bind, and unit fixtures whose layer documents sit below the plan
gate (an unknown global capability, empty owned requirements) stub the republication for the
same reason, since the executable-layer parser refuses such authority. Every acceptance ledger
publication re-derives the index under the writer fence before staging.

Plan and JIT republication re-derive the member inside the authority-state transaction. Every
reader on the derivation path — receipts, sealed outcomes, lineage authorization, the selected
view — fails closed while a WAL is selected, by design, so the republication runs at the one
point where the successor is complete: after the successor coordinator head resolves and the
WAL is removed, while the committing thread still holds the exclusive selection lease. The
writer fence nests on that lease, the derivation resolves the successor selection, and the
member commits through the typed transport only when it differs from the stored one; an equal
member is left byte-identical. Layers whose terminal receipts the transition archived as
superseded fall out of the accepted prefix, preserved receipts bind through lineage
authorization, and a shot without a ledger receives one holding only the derived member.
The one remaining window — a death after the WAL clears and before the member commits —
leaves committed authority and a stale member that every reader refuses; recovery closes
it. `vfx recover-authority-state` republishes the member in both dispositions: a
roll-forward republishes it as part of the successor, and `already_current` re-derives it
and republishes only when stale, which is the one projection an already-current recovery may
write; its typed result records `accepted_build_projection` as `republished` or `current`. A
republication failure after the head commits reports that the head is committed and names
the recovery command; it never rolls authority back. Readers still never trust the stored
member: the reader re-derives under a shared fence and refuses a stale index. Callers never
enumerate accepted files; the merge of other legacy top-level keys and base `Ledger.save()`
remain unscoped, and the independent archive-reopening evaluator of prerequisite 7 is still
absent.

#### Run-owned archive and independent evaluator

Prerequisites 6 and 8 now have their mechanism in bounded form. One pure domain
classification turns a source's kind, locator, and exact bytes into its typed identity, and it is
the only implementation both the capturer and the evaluator call. Typed kinds decode strictly
(UTF-8, no duplicate keys, no non-finite numbers) and digest through a closed registry:
authority-state records through their own typed parsers, plan and view pointers through the
domain parsers and canonical JSON, any other schema-bearing object as canonical JSON. Every other
kind is an exact opaque byte stream, and failures map to the closed invalid-reason vocabulary.
The selected `state/authority-state/current.json` holds the coordinator head record itself, so
the current-pointer identity binds the head schema and the closure's head row is that head's
content-addressed object copy.

The capturer runs only while the caller holds the shared shot-authority writer fence. It walks the
closed families from the live shot — plan pointer, bundle manifest and its declared artifacts,
effective view pointer and its artifacts, amendment and resolution streams; `shot.json` with every
composed script, sealed outcome, and acceptance evidence member the stored accepted-build index
names, plus the judgment streams; the current head and the record graph it reaches (commit,
evaluation, intent, proposal, capsule set), the pending pointer and its graph, and every
work-unit state member — classifies each source, and copies its exact bytes into create-only
content-addressed storage under the target run (`archive/interruption/objects/<sha256>`). A
symlinked or unreadable source, a dangling pointer, or a graph the closure schema cannot
represent refuses capture rather than publishing a projection. Transcript frontiers derive from
the exact transcript bytes through one pure function shared with the evaluator, and those bytes
are archived too. Two captures bracket terminalization; an authority change between them is
unrepresentable and refuses. The closed archive manifest lists every archived object by
namespace, locator, byte count, and SHA-256, binds the observed closure digest, and the receipt
binds that manifest by reference and requires it to cover every closure source and frontier.

The evaluator derives `satisfied | failed` solely from the target run: it reopens the receipt
(without one there is no evaluation at all), the owner claim through the descriptor-bound reader
that joins it to the manifest, the authority observation, every transcript frontier record, the
archive manifest, any owner-loss observation with its reconciler manifest and prior-status
snapshot, and every archived object, whose bytes it re-classifies through the same domain function
and compares to the recorded identity. Every mismatch, absence, or stale reference maps to the
closed issue vocabulary the evaluation record enforces; the evaluator mutates nothing and never
reads the live shot tree, so a later valid authority change leaves a committed interruption
verifiable while a fresh live capture sees the new authority. The terminal commit path, public
signal integration, owner-loss reconciliation, capability-bound issuance, and authored-input and
`refobs-*` capture (prerequisites 4 and 7) remain open, and `interrupted` status publication and
authoritative read-back remain refused.

#### Terminal commit path

The root owner terminalizes while it still holds its exclusive run-owner fence. It requires a v2
`running` status that selects the held claim and refuses a non-running status or an existing
receipt, because terminal selection is exactly once. Under the shared shot-authority fence it
captures the before/after observation and archive, publishes the observation, transcript
frontiers, and archive manifest, mints the receipt from the recorded signal kind, publishes it,
and only then lets the independent evaluator reopen the archive. An unsatisfied evaluation stops
there: the run keeps its running status and its interruption authority is unavailable, which
the later owner-loss reconciler may complete from the prepared records. A satisfied evaluation
is published at its canonical locator, followed by an interruption summary that carries zero
legal transactions and no retry, resume, or dispatch authority, the artifact inventory, and the
terminal status, which replaces the exact `running` bytes observed at entry through an atomic
rename; the latest-run projection follows, and the terminalizer reads its own status back
through the authoritative reader. That reader selects the receipt and satisfied evaluation by
digest, re-evaluates the archive, and derives the empty legal-transaction set; a run whose
archive no longer verifies has interruption authority unavailable even though its status is
selected. A death before status selection leaves the run running with its prepared records and
the terminalizer refuses to mint a second semantic receipt. The lease is checked live before
the receipt and before the status are published, and nothing here mutates shot authority.

#### Root-owner boundary and the v2 run generation

The v1 run generation is replaced rather than kept beside v2. `run_artifacts.create` now
writes only the closed `vfx-harness.run/v2` manifest whose typed dispatch discriminant is the
sole source of the public command and owner kind, validated through the same strict parser the
owner claim reader uses; a prepared run has no status until it is owned. The direct-command
boundary creates the run, acquires the exclusive run-owner fence, publishes the v2 `running`
status that selects the claim, and installs SIGINT/SIGTERM handlers on the main thread that
record the first intent and request cancellation by raising; later deliveries are counted and
otherwise ignored so they converge on one receipt, and the handlers are restored on exit. On
success the boundary publishes the run summary and selects it as `passed`; on any other
exception it publishes the typed stop envelope (or, when that publication itself fails, a
harness-defect envelope about the failure) and selects it as `failed`; on a recorded intent it
terminalizes the interruption and exits with the receipt's code. A cancellation with no
recorded intent is a failure with exit 130, never an interruption. A stage inherited inside a
driver run publishes only its typed stop envelope; the driver consumes that prepared envelope
and selects every terminal status itself, and the whole-shot driver owns its run the same way
with the `driver` dispatch. `status.json` carries only the selected record locators and digests;
terminal diagnostics such as `terminal_cause` and `stop_class` live in `reports/summary.json`,
which the passed status binds by digest and promotion reads for the plan outcome. Prior-generation
runs fail closed in every reader.

#### Owner-loss reconciliation

`vfx reconcile <shot> --run-id <run>` is the explicit model-free boundary. It runs as its own
owned v2 run, reads the target's source-verified owner claim (a legacy ownerless run is not
reconcilable), returns the already selected receipt when the target is no longer running, and
otherwise attempts non-blocking exclusive acquisition of the exact recorded fence. A held fence
means the owner is live and nothing is written. After acquiring, it snapshots the exact running
status bytes as prior-status evidence, archives its own manifest into the target run, and either
completes an already prepared source-valid receipt without changing its bytes or captures the
current authority and transcript frontiers and mints one `owner_lost` receipt with unknown signal
and exit. The terminalizer then evaluates, publishes evaluation, summary, inventory, terminal
status, and the latest projection under the reconciler's lease, and reads the status back. Its
typed result carries `owner_live`, `already_terminal`, `completed_prepared`, or `owner_lost`.
Strict preflight now also proves the kernel-owned plan-consumer directory primitive on the
host and filesystem (`plan_consumer_directory`, probe revision 5).

#### Fork-safe descriptor ownership and kernel-proven consumer directories

The fork-visible descriptor registry treats every observation as tri-state. A slot that still
names its captured identity is live, `EBADF` proves it closed, and any other read failure is
unproven: it poisons the process for engineering, admission and cleanup retain it, and a forked
child that cannot prove inherited authority released terminates with exit 86. A persistent
identity-capture failure during adoption no longer leaves the caller's slot live, untracked, and
fork-inheritable: the slot is replaced with the private neutral identity under deferred signals
and closed exactly once, and a slot that cannot even be neutralized stays registered as unproven.
The run-owner claim file is allocated through the same armed acquisition, so a signal handler that
forks immediately after the create-only open finds the staging descriptor neutralized rather than
inherited. Lease release unlocks the exact fence and then neutralizes every lease slot through the
registry; retained authority, neutral, or unproven rows surface as one typed cleanup failure that
outranks substitution and operating-system diagnostics. Every other consumer of the neutralizer
(the ledger lock, plan-consumer transactions, and authority selection) reads only that typed
state instead of duck-typing a retained attribute or collapsing an unreadable slot into closed.

Plan-consumer scratch roots and every descendant directory are created through one kernel-proven
primitive. The already-held parent is marked with fanotify `FAN_REPORT_TARGET_FID` before
`mkdirat`; the resulting `FAN_CREATE` event must carry the parent's handle, the exact child name,
and the created directory's opaque file handle; the child is opened with `openat2` under
`RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS | RESOLVE_NO_XDEV`; and adoption
requires `name_to_handle_at` on that descriptor to equal the event's target handle. A name swap
between creation and capture therefore cannot turn a foreign inode into owned state, and the
proven staging inode reaches its final name only through a no-replace rename. This requires Linux
5.17 (or the target-FID backport), an unprivileged fanotify listener, and a local filesystem that
exports file handles. `ENOSYS`, `EPERM`, `ENODEV`, `EOPNOTSUPP`, a queue overflow, a malformed or
duplicate event, or a handle mismatch fails closed before any consumer write. Strict preflight
does not yet probe this capability; that report is a separate bounded step.

### Prerequisite implementation order

The current foundation is schema and refusal machinery, not a partly enabled publisher. The
following order is required before any production path may select `interrupted`:

1. **Finish root-owner authority.** Bind the canonical shot root, `runs/` directory, run root,
   `owner/` directory, fence, and claim by device/inode identity and prove their namespaces have
   no alternate names. Close every
   pre-lease and inherited-descriptor fork window, durably publish the new fence and its parent
   directory before publishing the claim, and derive `direct | driver` plus the public command
   from a closed typed manifest dispatch discriminant rather than invented `argv` positions.
2. **Install one shot-authority capture fence.** Reuse the descriptor-stable
   `state/authority-selection/selection.lock` as the shared exclusion boundary for capture and
   every sanctioned shot-authority writer. The global acquisition order is run terminalization
   lock, exact run-owner fence, authority-selection lock, sorted unit-state locks, shot-ledger
   lock, plan-resolution-event lock, judgment-debt-event lock, then
   judgment-payment-attempt-event lock. A writer that currently acquires one of those inner locks
   first must migrate; no second capture-only lock may create a competing serialization order.
   Ordinary writers receive only a process-bound semantic shared capability; the terminal
   capturer receives the exclusive capability. The lock registry is process-wide across threads
   so an at-fork child closes every inherited lock descriptor without `LOCK_UN`, invalidates every
   copied capability, and cannot pin or reuse the parent's transaction. The lease retains and
   revalidates the canonical shot-root identity as well as the permanent lock inode. Path aliases,
   shot-root replacement with a transplanted `state/` subtree, and a prepared write whose actual
   target belongs to another shot all fail. Inner ordering is coupled to the actual lock
   constructors and the exact bounded source closure; a rank marker in wrapper code is not proof
   that the underlying lock followed that order.
3. **Publish a closed accepted-build root.** Introduce strict `vfx-harness.shot-ledger/v2`
   authority with a selection token/head reference, one canonical accepted generation per layer,
   exact finalization receipt, composed script, outcome and applicable acceptance-evidence edges,
   and a derived accepted-member digest. Accepted layers retain the selected DAG's stable
   topological order rather than sorting display ids. The root binds the existing full accepted
   chain digest; it does not invent a second digest from a reduced layer projection. Capture
   derives the complete member set from that index; callers never enumerate accepted files.
4. **Close authored and construction inputs.** Capture exact `brief.md` bytes and the complete
   admissible `refs/` tree, including explicit absence and hostile namespace cases. A selected
   `refobs-*` witness binds both its typed registration record and exact crop bytes. A whole-frame
   reference or an unselected nearby crop cannot enter by proximity.
5. **Represent current and pending authority independently.** Current and pending roots each
   carry their own validity and reachability closure. They may share immutable nodes only through
   one canonical deduplicated record/member table; neither graph is inferred as a subset or
   extension of the other. Heterogeneous fixtures cover current-only, pending-only, shared-node,
   disjoint-node, malformed-root, and substituted-member cases.
6. **Capture into a run-owned immutable archive.** While holding the shared shot-authority fence,
   the capturer resolves live namespaces, opens and verifies every source, copies exact bytes into
   create-only content-addressed storage owned by the target run, and publishes a closed archive
   manifest. Evaluation reopens that immutable archive and its live-capture provenance. It must
   not rehash the later live shot tree and thereby invalidate a historical interruption merely
   because a subsequent valid plan, ledger, state, debt, or reference publication occurred.
7. **Make issuance capability-bound and references canonical.** Only the live root owner holding
   the exact lease and an owner-bound signal-intent capability may issue `SIGINT`/`SIGTERM`
   cessation evidence. Only a reconciler holding the exact acquired owner fence and its typed
   invocation capability may issue owner-loss evidence. Receipt, owner-loss, authority,
   transcript-frontier, and prior-status references use canonical derived locators. Optional
   checkpoint/journal/candidate/boundary context is either a closed parsed union with complete
   semantic verification or absent; arbitrary caller-provided references are forbidden.
8. **Land the evaluator, then the commit path.** The independent evaluator derives failure or
   satisfaction solely by reopening the complete archive graph. Only after it passes the
   corruption, race, fork, crash, and heterogeneous-graph matrices may the terminalizer publish
   receipt, evaluation, summary, inventory, terminal status, and latest projection in one
   exactly-once protocol. Public signal/cancellation integration and owner-loss reconciliation
   are last because they may only invoke this already source-verifying commit boundary.

Until all eight steps are implemented and validated, no public signal handler, reconciler, status
writer, inspection reader, or test helper may turn the foundation records into authoritative
interruption publication.

Signal-intent issuance (step 7, first half) landed with HIR-0174's batch: the root owner's
handler mints a typed `RecordedSignalIntent` (`domain/run_signal_intent.py`), `RunCancellation`
carries it, `terminalize_interruption` accepts only that record and refuses any other value, and
`test_run_interruption_issuance_boundary.py` pins the handler as the sole production minter.
The reconciler's owner-loss issuance was already capability-bound through the acquired fence.
Canonical reference locators for optional checkpoint/journal/candidate context remain open.

Run `20260902T185214Z-2d588f` (2026-09-03) proved the reconciler on a real dead owner: the
driver's `main` handled only cancellation, a strict-migration `ValueError` from an unreadable
selected view escaped, and the run stayed `running`; `vfx reconcile` selected `interrupted`
(owner lost) from the released fence. The driver now terminalizes any exception no stage
classified through `terminalize_failure` when the status is still running, exactly once
(`test_driver_terminalizes_an_unhandled_exception_as_failed_exactly_once`).

### Failure and interruption remain disjoint

New runs use a run-status schema whose terminal union is closed. Every state retains the exact
root-owner claim locator and digest so a reader can verify which invocation owned the transition:

- `passed` selects the existing accepted result and run summary plus the root-owner claim;
- `dry-run` selects the existing non-accepting run summary plus the root-owner claim and carries no
  acceptance, stop, or interruption authority;
- `failed` selects exactly one `vfx-harness.stop-envelope/v1` plus the root-owner claim, and
  retains HIR-0164's
  exactly-one-action invariant; and
- `interrupted` selects exactly one `vfx-harness.interruption-receipt/v1` plus one satisfied
  `vfx-harness.interruption-receipt-evaluation/v1` plus the same root-owner claim bound by the
  receipt.

An interrupted status forbids `stop_envelope`, `stop_class`, `cause_fingerprint`, and legal-action
fields. Its reader derives an empty legal-transaction set, `retryable = false`,
`resume_authorized = false`, and `dispatch_authority = false`; those are not caller assertions.
`exit_code` and `detail` remain operator diagnostics.

An interruption receipt cannot be wrapped in a generic engineering stop. If its publication or
read-back fails, the target run has interruption authority unavailable and authorizes nothing.
A separately invoked reconciler may fail in its own run under HIR-0164, but that failure cannot
manufacture terminal meaning for the target run.

### Root run-owner lifecycle

`vfx-harness.run-owner-claim/v1` is immutable and create-only. It binds the run id, public command
and invocation digest, owner kind (`direct | driver`), an unpredictable owner id, process-instance
audit identity, canonical fence locator and fence implementation, manifest identity, and claim
digest. PID and process-start data are diagnostic joins; the held fence is the liveness authority.
Audit time does not participate in semantic identity.

The claim reader accepts only the exact new manifest generation and derives command/owner kind
from its closed invocation shape. The canonical shot root, `runs/` directory, run root, `owner/`
directory, fence, and claim are all inode-verified. Creating `owner/` is durably synced through the
run root, and the create-only claim is publishable only after its staging alias is durably retired
and the canonical file has one filesystem name. A forked child closes inherited descriptors
without unlocking the parent's open-file-description lock and cannot use or release the creator
process's lease.

The root process creates the run directory, acquires the exclusive fence, publishes and reads back
the manifest and owner claim, then publishes `running` with the exact claim locator and digest.
The fence descriptor is close-on-exec/non-inheritable. A full-run driver may deliberately retain
ownership while launching stage subprocesses; inherited stages do not acquire the root claim and
cannot publish a run-level interruption receipt. Their existing typed failure channel remains a
narrow HIR-0164 boundary consumed by the root driver.

Owner state is derived rather than edited informally:

1. a valid claim not yet selected by `running` is prepared and inert;
2. `running` plus the held claim fence is live ownership;
3. a terminal status retaining the claim and selecting a source-verified terminal record closes
   the claim; and
4. `running` plus exclusive fence acquisition by the reconciler is proven owner loss.

The root owner keeps the fence through receipt/evaluation publication, summary and inventory
preparation, terminal status selection, latest-pointer projection, and read-back. Terminal status
is the commit point. A signal arriving after that point cannot change it.

### Signal semantics

The v1 vocabulary is closed to:

| Interruption kind | Required signal | Diagnostic exit |
| --- | --- | --- |
| `operator_interrupt` | `SIGINT` | 130 |
| `termination_request` | `SIGTERM` | 143 |
| `owner_lost` | null | null |

An exact POSIX supervisor wait status may be retained inside owner-loss evidence for audit, but it
does not change the `owner_lost` row: a released fence alone cannot prove which signal or normal
exit caused ownership to disappear.

The first SIGINT or SIGTERM records an in-memory owner-bound intent and requests cancellation of
the active AnyIO scope, SDK client, and owned child process tree through their supported
cancellation APIs. The OS signal handler itself performs no filesystem or domain work. The
outermost root-owner boundary then closes resources for a bounded interval and terminalizes from
the recorded intent. A cancellation exception with no recorded signal intent is not reclassified
as an operator interruption.

Terminal publication is serialized against signal delivery. Repeated SIGINT/SIGTERM while the
same intent is draining can only converge on the same receipt. If a second signal, SIGKILL, fatal
runtime failure, or power loss prevents orderly publication, the claim fence is released by the
OS and the owner-loss path applies. Unsupported signals are never guessed into SIGINT or SIGTERM.

### Strict interruption evidence

`vfx-harness.interruption-transcript-frontier/v1` binds every transcript that exists at the
boundary by shot-relative locator, byte count, SHA-256, last complete sequence, truncated-tail
observation, and terminal marker. Its state is exactly `closed | incomplete`. Only the live
transcript writer may emit `close`; an owner-loss reconciler snapshots existing bytes and records
`incomplete`.

`vfx-harness.interruption-authority-observation/v1` binds the exact selected-authority and durable
accepted-state source closure immediately before and after terminalization. The two semantic
digests must be equal. For later owner-loss reconciliation, this is the authority observed at
reconciliation time, not a claim about unknown crash-time state. The observation proves that the
terminalization transaction changed no authority; it does not claim the interrupted command had
never completed an earlier independently valid atomic publication.

The source closure is not a caller-selected projection. Its fixed families bind authored
`brief.md` and admissible `refs/` inputs; every selected `refobs-*` registration and crop; the
selected plan pointer, bundle manifest and members, effective JIT pointer and members,
plan-amendment and plan-resolution event inputs; the strict accepted-build index and every member
derived from it; and independently rooted current and pending authority graphs over coordinator,
work-unit, judgment-debt, and judgment-payment-attempt records. Shared immutable nodes appear once
in a canonical record table. Each optional source is explicitly absent or carries exact locator,
byte count, and SHA-256. Intrinsically typed records also carry schema and semantic digest;
validated opaque sources do not invent those fields; malformed but readable sources retain raw
byte identity and a closed invalid reason. An empty or arbitrary mapping cannot stand in for this
closure. An unreadable source cannot prove equality and therefore cannot satisfy evaluation.

The live resolver exists only at capture time under the shared shot-authority fence. The receipt
binds the resulting run-owned immutable archive manifest, not mutable shot paths. Historical
verification reopens archived bytes and capture provenance; current-live verification remains a
separate operation for deciding whether new work may start. This distinction prevents a valid
post-interruption authority publication from falsifying the already committed historical fact.

`vfx-harness.interruption-receipt/v1` binds:

- exact run, command, manifest, and owner-claim references;
- `operator_interrupt | termination_request | owner_lost` and its closed signal/exit mapping;
- terminalizer kind (`owner | reconciler`);
- the exact transcript-frontier and authority-observation references;
- an optional exact `vfx-harness.run-owner-loss-observation/v1`, required only for `owner_lost`;
- for owner loss, a typed prior-running-status evidence record whose content-addressed source ref
  reopens the exact `status.json` bytes observed after acquiring the fence and before terminal
  selection;
- no checkpoint, journal, candidate, or active-boundary reference until an owner-published closed
  context index and schema-specific parsers exist; and
- its canonical receipt digest.

The schema has no action collection, not even an empty polymorphic `StopAction` list. This prevents
a generic stop consumer from treating interruption as a degenerate failure transaction.

`vfx-harness.interruption-receipt-evaluation/v1` will be produced only by the separate
deterministic evaluator; the current foundation does not issue one. It must reopen the manifest,
owner claim, transcript frontier, authority observation,
optional owner-loss observation and prior-running-status snapshot, and receipt; verify every
schema, byte hash, digest, path, run join, signal mapping, and authority equality; and derive
`satisfied | failed`. The interrupted status
selects both immutable records by exact digest and is publishable only for `satisfied`.
Receipt time must not follow evaluation time, evaluation time must not follow terminal status
selection, and every selected status must not predate its retained owner claim.

### Owner-loss reconciliation

The explicit model-free reconciliation boundary targets one exact run and claim. It refuses a
non-`running` status. While holding the run terminalization lock, it attempts non-blocking
exclusive acquisition of the claim's recorded owner fence. A held fence means the owner is live
and reconciliation makes no write. PID absence, PID reuse, an old `updated_at`, or a quiet
transcript cannot override that result.

After acquiring the fence, the reconciler publishes
`vfx-harness.run-owner-loss-observation/v1`, binding the target run/claim, previous status bytes,
fence adapter and acquisition result, the distinct reconciler run id, a canonical archived
`vfx-harness.run/v2` manifest reference, and the observation digest. It then:

1. snapshots the exact running `status.json` bytes to a create-only content-addressed record and
   proves that record selected the same owner claim;
2. completes an already prepared, source-valid interruption receipt/evaluation for the same claim
   if they exist, without changing their bytes;
3. otherwise snapshots the existing transcript frontier and current authority and publishes one
   `owner_lost` receipt with unknown signal and exit status;
4. publishes and reads back the independent evaluation, summary, inventory, and terminal status;
   and
5. releases the fence only after the terminal selection and latest-pointer projection verify.

An unselected stop envelope, model output, checkpoint, or receipt from another claim is never
promoted by proximity. Repeating reconciliation returns the already selected exact receipt.
Conflicting prepared records, altered source bytes, or a malformed claim fail closed without
changing the target status. Reconciliation terminalizes generated run evidence only; it is not a
recovery controller or an authority-state transaction.

## Rejected patch-level alternatives

- Add `atexit` to direct invocations: it does not run for SIGKILL, fatal aborts, or power loss and
  supplies no proof that another owner is gone.
- Catch `KeyboardInterrupt` in more planner functions: SDK/AnyIO cancellation and process signals
  do not reliably unwind through the same lexical frame, and the result would still lack an owner
  lifecycle.
- Treat interruption as `harness_defect -> route_engineering`: operator intent proves cessation,
  not an engineering transaction or defect owner.
- Publish a stop envelope with `actions: []`: that weakens HIR-0164's exactly-one-action invariant
  and makes every stop consumer accept two meanings for one schema.
- Infer owner loss from PID absence, timeouts, or transcript silence: PIDs are reused, clocks move,
  and a live model may legitimately be quiet.
- Append `close` from the reconciler: it would falsely claim the transcript writer completed.
- Resume the SDK session after reconciliation: the receipt lacks HIR-0164's complete immutable
  checkpoint/session/budget authority and explicitly grants none.

## Validation matrix

| Fixture | Required result |
| --- | --- |
| `dry-run` reaches its normal terminal boundary | Status selects only the non-accepting summary and exact owner claim; no stop envelope, interruption receipt, or acceptance authority exists |
| Public direct and driver invocations with their real manifest dispatch shapes | Owner kind and command derive from the typed dispatch discriminant; positional or contradictory `argv` data is rejected |
| Direct subprocess blocked in an AnyIO/SDK-shaped wait, parent sends SIGINT | Exit 130; one valid `operator_interrupt` receipt/evaluation; summary and inventory exist; zero legal transactions; transcript truthfully `closed` or `incomplete`; authority unchanged by terminalization |
| Same fixture receives SIGTERM | Exit 143 and `termination_request`; otherwise the same closure |
| Cooperative signal during ordinary synchronous work | Same receipt shape as the async fixture; no special lexical-path schema |
| Full driver with an inherited active stage | Only the root claim may terminalize; the child cannot close the run or mint a second interruption |
| Fork-only child calls the inherited lease or exits while the root remains live | Child descriptors close without `LOCK_UN`; creator lease stays live and reconciliation is refused |
| Another thread forks while a different thread holds the shot-authority lock | The child closes every process-registered descriptor without unlocking the parent, inherits no usable capability, and cannot pin the lock after parent death |
| Fork races each point before lease registration and owner-claim publication | Child never retains a usable owner capability; parent ownership remains live or setup fails before `running` |
| Fence file or parent-directory durability fails before claim publication | No owner claim or `running` status publishes; exact failed durability operation is reported |
| Canonical `owner/`, fence, or claim gains another name or inode | Live owner and reconciler readers fail closed; a substituted directory cannot be verified through a stale descriptor |
| Repeated signal during cancellation or terminal publication | One semantic receipt and one terminal status, or later `owner_lost`; never conflicting records |
| Signal races an already committed pass or typed failure | The existing terminal commit wins; interruption cannot replace it; HIR-0164 failure still has exactly one action |
| Root is SIGKILLed after `running` | Status initially remains running; reconciler acquires the released fence and publishes one action-free `owner_lost` receipt |
| Reconciler targets a live owner | Fence acquisition fails and no target-run byte changes |
| PID is absent/reused or status is old while fence remains held | No reconciliation; PID and age have no authority |
| Crash after receipt/evaluation but before status selection | Exact repeat reconciles the prepared records; it does not mint a second semantic receipt |
| Transcript has a truncated final JSONL line and no close | Frontier binds exact bytes and reports `incomplete`; reader does not discard or embellish them |
| Claim, receipt, evaluation, transcript, authority source, or status digest is altered | Terminal reader and `vfx inspect` fail closed and expose no legal action |
| Authority changes during terminalization | Receipt evaluation cannot satisfy and interrupted status is not selected |
| Current-only, pending-only, disjoint, and shared-node current/pending graphs | Both roots are independently traversed; shared nodes deduplicate through the canonical table; omitted or substituted nodes fail evaluation |
| Live authority changes validly after an interrupted status commits | Historical inspection still verifies the run-owned archive; a new live capture sees the new authority independently |
| Caller forges SIGINT intent, owner-loss acquisition, reconciler invocation, or a context locator | Issuance refuses before receipt publication; structurally plausible caller assertions grant no capability |
| Concurrent sanctioned writer enters any captured authority family | Shared authority-selection fencing serializes the writer outside the fixed inner-lock order; capture is one complete before/after generation or refuses |
| Shot path alias, shot-root substitution with transplanted `state/`, or cross-shot prepared publication | One canonical shot identity is retained by the lease and checked at the actual write sink; the alias cannot create a second capability and the substituted or foreign target is refused |
| Independent-layer ids sort differently from stable DAG order | Ledger v2 retains the selected topological order, while duplicate, omitted, or reordered accepted rows fail source verification |
| Plan amendment/resolution, authority pending WAL, work-unit, debt, or payment-attempt bytes change | Closed authority equality fails even when plan and JIT pointers are unchanged |
| Legacy run has `running` but no owner claim/fence | New reconciler refuses migration rather than guessing that the owner died |
| Descriptor adoption cannot read the opened slot's identity | The slot is neutralized and closed once and admission is poisoned; a slot that cannot be neutralized is retained as unproven and a forked child exits 86 |
| Signal handler forks immediately after the create-only owner-claim open | The child observes the staging descriptor closed while the parent's claim publication completes |
| Lease release meets a corrupted fork registry or a substituted fence descriptor | The reconciled registry row stays visible through every neutralization, substitution never touches the reused slot, and retained rows surface as the typed cleanup failure |
| Plan-consumer directory creation races a name swap, or the host lacks target-FID reporting | Adoption refuses a handle mismatch, and the unsupported platform fails closed before any consumer write |
| Two layers finalize on the public pipeline, then a composed script or an acceptance evidence record changes | The stored `accepted_build` index lists the exact accepted prefix in selected order and the reader refuses it once re-derivation differs |
| A caller writes `accepted_build` rows, copies the derived index, or a failed terminal receipt reconciles | Preparation refuses the rows and the copy; the failed layer ends the accepted prefix instead of failing the publication |
| A process dies after terminal debt resolution and the layer reconciles again, with the terminal receipt failed or left due | The resumed reconcile publishes byte-identical `shot.json` and sealed-outcome bytes: the failed receipt's row binds through receipt verification, never through the slot being rewritten |
| A preserved terminal receipt reconciles under a successor selection | The row binds through the contiguous lineage authorization; an unauthorized token is a derivation conflict |
| Two layers are accepted, then a document successor transition changes the second layer's capsule | The same transaction that selects the successor head republishes `accepted_build` with the preserved first layer only, bound to the new head and token, and the reader verifies it at once |
| A process dies after the WAL clears but before the member republishes, or right after it does | Recovery reports `already_current` with `accepted_build_projection` `republished` or `current`, the member binds the current head and re-derives byte-identically, and repeating recovery leaves `shot.json` byte-identical |
| Two accepted layers with plan, JIT view, ledger, head graph, and work-unit state are captured under the fence and sealed as an operator interruption | The receipt's closure is present and valid in every family, the evaluator is `satisfied` from the archive alone, and appending a plan amendment afterwards leaves the evaluation satisfied while a fresh live capture yields a different authority digest |
| Authority changes between the before and after snapshots | Capture refuses; no observation, manifest, or receipt is minted |
| An archived source's bytes change or the object is removed; a transcript's archived bytes change; the archive manifest, authority observation, frontier record, or owner claim is rewritten | Evaluation is `failed` with the exact issue ids (`archive_object_mismatch`, `archive_object_missing`, `archive_manifest_mismatch` and `archive_ref_mismatch`, `authority_observation_mismatch` and `authority_ref_mismatch`, `transcript_frontier_mismatch` and `transcript_frontier_ref_mismatch`, `owner_claim_unverified`, `owner_claim_mismatch`, `owner_ref_mismatch`); whitespace-only claim rewrites mismatch only the byte reference |
| The receipt itself is rewritten or absent | No evaluation exists; the evaluator reports the run's interruption authority unavailable instead of minting `failed` |
| An `owner_lost` receipt binds a reconciler manifest and a prior running-status snapshot | Evaluation is `satisfied`; rewriting the snapshot fails it with `prior_status_snapshot_mismatch` |
| The root owner terminalizes an operator interruption of a v2 running run | One receipt, one satisfied evaluation, an interruption summary with zero legal transactions, the inventory, an `interrupted` status selecting receipt and evaluation by digest with exit 130, and the latest projection exist; a second terminalization refuses and the status bytes are unchanged; the authoritative reader re-verifies the archive and derives no retry, resume, or dispatch authority |
| The evaluator does not satisfy the closure during terminalization | The run keeps its exact running status and latest projection, the receipt exists, no evaluation is published, and the interrupted reader reports authority unavailable |
| The process dies after the evaluation publishes but before status selection | The run stays running with receipt and evaluation prepared; a repeat terminalization refuses a second receipt |
| An archived object changes after an interrupted status commits | The reader refuses with the closure no longer verifying; the status file alone grants nothing |
| The run has no status, a v1 running status, or the owner asks to terminalize `owner_lost` | Each refuses before any record is minted |
| A direct public invocation completes, fails, or receives SIGINT/SIGTERM in its body | The run is the v2 generation with an owner claim; `passed` selects the summary, `failed` selects the typed stop envelope and reads back through the claim, and a recorded intent yields an `interrupted` status with exit 130 or 143, a satisfied receipt, and a restored default handler |
| A second signal arrives while the first intent drains | One receipt, one terminal status, and the delivery is only counted |
| `KeyboardInterrupt` is raised with no recorded intent | The run fails with exit 130 and `cancelled_without_intent`; no receipt is minted |
| A stage fails inside a driver run | The stage publishes only its typed stop envelope; the root run's running status is untouched until the driver selects the terminal status |
| A reconciler targets a run whose owner still holds the fence | `owner_live`; no byte of the target run changes |
| The owner released its fence with the run still `running` | One `owner_lost` receipt with unknown signal and exit, a satisfied evaluation, an `interrupted` status with null exit, and the reconciler manifest archived in the target; a repeat returns `already_terminal` with the same receipt |
| The owner died after publishing receipt and evaluation but before status selection | `completed_prepared`: the receipt bytes are unchanged and the status selects them |
| The target is already terminal or has no verifiable owner claim | `already_terminal`, or a typed refusal for the legacy ownerless run |
| Strict preflight runs on a host without fanotify target-FID reporting or openat2 | `plan_consumer_directory` fails with the primitive's own diagnostic before any spend |

The key regression is a real subprocess test, not a raised exception inside the context manager:
enter the public direct invocation, publish kickoff, block in an external-session-shaped AnyIO
wait, send SIGINT from the parent, wait for process exit, and source-verify the complete selected
interruption closure. Separate injected-failure tests kill the owner, corrupt each source, race
terminal states, and prove normal passed and HIR-0164 failed runs retain their existing semantics.

## Migration

Implementation introduces a new run manifest/status generation rather than adding a second
meaning to `vfx-harness.run/v1`. The first slice lands strict domain parsers and a fail-closed
owner-fence foundation while interruption selection remains unavailable. Migration then follows
the prerequisite order above: complete owner namespace/fork/durability and typed dispatch; shared
shot-authority writer fencing; ledger v2; authored/reference/refobs and independent
current/pending graphs; run-owned archive capture; capability-bound issuance and canonical
context; independent evaluation; then terminal commit, root CLI signals, owner-loss reconciliation,
and terminal readers. The default writer changes only after direct and driver subprocess matrices
pass.

Historical v1 runs remain historical v1 records. A v1 `interrupted` run selecting a generic stop
envelope is not rewritten as evidence that no action existed. A v1 `running` run without the new
claim and fence cannot be reconciled, including the exposing run; inspection may label it a
legacy orphan but must not synthesize a receipt. New readers select behavior by exact manifest
schema and fail closed on hybrids, unknown fields, missing claims, or stale digests. There is no
filename, PID, or timestamp compatibility fallback.

## Release and rollback

Release requires pure schema tests, process-level SIGINT/SIGTERM tests, owner-loss and race tests,
tamper tests for every causal source, and unchanged HIR-0164 stop-envelope coverage. Operational
documentation must teach that interruption is terminal evidence with no next transaction, while
failed runs still route only through their selected stop envelope.

Once the new writer has emitted a run, rollback must retain its strict reader and inspection
support or stop creating runs until the newer runtime is restored. An older writer must not
overwrite or reinterpret the run. Rolling back signal ownership knowingly reopens the orphaned
`running` defect; it cannot be presented as a harmless behavior flag. No planning, build, or
judgment-debt authority needs reversal because this mechanism never mutates those boundaries.

## Remaining limitations

An interruption receipt proves how the invocation ended and that terminalization itself changed
no authority. It does not prove what the command was doing at the instant of death, undo an
already committed transaction, certify a candidate, classify a provider failure, or authorize
retry, resume, replan, engineering routing, or human escalation. Those decisions require their
own current authority and typed evidence.

SIGKILL, interpreter abort, host crash, and power loss cannot be handled in-process; they require
later owner-loss reconciliation. A remote provider may continue work briefly after local
cancellation, but a late result cannot publish through a closed or lost owner claim. V1 assumes a
local filesystem with a tested process-lifetime fence. Network filesystems, multiple execution
hosts, and platforms without equivalent lock and process-control semantics require a separately
validated backend and otherwise leave the run unclassified rather than guessing.

This HIR does not implement a daemon, automatic recovery controller, model-session resume,
distributed lease service, or retroactive repair of legacy orphan runs.
