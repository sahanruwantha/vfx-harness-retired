"""Executable checks — the plan's done-conditions as artifacts instead of prose.

WHY THIS EXISTS, and why it replaces three parsers.

Every gate added to this pipeline before it read English and inferred structure, and every
one needed three rounds of false-positive repair: `build/01_layout.py` read as a dead
citation when it was a deliverable; `spike #1` read as an uncited claim when it was the
spike tool's own wording; `mean 66-84` read as a frame mean when it was a band mean. That
is not three bugs. It is one root cause — prose is the interface between stages, so every
consumer re-derives intent by guessing — wearing three faces.

The second root cause is that a check was authored by the agent doing the work and never
run before shipping. `mean(shadow pier) >= 14` was written and never executed; nothing
would execute it until a build agent did, twenty minutes and real money later. That single
fact produced all three defects an independent review found:

    unreachable   L5a wants lit/shadow pier ratio 1.35-2.2; refs/f440_final.jpg reads 1.06
    unreachable   L6d wants whole-frame G/R < 1.08 at f440; the plate reads 1.139 — and the
                  SAME plan had already caught this exact failure at f100, written it up as
                  numbered resolved decision 4, and then committed it again 600 lines later
    toothless     L5a wants mean(shadow pier) >= 14, a FLOOR, against a defect that was a
                  CEILING: v1's tower had no dark side at all and passes it comfortably

So a check is a record, not a sentence, and it may not enter a plan until it has been RUN.
Three rules, enforced by `verify()`, all three necessary:

    1. the REFERENCE satisfies it        — else the target is unreachable and the layer
                                           loops until its budget is gone
    2. a KNOWN-BAD artifact fails it     — else it cannot catch the defect it exists for
    3. the gap exceeds the NOISE FLOOR   — else it is a coin flip. The floor is measured
                                           here by resampling, never declared by the author,
                                           so it cannot be talked down

Rule 3 is what would have rejected scoring a fog-wall sky on `structure_top`: that metric
separates two skies with nothing in common by 15% against its own 45% tolerance, while
`aniso_top` separates the same pair by 76%.

REGIONS are normalised (x0, y0, x1, y1) in 0..1 with origin TOP-LEFT (x right, y down),
so a check means the same thing at any render scale, and so "the shadow pier" stops being
a phrase a reader has to interpret.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from PIL import Image

from vfx_harness.domain.contracts import active_for, load_document, validate_lifecycle
from vfx_harness.evidence.metrics import _prep, look_vector
from vfx_harness.orchestration.plan_authority import selected_artifact_path

if TYPE_CHECKING:
    from vfx_harness.orchestration.authority_selection import ResolvedSelectedAuthority

# Where in the pipeline a measurement is valid. Both independent reviewers of the current
# plan flagged the same defect: absolute band means measured off GRADED reference JPEGs,
# enforced on layers that run before any view transform exists.
STAGES = ("pre_grade", "post_grade", "any")


def _crop(im: Image.Image, box) -> Image.Image:
    W, H = im.size
    x0, y0, x1, y1 = box
    return im.crop((int(W * x0), int(H * y0), max(int(W * x0) + 1, int(W * x1)), max(int(H * y0) + 1, int(H * y1))))


def _px(im: Image.Image) -> list[int]:
    g = im.convert("L")
    return list(g.getdata())


def _region_stats(im: Image.Image, box) -> dict:
    p = _px(_crop(im, box))
    n = len(p) or 1
    mean = sum(p) / n
    return {
        "mean": mean,
        "min": min(p),
        "max": max(p),
        "sigma": (sum((v - mean) ** 2 for v in p) / n) ** 0.5,
        "p5": sorted(p)[int(0.05 * (n - 1))],
        "lit_pct": 100 * sum(1 for v in p if v >= 120) / n,
    }


def _lit_variance(im: Image.Image, box, n: int = 6) -> float:
    """Spread of LIT FRACTION across n x n sub-blocks of a region.

    The brief's first anti-goal is "windows on a regular grid, identical spacing, one
    colour", and layer 2 shipped exactly that and scored 3. Nothing here could see it:
    every metric is a histogram or an edge count, and a uniform lattice with varied
    per-cell brightness satisfies region_sigma completely.

    The first attempt at a fix measured SPATIAL PERIODICITY by autocorrelation and had to
    be thrown away — the references scored MORE periodic than the render (0.857 and 0.885
    against 0.689), because real towers genuinely do have regular window columns. The
    anti-goal was never about spacing.

    It is about OCCUPANCY. Measured on the same crops, the render and the references carry
    the same mean lit fraction (~0.11) and differ 2x in its spread:

        L2 render   lit-fraction sd 0.080   (mean 0.108)
        ref f001    lit-fraction sd 0.157   (mean 0.111)
        ref f440    lit-fraction sd 0.168   (mean 0.196)

    The render lights the right PROPORTION of windows and spreads them evenly; a real
    facade clusters them, so dark floors sit next to busy ones.
    """
    src = _crop(im, box)
    px = src.convert("L").load()
    W, H = src.size
    fr = []
    for j in range(n):
        for i in range(n):
            ax, bx = W * i // n, max(W * i // n + 1, W * (i + 1) // n)
            ay, by = H * j // n, max(H * j // n + 1, H * (j + 1) // n)
            v = [px[x, y] for y in range(ay, by, 2) for x in range(ax, bx, 2)]
            if v:
                fr.append(sum(1 for q in v if q >= 90) / len(v))
    if not fr:
        return 0.0
    m = sum(fr) / len(fr)
    return (sum((f - m) ** 2 for f in fr) / len(fr)) ** 0.5


def _green_excess(im: Image.Image, box=None) -> float:
    src = _crop(im, box) if box else im
    px = src.convert("RGB").load()
    W, H = src.size
    R = G = 0
    for y in range(0, H, 2):
        for x in range(0, W, 2):
            r, g, _ = px[x, y]
            R += r
            G += g
    return G / max(R, 1)


# metric name -> (needs_regions, fn(image, regions) -> float)
# Explicit registry: a plan naming a metric that is not here is rejected at plan time
# rather than discovered by a build agent mid-layer. `measure_regions` returns no `min`,
# and a ticket told a junior to use it to check exactly the quantity it cannot report.
def _m_frame(key):
    return lambda im, rg, _c={}: look_vector(im)[key]


METRICS: dict[str, tuple[tuple[str, ...], object]] = {
    "frame_mean": ((), _m_frame("exposure_mean")),
    "frame_black_pct": ((), _m_frame("black_pct")),
    "frame_detail": ((), _m_frame("detail")),
    "frame_halation": ((), _m_frame("halation")),
    "frame_aniso_top": ((), _m_frame("aniso_top")),
    "frame_points": ((), _m_frame("points")),
    "green_excess": ((), lambda im, rg: _green_excess(im)),
    "region_mean": (("r",), lambda im, rg: _region_stats(im, rg["r"])["mean"]),
    "region_min": (("r",), lambda im, rg: _region_stats(im, rg["r"])["min"]),
    "region_p5": (("r",), lambda im, rg: _region_stats(im, rg["r"])["p5"]),
    "region_max": (("r",), lambda im, rg: _region_stats(im, rg["r"])["max"]),
    "region_sigma": (("r",), lambda im, rg: _region_stats(im, rg["r"])["sigma"]),
    "region_lit_pct": (("r",), lambda im, rg: _region_stats(im, rg["r"])["lit_pct"]),
    "region_green_excess": (("r",), lambda im, rg: _green_excess(im, rg["r"])),
    "region_lit_variance": (("r",), lambda im, rg: _lit_variance(im, rg["r"])),
    "region_ratio": (
        ("a", "b"),
        lambda im, rg: _region_stats(im, rg["a"])["mean"] / max(_region_stats(im, rg["b"])["mean"], 1e-6),
    ),
}


def _metric_regions(metric: str, regions: dict) -> dict:
    """Resolve semantic region labels into the metric's positional operands."""
    if metric != "region_ratio" or ("a" in regions and "b" in regions):
        return regions
    if "numerator" in regions and "denominator" in regions:
        return {**regions, "a": regions["numerator"], "b": regions["denominator"]}
    if len(regions) == 2:
        # JSON preserves author order. This lets checks say `orb_high` / `orb_low` while
        # retaining the declared numerator/denominator direction without a parallel map.
        first, second = regions.values()
        return {**regions, "a": first, "b": second}
    raise KeyError(
        "metric 'region_ratio' needs exactly two semantic regions (first/second = "
        "numerator/denominator), explicit numerator+denominator, or legacy a+b"
    )


