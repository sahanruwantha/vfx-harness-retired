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
"""

from __future__ import annotations

from dataclasses import dataclass

from PIL import Image

# metric -> (relative tolerance, blocking?, human hint about which direction is "more")
SPEC: dict[str, tuple[float, bool, str]] = {
    "exposure_mean":     (0.30, True,  "overall brightness"),
    "clipped_pct":       (0.50, False, "blown highlights"),
    "black_pct":         (0.30, False, "crushed blacks"),
    "detail":            (0.35, True,  "high-frequency detail at every scale"),
    "points":            (0.50, True,  "distinct small light sources"),
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
    "structure_top":     (0.45, False, "texture/structure in the upper third"),
    "structure_mid":     (0.45, False, "texture/structure in the middle third"),
    "structure_bot":     (0.45, False, "texture/structure in the lower third"),
}


def _prep(im: Image.Image, width: int = 960) -> Image.Image:
    if im.width != width:
        im = im.resize((width, max(1, round(im.height * width / im.width))))
    return im


def look_vector(img: Image.Image | str, width: int = 960) -> dict[str, float]:
    """The full look vector for one frame. Resolution-normalised so a 1920x1080 render
    and a 1920x960 reference grab are comparable."""
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
        out[f"structure_{name}"] = (sum((v - m) ** 2 for v in vals) / max(len(vals), 1)) ** 0.5

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

    # halation: glow area per unit of hot core
    hot = halo = 0
    for y in range(0, H, 2):
        for x in range(0, W, 2):
            v = gp[x, y]
            if v >= 240: hot += 1
            elif v >= 120: halo += 1
    out["halation"] = halo / hot if hot else 0.0
    for name, (y0, y1) in (("top", (0, H // 3)), ("mid", (H // 3, 2 * H // 3)),
                           ("bot", (2 * H // 3, H))):
        bh = bl = 0
        for y in range(y0, y1, 2):
            for x in range(0, W, 2):
                v = gp[x, y]
                if v >= 240: bh += 1
                elif v >= 120: bl += 1
        out[f"halation_{name}"] = bl / bh if bh else 0.0

    # chroma_spread: colour variety among LIT pixels (all-one-colour windows -> ~0)
    rb = [cp[x, y][0] - cp[x, y][2] for y in range(0, H, 3) for x in range(0, W, 3)
          if gp[x, y] > 60]
    if len(rb) > 8:
        m = sum(rb) / len(rb)
        out["chroma_spread"] = (sum((v - m) ** 2 for v in rb) / len(rb)) ** 0.5
    else:
        out["chroma_spread"] = 0.0
    return {k: round(v, 3) for k, v in out.items()}


# Below this absolute difference a metric gap is noise, whatever the ratio says.
_FLOOR: dict[str, float] = {
    "exposure_mean": 6.0, "clipped_pct": 1.0, "black_pct": 5.0,
    "detail": 0.5, "detail_top": 0.5, "detail_mid": 0.5, "detail_bot": 0.5,
    "points": 200.0, "points_top": 200.0, "points_mid": 200.0, "points_bot": 200.0,
    "halation": 0.5, "halation_top": 0.8, "halation_mid": 0.8, "halation_bot": 0.8,
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
        return (f"{self.key} {self.got:g} vs ref {self.ref:g} "
                f"({abs(self.rel) * 100:.0f}% {arrow} — {self.hint})")


def compare(cand: dict, ref: dict, spec: dict = SPEC) -> list[Delta]:
    """Deltas beyond tolerance, worst first. The reference decides what matters: a metric
    the reference doesn't care about produces no delta no matter what the render does."""
    out = []
    for key, (tol, blocking, hint) in spec.items():
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
    return sorted(out, key=lambda d: (not d.blocking, -abs(d.rel)))


def report(deltas: list[Delta], limit: int = 6) -> str:
    if not deltas:
        return "metrics: all within tolerance of the reference"
    lines = [("✗ " if d.blocking else "· ") + str(d) for d in deltas[:limit]]
    n_block = sum(1 for d in deltas if d.blocking)
    head = (f"metric gaps vs reference ({len(deltas)} out of tolerance, "
            f"{n_block} blocking):")
    return head + "\n  " + "\n  ".join(lines)
