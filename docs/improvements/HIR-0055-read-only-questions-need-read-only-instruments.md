---
id: HIR-0055
title: Read-only scene questions need typed read-only instruments
status: proposed
introduced_in: unreleased
date: 2026-08-27
failure_class: improvised_read_probe
mechanism: bound_contract_and_render_state_instruments
adr: null
---

# Read-only scene questions need typed read-only instruments

## Observed failure

The same Layer 2 run used `run_bpy` to inspect color management, compositor state, material sockets,
light energy/visibility, and contract quantities. A raw shader-socket probe raised on a socket with
no `default_value`; manual light/compositor toggles needed restoration but convergence blocked the
restore. Shared-role guidance led two sessions to send both `role` and `object`, and
`verify_change` called a blocked edit a visible no-op. On rerun, `inspect_nodes` silently kept only
the first six unlinked inputs. That hid Blender 5 Glare `Size` and `Strength`; the lighting builder
used a read-only `run_bpy` enumeration, which consumed the one reopened image-repair turn and caused
the real bloom edit to be rejected. The detail builder repeated the pattern for evaluated camera and
hero transforms; its following relocation was rejected. In that turn the bare-`next` AST finding was
also hidden by the later convergence denial.

Run `20260827T195146Z-03eb25` then asked for the world name, EEVEE volumetric
start/end/samples, view-layer vector pass, compositor group, and light cutoff through a read-only
`run_bpy`. The guard correctly refused it, but `inspect_scene('render')` and `lights` omitted those
fields, so the advertised legal next action could not answer the question.
The same corrected run then called `check_scene(kind='framing')` on a Light host and received a
syntactically valid zero-width bbox, although lights do not occupy rendered subject pixels.

## Root cause

The tool surface exposed raw mutation as the shortest path to facts already owned by deterministic
evaluators. Diagnostics did not identify whether a successful mutation occurred between baseline
and comparison, and ambiguity guidance named `object=` without explicitly saying to omit `role=`.

## Decision criteria

Measurements must use one canonical evaluator, observations must not enter the mutation journal or
convergence gate, rejected calls must teach one exact legal next action, and filesystem writes must
remain under harness artifact routing.

## General mechanism

`contract_result(id=...)` evaluates one active bound scene or image contract using canonical code;
`inspect_scene(render/lights)` reports render, color, compositor, and light state; existing
`render_pass(light=...)` remains the transactional contribution probe. `verify_change` records the
mutation serial and refuses comparison when no successful edit occurred. Shared-role diagnostics
give an exact `object='name'` example and say to omit `role`. Visual-subject checks reject a camera
as the subject. `run_bpy` filesystem/render writes are blocked and routed to render tools and image
handles. `inspect_nodes` reports every unlinked socket rather than truncating an API-ordered list;
`inspect_scene(section='cameras', frame=...)` reports evaluated camera pose/lens and object rows carry
evaluated world location, dimensions, and render visibility. AST classification refuses a
`run_bpy` payload with no authored write, so observations never consume the journal or repair arm.
The render report also includes world identity, compositor-group identity, motion-blur shutter,
EEVEE volumetric range/samples/shadows, TAA samples, and vector/depth pass state; light rows include
shape, dimensions, custom-distance policy, and cutoff. Metric-region schema text names literal `r`
and `a`/`b` operands.
Visual framing/visibility/bbox checks reject Light, Empty, Camera, Armature, Lattice, Speaker, and
Light Probe control hosts. A Light rejection routes to `render_pass(light=..., pass='beauty')` so
the agent measures illumination contribution rather than a nonexistent bounding box.

## Rejected patch-level alternatives

More prompt examples do not remove raw mutation as an observation path. Allowing manual toggle and
restore loops makes cleanup conditional on convergence policy and model follow-through.

## Validation

Focused image-payment and read-boundary tests report `8 passed`; full regression, Blender-backed
instrument validation, and a producing Layer 2 rerun remain required before acceptance.
`test_render_report_closes_common_eevee_read_only_probe` and
`test_light_report_includes_shape_size_and_cutoff` pin the newly exposed state without importing
`bpy`; focused Ruff and both tests pass.
`test_visual_checks_route_light_hosts_to_contribution_pass` pins the Light refusal and legal
motion/renderable-subject cases; the focused semantic-role suite reports 12 passed.

## Release and rollback

No persisted schema migration beyond ADR-0008. Tool removal is the rollback; do not re-author raw
probe snippets into prompts.

## Remaining limitations

`render_pass(light=...)` measures contribution visually but does not yet return a target-role
irradiance scalar. Add that only when a real decision cannot be made from the canonical image metric
and isolated pass.