@dataclass
class Check:
    id: str
    metric: str
    op: str  # ">=" | "<=" | "band"
    lo: float = float("-inf")
    hi: float = float("inf")
    ref: str = ""
    frame: int | None = None
    layer: str = ""
    owner_layer: str = ""
    fault_owner: str = ""
    activates_at: str = ""
    lifecycle: str = "layer"
    valid_through: str | None = None
    axis: str = ""
    stage: str = "any"
    regions: dict = field(default_factory=dict)
    # The artifacts this check EXISTS TO REJECT. Naming them is the author's real work:
    # rule 2 is only as strong as the negative it is tested against, and a corpus-wide
    # "some bad render fails it" is far too easy. Scoring a sky on band sigma looked
    # discriminating only because a LAYOUT render with no sky at all failed it — while the
    # banded-fog-wall render it was actually meant to catch sailed through. A check whose
    # adversary is unnamed is graded against whatever happens to be lying around.
    rejects: list[str] = field(default_factory=list)
    # What measure_check RETURNED when this spec was authored: {"ref": .., "adversary": [..]}.
    # The gate recomputes it. L3c-2 shipped with its proof carried in free-text `note`
    # ("ref 23.74, adversary 5.67") while the region it shipped measures 18.50/13.01 — the
    # planner tested one box and shipped another, and nothing connected the two. Moving the
    # CHECK out of prose while leaving its PROOF in prose left the last mile self-certified,
    # which is the same shape as [unknown]=0 and zero source URLs.
    proof: dict = field(default_factory=dict)
    note: str = ""

    def holds(self, v: float) -> bool:
        return self.lo <= v <= self.hi

    def target(self) -> str:
        if self.op == "band":
            return f"{self.lo:g}..{self.hi:g}"
        return f">= {self.lo:g}" if self.hi == float("inf") else f"<= {self.hi:g}"

    @staticmethod
    def from_dict(d: dict) -> Check:
        op = d.get("op", "band")
        lo = float(d.get("lo", float("-inf")))
        hi = float(d.get("hi", float("inf")))
        owner = str(d.get("owner_layer") or d.get("layer") or "")
        return Check(
            id=str(d.get("id", "?")),
            metric=str(d.get("metric", "")),
            op=op,
            lo=lo,
            hi=hi,
            ref=d.get("ref", ""),
            frame=d.get("frame"),
            layer=owner,
            owner_layer=owner,
            fault_owner=str(d.get("fault_owner") or owner),
            activates_at=str(d.get("activates_at") or owner),
            lifecycle=str(d.get("lifecycle") or "layer"),
            valid_through=(str(d["valid_through"]) if d.get("valid_through") is not None else None),
            axis=d.get("axis", ""),
            stage=d.get("stage", "any"),
            regions={k: tuple(v) for k, v in (d.get("regions") or {}).items()},
            rejects=list(d.get("rejects") or []),
            proof=dict(d.get("proof") or {}),
            note=d.get("note", ""),
        )


def evaluate(check: Check, image: str | Path) -> float:
    """The check's measured value on one image. The single evaluator both the PLANNER (to
    author a check) and the GATE (to re-verify it) call — which is what makes a proof
    impossible to fabricate."""
    spec = METRICS.get(check.metric)
    if spec is None:
        raise KeyError(f"unknown metric '{check.metric}'; known: {sorted(METRICS)}")
    needs, fn = spec
    regions = _metric_regions(check.metric, check.regions)
    missing = [k for k in needs if k not in regions]
    if missing:
        raise KeyError(f"metric '{check.metric}' needs region(s) {missing}")
    if check.metric.startswith("frame_"):
        return float(fn(str(image), regions))
    with Image.open(str(image)) as im:
        return float(fn(_prep(im.convert("RGB"), 960), regions))


