"""Strict value parsing for sealed layer replay observations."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from vfx_harness.domain.layer_finalization_claims import _relative_path, _text
from vfx_harness.domain.layer_finalization_values import json_object
from vfx_harness.domain.stop_envelope_primitives import canonical_digest, require_digest

_RENDER_CAPTURE_FIELDS = frozenset(
    {
        "schema",
        "frame",
        "mode",
        "scale",
        "resolution",
        "render_state",
        "warnings",
        "png_sha256",
        "capture_digest",
    }
)
_AUXILIARY_CAPTURE_FIELDS = frozenset({"kind", "locator", "sha256", "frames"})
_EXECUTION_FAILURE_FIELDS = frozenset({"stage", "message", "failure_digest"})
_EXECUTION_FAILURE_STAGES = frozenset(
    {"reset", "preamble", "predecessor_replay", "payer_replay", "scope_validation"}
)


def strings(value: object, where: str, *, allow_empty: bool) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{where} must be a list of strings")
    rows = tuple(_text(item, f"{where}[{index}]") for index, item in enumerate(value))
    if not allow_empty and not rows:
        raise ValueError(f"{where} must not be empty")
    if len(rows) != len(set(rows)):
        raise ValueError(f"{where} contains duplicates")
    return rows


def positive_frames(value: object, where: str) -> tuple[int, ...]:
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError(f"{where} must be a non-empty list of positive frames")
    rows: list[int] = []
    for index, item in enumerate(value):
        if not isinstance(item, int) or isinstance(item, bool) or item < 1:
            raise ValueError(f"{where}[{index}] must be a positive integer")
        rows.append(item)
    if len(rows) != len(set(rows)):
        raise ValueError(f"{where} contains duplicates")
    return tuple(rows)


def render_capture(
    value: object,
    where: str,
    *,
    frame: int,
    mode: str,
    scale: float,
) -> dict[str, Any]:
    row = json_object(value, where)
    if set(row) != _RENDER_CAPTURE_FIELDS:
        raise ValueError(f"{where} has unsupported canonical render-capture shape")
    if row["schema"] != "vfx-harness.canonical-render-capture/v1":
        raise ValueError(f"{where}.schema is unsupported")
    observed_frame = row["frame"]
    observed_scale = row["scale"]
    if (
        not isinstance(observed_frame, int)
        or isinstance(observed_frame, bool)
        or observed_frame < 1
        or observed_frame != frame
        or row["mode"] != mode
        or isinstance(observed_scale, bool)
        or not isinstance(observed_scale, (int, float))
        or not math.isfinite(float(observed_scale))
        or float(observed_scale) != float(scale)
    ):
        raise ValueError(f"{where} does not match replay observation settings")
    resolution = row["resolution"]
    if (
        not isinstance(resolution, list)
        or len(resolution) != 3
        or any(
            not isinstance(value, int) or isinstance(value, bool) or value < 1
            for value in resolution
        )
        or not isinstance(row["render_state"], Mapping)
        or not isinstance(row["warnings"], list)
        or any(not isinstance(value, str) for value in row["warnings"])
    ):
        raise ValueError(f"{where} has invalid render provenance")
    require_digest(row["png_sha256"], f"{where}.png_sha256")
    payload = dict(row)
    observed_digest = payload.pop("capture_digest")
    if require_digest(
        observed_digest,
        f"{where}.capture_digest",
    ) != canonical_digest(payload):
        raise ValueError(f"{where}.capture_digest does not match its payload")
    return row


def auxiliary_captures(
    value: object,
    where: str,
) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{where} must be an ordered list")
    rows: list[dict[str, Any]] = []
    identities: set[tuple[str, str]] = set()
    for index, raw in enumerate(value):
        row_where = f"{where}[{index}]"
        row = json_object(raw, row_where)
        if set(row) != _AUXILIARY_CAPTURE_FIELDS:
            raise ValueError(f"{row_where} has unsupported auxiliary capture shape")
        kind = _text(row["kind"], f"{row_where}.kind")
        locator = _relative_path(row["locator"], f"{row_where}.locator")
        sha256 = require_digest(row["sha256"], f"{row_where}.sha256")
        frames = positive_frames(row["frames"], f"{row_where}.frames")
        identity = (kind, locator)
        if identity in identities:
            raise ValueError(f"{where} contains duplicate auxiliary captures")
        identities.add(identity)
        rows.append(
            {
                "kind": kind,
                "locator": locator,
                "sha256": sha256,
                "frames": list(frames),
            }
        )
    return tuple(rows)


def execution_failure(value: object, where: str) -> dict[str, Any]:
    row = json_object(value, where)
    if set(row) != _EXECUTION_FAILURE_FIELDS:
        raise ValueError(f"{where} has unsupported replay execution-failure shape")
    stage = _text(row["stage"], f"{where}.stage")
    if stage not in _EXECUTION_FAILURE_STAGES:
        raise ValueError(f"{where}.stage is unsupported")
    message = _text(row["message"], f"{where}.message")
    expected = canonical_digest({"stage": stage, "message": message})
    if require_digest(row["failure_digest"], f"{where}.failure_digest") != expected:
        raise ValueError(f"{where}.failure_digest does not match its payload")
    return {"stage": stage, "message": message, "failure_digest": expected}


def evidence_rows(value: object, where: str) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{where} must be a list")
    rows: list[dict[str, Any]] = []
    ids: list[str] = []
    for index, value_row in enumerate(value):
        row_where = f"{where}[{index}]"
        row = json_object(value_row, row_where)
        identifier = _text(row.get("id"), f"{row_where}.id")
        if not isinstance(row.get("authoritative"), bool):
            raise ValueError(f"{row_where}.authoritative must be a boolean")
        if not isinstance(row.get("pass"), bool):
            raise ValueError(f"{row_where}.pass must be a boolean")
        rows.append(row)
        ids.append(identifier)
    if len(ids) != len(set(ids)):
        raise ValueError(f"{where} contains duplicate evidence ids")
    return tuple(rows)


__all__ = [
    "auxiliary_captures",
    "evidence_rows",
    "execution_failure",
    "positive_frames",
    "render_capture",
    "strings",
]
