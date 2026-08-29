---
id: HIR-0083
title: Work-unit atomicity is a derived publication predicate
status: proposed
introduced_in: unreleased
date: 2026-08-29
failure_class: heterogeneous_work_unit
mechanism: derived_write_cluster_gate
adr: null
---

# Work-unit atomicity is a derived publication predicate

## Observed failure

HIR-0018's `atmosphere_and_bloom` unit and later Layer 2 `lighting_atmosphere` charters
published two mutation families under one repair owner: compositor bloom with World-volume
atmosphere, and `world.lighting_rig` with `world.atmosphere_volume`. Sessions then guessed
host type, density-control identity, and which family a black plate belonged to. The same
shape is reconstructable without those runs: a unit whose `mutates.roles` span distinct
two-token namespaces is a heterogeneous publish.

## Root cause

Materialization and the plan gate had no computable atomicity predicate. Any authored
"coherent family" field would have been HIR-0017 padding: the materializer could satisfy
the lint by renaming the mix. HIR-0057 already showed the bar — a publication gate whose
predicate is computed from the candidate alone.

## Decision criteria

The gate must refuse mixed publishes by naming derived clusters. Exceptions must be derived
from existing typed fields (dressing, vis observation/protection, coordination, consumed
assembly interfaces). The materializer's only lever is to restructure the unit. Layer 2 is
not retrofitted; first acceptance materialization is Layer 3.

## General mechanism

From each candidate `WorkUnit` and its bound contracts the harness derives write-clusters
`(role_namespace × host_class × instrument_family)`. Role namespace is the first two dotted
tokens of each `mutates.roles` selector. Host class follows the resolved instrument family,
not a single unit-wide `provides` class. Instrument family is a closed map from bound
write-kind evidence resolved per mutation role. A namespace with multiple mutation roles
and any role lacking a write-kind family is unpublished (`unresolved_family`); one typed
role cannot lend its family to an untyped sibling. Residual `control` is only for a
single-role namespace. Unknown kinds fail closed. `dresses` does not create a write
namespace. Image-domain and `visible_fraction` rows do not create a write family. Bounded
coordination resolves only the interaction owner's exact declared controls through that
owner's `control_roles`; participant unit ids are never interpreted as role namespaces.
Consumed interfaces are read-only: listing a producer export role in `mutates.roles` is
`consumed_mutation`, and `depends_on` cannot strip a mixed cluster. Required claims share
one `repair_owner`, the same ownership model compiled into `fault_owner_options` (HIR-0056).

More than one write-cluster after exceptions is a blocking `unit-atomicity` finding at
materialization and the plan gate. The finding names the clusters and the exceptions that
were considered and insufficient. Authored `family` / `mutation_family` /
`coherent_family` / `primary_subject` fields are padding and do not publish.

## Rejected patch-level alternatives

Authored family strings, department taxonomies in core, scheduler heuristics, and a new
raw Read surface were rejected in
`docs/research/jit-work-unit-planning-proposal.md`. Prompt wording cannot make mixed
clusters unrepresentable.

## Validation

`tests/unit/test_atomicity.py` pins mixed lighting/volume and bloom/volume refusal by
named cluster, padding that cannot satisfy the gate, dressing and vis observation that
still publish, assembly that mutates only its own roles while consuming `instance_source`,
mutation of a producer export role (`consumed_mutation`), a fake dependency that cannot
hide mixed clusters, same-namespace light+volume unresolved with missing or partial write
coverage and mixed with complete write kinds, exact control-mapped coordination that cannot
be forged with participant strings, and the same mixed charter refused at the plan gate and
materialization write hook. HIR-0051/0057 vis fixtures remain the owning mechanisms for
volume-owned mesh vis and unsatisfiable geometry-vis DAGs.

## Release and rollback

Stricter publication validation; no WorkUnit digest-schema bump. Rollback removes the
finding and would restore heterogeneous publishes. Do not rematerialize Layer 2 to
satisfy this gate.

## Remaining limitations

Independently testable splits inside one namespace (blade vs frame both `asset.iris`)
remain a materializer judgment when write kinds do not distinguish them. First-pass
Layer 3 materialization under the gate is the held-out acceptance run.
