---
id: HIR-0147
title: Literal role selectors cover descendant namespaces
status: accepted
introduced_in: unreleased
date: 2026-08-30
failure_class: planner_runtime_selector_semantics_diverge
mechanism: shared_namespace_aware_semantic_matcher
adr: ADR-0003
---

# Literal role selectors cover descendant namespaces

## Observed failure

Room 1046's three camera-owned deferred bbox rows select role `building`. The producing
Layer 2 geometry truthfully tags only `building.mass.tower`, `building.mass.base`,
`building.roof.silhouette`, and `building.roof.sign`. Planning considered those child
roles overlapping producers and assigned the bbox debts to their dependency-complete
geometry unit. Blender evidence used plain `fnmatch`, where literal `building` matched
none of them. The control plane said the debt was payable while the instrument observed
an empty subject.

Adding an otherwise useless parent mesh tagged `building` would have made the check run,
but would measure a proxy instead of the composed rendered subject.

## Root cause

`plan_selector_declared` implemented dotted namespace ancestry while the shared runtime
`match_semantic` implemented only fnmatch. The same selector therefore had two meanings
on opposite sides of publication.

## Decision

- A literal dotted selector matches an exact tag and every dotted descendant namespace.
  `building` selects `building.mass.tower`; it does not select `buildingish.mass`.
- A wildcard selector keeps ordinary fnmatch behavior.
- Planning overlap delegates to the same matcher in both directions; it has no private
  namespace implementation.
- Blender probes, scene inspection, bbox/visibility evaluation, and role-addressed tools
  continue to call the shared matcher, so aggregate-subject evidence sees the same hosts
  that publication assigned.
- Tags remain one concrete dotted token per host. Namespace selection does not permit
  wildcard tags or comma membership.
- When a parent selector reaches multiple hosts, single-host tools use the existing
  ambiguity rejection and enumerate `object=` choices. Aggregate bbox and visibility
  instruments intentionally compose all matching rendered surfaces.

## General mechanism

`semantic_roles.match_semantic` is the sole selector predicate. It first applies fnmatch,
then—only for literal selectors—accepts a `selector + "."` prefix. The planning helper
calls that function rather than reproducing its rules. The Blender worker already loads
the shared module by path, so the change reaches isolated replay without importing the
application package.

## Rejected alternatives

- Retag one object as `building`: the deferred contract would measure only that host and
  hide other massing/roof surfaces.
- Change camera-owned rows to enumerate current child roles: that reopens sealed Layer 1
  and couples camera authority to a future layer's unit decomposition.
- Add a special `building` branch: namespace composition applies to every semantic role.
- Keep planning permissive and teach builders to duplicate tags: the runtime mismatch
  remains and one host can carry only one truthful role.

## Validation

Tests prove literal parent selection, exact and wildcard matching, boundary separation
from similarly prefixed tokens, parent collection over multiple child hosts, and parity
with planning overlap. Existing deferred-subject payment, visibility authority, and
atomicity suites remain green.

## Release and rollback

This is a selector-semantics migration without a schema version change. Literal parent
selectors may now return several hosts; single-host callers already fail closed on
ambiguity. Rollback restores the known planning/runtime divergence and is unsafe for
aggregate-subject contracts.

