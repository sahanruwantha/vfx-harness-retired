---
id: HIR-0150
title: Materialization roles encode one write namespace
status: accepted
introduced_in: unreleased
date: 2026-08-30
failure_class: materializer_repeatedly_submits_mixed_role_namespaces
mechanism: cluster_shaped_mutation_role_authoring
adr: ADR-0005
---

# Materialization roles encode one write namespace

## Observed failure

Room 1046 rematerialization twice submitted one geometry unit containing
`building.mass.*` and `building.roof.*`. The atomicity gate correctly rejected the unit
because those are distinct derived write clusters. In the first run the agent recovered
by splitting them. After a fresh sparse-bundle rematerialization, the same guess recurred
and consumed another staging turn before any candidate bytes existed. The same pattern
also occurred for `site.island` plus `site.streetlight`.

The one-cluster rule was prose in the tool description and a post-submission gate. The
tool schema still offered `mutates.roles` as an arbitrary array of absolute strings, so
the invalid combination remained representable.

## Root cause

WorkUnit's durable representation is necessarily a list of full semantic role selectors,
but exposing that storage shape directly as authoring input erased the cluster invariant.
JSON Schema cannot assert that every arbitrary string in an array shares its first two
dotted tokens. The agent had to remember and manually compare role namespaces.

## Decision

- The materialization staging tool no longer accepts absolute `mutates.roles`.
- It accepts one two-token `mutates.role_namespace` and relative
  `mutates.role_members`. `$self` denotes the namespace tag itself.
- A unit with no mutated roles omits `role_namespace` and supplies an empty member list.
- The tool adapter compiles relative members into the durable full-role WorkUnit shape
  before the locked staging transaction.
- The compiled roles are revalidated as exact/descendant members of the one namespace
  below the JSON schema for direct callers.
- The durable WorkUnit schema and selected plans remain unchanged. This is an authoring
  type, not a compatibility interpretation.
- `dresses` remains a typed exception handled by the existing atomicity gates; consumed
  interfaces remain read-only and cannot add a second mutation namespace.

## General mechanism

`work_unit_authoring_schema(clustered_mutation_roles=True)` replaces the free absolute
array with the cluster-shaped union. Because there is only one namespace field, a request
cannot express both `building.mass` and `building.roof`; writing `roof.silhouette` as a
member of `building.mass` compiles to `building.mass.roof.silhouette`, which remains in
the chosen cluster rather than granting roof authority.

The plan tool uses this strict authoring form. Other callers of the durable schema retain
the existing full-role representation unless they opt into clustered staging.

## Rejected alternatives

- Keep returning the same teaching rejection: the option space is knowable and the guess
  had already recurred.
- Infer a cluster from the first role and silently drop later roles: that loses authored
  intent and can publish incomplete work.
- Let the model declare a `family` string: authored labels are padding and do not derive
  mutation authority.
- Enumerate only current-shot namespaces: broad reserved patterns such as `site.*` make
  future two-token clusters intentionally JIT-owned; the structural relative form is
  general without shot names.
- Change durable WorkUnit roles: replay and evaluation need full selectors and have no
  authoring ambiguity.

## Validation

Schema tests prove clustered tickets accept one namespace, reject the legacy absolute
roles field, reject a three-token namespace, and compile relative members into exact
durable roles. A hostile-looking `roof.silhouette` member under `building.mass` remains
inside `building.mass` and cannot emit `building.roof`. Existing WorkUnit, materialization,
plan-session, and requirement suites remain green.

## Release and rollback

No selected-authority schema migration. Materialization agents see a new staging-tool
input shape and must use it immediately. Rollback restores a repeatedly observed guess
surface and is unsafe.

