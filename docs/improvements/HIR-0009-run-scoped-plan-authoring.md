---
id: HIR-0009
title: Confine global-plan authoring to the producing run
status: accepted
introduced_in: unreleased
date: 2026-08-21
failure_class: prior_plan_state_leaked_into_fresh_authoring
mechanism: authored_input_only_run_workspace_and_write_scope_guard
adr: ADR-0004
---

# Confine global-plan authoring to the producing run

## Observed failure

A requested fresh invocation, planning run `20260821T151219Z-81c338`, started its model session
with the shot root as the current working directory. Its initial discovery read the previous
`plans/global.md`, contracts, unanswered questions, provenance, and earlier run reports. The
model consequently reported that no changes were needed. Because the prior `plans/global.md`
already existed, the write postcondition then retried the supposedly successful pass. The run was
interrupted and recorded failed before any new plan was accepted.

The first live validation run after staging was introduced, `20260821T152447Z-b82d87`, proved
draft and verify discovery were clean, then exposed a narrower escape during repair: the assigned
snapshot used an absolute run path, and the repair model inferred the shot root from it and read
the legacy shot-root plan/contracts directly. That run was interrupted rather than accepted.

The structured run layout had isolated generated output, but it had not isolated the planner's
input namespace or mutation surface. Calling the invocation “fresh” was therefore false.

## Root cause

HIR-0008 made publication run-owned while leaving SDK authoring on the mutable shot-root
compatibility surface. Run identity controlled where logs, reports, lab evidence, snapshots, and
bundles were written, but the model's `cwd`, plan tools, hooks, and gate still received the shot
root. Directory discovery silently promoted old generated state into the next run's context.

## General mechanism

- Each global planning invocation atomically stages `brief.md` and `refs/` into
  `runs/<run-id>/scratch/plan-workspace/`. No plan, contract, question, build, provenance record,
  or earlier run is copied implicitly.
- Draft, verify, repair, MCP planning tools, write-time validation, completion hooks, and the
  deterministic gate all resolve paths against that one workspace.
- A PreToolUse path guard confines Read/Glob/Grep discovery and all writes to the workspace. A
  repair receives one exact read-only exception for its assigned immutable snapshot; the
  snapshot's parent, shot root, sibling runs, and legacy authority remain inaccessible.
- Staged refs are copies, not hard links, so an in-workspace mutation cannot alter authored input
  through a shared inode.
- The final gate report retains the shot identity from `brief.md`, not the workspace directory
  name.
- Clean publication accepts staged bytes only from the exact workspace owned by the producing
  run, freezes them into its content-addressed bundle, and atomically updates
  `plans/current.json`. Failed and interrupted candidates never modify the pointer.

## Rejected alternatives

- Delete old shot-root artifacts before each run: destroys evidence and still leaves no explicit
  authoring boundary.
- Tell the model to ignore prior files: discovery and writes would remain technically possible,
  so freshness would still depend on prompt compliance.
- Copy every prior artifact into a new directory: changes the path, not the leak.
- Sandbox all reads to the workspace: prevents repair from reading the immutable snapshot it is
  specifically assigned. Mutation scope, not evidence access, is the required boundary.

## Validation

- Unit fixtures seed a shot with an old plan, contracts, questions, and run directory, then prove
  staging contains only the authored brief/reference inputs.
- Publication fixtures prove the bundle contains the staged candidate while the old shot-root
  plan remains unchanged, and reject an arbitrary alternate directory inside the same run.
- Hook fixtures prove only the assigned outside repair snapshot can be read, an inside plan can be
  written, and shot-root reads/writes plus traversing glob patterns are denied.
- Focused authority/planner suites pass: 26 tests.
- `.venv/bin/ruff check src tests`, `.venv/bin/python -m pytest -q` (79 tests),
  `.venv/bin/vfx --help`, and `git diff --check` pass.
- Live run `20260821T161447Z-db0932` denied the verifier's attempted
  `Glob ../*/plans/global.md` before execution, then completed `passed` / `clean` with zero
  blocking findings. Its five remaining findings are projected-composition warnings, not
  authority-isolation defects.
- The live run spent $14.4639899 across draft and verify. It published immutable bundle
  `176a7495d112f50a7b3eea889633b181e8d4c27c1546d8d0f73da51476d41a7b`; pointer resolution
  revalidated the bundle, and the staged/published `global.md` hashes match while the legacy
  shot-root `plans/global.md` hash remains different.

## Remaining limitation

This slice closes fresh authoring and publication input isolation. Existing build and standalone
evaluation consumers still read the shot-root compatibility files rather than resolving
`plans/current.json`; ADR-0004 therefore remains proposed. That consumer migration must be one
strict cut and must not be simulated by copying the new bundle back to shot root.

## Rollback and review trigger

The prior pointer and immutable bundles are unaffected by a staging failure. Reverting the
workspace routing restores legacy authoring behavior but is not a safe operational fallback.
Review this mechanism if a planner requires a prior generation: add an explicit, typed import
operation naming the bundle and artifacts, rather than restoring directory-proximity discovery.