def _threshold_window(check: Check, value: float, margin: float, prior, floor: float) -> str:
    """State the thresholds that would have been legal, not just that this one was not.

    A rejection that names only the failed verdict makes the author search. hansa_silk_road
    layer 2 spent $12.62 over 48 mutations and 62 renders on a `frame_detail` debt whose
    legal window had already closed; 3 of its 47 candidate plates did admit a threshold and
    it could not see which. The window is two-sided: above the adversary, or the check is
    NOT NECESSARY; at or below value - margin, or it is FRAGILE.
    """
    if check.op not in (">=", "<="):
        return ""
    bound = value - margin if check.op == ">=" else value + margin
    side = "lo" if check.op == ">=" else "hi"
    out = [f". Legal {side} on this render: "]
    if prior is None:
        out.append(f"{side} <= {bound:.4g}" if check.op == ">=" else f"{side} >= {bound:.4g}")
    elif check.op == ">=":
        out.append(f"({float(prior):.4g}, {bound:.4g}]" if bound > float(prior)
                   else f"EMPTY — needs lo > {float(prior):.4g} (adversary) and "
                        f"lo <= {bound:.4g} (value {value:.4g} - margin {margin:.4g})")
    else:
        out.append(f"[{bound:.4g}, {float(prior):.4g})" if bound < float(prior)
                   else f"EMPTY — needs hi < {float(prior):.4g} (adversary) and "
                        f"hi >= {bound:.4g}")
    if 2 * floor > 0.05 * max(abs(value), 1.0):
        out.append(
            f". The margin here is 2x the measured noise floor {floor:.4g}, not the 5% "
            "term. That floor is measured by resampling, and it rises with the "
            "high-frequency content this metric rewards — so adding more detail can "
            "narrow this window rather than widen it. If it is EMPTY, a different "
            "property or a region-scoped subject will pay where more signal will not"
        )
    return "".join(out)


def noise_floor(check: Check, image: str | Path, scales=(0.5, 0.75, 1.0)) -> float:
    """How much this metric moves on ONE unchanged image under resampling alone.

    Measured, never declared. An author who could state their own noise floor would state
    whatever made their check pass, which is the failure this whole module exists to stop.
    """
    vals = []
    with Image.open(str(image)) as src:
        base = src.convert("RGB")

        for s in scales:
            d = Path(tempfile.mkdtemp()) / f"s{s}.png"
            base.resize((max(8, round(base.width * s)), max(8, round(base.height * s))), Image.LANCZOS).save(d)
            with contextlib.suppress(Exception):
                vals.append(evaluate(check, d))
    return (max(vals) - min(vals)) if len(vals) > 1 else 0.0


def _find_reject(rel: str, ref: Path, root: Path | None) -> Path | None:
    """Resolve an adversary path the way a human would expect it to resolve.

    The plan agent runs with its cwd set to the SHOT folder while the gate runs from the
    repo root, so the same string means two different files depending on who reads it. The
    first version resolved only against the caller's root; the planner hit that within
    minutes ("relative rejects paths fail to resolve - switching to absolute") and worked
    around it with absolute paths, which would then not survive the shot being moved.
    Try every base that could sensibly be meant, and accept the first that exists.
    """
    p = Path(rel)
    if p.is_absolute():
        return p if p.is_file() else None
    shot = Path(ref).parent.parent  # <shot>/refs/x.jpg -> <shot>
    for base in (shot, shot.parent, Path(root or "."), Path.cwd()):
        q = base / p
        if q.is_file():
            return q
    return None


@dataclass
class Verdict:
    check_id: str
    ok: bool
    ref_value: float | None = None
    bad_values: list[float] = field(default_factory=list)
    floor: float = 0.0
    reasons: list[str] = field(default_factory=list)

    def why(self) -> str:
        """Every reason this verdict carries, whole.

        A rejection built to teach was rendered by each consumer with its own private
        slice: the builder's payment surface cut it at 120 characters, mid-word and
        before the legal threshold window began, and took `reasons[0]` so a verdict
        carrying FRAGILE and NOT NECESSARY showed one of them. The window landed on main,
        was tested at the point of production, and never reached a builder (HIR-0244).
        """
        return " · ".join(reason for reason in self.reasons if reason) or "failed verification"


