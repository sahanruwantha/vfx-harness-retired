---
id: HIR-0194
title: The confined Blender worker renders on the host GPU, and preflight proves it
status: accepted
introduced_in: unreleased
date: 2026-09-04
failure_class: sandbox_hid_the_gpu_so_every_render_ran_on_software_opengl
mechanism: gpu_device_binds_in_confinement_with_worker_platform_report_and_strict_preflight_check
adr: null
---

# The confined Blender worker renders on the host GPU, and preflight proves it

## Observed failure

Every `vfx run`, build, canonical replay, and real-Blender test on this host felt slow, and
the question "is it even using the GPU" had no typed answer. Measured on the development
machine (NVIDIA GeForce RTX 2050, Blender 5.2.1 LTS, 2026-09-04):

```
headless Blender on the host       gpu.platform: NVIDIA GeForce RTX 2050/PCIe/SSE2 | OPENGL | NVIDIA
same binary inside the confinement gpu.platform: llvmpipe (LLVM 20.1.2, 256 bits)   | OPENGL | SOFTWARE
                                   EGL Error (0x3009): EGL_BAD_MATCH ... (three times)

EEVEE 1920x1080, 64 samples, principled world volume, one mesh (steady-state second frame):
  confinement, software OpenGL   21.1 s
  confinement + GPU device nodes  0.9 s
```

`nvidia-smi` showed no compute process while a build was running. The harness renders a
plate for every framing check, critic round, canonical replay, and finalization; the four
real-Blender integration tests pay the same tax on every suite run.

## Root cause

`blender/filesystem_confinement.confined_worker_argv` mounts `--dev /dev`, bubblewrap's
minimal devtmpfs: `null`, `zero`, `random`, `tty`, `pts`, `shm` and nothing else. No
`/dev/dri/*` and no `/dev/nvidia*` node exists inside the worker, so Mesa cannot open a
hardware EGL device and Blender falls back to llvmpipe. The confinement decision (HIR-0173's
mandatory worker sandbox) was written for filesystem and syscall containment and never
considered device access; nothing in the worker, the session, or preflight observed which
renderer Blender had initialized, so the fallback was silent. Strict preflight proved the
worker's version identity, filesystem view, syscall denial, and self-test, and reported
"ok" while every render ran on software.

This is the failure class AGENTS.md names first: an environment defect that resembles slow
or weak agent work. Time attribution across the day's runs shows model turns dominate wall
clock (materialize-layer-2: 21.9 min span, 21.4 min model, 0.4 min tools), so this is not the
main lever on run duration, but it is a 23x tax on every plate the harness draws, it hides
in the sandbox, and preflight could not see it.

## Decision criteria

- The confinement keeps its filesystem and syscall containment; it gains exactly the device
  nodes rendering needs, read-write, and only those present on the host.
- The worker reports the renderer it actually initialized; preflight never infers GPU use
  from the host alone.
- A host with a GPU whose worker reports software rendering is a strict-preflight failure
  before spend; a host without a GPU passes with software rendering, honestly reported.
- The observation joins the typed environment result so a change in renderer changes the
  confinement check's observed identity.

## General mechanism

1. `filesystem_confinement.GPU_DEVICE_NODES` lists the DRI and NVIDIA nodes;
   `host_gpu_device_nodes()` returns the present ones plus every `/dev/nvidiaN`, and
   `gpu_device_binds()` renders them as `--dev-bind node node`, appended right after
   `--dev /dev` in both the shot-bound and the plain confinement.
2. `blender/worker.h_ping` adds `gpu`: `{renderer, backend, device_type}` read from
   Blender's `gpu.platform` after `gpu.init()`, or `{error}` when the module cannot
   initialize. The context is created once and reused by the worker's renders.
3. `application/preflight._probe_blender_confinement` records `worker_gpu` and
   `host_gpu_device_nodes`, and adds a problem when the host has nodes but the worker
   reports `SOFTWARE` or no platform. The confinement observation is
   `vfx-harness.blender-confinement-observation/v2` and the probe revision is 6, so prior
   environment results and recovery keys are stale by identity rather than reinterpreted.
4. The operations guide names the field to read and what a `SOFTWARE` report means.

## Rejected patch-level alternatives

- Dropping the confinement for renders, or running renders on the host: the sandbox is the
  boundary that keeps the worker off the shot tree and the network.
- Binding all of `/dev`: exposes every device to model-authored `run_bpy` code.
- Setting `LIBGL_ALWAYS_SOFTWARE=0` or other environment hints: no device node, no GPU;
  the environment was never the cause.
- Detecting slowness by render timing: a measurement of the symptom, not of the renderer.

## Validation

- `src/tests/unit/test_gpu_confinement.py`: device binds follow the present host nodes
  (including indexed `/dev/nvidiaN`) and are empty without them; the preflight probe fails
  closed on `SOFTWARE` or a missing platform when the host has nodes, names the renderer or
  error and the nodes, passes on hardware, and passes on a host without a GPU; the typed
  environment result carries the observation and its digest changes with the renderer; the
  worker's ping payload shape is closed.
- `src/tests/integration/test_real_confined_worker_gpu.py`: on a host with GPU nodes, a real
  confined worker reports a non-software renderer through the same probe strict preflight
  runs.
- Measured, not assumed: the four pre-existing real-Blender integration tests take 69 s
  on software OpenGL and 68 s with the GPU bound, because they boot the worker and run
  scene checks without rendering; the change is not a wall-clock lever for the suite, and
  the transcript attribution above says the same for runs. The gain is confined to what
  actually renders: EEVEE and Workbench plates, canonical replay captures, and critic
  inputs (21.1 s to 0.9 s per volumetric 1080p frame in the benchmark above).
- Live: `vfx preflight --strict` on the development host reports
  `worker_gpu: NVIDIA GeForce RTX 2050/PCIe/SSE2 | OPENGL | NVIDIA` with all seven host
  nodes bound; before the change the same probe would have failed with the software
  report above.

## Release and rollback

Lands with a probe-revision bump. Rollback is reverting the confinement binds; preflight
then fails closed on this host, which is the intended signal.

## Remaining limitations

Cycles device selection is untouched: the harness renders with EEVEE and Workbench, and no
unit configures Cycles. AMD or Intel hosts expose `/dev/dri` only, which the same list
covers, but no such host has been measured. The worker's `gpu.init()` runs at ping time; a
driver that fails only under load is reported by the render, not by preflight.
