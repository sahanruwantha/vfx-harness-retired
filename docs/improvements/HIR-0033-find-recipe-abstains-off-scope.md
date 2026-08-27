---
id: HIR-0033
title: find_recipe abstains when the hit requires roles this unit cannot mutate
status: accepted
introduced_in: unreleased
date: 2026-08-26
failure_class: off_scope_recipe_ranked_as_permission
mechanism: recipe_scope_against_mutation_roles
adr: null
---

# find_recipe abstains when the hit requires roles this unit cannot mutate

## Observed failure

Run `20260826T170413Z-ba2b4c`, unit `cam_spine` (mutation roles `cam_rig`,
blockout, iris). `find_recipe("camera parented spill lights")` returned
`camera-parented-spill-lights`. The builder then tried a temporary sun — a
scope violation. Keyword hits are not permission.

## Root cause

`search_recipes` ranked name/tag/body tokens only. Recipes are data; retrieval
did not close over the active unit's mutation roles. Classification: missing
instrument at the cookbook boundary. A lighting recipe that also tags `camera`
must not match a camera-only unit.

## Decision criteria

- Enumerate, don't imagine: the option space is the recipes whose required
  role globs match the unit's mutation roles.
- Abstain naming both sides: query and roles present (HIR-0018 class).
- Lighting/volume/material/compositor tags win over a co-tagged `camera`
  (spill-lights must not match `cam_rig`).
- Explicit `requires_roles` frontmatter wins over tag-derived globs.
- Unscoped search (no mutation roles) keeps keyword ranking for discovery
  tools and tests.
- API / workflow recipes (`blender-5-api`, `warm-session-probe-loop`) stay
  always in scope.

## General mechanism

- `recipe_required_globs` / `recipe_in_scope` / `out_of_scope_hits` in
  `knowledge/recipes.py`.
- `build_recipe_tools(..., mutation_roles=unit.mutates.roles)` filters
  `search_recipes`. Zero in-scope hits with out-of-scope keyword hits return
  `ABSTAIN: no in-scope recipe…` naming the hits and present roles.

## Rejected patch-level alternatives

- Prompt "do not pull lighting recipes": the tool still ranked them.
- Delete spill-lights from the cookbook: a later lighting unit needs it.
- A second, camera-only cookbook: one cookbook, scoped retrieval.

## Validation

- `tests/unit/test_recipe_scope.py`: spill-lights out of scope for
  `cam_rig` / blockout / iris; `camera-roll-rig` in scope; unscoped
  `make the city look real` still finds `night-city-field`; API recipe always
  in scope.
- `tests/integration/test_harness.py`: the same camera-only abstention.

## Release and rollback

No schema migration. Optional `requires_roles` on existing recipes. Rollback
is unscoped ranking, which again treats a lighting hit as builder permission.

## Remaining limitations

Tag-derived globs are conservative (emission-tagged city recipes look like
lighting when scoped). Explicit `requires_roles` is the override. Recipes
with no tags and no `requires_roles` stay in scope — add tags or the field.
