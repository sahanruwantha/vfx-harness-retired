"""Render the evidence a rematerialization was dispatched against into its kickoff.

`vfx plan --layer N --rematerialize --evidence <locator>` used to record the locators for
audit only: the materializer saw the operator's trigger sentence and nothing of what the
finding measured. When the finding belongs to a downstream unit (layer 2's geometry proving
that layer 1's sealed camera cannot frame it), the replacement camera layer must design
against that measurement or it re-authors the same defect and the controller converges on
the repeated fingerprint (HIR-0191). Typed hypothesis-falsification records are rendered
compactly and bounded; every other locator is named by path so nothing cited is hidden.
"""

from __future__ import annotations

from pathlib import Path

from vfx_harness.domain.unit_outcomes import (
    HypothesisFalsification,
    load_hypothesis_falsification,
)

REASON_LIMIT = 600
BLOCK_LIMIT = 6000


def _resolve(shot_root: Path, locator: str) -> Path:
    path = (shot_root / locator).resolve()
    path.relative_to(shot_root)
    return path


def _render_finding(finding: HypothesisFalsification, locator: str) -> str:
    lines = [
        f"- finding {finding.record_id} ({locator}): layer {finding.layer} unit {finding.unit}",
        f"  contracts: {', '.join(finding.contract_ids) or '(none)'}",
    ]
    for observation in finding.observations:
        classification = str(observation.get("classification") or "observation")
        ids = observation.get("contract_ids") or []
        reason = str(observation.get("reason") or "").strip()
        if len(reason) > REASON_LIMIT:
            reason = reason[: REASON_LIMIT - 1] + "…"
        lines.append(f"  {classification} [{', '.join(str(item) for item in ids)}]: {reason}")
    lines.append(f"  required authority: {finding.conflict.required_authority}")
    if finding.fault_owner_units:
        lines.append(f"  fault owners: {', '.join(finding.fault_owner_units)}")
    if finding.affected:
        lines.append(f"  affected units: {', '.join(finding.affected)}")
    return "\n".join(lines)


def replacement_evidence_block(shot_root: str | Path, locators: tuple[str, ...] | list[str]) -> str:
    """The evidence card appended to a rematerialization's replacement reason.

    Locators must stay inside the shot root. A locator that parses as a hypothesis
    falsification is rendered; any other file is named so the materializer knows it was
    cited. The whole block is bounded so a batch of findings cannot scale the kickoff.
    """

    root = Path(shot_root).resolve()
    rendered: list[str] = []
    for locator in dict.fromkeys(str(item) for item in locators):
        path = _resolve(root, locator)
        try:
            finding = load_hypothesis_falsification(path)
        except (OSError, ValueError):
            rendered.append(f"- evidence {locator}")
            continue
        rendered.append(_render_finding(finding, locator))
    if not rendered:
        return ""
    block = (
        "EVIDENCE THIS REPLACEMENT MUST ANSWER. Each record below is an executable finding "
        "that the discarded design could not satisfy; design so that its measured floor no "
        "longer holds, or record why the requirement itself must change:\n" + "\n".join(rendered)
    )
    if len(block) > BLOCK_LIMIT:
        block = block[: BLOCK_LIMIT - 1] + "…"
    return block


__all__ = ["BLOCK_LIMIT", "REASON_LIMIT", "replacement_evidence_block"]
