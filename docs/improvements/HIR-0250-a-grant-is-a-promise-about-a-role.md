---
id: HIR-0250
title: A grant is a promise about a role, not a word a unit may repeat
status: accepted
introduced_in: unreleased
date: 2026-09-06
failure_class: a_rule_about_a_closed_set_was_written_against_one_of_its_members
mechanism: the_grant_to_role_check_enumerates_the_capability_vocabulary
adr: ADR-0011
---

# A grant is a promise about a role, not a word a unit may repeat

## Observed failure

ADR-0011 admitted `illumination` to `GLOBAL_SCENE_CAPABILITIES` and stated, as its third
decision point, that "a materialized unit on an illumination-providing layer must mutate
one of that layer's exact reserved illumination selectors, exactly as the camera grant
already requires."

That sentence describes a check the change did not add. Reproduced against
`validate_materialization`: a layer declaring `jit.provides.illumination = ["light.*"]`,
with a single unit declaring `provides: ["geometry", "illumination"]` while mutating only
`hall.mass`, validates clean. **DID NOT RAISE.**

The global promise was checked for presence and never for substance, so the provider the
plan promised could go unbuilt while the plan read as fulfilled.

## Cause

`GLOBAL_SCENE_CAPABILITIES` was `{"camera"}` for its entire life, so the grant-to-role
check was written as:

```python
if "camera" in unit.provides:
    camera_roles = global_capabilities.get("camera", ())
```

Correct when written, and indistinguishable from a correct rule until the set grew. The
capability's *presence* check next to it already ran over the vocabulary
(`set(global_capabilities) - unit_capabilities`), which is why the gap was invisible: one
half generalised itself and the other did not, and adding a member silently split them.

The root cause is not an omitted line in the ADR-0011 change. It is that **a rule about a
closed set was written against one of its members.** Landing `illumination` only revealed
it.

## Mechanism

The single `if "camera"` block was doing two jobs, and generalising it wholesale broke the
second capability in the opposite direction. They are now separate rules over separate
sets:

```python
for capability in set(unit.provides) & GRANT_REQUIRED_CAPABILITIES:   # {"camera"}
    if not global_capabilities.get(capability, ()):  ...              # must a grant exist
for capability in set(unit.provides) & GLOBAL_SCENE_CAPABILITIES:     # {"camera","illumination"}
    if reserved and not any(...): ...                                 # must a grant bind
```

**"Must a grant exist" is camera's rule alone.** Camera ownership is decided in the sparse
global DAG because every judged layer's closure must reach it. Illumination is deliberately
absent from `GRANT_REQUIRED_CAPABILITIES`: an emissive facade *is* the light and lives on
an ordinary look layer that reserved no light namespace, so demanding a grant there makes
the declaration unusable exactly where HIR-0234 introduced it.

**"Must an existing grant bind" is every capability's rule**, and checking it only for
camera is the original defect.

The first fix generalised both halves together and refused a legal unit-local emissive
facade -- caught in review before merge. Two questions that happened to have the same
answer while the vocabulary had one member are not one question.

Two camera-specific rules were deliberately left alone, because they are not this defect:
the camera-layer form-selector rule (`CAMERA_LAYER_DEFERS_SUBJECT_FORM_RULE`) and the
repair-owner rule at `validate.py`'s vis-coverage check, where a camera provider *observes*
`visible_fraction` rather than repairing it (HIR-0051). Those encode properties camera has
and illumination does not. An architecture test forbidding every literal test against
`unit.provides` was written and discarded for exactly that reason: it flagged both of
them, and a rule that cannot tell a grant check from a semantic one would have to be
suppressed at each, which is how a lint becomes noise.

## Validation

`src/tests/unit/test_plan_records.py`, parametrised over `GLOBAL_SCENE_CAPABILITIES` so
the next capability admitted inherits coverage instead of a presence check:

- `test_a_global_capability_grant_demands_a_mutated_reserved_role[camera|illumination]`
- `test_a_unit_honouring_a_global_capability_grant_is_accepted[camera|illumination]` --
  the accepting half, without which the refusal could pass by refusing always
- `test_every_global_capability_is_covered_by_a_grant_test` -- asserts the parametrisation
  covers the live vocabulary, so a member added without a reserved namespace here fails
  rather than quietly dropping out of the matrix

Verified both ways by restoring the camera-only form: `[illumination]` fails, `[camera]`
still passes. The regression reproduces the reported acceptance exactly before the fix.
