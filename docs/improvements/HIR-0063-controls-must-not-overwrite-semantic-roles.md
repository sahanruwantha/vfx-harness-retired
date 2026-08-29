---
id: HIR-0063
title: Controls must not overwrite semantic roles
status: proposed
introduced_in: unreleased
date: 2026-08-28
failure_class: control_tag_rewrites_role_identity
mechanism: independent_role_and_control_tagging
adr: null
---

# Controls must not overwrite semantic roles

## Observed failure

In run `20260827T195146Z-03eb25`, `lighting_atmosphere` created a light, tagged it
`bvfx_role=world.lighting_rig`, then tagged its control as `lighting_arc_intensity`.
`bvfx_control` internally called `bvfx_role`, overwrote the object's semantic identity with the
control token, and the scope checker immediately reported the new light outside its declared role.
The same ordering defect had earlier forced material nodes to reapply their roles after control
tagging during canonical distillation.

## Root cause

The helper treated role identity and control interface as aliases even though contracts, scope,
and compiled unit authority store them as independent fields. One host could not legally carry a
semantic role and a differently named control.

## Decision criteria

Existing object/material roles must be immutable under control tagging; untagged nodes must retain
legacy role=control behavior because node-role contracts address them that way; owner checks and
role-token validation must remain fail-closed.

## General mechanism

`bvfx_control` now validates and writes `bvfx_control` independently. It sets `bvfx_role` only when
the host has no role, preserving the convenient identity for shader/compositor nodes. An existing
role remains untouched, and cross-owner control retagging is rejected. The pure tag operation is
unit-tested outside Blender and called by the worker helper.

## Rejected patch-level alternatives

Reapplying `bvfx_role` after every control call makes correctness depend on call order and was the
exact recurring symptom. Giving objects control-shaped roles weakens scope and selector identity.
Removing node role=control compatibility would invalidate existing node contracts.

## Validation

`test_control_tag_preserves_existing_semantic_role`,
`test_control_tag_gives_untagged_node_legacy_role_identity`, and
`test_control_tag_refuses_cross_owner_retag` pin both legal modes and ownership refusal. Focused
Ruff and nine semantic/unit-scope tests pass.

## Release and rollback

No persisted schema migration; correctly tagged artifacts already carry both independent custom
properties. Revert worker and pure helper together only if a consumer is found to require control
tagging to rewrite an existing role, which would itself violate current scope authority.

## Remaining limitations

Legacy scripts that deliberately relied on call-order role replacement will now preserve their
first role and should be migrated explicitly rather than reintroducing ambiguity.
