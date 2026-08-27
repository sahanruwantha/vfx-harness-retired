---
id: HIR-0050
title: A keyframe_schedule path miss is not a binding defect
status: accepted
introduced_in: unreleased
date: 2026-08-27
failure_class: path_miss_misclassified_as_inapplicable
mechanism: data_block_path_aliases_and_both_sides_schedule_miss
adr: null
---

# A keyframe_schedule path miss is not a binding defect

## Observed failure

Run `20260827T110846Z-a18160` built layer 2 after consuming
`hf-92d9d1904858b85d9198` (HIR-0049). `materials_energy` passed.
`lighting_bloom` recorded `hypothesis_falsified` (`hf-6fdd6f76988a78cb957a`)
on `light-arc-schedule`. `atmosphere` blocked; `detail_instancing` never
started. Exit 7. Cost $15.82 / 162 turns on the lighting unit.

Canonical `keyframe_schedule` read `energy keyframes [] != [1, 72, 150, 204, 240]`
and stored `value: None`. `_scene_contract_issue` (then nested `_issue`) treated
every None as **INAPPLICABLE / binding defect / rematerialize, not repair**.
Candidate repair called `cannot_express_in_scope` (HIR-0043 held). Remaining
repairs skipped. The finding's `required_authority` reused the HIR-0031
interpolation boilerplate.

The hosts had keys: custom `["energy"]` on the objects, driving `data.energy`
watts. `list_keyframes` listed only the object action, so the data-block path
the contract meant was not even in the live inventory.

## Root cause

`keyframe_schedule` compared `fc.data_path == path` on the **object** action
only, then `_raw_property(obj, path)`. Camera `location` lives there. Light
`energy` does not: Blender stores object fcurve `data.energy` or Light ID
fcurve `energy`. Empty `actual_frames` raised `ValueError`; the probe's
blanket `except` set `value=None`; the verdict template equated None with
"this metric cannot apply to the subject class".

That is the same class as a one-sided selector miss (HIR-0018): the
instrument knew the requested path and could have listed the data_paths
present, then classified the empty set as a **failing measurement**. Instead
it converted a keyable path miss into a plan defect, so repair legally
abstained.

A second, content-level tension remains after the instrument tells the truth:
published samples `0.2 … 1.0` on RNA `energy` are watts, not a normalized
0–1 control. Keying `data.energy` to those samples satisfies the schedule and
can starve the look claims. That is a jointly unsatisfiable published pair
once measured honestly — not a reason to keep the false INAPPLICABLE. Custom
`["energy"]` is a different path and must not silently PASS an `energy`
sample.

Classification: evidence / Blender-adapter defect, then verdict misroute.

## Decision criteria

- Sample path `P` matches object `P`, object `data.P`, or data-block `P`.
- Explicit `data.P` or custom `["P"]` stays exact.
- A path or key-set miss is a numeric fail that exceeds `hi`, with a note
  naming requested aliases, actual frames, and fcurve data_paths present.
- `value is None` on `keyframe_schedule` is never INAPPLICABLE.
- `list_keyframes` lists object and data-block curves (`data.{path}`).
- Camera `location` schedules keep passing without a data-block action.

## General mechanism

- Pure alias and match helpers in `scene_checks.py`; the Blender probe inlines
  the same rules (`_path_aliases`, `_schedule_frames`, `_eval_property` also
  used by `curve_derivative_max`).
- Frame mismatch no longer raises. Missing keys append `hi+1` so `op: max`
  cannot PASS because static RNA happened to match a sample.
- `_scene_contract_issue` routes `keyframe_schedule` None rows to an
  executable fail that names `list_keyframes`.
- `h_keyframes` enumerates `obj.data.animation_data`.

## Rejected patch-level alternatives

- Rematerialize layer 2 samples to `["energy"]` or prompt "don't use drivers":
  the next lighting unit with `energy` on a Light would fail the same way.
- Silently treat custom `["energy"]` as RNA `energy`: the contract would PASS
  a proxy the samples did not name.
- Keep the raise and only change the verdict string: repair would still not
  see `data.energy` as a legal keying of `energy`.
- `--discard-accepted` / hand-edit `layer_2.json`: not a harness fix.

## Validation

- `tests/unit/test_keyframe_schedule.py`: aliases; object `data.energy` and
  data-block `energy` match sample `energy`; custom `["energy"]` does not;
  path-miss value exceeds `hi`; probe compiles without the raise; packed
  evidence fails closed with both-sides note; None schedule row is not
  INAPPLICABLE; `smooth_fraction` None stays INAPPLICABLE.
- `tests/unit/test_plan_improvements.py`:
  `test_exact_keyframe_schedule_is_typed_and_executable` compiles the probe
  and requires `_schedule_frames`.

## Release and rollback

No schema migration. Rollback restores object-only exact `data_path` equality
and the None→INAPPLICABLE route.

## Remaining limitations

Honest `data.energy` measurement does not make 0.2 W light a look plate.
Rematerialize sample *values* (or bind a named control path) when watts and
the look claims cannot both hold. Live `compare_frame` vs freeze payment
(HIR-0048 residual) is a separate defect.

## Resume

Consume `hf-6fdd6f76988a78cb957a` with `--preview` first. Preserve passed
`materials_energy` (HIR-0040 / HIR-0049). Do not empty-base layer 2.
