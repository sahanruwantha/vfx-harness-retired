"""Name the judgments a terminal finalization failed on, and whether evidence existed.

`layer_publication` held the whole terminal receipt and reported one field of it:

    layer 2 terminal finalization is 'failed', not 'passed'

The receipt beside that string carried three canonical rows decided
`no_optical_signal` at frames 1, 51 and 151, and an evaluation group naming the debt
and requirement they were paying. An operator got none of it, and the driver got a
generic engineering route (HIR-0247).

The distinction this draws is the one a reader acts on: **a judgment that scored badly
and a judgment that could not be made are different failures with different owners.**
The first is the work; the second is a missing prerequisite.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from vfx_harness.domain.verdict_deciders import CONTRACT_GAP_DECIDERS

#: The one decider meaning the plate itself could not be observed.
NO_SIGNAL_DECIDER = "no_optical_signal"

# A verdict decided by one of these did not weigh the work: either the authority was
# incomplete (a contract gap) or the plate could not be judged at all. Everything else
# that fails is a negative judgment of work that was actually evaluated.
#
# This is deliberately NOT `CONTRACT_GAP_DECIDERS | JUDGMENT_ONLY_DECIDERS`, which is what
# it was first written as. `JUDGMENT_ONLY_DECIDERS` means "decided by judgment rather than
# mechanically" -- the opposite property -- and it carries
# `provisional_requirement_contract_gap`, which `_provisional_composition_contract_gap`
# emits *after* a qualified critic identified a concrete defect. That producer returns
# early for `no_optical_signal` precisely because the two are different, and then clears
# `issues` and moves the criticism into `contract_gaps`. Reading the union treated an
# actionable observation as an unproducible plate and dropped the criticism with it, on a
# set whose own docstring says it is critic-derived.
EVIDENCE_UNAVAILABLE_DECIDERS = CONTRACT_GAP_DECIDERS | {NO_SIGNAL_DECIDER}


def _group_owner(index: int, groups: Sequence[Mapping[str, Any]]) -> str:
    """The debt and requirements the canonical row at ``index`` was paying, if any."""
    for group in groups:
        if not isinstance(group, Mapping):
            continue
        start = group.get("canonical_start")
        end = group.get("canonical_end")
        if not isinstance(start, int) or not isinstance(end, int):
            continue
        if not start <= index < end:
            continue
        parts: list[str] = []
        requirements = group.get("requirement_ids")
        if isinstance(requirements, Sequence) and not isinstance(requirements, str):
            named = [str(item) for item in requirements if str(item)]
            if named:
                parts.append("requirement " + ", ".join(named))
        debt = group.get("debt_id")
        if debt:
            parts.append(f"debt {str(debt)[:20]}")
        if parts:
            return f"group {group.get('group_index')} paying " + " / ".join(parts)
        return f"group {group.get('group_index')}"
    return "no group"


def _first_criticism(verdict: Mapping[str, Any]) -> str:
    """The criticism this verdict carries, wherever the producer left it.

    `_provisional_composition_contract_gap` sets `issues` to `[]` and preserves the
    critic's exact observation in `contract_gaps`, so reading only `issues` reports a
    failure with no reason on precisely the verdicts that carry the most specific one.
    """
    issues = verdict.get("issues")
    if isinstance(issues, Sequence) and not isinstance(issues, str) and issues:
        return str(issues[0])
    gaps = verdict.get("contract_gaps")
    if isinstance(gaps, Sequence) and not isinstance(gaps, str):
        for gap in gaps:
            if not isinstance(gap, Mapping):
                continue
            observation = gap.get("observation")
            text = (
                observation.get("observation")
                if isinstance(observation, Mapping)
                else None
            )
            if text:
                return str(text)
            if gap.get("reason"):
                return str(gap["reason"])
    return ""


def describe_failed_finalization(
    canonical: Sequence[Mapping[str, Any]],
    groups: Sequence[Mapping[str, Any]] = (),
) -> str:
    """One sentence naming what failed, why, and who owns it.

    Returns an empty string when nothing in ``canonical`` failed -- a caller reporting a
    non-passed status with no failing row has a different problem and should say so
    rather than borrow this one's words.
    """
    failed: list[tuple[str, int, Any, str]] = []
    for index, row in enumerate(canonical):
        if not isinstance(row, Mapping):
            continue
        verdict = row.get("verdict")
        verdict = verdict if isinstance(verdict, Mapping) else {}
        if verdict.get("pass"):
            continue
        decided_by = str(verdict.get("decided_by") or "undeclared")
        failed.append((decided_by, index, row.get("frame"), _first_criticism(verdict)))
    if not failed:
        return ""

    by_decider: dict[str, list[tuple[int, Any, str]]] = {}
    for decided_by, index, frame, issue in failed:
        by_decider.setdefault(decided_by, []).append((index, frame, issue))

    sentences: list[str] = []
    for decided_by in sorted(by_decider):
        rows = by_decider[decided_by]
        frames = ", ".join(f"f{frame}" for _index, frame, _issue in rows)
        owners = sorted({_group_owner(index, groups) for index, _frame, _issue in rows})
        kind = (
            "the evidence could not be produced"
            if decided_by in EVIDENCE_UNAVAILABLE_DECIDERS
            else "the work was judged and did not pass"
        )
        text = (
            f"{len(rows)} judgment(s) failed at {frames} decided by {decided_by!r} -- "
            f"{kind}; {', '.join(owners)}"
        )
        issue = next((issue for _i, _f, issue in rows if issue), "")
        if issue:
            text += f". First issue: {issue}"
        sentences.append(text)
    return " | ".join(sentences)


__all__ = ["EVIDENCE_UNAVAILABLE_DECIDERS", "describe_failed_finalization"]
