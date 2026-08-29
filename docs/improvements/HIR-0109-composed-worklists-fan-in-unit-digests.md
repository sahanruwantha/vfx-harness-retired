---
id: HIR-0109
title: Composed worklists fan in constituent unit digests
status: accepted
introduced_in: unreleased
date: 2026-08-29
failure_class: synthetic_composition_unit_has_no_digest_identity
mechanism: constituent_worklist_fan_in
adr: ADR-0002
---

# Composed worklists fan in constituent unit digests

## Observed failure

Layer 1 revalidation run `20260829T124508Z-bf6edb` reused three digest-matched passed
units and executed cumulative empty-scene replay. All ordinary contracts passed, but both
composed judge frames failed on the harness row
`L1@1._composition-builder-worklist-valid`: `asdict() should be called on dataclass
instances`. The run exited 9 with `layer 1 units passed but the composed verdict is
'failed'`.

## Root cause

HIR-0039 represents an executable-only composed canonical as a synthetic fan-in object.
HIR-0107 made worklist evidence derive its path from the active `WorkUnit` digest. The
composition object is deliberately not a `WorkUnit`, but the evidence path passed it to
`unit_digest` as if it were one. The type mismatch became a false invalid-worklist row
and blocked a valid replay.

Skipping worklist evidence during composition would hide a constituent unit's durable
unfinished work. Giving the synthetic object its own digest would create a worklist
identity that no builder session owns.

## Decision criteria

- A live unit reads exactly its own layer/unit/digest worklist.
- An executable-only composed canonical reads every constituent unit's exact digest-bound
  worklist in dependency order.
- The synthetic composition object never receives or invents a worklist identity.
- An invalid or unfinished constituent worklist still blocks composed acceptance.
- An absent constituent worklist remains non-authoritative, as it does in unit evaluation.

## General mechanism

The composition fan-in carries the immutable tuple of its actual `WorkUnit` constituents.
`_worklist_evidence` recognizes that typed fan-in boundary and recursively evaluates each
real unit. The resulting evidence rows keep the constituent ids and digests; ordinary
unit evaluation is unchanged.

## Rejected alternatives

- Suppressing the `TypeError` would silently discard authoritative builder state.
- Hashing the synthetic namespace would create an unowned, unstable identity.
- Reusing only the last unit's worklist would let an unfinished predecessor disappear at
  composition.
- Treating the failure as a plan/rematerialization defect would change shot authority to
  compensate for an orchestration bug.

## Validation

`test_lookless_composition_fans_in_each_constituent_worklist` reproduces the pre-fix
`L2@2._composition-builder-worklist-valid` row, then requires one row per actual unit:
an unfinished predecessor fails and a completed successor passes. The existing look-less
composition test also asserts that the fan-in carries the exact stage tuple.

Validation results:

- Before the mechanism, focused regression: 2 passed / 1 failed. The failure was the
  synthetic `L2@2._composition-builder-worklist-valid` row.
- After the mechanism, focused worklist and look-capability suites: 24 passed.
- Repository suite: 560 passed in 43.10s.
- `.venv/bin/ruff check src tests`: passed.
- `.venv/bin/vfx --help`: passed.
- Producing run `20260829T124954Z-c1c643` re-executed Layer 1 cumulative empty-scene
  replay on the current harness. It observed 9/9 bound checks at f1 and 7/7 at f240,
  passed both composed frames at 5.0 by `unit_executable_evidence`, and exited 0 in
  4.7 seconds. The pre-fix run `20260829T124508Z-bf6edb` observed the same ordinary
  contract counts but injected the false worklist row at both frames and exited 9 in
  4.8 seconds.

## Release and rollback

No persisted schema changes. The mechanism changes only composed evidence routing. A
rollback recreates false replay failures on every executable-only multi-unit layer under
HIR-0107 and is unsafe.
