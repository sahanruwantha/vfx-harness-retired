"""Image transport and objective look readouts for Blender agent tools."""

from __future__ import annotations

import base64
import io
from pathlib import Path

from PIL import Image, ImageChops, ImageStat

from vfx_harness.evidence.metrics import look_vector

# Keep tool-result images below the SDK buffer while retaining useful visual detail.
MAX_IMAGE_WIDTH = 2048
JPEG_QUALITY = 85


def load_image(path: str) -> Image.Image:
    image = Image.open(path).convert("RGB")
    if image.width > MAX_IMAGE_WIDTH:
        image = image.resize((MAX_IMAGE_WIDTH, round(image.height * MAX_IMAGE_WIDTH / image.width)))
    return image


def encode_jpeg(image: Image.Image) -> str:
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=JPEG_QUALITY)
    return base64.standard_b64encode(buffer.getvalue()).decode()


def exposure_summary(image: Image.Image, *, feedback_groups=None) -> str:
    """Objective exposure readout so the agent does not estimate clipping by eye."""
    groups = set(feedback_groups) if feedback_groups is not None else {"exposure"}
    if "exposure" not in groups:
        return "exposure metrics suppressed — exposure is owned by another layer"
    metrics = look_vector(image)
    mean = metrics["exposure_mean"]
    clipped = metrics["clipped_pct"]
    black = metrics["black_pct"]
    line = f"exposure: mean {mean:.0f}/255 · clipped(blown) {clipped:.0f}% · black {black:.0f}%"
    if clipped > 12:
        line += "  ⚠ highlights BLOWN — lower emission/light strength or exposure"
    if black > 85:
        line += (
            "  ⚠ frame almost entirely black — on draft/EEVEE use the typed scene "
            "cause card before changing energy, density, or exposure; on solid/wire "
            "inspect framing and visibility because Workbench does not test lighting"
        )
    return line


def region_metrics(image: Image.Image) -> dict:
    """Look-agnostic structure and halation measurements by horizontal band."""
    metrics = look_vector(image)
    bands = {name: (metrics[f"band_mean_{name}"], metrics[f"structure_{name}"]) for name in ("top", "mid", "bot")}
    sampled = 960 * max(1, round(image.height * 960 / image.width)) / 4
    hot_core = metrics.get("hot_core", 0.0)
    return {
        "bands": bands,
        "halation": metrics.get("halation"),
        "hot_px": round(hot_core * sampled / 1e6),
        "hot_core": round(hot_core),
    }


def reference_metrics_summary(
    image: Image.Image,
    ref: Image.Image | None = None,
    *,
    feedback_groups=None,
) -> str:
    """Summarize structure/halation and, when present, actionable reference deltas."""
    groups = (
        set(feedback_groups)
        if feedback_groups is not None
        else {"exposure", "detail", "emitters", "halation", "color", "motion"}
    )
    if "detail" not in groups and "halation" not in groups:
        return (
            "reference appearance metrics suppressed — use authoritative contracts and diagnostics owned by this layer"
        )
    measured = region_metrics(image)
    parts = []
    for band in ("top", "mid", "bot"):
        mean, deviation = measured["bands"][band]
        parts.append(f"{band} μ{mean:.0f}/σ{deviation:.0f}")
    halation = measured["halation"]
    line = f"structure: {' · '.join(parts)}" if "detail" in groups else ""
    if "halation" in groups:
        if line:
            line += " · "
        line += "halation " + (
            f"{halation}" if halation is not None else f"n/a (only {measured['hot_px']}px of hot core)"
        )
    if ref is None:
        return line

    reference = region_metrics(ref)
    deltas = []
    if "detail" in groups:
        for band in ("top", "mid", "bot"):
            deviation = measured["bands"][band][1]
            reference_deviation = reference["bands"][band][1]
            if reference_deviation > 4 and deviation < reference_deviation * 0.45:
                deltas.append(
                    f"{band} σ{deviation:.0f} vs ref σ{reference_deviation:.0f} "
                    f"→ needs ~{reference_deviation / max(deviation, 1):.1f}× more structure"
                )
    reference_halation = reference["halation"]
    if "halation" in groups and reference_halation is not None and reference_halation > 1:
        if halation is None:
            deltas.append(
                f"halation n/a vs ref {reference_halation} → only "
                f"{measured['hot_px']}px reach the hot-core threshold "
                f"(ref {reference['hot_px']}px): raise emitter or key intensity "
                "until something blows out, THEN judge bloom"
            )
        elif halation < reference_halation * 0.45:
            deltas.append(f"halation {halation} vs ref {reference_halation} → crank bloom")
    if deltas:
        line += "\nref gap: " + "; ".join(deltas)
    return line


def subtract_png(a_path: str, b_path: str, dest: str) -> dict:
    """Write ``|A - B|`` and report whether the edit produced a visible delta."""
    a = Image.open(a_path).convert("RGB")
    b = Image.open(b_path).convert("RGB")
    resized = a.size != b.size
    if resized:
        b = b.resize(a.size, Image.LANCZOS)
    diff = ImageChops.difference(a, b)
    stats = ImageStat.Stat(diff.convert("L"))
    mean = stats.mean[0]
    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    diff.save(dest)
    return {
        "image_path": dest,
        "mean_delta": round(mean, 2),
        "max_delta": int(stats.extrema[0][1]),
        "did_work": mean > 1.5,
        "resized": resized,
    }


def image_content(path: str, caption: str, *, feedback_groups=None) -> dict:
    image = load_image(path)
    return {
        "content": [
            {
                "type": "text",
                "text": (
                    f"{caption}\n{exposure_summary(image, feedback_groups=feedback_groups)}\n"
                    f"{reference_metrics_summary(image, feedback_groups=feedback_groups)}"
                ),
            },
            {"type": "image", "data": encode_jpeg(image), "mimeType": "image/jpeg"},
        ]
    }

# Aliases used by MCP tool bodies and plan_tools.
_b64 = encode_jpeg
_load = load_image
_stats = exposure_summary
_metrics_line = reference_metrics_summary
_region_metrics = region_metrics
_image = image_content
