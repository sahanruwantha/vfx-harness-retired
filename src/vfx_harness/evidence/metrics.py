"""Look-agnostic image metrics — what to check is DERIVED from the reference.

The pipeline must generalise across shots that share nothing: a grey rack on a bare
floor, a green particle tower over a city, a street-level run. Enumerating "check glow
here, check the city there" per shot does not scale and is exactly the knowledge nobody
has up front.

So: compute the SAME vector on the reference and on the render, and let the deltas say
what matters. A metric where the reference is unremarkable tells you nothing; a metric
where the render diverges hard is the defect, whatever the subject happens to be.

Every metric here was added because it caught a real defect by hand:

    exposure_mean     barrel_roll M1 rendered mean 81 against a measured target of 53
                      and no critic mentioned it in any round
    halation          server_to_hansa M2 type had a 5.6x glow:core halo (ref 3.3x)
                      while the city had none — one bloom setting, two wrong answers
    detail            M5 gradient energy 2.72 vs ref 3.99: everything one smooth shape
    points            M5 horizon had 26 distinct lights; the reference has 114
    chroma_spread     every window the same colour (sigma 0.37) vs a real mix (16.4)
    streak_continuity fast motion ghosting into discrete copies instead of smearing,
                      on BOTH shots — a render setting, not a shot bug
    structure_*       flat fog-wall skies vs wispy structured cloud
    aniso_top         the same claim, actually delivered — see below
    local_range       a render sharp at every depth, with no atmospheric falloff

Two entries are here for reasons other than "it caught a defect", and are marked as such.

`hot_core` never caught anything; it exists so that suppressing an unmeasurable `halation`
does not also suppress a real one. See _HOT_FLOOR_PPM.

`aniso_top` exists because the `structure_*` line above is a claim this module could not
back. structure_* is a STANDARD DEVIATION — a histogram statistic — so it cannot see
spatial arrangement at all, and a sky of flat horizontal bands and a turbulent cloudscape
with the same value distribution score identically. Measured: a layer-5 render against
refs/f001_open.jpg reads structure_top 35.1 vs 30.5, 15% apart against a 45% tolerance,
i.e. silent, on two skies with nothing in common to look at. aniso_top separates that pair
at 76% and blocks.

The wider gap this opened, still open: every metric in this file is a histogram or an edge
count over ONE image, so a flat card and a solid tower with the same pixel statistics are
indistinguishable to all of them. `render_pass` already renders `depth` and `normal` and
nothing here consumes either. Until something does, "does the built thing read as
three-dimensional" is unmeasured — which is the axis a layer spent sixteen rounds failing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from PIL import Image

METRIC_SET = "vfx-harness.look-vector/v1"
FINGERPRINT_KEYS = (
    "exposure_mean",
    "clipped_pct",
    "black_pct",
    "band_mean_top",
    "structure_top",
    "band_mean_mid",
    "structure_mid",
    "band_mean_bot",
    "structure_bot",
    "hot_core",
    "halation",
    "detail",
    "points",
)

# metric -> (relative tolerance, blocking?, human hint about which direction is "more")
SPEC: dict[str, tuple[float, bool, str]] = {
    "exposure_mean":     (0.30, True,  "overall brightness"),
    "clipped_pct":       (0.50, False, "blown highlights"),
    "black_pct":         (0.30, False, "crushed blacks"),
    "detail":            (0.35, True,  "high-frequency detail at every scale"),
    "points":            (0.50, True,  "distinct small light sources"),
    "hot_core":          (0.50, False, "how much blown-out core there is to glow FROM"),
    "halation":          (0.50, False, "glow spread around hot cores"),
    "halation_top":      (0.55, False, "glow spread, upper third"),
    "halation_mid":      (0.55, False, "glow spread, middle third"),
    "halation_bot":      (0.55, False, "glow spread, lower third"),
    "chroma_spread":     (0.50, False, "colour variety among lit elements"),
    "streak_continuity": (0.45, False, "smooth motion smear vs discrete ghost copies"),
    "detail_top":        (0.40, False, "detail in the upper third (sky/cloud structure)"),
    "detail_mid":        (0.40, False, "detail in the middle third"),
    "detail_bot":        (0.40, True,  "detail in the lower third (ground/city mass)"),
    "points_top":        (0.60, False, "light sources in the upper third"),
    "points_mid":        (0.60, False, "light sources in the middle third"),
    "points_bot":        (0.60, True,  "light sources in the lower third (city/horizon)"),
    # Blocking, and structure_top beside it is not, which is the point. structure_top is a
    # standard deviation and cannot see spatial arrangement at all; on the layer-5 render
    # against f001_open it reads 15% apart against its own 45% tolerance — silent — on two
    # skies with nothing in common. aniso_top separates the same pair at 71%. It is the only
    # metric here that catches a banded fog-wall sky, so advice status would leave the defect
    # measurable and unenforceable, which is the condition that let sixteen rounds run.
    # Tolerance 0.40 against a measured worst-case self-inconsistency of 16% (2.5x margin).
    "aniso_top":         (0.40, True,  "sky texture: banded fog wall vs turbulent cloud"),
    # NOT blocking: it moves with `detail`, which is already blocking and already fires on
    # the same frames, so a veto here would mostly duplicate an existing veto. It earns its
    # place as the readout that says WHICH KIND of excess — local contrast (no atmospheric
    # falloff) rather than edge density.
    "local_range":       (0.45, False, "local contrast — haze suppresses it, sharp-everywhere does not"),
    "structure_top":     (0.45, False, "texture/structure in the upper third"),
    "structure_mid":     (0.45, False, "texture/structure in the middle third"),
    "structure_bot":     (0.45, False, "texture/structure in the lower third"),
}


# halation is a RATIO with a tiny count in the denominator, and it was reporting a number
# whatever that count happened to be. Measured on barrel_roll/refs/f045_pullback.jpg, one
# unchanged image read at six widths:
#
#     width   hot px (>=240)   halo px   halation
#       320          0            799       0.0
#       480          1           1799    1799.0
#       512          0           2022       0.0
#       640          0           3155       0.0
#       960          4           7078    1769.5
#
# Same picture, same glow, and the metric says either "none at all" or "1800x" depending
# on nothing but resampling. `if hot else 0.0` was reporting UNMEASURABLE as ZERO, and 0.0
# is not a neutral value here — it is the strongest possible claim the metric can make.
# That is the mechanism behind the 245375% delta recorded in Delta.__str__ below; the note
# there records the symptom, this records the cause.
#
# The denominator is a count, so its sampling noise is ~1/sqrt(hot). Requiring that noise
# to sit at a quarter of the metric's own 0.50 tolerance — a reading should not be able to
# move a quarter of the way to a verdict on resampling alone — gives 1/sqrt(hot) <= 0.125,
# so hot >= 64 at the 960-wide sampling grid this module measures on.
#
# That derivation is confirmed by the corpus rather than assumed. Across all 15 shot
# references the readings fall into two disjoint groups with nothing between them:
#
#     hot >= 133  ->  halation 2.3 .. 24.1   (11 refs, and stable across width)
#     hot <=  56  ->  halation 0.0, or 94 .. 1770
#
# 64 lands in the empty gap between 56 and 133. The noise argument and the corpus pick the
# same line, which is the only reason to trust either.
#
# Stated as a DENSITY rather than a count, because a threshold in raw pixels would make the
# answer depend on how large the image happens to be — which is the defect above wearing a
# different hat. 64 samples of a 960x540 grid (480*270 = 129,600 samples) is ~500 per
# million, so the floor is exactly "hot_core >= 500" and it shares units with that metric.
# Blown regions span many pixels, so reading the same plate at 2048 oversamples the same
# cores rather than earning new evidence.
#
# This narrows the disagreement between the two measurement paths in this repo (here at
# 960, blender.tools._region_metrics at _MAX_W) without closing it, and the residue is
# worth knowing about. Measured across the 10 barrel_roll plates, 9 land on the same side
# of the floor; f100_city reads 486 ppm at 960 and 669 at 2048 and straddles it. The bias
# has a direction: a >=240 threshold is nonlinear under resampling, so downscaling averages
# a small bright core with its dark neighbours and pushes it under. Plates well clear of
# the floor agree within ~2% (8646/8874, 5868/5863, 4392/4327); only sparse cores move
# (35/73, 130/209, 69/135). So a plate NEAR the floor can still be called measurable by one
# path and not the other. That is a borderline reading reported as borderline, which is the
# behaviour being bought here — not the old failure, where one plate read 0.0 and 1769.5.
_HOT_FLOOR_PPM = 500


def _prep(im: Image.Image, width: int = 960) -> Image.Image:
    if im.width != width:
        im = im.resize((width, max(1, round(im.height * width / im.width))))
    return im


def look_pair(cand: str, ref: str) -> tuple[dict[str, float], dict[str, float]]:
    """Look vectors for a candidate and its reference, measured at a COMMON width.

    look_vector() normalises to width 960, which is fine for one image and wrong for a
    comparison: a render at scale 0.4 is 768px wide and gets UPSCALED 1.25x, while the
    1920px reference downscales 0.5x. The two then travel different resampling paths and
    every detail metric reads soft — the exact asymmetry fixed in compare_frame, which I
    promptly reintroduced by routing the new signed-gap readout through look_vector.
    Found by the eval suite's scale-invariance check, not by me.

    Measuring at min(candidate, reference, 960) means neither image is ever upscaled.
    """
    with Image.open(cand) as c, Image.open(ref) as r:
        w = min(c.width, r.width, 960)
    return look_vector(cand, width=w), look_vector(ref, width=w)


# Reference plates do not change during a run, yet their metrics were recomputed on every
# comparison — refs/f100_city.jpg was re-derived ~6 times inside a single layer, each pass
# a full per-pixel walk. Keyed on (realpath, mtime_ns, size, width) so an edited or swapped
# plate is never served from a stale entry: silently comparing against the PREVIOUS version
# of a reference would be a far worse bug than the work this saves.
_VEC_CACHE: dict[tuple, dict[str, float]] = {}
_VEC_CACHE_MAX = 64


def look_vector(img: Image.Image | str, width: int = 960) -> dict[str, float]:
    """The full look vector for one frame. Resolution-normalised so a 1920x1080 render
    and a 1920x960 reference grab are comparable."""
    key = None
    if isinstance(img, str):
        try:
            st = os.stat(img)
            key = (os.path.realpath(img), st.st_mtime_ns, st.st_size, width)
        except OSError:
            key = None                  # unreadable — let the open() below raise properly
        if key is not None and key in _VEC_CACHE:
            return dict(_VEC_CACHE[key])   # a copy; callers mutate look vectors
    im = _prep(Image.open(img).convert("RGB") if isinstance(img, str) else img.convert("RGB"), width)
    g = im.convert("L")
    W, H = g.size
    gp, cp = g.load(), im.load()
    n_px = W * H

    total = clipped = black = 0
    for y in range(0, H, 2):
        for x in range(0, W, 2):
            v = gp[x, y]
            total += v
            if v >= 250: clipped += 1
            elif v <= 4: black += 1
    n = (H // 2 + H % 2) * (W // 2 + W % 2)
    out = {"exposure_mean": total / n,
           "clipped_pct": 100 * clipped / n,
           "black_pct": 100 * black / n}

    # band structure (local variation per horizontal third)
    for name, (y0, y1) in (("top", (0, H // 3)), ("mid", (H // 3, 2 * H // 3)),
                           ("bot", (2 * H // 3, H))):
        vals = [gp[x, y] for y in range(y0, y1, 2) for x in range(0, W, 2)]
        m = sum(vals) / max(len(vals), 1)
        out[f"band_mean_{name}"] = m
        out[f"structure_{name}"] = (sum((v - m) ** 2 for v in vals) / max(len(vals), 1)) ** 0.5

    # aniso_top: vertical vs horizontal gradient energy in the upper band.
    #
    # structure_top is a STANDARD DEVIATION, so it is a histogram statistic and throws away
    # spatial arrangement entirely: a sky of flat horizontal bands and a turbulent cloudscape
    # with the same value distribution score identically. Measured on the layer-5 render
    # against refs/f001_open.jpg, structure_top reads 35.1 vs 30.46 — 15% apart against its
    # own 45% tolerance, i.e. SILENT — on two skies that share nothing to look at. The
    # module docstring credits structure_* with catching "flat fog-wall skies vs wispy
    # structured cloud". On this pair it does not.
    #
    # Sky texture is spatial, so measure a spatial property. Banded cloud is continuous
    # along its bands and abrupt across them, so crossing them vertically dominates: the
    # render reads 1.43 against the reference's 0.84. Turbulent cloud is near-isotropic.
    # (My first guess had this backwards — I expected banding to raise the HORIZONTAL term.
    # It is the hard band BOUNDARIES that carry the energy, not the bands.)
    gtx = gty = tn = 0
    for y in range(1, max(2, H // 3 - 1), 2):
        for x in range(1, W - 1, 2):
            v = gp[x, y]
            gtx += abs(gp[x + 1, y] - v); gty += abs(gp[x, y + 1] - v); tn += 1
    out["aniso_top"] = gty / max(gtx, 1e-9)

    # local_range: median of (max - min) within a small block — LOCAL contrast, as opposed
    # to detail's edge density and structure's global spread. A frame with atmosphere has
    # its local contrast suppressed by distance haze; one rendered with everything equally
    # sharp at every depth does not. The block is a fraction of width so it stays the same
    # fraction of the picture at any measuring resolution.
    blk = max(8, W // 60)
    ranges = []
    for by in range(0, max(1, H - blk), blk * 2):
        for bx in range(0, max(1, W - blk), blk * 2):
            b = [gp[x, y] for y in range(by, by + blk, 3) for x in range(bx, bx + blk, 3)]
            if b:
                ranges.append(max(b) - min(b))
    ranges.sort()
    out["local_range"] = float(ranges[len(ranges) // 2]) if ranges else 0.0

    # detail: mean absolute gradient — one number for "is there information here"
    gx = gy = 0
    cnt = 0
    for y in range(0, H - 1, 2):
        for x in range(0, W - 1, 2):
            v = gp[x, y]
            gx += abs(gp[x + 1, y] - v); gy += abs(gp[x, y + 1] - v); cnt += 1
    out["detail"] = (gx + gy) / max(cnt, 1)
    # Per-band detail and points. Whole-frame averages hide regional failures: M5's
    # horizon has 26 lights against the reference's 114, but our bright tower lifts the
    # frame average enough that nothing tripped until the bands were split out.
    for name, (y0, y1) in (("top", (0, H // 3)), ("mid", (H // 3, 2 * H // 3)),
                           ("bot", (2 * H // 3, H))):
        bg = bn = 0
        for y in range(y0, max(y0 + 1, y1 - 1), 2):
            for x in range(0, W - 1, 2):
                v = gp[x, y]
                bg += abs(gp[x + 1, y] - v) + abs(gp[x, y + 1] - v); bn += 1
        out[f"detail_{name}"] = bg / max(bn, 1)
        bp = 0
        for y in range(max(y0, 1), min(y1, H - 1)):
            for x in range(1, W - 1):
                v = gp[x, y]
                if v > 180 and v >= gp[x - 1, y] and v > gp[x + 1, y] \
                   and v >= gp[x, y - 1] and v > gp[x, y + 1]:
                    bp += 1
        out[f"points_{name}"] = bp * 1e6 / max((y1 - y0) * W, 1)
    # streak_continuity: horizontal smear kills HORIZONTAL gradients but keeps vertical
    # ones. Discrete ghost copies have hard vertical edges, so gx stays high. <1 = smeared.
    out["streak_continuity"] = gx / max(gy, 1)

    # points: distinct bright local maxima per megapixel — "how many separate lights"
    pts = 0
    for y in range(1, H - 1):
        for x in range(1, W - 1):
            v = gp[x, y]
            if v > 180 and v >= gp[x - 1, y] and v > gp[x + 1, y] \
               and v >= gp[x, y - 1] and v > gp[x, y + 1]:
                pts += 1
    out["points"] = pts * 1e6 / n_px

    # halation: glow area per unit of hot core, PLUS the size of the core it divides by.
    # Two numbers because they answer different questions and only one of them is always
    # answerable. See _HOT_FLOOR_PPM: below that density the ratio is an artifact of its
    # denominator, so it is omitted rather than reported as 0.0.
    hot = halo = 0
    for y in range(0, H, 2):
        for x in range(0, W, 2):
            v = gp[x, y]
            if v >= 240: hot += 1
            elif v >= 120: halo += 1
    out["hot_core"] = hot * 1e6 / n
    if out["hot_core"] >= _HOT_FLOOR_PPM:
        out["halation"] = halo / hot
    for name, (y0, y1) in (("top", (0, H // 3)), ("mid", (H // 3, 2 * H // 3)),
                           ("bot", (2 * H // 3, H))):
        bh = bl = bn = 0
        for y in range(y0, y1, 2):
            for x in range(0, W, 2):
                v = gp[x, y]
                bn += 1
                if v >= 240: bh += 1
                elif v >= 120: bl += 1
        # Against the BAND's own sample count, so a third of the frame is held to the same
        # density as the whole of it rather than to a third of the evidence.
        if bn and bh * 1e6 / bn >= _HOT_FLOOR_PPM:
            out[f"halation_{name}"] = bl / bh

    # chroma_spread: colour variety among LIT pixels (all-one-colour windows -> ~0)
    rb = [cp[x, y][0] - cp[x, y][2] for y in range(0, H, 3) for x in range(0, W, 3)
          if gp[x, y] > 60]
    if len(rb) > 8:
        m = sum(rb) / len(rb)
        out["chroma_spread"] = (sum((v - m) ** 2 for v in rb) / len(rb)) ** 0.5
    else:
        out["chroma_spread"] = 0.0
    vec = {k: round(v, 3) for k, v in out.items()}
    if key is not None:
        if len(_VEC_CACHE) >= _VEC_CACHE_MAX:
            _VEC_CACHE.pop(next(iter(_VEC_CACHE)))     # plain FIFO; refs are few
        _VEC_CACHE[key] = dict(vec)
    return vec


def canonical_fingerprint(img: Image.Image | str) -> dict:
    """A typed reference fingerprint produced by the one canonical metric registry."""
    values = look_vector(img)
    return {
        "metric_set": METRIC_SET,
        "values": {key: values[key] for key in FINGERPRINT_KEYS if key in values},
    }


# Below this absolute difference a metric gap is noise, whatever the ratio says.
_FLOOR: dict[str, float] = {
    "exposure_mean": 6.0, "clipped_pct": 1.0, "black_pct": 5.0,
    "detail": 0.5, "detail_top": 0.5, "detail_mid": 0.5, "detail_bot": 0.5,
    "points": 200.0, "points_top": 200.0, "points_mid": 200.0, "points_bot": 200.0,
    "hot_core": 200.0,
    "halation": 0.5, "halation_top": 0.8, "halation_mid": 0.8, "halation_bot": 0.8,
    # aniso_top is a ratio around 1, so its floor is absolute-in-ratio-units: 0.25 keeps the
    # near-black acceptance moment quiet (f195_black's own reading swings 16% under
    # resampling alone) while passing the 0.50-0.59 gaps the real defect produces.
    "aniso_top": 0.25, "local_range": 6.0,
    "chroma_spread": 3.0, "streak_continuity": 0.15,
    "structure_top": 4.0, "structure_mid": 4.0, "structure_bot": 4.0,
}


@dataclass
class Delta:
    key: str
    got: float
    ref: float
    rel: float          # signed relative deviation
    blocking: bool
    hint: str

    def __str__(self) -> str:
        arrow = "LOW" if self.rel < 0 else "HIGH"
        # A percentage against a ~zero reference is meaningless and shouts over the real
        # findings: the first live feedback read "halation_mid 1963 vs ref 0 (245375%
        # HIGH)", burying detail_bot 77% LOW, which was the actual defect. This clause
        # stops that reading from SHOUTING; it does not stop it from being wrong. The ref 0
        # was never a measurement — see _HOT_FLOOR_PPM, which now keeps it out of the vector.
        if abs(self.ref) < _FLOOR.get(self.key, 0.5):
            return (f"{self.key} {self.got:g} vs ref ~0 "
                    f"(reference has none of this — {self.hint})")
        pct = min(abs(self.rel) * 100, 999)
        cap = ">" if abs(self.rel) * 100 > 999 else ""
        return (f"{self.key} {self.got:g} vs ref {self.ref:g} "
                f"({cap}{pct:.0f}% {arrow} — {self.hint})")


def compare(cand: dict, ref: dict, spec: dict = SPEC) -> list[Delta]:
    """Deltas beyond tolerance, worst first. The reference decides what matters: a metric
    the reference doesn't care about produces no delta no matter what the render does."""
    out = []
    for key, (tol, blocking, hint) in spec.items():
        # An ABSENT key means "this frame could not support that measurement", which is not
        # the same as a value and must never be substituted for with one. look_vector omits
        # halation rather than dividing by a 4-pixel core; the correct response to a missing
        # measurement is to say nothing, so silence here is the point rather than a shortcut.
        if key not in cand or key not in ref:
            continue
        r, c = ref[key], cand[key]
        # Relative deviation is meaningless when the reference is ~0 (a 0.017% vs 0%
        # clip difference reported as "1700000% HIGH"). Require the gap to matter in
        # ABSOLUTE terms too, on a per-metric floor.
        floor = _FLOOR.get(key, 0.5)
        if abs(c - r) < floor:
            continue
        rel = (c - r) / max(abs(r), floor)
        if abs(rel) > tol:
            out.append(Delta(key, c, r, rel, blocking, hint))
    return sorted(out, key=lambda d: (not d.blocking, -min(abs(d.rel), 9.99)))


def report(deltas: list[Delta], limit: int = 6) -> str:
    if not deltas:
        return "metrics: all within tolerance of the reference"
    lines = [("✗ " if d.blocking else "· ") + str(d) for d in deltas[:limit]]
    n_block = sum(1 for d in deltas if d.blocking)
    head = (f"metric gaps vs reference ({len(deltas)} out of tolerance, "
            f"{n_block} blocking):")
    return head + "\n  " + "\n  ".join(lines)
