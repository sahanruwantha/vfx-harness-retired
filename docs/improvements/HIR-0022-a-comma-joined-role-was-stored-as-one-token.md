---
id: HIR-0022
title: A comma-joined role was stored as one token that matched neither contract
status: accepted
introduced_in: unreleased
date: 2026-08-26
failure_class: comma_joined_role_silently_corrupted_semantic_identity
mechanism: one_dotted_token_alphabet_shared_by_tagging_selectors_and_scene_tools
adr: ADR-0003
---

# A comma-joined role was stored as one token that matched neither contract

## Observed failure

Shot `vfx-test`, layer 1, run `20260825T143912Z-0b5ab4`. The blockout builder tagged
hosts with `'cam.blockout_fg,cam.blockout_depth_tiers'`. `_bvfx_role` rejected
whitespace and accepted commas, then wrote one custom-property string that matched
neither published contract. Sessions recovered by inventing marker empties for the
"other" role — the same shape as layer 2 inventing `HL_ClampBlue` when the legal
control could not express the need.

The builder then called `check_scene(object="cam_rig.camera")`. That string is a
**role**; the object is `camera`. `checks._obj` was `bpy.data.objects.get(name)` and
the KeyError named neither present names nor present roles. `inspect_scene` did not
print `bvfx_role`. Agents recovered with `inspect.getsource`.

## Root cause

Semantic identity had no alphabet. Tagging, contract selectors, and builder scene
tools each decided what a "role" was. Tagging stored any non-whitespace string, so
CSV looked like membership. Scene tools addressed display names, so a role-shaped
argument was a miss with one-sided diagnostics — HIR-0018 unbound on the builder
surface. Classification: contract/state-model gap plus diagnostic asymmetry.

## Decision criteria

- One alphabet, one matcher (ADR-0003). A private `fnmatch` copy in the probe, the
  control script, and the tools is the next silent split.
- Make the failure unrepresentable: a comma is not a tag; two roles on one host wait
  for a real membership API.
- Rejections teach: a miss names the request and the names *and* roles that exist.
- No prompt patch. Tool schemas expose `role=`; tagging raises.

## General mechanism

- `domain/semantic_roles.py` is the alphabet: `validate_role_token` (`[A-Za-z0-9._-]+`),
  `match_semantic` (fnmatch), both-sides `format_object_miss`, authoring lint that a
  selector may be a pattern (`cam.blockout_*`) never a comma-joined list.
- `_bvfx_role` validates through that function. The Blender worker loads the module
  by path so it never imports the package.
- `validate_row` refuses comma/whitespace selector tokens at authoring.
- Authoritative probe and `probe_control` scripts call `checks.match_semantic` instead
  of inlined `fnmatch` copies.
- `inspect_scene`, `check_scene`, and `list_keyframes` accept `role=` (XOR `object=`).
  Object resolution uses the same matcher. Inspect prints `role=` and `owner=` on
  every object line. A miss lists present names and roles.

## Rejected patch-level alternatives

- Prompt wording "use roles not names": the catalog already preferred roles while the
  tools demanded names — HIR-0021's dual. Only the tool binds.
- Accepting CSV as membership: there is no membership API; storing two tokens as one
  is silent corruption.
- A second role→object map in `tools.py`: ADR-0003's split, re-grown.
- Treating a display-name miss as a role lookup: heuristic. The miss must name both
  sides and tell the caller to pass `role=`.

## Validation

- Pinned unit tests in `tests/unit/test_semantic_roles.py` and
  `tests/unit/test_checkpoint_fidelity.py`: the production CSV raises; legal dotted
  tokens tag; a name miss that was actually a role names present roles and says
  `pass role=`; contract selectors refuse commas and still accept `cam.blockout_*`;
  the probe and control script call `checks.match_semantic`; reproduction hints
  address `role=` not the display name.
- Integration (`tests.integration.test_harness`): `check_scene` requires `role=` or
  `object=` (not both); `passes` does not require a role; framing mistakes still fail
  closed before the worker.
- `.venv/bin/ruff check src tests`: All checks passed.
- `.venv/bin/python -m pytest -q`: 306 passed.
- `.venv/bin/python -m tests.integration.test_harness`: ALL PASS (0 failed).
- `.venv/bin/vfx --help`: ok.

## Release and rollback

No schema migration. Existing hosts tagged with a comma token will fail closed on
retag or on the next `bvfx_role` call; they already fail contract match. Rollback is
reverting the alphabet module and the three tool schemas.

## Remaining limitations

- One role per host until a real membership API exists. That is intentional.
- `inspect_nodes` still addresses material/object display names.
- Compiled unit-scope cards shipped as HIR-0025.
