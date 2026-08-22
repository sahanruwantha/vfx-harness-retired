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

from vfx_harness.evidence.metrics import (
    _FLOOR,
    _HOT_FLOOR_PPM,
    METRIC_SET,
    canonical_fingerprint,
)

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
    values = canonical_fingerprint(str(ref))["values"]
    out: dict[str, float | None] = {
        "mean": values["exposure_mean"],
        "clipped_pct": values["clipped_pct"],
        "black_pct": values["black_pct"],
    }
    for band in ("top", "mid", "bot"):
        out[f"{band}_mu"] = values[f"band_mean_{band}"]
        out[f"{band}_sigma"] = values[f"structure_{band}"]
    out["halation"] = values.get("halation")
    out["_hot_px"] = round(values.get("hot_core", 0.0) * 960 * 540 / 4e6)
    out["_hot_core"] = values.get("hot_core", 0.0)
    out["detail"] = values["detail"]
    out["points"] = values["points"]
    return out


def check_structured_fingerprint(fingerprint: dict, ref: Path) -> tuple[list[dict], list[str]]:
    """Validate typed metric ids directly; no prose or regex can change their meaning."""
    if fingerprint.get("metric_set") != METRIC_SET:
        return [], [f"unsupported metric_set {fingerprint.get('metric_set')!r}; expected {METRIC_SET}"]
    claimed = fingerprint.get("values")
    if not isinstance(claimed, dict) or not claimed:
        return [], ["structured fingerprint requires a non-empty values object"]
    truth = canonical_fingerprint(str(ref))["values"]
    rows, errors = [], []
    for key, value in claimed.items():
        if key not in truth:
            if key == "halation":
                rows.append({
                    "key": key,
                    "claimed": float(value),
                    "actual": None,
                    "verdict": "UNMEASURABLE",
                    "why": "the canonical metric registry omitted halation because hot-core density is below its floor",
                })
            else:
                errors.append(f"unknown or unavailable metric id {key!r}")
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            errors.append(f"metric {key!r} must be numeric")
            continue
        actual = float(truth[key])
        floor = _FLOOR.get(key, 0.5)
        gap = abs(float(value) - actual)
        rel = gap / max(abs(actual), floor)
        rows.append({
            "key": key,
            "claimed": float(value),
            "actual": round(actual, 3),
            "rel": round(rel, 3),
            "verdict": "ok" if gap < floor or rel <= TOL else "MISMATCH",
        })
    return rows, errors


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
    from vfx_harness.orchestration.plan_authority import selected_artifact_path

    acc = selected_artifact_path(folder, "acceptance.json")
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
        schema_errors: list[str] = []
        if isinstance(fp, dict):
            rows, schema_errors = check_structured_fingerprint(fp, ref)
            unparsed = []
        elif isinstance(fp, str):
            rows, unparsed = check_fingerprint(fp, truth)
        else:
            moments.append(
                {
                    "id": m.get("id"),
                    "ref": m.get("ref"),
                    "error": "fingerprint must be an object or legacy string",
                }
            )
            continue
        moments.append({"id": m.get("id"), "ref": m.get("ref"), "claims": rows,
                        "unparsed": unparsed, "schema_errors": schema_errors,
                        "hot_core": truth["_hot_core"]})
    flat = [r for mo in moments for r in mo.get("claims", [])]
    return {
        "shot": folder.name,
        "moments": moments,
        "n_claims": len(flat),
        "n_ok": sum(1 for r in flat if r["verdict"] == "ok"),
        "n_mismatch": sum(1 for r in flat if r["verdict"] == "MISMATCH"),
        "n_unmeasurable": sum(1 for r in flat if r["verdict"] == "UNMEASURABLE"),
        "n_error": sum(1 for mo in moments if "error" in mo) + sum(
            len(mo.get("schema_errors", [])) for mo in moments
        ),
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
        bad_schema = mo.get("schema_errors", [])
        head = "✓" if not bad and not bad_schema else "✗"
        out.append(f"\n   {head} {mo['id']}  {mo['ref']}  "
                   f"({len(mo['claims']) - len(bad)}/{len(mo['claims'])} reproduce)")
        for r in bad:
            if r["verdict"] == "UNMEASURABLE":
                out.append(f"       {r['key']:<12} claims {r['claimed']:<9g} "
                           f"UNMEASURABLE — {r['why']}")
            else:
                out.append(f"       {r['key']:<12} claims {r['claimed']:<9g} "
                           f"plate reads {r['actual']:<9g} ({r['rel'] * 100:.0f}% off)")
        for error in bad_schema:
            out.append(f"       schema       {error}")
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
