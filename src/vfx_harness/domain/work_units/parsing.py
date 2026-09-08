"""Low-level work-unit field parsers and schema constants."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any

from vfx_harness.domain.field_parsing import identifier as _id
from vfx_harness.domain.field_parsing import mapping as _mapping
from vfx_harness.domain.field_parsing import text as _text
from vfx_harness.domain.unit_artifact_paths import canonical_unit_script_path as canonical_unit_script_path

__all__ = ["_id", "_mapping", "_text"]

SCHEMA = 4
TEMPORAL_EVIDENCE = {"none", "keyframes", "motion"}
CLAIM_AUTHORITIES = {
    "executable_required",
    "qualified_qualitative_required",
    "advisory",
}
# Retired vocabulary keeps a teaching rejection: nothing in the runtime produces a
# human decision for a unit claim, so such a claim could only drag an executable-only
# unit into raster rounds it cannot pay (HIR-0174). The human domain is judgment debt
# on the owning requirement (approved_start / planner_start, HIR-0124, HIR-0163).
RETIRED_CLAIM_AUTHORITIES = {"human_required"}
# Authorities an authored (staged) claim may carry. qualified_qualitative_required is
# minted only by the harness for judgment debt; no qualification suite registers a
# rubric artifact an authored claim could cite (HIR-0177).
STAGEABLE_CLAIM_AUTHORITIES = frozenset({"executable_required", "advisory"})
RETIRED_EVIDENCE_KINDS = {"human_decision"}
CLAIM_KINDS = {"atomic", "interaction"}
EVIDENCE_KINDS = {
    "scene_contract",
    "image_contract",
    "semantic_diff",
    "qualification",
}
UNIT_STATES = {
    "pending",
    "planning",
    "building",
    "frozen",
    "evaluating",
    "repairing",
    "passed",
    "failed",
    "hypothesis_falsified",
    "retryable",
    "blocked",
    "superseded",
}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")



def _strings(value: Any, where: str, *, allow_empty: bool = True) -> tuple[str, ...]:
    if not isinstance(value, list) or (not allow_empty and not value):
        suffix = "non-empty " if not allow_empty else ""
        raise ValueError(f"{where} must be a {suffix}list")
    out = tuple(_text(item, f"{where}[{index}]") for index, item in enumerate(value))
    if len(set(out)) != len(out):
        raise ValueError(f"{where} contains duplicates")
    return out


def _relative_path(value: Any, where: str) -> str:
    out = _text(value, where).replace("\\", "/")
    p = PurePosixPath(out)
    if p.is_absolute() or ".." in p.parts:
        raise ValueError(f"{where} must be a safe relative path")
    return out
