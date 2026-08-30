"""Low-level work-unit field parsers and schema constants."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any

from vfx_harness.domain.field_parsing import identifier as _id
from vfx_harness.domain.field_parsing import mapping as _mapping
from vfx_harness.domain.field_parsing import text as _text

__all__ = ["_id", "_mapping", "_text"]

SCHEMA = 4
TEMPORAL_EVIDENCE = {"none", "keyframes", "motion"}
CLAIM_AUTHORITIES = {
    "executable_required",
    "qualified_qualitative_required",
    "human_required",
    "advisory",
}
CLAIM_KINDS = {"atomic", "interaction"}
EVIDENCE_KINDS = {
    "scene_contract",
    "image_contract",
    "semantic_diff",
    "qualification",
    "human_decision",
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


def canonical_unit_script_path(layer_id: str, unit_id: str) -> str:
    """Return the sole replay artifact path for one unit.

    Unit scripts are executable files, not addressable spans inside a composed layer
    script.  Keeping the path derivable from typed layer/unit identity prevents a
    planner from publishing fragment notation that the filesystem later interprets as
    a literal filename.
    """
    layer = _id(str(layer_id), "layer id")
    unit = _id(str(unit_id), "unit id")
    directory = layer.zfill(2) if layer.isdigit() else layer
    return f"build/units/{directory}/{unit}.py"
