---
id: HIR-0073
title: Local-light coverage must be isolated from World volume
status: proposed
introduced_in: unreleased
date: 2026-08-28
failure_class: confounded_light_diagnosis
mechanism: transactional_light_coverage_render
adr: null
---

# Local-light coverage must be isolated from World volume

## Observed failure

In build `20260827T223035Z-44cedd`, both density sweeps proved that the current plate
stayed nearly black even at zero density. The builder then moved the local light while
the original World density was restored, rendered black again, and could not tell
whether the new placement missed the subject or whether the unbounded volume extinguished
it. It spent more than two minutes deliberating, repeatedly moved the light, and finally
toggled `hide_render` in authored mutation calls to probe contribution.

## Root cause

The existing `diffuse_direct` and isolated-light renders left World surface and volume
contributions active. That is a faithful combined-scene result but cannot answer the
causal question “does this local light cover the surface?” A density sweep at one light
position followed by a placement change under restored density is not a controlled
experiment; the two variables remain coupled.

## General mechanism

`render_pass(pass='light_coverage', light='<object>')` is a read-only scene transaction.
It installs a black zero-strength temporary World, applies a neutral diffuse clay material,
isolates the named local light, temporarily mutes its energy curve and raises diagnostic
energy above a scene-scale floor, renders, and restores the exact energy/curve state,
World datablock, material override, light visibility, and common render settings. Visible form proves local-light
placement/coverage without atmospheric attenuation or emissive material; black form routes
to placement, direction, range, or occlusion. The diagnostic does not mint payment evidence
or trigger the ordinary black-World-density mutation guard.

## Rejected patch-level alternatives

Prompting the builder to unlink and relink World volume uses mutation authority for an
observation and makes interruption a scene-corruption path. Treating `diffuse_direct` as
coverage would confidently mislabel a volume-extinguished plate. Adding another density
sweep after every light move grows a two-variable search without isolating either cause.

## Validation

The integration ratchet requires a non-empty caption that explicitly names World suppression,
placement/coverage, normalized diagnostic energy, and restoration. Blender 5.2.1 producing
validation used an animated 30→450 W Spot 150 scene units from a cube. The coverage render
temporarily used 100,000,000 W, produced a non-black lit surface, then restored energy 30 W,
the original curve state, frame 1, World, empty material override, and light visibility.

## Release and rollback

No schema migration. The new render-pass enum is additive and diagnostic images remain
disposable scratch evidence.
