---
id: HIR-0187
title: A plan-gate rejection is a typed transaction, not an unclassified boundary defect
status: accepted
introduced_in: unreleased
date: 2026-09-03
failure_class: deterministic_rejection_without_dispatchable_authority
mechanism: typed_layer_ownership_on_gate_findings_and_a_plan_gate_stop
adr: null
---

# A plan-gate rejection is a typed transaction, not an unclassified boundary defect

## Observed failure

Run `20260903T171506Z-4eb421` on `artifacts/room_1046_opening` (main `6f52b2a`,
`vfx run --from 2 --rounds 2`) ended in 7.9 s having spent `$0`:

```
status.json  state=failed exit_code=3
detail: harness_defect: The 'layer-2-plan-gate' boundary returned without typed stop
        authority. Route the boundary and exact attempt evidence to engineering.
summary:  stop_class=harness_defect  controller.dispatches=[]  refusal=null  cost_usd=0
stop-envelope.json: dispatch_mode=terminal_route -> sink engineering_handoff
```

The gate itself had been precise. It rejected the selected view `c07b7617…` with three
blocking findings, each naming its subject and its legal repair:

```
✗ [composition-coverage] layer 1 judge f200 subject layer 3
✗ [data-block-carrier]   layer 2 unit streetlight_flicker contract streetlight-flicker-schedule
✗ [data-block-carrier]   layer 2 unit light_reveal        contract reveal-light-keyframe
```

The two `data-block-carrier` rows are HIR-0185 firing correctly on a view published
45 minutes before that gate existed. Layer 2 was already in `materialized_layers`, so
the just-in-time step was a no-op, the gate rejected the same stale view, and the run
stopped. Every subsequent invocation repeated it exactly: a shot that could not
progress and could not say why in a form anything could act on.

Reproduced deterministically, free of model and Blender spend:

```
.venv/bin/python -m vfx_harness.evaluation.cli plan artifacts/room_1046_opening  # exit 3
```

## Root cause

Two defects on one causal chain, neither of them where the failure surfaced.

**1. Layer ownership lived in prose, not in the type.** `Finding.layer` was declared
`str | None = None`, so attribution was opt-in. Measured across the package: of 140
`Finding(...)` constructions, 34 interpolate a layer id into the human-readable `where`
string, and **33 of those 34 passed no typed `layer=`**. The owning layer was known at
the construction site — the loop variable was being formatted into the message — and
discarded from the value. `GateResult.clean_for(layer_id)`, whose entire purpose is
"another layer's findings must not block this layer's transaction", could therefore
never see ownership, so every such finding was silently promoted to a plan-wide block
owned by nobody. `GateResult.to_dict` dropped `layer` too, so no consumer could recover
it from the persisted report either.

**2. The most deterministic boundary in the system had no typed stop.** The driver ran
the gate as a subprocess and read only its exit code:

```python
rc = _run([py, "-m", "vfx_harness.evaluation.cli", "plan", str(shot.folder)], ...)
if rc:
    _stop_after_stage(layout, lease, rc, f"layer-{lid}-plan-gate")
```

`_stop_after_stage` consumes a child-prepared envelope and dispatches it. The gate
prepared none, so the run fell through to `missing_boundary_stop` and the engineering
route. Under ADR-0010 the controller dispatches only receipt-backed typed stops, so a
machine-decidable rejection — the one class of failure the harness can repair without
judgment — was the single class with no transaction attached.

The earliest owning decision is the first: ownership made optional on the finding type.
Everything downstream (`clean_for` over-blocking, the unusable report, the untyped
boundary) is a consequence of the gate knowing whose defect it found and having nowhere
to put it.

## Decision criteria

- Make the failure unrepresentable rather than detected: prose and type must come from
  one value, so they cannot disagree.
- The gate is free and exact. Its verdict is authority, not a diagnostic to be reduced
  to a POSIX exit code.
- Scope follows ownership, and no scope widens silently: a layer's own findings are its
  own transaction; a plan-wide finding stays a reviewed operator amendment (ADR-0010).

## General mechanism

**`Finding.in_layer(check, blocking, layer, where, what, fix)`** composes the rendered
`where` from the same value it stores in `layer`. All 33 sites were migrated to it; the
change is message-preserving, proven by byte-identical gate output on the failing shot.
`to_dict` now serializes `layer`, and the stop-report parser accepts and validates it.

An **architecture test** rejects any raw `Finding(...)` in the package whose `where`
interpolates a layer id without the typed field, so the attribution cannot regress
silently. It carries an injected violation proving the scan actually fails.

**`publish_layer_plan_gate_stop`** gives the boundary typed authority. The existing
global builder was parameterized by scope rather than copied. Before building a layer,
the driver now gates the selected authority in process and filters by ownership:

- no blocker this layer owns → build;
- every blocker owned by this layer → a `plan_gate` stop proposing one
  `publish_validated_amendment` on that **layer view**, which the controller dispatches
  under its existing caps, cause-fingerprint convergence, ledger and receipt proof;
- any plan-wide blocker → the **global** scope, which the controller refuses as a
  reviewed operator transaction.

No new transaction kind, no new authority, no special case for either failing check:
`publish_validated_amendment` was already legal from the `plan_gate` stage, and the
controller already knew how to run a layer rematerialization. The gate simply stopped
throwing its answer away.

## Validation

- `src/tests/architecture/test_gate_finding_layer_authority.py` — the scan reports 33
  offenders against the unfixed tree and 0 against the fixed one; plus an injected
  violation and `in_layer` composition cases.
- `src/tests/unit/test_run_shot_plan_gate_stop.py` — six ownership permutations:
  clean, layer-owned (the exact shape of the observed run), another layer's finding not
  blocking, plan-wide, mixed escalation, and mixed owners resolving to the layer scope.
- Message equivalence: identical gate output and exit code before and after migration.
