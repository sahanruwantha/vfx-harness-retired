---
id: HIR-0244
title: A rejection that teaches must survive its surface
status: accepted
introduced_in: unreleased
date: 2026-09-06
failure_class: the_payment_surface_sliced_a_rejection_to_120_chars_and_kept_only_the_first_reason
mechanism: one_rendering_function_returns_every_reason_whole
adr: null
---

# A rejection that teaches must survive its surface

## Observed failure

`daae404` (HIR-0219's neighbour) made a fragile-threshold rejection name the legal `lo`
range for the current render, the EMPTY diagnosis, and the note that the floor rises with
the content the metric rewards. It is correct, it is tested, it has been on main since last
night, and **it has never reached a builder.**

`blender/tools/misc.py:641`:

```python
lines.append(f"  REJECTED {cid:10} {v.reasons[0][:120]}")
```

What a builder actually saw:

```
FRAGILE THRESHOLD — candidate clearance 1.533 is below the measured decision margin 2.673 (max of 2× resampling noise an
```

Cut mid-word at 120 characters, **before the window begins**. Everything the fix appends is
past the cut.

Two defects in one line, and the second is the worse one:

- `[:120]` truncates a rejection whose whole purpose is to teach;
- **`reasons[0]` drops every other reason.** A verdict carrying FRAGILE *and* NOT NECESSARY
  shows one. That is HIR-0201's "gate that returned alone", alive at the payment surface.

## Why it was allowed

The fix's own test asserts the reason contains the window **at the point of production**,
in `verify_necessity`. Nothing asserted the reason survives the tool boundary. Producer
tested, consumer unchecked -- and between them a private slice.

Five consumers read `Verdict.reasons`, each with its own rendering: `[:120]` and `[0]`
here, `[0]` for the revalidation drop record, `[0]` split on an em-dash and cut at 150 in
the plan surface, and one deliberate split in the plan gate that uses `reasons[0]` as the
finding's `what` and the rest as its `fix`. **One value, five renderings, no two the same,
and only the last is intentional.**

It also retired an inference. The hansa_silk_road driver had reported that an attempt-5
builder kept re-proposing thresholds after fragile rejections, and read it as a model not
using the window. **It never had the window.** A model behaviour attributed to judgment was
a string slice -- the same class as the flat-grey REVISE in HIR-0241, where a critic was
faulted for describing the plate it was shown.

## Mechanism

`Verdict.why()` renders every reason the verdict carries, whole, joined with ` · `, and
returns `"failed verification"` for a verdict carrying none. The builder-facing payment
surface and the revalidation drop record both call it.

The plan gate's split is left alone: `reasons[0]` as `what` and the remainder as `fix` is a
deliberate mapping onto a two-field finding, not a slice. The plan-authoring surface's
150-character cut is **owed and not fixed here** -- same shape, different audience, and no
evidence yet that it has cost anything.

## Validation

`src/tests/unit/test_rejection_reaches_the_builder_whole.py`. The window is past character
120 in the real rejection text, so `test_the_whole_reason_survives` fails on the pre-fix
tree at the exact boundary that hid it.

The no-slice guard is **parsed, not grepped**, and the first draft proved why: a substring
check for `reasons[0]` failed on the comment above the fix, which describes the defect. It
now walks for an `ast.Subscript` over an attribute named `reasons`, so a future consumer
adding its own slice fails and prose about the defect does not.

## What this does not fix

`agents/plan_tools/session_tools.py:393-394` still renders a rejection privately. **Measured
rather than described, because the first two accounts of it -- mine and the hansa driver's
-- were both wrong about the mechanism**, on the real FRAGILE text:

```
why    = v.reasons[0].split("—")[0].strip()                       -> "FRAGILE THRESHOLD"
detail = v.reasons[0].split("—", 1)[1].strip()[:150]              -> cut at "...one-sh"
```

- `reasons[0]` drops every other reason. Real, and the same defect fixed above.
- The em-dash split is **harmless**: `maxsplit=1` keeps the whole remainder, so a second or
  third em-dash costs nothing. The hansa driver read it as losing everything after the
  second; it does not.
- **The 150 is the operative cut**, and it lands mid-word roughly 150 characters before the
  window begins. My own note said "truncates at 150" as if length were incidental; it is the
  whole of it.

The decision is unchanged -- zero plan-authoring refusals in this shot's transcripts were cut
near 150, so there is no evidence it has cost anything, and the audience differs: this
degrades a materializer that is authoring, where the payment surface hid a window from a
builder mid-repair with turns to spend. **But if it ever does bite, the length is the cause
and the em-dash is not**, and someone reading either earlier account would tune the wrong
thing.

Nothing here bounds the rendered length. These strings are harness-authored and bounded by
their producers; if a reason ever grows unbounded the cap belongs at the producer, where it
can cut at a meaning rather than at a character.

Found by the hansa_silk_road driver, in their own landed fix, and handed over because the
surface is in `blender/tools/`.
