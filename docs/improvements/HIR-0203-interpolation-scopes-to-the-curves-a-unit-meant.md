---
id: HIR-0203
title: Interpolation scopes to the curves a unit meant, and the schedule guard names a legal form
status: accepted
introduced_in: unreleased
date: 2026-09-04
failure_class: a_protected_schedule_made_a_legitimate_edit_inexpressible_and_the_guard_advised_the_wrong_domain
mechanism: curve_scoped_interpolation_with_a_guard_that_names_the_scoped_call_form
adr: null
---

# Interpolation scopes to the curves a unit meant, and the schedule guard names a legal form

## Observed failure

Room run `20260904T143607Z-565c1e` failed terminal at layer 1, unit `camera_path`, after
$4.25 and 36 minutes of wall clock.

The builder read its own margins, found one thin (`cam-deriv-mid` at 0.0447 against a 0.05
ceiling), and wrote the edit that measurement implied: re-aim, key `rotation_euler` at frame
151, then re-interpolate. The guard refused it:

```
BLOCKED: required exact schedule(s) already pass: cam-lens-schedule. This payload would
disable or override protected animated path(s) data.lens without keying a legal schedule.
Do not use authored state as a diagnostic. Use render_pass(pass='light_coverage', light=...)
... if coverage is visible but the contract-scale beauty remains black after the density
branch is closed, call cannot_express_in_scope ...
```

The payload never mentions `data.lens`. What reaches it is `bvfx_interp(cam, mode='LINEAR')`,
which walks the whole host closure. The session then emitted nothing for 360 seconds, the
stream failed closed as indeterminate, and the run ended as an unclassified boundary defect
the controller correctly refused to dispatch.

The trap needs two conditions, both set by published authority before any builder runs: an
animated protected schedule on the camera data-block (`cam-lens-schedule` keys `data.lens`
32 → 30 → 28 across the push-in, bound to `camera.main.root`), and motion the unit must key
on that same host. Caesar is the negative control: its lens contract is a single-frame
`object_property` band satisfied by a constant 40 mm, and its motion is keyed on the rig
pivot, so the guard never armed and its three interpolation calls passed.

## Root cause

HIR-0074 deliberately made `bvfx_interp` traverse the same host closure `list_keyframes`
reports, so a curve reported as `data.P` is reachable without Python-host rediscovery. That
is right, and it is also the whole call surface: there was no argument for "these curves".
On a host carrying both a motion schedule and an optics schedule, "re-interpolate the motion"
and "re-interpolate the optics" are the same call, so a unit with a measured, contract-
motivated rotation edit had no legal form in which to express it. The guard was not wrong
that the lens would be touched; the tool left no way not to touch it.

The second defect is separate and worse in reach. The guard's remediation text is volumetric
density advice (`render_pass(pass='light_coverage')`, "the density branch", "beauty remains
black") emitted from a generic schedule-protection path that can fire on any animated host.
It reached a camera unit with `look_capabilities: []`, no lights, no volume, and no raster
debt at all. AGENTS.md requires a rejection to name the legal next action; this one named an
action from another domain, and a builder that followed it would render a light-coverage pass
on a scene with no lights.

## Decision criteria

- A guard may only refuse an edit the caller can express another way; where no legal form
  exists, the tool is missing an argument, not the caller a rule.
- A refusal names the call form that expresses the refused intent legally, in the caller's
  own vocabulary.
- Remediation text is derived from the payload and the unit, never carried over from the
  domain whose guard the code path was first written for.
- The unscoped walk stays the default, so HIR-0074's closure behaviour is unchanged.

## General mechanism

1. `_bvfx_interp(target, mode, const=..., data_paths=None, exclude_paths=())` filters the
   closure walk by `data_path` prefix. `data_paths` restricts, `exclude_paths` removes, and
   the default walks everything exactly as before. The compiled helper inventory in the unit
   card carries the new parameters, so a builder discovers them without `inspect.getsource`.
2. The schedule guard now names, in order: scoping the call to the curves the payload meant
   (printing `exclude_paths=[<the protected paths>]` and a `data_paths=` example), re-keying
   the protected schedule legally in the same transaction, or `cannot_express_in_scope`
   naming the conflict. The volumetric advice is gone.

## Rejected patch-level alternatives

- Letting the guard pass an edit that touches a passing protected schedule: that is the
  protection HIR-0075 exists for, and it caught a real overlap here.
- Telling builders to move motion onto a rig pivot: sound advice for a camera, useless for
  every other host with two schedules, and it is a technique choice rather than a mechanism.
- Refusing the plan shape (an animated `data.lens` schedule on a role a unit must animate)
  at materialization: worth considering separately, but it forbids a legitimate authored
  intent because a tool lacked an argument.

## Validation

- `src/tests/unit/test_scoped_interpolation.py`: `data_paths` touches only the named curves
  and leaves a protected `data.lens` schedule at its original interpolation and un-updated;
  `exclude_paths` touches everything else and still forces visibility curves to CONSTANT; the
  unscoped call is unchanged; and the guard's text names both scoped forms and
  `cannot_express_in_scope` while containing none of the volumetric remediation strings.

## Release and rollback

Additive parameters and a rewritten refusal message; no schema or authority change. A run
pinned to an earlier revision keeps the old behaviour, which is why the room run that failed
must be re-run rather than resumed to benefit.

## Remaining limitations

The run also sat for about 30 minutes between the stream failing closed at 360 s and the
driver terminalising, and its terminal advice ("raise the budget or split the layer") does not
describe an indeterminate session; both belong to the truncation path, not here. Whether a
`keyframe_schedule` animating `data.lens` on the same role a unit must animate for motion
should be refused at materialization is left open: with a scoped call form it is now
satisfiable, so it is no longer a trap.
