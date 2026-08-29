---
id: HIR-0066
title: Black frames need a scene cause card
status: proposed
introduced_in: unreleased
date: 2026-08-28
failure_class: black_frame_sign_guessing
mechanism: typed_black_frame_scene_diagnosis
adr: null
---

# Black frames need a scene cause card

## Observed failure

Two clean retries of `lighting_atmosphere` produced nearly 100% black EEVEE frames while a World
Volume Scatter node used densities between `0.0025` and the contract ceiling `0.15`. The builders
repeatedly moved lights and injected energy up to `5,000,000`, even though the exact schedule
contract required `30@f72` and `450@f150`. One session raised density from `0.02` to `0.15`,
mistaking a safety ceiling for a target. The earlier successful calibration had already measured
that the long camera ray needed density near `1e-05`.

## Root cause

The render tool reported only pixel exposure. It did not expose the active World-volume density,
camera span, background strength, or light distance at the same decision boundary. The model had
to guess whether black came from absent energy, inverse-square placement, or accumulated
extinction, despite all causal state being mechanically readable.

## General mechanism

Nearly black draft/EEVEE renders now append a typed scene cause card. It enumerates active World
volume nodes and exact density/link state, camera clip range, Background strength, and light
energy/distance. For an unlinked positive World density whose `density × clip_end` scale is at
least one, the card names a logarithmic `probe_control` sweep from zero through `1e-06..1e-03`
and the current value. This is a routing instrument, not a verdict: the existing image contracts
still select the legal result. Free-form scene mutation is blocked until the named sweep runs.
Afterward, direct World-density commits must use one of the measured values; a new candidate must
first be added to another sweep. This prevents a model from acknowledging the card and then
raising density to an unmeasured ceiling anyway.

## Rejected patch-level alternatives

Prompting “try lower density” would be scene-shaped advice and would not distinguish a shot with
no World volume. Raising the density limit or energy schedule would weaken authority. Hard-coding
`1e-05` as a universal target would confuse scene scale with a physical constant; the logarithmic
sweep measures the active shot instead.

## Validation

`test_black_frame_context_routes_high_world_density_to_log_sweep` pins the exact causal state and
finite next action. `test_authored_density_values_finds_socket_and_helper_literals` pins the
mutation classifier used by the measured-value guard.
`test_black_frame_context_does_not_prescribe_density_without_volume` prevents
the tool from inventing a volume diagnosis when none exists. Producing validation is a fresh
Layer-2 build whose first black World-volume render immediately calls the named sweep rather than
changing the energy contract.

## Release and rollback

Observation-only; no persisted schema migration. Revert the worker handler and tool attachment
together if Blender changes the World density semantics, preserving an equivalent typed cause
instrument.

## Remaining limitations

`density × clip_end` is a scale warning, not rendered transmittance. Linked density networks are
reported as linked and intentionally receive no scalar sweep prescription until a node-path-aware
control instrument can identify their effective range.

HIR-0069 supersedes the original arrival-ordered guard behavior by retiring completed
measurements and attaching a causal interpretation to non-explanatory sweeps.