def verify(check: Check, ref: Path, known_bad: list[Path], root: Path | None = None) -> Verdict:
    """The three rules. All three, because each catches a defect the others do not.

    `check.rejects` overrides the corpus: a check is graded against the artifact it NAMES as
    its adversary, and only falls back to "anything bad lying around" when it names none —
    which is reported, because that fallback is how a weak check looks strong.
    """
    v = Verdict(check.id, True)
    named = [q for q in (_find_reject(r, ref, root) for r in check.rejects) if q]
    if check.rejects and not named:
        return Verdict(check.id, False, reasons=[f"names adversaries that do not resolve: {check.rejects}"])
    if named:
        known_bad = named
    else:
        v.reasons.append(
            "WEAK — this check names no adversary, so rule 2 grades it against whatever "
            "known-bad renders exist. Declare `rejects` with the artifact showing the "
            "defect it targets, or it can pass by rejecting an easy unrelated failure"
        )
    try:
        v.ref_value = evaluate(check, ref)
    except Exception as e:
        return Verdict(check.id, False, reasons=[f"cannot be evaluated: {e}"])

    # 1. the reference must satisfy it, or the target is unreachable
    if not check.holds(v.ref_value):
        v.ok = False
        v.reasons.append(
            f"UNREACHABLE — the reference {Path(ref).name} reads {v.ref_value:.3g} and the "
            f"check demands {check.target()}. No correct render can pass it; the layer "
            f"aiming at it loops until its budget is gone or satisfies it by breaking "
            f"something the reference contains"
        )

    for b in known_bad:
        try:
            v.bad_values.append(evaluate(check, b))
        except Exception:
            continue
    # 2. some known-bad artifact must FAIL it, or it cannot catch anything
    if v.bad_values and all(check.holds(x) for x in v.bad_values):
        v.ok = False
        v.reasons.append(
            f"TOOTHLESS — every known-bad render passes it too "
            f"({min(v.bad_values):.3g}..{max(v.bad_values):.3g}). A check that accepts the "
            f"artifact it was written to reject cannot fail the defect it exists for; look "
            f"at its DIRECTION, since a floor where the defect was a ceiling accepts "
            f"exactly the wrong picture"
        )

    # 4. the shipped spec must reproduce the proof recorded with it. A check whose stated
    #    evidence does not come back is a check that was tested in a form nobody kept.
    if check.proof and v.ref_value is not None:
        claimed = check.proof.get("ref")
        if claimed is not None:
            tol = max(0.02 * abs(float(claimed)), 2 * (v.floor or 0.0), 1e-6)
            if abs(v.ref_value - float(claimed)) > tol:
                v.ok = False
                v.reasons.append(
                    f"PROOF DOES NOT REPRODUCE — the record claims the reference reads "
                    f"{float(claimed):.4g} and this spec measures {v.ref_value:.4g}. The spec "
                    f"that was tested is not the spec that shipped; re-run measure_check on "
                    f"the regions actually written here and record what it returns"
                )
        adv = check.proof.get("adversary")
        if adv and v.bad_values:
            want, got = float(adv[0]), v.bad_values[0]
            tol = max(0.02 * abs(want), 2 * (v.floor or 0.0), 1e-6)
            if abs(got - want) > tol:
                v.ok = False
                v.reasons.append(
                    f"PROOF DOES NOT REPRODUCE — the record claims the adversary reads "
                    f"{want:.4g} and this spec measures {got:.4g}"
                )

    # 5. a region check must survive a small nudge to its own box. L3c-2's region is a
    #    4%-tall strip straddling the horizon, so a 3% shift swung its adversary 2.4x
    #    (5.55 -> 13.01). Rule 3 measures RESAMPLING noise; this measures SPEC sensitivity,
    #    and a target that is not stable under perturbation is not a target.
    if v.ok and check.regions and v.bad_values:
        for name, box in check.regions.items():
            x0, y0, x1, y1 = box
            dy = max(0.01, 0.02 * (y1 - y0))
            for shift in (-dy, dy):
                alt = dict(check.regions)
                alt[name] = (x0, max(0.0, y0 + shift), x1, min(1.0, y1 + shift))
                probe = Check(check.id, check.metric, check.op, check.lo, check.hi, regions=alt)
                try:
                    r2, b2 = evaluate(probe, ref), evaluate(probe, known_bad[0])
                except Exception:
                    continue
                if not probe.holds(r2) or probe.holds(b2):
                    v.ok = False
                    v.reasons.append(
                        f"FRAGILE — nudging region '{name}' by {shift:+.3f} in y makes this "
                        f"check stop working (reference {r2:.4g}, adversary {b2:.4g}). The "
                        f"verdict is an artifact of exactly where the box was put, so a "
                        f"builder reproducing it slightly differently gets a different answer"
                    )
                    break
            if not v.ok:
                break

    # 3. the gap must beat the instrument's own noise
    if v.ref_value is not None and v.bad_values:
        v.floor = noise_floor(check, ref)
        gap = min(abs(v.ref_value - x) for x in v.bad_values)
        if gap <= v.floor:
            v.ok = False
            v.reasons.append(
                f"INDISCRIMINATE — reference and known-bad differ by {gap:.3g}, within this "
                f"metric's own resampling noise of {v.floor:.3g}. The check cannot tell them "
                f"apart, so passing it is a coin flip"
            )
    return v


def load(path: Path) -> list[Check]:
    """Load strict planner image contracts; legacy list documents are rejected."""

    rows = load_document(path, "checks")
    errors = [
        (str(row.get("id", "?")), validate_lifecycle(row))
        for row in rows
        if isinstance(row, dict) and validate_lifecycle(row)
    ]
    if errors:
        rid, error = errors[0]
        raise ValueError(f"{rid}: {error}")
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError("every image contract must be an object")
    for row in rows:
        focus = row.get("focus")
        if focus is None:
            continue
        if not isinstance(focus, dict) or focus.get("required") is not True:
            raise ValueError(f"{row.get('id')}: focus must be an object with required=true")
        crop = focus.get("crop")
        if (
            not isinstance(crop, list)
            or len(crop) != 4
            or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in crop)
            or not all(0 <= float(v) <= 1 for v in crop)
            or not (crop[0] < crop[2] and crop[1] < crop[3])
        ):
            raise ValueError(f"{row.get('id')}: focus.crop must be a normalized TOP-LEFT box")
        if not str(focus.get("reason") or "").strip():
            raise ValueError(f"{row.get('id')}: focus.reason is required")
    checks = [Check.from_dict(d) for d in rows]
    for check in checks:
        if check.metric not in METRICS:
            raise ValueError(f"{check.id}: unknown metric {check.metric!r}; known: {sorted(METRICS)}")
        if check.stage not in STAGES:
            raise ValueError(f"{check.id}: stage must be one of {STAGES}")
        if not check.ref:
            raise ValueError(f"{check.id}: ref is required")
        # Resolve operand names now; a missing region should return to the planner's warm
        # session rather than surface during the first build evaluation.
        regions = _metric_regions(check.metric, check.regions)
        missing = [key for key in METRICS[check.metric][0] if key not in regions]
        if missing:
            raise ValueError(f"{check.id}: metric {check.metric!r} needs regions {missing}")
    return checks


def load_image_contract_payment_rows(
    shot_folder: str | Path,
    *,
    selected_authority: ResolvedSelectedAuthority | None = None,
) -> list[dict]:
    """Planner ``checks.json`` rows plus builder ``runtime_checks.json`` payments.

    An image-contract debt is paid only when a row here matches the compiled card
    (id, frame, property kind, axis). Sibling leftover runtime rows are ignored
    at freeze because they do not match an owed card (HIR-0048).
    """
    root = Path(shot_folder)
    rows: list[dict] = []
    if selected_authority is None:
        planner_spec = selected_artifact_path(root, "checks.json")
    elif selected_authority.plan is None:
        planner_spec = root / "checks.json"
    else:
        try:
            planner_spec = selected_authority.artifact_paths["checks.json"]
        except KeyError as exc:
            raise ValueError(
                "selected image-payment authority omits checks.json"
            ) from exc
    try:
        for row in load_document(planner_spec, "checks"):
            if isinstance(row, dict) and row.get("id"):
                rows.append(row)
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    rows.extend(valid_runtime_image_payment_rows(root))
    return rows


