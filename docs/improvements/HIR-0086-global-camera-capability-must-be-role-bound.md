---
id: HIR-0086
title: Global camera capability must be role-bound
status: proposed
introduced_in: unreleased
date: 2026-08-29
failure_class: self_certifying_scene_capability
mechanism: role_bound_global_capability_closure
adr: ADR-0005
---

# Global camera capability must be role-bound

## Observed failure

After HIR-0085, global planning run `20260829T040427Z-2ccfe6` again put façade
geometry in Layer 1 and camera in Layer 3. The sparse gate published that order as clean
after 543.7 seconds and $1.5484. Layer-1 materialization run
`20260829T041349Z-783b6c` eventually split the work into 13 atomic units and authored 54
scene contracts. At 930 seconds it cleared every camera-bootstrap finding by adding
`provides: ["camera"]` to `massing_tower_unit`, whose only mutation role was
`massing.tower`. Validation and the terminal gate treated that authored capability label
as camera authority even though global ownership had reserved no camera interface.

The session used 38 model turns, 952.1 seconds, $2.8569, and produced a 198,287-byte
transcript before stopping on stale unit state. No scene unit or checkpoint was accepted.

## Root cause

Camera availability changes the global DAG and is needed before the first judged unit,
so ADR-0005 says it is global authority. The sparse ownership mapping had no field for
scene capabilities. HIR-0085 therefore had to trust a materialized unit's `provides`
label. That made the dependency predicate self-certifying: any unit could relabel itself
as the missing camera after the global planning boundary.

## Decision criteria

The mechanism must reject the impossible order before materialization, bind capability
ownership to explicit semantic interfaces, avoid name or charter inference, preserve
small JIT context, and force both global promises and local implementations to close.

## General mechanism

Each sparse global layer now declares `provides`, a map from a closed scene-capability
registry (currently `camera`) to role selectors repeated verbatim in that layer's
`reserved_roles`. `{}` declares no new global capability. The global authoring validator
and deterministic gate compute capability closure through `depends_on` and refuse every
judged layer whose own/transitive closure lacks `camera`.

At materialization, a globally promised capability must be fulfilled by a unit. A unit
may declare `provides: ["camera"]` only when its global layer reserved camera and the
unit mutates one of the exact role interfaces bound to that capability. Thus a massing
unit cannot invent camera authority, while a camera bootstrap unit with an explicitly
reserved rig role can publish and make projection evidence legal for dependants.

This is a narrow ADR-0005 extension: capability identity and its reserved interface are
global because they alter the DAG; camera technique, values, contracts, and implementation
remain JIT decisions.

## Rejected patch-level alternatives

Trusting `provides` repeats the observed self-certification. Inferring camera ownership
from `camera` substrings violates typed semantic authority and fails for roles such as
`cam_rig`. Letting every root geometry layer create a temporary camera broadens scope and
breaks cumulative replay. Catching the missing camera in Blender remains too late and
does not repair the global DAG.

## Validation

Heterogeneous global-authoring fixtures cover a still product shot and a motion shot.
Injected failure moves camera provision to a later layer and is refused at authoring;
the deterministic gate independently rejects a generated selected view whose camera
closure was removed. Materialization fixtures prove both directions: an unreserved unit
cannot invent camera, and a globally promised camera cannot publish without a fulfilling
unit. The affected planning/materialization suite passes 120 tests.

On the production shot, the previously clean selected view is rejected in 2.5 seconds
with typed `global-capability` findings for every layer, instead of spending another
952-second materialization session. A fresh global plan and Layer-1 replay remain the
production acceptance test.

## Release and rollback

This strictly extends schema-5 JIT authority. Existing mappings without `provides` fail
closed and must be regenerated through `vfx plan`; no default or inferred capability is
accepted by the publication gate. Rollback would restore self-certifying materialized
camera ownership and is unsafe.

## Remaining limitations

The global capability proves ownership and dependency availability, not the Blender
implementation. The producing unit's required projection evidence still proves that an
active camera exists in cumulative replay. Additional shot-wide capabilities should join
the closed registry only after an observed dependency failure, not speculatively.
