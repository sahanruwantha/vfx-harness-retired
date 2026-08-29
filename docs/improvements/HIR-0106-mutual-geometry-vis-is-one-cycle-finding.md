---
id: HIR-0106
title: Mutual geometry visibility is one cycle finding
status: proposed
introduced_in: unreleased
date: 2026-08-29
failure_class: duplicated_visibility_gaps_hide_unsealable_cycle
mechanism: geometry_visibility_strong_component_card
adr: null
---

# Mutual geometry visibility is one cycle finding

## Observed failure

Layer 2 rematerialization run `20260829T103330Z-66797b` authored separate blade
and rim-light geometry units with lifecycle-active visibility rows on both role sets.
HIR-0057 emitted four independent future-producer findings at 464.8 seconds: each
geometry unit protected the other's rows. The messages described valid repairs for an
acyclic gap but did not say that reordering could not satisfy both directions.

The materializer consequently tried removing a real geometry capability, changing
visibility lifecycle to an invalid enum, adding window fields to a still-layer row, and
adding those fields with the wrong meaning. It retired the rim pair only at 774.2
seconds, after five rejected writes and 309.4 seconds. The 24-turn session later
exhausted after staging its replacement control unit.

## Root cause

The validator computed the full producer graph but rendered each missing dependency
edge independently. A strongly connected component is a different design defect from
an acyclic future producer: no dependency ordering can make every member precede every
other member. Repeating the edge-level repair text made an impossible reorder look like
several local field repairs.

This is a missing graph instrument at the authority boundary, not a reason to weaken
visibility protection, lifecycle, or the unit DAG.

## Decision criteria

- Every geometry/visibility producer edge remains derived from HIR-0057's exact typed
  detector.
- A strongly connected component of two or more geometry units is reported once with
  all member units, protected contract ids, selected roles, and directed producer edges.
- Edge-level findings inside that component are suppressed; unrelated acyclic gaps
  remain independently reported.
- The finding states that reordering, narrow protects, lifecycle weakening, capability
  removal, and cyclic dependencies cannot repair the component.
- Legal recovery names the existing typed operations and gates: retire unpublished
  members in reverse dependency order, then consolidate the judged surfaces into one
  truthful derived write-cluster or use owner-granted existing dressable geometry.
- Materialization validation and the deterministic plan gate consume the same domain
  cycle detector.

## General mechanism

`geometry_vis_dependency_cycles` builds the directed sibling-producer graph from
`geometry_vis_dependency_gaps` and computes its deterministic strongly connected
components. Each component compiles a typed card containing sorted unit ids, contract
ids, roles, and producer edges. Materialization emits that card at `/layer/stages`; the
plan gate emits one blocking `geometry-vis-cycle` finding. Both suppress only the
edge-level rows represented by the component.

The existing acyclic HIR-0057 finding and publication rule are unchanged.

## Rejected alternatives

- Prompting the materializer to recognize cycles would duplicate a graph the validator
  already owns and would still require inference across truncated findings.
- Allowing per-unit protection lists to omit sibling visibility would reopen the
  premature-sealing defect fixed by HIR-0051.
- Reclassifying visibility as `window` or delaying activation to escape the cycle would
  weaken judge-frame authority rather than repair the DAG.
- Automatically merging units would cross the model's decomposition decision and could
  violate derived write-cluster atomicity.

## Validation

Domain fixtures prove that two independent geometry units produce one deterministic
cycle with both contracts, roles, and directed edges. Plan-gate and JIT materialization
fixtures prove that the component becomes one typed cycle finding, its internal
edge-level findings are suppressed, and restoring a real dependency returns to the
existing single acyclic HIR-0057 finding.

Producing-run evidence and broad regression results will be added after the Layer 2
rematerialization path exercises the cycle card.

## Release and rollback

No authority schema or migration. The gate remains equally strict; only the diagnostic
unit changes from repeated edges to their graph-level cause. Rollback restores repair
guidance that falsely presents a cycle as reorderable, so rollback is unsafe.
