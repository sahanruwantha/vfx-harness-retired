"""Measure a tower's facade as a horizontal luminance profile.

WHY THIS EXISTS. The critic has spent five attempts on layer 2 describing the hero tower
in prose -- "two bright OUTER window strips against a darker recessed core", "the sign
cabinet is inverted: panel glows, text dark" -- and the builder has inverted those
polarities four times running. Prose is not a target. `measure_ref` reports exposure and
structure over a whole frame and cannot see any of this, and four hand-rolled detectors
written in this project have each produced a different answer for the same image (see
LAYER_REFERENCES_DESIGN.md, "how to verify framing fidelity"). So this module does the
one thing that IS robustly measurable on a facade and happens to be exactly the thing
that keeps being got wrong.

Collapse the shaft to a single row: for each pixel column, the mean luminance down the
shaft. Windows are vertical runs, so a lit strip becomes a peak and a recessed core a
trough. The result is scale- and distance-invariant once x is normalised by shaft width
and luminance by the shaft's own range, which means a 321px-wide isolation plate and a
260px-wide shot render can be compared directly.

WHAT IT DELIBERATELY DOES NOT DO. It does not locate the tower in a cluttered frame.
Segmenting a dark tower from a dark city at night is the problem that defeated the earlier
detectors, and pretending otherwise is how they produced confident wrong answers. Callers
either supply `box`, or use a render whose background is separable -- which is exactly the
case for the lookdev turntable this was built for.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image

# The shaft only. Above this the sign cabinet dominates; below it the podium flares wider
# than the shaft and would drag background into the profile.
SHAFT_TOP, SHAFT_BOT = 0.16, 0.70

# Where the outer strips and the recessed core live, as fractions of shaft width. Fixed
# rather than derived from peaks so two images are always measured the same way -- a
# derived band would move to wherever each image happens to be bright and could not
# report that a strip is in the WRONG PLACE.
OUTER = ((0.04, 0.30), (0.70, 0.96))
CORE = (0.38, 0.62)


def _luma(path: str | Path) -> tuple[Image.Image, int, int]:
    im = Image.open(path).convert("L")
    return im, *im.size


def _shaft_extent(px, w: int, h: int, is_fg, box) -> tuple[int, int, int, int]:
    """Shaft bbox. The horizontal extent is measured INSIDE the shaft band, not from the
    whole silhouette: the podium is far wider than the shaft, so a whole-object bbox puts
    pure background at both ends of the profile and buries the strips in the middle."""
    if box:
        x0, y0, x1, y1 = (int(box[0] * w), int(box[1] * h),
                          int(box[2] * w), int(box[3] * h))
        by0, by1 = y0, y1
    else:
        ys = [y for y in range(0, h, 2) if any(is_fg(px[x, y]) for x in range(0, w, 3))]
        if not ys:
            raise ValueError("no foreground found; pass box=(x0,y0,x1,y1) normalised")
        by0, by1 = min(ys), max(ys)
        x0, x1 = 0, w - 1
    H = by1 - by0
    sy0, sy1 = int(by0 + SHAFT_TOP * H), int(by0 + SHAFT_BOT * H)
    xs = [x for x in range(x0, x1 + 1)
          if any(is_fg(px[x, y]) for y in range(sy0, sy1, 4))]
    if not xs:
        raise ValueError("no foreground in the shaft band")
    return min(xs), max(xs), sy0, sy1


def facade_profile(path: str | Path, box=None, bg: str = "auto",
                   bins: int = 40) -> dict:
    """Horizontal luminance profile of a tower shaft, plus the polarity statistics.

    box  — (x0, y0, x1, y1) normalised, when the tower cannot be segmented from the
           background. Required for shot renders; unnecessary for isolation plates.
    bg   — "light" (plate on white), "dark" (render on night), or "auto".
    """
    im, w, h = _luma(path)
    px = im.load()
    # Segment against the ACTUAL backdrop value, not a magic constant. A hardcoded
    # "light background is >= 240" silently classified an entire turntable render as
    # foreground, because the white backdrop resolved to 211 after the view transform.
    # The profile was then measured over the full frame width, every angle produced an
    # identical result, and nothing in the output said so.
    corners = [px[1, 1], px[w - 2, 1], px[1, h - 2], px[w - 2, h - 2]]
    bgv = sum(corners) / 4
    if bg == "auto":
        bg = "light" if bgv > 128 else "dark"
    margin = 22
    is_fg = ((lambda v: v < bgv - margin) if bg == "light"
             else (lambda v: v > bgv + margin))

    x0, x1, sy0, sy1 = _shaft_extent(px, w, h, is_fg, box)
    rows = range(sy0, sy1, 2)
    raw = [sum(px[x, y] for y in rows) / len(rows) for x in range(x0, x1 + 1)]
    lo, hi = min(raw), max(raw)
    norm = [(v - lo) / (hi - lo + 1e-9) for v in raw]

    def band(a, b):
        i, j = int(a * len(norm)), int(b * len(norm))
        return norm[i:j] or [0.0]

    outer = band(*OUTER[0]) + band(*OUTER[1])
    core = band(*CORE)
    outer_peak = max(outer)
    core_mean = sum(core) / len(core)
    # Peak-to-core, not mean-to-core: the claim under test is "bright STRIPS against a
    # dark core", and the outer band necessarily contains dark pier between the strips,
    # which would dilute a mean until the ratio said nothing.
    ratio = outer_peak / max(core_mean, 1e-3)

    binned = []
    for i in range(bins):
        a, b = int(i * len(norm) / bins), int((i + 1) * len(norm) / bins)
        seg = norm[a:b] or [norm[min(a, len(norm) - 1)]]
        binned.append(round(sum(seg) / len(seg), 3))

    # A shaft as wide as the frame means segmentation failed and this profile is of the
    # BACKDROP. Say so in the result: the silent version of this produced four identical
    # turntable angles that looked like a real measurement.
    warn = None
    if not box and (x1 - x0) > 0.97 * w:
        warn = (f"shaft spans {x1 - x0}px of a {w}px frame — segmentation almost "
                f"certainly failed (backdrop ~{bgv:.0f}); profile is not the subject")

    return {
        "profile": binned,
        "warning": warn,
        "shaft_px": x1 - x0,
        "outer_peak": round(outer_peak, 3),
        "core_mean": round(core_mean, 3),
        "outer_core_ratio": round(ratio, 2),
        "brightest_at": round(norm.index(max(norm)) / len(norm), 3),
        "raw_lo": round(lo, 1), "raw_hi": round(hi, 1),
    }


def render_profile(prof: list[float], width: int = 40) -> str:
    """A profile as four threshold rows. Cheap to eyeball, and legible in a log or a
    critique where an image cannot go."""
    out = []
    for lvl in (0.85, 0.60, 0.35, 0.15):
        out.append(f"  {lvl:.2f} |" + "".join("#" if v >= lvl else " " for v in prof))
    return "\n".join(out)


def compare_profiles(cand: dict, ref: dict) -> dict:
    """How far a candidate facade is from a reference facade.

    `l1` is the mean absolute difference between the normalised profiles -- shape, not
    brightness, because both are normalised to their own range. A tower rendered darker
    overall but with the same strip pattern scores well, which is correct: exposure is
    the grade's business, not the facade's.
    """
    a, b = cand["profile"], ref["profile"]
    n = min(len(a), len(b))
    l1 = sum(abs(a[i] - b[i]) for i in range(n)) / n
    return {
        "l1": round(l1, 3),
        "outer_core_ratio": (cand["outer_core_ratio"], ref["outer_core_ratio"]),
        "ratio_gap": round(cand["outer_core_ratio"] - ref["outer_core_ratio"], 2),
        "brightest_at": (cand["brightest_at"], ref["brightest_at"]),
    }
