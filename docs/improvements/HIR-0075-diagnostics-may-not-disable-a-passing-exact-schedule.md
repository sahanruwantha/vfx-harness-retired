---
id: HIR-0075
title: Diagnostics may not disable a passing exact schedule
status: proposed
introduced_in: unreleased
date: 2026-08-28
failure_class: diagnostic_contract_regression
mechanism: passing_schedule_override_guard
adr: null
---

# Diagnostics may not disable a passing exact schedule

## Observed failure

In build `20260827T230519Z-a187c2`, `light-energy-arc-schedule` passed at 30 W on
f72 and 450 W on f150. After density and normalized coverage had isolated insufficient
contract-scale signal, the builder muted the energy F-curve and tried 300 W, 30,000 W,
and 5,000,000 W through authored `run_bpy` mutations. Each attempt predictably regressed
the exact schedule, but the post-mutation contract report did not stop the next probe.
The same run also had to repeat a density sweep because the first requested range omitted
the restored live value 0.008 even though the black render and sweep diagnosis had already
closed density as causal.

## Root cause

Executable-contract evaluation taught only after mutation. It did not distinguish legal
rekeying from an unkeyed live override or curve mute once an exact schedule already passed.
The control-sweep API likewise treated caller values as the whole experiment rather than
including the value it promised to restore, so guard settlement depended on the model
copying the current float into its list exactly.

Run `20260828T001601Z-9ca8f6` exposed a second mutation surface: the builder obtained
the protected curves through `bvfx_fcurves` and changed `keyframe_point.co[1]` directly.
That bypassed the property-assignment and curve-mute checks, regressed three of four scene
contracts, and then required an exact restoration of the published schedule.

## General mechanism

When the active scene-contract set passes, `run_bpy` compiles the property paths of its
bound `keyframe_schedule` rows. A payload that mutes animation or assigns one of those
properties without keying it in the same transaction is refused before Blender mutation.
The rejection names the schedule ids and paths, routes normalized optical questions to
`light_coverage`, and routes a proven hard conflict to `cannot_express_in_scope`. Legal
authored rekeying remains available and is still judged by the exact schedule.
The same guard treats keyframe-point coordinate writes reached through `bvfx_fcurves`, or
directly through `keyframe_points`, as schedule mutation. A diagnostic cannot evade path
protection by editing the curve representation instead of the driven property.

Every `probe_control` sweep now includes the restored live value automatically. If the
public eight-value cap is already full, the closest proposed sample is replaced by the
live value so the range keeps its extremes and the transaction can settle its own guard.

## Rejected patch-level alternatives

Allowing a passing schedule to fail for a “temporary” authored probe leaves interruption
and journaling paths able to retain invalid authority. Prompting the model to restore the
curve repeats the mechanical work and does not prevent the next value. Treating any nearby
density sample as exact settlement would hide an unmeasured value; including the actual
live value makes the measurement explicit instead.

## Validation

Unit tests pin rejection of unkeyed protected-property assignments, curve muting, and both
helper-mediated and direct keyframe-point coordinate writes, while allowing a same-call
keyframe insertion. Probe tests pin current-value inclusion both below and at the eight-value
cap. The full suite and producing Blender diagnostics remain the release ratchet.

## Release and rollback

No persisted schema migration. The guard activates only after the active scene contracts
have all passed; initial construction and legal rekeying are unchanged.
