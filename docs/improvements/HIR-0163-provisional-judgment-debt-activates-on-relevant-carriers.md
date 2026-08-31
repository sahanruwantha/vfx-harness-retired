---
id: HIR-0163
title: Provisional judgment debt activates on relevant rendered carriers
status: accepted
introduced_in: unreleased
date: 2026-08-31
failure_class: provisional_judgment_billed_before_relevant_subject_exists
mechanism: typed_judgment_debt_definition_and_dag_compiled_activation
adr: ADR-0004
---

# Provisional judgment debt activates on relevant rendered carriers

## Observed failure

Run `20260831T060854Z-9274e5` completed the executable camera units, then the
HIR-0137 composed judge rendered an empty camera-only replay prefix and attempted to
judge eight image-domain `approved_start` requirements. HIR-0032 correctly refused to
invoke a critic on plates with no optical signal. Runtime converted that absence into a
`contract_gap`, discarded the useful layer boundary, and routed recovery toward global
replanning even though a later form layer was already the selected carrier provider.

## Root cause

HIR-0137 records the proposition and owner but not its subject, required carrier, or
first legal replay prefix. Runtime therefore reconstructs subjects from the currently
active layer's mutation roles and treats owner time as payment time. HIR-0160 checks
carrier presence for ordinary image-contract debt, but its current predicate does not
identify whether the carrier is relevant to the proposition. The two independent facts
"this executable artifact passed" and "this qualitative judgment is not yet due" have
no separate persisted states.

## Decision criteria

- The exact requirement proposition and its semantic and fault owners remain immutable.
- The materializer declares closed judgment semantics, exact subject selectors, owner
  moments, carrier families, and observation medium; it never authors `activates_at`.
- The harness derives the earliest relevant dependency-complete replay prefix from the
  selected DAG. An unrelated mesh cannot activate the debt.
- A selected DAG with no reachable matching provider fails before builder or critic
  spend.
- Executable artifact state and judgment-debt state are independent. A passed artifact
  may carry `pending_not_due` debt; final acceptance may not.
- No-signal plates never invoke the critic and never become qualitative falsification.
- Incorrect semantic ownership is a plan-authority defect. Activation cannot make a
  camera layer own hall appearance.

## General mechanism

Requirements authority migrates strictly to `vfx-harness.requirements/v2` and carries
two typed catalogs: immutable judgment-debt definitions and later exact activation
bindings. JIT materialization migrates strictly to
`vfx-harness.jit-layer-materialization/v2`.

The pure domain compiler creates a stable debt id, canonical definition digest, sparse
provider binding, and lifecycle state machine. Semantic matching uses the repository's
one dotted-role matcher. The compiler searches an owner's predecessor/current prefix,
then dependency-reachable successors in stable topological order, and requires every
subject selector to be covered by a permitted `mesh`, `volume`, or `compositor` carrier.
When the selected activation layer materializes, a second immutable record pins the
exact payer unit ids and unit digests. Runtime schedules only due definitions and records
one digest-bound terminal result. Selected authority is not itself a replay receipt:
the canonical verifier moves debt to `due` only after the actual prior-plus-current
empty-scene replay succeeds. Its receipt revalidates selected unit digests, durable
`passed` states, accepted checkpoint identities, and current unit-artifact hashes.

Every due qualitative observation compiles a sealed, pre-render
`vfx-harness.judgment-observation-request/v1`. It binds the exact definition and
activation generation, selected bundle and cumulative owner/payer view, ordered replay
receipt and parent chain, one declared judge point, reference bytes, medium/mode/scale,
typed evaluated Blender environment, promoted-construction provenance, comparison
configuration, and judge configuration. The Blender probe refreshes the requested frame
and resolves each subject only through its declared `mesh`, `volume`, or `compositor`
carrier family; an unrelated family cannot enter the digest or pay the debt.

The raster boundary returns a separate
`vfx-harness.canonical-render-capture/v1` receipt containing the actual worker frame,
mode, resolution, render/color settings, warnings, and candidate PNG digest. Successful
and negative eligible verdict evidence carries both records. A no-optical-signal result
writes `vfx-harness.judgment-payment-attempt-failure/v1` to an append-only current-
authority ledger and leaves the debt `due`. A direct restart must re-prove the replay
prefix and rebuild the pre-render request; an exact prior failure suppresses the raster
and critic. Any relevant request input change creates a different digest and permits one
new attempt.

