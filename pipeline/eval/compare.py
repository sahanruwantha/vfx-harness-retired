"""Diff two baselines, and refuse to make the comparison look more conclusive than it is.

The review is explicit about the decision rule: *"The primary decision statistic should
be final task success with a non-inferiority bound — not mean tokens, layer pass rate, or
critic score."* All three of the rejected statistics are gameable by the change being
tested, and each has already misled here:

  mean tokens      a change that truncates the builder earlier "saves tokens" and ships
                   a worse shot.
  layer pass rate  layers are judged on their own scoped axes; the review's own risk list
                   records that local pass rates are weak predictors of final success.
  critic score     the same render on the same axis scored 4.0, 3.0, 3.0, 2.0. A 0.3
                   "improvement" in a mean is inside that.

So the report is ordered: final success first and alone at the top, everything else
below a line that says what it is. And where a difference cannot be distinguished from
noise, the report says so instead of printing a delta that reads like a result.

The statistics are deliberately the simple exact ones. With a handful of paired
acceptance moments per arm, a sign test on the discordant pairs is both the right tool
and the one whose assumptions can be stated in a sentence.
"""

from __future__ import annotations

from math import comb

ALPHA = 0.05


def sign_test_p(b: int, c: int) -> float:
    """Two-sided exact binomial p for `b` flips one way and `c` the other, under the null
    that a flip is a coin toss (McNemar's exact test).

    Exact rather than the chi-square approximation because the counts here are single
    digits, which is precisely where the approximation is wrong in the optimistic
    direction.
    """
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(comb(n, i) for i in range(k + 1)) / (2 ** n)
    return min(1.0, 2 * tail)


def min_discordant_for_significance(alpha: float = ALPHA) -> int:
    """Smallest number of all-in-one-direction flips that could ever reach `alpha`.

    Answering "how big would the effect have to be before this design could see it"
    up front is the difference between an underpowered eval and a misleading one.
    """
    d = 1
    while d < 64:
        if sign_test_p(d, 0) <= alpha:
            return d
        d += 1
    return d


def _final_block(a: dict, b: dict, margin: float) -> list[str]:
    fa, fb = a["final"], b["final"]
    out = ["═══ PRIMARY: FINAL TASK SUCCESS ═══"]
    if not (fa.get("present") and fb.get("present")):
        out += [
            "  UNAVAILABLE — the primary decision statistic does not exist for "
            + ("A" if not fa.get("present") else "")
            + (" and " if not fa.get("present") and not fb.get("present") else "")
            + ("B" if not fb.get("present") else "") + ".",
            f"    A: {fa.get('why', str(fa.get('passed')) + '/' + str(fa.get('total')))}",
            f"    B: {fb.get('why', str(fb.get('passed')) + '/' + str(fb.get('total')))}",
            "  Nothing below this line can decide whether the change helped. Layer",
            "  verdicts, critic means and token counts are all gameable by the change",
            "  being tested; that is why they are not the decision statistic. Run the",
            "  accept stage on a finished chain in BOTH arms before concluding anything.",
        ]
        return out

    ma, mb = fa["moments"], fb["moments"]
    shared = sorted(set(ma) & set(mb))
    only_a, only_b = sorted(set(ma) - set(mb)), sorted(set(mb) - set(ma))
    out.append(f"  A  {fa['passed']}/{fa['total']} moments passed"
               f"   ({a.get('label') or a['at']})")
    out.append(f"  B  {fb['passed']}/{fb['total']} moments passed"
               f"   ({b.get('label') or b['at']})")
    if only_a or only_b:
        # A changed moment set means the two arms were not asked the same question.
        out.append(f"  ⚠ the acceptance suites DIFFER (only in A: {only_a or '—'}; "
                   f"only in B: {only_b or '—'}) — this is not a paired comparison for "
                   f"those moments, and the totals above are not comparable.")
    if not shared:
        out.append("  No moments in common. Nothing to test.")
        return out

    b_flips = [m for m in shared if ma[m]["pass"] and not mb[m]["pass"]]   # A pass → B fail
    c_flips = [m for m in shared if not ma[m]["pass"] and mb[m]["pass"]]   # A fail → B pass
    p = sign_test_p(len(b_flips), len(c_flips))
    delta = (len(c_flips) - len(b_flips)) / len(shared)
    need = min_discordant_for_significance()

    out.append("")
    out.append(f"  paired on {len(shared)} shared moment(s):")
    out.append(f"    regressed (A pass → B fail):  {len(b_flips)}"
               + (f"  {b_flips}" if b_flips else ""))
    out.append(f"    improved   (A fail → B pass):  {len(c_flips)}"
               + (f"  {c_flips}" if c_flips else ""))
    out.append(f"    observed difference: {delta:+.0%} of moments "
               f"· exact sign-test p = {p:.3f}")
    out.append("")
    if not b_flips and not c_flips:
        out.append("  VERDICT: IDENTICAL on the primary statistic. Every shared moment "
                   "landed the same way.")
        out.append(f"  This does NOT establish non-inferiority within {margin:.0%}: with "
                   f"{len(shared)} moments and one run per arm, a real regression smaller "
                   f"than {1 / len(shared):.0%} cannot appear at all.")
    elif p > ALPHA:
        out.append(f"  VERDICT: WITHIN NOISE. {len(b_flips) + len(c_flips)} moment(s) "
                   f"flipped, p = {p:.3f} > {ALPHA}. This design cannot distinguish that "
                   f"from chance, so the {delta:+.0%} above is NOT a result — do not "
                   f"quote it as one.")
        out.append(f"  For any difference to reach p ≤ {ALPHA} at all, at least {need} "
                   f"moments would have to flip in the SAME direction "
                   f"(out of {len(shared)} shared).")
    else:
        direction = "IMPROVED" if len(c_flips) > len(b_flips) else "REGRESSED"
        out.append(f"  VERDICT: B {direction} (p = {p:.3f}).")
        out.append("  Caveat that must travel with this: one run per arm, and the judge "
                   "deciding each moment is the same noisy critic measured elsewhere in "
                   "this harness. A flip is evidence about the JUDGED outcome, which is "
                   "the outcome we ship, but blind human labels are what the review asks "
                   "for before a model/cost change is accepted.")
    out.append("")
    out.append(f"  Non-inferiority margin requested: {margin:.0%}. With {len(shared)} "
               f"paired moments the resolution of this design is {1 / len(shared):.0%} "
               f"per moment, so a margin below that is not testable here regardless of "
               f"the result above.")

    # A moment can pass on the critic and fail on a blocking metric (or vice versa).
    # Which mechanism decided it is the difference between "the look changed" and "the
    # threshold caught something new", and they warrant different responses.
    switched = [m for m in shared
                if ma[m].get("decided_by") != mb[m].get("decided_by")]
    if switched:
        out.append("")
        out.append("  moments whose DECIDING MECHANISM changed (metric vs critic):")
        for m in switched:
            out.append(f"    {m}: {ma[m].get('decided_by')} → {mb[m].get('decided_by')}")
    return out


