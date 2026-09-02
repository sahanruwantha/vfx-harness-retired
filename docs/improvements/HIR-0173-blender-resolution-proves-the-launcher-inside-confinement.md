---
id: HIR-0173
title: Blender resolution proves the launcher inside the mandatory confinement
status: accepted
introduced_in: unreleased
date: 2026-09-02
failure_class: environment_probe_context_mismatch
mechanism: confined_executable_resolution
adr: null
---

# Blender resolution proves the launcher inside the mandatory confinement

## Observed failure

On a host whose `PATH` contains `/snap/bin`, `resolve_blender("blender")` selected
`/snap/bin/blender`, the snap `run` shim, because its unconfined `--version` probe succeeded.
Every shot-bound worker then failed at boot, after the boot timeout, with the snap runtime's
own diagnostic:

```text
Blender worker did not become ready: internal error, please report: running "blender"
failed: timeout waiting for snap system profiles to get updated
```

The three real-Blender tests (`test_confined_blender_imports_glb_through_held_memfd`,
`test_receipt_backed_layer_replays_twice_in_real_confined_blender`,
`test_public_multi_unit_artifact_replays_from_empty_in_real_blender`) failed identically on a
clean `20515d5` worktree, so the failure predates the surrounding work. In the Codex sandbox
that validated earlier commits, `/snap/bin` was not on `PATH`, so the same tests were skipped as
"Blender is unavailable" and never passed on this host. Unconfined, both `/snap/bin/blender`
and the raw binary `/snap/blender/current/blender` print their version; under a bubblewrap
sandbox with the worker's system roots (`/etc`, `/opt`, `/snap`, `/sys`, `/usr`) the shim
times out and the raw binary answers. Binding `/var/lib/snapd` and `/run/snapd.socket` does
not rescue the shim: `snap-confine` refuses to continue without `cap_dac_override`, which the
sandbox withholds by design.

## Root cause

`resolve_blender` decided selection with a probe executed in the wrong context. The worker
always runs inside the mandatory filesystem confinement, but the probe ran on the host, so any
launcher that works only on the host (a snap shim needing snapd and capabilities, or a binary
outside the bound system roots) passed selection and failed later, slowly, at worker boot with
a diagnostic that named neither the contract nor the next action. Strict preflight resolved
the executable with a bare `which`, so it could report the shim's confinement failure but could
not select the raw binary that the same enumeration already listed as a fallback.

## Decision criteria

- The probe must execute under exactly the roots, mounts, and environment a worker receives;
  a second, weaker confinement would be a parallel implementation of the same boundary.
- Selection must fail closed before any worker boot or model spend, with a diagnostic that
  names the confinement, every rejected candidate's own reason, and the legal next action.
- Strict preflight and sessions must share one resolver, so preflight cannot pass what a run
  will refuse.
- No host-specific launcher heuristics: the snap shim is rejected because it fails the
  confined probe, not because of its path.

## General mechanism

`vfx_harness.blender.resolution` owns resolution. Each candidate (the requested name, its
`PATH` resolution, `BLENDER_BIN`, then the packaged fallbacks) runs `--version` through
`prepared_worker_command` against a private throwaway layout: the shot-bound view for runs and
preflight, or the read-only host root view for ephemeral sessions. The first candidate that
prints its Blender identity inside that confinement is selected. Otherwise
`BlenderResolutionError` carries every `BlenderCandidateRejection` with the confinement's own
words (for the shim, the snapd profile timeout; for a binary outside the bound roots, the
`execvp` failure) and names `BLENDER_BIN` as the next action. `BlenderSession.start()` resolves
with the view it will use, and strict preflight resolves through the same function, so its
`blender_executable` check now expects an executable that answers inside the confinement; the
preflight probe specification advances to revision 4 because the observation changed.

## Rejected patch-level alternatives

- Preferring `/snap/blender/current/blender` over `/snap/bin/blender` by path: a launcher
  heuristic that fixes one packaging and leaves every other host-only launcher undetected.
- Binding snapd's socket and state into the confinement: `snap-confine` still needs
  capabilities the sandbox withholds, and widening the worker's view for a launcher's
  convenience weakens the authority boundary.
- Documenting `BLENDER_BIN=/snap/blender/current/blender` as the required setting: prose
  cannot stop the resolver from choosing an unusable launcher when the variable is unset.

## Validation

- `src/tests/unit/test_blender_resolution.py`: a launcher that answers `--version` on the host
  but is invisible inside the sandbox is rejected with the confinement diagnostic, naming the
  candidate and `BLENDER_BIN`; a shot-bound session refuses it before spawning any worker; the
  selected Blender re-proves inside the confinement. The first two fail without the mechanism.
- `src/tests/unit/test_preflight_results.py`: strict preflight reports the confined resolution
  diagnostic as the Blender problem, the confinement probe declines to run, and the typed
  `blender_executable` check carries the new expectation and next action.
- The three real-Blender tests above pass on this host without environment changes: the shim is
  rejected and the raw snap binary is selected.

## Release and rollback

No schema change beyond the preflight probe revision; `vfx-harness.environment-result/v2`
readers compare revisions and treat the change as a new observation surface. Rollback is
reverting the commit; no durable artifact depends on the selection method.

## Remaining limitations

The planner spike lab still executes Blender outside the confinement through the plan-tools
shell runner; it now receives a confinement-proven binary, but its own execution boundary is
unchanged. The confined probe adds roughly one Blender `--version` launch per distinct
requested name and view per process.
