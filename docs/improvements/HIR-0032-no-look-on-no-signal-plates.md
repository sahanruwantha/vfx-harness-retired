---
id: HIR-0032
title: Declared empty look capabilities are not an identifier scan
status: accepted
introduced_in: unreleased
date: 2026-08-26
failure_class: look_feedback_from_axis_identifiers_on_executable_only_units
mechanism: declared_empty_capabilities_and_no_signal_abstention
adr: null
---

# Declared empty look capabilities are not an identifier scan

## Observed failure

`cam_spine` declared `look_capabilities: []` (executable-only). Tool policy still
derived motion look-feedback because axis id `camera_continuity` contains
`continuity`, so `_layer_feedback_policy` / `axis_feedback_groups` scanned
identifiers. Canonical EEVEE plates were black (no lights on a camera unit). A
look score on that plate is not a judgment. The terminal critic-shaped 1.0 on
this run was `decided_by: unit_executable_evidence` (synthetic fail), but the
identifier scan is the trap that would have called the critic had executable
claims not already decided.

Materialization already requires an explicit `look_capabilities` list: silence
is indistinguishable from "owns no appearance"
(`test_materialization_requires_an_explicit_capability_declaration`). The hole
was treating declared `[]` as "missing" and falling back to the scan.

## Root cause

`if declared: capability_groups else axis_scan` collapsed omitted and explicit
empty. Passing `feedback_groups=None` into blender tools then ran
`_layer_feedback_policy`. Classification: missing compiled context / tool policy.
Identifiers are names, not authority (same class as 20260823T154920Z, inverted).

## Decision criteria

- A declaring work unit owns look scope even when the tuple is empty: `[]`
  means executable-only.
- Pass `feedback_groups=[]` (not `None`) so tools do not fall back to layer-axis
  scanning.
- Schema-4 layers with no work unit still scan identifiers.
- A candidate plate with no `focus_signal` is not a look score: fail closed
  without calling the model (`decided_by: no_optical_signal`).

## General mechanism

- Builder: if `active_unit is not None`, always
  `capability_feedback_groups(declared)` and pass that list (possibly empty) as
  `feedback_groups`.
- `_judge`: if `focus_signal` has no optical signal, return a failing verdict
  without a critic call.

## Rejected patch-level alternatives

- Prompt "this unit is layout, do not judge look": the tools still emitted
  motion groups from the identifier scan.
- Light the camera unit so EEVEE is not black: lighting is another layer's
  mutation scope.
- Score look on solid viewport captures: canonical EEVEE is the artifact of
  record.

## Validation

- `tests/unit/test_look_capabilities.py`:
  `test_continuity_axis_scan_is_not_used_for_a_declaring_empty_unit`.
- `tests/unit/test_builder_instruments.py`: black PNG has no optical signal;
  a structured plate does.

## Release and rollback

No schema migration. Rollback is the `if declared else scan` branch, which
again treats executable-only camera units as motion-look owners.

## Remaining limitations

`focus_signal` is conservative (stddev / edges / range). A nearly-black but
intentional plate could abstain; that is fail-closed, not a look pass. Units
that declare look capabilities and produce no-signal plates still fail — they
must light and surface in a unit that owns those roles. Composed canonical of
a look-less layer is not a critic session (HIR-0039).