HIR-0158 deferred bbox activation reuses the same provider-activation primitive so
scene-contract and qualitative debt cannot grow competing definitions of relevance.

## Rejected patch-level alternatives

- Treat black or empty plates as a critic failure: absence is not evidence and restores
  the pre-HIR-0032 false-pass path.
- Invoke the critic anyway: a score on no subject cannot certify the proposition.
- Mark every downstream mesh as relevant: an unrelated prop must not activate hall debt.
- Keep the camera layer open until geometry exists: that conflates executable publication
  with qualitative satisfaction and discards a proven checkpoint.
- Replan every `contract_gap`: no authority changed in the exposing run.
- Add an informal retry/controller first: it would automate the same misclassification.

## Validation

Before acceptance, focused fixtures must prove camera-to-matching-mesh activation,
unrelated-mesh-then-matching-mesh, no reachable carrier, volume, compositor, matching
ancestor carrier, aggregate dependency-complete subjects, strict digest round trips,
no-signal critic suppression, debt persistence across rematerialization, exact one-time
payment at the activation layer, and final acceptance refusal while persistent debt is
open. The key integration fixture is:

> Layer 1's executable checkpoint passes while its debt remains `pending_not_due`;
> Layer 2 activates and judges that exact debt once; global replan occurs only when
> authority changes.

The landed deterministic coverage exercises exact role-aware mesh activation,
unrelated-carrier exclusion, missing-provider refusal, ancestor and aggregate provider
closure, volume/compositor carriers, strict digest round trips, JIT camera-to-form
materialization, exact payer unit digests, non-camera mutation-owner closure, one-debt
composition scheduling, no-signal critic suppression, durable lifecycle transitions,
superseded payer generations, strict catalog linkage, and final-acceptance refusal.

The deterministic two-layer public-orchestration ratchet now publishes sparse authority,
materializes camera then form, passes the camera artifact with `pending_not_due` debt,
refuses a camera-only replay receipt, pays the exact debt once through composed replay,
survives a direct builder restart without repayment, and closes acceptance without a
global replan. Its failure branch produces one black canonical plate, makes zero critic
calls, persists one exact attempt, suppresses render and critic on an unchanged direct
restart, and permits exactly one new render after the typed Blender environment changes.
Successful payment evidence also carries the strict request and render-capture receipts.
A separate downstream-authorization fixture family proves that JIT publication, planner
kickoff, and materialization-stop classification reject a formerly passed dependency after
its script, reference, canonical render, receipt, or manifest changes. Producer-real raster
outcomes bind the actual render and capture bytes; executable-only camera/control outcomes
bind typed evidence without inventing a PNG. Replay prefixes follow the selected global DAG,
and semantic layer ids use one traversal-safe, injective locator family.
A fresh real-model run from brief through the public CLI and final Blender acceptance
remains required before returning to the chamber.

## Release and rollback

This is a strict migration. Requirements v1 and JIT materialization v1 fail closed and
must be republished/rematerialized; there is no optional-field interpretation. Rollback
requires restoring the prior schemas and republishes authority, and would restore the
known early-billing defect.

## Remaining limitations

The first implementation slice establishes immutable definitions, relevant activation,
exact payer bindings, durable cross-run terminal state, final-acceptance closure, and
no-signal safety. Sparse future-provider promises currently cover mesh; volume and
compositor carriers are supported by the universal compiler and exact materialized
providers but still need typed sparse global promises.

The pre-render observation request, carrier-aware evaluated-environment probe, canonical
render capture, and unchanged no-signal-attempt suppression are implemented. Terminal
classification now separates `EvidenceNotDue` continuation from closed typed stops and
binds every proposed recovery to exact transaction preconditions and a domain
postcondition (HIR-0164). Full deliverable rendering also requires a current passing
finished-chain acceptance outcome; forced and partial renders remain previews
(HIR-0165). Neither record implements recovery execution.

Remaining work is deterministic equivalence and successor-generation handling for
already `satisfied` discharges, failure reasons beyond `no_optical_signal`, explicit
same-bundle view lineage/retirement, cross-layer recovery transaction producers, the
fresh real-model two-layer CLI eval, transaction receipts and commit reconciliation,
and the deterministic dispatcher. These gaps remain tracked in
`docs/research/debt-activation-and-finding-dispatch.md`; the landed ratchet and typed
stop scaffolding are not permission to run the chamber again.
