"""Reference-observation handles for generate-construction witnesses (ADR-0009).

Witness ids authorize generation. They are not Meshy payloads, not whole frames,
and not prose subjects. Crop bytes live in shot-root ``state/refobs/``.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import PurePosixPath
from typing import Any

from vfx_harness.domain.unit_artifact_paths import canonical_unit_script_path

GENERATE_WITNESS_PATTERN = re.compile(r"^refobs-[A-Za-z0-9]+$")
PROMOTED_GLB_PATTERN = re.compile(r"^build/construction/[0-9a-f]{64}\.glb$")
REFOBS_SCHEMA = "vfx-harness.refobs/v1"
PROMOTED_CONSTRUCTION_SCHEMA = "vfx-harness.promoted-construction/v1"

WHOLE_FRAME_RULE = (
    "a generate witness is a crop of a reference still; a whole frame, a prose "
    "subject, and text-only generate are not legal Meshy inputs"
)
BOX_RULE = (
    "mint_refobs box is normalised [x0, y0, x1, y1] with origin top-left, "
    "x0 < x1, y0 < y1, each coordinate in 0..1, and the box must not cover the "
    "full frame"
)
UNREGISTERED_WITNESS_RULE = (
    "generate witnesses must already exist in the refobs registry; mint_refobs "
    "on a refs/ still with a crop box before staging construction.witnesses"
)
PROMOTED_PATH_RULE = (
    "replay imports only hash-verified bytes under build/construction/<sha256>.glb; "
    "runs/, shot-root assets/, and network paths are not construction authority"
)


def parse_box(value: Any, where: str = "box") -> tuple[float, float, float, float]:
    """Parse a normalised top-left crop box and refuse a whole-frame cover."""
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError(f"{where} must be [x0, y0, x1, y1]. " + BOX_RULE)
    try:
        x0, y0, x1, y1 = (float(item) for item in value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{where} values must be numeric. " + BOX_RULE) from exc
    if not all(0.0 <= item <= 1.0 for item in (x0, y0, x1, y1)):
        raise ValueError(f"{where} {value!r} is outside 0..1. " + BOX_RULE)
    if not (x0 < x1 and y0 < y1):
        raise ValueError(f"{where} {value!r} is empty or inverted. " + BOX_RULE)
    if is_whole_frame((x0, y0, x1, y1)):
        raise ValueError(f"{where} {value!r} covers the full frame. " + WHOLE_FRAME_RULE)
    return (x0, y0, x1, y1)


def is_whole_frame(box: tuple[float, float, float, float], *, slack: float = 0.02) -> bool:
    """True when the box is the entire normalised frame (illegal as a witness)."""
    x0, y0, x1, y1 = box
    return x0 <= slack and y0 <= slack and x1 >= 1.0 - slack and y1 >= 1.0 - slack


def witness_id(source_rel: str, box: tuple[float, float, float, float], crop_sha256: str) -> str:
    """Stable refobs-* token from source path, box, and crop bytes."""
    payload = f"{source_rel}\n{box[0]:.6f},{box[1]:.6f},{box[2]:.6f},{box[3]:.6f}\n{crop_sha256}"
    return "refobs-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


def promoted_glb_relpath(sha256: str) -> str:
    digest = str(sha256).strip().lower()
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise ValueError(f"construction sha256 {sha256!r} is not a 64-char hex digest")
    return f"build/construction/{digest}.glb"


def construction_pointer_relpath(layer_id: str, unit_id: str) -> str:
    return str(PurePosixPath(canonical_unit_script_path(layer_id, unit_id)).with_suffix(".construction.json"))


def legal_promoted_relpath(relpath: str) -> bool:
    return bool(PROMOTED_GLB_PATTERN.fullmatch(relpath))
