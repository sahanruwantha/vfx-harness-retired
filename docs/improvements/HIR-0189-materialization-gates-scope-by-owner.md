---
id: HIR-0189
title: A layer's materialization was gated on another layer's finding
status: accepted
introduced_in: unreleased
date: 2026-09-04
failure_class: session_blocked_by_a_finding_outside_its_scope
mechanism: materialization_gates_scope_the_verdict_to_the_owning_layer
adr: null
---

# A layer's materialization was gated on another layer's finding

## Observed failure

Rematerialization `20260903T211552Z-8ad44d` on `artifacts/room_1046_opening` did its job:
it staged five layer-2 units, closed all 21 requirement bindings, reported no open
findings, and called `finalize_materialization`. The terminal gate refused:

```
── plan gate · room_1046_opening · 1 blocking ──
✗ [composition-coverage] layer 1 judge f200 subject layer 3: camera layer authors no
  persistent bbox_* row for hero_window.* at a shared judge frame
```

That finding belongs to **layer 1**. Layer 2's materialization has no authority to author
layer 1's camera framing — the camera is sealed and its units are accepted. The session
retried finalize against it five times before the run was stopped to stop the spend.

## Root cause

A gate consumer ignoring finding ownership — the same class as HIR-0187, at a different
boundary. `GateResult.clean_for(layer_id)` has existed for exactly this ("another layer's
state-progress findings do not block; they belong to that layer's own transaction"), and
`agents/planner/generate.py` already used it for unit-plan generation. The two
materialization gates did not:

- `agents/plan_tools/materialize_mcp.py` finalize tool: `if not result.clean:`
- `orchestration/jit_materialization/publish.py` terminal publication: `if result.clean:`

Until HIR-0187 gave findings a typed `layer`, `clean_for` could not have helped anyway —
33 of 34 layer-owned findings carried their owner only in prose. With ownership in the
type, the predicate works and the gates can honour it.

Both gates matter, and they must agree: had only the tool been scoped, a session would
attest CLEAN and publication would still refuse.

## Decision criteria

- A boundary must not spend a session's turns against a constraint that session has no
  scope to satisfy. That is a harness defect, not a hard problem for the model.
- Scoping narrows *ownership*, never severity: a plan-wide finding still blocks every
  layer, and a layer's own findings still block it.
- The finding another layer owns is not dismissed — it blocks that layer's transaction,
  and the layer where its subject activates.

## General mechanism

`plan_gate.scoped_to_layer(result, layer_id)` returns the findings one layer's transaction
is answerable for: its own, plus every plan-wide one. Both materialization gates decide on
that value, and the finalize tool renders its report and repair brief from it, so the
session is never shown a finding it cannot act on.

## Validation

`src/tests/architecture/test_materialization_gates_scope_by_owner.py`:

- Every function in either gate module that branches on a `GateResult`'s cleanliness must
  pass it through `scoped_to_layer` — an AST rule, so a new gate site cannot regress it.
  Reverting either call site fails it.
- Heterogeneous scoping: a layer sees its own findings and every plan-wide one; another
  layer's finding *and warning* are dropped; a layer with nothing of its own still fails
  on a plan-wide blocker; stats survive for the report.
- The injected failure: the unscoped result blocks on `composition-coverage`, which is
  what stopped the observed session.
