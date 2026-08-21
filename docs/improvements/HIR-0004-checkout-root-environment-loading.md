---
id: HIR-0004
title: Restore deterministic checkout-root environment loading
status: accepted
introduced_in: unreleased
date: 2026-08-21
failure_class: checkout_environment_not_loaded
mechanism: correct_source_layout_root_resolution
adr: null
---

# Restore deterministic checkout-root environment loading

## Observed failure

Before run `20260821T115227Z-a8f966`, `.venv/bin/vfx preflight --strict` reported that neither
supported credential existed even though `/home/sahan/Desktop/vfx-harness/.env` contained both
supported variable names. Supplying `VFXH_ENV_FILE=/home/sahan/Desktop/vfx-harness/.env` made the
same preflight pass immediately.

Runtime inspection showed `PROJECT_ROOT=/home/sahan/Desktop/vfx-harness/src` and
`environment_file()=None`, so the default loader checked `src/.env` instead of the repository
root.

## Root cause

After the source-layout reorganization, `PACKAGE_ROOT` became
`<checkout>/src/vfx_harness/infrastructure`, but `PROJECT_ROOT` remained
`PACKAGE_ROOT.parents[1]`. That expression resolves `src/`; the checkout is `parents[2]`.

## Decision criteria

- Keep dotenv loading explicit, deterministic, and non-overriding.
- Do not search arbitrary parent directories or depend on the caller's working directory.
- Restore the same checkout root used by eval, knowledge, and shot paths.
- Preserve `VFXH_ENV_FILE` as the explicit installed-package/alternate-file override.

## General mechanism

Resolve `PROJECT_ROOT` from `PACKAGE_ROOT.parents[2]` for the declared `src/` checkout layout and
cover the boundary with a source-tree regression test. No credential values enter typed settings,
logs, or test output.

## Rejected patch-level alternatives

- Export the key in every shell invocation: this hides the resolver defect and leaves every other
  checkout-root path wrong.
- Copy `.env` into `src/`: that makes generated secrets conform to a bad code path and contradicts
  documented repository layout.
- Use `python-dotenv`'s arbitrary parent search: the selected environment could change with cwd and
  silently load an unrelated checkout.

## Validation

- `tests/unit/test_model_settings.py` asserts `PROJECT_ROOT` equals the checkout containing
  `pyproject.toml`.
- The original live reproduction now passes without an override:
  `.venv/bin/vfx preflight --strict` loads `ANTHROPIC_API_KEY` from the repository `.env`
  and exits 0. No credential value is printed.
- `.venv/bin/ruff check src tests` passes.
- `.venv/bin/python -m pytest -q` passes: 70 tests after the related plan-evidence improvements.
- `.venv/bin/vfx --help` exits 0.

## Release and rollback

Target: next minor release. The behavior restores the documented default and requires no data
migration. Rollback is the one-line root-resolution revert; `VFXH_ENV_FILE` remains an emergency
override either way.

## Remaining limitations

- Installed wheels have no implicit checkout `.env`; callers outside a source checkout must keep
  using real process environment values or `VFXH_ENV_FILE`.
- Credential validity and account balance are outside dotenv path resolution and remain preflight
  responsibilities.