def _matches_image_moment(row: dict, *, frame: int, ref: str) -> bool:
    """Match one image contract to its exact authored moment selector."""

    if row.get("frame") is not None:
        try:
            return int(row["frame"]) == int(frame)
        except (TypeError, ValueError):
            return False
    return not row.get("ref") or str(row.get("ref")) == str(ref)


def layer_evidence(
    shot_folder: str | Path,
    layer_id: str,
    *,
    frame: int,
    ref: str,
    render: str | Path,
    stage: str = "pre_grade",
    selected_authority: ResolvedSelectedAuthority | None = None,
) -> list[dict]:
    """Evaluate this layer's executable checks on the image being judged.

    The critic used to receive exact framing bands in prose and then estimate them from
    a downscaled JPEG.  On ``beacon_wake`` it called a measured 0.1634-W ring 0.11-W
    twice and sent a correct script through an $11 repair.  Checks are the machine's
    measurements, so put their *current* values beside the image instead of leaving the
    critic to rediscover them by eye.

    A check without an explicit frame is matched by its reference path.  Builder checks
    historically omitted ``frame`` but did record ``ref``; applying such a check to every
    frame of a multi-frame layer would manufacture evidence for the wrong beat.
    """
    root = Path(shot_folder)
    image = Path(render)
    if not image.is_absolute():
        image = root / image
    if not image.is_file():
        return []
    planner_rows, runtime_rows = [], []
    if selected_authority is None:
        planner_spec = selected_artifact_path(root, "checks.json")
    elif selected_authority.plan is None:
        planner_spec = root / "checks.json"
    else:
        try:
            planner_spec = selected_authority.artifact_paths["checks.json"]
        except KeyError as exc:
            raise ValueError("selected layer-evidence authority omits checks.json") from exc
    try:
        load(planner_spec)  # validates lifecycle and required focus metadata
        planner_rows = [
            row
            for row in load_document(planner_spec, "checks")
            if isinstance(row, dict) and active_for(row, layer_id, frame)
        ]
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return [
            {
                "id": "image-contract-document",
                "axis": "",
                "metric": "schema",
                "value": None,
                "target": "schema=2",
                "pass": False,
                "origin": "planner",
                "source": "image_contract",
                "authoritative": True,
                "error": str(exc)[:160],
            }
        ]
    runtime_rows = [
        row
        for row in valid_runtime_image_payment_rows(root)
        if str(row.get("layer", "")) == str(layer_id)
    ]
    rows = [*planner_rows, *runtime_rows]

    out = []
    for row in rows:
        if not _matches_image_moment(row, frame=frame, ref=ref):
            continue
        check = Check.from_dict(row)
        if check.stage not in ("any", stage):
            continue
        try:
            value = evaluate(check, image)
            passed = check.holds(value)
            error = ""
        except Exception as exc:
            value, passed = None, False
            error = str(exc)[:160]
        origin = str(row.get("origin") or "planner")
        proof = row.get("proof") or {}
        out.append(
            {
                "id": check.id,
                "axis": check.axis,
                "metric": check.metric,
                "value": round(value, 4) if isinstance(value, (int, float)) else None,
                "target": check.target(),
                "pass": passed,
                "origin": origin,
                "source": "image_contract",
                # Planner checks earn authority by naming and rejecting a known-bad image.
                # Builder checks remain useful evidence, but do not silently become an
                # independent acceptance oracle by marking their own homework.
                "authoritative": bool(origin != "builder" and check.rejects and proof.get("adversary")),
                "owner_layer": check.owner_layer,
                "fault_owner": check.fault_owner,
                "activates_at": check.activates_at,
                "lifecycle": check.lifecycle,
                **({"error": error} if error else {}),
            }
        )
    return out


def acceptance_evidence(
    shot_folder: str | Path,
    *,
    frame: int,
    ref: str,
    render: str | Path,
    selected_authority: ResolvedSelectedAuthority | None = None,
) -> list[dict]:
    """Evaluate finished-chain image contracts regardless of their build lifecycle.

    Layer lifecycle controls when a builder may use a check. Acceptance is a distinct
    finished-chain boundary, so every post-grade/any contract matching the selected
    milestone by explicit frame, or otherwise by exact reference, is eligible and
    retains the same planner-authored authority rules.
    """
    root = Path(shot_folder)
    image = Path(render)
    if not image.is_absolute():
        image = root / image
    if not image.is_file():
        return []
    if selected_authority is None:
        checks_path = selected_artifact_path(root, "checks.json")
    else:
        try:
            checks_path = selected_authority.artifact_paths["checks.json"]
        except KeyError as exc:
            raise ValueError("selected acceptance authority omits checks.json") from exc
    rows = load_document(checks_path, "checks")
    out = []
    for row in rows:
        if not _matches_image_moment(row, frame=frame, ref=ref):
            continue
        check = Check.from_dict(row)
        if check.stage not in {"post_grade", "any"}:
            continue
        try:
            value = evaluate(check, image)
            passed = check.holds(value)
            error = ""
        except Exception as exc:
            value, passed = None, False
            error = str(exc)[:160]
        proof = row.get("proof") or {}
        out.append({
            "id": check.id,
            "axis": check.axis,
            "metric": check.metric,
            "value": round(value, 4) if isinstance(value, (int, float)) else None,
            "target": check.target(),
            "pass": passed,
            "origin": "planner",
            "source": "image_contract",
            "authoritative": bool(check.rejects and proof.get("adversary")),
            "owner_layer": check.owner_layer,
            "error": error,
        })
    return out


