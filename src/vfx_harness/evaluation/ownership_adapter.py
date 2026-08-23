"""Derive ownership-feasibility records mechanically from an expanded workspace.

Research status, paired with `ownership_feasibility` (not wired into any gate). The
promotion review's deepest blocker was that the checker's fixtures supplied `moments`
and `implicated_roles` by hand — voluntary metadata is no metadata. This adapter proves
the record can be built from authored and machine-expanded artifacts alone:

- layers, judge frames, reserved namespaces, and dependencies from `layers.json`;
- the acceptance measurement schedule as the union of declared judge frames — what the
  pipeline will actually measure, not an assumption of omniscience;
- one record per still-open `deferred_owner` register row, with today's conflated
  semantics made explicit (producer = verifier = repair route = owner layer) so the
  checker can measure the current contract while the ownership ADR is pending;
- `moments` extracted deterministically from the clause's exact text (frame word lists,
  numeric ranges, bare integers in table-row cells);
- `implicated_roles` as the owner's reserved namespaces — the honest mechanical default,
  with the documented limit that cross-layer implication becomes representable only
  through declared interfaces.

Every derivation either produces the key or produces it empty; nothing is omitted, so
the checker's record contract never depends on an author remembering a field.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_FRAME_LIST = re.compile(
    r"\bframes?\s+(\d+(?:\s*,?\s*(?:and\s+)?\d+)*)", re.IGNORECASE
)
_NUMBER = re.compile(r"\d+")


def extract_moments(text: str) -> list[int]:
    """Deterministic frame extraction from one clause's exact text.

    A clause names measurement moments only when it literally says
    "frame(s) N[, M[, and K]]". Beat ranges ("1-24") and bare table integers delimit
    spans; treating their endpoints as verification moments over-derives and floods the
    observability check with window metadata."""
    moments: set[int] = set()
    for match in _FRAME_LIST.finditer(text):
        moments.update(int(n) for n in _NUMBER.findall(match.group(1)))
    return sorted(m for m in moments if 1 <= m <= 100_000)


def derive_record(workspace: str | Path) -> dict[str, Any]:
    """Build a complete checker record from `layers.json` and `requirements.json`."""
    workspace = Path(workspace)
    layers_doc = json.loads((workspace / "layers.json").read_text(encoding="utf-8"))
    register = json.loads((workspace / "requirements.json").read_text(encoding="utf-8"))

    layers = []
    reserved_by_layer: dict[str, list[str]] = {}
    judge_union: set[int] = set()
    for row in layers_doc.get("layers", []):
        lid = str(row.get("id"))
        jit = row.get("jit") or {}
        frames = sorted({int(j["frame"]) for j in row.get("judge", []) if "frame" in j})
        judge_union.update(frames)
        reserved = [str(r) for r in jit.get("reserved_roles", [])]
        reserved_by_layer[lid] = reserved
        layers.append({
            "id": lid,
            "judge_frames": frames,
            "reserved_roles": reserved,
            "depends_on": [str(d) for d in jit.get("depends_on_layers", [])],
        })

    requirements = []
    for row in register.get("requirements", []):
        resolution = row.get("resolution") or {}
        if resolution.get("kind") != "deferred_owner":
            continue  # concretely-resolved rows carry no debt and no feasibility question
        owner = str(resolution.get("owner_layer"))
        requirements.append({
            "id": str(row.get("id")),
            "producer": owner,
            "verify_at": {"kind": "layer", "layer": owner},
            "repair_routes": [owner],
            "moments": extract_moments(str(row.get("statement") or "")),
            "implicated_roles": list(reserved_by_layer.get(owner, [])),
        })

    return {
        "layers": layers,
        "acceptance_moments": sorted(judge_union),
        "requirements": requirements,
        "interfaces": [],
    }
