---
id: HIR-0119
title: Replay order is derived from the unit DAG
status: accepted
introduced_in: unreleased
date: 2026-08-30
failure_class: retry_replayed_accepted_units_in_authored_array_order
mechanism: deterministic_dependency_ordered_unit_replay
adr: null
---

# Replay order is derived from the unit DAG

## Observed failure

Room 1046 Layer 2 materialization legally stored `tower_massing` at `stages[0]` while
its declared producer `facade_hero_window` was at `stages[7]`; the tower's
`depends_on: ["facade_hero_window"]` edge was the ordering authority. Initial build run
`20260829T194427Z-04d002` used readiness scheduling and therefore executed the hero
window before the tower. Its tower candidate replay reported protected
`hero-window-visible-f38 = 0.428571` and sealed.

On audited retry, `build_layer` reconstructed already-passed priors by filtering raw
`layer.stages`. Run `20260829T201315Z-59c634` consequently replayed
`tower_massing.py` before `facade_hero_window.py`. The same accepted artifacts then
measured `hero-window-visible-f38 = 0.285714`, failed the roofline successor, and
published false plan finding `hf-117eeea1918a7055124e`. Composed layer publication used
the same raw array order, so even a fully built layer would have emitted a cumulative
artifact whose execution order contradicted its DAG.

## Root cause

The scheduler correctly treated `depends_on` as authority, but replay reconstruction and
composition treated JSON array proximity as authority. A first uninterrupted build
accidentally retained scheduler order because it appended artifacts as units passed. A
later invocation rebuilt that list from durable state in authored array order. This made
cumulative empty-scene truth depend on whether work was completed in one process or
across retries.

This is an orchestration/replay defect, not a reason to require materializers to sort an
otherwise valid declarative DAG. The array is a collection; its edges define order.

## Decision criteria

- One deterministic topological primitive orders unit replay and composition.
- Every producer precedes every transitive consumer regardless of authored array position.
- Simultaneously ready independent units retain authored relative order as the stable
  tie-break, so retries cannot reshuffle them by completion history.
- Reconstructing passed artifacts after interruption and appending newly passed work yield
  the same order.
- Missing producers, duplicate ids, and cycles fail closed rather than falling back to
  array order.
- The mechanism is independent of shot, layer, role, frame, geometry, and artifact name.

## General mechanism

`domain.work_units.dependency_ordered_units` performs stable Kahn ordering from exact
`depends_on` edges, using authored position only to order the current ready set. It
rejects duplicate ids, missing producers, and cycles.

`build_layer` derives `ordered_units` once from the selected layer authority. Passed prior
artifacts are reconstructed from that order on every invocation; after each acceptance
the list is reconstructed again rather than appended in attempt order. The final composed
artifact uses the same order, with HIR-0117's evaluated-state barrier between members.

## Rejected alternatives

- Require the materializer to rewrite `stages[]` into topological order: duplicates graph
  semantics in authoring and makes a valid DAG illegal because of presentation order.
- Preserve historical acceptance order in durable state: interruption timing is not plan
  authority, and independent sessions could still produce different compositions.
- Sort artifact filenames or unit ids: lexical order has no dependency meaning.
- Consume the falsification as a camera/geometry plan conflict: the selected plan did not
  change between the pass and failure; only replay order did.
- Special-case the hero/tower roles: that would overfit the held-out fixture.

## Validation

- `test_dependency_order_is_derived_for_replay_and_stable_for_independent_units` supplies
  a consumer before its producer plus an independent root and pins stable output
  `[independent, producer, consumer]`.
- Atomicity and builder-unit-evidence suites: 52 passed.
- `.venv/bin/ruff check src tests` passed; the focused integration harness passed every
  check; `.venv/bin/python -m pytest -q` passed 588 tests; and `.venv/bin/vfx --help`
  passed.
- Typed replan consumed false finding `hf-117eeea1918a7055124e` against unchanged bundle
  `d235ad49...`, invalidated only `roofline_signage`, and preserved accepted
  `facade_hero_window` and `tower_massing`.
- Held-out producing run `20260829T204459Z-4e2b95` reconstructed accepted priors as
  `01_camera_path.py -> facade_hero_window.py -> tower_massing.py` in both warm live
  replay and canonical empty-scene replay. Canonical `hero-window-visible-f38` returned
  to `0.428571 >= 0.3` from the raw-array replay's `0.285714`; the rebuilt roofline
  candidate reproduced that pass and the unit sealed at 5.0. The unit report records
  59 turns, 1,007.8 seconds, and $3.26; most of that cost was independent roofline
  refinement, while order derivation itself completed before the first model turn.

Implementation commit: `60cd275` (`fix: derive unit replay order from dependencies`).

## Release and rollback

No persisted schema change. Existing arrays remain valid; consumers derive their execution
order. The false finding remains immutable audit evidence and is consumed through the typed
replan transaction after the mechanism lands. Rollback restores attempt-dependent cumulative
replay and is unsafe.

## Remaining limitations

Blender scenes with exactly overlapping surfaces may still have renderer- or BVH-sensitive
visibility. Stable dependency order removes retry history as an input; geometric ambiguity
that remains under that one canonical order belongs to the owning plan/contracts.