def _layer_block(a: dict, b: dict, noise: float | None) -> list[str]:
    out = ["─── secondary: layer verdicts (GAMEABLE — not a decision statistic) ───"]
    shared = sorted(set(a["layers"]) & set(b["layers"]), key=lambda s: (len(s), s))
    only_a = sorted(set(a["layers"]) - set(b["layers"]))
    only_b = sorted(set(b["layers"]) - set(a["layers"]))
    if only_a or only_b:
        out.append(f"  layer sets differ — only in A: {only_a or '—'}; "
                   f"only in B: {only_b or '—'}")
    changed = []
    for lid in shared:
        la, lb = a["layers"][lid], b["layers"][lid]
        if la["status"] != lb["status"]:
            changed.append(f"  {lid:<4} {la['status']} → {lb['status']}")
    if changed:
        out.append("  verdict changes:")
        out += changed
    else:
        out.append("  no layer changed verdict")
    means = []
    for lid in shared:
        ma = (a["layers"][lid].get("best") or {}).get("mean")
        mb = (b["layers"][lid].get("best") or {}).get("mean")
        if isinstance(ma, (int, float)) and isinstance(mb, (int, float)) and ma != mb:
            d = mb - ma
            tag = ""
            if noise is not None:
                tag = ("  (inside measured judge noise ±"
                       f"{noise:g} — not a signal)" if abs(d) <= noise
                       else f"  (exceeds measured judge noise ±{noise:g})")
            means.append(f"  {lid:<4} best mean {ma} → {mb}  ({d:+.2f}){tag}")
    if means:
        out.append("  best-mean movement:")
        out += means
        if noise is None:
            out.append("  ⚠ no judge-variance measurement exists for this shot, so "
                       "there is no way to say which of these movements are noise. "
                       "Run: python -m pipeline.evals variance <shot> --n 6")
    return out