def verify_necessity(check: Check, after: Path, before: Path | None) -> Verdict:
    """Did THIS LAYER do its work? Pass on the layer's render, fail on the state before it.

    The five rules above ask whether a check is sound. This asks whether it is NECESSARY,
    and it is the question a layer actually needs answered: a check that passes both before
    and after the layer ran proves nothing about the layer.

    It exists because checks were authored by the stage with the least information. The
    planner writes every check BEFORE any work exists, from reference images alone — which
    is why all 15 metrics compare pixels to a plate, why 42 of 52 checks are post_grade, and
    why the layout layer got exactly one check that cannot even run at its own stage. That
    is not laziness: a planner cannot write a check about a scene that does not exist.

    The builder can. Layer 1 re-derived that the plan's pitches put the hero's roof at NDC
    0.89 and that the roll ladder was mirrored against f191/f205 — real validation, found by
    the only stage in a position to find it, with nowhere to be recorded. When the layer
    ended it evaporated, and the next layer could break it unnoticed.

    Letting the doer write its own test is normally marking your own homework. It is safe
    here because the adversary is not chosen: it is the previous layer's render, and no
    check can be gamed into failing a picture that already exists. `before=None` means the
    first layer, whose adversary is the empty scene.
    """
    v = Verdict(check.id, True)
    try:
        v.ref_value = evaluate(check, after)
    except Exception as e:
        return Verdict(check.id, False, reasons=[f"cannot be evaluated on the render: {e}"])
    if not check.holds(v.ref_value):
        v.ok = False
        v.reasons.append(
            f"DOES NOT HOLD — this layer's own render reads {v.ref_value:.4g} against "
            f"{check.target()}. The check does not describe what was built"
        )
    try:
        v.floor = noise_floor(check, after)
    except Exception:
        v.floor = 0.0
    # A generated threshold must have room for replay/resampling movement. Merely
    # choosing lo just below today's scalar makes one current plate pass but is not a
    # durable decision boundary (the motivating f150 row cleared lo=48 by only 1.28).
    decision_margin = max(2 * v.floor, 0.05 * max(abs(float(v.ref_value)), 1.0))
    prior = None
    if before is not None and Path(before).is_file():
        with contextlib.suppress(Exception):
            prior = evaluate(check, before)
    if check.op == ">=":
        clearance = float(v.ref_value) - float(check.lo)
    elif check.op == "<=":
        clearance = float(check.hi) - float(v.ref_value)
    else:
        clearance = min(
            float(v.ref_value) - float(check.lo),
            float(check.hi) - float(v.ref_value),
        )
    if clearance < decision_margin:
        v.ok = False
        v.reasons.append(
            f"FRAGILE THRESHOLD — candidate clearance {clearance:.4g} is below the "
            f"measured decision margin {decision_margin:.4g} (max of 2× resampling "
            "noise and 5% of the measured value). Do not shave a one-shot threshold "
            "against the current render; choose a stable property or build more margin"
            + _threshold_window(check, float(v.ref_value), decision_margin, prior, v.floor)
        )
    if prior is not None:
        v.bad_values = [prior]
        if check.holds(prior):
            v.ok = False
            v.reasons.append(
                f"NOT NECESSARY — the state BEFORE this layer ran already reads "
                f"{prior:.4g} and passes. A check that holds both before and after "
                f"proves nothing about this layer; it belongs to an earlier one"
                + _threshold_window(check, float(v.ref_value), decision_margin, prior, v.floor)
            )
        gap = abs(float(v.ref_value) - float(prior))
        if gap < decision_margin:
            v.ok = False
            v.reasons.append(
                f"INDISCRIMINATE — candidate/adversary separation {gap:.4g} is below "
                f"the decision margin {decision_margin:.4g}; this check is too close "
                "to survive deterministic replay"
            )
    return v


@dataclass(frozen=True, slots=True)
class PreparedLayerRevalidation:
    """Fully evaluated runtime-check replacement awaiting a short CAS commit."""

    shot_folder: Path
    layer_id: str
    source_sha256: str | None
    source_identity: _RevalidationFileIdentity | None
    parent_identity: _RevalidationFileIdentity
    replacement_path: Path | None
    replacement_identity: _RevalidationFileIdentity | None
    result: dict


@dataclass(frozen=True, slots=True)
class _RevalidationFileIdentity:
    device: int
    inode: int
    mode: int
    size: int
    modified_ns: int
    changed_ns: int


def _revalidation_identity(observed: os.stat_result) -> _RevalidationFileIdentity:
    return _RevalidationFileIdentity(
        device=observed.st_dev,
        inode=observed.st_ino,
        mode=observed.st_mode,
        size=observed.st_size,
        modified_ns=observed.st_mtime_ns,
        changed_ns=observed.st_ctime_ns,
    )


def _same_revalidation_directory(
    observed: os.stat_result,
    expected: _RevalidationFileIdentity,
) -> bool:
    return (
        stat.S_ISDIR(observed.st_mode)
        and observed.st_dev == expected.device
        and observed.st_ino == expected.inode
    )


