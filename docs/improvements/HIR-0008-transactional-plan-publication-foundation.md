---
id: HIR-0008
title: Establish run-owned plan publication and declared global-planner capabilities
status: accepted
introduced_in: unreleased
date: 2026-08-21
failure_class: mutable_shot_root_plan_authority_and_role_capability_drift
mechanism: immutable_plan_bundle_atomic_pointer_and_role_manifest
adr: ADR-0004
---

# Establish run-owned plan publication and declared global-planner capabilities

## Observed failure

Planning run `20260821T135111Z-f8e73c` exposed three related failures. Draft completion caught
invalid dependencies but the draft role had no `Edit` tool, so it rewrote the complete
`layers.json` artifact. Verify could not call the deterministic gate and declared claim closure
clean immediately before the gate found eleven claim-closure defects. Repair snapshots used
shot-global names such as `plans/global.round1.md`, allowing a later run to overwrite an earlier
run's evidence; moving `plans/global.md` to its draft name also left no published plan during part
of the run.

## Root cause

The plan stage used the mutable shot-root authority surface as both agent workspace and published
state. Its role capabilities were assembled through conditionals rather than a declared workflow
contract, and repair history had no run owner. The build stage's checkpoint/candidate/promotion
discipline had not been applied to planning.

## Decision criteria

- Previously published plan bytes must remain readable across incomplete, interrupted, tagged,
  or failed planning attempts.
- One publication operation must select one complete artifact generation.
- Historical repair inputs must be immutable and run-addressable.
- Draft, verify, and repair must all be able to patch and gate the transaction they author.
- The first slice must preserve current build consumers while making the strict ADR-0004 cutover
  explicit rather than silently claiming it is complete.

## General mechanism

- A clean untagged `--until-clean` run freezes the six current plan artifacts into a
  content-addressed bundle under `runs/<id>/checkpoints/plans/bundles/<hash>/`.
- `plans/current.json` atomically selects that bundle. Resolution verifies pointer schema, run
  ownership, every artifact hash, and the aggregate content hash; it never falls back after a
  malformed pointer.
- Candidate bytes are read in full before bundle or pointer mutation. Incomplete publication
  leaves the prior pointer unchanged.
- Repair inputs live under `runs/<id>/checkpoints/plans/snapshots/` and reject a conflicting
  rewrite within the same run.
- Global roles declare the verbs `author`, `patch`, `measure`, `gate`, and `escalate`. Draft,
  verify, and repair all receive `Edit` plus the bounded read-only `run_gate`; repair continues to
  deny delegation.

This is the publication and capability foundation from
[ADR-0004](../decisions/ADR-0004-transactional-plan-authority.md), not the full strict migration.

## Rejected patch-level alternatives

- Rename snapshots with timestamps at shot root: reduces collisions but leaves ownership and
  reader discovery implicit.
- Copy only `global.md`: readers can still combine contracts from another generation.
- Replace the six authority files sequentially: no sequence makes a multi-file generation atomic.
- Give only verify the gate: draft would still pay to create defects that executable validation
  can report while its context is warm.

## Validation

- Contract tests require immutable published bytes, prior-pointer preservation on incomplete
  candidates, run-isolated repair snapshots, and identical patch/gate verbs for all global roles.
- `.venv/bin/python -m pytest -q tests/unit/test_plan_authority.py` passes: 5 tests.
- `.venv/bin/ruff check src tests` passes.
- `.venv/bin/python -m pytest -q` passes: 75 tests.
- `.venv/bin/vfx --help` and `git diff --check` exit 0.

### Review follow-up

- `resolve_current` constrains a bundle to
  `runs/<run-id>/checkpoints/plans/bundles/<content-hash>/`, then cross-checks the path's run id
  against both pointer and manifest. It rejects traversal, arbitrary run artifacts, and
  cross-run pointer confusion before trusting artifact bytes.
- Write-time acceptance validation treats `UNMEASURABLE` as an error, matching terminal gate
  semantics. If a metric is legitimately unavailable for a still, the typed fingerprint must
  omit that metric id; writing an `n/a` value or inventing a numeric sentinel is not a valid
  claim.
- Against ADR-0004's acceptance list, this slice fully closes the role-capability and
  run-isolated-snapshot tests. It partially advances control closure, typed fingerprints, and
  interruption-safe publication, but their migration windows or compatibility readers still
  prevent those tests from being considered closed.

## Release and rollback

The new pointer is additive during this slice. Removing `plans/current.json` restores the previous
shot-root-only reader behavior; immutable bundles remain diagnostic run evidence. A later strict
migration will update all consumers in one cut and remove the compatibility surface.

## Remaining limitations

- Existing build/evaluation consumers do not yet resolve `plans/current.json`.
- Requirements, obligations, assumptions, migration sunsets, and verifier-output narrowing from
  ADR-0004 remain future slices.
- Gate warnings remain a flat list. Pattern-grouping and separation of migration chores from
  probable runtime defects remain an explicit warning-ergonomics item for the next slice.
- Publication currently runs in the planner process after the deterministic gate, not yet from a
  fresh independent commit subprocess.

The next prioritized slice is typed `requirements.json`, `obligations.json`, and
`assumptions.json` with dependency-outcome due gates. It enables mechanical closure for deferred
exact-return/final-lock evidence and provides the lifecycle substrate needed by migration sunsets,
honest assumption outcomes, warning grouping, and the narrowed adversarial verifier charter.

The formerly listed shot-root authoring limitation was closed by
[HIR-0009](HIR-0009-run-scoped-plan-authoring.md): global roles now operate in an
authored-input-only workspace inside the producing run.