def _telemetry_block(a: dict, b: dict) -> list[str]:
    out = ["─── telemetry only: cost / tokens / turns (NEVER a quality argument) ───"]
    shared = sorted(set(a["layers"]) & set(b["layers"]), key=lambda s: (len(s), s))
    rows, tot = [], {"cost": [0.0, 0.0], "turns": [0, 0], "out_tok": [0, 0], "cache": [0, 0]}
    for lid in shared:
        ta = a["layers"][lid]["telemetry"]
        tb = b["layers"][lid]["telemetry"]
        if not (ta.get("present") and tb.get("present")):
            rows.append(f"  {lid:<4} no run log in "
                        + ("A" if not ta.get("present") else "B"))
            continue
        ca, cb = ta.get("cost_usd") or 0.0, tb.get("cost_usd") or 0.0
        na, nb = ta.get("turns") or 0, tb.get("turns") or 0
        oa = (ta.get("tokens") or {}).get("output_tokens", 0)
        ob = (tb.get("tokens") or {}).get("output_tokens", 0)
        tot["cost"][0] += ca; tot["cost"][1] += cb
        tot["turns"][0] += na; tot["turns"][1] += nb
        tot["out_tok"][0] += oa; tot["out_tok"][1] += ob
        rows.append(f"  {lid:<4} ${ca:>7.2f} → ${cb:>7.2f} ({cb - ca:+.2f})  ·  "
                    f"{na:>4} → {nb:>4} turns  ·  out {oa:>7,} → {ob:>7,}")
    out += rows or ["  (no per-layer run logs in either baseline)"]
    out.append(f"  TOTAL ${tot['cost'][0]:.2f} → ${tot['cost'][1]:.2f} "
               f"({tot['cost'][1] - tot['cost'][0]:+.2f}) · "
               f"{tot['turns'][0]} → {tot['turns'][1]} turns")
    out.append("  A cost delta justifies a change ONLY once final task success has "
               "already cleared its non-inferiority bound above.")
    return out


def _artifact_block(a: dict, b: dict) -> list[str]:
    out = ["─── artifacts: what actually changed on disk ───"]
    ra, rb = a["renders"], b["renders"]
    same = [k for k in set(ra) & set(rb) if ra[k] == rb[k]]
    diff = sorted(k for k in set(ra) & set(rb) if ra[k] != rb[k])
    out.append(f"  renders: {len(same)} byte-identical, {len(diff)} changed, "
               f"{len(set(rb) - set(ra))} new, {len(set(ra) - set(rb))} gone")
    for k in diff[:12]:
        out.append(f"    changed  {k}")
    if len(diff) > 12:
        out.append(f"    … and {len(diff) - 12} more (full list is in the baselines)")
    sa, sb = a["scripts"], b["scripts"]
    s_diff = sorted(k for k in set(sa) & set(sb) if sa[k]["sha"] != sb[k]["sha"])
    out.append(f"  scripts: {len(set(sa) & set(sb)) - len(s_diff)} unchanged, "
               f"{len(s_diff)} edited, {len(set(sb) - set(sa))} new, "
               f"{len(set(sa) - set(sb))} removed")
    for k in s_diff:
        out.append(f"    edited   {k}  ({sa[k]['bytes']} → {sb[k]['bytes']} bytes)")
    pa, pb = a.get("plan_files", {}), b.get("plan_files", {})
    moved = sorted(k for k in set(pa) & set(pb) if pa[k] != pb[k])
    if moved:
        # If the plan moved, the two arms were not building the same shot.
        out.append(f"  ⚠ PLAN ARTIFACTS DIFFER: {', '.join(moved)}. The arms were not "
                   f"given the same specification, so any difference above confounds "
                   f"the change under test with a change of target.")
    return out


def report(a: dict, b: dict, *, margin: float = 0.10,
           judge_noise: float | None = None) -> str:
    head = [
        f"baseline A  {a.get('label') or '(unlabelled)'}  {a['at']}  "
        f"git {a['git']['commit']}{' DIRTY' if a['git']['dirty'] else ''}",
        f"baseline B  {b.get('label') or '(unlabelled)'}  {b['at']}  "
        f"git {b['git']['commit']}{' DIRTY' if b['git']['dirty'] else ''}",
    ]
    if a["shot"] != b["shot"]:
        head.append(f"  ⚠ DIFFERENT SHOTS ({a['shot']} vs {b['shot']}) — this is not a "
                    f"paired comparison at all.")
    if a["git"]["dirty"] or b["git"]["dirty"]:
        head.append("  ⚠ at least one arm was frozen from a DIRTY tree, so the commit "
                    "does not identify the code that produced it.")
    if a["git"]["commit"] == b["git"]["commit"] and not (a["git"]["dirty"] or b["git"]["dirty"]):
        head.append("  note: identical commit in both arms — any difference is run-to-run "
                    "variation, not the effect of a code change.")
    blocks = [head, [], _final_block(a, b, margin), [],
              _layer_block(a, b, judge_noise), [],
              _telemetry_block(a, b), [], _artifact_block(a, b)]
    return "\n".join(ln for block in blocks for ln in (block or [""]))
