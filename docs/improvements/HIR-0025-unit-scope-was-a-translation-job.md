---
id: HIR-0025
title: Unit scope was a translation job the builder had to guess
status: accepted
introduced_in: unreleased
date: 2026-08-26
failure_class: unit_authority_was_not_compiled_for_the_builder
mechanism: compiled_unit_scope_card_kickoff_and_queryable
adr: ADR-0003
---

# Unit scope was a translation job the builder had to guess

## Observed failure

Shot `vfx-test`, layer 1, run `20260825T143912Z-0b5ab4`. The builder called
`check_scene(object="cam_rig.camera")` (a role, not a display name), recovered
with `inspect.getsource` for `bvfx_camera_rig` / `bvfx_role`, and reused stale
role names from a rematerialization that never published. Kickoff and
`CLAUDE.md` were layer-shaped. Contracts, mutation, and helpers are unit-shaped.
HIR-0022 made scene tools address `role=`; the compiled projection of that
authority was still missing.

## Root cause

Unit authority lived in `WorkUnit` plus `scene_checks.json` plus `worker.py`
`_HELPERS`. Nothing compiled those three into one card the session could read.
Classification: missing instrument — the model was asked to rediscover the
active unit. Prompt text already preferred roles and helpers; only a compiled
card binds.

## Decision criteria

- One compiler. Kickoff, `CLAUDE.md`, and the `unit_scope` tool are projections
  of the same dict, not three handwritten lists (ADR-0003).
- Enumerate, don't imagine: mutation roles/controls/dresses/spans, bound
  contract ids, claims, judge frames, and every injected `bvfx_*` helper.
- Query, don't recall: the tool returns that JSON on demand after compaction.
- Helper signatures are parsed from `worker.py` `_HELPERS`. A private copy is
  the next silent split.
- Sibling units are not inputs. A camera-rig card must not name a blockout
  role.
- No prompt patch. The card is facts compiled from true authority.

## General mechanism

- `agents/unit_scope.py` compiles the active `WorkUnit` against the selected
  `scene_checks.json` rows it binds (claim `scene_contract` ids plus
  `composition_context.contract_ids`). Unknown bindings fail closed and name
  the requested ids and the ids that exist (HIR-0018).
- Helper inventory is an AST read of `blender/worker.py`. `run_bpy` still
  injects `_HELPERS`; the card cannot drift from that dict.
- `builder_kickoff` and `write_layer_context` embed `format_unit_scope_card`.
- `build_blender_tools` registers `unit_scope` with that same dict.

## Rejected patch-level alternatives

- A longer kickoff that says "use roles, use helpers": already lost on this
  run.
- `inspect.getsource` as the documented discovery path: it imports nothing the
  card cannot name, and it is how the session burned turns.
- A second role→object map in `tools.py`: ADR-0003's split, re-grown.
- Compiling the parent layer or every sibling unit: that is how stale remat
  names re-entered the session.

## Validation

- Pinned in `tests/unit/test_unit_scope.py`: helper names equal `_HELPERS`
  keys and include `bvfx_camera_rig` / `bvfx_role`; a camera unit's card
  contains `cam_rig` and not a sibling `cam.blockout_fg`; an unknown binding
  names requested versus present; kickoff carries the card.
- Integration (`tests.integration.test_harness`): `unit_scope` is registered;
  generated `CLAUDE.md` contains the compiled card.
- `.venv/bin/ruff check src tests`: All checks passed.
- `.venv/bin/python -m pytest -q`: 319 passed.
- `.venv/bin/python -m tests.integration.test_harness`: ALL PASS (0 failed).
- `.venv/bin/vfx --help`: ok.

This HIR is not validated by running the current production layer-1 builder:
that session would compile the falsified view. Fixtures pin the mechanism.

## Release and rollback

No schema migration. Rollback is reverting the compiler, the tool, and the
kickoff/`CLAUDE.md` projections.

## Remaining limitations

- `inspect_nodes` still addresses material/object display names (HIR-0022).
- Recipe ranking and abstention (P4) are separate.
- Critic/image completeness (P3-c) is separate.
