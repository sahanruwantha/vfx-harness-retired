"""Tier 2 — is the plan GROUNDED in the artifacts it claims to describe?

The plan is the only stage whose output nothing checks. `layers.json` gets a structural
read in `ledger.load_layers`; `acceptance.json` gets none at all. Its `fingerprint` fields
are the numeric targets every later stage aims at, and they are free prose written by a
model that was *asked* to measure but never *made* to:

    measure_ref: "Use these MEASURED numbers as the plan's look targets — never invent
                  fingerprint values."

Nothing enforced that sentence. Enforcing it costs one loop over the references and no
model call, because every number in a fingerprint is by construction re-derivable from the
still it describes — that is what makes it a fingerprint rather than an opinion.

Three verdicts, and the third is the one worth having:

    ok            the claim reproduces from the reference, within tolerance
    MISMATCH      the claim does not reproduce — the target is wrong, and every layer
                  aiming at it will burn attempts converging on a number that was never
                  in the picture
    UNMEASURABLE  the metric cannot be derived from this still AT ALL, so the claim was
                  not measured whatever its value. A plan cannot be judged against it and
                  neither can a render.

That last verdict is why this exists rather than a plain diff. barrel_roll M2 claimed
"halation 2340.1 (the shot's peak)" and M1 claimed "halation 0.0 (no point sources in
frame — do not manufacture a halo here)". Neither is a measurement: both references carry
hot core far under the density floor, so the ratio has no denominator (see
metrics._HOT_FLOOR_PPM). One of the two
was then rationalised into a build instruction. A checker that only compared numbers would
have called M1 a perfect match.

An unparsed token is REPORTED, never skipped. A checker that silently ignores what it does
not understand reports "all grounded" while checking nothing — the same failure this
module exists to catch.

Exit codes: 0 every claim grounded · 3 at least one MISMATCH or UNMEASURABLE
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from vfx_harness.evidence.metrics import _HOT_FLOOR_PPM, look_vector

from ..blender.tools import _load, _region_metrics

# token -> (pattern, absolute floor below which a relative gap is meaningless)
# The patterns accept both the glyphs _metrics_line emits (μ, σ) and the ASCII the planner
# transliterated them into when it composed the fingerprint prose.
_CLAIMS: dict[str, tuple[str, float]] = {
    "mean":        (r"\bmean\s+([\d.]+)", 3.0),
    "clipped_pct": (r"\bclipped(?:\(blown\))?\s+([\d.]+)\s*%", 1.5),
    "black_pct":   (r"\bblack\s+([\d.]+)\s*%", 1.5),
    "top_mu":      (r"\btop\s+(?:μ|mu)([\d.]+)", 3.0),
    "top_sigma":   (r"\btop\s+(?:μ|mu)[\d.]+\s*/\s*(?:σ|sigma)([\d.]+)", 3.0),
    "mid_mu":      (r"\bmid\s+(?:μ|mu)([\d.]+)", 3.0),
    "mid_sigma":   (r"\bmid\s+(?:μ|mu)[\d.]+\s*/\s*(?:σ|sigma)([\d.]+)", 3.0),
    "bot_mu":      (r"\bbot\s+(?:μ|mu)([\d.]+)", 3.0),
    "bot_sigma":   (r"\bbot\s+(?:μ|mu)[\d.]+\s*/\s*(?:σ|sigma)([\d.]+)", 3.0),
    "halation":    (r"\bhalation\s+([\d.]+)", 0.5),
    "detail":      (r"\bdetail\s+([\d.]+)", 0.5),
    "points":      (r"\blight\s+points\s+([\d.]+)", 200.0),
}

# Anything numeric the table above does not claim. Used only to report coverage honestly:
# a fingerprint that is 90% unrecognised must not read as 100% grounded.
_NUMBER = re.compile(r"(?<![\w.])\d+(?:\.\d+)?(?![\w.])")

TOL = 0.15      # a fingerprint is a rounded readout, not a checksum


def measure(ref: Path) -> dict[str, float | None]:
    """Re-derive every claimable number from the still, through the SAME functions the
    planner's `measure_ref` tool ran. Reimplementing the measurement here would test this
    file against itself; calling the producer tests the plan against what it was shown."""
    im = _load(str(ref))                       # _MAX_W resize, exactly as measure_ref saw it
    px = list(im.convert("L").getdata())
    n = len(px) or 1
    out: dict[str, float | None] = {
        "mean": sum(px) / n,
        "clipped_pct": 100 * sum(1 for p in px if p >= 250) / n,
        "black_pct": 100 * sum(1 for p in px if p <= 4) / n,
    }
    rm = _region_metrics(im)
    for band in ("top", "mid", "bot"):
        out[f"{band}_mu"], out[f"{band}_sigma"] = rm["bands"][band]
    out["halation"] = rm["halation"]           # None when there is no core to divide by
    out["_hot_px"] = rm["hot_px"]
    out["_hot_core"] = rm["hot_core"]
    v = look_vector(str(ref))
    out["detail"] = v["detail"]
    out["points"] = v["points"]
    return out


def check_fingerprint(text: str, truth: dict) -> tuple[list[dict], list[str]]:
    """One row per number the fingerprint states, plus the numbers no rule claimed."""
    rows: list[dict] = []
    masked = list(text)
    for key, (pat, floor) in _CLAIMS.items():
        m = re.search(pat, text, re.IGNORECASE)
        if not m:
            continue
        # Blank the span this rule consumed, so the leftover scan below reports what is
        # genuinely unchecked rather than re-finding numbers by value.
        for i in range(*m.span()):
            masked[i] = " "
        claimed = float(m.group(1))
        actual = truth.get(key)
        if actual is None:
            rows.append({"key": key, "claimed": claimed, "actual": None,
                         "verdict": "UNMEASURABLE",
                         "why": f"the plate carries {truth.get('_hot_core', 0)} ppm of hot "
                                f"core against a {_HOT_FLOOR_PPM} ppm floor, so this ratio "
                                f"has no denominator to be read off"})
            continue
        gap = abs(claimed - actual)
        rel = gap / max(abs(actual), floor)
        rows.append({"key": key, "claimed": claimed, "actual": round(actual, 2),
                     "rel": round(rel, 3),
                     "verdict": "ok" if gap < floor or rel <= TOL else "MISMATCH"})
    # Numbers present in the prose that no pattern claimed. Reported so the coverage
    # figure means something; not a failure, because fingerprints carry frame numbers and
    # pixel sizes that are not metrics.
    return rows, _NUMBER.findall("".join(masked))


def audit(folder: Path) -> dict:
    acc = folder / "acceptance.json"
    if not acc.is_file():
        return {"shot": folder.name, "error": f"no acceptance.json in {folder}"}
    moments = []
    for m in json.loads(acc.read_text()):
        fp = m.get("fingerprint") or ""
        ref = folder / m.get("ref", "")
        if not fp:
            moments.append({"id": m.get("id"), "ref": m.get("ref"),
                            "error": "no fingerprint — nothing to check"})
            continue
        if not ref.is_file():
            moments.append({"id": m.get("id"), "ref": m.get("ref"),
                            "error": "reference does not exist — the target describes "
                                     "a plate no stage can compare against"})
            continue
        truth = measure(ref)
        rows, unparsed = check_fingerprint(fp, truth)
        moments.append({"id": m.get("id"), "ref": m.get("ref"), "claims": rows,
                        "unparsed": unparsed, "hot_core": truth["_hot_core"]})
    flat = [r for mo in moments for r in mo.get("claims", [])]
    return {
        "shot": folder.name,
        "moments": moments,
        "n_claims": len(flat),
        "n_ok": sum(1 for r in flat if r["verdict"] == "ok"),
        "n_mismatch": sum(1 for r in flat if r["verdict"] == "MISMATCH"),
        "n_unmeasurable": sum(1 for r in flat if r["verdict"] == "UNMEASURABLE"),
        "n_error": sum(1 for mo in moments if "error" in mo),
    }


def report(rec: dict) -> str:
    if "error" in rec:
        return f"── grounding · {rec['shot']} ──\n   ✗ {rec['error']}"
    out = [f"── grounding · {rec['shot']} · every stated number vs the plate it describes ──"]
    for mo in rec["moments"]:
        if "error" in mo:
            out.append(f"\n   {mo['id']}  {mo['ref']}\n     ✗ {mo['error']}")
            continue
        bad = [r for r in mo["claims"] if r["verdict"] != "ok"]
        head = "✓" if not bad else "✗"
        out.append(f"\n   {head} {mo['id']}  {mo['ref']}  "
                   f"({len(mo['claims']) - len(bad)}/{len(mo['claims'])} reproduce)")
        for r in bad:
            if r["verdict"] == "UNMEASURABLE":
                out.append(f"       {r['key']:<12} claims {r['claimed']:<9g} "
                           f"UNMEASURABLE — {r['why']}")
            else:
                out.append(f"       {r['key']:<12} claims {r['claimed']:<9g} "
                           f"plate reads {r['actual']:<9g} ({r['rel'] * 100:.0f}% off)")
        if mo["unparsed"]:
            out.append(f"       · {len(mo['unparsed'])} number(s) not checked by any rule: "
                       f"{', '.join(mo['unparsed'][:6])}")
    out += ["",
            f"   {rec['n_ok']}/{rec['n_claims']} claims reproduce from their reference"
            f" · {rec['n_mismatch']} mismatch · {rec['n_unmeasurable']} unmeasurable"]
    if rec["n_unmeasurable"]:
        out.append("   an UNMEASURABLE target cannot be hit or missed. Remove it from the "
                   "fingerprint rather than\n   restating it — a layer aiming at one "
                   "converges on nothing and spends its whole attempt budget doing it.")
    return "\n".join(out)
