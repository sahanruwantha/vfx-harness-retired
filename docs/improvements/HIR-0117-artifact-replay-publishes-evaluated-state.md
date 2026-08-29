---
id: HIR-0117
title: Artifact replay publishes evaluated state
status: accepted
introduced_in: unreleased
date: 2026-08-30
failure_class: successor_consumed_stale_producer_world_state
mechanism: artifact_evaluation_transaction_barrier
adr: null
---

# Artifact replay publishes evaluated state

## Observed failure

Room 1046 held-out run `20260829T183607Z-5c15f2` replayed the accepted
`camera_target.py`, then built `camera_path` by reading the consumed target's
`matrix_world.translation`. Live work passed 23/23 contracts because an earlier
`inspect_scene(frame=1)` call had incidentally evaluated Blender. Candidate and canonical
replay ran the same prior and successor scripts without that incidental observation. The
successor therefore read the target at the origin instead of `(0,20,5.5)`, failed twelve
projection/distance rows, and emitted `hypothesis_falsified` finding
`hf-fbb680cf4b2acfa52761` after a 34-minute repair session and $3.49 repair cost. The
model's claimed contract collision was an inference from stale producer state, not a plan
defect.

## Root cause

Python script completion is not a Blender dependency-graph publication boundary. Direct
transform assignments can leave evaluated world state stale until the current frame and
view layer are refreshed. `_run_prior_paths`, candidate probes, revalidation, warm starts,
canonical replay, and ablation all executed raw script text and immediately allowed the
next consumer to read it. Correctness therefore depended on whether some unrelated tool
had evaluated the scene in between. Classification: canonical replay / interface
publication defect.

## Decision criteria

- Every authoritative artifact replay publishes freshly evaluated current-frame state
  before a successor script, interface check, candidate probe, or canonical judge runs.
- The barrier is deterministic, read-only with respect to authored authority, and never
  enters the mutation journal.
- Live prior chaining, disposable candidate replay, deterministic revalidation, failed
  artifact warm starts, ablation, and canonical empty-scene replay use one helper.
- The mechanism is independent of object names, semantic roles, layer count, and whether
  the producer uses animation, parenting, constraints, or direct transforms.
- A real post-barrier contract failure still fails closed; the barrier never catches and
  continues from a failing script.

## General mechanism

`_run_artifact_script` executes one artifact, then runs an unjournalled
`_ARTIFACT_EVALUATION_BARRIER` that resets `scene.frame_current` through
`Scene.frame_set` and updates the active view layer. `_run_prior_paths` uses it after
every member, so a multi-script chain publishes each producer before the next consumer.
All other artifact replay sites use the same helper. Composed layer artifacts embed the
same barrier after every constituent unit so standalone cumulative replay preserves the
unit interface boundaries rather than reducing the composition to raw concatenation.
This makes evaluated producer state part of the replay transaction instead of an
accidental side effect of observation.

## Rejected alternatives

- Patch the generated camera script to call `view_layer.update`: every successor can read
  world transforms, dimensions, drivers, or constraints; generated artifacts should not
  carry harness transaction mechanics.
- Tell builders to read `.location` instead of `matrix_world`: consumed interfaces may be
  parented or constrained, where local state is not the exported value.
- Refresh only candidate replay: live, revalidation, warm-start, ablation, and canonical
  would retain different scene semantics.
- Treat the resulting falsification as a genuine plan conflict: the same plan and script
  passed live; the contradiction was order-dependent instrumentation.

## Validation

- `test_artifact_replay_publishes_fresh_state_before_each_successor` proves the barrier is
  inserted between two artifacts, never journalled, embedded between composed units,
  and wired into candidate and canonical replay.
- The typed replan transaction consumed false finding `hf-fbb680cf4b2acfa52761` against
  exact bundle `d235ad49…`, invalidated only `camera_path`, and preserved accepted
  `camera_target`. Held-out run `20260829T192337Z-4909a3` warm-started the unchanged
  failed camera artifact after the new producer barrier: all 23 live rows passed, the
  candidate probe passed, and unit canonical passed every judge frame at 5.0. Initial
  composition then exposed the missing constituent barrier and failed closed. After
  extending the same mechanism to composition, producing run
  `20260829T192957Z-f915e1` replayed the cumulative artifact from empty and passed all
  seven frames at 5.0 in 1.3 seconds.

## Release and rollback

No persisted schema change. The existing falsification remains audit evidence but is
retired by the typed replan transaction. Rollback restores order-dependent replay and is
unsafe.

## Remaining limitations

The barrier publishes the current frame. Scripts that need a different temporal sample
must still declare and set that frame explicitly; temporal evidence remains responsible
for multi-frame evaluation.
