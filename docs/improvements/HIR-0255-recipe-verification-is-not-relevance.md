---
id: HIR-0255
title: Recipe verification cannot create a query match
status: accepted
introduced_in: unreleased
date: 2026-09-08
failure_class: unrelated_verified_recipe_masks_scope_refusal
mechanism: require_query_relevance_before_verification_bonus
adr: ADR-0012
---

# Recipe verification cannot create a query match

## Observed failure

The native builder recipe integration test requested a recipe requiring lighting
roles from an executable unit owning only its declared control role. A second,
unrelated verified recipe remained in the cookbook. Instead of the expected
`out_of_scope` observation, lookup returned `found`. The failing focused run is
recorded in `/tmp/vfx-native-recipes-focused.log` (53 passed, one failed).

## Root cause

`recipes._score` added one point for verification even when no query term matched.
`search_recipes` admitted every positive score, so the unrelated verified entry
survived scope filtering and prevented the no-in-scope-hit refusal. Verification
status answered a different question from query relevance.

## Mechanism and ownership

Compute term relevance first. Apply the verification bonus only to a positive
query match. The shared lookup now preserves scope refusal when the matching
recipes are outside the unit's roles, regardless of unrelated verified entries.
Scope and ranking remain VFX policy; Flynn transports their structured observation.
No model instruction substitutes for the lookup rule.

## Rejected alternatives

Removing the unrelated fixture would hide the production defect. Returning the
highest verified recipe would preserve the same false match. Broadening mutation
roles to make the requested recipe legal would violate the unit's authority.

## Validation

The native integration case retains both entries and requires an out-of-scope
refusal without exposing the forbidden body, followed by successful retrieval of
the independently requested legal recipe and actual canonical Blender replay.
The corrected focused run passed 54 tests, followed by one owner-loss test. The full
frozen-source regression passed **3,598 tests**, with 121 Pillow deprecation warnings;
logs are in `/tmp/vfx-spike-regression-0uldrfo6`. Complete-source Ruff, public CLI
loading and strict preflight passed.
No live model judgment or recipe quality claim is made by these fixtures.

## Release and rollback

The ranking change applies to native and remaining legacy callers of the shared
lookup. There is no schema change. Reverting it would restore unrelated verified
hits and mask the same scope refusal.
