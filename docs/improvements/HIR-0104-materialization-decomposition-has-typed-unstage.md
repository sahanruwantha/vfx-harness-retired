---
id: HIR-0104
title: Materialization decomposition has typed unit retirement
status: accepted
introduced_in: unreleased
date: 2026-08-29
failure_class: unpublished_materialization_unit_cannot_be_retired
mechanism: revision_checked_materialization_unit_unstage
adr: ADR-0006
---

# Materialization decomposition has typed unit retirement

## Observed failure

Layer 2 rematerialization run `20260829T094605Z-a1b1cb` staged five units before
the cross-unit visibility gate proved that its decomposition could not publish. The
candidate was still unpublished scratch, but the only dedicated stage mutation tool
could add or replace a unit. After 327.6 seconds without a successful candidate write,
the materializer attempted to replace `/layer/stages` through
`patch_materialization`; HIR-0100 correctly refused that weaker write surface. The
session then exhausted its 24-turn budget after 1,211.9 model seconds and $3.3584.

## Root cause

The candidate transaction supported typed insertion and field repair but not typed
retirement. A decomposition decision can become invalid only after another unit makes
cross-unit ownership, dependency, interface, or visibility constraints concrete. The
model therefore had no legal operation that expressed "remove this unpublished unit"
without reconstructing the entire candidate or bypassing the staging gates.

This is an agent-tool policy defect at the unpublished materialization boundary. It is
not justification for weakening cross-unit validation, permitting whole-array patches,
or increasing the turn budget.

## Decision criteria

- Retirement is available only for an unpublished materialization candidate.
- The write shares HIR-0100's lock, observed-revision check, local gates, and atomic
  replacement; refusal leaves candidate bytes unchanged.
- A unit cannot be removed while a surviving unit depends on it or consumes one of its
  interfaces. The finding names those exact surviving units and the legal next action.
- Only contracts no surviving unit binds are removed. Requirement bindings lose those
  exact contract ids and are removed only when they become empty.
- Selected authority and durable work-unit state are never mutated.
- The model supplies one validated unit id, not a reconstructed stages array.

## General mechanism

`unstage_materialization_unit` is the inverse scratch operation to incremental staging.
Inside the same revision-checked candidate transaction, it parses the closed WorkUnit
rows, refuses surviving dependency or consume edges, removes exactly one unit, computes
contract liveness through `bound_claim_contract_ids`, prunes only newly unbound scene
contracts and empty requirement bindings, reruns the local staged-unit gates, and
atomically publishes the next candidate revision.

The materialization policy exposes this operation only in the JIT candidate session and
directs the model to retire units in reverse dependency order. It does not reopen the
generic `/layer/stages` patch surface.

## Rejected alternatives

- Allowing `patch_materialization` to replace the stages array would restore the gate
  bypass and lost-update surface closed by HIR-0100.
- Requiring a new materialization session after any late cross-unit finding would throw
  away valid staged prefix work and make complexity scale with total layer size.
- Silently cascading removal through dependants would let one model choice rewrite a
  larger decomposition than it named.
- Leaving contracts or requirement bindings behind would create ambiguous orphan
  authority; deleting every contract supplied with the unit would erase shared rows.

## Validation

The unit fixtures prove that exact retirement prunes the target's three unshared
contracts and now-empty requirement binding, advances the candidate revision, and
leaves no stage rows. A dependency fixture proves that a surviving dependant is named,
the removal is refused, and candidate bytes remain identical. Tool registration and
materializer policy fixtures prove that the typed operation is available without
reopening generic writes.

- Materialization and tool-policy suites: `100 passed in 5.48s`.
- Full repository suite: `552 passed in 41.73s`.
- `.venv/bin/ruff check src tests`: `All checks passed!`.
- `.venv/bin/vfx --help`: exit 0.
- Producing run `20260829T101336Z-ac6aa6` staged
  `iris_blade_geometry`, then received a later control-scope rejection at 405.4
  seconds. At 441.1 seconds it invoked `unstage_materialization_unit`; the transaction
  removed that exact unit and its four now-unbound contracts, reported zero surviving
  units/contracts/bindings, and advanced to a revision-matched clean candidate. The
  model then restaged a legal mesh-only unit. Typed retirement converted the previously
  terminal decomposition into a continuing transaction in 35.7 seconds.
- That producing run later reached a validation-passing candidate twice and then
  exhausted after repairing terminal gate findings. HIR-0027 left selected authority
  unchanged. Its repeated numeric JSON-pointer guesses are a separate feedback defect;
  they do not invalidate the retirement transaction's exercised behavior.

Implementation commit: `51388aa` (`fix: add typed materialization unit retirement`).

## Release and rollback

No published schema or authority migration. The mechanism changes only unpublished
scratch. Rollback would restore a state in which a valid late cross-unit finding has no
legal recovery operation, so rollback is unsafe.
