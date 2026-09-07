---
id: ADR-0012
title: Flynn owns execution durability; VFX owns production authority
status: accepted
date: 2026-09-07
supersedes: null
---

# Flynn owns execution durability; VFX owns production authority

## Context

The owner requested a VFX rewrite using Flynn, improvements in both repositories,
SQLite journaling, and a clean break without backwards compatibility. The
[repository investigation](../research/flynn-sdk-rewrite-boundaries.md) records the
starting architecture and the evidence behind the separation.

Flynn previously separated an in-memory state store and budgets from an optional
SQLite journal. A durable evaluation did not prove a state commit, reopening could
not restore spend, and satisfied tool output became state implicitly. These are
execution defects shared by VFX and ARC. VFX's work-unit acceptance, canonical replay,
claims, plans, scene ownership and publication rules are separate domain contracts.

## Decision

Adopt Flynn as the target execution kernel. Use SQLite for execution control state
and journaling, and design VFX's eventual control-state replacement around explicit
domain transactions. Do not treat this decision as evidence that the production
controller has already migrated.

| Concern | Owner |
|---|---|
| Operation identities, permitted dispatch, reservations, results, evaluation transport and recorded state revisions | Flynn |
| Tool registration, allowed roles/controls, policy changes, context relevance and selected feedback | VFX |
| Required-context fit and reliable transport of selected inputs | Flynn |
| Blender confinement, instruments, read-back, checkpoints and cumulative empty-scene replay | VFX |
| Plan/unit identity, authority generations, fault ownership, acceptance and receipt verification | VFX |
| Domain completion and whether external recovery is safe | VFX |

An SDK operation is not a VFX unit or ARC level. Successful execution, valid observations,
prediction judgments, proposed state changes and domain acceptance must remain distinguishable.
Flynn must support observation-only work without an artificial state commit. Its database
transaction can bind a recorded evaluation and state revision; it cannot certify a Blender
scene, an external artifact, or a VFX receipt. VFX owns the transaction that promotes its
domain evidence into authority.

Keep large scripts, renders and checkpoints external, referenced by verified identities.
Never claim a SQLite commit is atomic with a filesystem publication. Preserve the current
VFX publishers and independent readers until their replacement proves the same obligations.
During development, each authority surface has exactly one writer and one selected generation;
there is no production dual-write or fallback authority. JSON exports from a future SQLite
authority are projections, not an independently editable source of truth.

The target API and schema may break. Remove superseded SDK interfaces; reject unsupported
records explicitly. Existing accepted VFX artifacts remain untouched historical evidence.
Any later VFX digest/schema cutover must name the new generation and either migrate it through
an explicit verified transaction or refuse it. No heuristics and no indefinite compatibility
layer. The general preference for Claude-native orchestration does not govern this migration;
the existing Claude path retains its constraints until replaced.

SQLite ownership is local to one run and one live owner. It does not replace the shot-wide
authority fence. A missing external result remains indeterminate after restart. An explicit
SDK operation recovery is not permission for automatic VFX model-session resume.

## Implementation sequence and gates

1. **Execution foundation.** Replace Flynn's split stores with SQLite requests, operation
   reservations, dispatch/result records, evaluations and optional state commits. Prove
   terminal refusal before spend, same-process and subprocess ownership, and process-death
   behavior before/after dispatch and inside publication.
2. **Shared semantic contracts.** Complete independent assessments, structured selected
   feedback, required context, provider-neutral usage and phase termination. Migrate ARC's
   consumers explicitly. Install the exact pushed SDK commit over SSH and pass ARC's offline
   tests and replay assertions; backwards API compatibility is not the gate.
3. **One VFX unit.** After strict preflight, run a scripted executable-only unit through
   inspect, permitted mutation, read-back, freeze, confined cold replay and the existing
   receipt writer. Inject an unauthorized mutation, stale authority, false finish, replay
   failure and interruption. A diagnostic success alone is insufficient evidence.
4. **Dependent production work.** Exercise two dependent units and an independent branch,
   composition, mixed evidence media and one receipt-backed amendment. Preserve unaffected
   receipts and refuse stale evidence. Prove complete acceptance from an empty scene.
5. **Role and control-state cutover.** Replace planning, materialization, building, critique,
   repair and acceptance sessions; replace domain control-state publishers only with verified
   domain transactions. Remove the Claude dependency, hooks, old transports and obsolete
   configuration. Run full regression and architecture suites.
6. **Capability evaluation.** Compare heterogeneous held-out fixtures at equal budgets,
   reporting completion, quality, latency, context growth, spend and all failures. A new
   database alone is not evidence of smarter experiment selection or better VFX.

