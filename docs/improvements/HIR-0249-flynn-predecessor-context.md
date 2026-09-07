---
id: HIR-0249
title: Flynn must carry verified predecessor interfaces
status: accepted
introduced_in: unreleased
date: 2026-09-07
failure_class: dependent_unit_context_omitted_predecessor_authority
mechanism: compile_context_from_attempt_dag_and_verified_completions
adr: ADR-0012
---

# Flynn must carry verified predecessor interfaces

## Observed failure

The first dependency-context assertion in the real three-unit layer fixture failed:
consumer `depends_on` contained `producer`, but the model-visible predecessor list
was empty. Both preceding units had already earned native completion receipts.
`compile_unit_scope_for_shot` requires the DAG, durable state and completion
authorization as explicit arguments; the Flynn adapter supplied none of them.
The single-unit probe could not detect this omission.

## Ownership and mechanism

VFX derives source-verified completion authorization for the exact attempt's complete
DAG before creating the SQLite run or reserving inference. It supplies that DAG,
current durable state and authorization to the existing scope compiler. The compiler
projects only declared consumed interfaces; it includes no predecessor script or plan.
The SDK transports the bounded packet and retains execution records as before.
No shared SDK API or dependency pin changes.

Passing all predecessor scripts to the model would violate bounded context. Treating
a `passed` status string as sufficient would omit source verification. Reimplementing
the context compiler inside Flynn would move VFX authority into the SDK. None is needed.

## Verification scope

`test_flynn_dependent_layer.py` seeds planning and gate attestations explicitly through
test fixtures. It does not prove planner quality. From that point onward it uses the
real layer controller, Flynn executor, confined Blender, checkpoint/completion writers,
composed replay, layer evaluation and finalization readers; none is mocked.

The three units are authored consumer-first, with an independent unit and producer
following it. Native topological scheduling executes independent → producer → consumer.
The consumer declares a placement-control interface and reads the accepted producer
without mutating it. Every inference request checks its exact predecessor set, verified
status, consumed interface, absence of sibling plans and the 12,000-character cap.
Canonical composition checks all constituent scene claims, and a separate read-back
checks the producer and consumer positions.

Injected consumer execution failure must preserve earlier completion/checkpoint records
and publish no layer artifact or consumer completion. Injected producer source drift
must stop before consumer inference or SQLite creation, preserve existing receipt bytes,
and leave the independent unit's own receipt source-verifiable. Global due/completion
resolution must still refuse the layer while the producer source is corrupt.

An initial fixture script used a generator over `bpy.data.objects`, which confinement
correctly refused. The fixture now uses a permitted object lookup; no policy was widened.
Another initial assertion called the global due resolver to verify just the independent
unit. That resolver correctly refused the stale producer elsewhere in the layer. The
final fixture uses the exact independent-unit receipt guard and separately asserts the
global refusal; it does not weaken source verification.

## Validation results

The three focused scenarios passed in 96.51 seconds. On the final unchanged source,
the complete suite passed **538 + 775 + 782 + 819 = 2,914 tests** in four isolated
processes, with 15 existing Pillow deprecation warnings. Logs are under
`/tmp/vfx-sqlite-regression-aojvalo_`. Complete-source Ruff, `git diff --check`,
`vfx --help` and strict preflight passed.

A live DeepSeek runner is prepared at `/tmp/vfx_live_flynn_dag.py`, bounded to six
model calls per unit (18 total), 2,048 output tokens per call and the existing
per-unit Flynn budgets. Automatic approval review rejected dispatch because the
three-unit synthetic payload and paid-call scope exceeded the earlier single-unit
approval. No live DAG model calls ran. Explicit user approval is the remaining
live-model gate; this record accepts the reproduced context fix and offline evidence,
not a live multi-unit result.
