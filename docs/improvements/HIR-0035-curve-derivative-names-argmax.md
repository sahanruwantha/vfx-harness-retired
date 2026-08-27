---
id: HIR-0035
title: curve_derivative_max names the argmax adjacent-frame pair
status: accepted
introduced_in: unreleased
date: 2026-08-27
failure_class: scalar_bound_without_argmax
mechanism: derivative_segments_named_on_evidence
adr: null
---

# curve_derivative_max names the argmax adjacent-frame pair

## Observed failure

Run `20260826T170413Z-ba2b4c`, unit `cam_spine`. Canonical
`cam-location-smoothness` failed at **7.391312** against `hi: 6.0`. The probe
already walked every adjacent frame in `[1, 240]`. The evidence row was a
scalar with an empty `note`. The builder derived that the burst was f1→f24
from the published schedule samples. Measure, don't estimate.

## Root cause

The Blender probe kept `max(deltas)` and discarded which pair produced it.
`check_scene(kind='motion')` already names `peak_speed_frame`; the
authoritative contract kind did not. Classification: instrument / teaching
gap (HIR-0018 class: a miss must name both sides).

## Decision criteria

- The note is part of the measurement, not critic prose.
- Argmax uses the probe's definition (max-component per adjacent frame), not
  Euclidean path length.
- Segments that exceed `hi` are listed (capped) so a multi-burst curve is not
  reduced to one pair.
- Full per-frame profiles stay off the evidence row; they are reconstructable
  from `segments` in the probe result.

## General mechanism

- Probe collects `[frame_a, frame_b, delta]` per adjacent pair.
- `_argmax_span` coalesces contiguous pairs that share the peak (LINEAR
  interpolation of f1→f24 is one span, not a first-pair accident).
- `curve_derivative_note` / `_evidence` write `note`, `argmax_frames`, and
  `argmax_delta`. Over-`hi` pairs compact the same way (`×N` when coalesced).
- `motion_from_positions` adds `peak_speed_span` `[start, end]` so
  `check_scene(kind='motion')` reports `fA→fB`, not only the arrival frame.

## Rejected patch-level alternatives

- Prompt "look at the schedule samples": the instrument already had the
  denser curve.
- Change the metric to Euclidean path length: it would disagree with the
  published 7.391312 reading.
- Attach 239 segment rows as required evidence: noise; the note is the
  teaching surface.

## Validation

- `tests/unit/test_evidence_vocabulary.py`:
  `test_curve_derivative_evidence_names_argmax_segment`;
  probe compile contains `segments.append`.
- `tests/unit/test_builder_instruments.py`:
  `test_motion_instrument_names_the_peak_speed_span`.

## Release and rollback

No schema migration. Extra evidence keys are additive. Rollback is an empty
note and a scalar-only smoothness miss.

## Remaining limitations

The probe still evaluates every integer frame in the window. Naming the
argmax does not make an unsatisfiable published pair pass (HIR-0030). LINEAR
interpolation coalesces into one span; a true single-frame spike stays a
one-pair argmax.
