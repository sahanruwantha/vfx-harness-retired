"""Receipt-bound projections for finalized runtime image-check revalidation."""

from __future__ import annotations

import hashlib
import stat
from pathlib import Path

from vfx_harness.evidence import checks as image_checks

_LAYER_REVALIDATION_PROJECTION_SCHEMA = (
    "vfx-harness.layer-image-check-revalidation/v1"
)
_LAYER_REVALIDATION_PROJECTION_FIELDS = {
    "schema",
    "layer_id",
    "source_sha256",
    "replacement_sha256",
    "replacement_text",
    "result",
}


def layer_revalidation_projection(
    candidate: image_checks.PreparedLayerRevalidation,
) -> dict:
    """Bind exact runtime-check replacement bytes into terminal authority."""

    if not isinstance(candidate, image_checks.PreparedLayerRevalidation):
        raise ValueError("layer revalidation projection requires a prepared candidate")
    replacement_text = None
    replacement_sha256 = None
    if candidate.replacement_path is not None:
        if candidate.replacement_identity is None:
            raise ValueError("prepared runtime-check replacement lacks an identity")
        observed = candidate.replacement_path.lstat()
        if (
            image_checks._revalidation_identity(observed)
            != candidate.replacement_identity
        ):
            raise ValueError("prepared runtime-check replacement changed")
        raw = candidate.replacement_path.read_bytes()
        replacement_sha256 = hashlib.sha256(raw).hexdigest()
        try:
            replacement_text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(
                "prepared runtime-check replacement must be UTF-8 JSON"
            ) from exc
    return {
        "schema": _LAYER_REVALIDATION_PROJECTION_SCHEMA,
        "layer_id": candidate.layer_id,
        "source_sha256": candidate.source_sha256,
        "replacement_sha256": replacement_sha256,
        "replacement_text": replacement_text,
        "result": {
            "kept": int(candidate.result.get("kept") or 0),
            "dropped": [
                {"id": str(identifier), "reason": str(reason)}
                for identifier, reason in candidate.result.get("dropped", [])
            ],
        },
    }


def reconcile_layer_revalidation_projection(
    shot_folder: str | Path,
    layer_id: str,
    projection: dict,
) -> dict:
    """Apply or confirm the exact receipt-bound runtime-check replacement."""

    if (
        not isinstance(projection, dict)
        or set(projection) != _LAYER_REVALIDATION_PROJECTION_FIELDS
        or projection.get("schema") != _LAYER_REVALIDATION_PROJECTION_SCHEMA
    ):
        raise ValueError("layer revalidation projection has an unsupported shape")
    if projection.get("layer_id") != str(layer_id):
        raise ValueError("layer revalidation projection belongs to another layer")
    source_sha256 = projection.get("source_sha256")
    if source_sha256 is not None and (
        not isinstance(source_sha256, str)
        or len(source_sha256) != 64
        or any(ch not in "0123456789abcdef" for ch in source_sha256)
    ):
        raise ValueError("layer revalidation source_sha256 must be null or SHA-256")
    replacement_text = projection.get("replacement_text")
    replacement_sha256 = projection.get("replacement_sha256")
    if (replacement_text is None) != (replacement_sha256 is None):
        raise ValueError(
            "layer revalidation replacement text and SHA-256 must appear together"
        )
    if replacement_text is not None:
        if not isinstance(replacement_text, str) or not isinstance(
            replacement_sha256,
            str,
        ):
            raise ValueError("layer revalidation replacement must be typed text")
        if (
            hashlib.sha256(replacement_text.encode("utf-8")).hexdigest()
            != replacement_sha256
        ):
            raise ValueError("layer revalidation replacement SHA-256 is stale")
    raw_result = projection.get("result")
    if not isinstance(raw_result, dict) or set(raw_result) != {"kept", "dropped"}:
        raise ValueError("layer revalidation result has an unsupported shape")
    kept = raw_result.get("kept")
    dropped = raw_result.get("dropped")
    if (
        not isinstance(kept, int)
        or isinstance(kept, bool)
        or kept < 0
        or not isinstance(dropped, list)
        or any(
            not isinstance(row, dict)
            or set(row) != {"id", "reason"}
            or not isinstance(row.get("id"), str)
            or not row["id"]
            or not isinstance(row.get("reason"), str)
            or not row["reason"]
            for row in dropped
        )
    ):
        raise ValueError("layer revalidation result is invalid")
    result = {
        "kept": kept,
        "dropped": [(row["id"], row["reason"]) for row in dropped],
    }

    root = Path(shot_folder)
    spec = root / "runtime_checks.json"
    current, current_identity = image_checks._read_revalidation_source(spec)
    current_sha256 = None if current is None else hashlib.sha256(current).hexdigest()
    if replacement_text is None or current_sha256 == replacement_sha256:
        expected = source_sha256 if replacement_text is None else replacement_sha256
        if current_sha256 != expected:
            raise ValueError(
                "runtime image checks conflict with the terminal revalidation projection"
            )
        return result
    if current_sha256 != source_sha256:
        raise ValueError(
            "runtime image checks changed outside the terminal revalidation projection"
        )
    parent_identity = image_checks._revalidation_identity(spec.parent.lstat())
    if not stat.S_ISDIR(parent_identity.mode):
        raise ValueError("runtime image-check parent must be a real directory")
    replacement_path, replacement_identity = (
        image_checks._stage_revalidation_replacement(
            spec,
            replacement_text.encode("utf-8"),
        )
    )
    candidate = image_checks.PreparedLayerRevalidation(
        shot_folder=root,
        layer_id=str(layer_id),
        source_sha256=source_sha256,
        source_identity=current_identity,
        parent_identity=parent_identity,
        replacement_path=replacement_path,
        replacement_identity=replacement_identity,
        result=result,
    )
    try:
        return image_checks.commit_layer_revalidation(candidate)
    finally:
        image_checks.discard_layer_revalidation(candidate)


__all__ = [
    "layer_revalidation_projection",
    "reconcile_layer_revalidation_projection",
]
