---
id: HIR-0165
title: Deliverables require current finished-chain acceptance
status: accepted
introduced_in: unreleased
date: 2026-08-31
failure_class: render_published_without_current_acceptance_authority
mechanism: content_addressed_acceptance_outcome_gates_deliverable_render
adr: null
---

# Deliverables require current finished-chain acceptance

## Observed failure

The render boundary could prove that every ledger layer said `passed` and that its listed
scripts existed, then write the default full-shot output. It did not require a complete
passing acceptance result for the exact selected plan/view and accepted script chain.
An absent or stale acceptance, changed moment evidence, or a partial acceptance run was
therefore representable alongside a path that looked like a final deliverable. `--force`
and `--upto` also did not carry a distinct publication class in the default destination.

## Root cause

Acceptance was stored as mutable summary fields (`passed`, `total`, and per-moment rows),
not one strict outcome binding all selected moments to the authority and bytes that were
judged. Rendering checked layer status independently and had no content-addressed
acceptance token to revalidate. A layer-pass ledger proves executable publication, not
finished-chain visual acceptance.

## Decision criteria

- A default full render is a deliverable only after complete finished-chain acceptance
  passes for the exact current selected bundle, materialized view, and accepted build
  chain.
- Every selected acceptance moment and its render/reference evidence is covered. A pass
  count cannot hide a missing, reordered, changed, or failed moment.
- Changing selected authority, unit or layer script bytes, unit digests, accepted statuses,
  moment authority, canonical capture, render bytes, or reference bytes makes the outcome
  stale and refuses publication.
- A failed full acceptance is recorded as a failed outcome and exits unaccepted. It never
  becomes deliverable authority.
- Acceptance re-checks current judgment debt and acceptance-due authority at publication
  and again at final-render entry. A previously passing image cannot conceal debt that
  became due afterward.
- Partial and forced renders remain useful diagnostics, but their default outputs are
  previews under the current run's scratch tree. `--force`, `--upto`, or an explicit path
  does not promote their evidence status.
- Forced acceptance is likewise run-scoped debugging evidence: it writes no typed outcome,
  plan resolution, accepted-work supersession, repair route, or shot-ledger mutation.
- A diagnosed layer is not an exact revision-checked repair target. Acceptance failure
  cannot mutate unit state or start automatic repair on that approximation.

## General mechanism

Complete acceptance writes `vfx-harness.acceptance-outcome/v1`. It binds the exact
acceptance-authority snapshot, selected bundle digest, selected materialized-view digest,
accepted chain digest, and one `vfx-harness.acceptance-moment-outcome/v1` for every
selected moment. Each moment records pass/fail plus a digest over its canonical render
capture, candidate and reference bytes, typed metric readings, critic scores/result, and
typed contract readings. It also records the closed decision source (`critic`, `metrics`,
or `no_optical_signal`); an empty plate keeps the last value and makes no critic call.
Frame-less image contracts match their exact selected reference rather than being applied
to every acceptance moment, and repeated readings of one contract resolve only when every
applicable moment passes. Run locators, render warnings, issue prose, metric prose, and
contract error prose remain in a separate audit projection and do not create a new
semantic outcome. The outcome's pass is derived by logical AND over the full moment set.

Before a normal full render, the harness resolves current plan authority and the strict
consumer view again, reconstructs the selected moment set and accepted chain, hashes every
layer and unit script, and re-hashes every stored moment's render/reference/capture
evidence. It refuses absent, failed, incomplete, malformed, or stale outcomes and
independently requires every current judgment debt and acceptance-due record to be clear.
Only then does it replay the selected executable layer set in the global DAG's stable
topological order. Acceptance and final rendering share that resolver, and every layer
script crosses the evaluated-current-frame replay barrier instead of being concatenated
into Blender.

The deliverable renderer snapshots the exact accepted replay chain before launching Blender.
It copies every replay script and bound construction GLB into an immutable run-local tree,
pins the assets and authored/decision inputs, and records the selected artifacts, acceptance
evidence, ledger, and digest-bound work-unit checkpoint state. Blender imports construction
only through the verified snapshot sidecar and replays only those copied bytes. After encoding,
publication holds the selected-authority, work-unit, and ledger locks, revalidates the complete
snapshot and acceptance outcome, then tentatively renames the MP4. A failed postcondition restores
the exact predecessor bytes or exact absence; another writer's replacement is never deleted.

JIT materialization overlays all consumer artifacts, including `acceptance.json`; selected
moment loading, acceptance authority capture, and staged consumer views therefore read the
same content-addressed moment generation. Acceptance pins the selected bundle before
resolving its JIT artifacts and refuses a generation change before any ledger or resolution
write. Resolution rows bind that exact bundle, and concurrent resolution writers serialize
their read/merge/publish operation.

`--force` and `--upto` bypass deliverable publication authority only for diagnostic
previews; without an explicit path they write under `runs/<run>/scratch/previews/`, never
the deliverables directory. Full acceptance failure publishes the HIR-0164 typed terminal
boundary as `human_decision_required` because current diagnostics identify a layer but do
not prove an exact legal unit transaction.

## Rejected patch-level alternatives

- Treat all-passed layer statuses as final acceptance: executable checkpoints and visual
  acceptance are independent facts.
- Trust `passed == total`: mutable counters do not authenticate which moments or evidence
  were judged.
- Reuse an earlier passing acceptance after plan or script changes: it judged another
  authority generation or build chain.
- Let `--force` write a normal deliverable by default: an override is a debugging preview,
  not evidence.
- Automatically retry the layer named by acceptance diagnostics: a layer-level guess does
  not identify the exact fault-owning unit or authorize mutation.

## Validation

Focused acceptance tests prove a complete pass stores a strict round-trippable outcome, a
complete failure stores a non-passing outcome and raises a typed stop, partial acceptance
does not mint full-chain authority, each identity-bearing measurement changes the outcome,
audit-only prose and locators do not, malformed evidence fails closed, changed acceptance
evidence is refused, no-signal evidence invokes no critic, changed debt/due state is refused,
forced acceptance cannot mutate authority, and current unchanged evidence is accepted.
JIT fixtures prove a materialized moment reaches every consumer and a forged view digest is
rejected. Shared-chain fixtures prove dependency order and the replay barrier. Render-boundary
tests prove a normal full render requires that verifier while forced and partial renders bypass
it only as previews and default to run scratch rather than deliverables. Snapshot race fixtures
cover script, construction, asset, authored-input, checkpoint, ledger, and rename-seam mutation
plus exact predecessor/absence restoration.

This validation is deterministic and fixture-based. It does not claim a fresh chamber run
or end-to-end real-model acceptance/render evaluation.

## Release and rollback

There is no compatibility interpretation for an old acceptance summary: a full
deliverable requires a current v1 outcome and must run acceptance again when it is absent
or stale. Preview workflows remain available with explicit diagnostic intent. Rollback
would reopen publication without finished-chain authority and is therefore not a safe
operational fallback.

## Remaining limitations

The acceptance outcome authenticates the evidence and accepted chain that authorize the render,
and final-media publication now revalidates that authority and promotes atomically. The encoded
MP4 still has no immutable deliverable receipt that binds encoder provenance and output digest.
Acceptance diagnostics still do not produce an
exact revision-checked unit repair transaction, and HIR-0164 has no controller or automatic
dispatch. Fresh real-model CLI and chamber validation remain outstanding.
