---
id: HIR-0190
title: A blocking finding owned by a layer the run never visits had no dispatcher
status: accepted
introduced_in: unreleased
date: 2026-09-04
failure_class: orphaned_finding_builds_on_rejected_authority
mechanism: passedness_is_not_skip_authority_and_an_unreachable_owner_stops_the_run
adr: null
---

# A blocking finding owned by a layer the run never visits had no dispatcher

## Observed failure

After HIR-0189 scoped each gate to the layer that owns its findings, `vfx run
artifacts/room_1046_opening --from 2` built layer 2 on a camera the gate rejects. The
standalone gate still exits 3:

```
✗ [composition-coverage] layer 1 judge f200 subject layer 3: camera layer authors no
  persistent bbox_* row for hero_window.* at a shared judge frame
```

Layer 1 owns it. `--from 2` never visits layer 1, so ownership scoping filtered the
finding out of every gate the run did run, and nothing dispatched it. The build then
spent its session fighting the consequence: `check framing: ISSUES ✗ f150: only 12% of
hotel_corner_root bbox corners on screen`, because the sealed camera cannot keep the
subject framed and layer 2 cannot move it.

A full `vfx run` would not have caught it either. `_drive_layers` skips a layer whose
build receipt still verifies **before** it gates anything:

```python
if lid in done:
    ...
    if dispatch_layer is not None and lid in _receipt_backed_passed_layers(...):
        continue
```

## Root cause

HIR-0189 is correct and incomplete. Scoping answers "whose finding is this?" and
correctly stops the wrong session being blocked; it does not guarantee the right session
ever runs. Two paths let a finding fall through:

1. **A passed layer is skipped before it is gated.** A build receipt proves the layer's
   *work* passed against the authority of its day. It says nothing about whether that
   authority still clears the current gate — and gate predicates are added often
   (`composition-coverage` is newer than layer 1's view). Treating passed-ness as skip
   authority is the same "query, don't recall" failure as HIR-0187: a remembered verdict
   standing in for a live one.
2. **A layer outside the run's range is never a candidate at all.** `--from 2` cannot
   repair layer 1, so proceeding silently builds on authority the gate rejects.

## Decision criteria

- Every blocking finding has exactly one owner, and that owner's transaction must be
  reachable. A finding nobody can dispatch is worse than one that blocks: the run spends
  real money producing work that later acceptance must reject.
- A run may repair what is in its scope and must refuse what is not. Silently widening
  `--from 2` into a layer-1 rematerialization would supersede accepted units the operator
  did not ask to touch.

## General mechanism

**Passed-ness is not skip authority.** A `done` layer is skipped only when its receipt
verifies *and* `plan_gate.scoped_to_layer(gate, layer).clean`. A passed layer whose own
authority the gate rejects falls through to the gate boundary, which publishes its typed
stop and lets the controller dispatch the layer-view amendment — rematerialization
preserves exact unchanged units, so passed work that is still valid survives.

**An unreachable owner stops the run.** Before driving any layer, the driver names every
blocking finding whose owning layer lies outside the run's range and stops with a typed
stop and the exact remedy — widen the range so that layer's amendment can be dispatched.

## Rejected patch-level alternatives

- *Dispatch the lower layer's amendment anyway.* Silently rematerializing layer 1 under
  `--from 2` supersedes accepted units outside the requested scope. Scope belongs to the
  operator; the harness names what it needs, it does not take it.
- *Let the build proceed and rely on acceptance.* Acceptance would reject it, after the
  full cost of building every layer on a camera that cannot frame the subject. The gate
  already knows, for free, before any spend.
- *Drop `composition-coverage` for views published before it existed.* That is silent
  compatibility for a rule whose whole purpose is to catch this defect.

## Validation

`src/tests/unit/test_run_shot_orphaned_owner.py`:

- A finding owned outside the run's range stops it, naming the owning layer.
- The same finding owned *inside* the range does not stop it — that layer's own boundary
  dispatches the amendment.
- A plan-wide finding is not an unreachable owner; it blocks at the layer boundary.
- A clean gate starts the run; a full run reaching the owner does not refuse to start.
- `_layer_authority_is_clean` is false for a passed layer whose authority is rejected and
  true for one whose is not — the skip predicate itself.

## Release and rollback

Behavioural change to `vfx run` only: some invocations that previously started now stop
before spending, naming the layer to include. Rollback is reverting this commit; nothing
durable changes shape, and no state is migrated.

## Remaining limitations

- A run that legitimately wants to build on a lower layer's known-rejected authority (a
  bounded debugging experiment) must widen its range or use `--force`; there is no
  narrower override, deliberately.
- The unreachable-owner stop resolves to the global amendment scope, which the controller
  refuses as a reviewed operator transaction. That is correct today — repairing a passed
  lower layer supersedes accepted units — but it means the operator, not the controller,
  widens the range.
