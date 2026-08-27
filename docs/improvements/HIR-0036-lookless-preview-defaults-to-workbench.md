---
id: HIR-0036
title: Executable-only live preview defaults to Workbench, not EEVEE
status: accepted
introduced_in: unreleased
date: 2026-08-27
failure_class: lookless_preview_defaulted_to_unlit_eevee
mechanism: preview_mode_follows_declared_look_actions
adr: null
---

# Executable-only live preview defaults to Workbench, not EEVEE

## Observed failure

Run `20260826T170413Z-ba2b4c`, unit `cam_spine` (`look_capabilities: []`).
The builder created `cam_spine_temp_sun` to light an EEVEE verification
render. Scope audit deleted it. f1 verify stayed 100% black — lighting is
layer 2. `render_pass` already forced `matcap:check_normal+y` when
`look_actions` is false. `render_frame` defaulted to `eevee` and
`verify_change` defaulted to `draft`.

## Root cause

Live preview defaults were beauty-oriented. An executable-only unit has no
look feedback groups, but the tools still asked EEVEE to show form. The
agent then guessed a light, which is a scope violation. Classification:
missing compiled default at the tool boundary.

## Decision criteria

- Canonical empty-scene replay remains EEVEE. This changes live
  `render_frame` / `verify_change` defaults only.
- An explicit `mode='eevee'` is honored. The default is the instrument, not a
  ban.
- Default is Workbench `solid` (geometry without lights), matching the
  existing `render_pass` matcap policy for the same `look_actions=false`
  condition.

## General mechanism

- `preview_render_mode(look_actions, requested, look_default=...)`.
- `render_frame` look-default `eevee`; `verify_change` look-default `draft`;
  both become `solid` when the unit has no look capabilities and no mode was
  passed. The caption names why.

## Rejected patch-level alternatives

- Prompt "use solid on camera units": `render_pass` already did the right
  thing; the other two tools did not.
- Light the camera unit: lighting is another layer's mutation scope.
- Score look on solid plates as canonical: EEVEE remains the artifact of
  record (HIR-0032 still fails closed on no-signal beauty).

## Validation

- `tests/unit/test_look_capabilities.py`:
  `test_executable_only_preview_defaults_to_workbench`.

## Release and rollback

No schema migration. Rollback is EEVEE/draft defaults for look-less units,
which again invites a temp sun.

## Remaining limitations

`compare_frame` against a beauty reference is unchanged. A look-less unit
that explicitly requests EEVEE still gets a black plate if the scene has no
lights — that request is now a choice, not the default.
