---
id: HIR-0155
title: Durable layer memory does not create runs
status: accepted
introduced_in: unreleased
date: 2026-08-30
failure_class: read_only_cross_run_state_creates_orphan_run
mechanism: explicit_shot_state_routing
adr: null
---

# Durable layer memory does not create runs

## Observed failure

The authoritative pointer for `shots/barrel_roll` selected run
`20260830T130421Z-97b4fd` even though no harness process remained. Its manifest recorded
a direct `layer-state` invocation from `tests/integration/test_harness.py`; both
`runs/latest.json` and `status.json` remained `running`, and the run published no summary.
The preserved manifest and status hashes are respectively
`10de346d79de986e18f4ba08e2f27d2b74dca8c91c29d009e25f895dd29ff7ea` and
`20679edbf1a232f57bb833057d77f69e28cc55352bb29381e753b2d7334904df`.

The narrow reproduction called `layer_state.load()` on a fresh temporary shot with no
active run. A read returned `{}` but also created `runs/latest.json`, left the new run at
`running`, and resolved the durable path as
`runs/<run-id>/state/layer_state.json`. The read therefore displaced the latest
production pointer while the memory it was meant to preserve could not survive a new
process or run.

## Root cause

`layer_state.path_for()` used `run_artifacts.ensure(..., command="layer-state")`.
`ensure()` is an output-writer boundary: without an inherited invocation it creates a
structured run and publishes a running latest pointer. A state-path query has no matching
invocation context, so nothing finalizes that run. Tying the path to the returned run root
also contradicted the existing `shot/state/` cross-run authority.

## Decision criteria

- Reading durable memory must be observationally pure with respect to run authority.
- Cross-run memory must retain one stable path across process and run boundaries.
- Run-local evidence and shot-wide state must remain separate; no proximity fallback may
  reinterpret old run output as current state.
- The mechanism must apply independently of shot subject, layer id, frame, or reference.

## General mechanism

`layer_state.path_for()` resolves through the existing
`run_artifacts.shot_state_dir()` boundary. Reads no longer call a run-producing helper;
writes atomically replace `shot/state/layer_state.json`; and active runs do not acquire a
private state copy. No new authority or schema is introduced.

## Rejected patch-level alternatives

- Finalize the synthetic run after each read: a read would still displace the production
  pointer and falsely present itself as a harness invocation.
- Reuse the latest run's state directory: run proximity is not cross-run authority and a
  new latest pointer would silently discard memory.
- Search earlier runs for the newest `layer_state.json`: recursive historical discovery
  is ambiguous and violates strict authority selection.

## Validation

The regression proves on static-product and outdoor-motion fixtures that an empty read
creates no `runs/` tree and resolves exactly to `state/layer_state.json`. An
animated-character fixture writes memory during one explicit run, opens a second run,
and proves the attempt history survives while neither run acquires a private `state/`
copy and the latest pointer remains owned by the explicit invocation.

The original temporary-shot reproduction now reports `latest_exists=False` and
`durable_path=state/layer_state.json`. Verification on 2026-08-30:

- `.venv/bin/ruff check src/vfx_harness/orchestration/layer_state.py tests/unit/test_layer_state.py`
  — passed.
- `.venv/bin/python -m pytest -q tests/unit/test_layer_state.py tests/unit/test_run_artifacts.py`
  — `15 passed in 2.24s`.
- `.venv/bin/ruff check src tests` — passed.
- `.venv/bin/python -m pytest -q` — `666 passed in 44.61s`.
- `.venv/bin/vfx --help` — exit 0.

## Release and rollback

No schema migration or compatibility fallback. Prior run-local copies were generated
output rather than selected cross-run authority and are intentionally not adopted by
proximity. Rollback would restore orphan running pointers and run-scoped memory loss.

## Remaining limitations

An operating-system crash can still leave a genuine invocation marked running; this HIR
only removes synthetic runs caused by layer-memory access.