def _read_revalidation_source(
    path: Path,
) -> tuple[bytes | None, _RevalidationFileIdentity | None]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        raise ValueError("runtime image checks must be a real regular file") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("runtime image checks must be a real regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            source = handle.read()
        after = os.fstat(descriptor)
        identity = _revalidation_identity(after)
        if _revalidation_identity(before) != identity:
            raise ValueError("runtime image checks changed while revalidation read them")
        try:
            current = path.lstat()
        except OSError as exc:
            raise ValueError(
                "runtime image checks changed while revalidation read them"
            ) from exc
        if _revalidation_identity(current) != identity:
            raise ValueError("runtime image checks changed while revalidation read them")
        return source, identity
    finally:
        os.close(descriptor)


def _stage_revalidation_replacement(
    path: Path,
    replacement: bytes | None,
) -> tuple[Path | None, _RevalidationFileIdentity | None]:
    if replacement is None:
        return None, None
    descriptor, raw_path = tempfile.mkstemp(
        prefix=f".{path.name}.revalidation.",
        dir=path.parent,
    )
    staged = Path(raw_path)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(replacement)
            handle.flush()
            os.fsync(handle.fileno())
        identity = _revalidation_identity(os.fstat(descriptor))
        return staged, identity
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            staged.unlink()
        raise
    finally:
        os.close(descriptor)


def prepare_layer_revalidation(
    shot_folder: Path,
    layer_id: str,
    render_for: Callable[[Check], Path | None],
    *,
    selected_authority: ResolvedSelectedAuthority | None = None,
) -> PreparedLayerRevalidation:
    """Re-run this layer's BUILDER checks against the renders that actually shipped, and
    drop the ones that no longer hold.

    A builder check is authored mid-layer against the render in front of it. A later
    attempt rebuilds the scene and replaces every render, so a check proven in attempt 2
    can describe a picture that no longer exists by attempt 3 — measured: three L1 checks
    proven at 0.354 / 2.052 / 0.675 read 1.532 / 1.000 / 8.107 against the final renders.

    Nothing distinguished that from a wrong check, which is why `proof.on` now records the
    render each was proven against. But recording it only makes staleness VISIBLE; this
    makes it impossible to ship, by re-verifying at the one moment the renders are final.
    A stale check is removed rather than repaired: it was true of a scene this layer no
    longer builds, and inventing a new threshold for it here would be authoring a check
    nobody ran.
    """
    if selected_authority is not None and selected_authority.plan is None:
        raise ValueError("image-contract revalidation requires selected plan authority")
    shot_folder = Path(shot_folder)
    spec = shot_folder / "runtime_checks.json"
    parent_identity = _revalidation_identity(spec.parent.lstat())
    if not stat.S_ISDIR(parent_identity.mode):
        raise ValueError("runtime image-check parent must be a real directory")
    source, source_identity = _read_revalidation_source(spec)
    if source is None:
        return PreparedLayerRevalidation(
            shot_folder=shot_folder,
            layer_id=str(layer_id),
            source_sha256=None,
            source_identity=None,
            parent_identity=parent_identity,
            replacement_path=None,
            replacement_identity=None,
            result={"kept": 0, "dropped": []},
        )
    source_sha256 = hashlib.sha256(source).hexdigest()
    rows = json.loads(source)
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("runtime image checks must be a list of objects")
    keep: list[dict] = []
    # One entry per CONTRACT ID, not per row. A multi-frame image debt has one runtime
    # row per frame, each bound to its own candidate handle, so one id can be dropped
    # more than once -- and the drop record's whole shape is {"id", "reason"} with no
    # frame, so per-row appends produced duplicate ids and the projection validator
    # refused to mint the receipt. hansa_silk_road layer 2 lost a build in which all
    # four units had sealed to exactly that: `hero-facade-appearance-debt` read 1.826
    # against `>= 2` at two frames (HIR-0237).
    drops: dict[str, list[str]] = {}

    def _drop(identifier: object, row: dict, reason: str) -> None:
        """Record why one contract id was dropped, naming the frame it was read at."""
        frame = row.get("frame")
        drops.setdefault(str(identifier or "?"), []).append(
            f"f{frame}: {reason}" if frame is not None else reason
        )

    for d in rows:
        if d.get("origin") != "builder" or str(d.get("layer")) != str(layer_id):
            keep.append(d)
            continue
        provenance_error = runtime_image_payment_error(shot_folder, d)
        if provenance_error:
            _drop(d.get("id"), d, provenance_error)
            continue
        c = Check.from_dict({**d, "lo": d.get("lo", float("-inf")), "hi": d.get("hi", float("inf"))})
        img = render_for(c)
        if img is None:
            _drop(c.id, d, "no shipped render for its frame")
            continue
        try:
            adversary_rel = ((d.get("payment") or {}).get("adversary") or {}).get("path")
            adversary = (
                Path(shot_folder) / str(adversary_rel) if adversary_rel else None
            )
            verdict = verify_necessity(c, Path(img), adversary)
            v = verdict.ref_value
        except Exception as e:
            _drop(c.id, d, f"unevaluable: {str(e)[:60]}")
            continue
        if verdict.ok and isinstance(v, (int, float)):
            d.setdefault("proof", {})["ref"] = round(v, 4)
            d["proof"]["on"] = str(Path(img).relative_to(Path(shot_folder)))
            keep.append(d)
        else:
            reason = (
                verdict.why()
                if verdict.reasons
                else f"reads {v:.4g} against {c.target()} on the final render"
            )
            _drop(c.id, d, reason)
    # Insertion order is the order the rows were read, so the projection stays stable.
    dropped = [(cid, "; ".join(reasons)) for cid, reasons in drops.items()]
    result = {
        "kept": sum(1 for d in keep if d.get("origin") == "builder" and str(d.get("layer")) == str(layer_id)),
        "dropped": dropped,
    }
    replacement = (
        (json.dumps(keep, indent=1) + "\n").encode("utf-8")
        if dropped
        else None
    )
    replacement_path, replacement_identity = _stage_revalidation_replacement(
        spec,
        replacement,
    )
    return PreparedLayerRevalidation(
        shot_folder=shot_folder,
        layer_id=str(layer_id),
        source_sha256=source_sha256,
        source_identity=source_identity,
        parent_identity=parent_identity,
        replacement_path=replacement_path,
        replacement_identity=replacement_identity,
        result=result,
    )


def commit_layer_revalidation(candidate: PreparedLayerRevalidation) -> dict:
    """CAS-publish only the already evaluated runtime-check projection."""

    if not isinstance(candidate, PreparedLayerRevalidation):
        raise ValueError("runtime-check publication requires a prepared revalidation")
    spec = candidate.shot_folder / "runtime_checks.json"
    directory = os.open(
        spec.parent,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        if not _same_revalidation_directory(
            os.fstat(directory),
            candidate.parent_identity,
        ):
            raise ValueError("runtime image-check parent changed before publication")
        try:
            source_stat = os.stat(
                spec.name,
                dir_fd=directory,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            source_identity = None
        else:
            source_identity = _revalidation_identity(source_stat)
        if source_identity != candidate.source_identity:
            raise ValueError(
                "runtime image checks changed after revalidation; evaluate the new bytes"
            )
        if candidate.replacement_path is None:
            return dict(candidate.result)
        if candidate.replacement_identity is None:
            raise ValueError("prepared runtime-check replacement lacks an exact identity")
        if candidate.replacement_path.parent != spec.parent:
            raise ValueError("prepared runtime-check replacement belongs to another parent")
        try:
            replacement_stat = os.stat(
                candidate.replacement_path.name,
                dir_fd=directory,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise ValueError("prepared runtime-check replacement disappeared") from exc
        if _revalidation_identity(replacement_stat) != candidate.replacement_identity:
            raise ValueError("prepared runtime-check replacement changed")
        os.replace(
            candidate.replacement_path.name,
            spec.name,
            src_dir_fd=directory,
            dst_dir_fd=directory,
        )
        current_directory = os.open(
            spec.parent,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            if not _same_revalidation_directory(
                os.fstat(current_directory),
                candidate.parent_identity,
            ):
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(spec.name, dir_fd=directory)
                os.fsync(directory)
                raise ValueError(
                    "runtime image-check parent changed during guarded publication"
                )
        finally:
            os.close(current_directory)
        os.fsync(directory)
        return dict(candidate.result)
    finally:
        os.close(directory)


def discard_layer_revalidation(candidate: PreparedLayerRevalidation) -> None:
    """Remove an uncommitted staged replacement without deleting foreign bytes."""

    if candidate.replacement_path is None or candidate.replacement_identity is None:
        return
    try:
        observed = candidate.replacement_path.lstat()
    except FileNotFoundError:
        return
    if _revalidation_identity(observed) == candidate.replacement_identity:
        candidate.replacement_path.unlink()


def revalidate_layer(
    shot_folder: Path,
    layer_id: str,
    render_for: Callable[[Check], Path | None],
    *,
    selected_authority: ResolvedSelectedAuthority | None = None,
) -> dict:
    """Prepare then publish for legacy non-attempt callers."""

    prepared = prepare_layer_revalidation(
        shot_folder,
        layer_id,
        render_for,
        selected_authority=selected_authority,
    )
    try:
        return commit_layer_revalidation(prepared)
    finally:
        discard_layer_revalidation(prepared)
IMAGE_PAYMENT_SCHEMA = "vfx-harness.image-payment/v2"


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def runtime_image_payment_error(shot_folder: str | Path, row: dict) -> str | None:
    """Return why a builder image-payment row lacks immutable run provenance.

    Runtime rows are durable evidence, so existence at an arbitrary shot path is not
    identity.  Both plates must be immutable render artifacts owned by one structured
    run, hash-pinned, frame/settings aligned, and tied to the same pre-unit parent chain.
    Legacy rows are deliberately rejected rather than guessed into the new authority
    model (ADR-0002 strict migration).
    """
    root = Path(shot_folder).resolve()
    payment = row.get("payment")
    if not isinstance(payment, dict) or payment.get("schema") != IMAGE_PAYMENT_SCHEMA:
        return f"missing payment schema {IMAGE_PAYMENT_SCHEMA}"
    run_id = str(payment.get("run_id") or "")
    if not run_id or run_id in {".", ".."} or "/" in run_id or "\\" in run_id:
        return "payment.run_id is invalid"
    run_root = (root / "runs" / run_id).resolve()
    if not (run_root / "manifest.json").is_file():
        return f"payment run {run_id!r} has no manifest"
    try:
        manifest = json.loads((run_root / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return f"payment run {run_id!r} has an unreadable manifest"
    if str(manifest.get("run_id") or "") != run_id:
        return f"payment run manifest names {manifest.get('run_id')!r}, not {run_id!r}"

    records: dict[str, tuple[dict, Path]] = {}
    for role in ("candidate", "adversary"):
        record = payment.get(role)
        if not isinstance(record, dict):
            return f"payment.{role} is required"
        rel = str(record.get("path") or "")
        if not rel:
            return f"payment.{role}.path is required"
        path = (root / rel).resolve()
        evidence_root = run_root.joinpath("evidence", "renders").resolve()
        if path != evidence_root and evidence_root not in path.parents:
            return f"payment.{role}.path is not owned by run {run_id} evidence/renders"
        if not path.is_file():
            return f"payment.{role}.path does not exist"
        expected = str(record.get("sha256") or "")
        if len(expected) != 64 or _file_sha256(path) != expected:
            return f"payment.{role}.sha256 does not match its artifact"
        try:
            record_frame = int(record.get("frame"))
            row_frame = int(row.get("frame"))
        except (TypeError, ValueError):
            return f"payment.{role}.frame and row.frame must be integers"
        if record_frame != row_frame:
            return f"payment.{role}.frame {record_frame} != row.frame {row_frame}"
        records[role] = (record, path)

    candidate = records["candidate"][0]
    adversary = records["adversary"][0]
    for key in ("mode", "scale", "resolution"):
        if candidate.get(key) != adversary.get(key):
            return f"candidate/adversary {key} settings differ"
    parent_hash = str(payment.get("parent_chain_hash") or "")
    if len(parent_hash) != 64 or any(ch not in "0123456789abcdef" for ch in parent_hash):
        return "payment.parent_chain_hash must be a lowercase SHA-256"
    if str(adversary.get("parent_chain_hash") or "") != parent_hash:
        return "adversary is not bound to payment.parent_chain_hash"
    unit_hash = str(payment.get("unit_hash") or "")
    if len(unit_hash) != 64 or any(ch not in "0123456789abcdef" for ch in unit_hash):
        return "payment.unit_hash must be a lowercase SHA-256"
    if not str(payment.get("unit_id") or ""):
        return "payment.unit_id is required"
    return None


def valid_runtime_image_payment_rows(shot_folder: str | Path) -> list[dict]:
    """Load only provenance-complete builder rows; legacy rows pay no debts."""
    root = Path(shot_folder)
    runtime = root / "runtime_checks.json"
    if not runtime.is_file():
        return []
    try:
        loaded = json.loads(runtime.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(loaded, list):
        return []
    return [
        row
        for row in loaded
        if isinstance(row, dict)
        and row.get("origin") == "builder"
        and row.get("id")
        and runtime_image_payment_error(root, row) is None
    ]
