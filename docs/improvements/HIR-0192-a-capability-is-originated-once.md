---
id: HIR-0192
title: Two units declared the same capability and the accepted chain broke mid-layer
status: accepted
introduced_in: unreleased
date: 2026-09-04
failure_class: unordered_capability_declarers_break_canonical_replay
mechanism: a_capability_is_originated_once_and_every_other_declarer_depends_on_it
adr: null
---

# Two units declared the same capability and the accepted chain broke mid-layer

## Observed failure

Run `20260904T020917Z-7afeae` on `artifacts/room_1046_opening` rebuilt layer 1 after its
rematerialization. `cam_targets`, `cam_path` and `cam_lens` each PASSED. Building the next
unit, replaying the accepted priors:

```
running prior layer script cam_targets.py
running prior layer script cam_lens.py
CHAIN BROKEN — cam_lens.py no longer composes onto the scene built by the layers before
it: AttributeError: 'NoneType' object has no attribute 'data'
✗ layer 1 exited 4  ($5.91 spent)
```

The layer's declared edges:

```
cam_targets  depends_on=[]                          mutates camera.aim.*
cam_path     depends_on=['cam_targets']  provides=['camera']  mutates camera.rig
cam_lens     depends_on=[]               provides=['camera']  mutates camera.lens
```

`cam_path` creates the camera. `cam_lens` only sets its focal length, and declared no
dependency on it. Replay order is topological over `depends_on` with authored position as
the only tie-break (HIR-0119), so with no edge between them `cam_lens.py` replayed first,
onto a scene with no camera.

## Root cause

`provides` was treated as a capability *declaration* that satisfies a unit's own
requirements. AGENTS.md already says a typed producer capability is "additive mutation
authority, not a fallback label" (HIR-0112) and that camera availability comes only from
typed `provides` authority rather than from role names (HIR-0098) — but nothing said what
happens when two units in one layer declare the same capability.

Declaring `provides: ["camera"]` does not create a camera. One unit builds the host; the
rest modify it. Without an edge, the DAG believes they are independent and authored
position decides which script replays first — a decision no rule owns, and the wrong
answer breaks the invariant that empty-scene replay is the source of truth.

The failure is silent until replay: every unit passed its own evaluation, because each was
built on a warm scene where the camera already existed. Only the cold replay for a later
unit exposed the ordering.

## Decision criteria

- Ordering that decides whether a script runs at all must come from a declared edge, never
  from authored position. Authored order is a tie-break among genuinely independent units.
- Decide it before spend: the layer's own units and edges are enough, so this belongs at
  materialization and the plan gate, not at the builder's cold replay.
- Say it generally: nothing here is about cameras.

## General mechanism

`domain/capability_origin.py` derives, per capability declared in a layer, the declarers
that reach no other declarer in their dependency closure. Exactly one may — the originator
that creates the host. A second such declarer means the two are unordered, and the finding
names them and the two legal repairs: add the originator to `depends_on`, or drop
`provides` from the unit that does not create the host.

Both boundaries use it: the materialization validator (staging and finalize) and the plan
gate on published views, so a view carrying the defect cannot be selected and a session
cannot stage it.

## Rejected patch-level alternatives

- *Order the replay by `provides` as a heuristic.* Ordering would then come from a rule
  the plan never stated, and a genuinely independent pair would be serialised arbitrarily.
  The edge is the authority; the gate's job is to require one.
- *Let the builder retry with a different replay order on CHAIN BROKEN.* Guessing an order
  that the DAG does not express, after paying for a failed replay.
- *Forbid more than one `provides` declarer per capability per layer.* Too narrow: a unit
  that legitimately extends a capability may declare it, provided it depends on the
  originator. The defect is the missing order, not the second declaration.

## Validation

`src/tests/contract/test_capability_origin.py` — the exact observed layer shape is
refused; adding the one edge clears it; a transitive edge suffices; a single declarer and
a capability-free layer are clean; each capability is judged separately, so a clean camera
chain does not excuse an unordered geometry pair; three unordered declarers are all named;
and the rule is parametrised over unrelated capability names to show it is not
camera-specific.

Confirmed against the live shot: the plan gate now reports
`capability-origin | layer 1 | units cam_lens, cam_path`, owned by layer 1, so HIR-0190
dispatches layer 1's own amendment to repair it.

## Release and rollback

Adds a blocking predicate at two gates. A selected view carrying unordered declarers now
fails the gate until its layer is rematerialized — which is the point, and the repair is
dispatched automatically for a layer in range. Rollback is reverting the commit; no
durable state changes shape.

## Remaining limitations

- The rule proves that declarers are *ordered*, not that the originator genuinely creates
  the host. A layer whose only declarer creates nothing still breaks at replay; that would
  need write-cluster evidence that the unit instantiates rather than mutates, which the
  registry does not currently distinguish.
- Undeclared dependencies between units that share no capability remain invisible to this
  predicate; they surface at cold replay as before.
