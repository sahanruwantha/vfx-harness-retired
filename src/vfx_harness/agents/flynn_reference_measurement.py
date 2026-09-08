"""Bounded measurements of the reference stills owned by one planning scope."""

from __future__ import annotations

import io
import json
from collections.abc import Callable

import flynn_agents_sdk as flynn
from PIL import Image, UnidentifiedImageError

from vfx_harness.agents import image_inputs
from vfx_harness.evidence import metrics
from vfx_harness.observability.run_artifacts import RunLayout
from vfx_harness.orchestration.plan_bundle_integrity import digest, read_real_file

REFERENCE_READS = 6
MAX_MEASUREMENT_PIXELS = 16_000_000


def reference_measurement_tool(
    *, layout: RunLayout, references: tuple[str, ...], check_current: Callable[[], None],
    identity: Callable[[], dict],
) -> flynn.Tool:
    """The caller registers its current-scope guard; no cross-session seen-image cache."""
    check_current()
    hashes = {name: digest(read_real_file(layout.shot, layout.shot / name, "planning measurement reference"))
              for name in references}
    remaining = REFERENCE_READS

    def validate(arguments):
        if (set(arguments) != {"path"} or not isinstance(arguments["path"], str)
                or arguments["path"] not in hashes):
            raise ValueError("measure_ref requires one exact reference path judged by the bound layer or unit")

    async def measure(arguments):
        nonlocal remaining
        validate(arguments)
        check_current()
        name = arguments["path"]
        if remaining == 0:
            return flynn.ToolResult(status="refused", content=(flynn.TextContent(
                "Reference measurement budget exhausted; use the selected observations."
            ),), data_json=json.dumps({"schema": "vfx-harness.reference-measurement/v1", **identity(),
                                      "path": name, "remaining_reads": 0, "measured": False}))
        remaining -= 1
        payload = read_real_file(layout.shot, layout.shot / name, "planning measurement reference")
        if digest(payload) != hashes[name]:
            raise ValueError("reference bytes changed; start a new bound planning attempt")
        try:
            snapshot, source = image_inputs.snapshot_image_payload(payload, name)
            with Image.open(io.BytesIO(payload)) as image:
                width, height = image.size
                if width * height > MAX_MEASUREMENT_PIXELS:
                    raise ValueError(f"reference exceeds {MAX_MEASUREMENT_PIXELS} measurement pixels")
                if getattr(image, "n_frames", 1) != 1:
                    raise ValueError("reference measurement requires a single still, not an animated image")
                pixels = image.convert("RGB")
        except (UnidentifiedImageError, Image.DecompressionBombError, ValueError, OSError) as exc:
            check_current()
            return flynn.ToolResult(status="refused", content=(flynn.TextContent(str(exc)[:2000]),),
                                    data_json=json.dumps({
                                        "schema": "vfx-harness.reference-measurement/v1", **identity(),
                                        "path": name, "sha256": hashes[name], "measured": False,
                                        "remaining_reads": remaining,
                                    }))
        with pixels:
            fingerprint = metrics.canonical_fingerprint(pixels)
        check_current()
        if digest(read_real_file(layout.shot, layout.shot / name, "measured reference read-back")) != hashes[name]:
            raise ValueError("reference changed during measurement; discard this observation and restart")
        return flynn.ToolResult(content=(
            flynn.TextContent(f"Measured {name}. Read the image alongside its canonical fingerprint."),
            flynn.ImageContent(snapshot.url),
        ), data_json=json.dumps({
            "schema": "vfx-harness.reference-measurement/v1", **identity(), **source,
            "width": width, "height": height, "fingerprint": fingerprint, "measured": True,
            "remaining_reads": remaining, "plan_authority_changed": False,
        }, sort_keys=True, allow_nan=False))

    return flynn.Tool.structured("measure_ref", description=(
        "Measure one still judged by this planning scope. Returns the exact image with its canonical "
        "fingerprint and source hash on every read, including rereads. Six reads per session; "
        "PNG/JPEG/WEBP only, at most 8 MiB and 16 million pixels. Measurements do not approve a plan."
    ), parameters_json=json.dumps({
        "type": "object", "properties": {"path": {"type": "string", "enum": sorted(hashes)}},
        "required": ["path"], "additionalProperties": False,
    }), validate=validate, execute=measure)
