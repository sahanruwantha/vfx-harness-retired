"""Durable crop-handle registry under shot-root ``state/refobs/`` (HIR-0155)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from PIL import Image

from vfx_harness.domain.refobs import (
    BOX_RULE,
    REFOBS_SCHEMA,
    UNREGISTERED_WITNESS_RULE,
    WHOLE_FRAME_RULE,
    parse_box,
    witness_id,
)
from vfx_harness.observability.provenance import atomic_write
from vfx_harness.observability.run_artifacts import shot_state_dir

_MIN_CROP_PX = 8


def refobs_dir(shot_folder: str | Path) -> Path:
    return shot_state_dir(shot_folder) / "refobs"


def missing_witness_ids(shot_folder: str | Path, witnesses: tuple[str, ...]) -> tuple[str, ...]:
    root = refobs_dir(shot_folder)
    missing: list[str] = []
    for token in witnesses:
        record = root / f"{token}.json"
        crop = root / f"{token}.png"
        if not record.is_file() or not crop.is_file() or crop.stat().st_size == 0:
            missing.append(token)
    return tuple(missing)


def load_witness_crop(shot_folder: str | Path, token: str) -> Path:
    missing = missing_witness_ids(shot_folder, (token,))
    if missing:
        raise FileNotFoundError(
            f"generate witness {token} is not in the refobs registry. "
            + UNREGISTERED_WITNESS_RULE
        )
    return refobs_dir(shot_folder) / f"{token}.png"


def mint_refobs(
    shot_folder: str | Path,
    source: str | Path,
    box: Any,
    *,
    source_rel: str,
) -> str:
    """Crop a reference still, persist it, and return a stable ``refobs-*`` id."""
    path = Path(source)
    if not path.is_file():
        raise FileNotFoundError(
            f"mint_refobs source {source_rel!r} is not a file. Crop a still under refs/."
        )
    rel = source_rel.replace("\\", "/").lstrip("./")
    if not rel.startswith("refs/") or ".." in Path(rel).parts:
        raise ValueError(
            f"mint_refobs source {source_rel!r} must be a relative refs/ path. "
            + WHOLE_FRAME_RULE
        )
    parsed = parse_box(box, "mint_refobs.box")
    image = Image.open(path).convert("RGB")
    width, height = image.size
    x0 = int(parsed[0] * width)
    y0 = int(parsed[1] * height)
    x1 = max(x0 + 1, int(parsed[2] * width))
    y1 = max(y0 + 1, int(parsed[3] * height))
    if x1 - x0 < _MIN_CROP_PX or y1 - y0 < _MIN_CROP_PX:
        raise ValueError(
            f"mint_refobs crop is {x1 - x0}x{y1 - y0} px; need at least "
            f"{_MIN_CROP_PX}x{_MIN_CROP_PX}. " + BOX_RULE
        )
    if x1 - x0 >= width and y1 - y0 >= height:
        raise ValueError(
            f"mint_refobs crop equals the full {width}x{height} still. " + WHOLE_FRAME_RULE
        )
    crop = image.crop((x0, y0, x1, y1))
    payload = crop.tobytes()
    digest = hashlib.sha256(payload).hexdigest()
    token = witness_id(rel, parsed, digest)
    root = refobs_dir(shot_folder)
    root.mkdir(parents=True, exist_ok=True)
    png = root / f"{token}.png"
    crop.save(png, format="PNG")
    record = {
        "schema": REFOBS_SCHEMA,
        "id": token,
        "source": rel,
        "box": list(parsed),
        "sha256": hashlib.sha256(png.read_bytes()).hexdigest(),
        "width": crop.size[0],
        "height": crop.size[1],
    }
    atomic_write(root / f"{token}.json", json.dumps(record, indent=2, sort_keys=True) + "\n")
    return token
