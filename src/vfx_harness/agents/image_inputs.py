"""Explicit, verified local stills for native model input and tool observations."""

from __future__ import annotations

import base64
import hashlib
import io
from pathlib import Path

import flynn_agents_sdk as flynn
from PIL import Image

from vfx_harness.orchestration.plan_bundle_integrity import read_real_file

MAX_IMAGE_BYTES = 8 * 1024 * 1024


def snapshot_image(folder: Path, relative: str) -> tuple[flynn.ImageInput, dict]:
    """Snapshot a selected still; links, escapes and unsupported bytes refuse."""
    payload = read_real_file(folder, folder / relative, "selected model image")
    if len(payload) > MAX_IMAGE_BYTES:
        raise ValueError(f"selected image {relative!r} exceeds {MAX_IMAGE_BYTES} bytes")
    with Image.open(io.BytesIO(payload)) as image:
        mime = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}.get(image.format)
        if mime is None:
            raise ValueError(f"selected image {relative!r} must be PNG, JPEG or WEBP")
        image.verify()
    url = f"data:{mime};base64,{base64.b64encode(payload).decode('ascii')}"
    return flynn.ImageInput(url), {"path": relative, "sha256": hashlib.sha256(payload).hexdigest()}