Each pushed shared SDK change must pass the ARC SSH installation gate before it is reported
complete. Extract scheduling, memory or confinement only when a concrete shared requirement
establishes the interface. Avoid moving VFX classes into Flynn merely to reduce harness size.

## Current evidence

Development branches are `codex/sqlite-runtime` in Flynn and
`codex/flynn-sqlite-migration` in VFX. The initial SDK implementation removes `Budget`,
`InMemoryStore` and `SQLiteJournal`, introduces `SQLiteRun` schema 2, and adds required
context and preservation of prepared tool schemas. Its offline suite currently has 88 tests.

Local checkpoint: SDK commit `f6b164efa03efe5472334983c7c4a740823d65fc` passes
88 SDK tests, Ruff, formatting, mypy, release metadata validation and the scripted reopen
example. Its wheel and source distribution build; the installed wheel passes all 86
unit/integration tests outside the checkout. VFX's complete source Ruff check and its 133
architecture tests pass. The full VFX behavior suite was not run for this documentation
and rule-scope change.

The initial commits were pushed on 2026-09-07. ARC's toy, visual and official consumers
now use schema 2 and explicitly treat prediction/observation assessments as observation-only
work. Official RESET runs through a scripted Flynn operation, so its reservation counts a
scripted inference-adapter invocation rather than a model request. Replay reads dispatched
schema-2 operations and rejects old generations. A small ARC reporting projection derives
execution counts from tool reservations rather than counting rejected inference proposals.

The required sync command in the real ARC checkout installed exactly
`f6b164efa03efe5472334983c7c4a740823d65fc` over SSH and pinned it in `uv.lock`.
All 82 ARC tests, Ruff and mypy passed. This includes deterministic replay equality,
restored operation budgets, terminal refusal and old-schema refusal. ARC already had substantial
uncommitted work; the migration was prepared in an isolated copy, tested, reviewed as an exact
12-file patch, and applied only after checking every original file hash. Preimages remain at
`/tmp/arc-sqlite-apply/before`. Its unrelated changes were not staged or committed.

VFX now declares the same exact Git dependency in its explicit `flynn` development extra
and a Python 3.11 minimum. VFX CI currently has no repository secrets; the extra avoids
breaking its existing credential-free installation. Migration tests explicitly skip when
the extra is absent. Both local migration environments install it from SSH; production
cutover must configure CI authentication and make this gate mandatory. Four offline
contract tests exercise the real VFX WorkUnit/scope compiler through Flynn: required context
cannot be omitted, an ungranted mutation is rejected, and scope evidence survives reopening
without becoming a unit acceptance or state commit. These tests do not execute Blender.

Strict `vfx preflight --strict` passed on this host, including a real confined Blender
boot, kernel fences and the plan-consumer directory primitive. Two further integration
tests dispatch a frozen fixture program through Flynn into the existing confined VFX
worker, measure it with VFX's scene-contract evaluator, and compare the observation with
a fresh worker's empty-scene replay through `_run_artifact_script`. One fixture satisfies
its declared count; the other deliberately fails it. Both preserve observation/evaluation
without accepting a unit. The first attempt correctly rejected an overbroad scene reset
inside the artifact; the corrected fixture leaves reset with the harness-owned setup.

These are six execution/context seam tests, not a complete production unit lifecycle.
Attempt-bound authorization, freeze, canonical unit receipt publication and composition
still need to be connected to Flynn. The current VFX controller and authority writers
are unchanged. No paid model calls were made.

Final follow-up validation: the complete current VFX collection ran in four isolated
pytest processes with separate temporary directories: **586 + 717 + 822 + 775 = 2,900
passed**. This includes all six Flynn seam tests. Ruff passed on the complete `src` tree.
The suite reported 15 existing Pillow `getdata()` deprecation warnings. The parallel run
replaced an interrupted serial attempt; no partial run is counted as full-suite evidence.

## Consequences

SDK durability defects can be fixed once for both harnesses. Domain acceptance remains
independently testable. The tradeoff is an explicit consumer migration and a measured staged
cutover rather than treating existing orchestration as a mechanical import replacement.

SQLite reduces cross-record publication gaps, but its hardware/filesystem assumptions still
apply; see the [SQLite atomic commit documentation](https://www.sqlite.org/atomiccommit.html).
The first implementation uses local rollback journaling with full synchronization. WAL mode
is a later measured concurrency choice, not a prerequisite for SQLite-based journaling.
