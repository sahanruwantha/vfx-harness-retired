---
id: HIR-0041
title: A shared role is not an inexact selector
status: accepted
introduced_in: unreleased
date: 2026-08-27
failure_class: many_hit_role_taught_the_wrong_next_action
mechanism: ambiguous_role_names_object_equals
adr: null
---

# A shared role is not an inexact selector

## Observed failure

Run `20260827T040204Z-5f4489` atmosphere builder called
`check_scene(kind=bbox, role=world.volumetric_haze)` and
`check_scene(kind=bbox, role=world.particulate_system)`. Both roles were
exact tokens. The first matched 9 hosts (haze domain plus shaft blockers);
the second matched 2. The error said "pass an exact role so the check has
one subject". The builder then addressed display names (`object=atmo_haze_domain`).
`list_keyframes(role=world.volumetric_haze)` could not list `hide_render` on
the blocker family, so repair 2 had to read the script to see the visibility
window.

## Root cause

`resolve_object` treats many-hits like a fuzzy selector. The legal next action
for a single-subject check is `object=` with one named host, not a "more
exact" role. `list_keyframes` used the same exactly-one gate, so a shared
mutation role could not be enumerated. Classification: teaching defect
(HIR-0018 miss path names both sides; the many-hit path named the wrong
next action).

## Decision criteria

- A shared exact role names the hosts and says pass `object=` with one of
  them. It does not say "pass an exact role".
- `list_keyframes` with a shared role lists every host, including
  `hide_render`.
- `check_scene` bbox/visibility/framing stay single-subject.

## General mechanism

`format_object_ambiguous` is the many-hit string. `resolve_object` raises it.
`h_keyframes` enumerates `pick_objects` hits.

## Rejected patch-level alternatives

- Prompt "use object= when a role is shared": the error already fired and
  taught the opposite.
- Collapse shaft blockers onto a different role in this shot: fixture-shaped.
- Make `check_scene(bbox)` union N hosts: ambiguous crop; not this failure.

## Validation

- `tests/unit/test_semantic_roles.py`:
  `test_shared_role_names_object_equals_not_a_more_exact_role`.

## Release and rollback

No schema migration. Rollback restores "pass an exact role" and
exactly-one `list_keyframes`.

## Remaining limitations

`inspect_scene(role=world.atmosphere*)` still misses; HIR-0018 already names
the roles that exist. Particle *projected size* for the critic's "cards"
complaint is a missing look instrument, not a selector bug (HIR-0042).
