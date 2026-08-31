"""Strict semantic projection for finished-chain acceptance evidence.

Diagnostic prose and source locators remain in the run audit.  This module validates
the complete producer record and returns only the measured facts that may affect an
acceptance outcome or stop transaction identity.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from vfx_harness.domain.acceptance_outcomes import ACCEPTANCE_DECISION_KINDS
from vfx_harness.domain.stop_envelope_primitives import canonical_digest, require_digest

MOMENT_SCHEMA = "vfx-harness.acceptance-moment-evidence/v1"
RENDER_CAPTURE_SCHEMA = "vfx-harness.canonical-render-capture/v1"

_MOMENT_FIELDS = {
    "schema",
    "frame",
    "ref",
    "render",
    "render_capture",
    "mean",
    "pass",
    "critic_pass",
    "decided_by",
    "metric_failures",
    "metric_readings",
    "scores",
    "issues",
    "contract_evidence",
}
_RENDER_CAPTURE_FIELDS = {
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
_RENDER_STATE_FIELDS = {
    "engine",
    "resolution",
    "image_settings",
    "workbench_shading",
    "eevee_render_samples",
    "color_management",
}
_IMAGE_SETTINGS_FIELDS = {"file_format", "color_mode", "color_depth"}
_COLOR_MANAGEMENT_FIELDS = {
    "display_device",
    "view_transform",
    "look",
    "exposure",
    "gamma",
}
_METRIC_FIELDS = {"metric_id", "value", "reference", "relative_delta", "blocking"}
_CONTRACT_FIELDS = {
    "id",
    "axis",
    "metric",
    "value",
    "target",
    "pass",
    "origin",
    "source",
    "authoritative",
    "owner_layer",
    "error",
}


def _object(value: object, where: str, fields: set[str]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        found = (
            sorted((repr(key) for key in value), key=str)
            if isinstance(value, Mapping)
            else type(value).__name__
        )
        raise ValueError(f"{where} fields mismatch; expected={sorted(fields)}, found={found}")
    return value


def _text(value: object, where: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or value != value.strip() or (not value and not allow_empty):
        qualifier = "trimmed string" if allow_empty else "non-empty trimmed string"
        raise ValueError(f"{where} must be a {qualifier}")
    return value


def _number(value: object, where: str) -> int | float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
    ):
        raise ValueError(f"{where} must be a finite scalar")
    return value


def _positive_ints(value: object, where: str) -> list[int]:
    if (
        not isinstance(value, list)
        or len(value) != 3
        or not all(isinstance(item, int) and not isinstance(item, bool) and item > 0 for item in value)
    ):
        raise ValueError(f"{where} must contain three positive integers")
    return list(value)


def validate_moment_record(value: object, where: str) -> Mapping[str, Any]:
    row = _object(value, where, _MOMENT_FIELDS)
    if row["schema"] != MOMENT_SCHEMA:
        raise ValueError(f"{where}.schema must be {MOMENT_SCHEMA!r}")
    if not isinstance(row["frame"], int) or isinstance(row["frame"], bool):
        raise ValueError(f"{where}.frame must be an integer")
    _text(row["ref"], f"{where}.ref")
    _text(row["render"], f"{where}.render")
    _number(row["mean"], f"{where}.mean")
    if not isinstance(row["pass"], bool) or not isinstance(row["critic_pass"], bool):
        raise ValueError(f"{where} verdict fields must be Boolean")
    if row["decided_by"] not in ACCEPTANCE_DECISION_KINDS:
        raise ValueError(
            f"{where}.decided_by must be one of {sorted(ACCEPTANCE_DECISION_KINDS)}"
        )
    for field in ("metric_failures", "metric_readings", "issues", "contract_evidence"):
        if not isinstance(row[field], list):
            raise ValueError(f"{where}.{field} must be a list")
    if any(not isinstance(item, str) for item in (*row["metric_failures"], *row["issues"])):
        raise ValueError(f"{where} diagnostic rows must be strings")
    if not isinstance(row["scores"], Mapping):
        raise ValueError(f"{where}.scores must be an object")
    return row


def _render_capture(row: Mapping[str, Any], *, render_sha256: str, where: str) -> dict[str, Any]:
    receipt = _object(row["render_capture"], f"{where}.render_capture", _RENDER_CAPTURE_FIELDS)
    if receipt["schema"] != RENDER_CAPTURE_SCHEMA:
        raise ValueError(f"{where}.render_capture.schema must be {RENDER_CAPTURE_SCHEMA!r}")
    if receipt["frame"] != row["frame"]:
        raise ValueError(f"{where}.render_capture.frame is stale")
    if receipt["mode"] != "eevee":
        raise ValueError(f"{where}.render_capture.mode must be 'eevee'")
    if _number(receipt["scale"], f"{where}.render_capture.scale") <= 0:
        raise ValueError(f"{where}.render_capture.scale must be positive")
    resolution = _positive_ints(receipt["resolution"], f"{where}.render_capture.resolution")
    warnings = receipt["warnings"]
    if not isinstance(warnings, list) or any(not isinstance(item, str) for item in warnings):
        raise ValueError(f"{where}.render_capture.warnings must be strings")
    require_digest(receipt["png_sha256"], f"{where}.render_capture.png_sha256")
    if receipt["png_sha256"] != render_sha256:
        raise ValueError(f"{where}.render_capture.png_sha256 is stale")

    state = _object(receipt["render_state"], f"{where}.render_capture.render_state", _RENDER_STATE_FIELDS)
    engine = _text(state["engine"], f"{where}.render_capture.render_state.engine")
    if engine not in {"BLENDER_EEVEE", "BLENDER_EEVEE_NEXT"}:
        raise ValueError(f"{where}.render_capture.render_state.engine is not EEVEE")
    if _positive_ints(state["resolution"], f"{where}.render_capture.render_state.resolution") != resolution:
        raise ValueError(f"{where}.render_capture render-state resolution is stale")
    image = _object(
        state["image_settings"],
        f"{where}.render_capture.render_state.image_settings",
        _IMAGE_SETTINGS_FIELDS,
    )
    for key in _IMAGE_SETTINGS_FIELDS:
        _text(image[key], f"{where}.render_capture.render_state.image_settings.{key}")
    _text(state["workbench_shading"], f"{where}.render_capture.render_state.workbench_shading")
    if state["eevee_render_samples"] is not None:
        _number(state["eevee_render_samples"], f"{where}.render_capture.render_state.eevee_render_samples")
    color = _object(
        state["color_management"],
        f"{where}.render_capture.render_state.color_management",
        _COLOR_MANAGEMENT_FIELDS,
    )
    for key in ("display_device", "view_transform"):
        _text(color[key], f"{where}.render_capture.render_state.color_management.{key}")
    _text(color["look"], f"{where}.render_capture.render_state.color_management.look", allow_empty=True)
    _number(color["exposure"], f"{where}.render_capture.render_state.color_management.exposure")
    _number(color["gamma"], f"{where}.render_capture.render_state.color_management.gamma")

    capture_payload = {key: item for key, item in receipt.items() if key != "capture_digest"}
    if receipt["capture_digest"] != canonical_digest(capture_payload):
        raise ValueError(f"{where}.render_capture.capture_digest is stale")
    return {
        "schema": receipt["schema"],
        "frame": receipt["frame"],
        "mode": receipt["mode"],
        "scale": receipt["scale"],
        "resolution": resolution,
        # The producer records the UI's Workbench shading mode for audit, but EEVEE
        # does not consume it.  It therefore cannot distinguish two semantic render
        # attempts that produced the same pixels under the same EEVEE state.
        "render_state": {
            key: item
            for key, item in state.items()
            if key != "workbench_shading"
        },
        "png_sha256": receipt["png_sha256"],
    }


def _metric_readings(row: Mapping[str, Any], where: str) -> list[dict[str, Any]]:
    raw = row["metric_readings"]
    if row["metric_failures"] and not raw:
        raise ValueError(f"{where} has blocking metric prose without typed metric readings")
    readings: list[dict[str, Any]] = []
    for index, value in enumerate(raw):
        reading = _object(value, f"{where}.metric_readings[{index}]", _METRIC_FIELDS)
        metric_id = _text(reading["metric_id"], f"{where}.metric_readings[{index}].metric_id")
        normalized = {
            "metric_id": metric_id,
            "value": _number(reading["value"], f"{where}.metric_readings[{index}].value"),
            "reference": _number(reading["reference"], f"{where}.metric_readings[{index}].reference"),
            "relative_delta": _number(
                reading["relative_delta"], f"{where}.metric_readings[{index}].relative_delta"
            ),
            "blocking": reading["blocking"],
        }
        if normalized["blocking"] is not True:
            raise ValueError(f"{where}.metric_readings[{index}] must be blocking")
        readings.append(normalized)
    if len({item["metric_id"] for item in readings}) != len(readings):
        raise ValueError(f"{where}.metric_readings contains duplicate metric ids")
    return sorted(readings, key=lambda item: item["metric_id"])


def _contract_evidence(row: Mapping[str, Any], where: str) -> list[dict[str, Any]]:
    readings: list[dict[str, Any]] = []
    for index, value in enumerate(row["contract_evidence"]):
        evidence = _object(value, f"{where}.contract_evidence[{index}]", _CONTRACT_FIELDS)
        _text(evidence["id"], f"{where}.contract_evidence[{index}].id")
        for field in ("axis", "metric", "target", "origin", "source"):
            _text(evidence[field], f"{where}.contract_evidence[{index}].{field}")
        if evidence["owner_layer"] is not None:
            _text(evidence["owner_layer"], f"{where}.contract_evidence[{index}].owner_layer")
        if evidence["value"] is not None:
            _number(evidence["value"], f"{where}.contract_evidence[{index}].value")
        if not isinstance(evidence["pass"], bool) or not isinstance(evidence["authoritative"], bool):
            raise ValueError(f"{where}.contract_evidence[{index}] verdict fields must be Boolean")
        _text(evidence["error"], f"{where}.contract_evidence[{index}].error", allow_empty=True)
        readings.append(
            {
                key: evidence[key]
                for key in (
                    "id",
                    "axis",
                    "metric",
                    "value",
                    "target",
                    "pass",
                    "origin",
                    "source",
                    "authoritative",
                    "owner_layer",
                )
            }
        )
    if len({item["id"] for item in readings}) != len(readings):
        raise ValueError(f"{where}.contract_evidence contains duplicate contract ids")
    return sorted(readings, key=lambda item: item["id"])


def semantic_capture(
    value: object,
    *,
    moment_id: str,
    render_sha256: str,
    reference_sha256: str,
) -> dict[str, Any]:
    """Validate one complete moment record and return its identity-bearing facts."""

    where = f"acceptance moment {moment_id}"
    row = validate_moment_record(value, where)
    require_digest(render_sha256, f"{where}.render_sha256")
    require_digest(reference_sha256, f"{where}.reference_sha256")
    scores: dict[str, int | float] = {}
    for axis, score in row["scores"].items():
        axis_id = _text(axis, f"{where}.scores key")
        scores[axis_id] = _number(score, f"{where}.scores.{axis_id}")
    metric_readings = _metric_readings(row, where)
    contract_evidence = _contract_evidence(row, where)
    failed_authoritative_contracts = [
        evidence
        for evidence in contract_evidence
        if evidence["authoritative"] is True and evidence["pass"] is not True
    ]
    expected_pass = (
        row["critic_pass"]
        and not metric_readings
        and not failed_authoritative_contracts
    )
    if row["pass"] != expected_pass:
        raise ValueError(
            f"{where}.pass contradicts its critic, blocking metric, or authoritative "
            "contract evidence"
        )
    if (row["decided_by"] == "metrics") != bool(metric_readings):
        raise ValueError(f"{where}.decided_by contradicts its blocking metric evidence")
    if row["decided_by"] == "no_optical_signal" and (
        row["critic_pass"] is not False or row["pass"] is not False
    ):
        raise ValueError(
            f"{where}.decided_by=no_optical_signal requires a failed non-critic verdict"
        )
    semantic = {
        "moment_id": _text(moment_id, f"{where}.moment_id"),
        "frame": row["frame"],
        "render_sha256": render_sha256,
        "reference_sha256": reference_sha256,
        "render_capture": _render_capture(row, render_sha256=render_sha256, where=where),
        "pass": row["pass"],
        "critic_pass": row["critic_pass"],
        "decided_by": row["decided_by"],
        "mean": row["mean"],
        "metric_readings": metric_readings,
        "scores": {axis: scores[axis] for axis in sorted(scores)},
        "contract_evidence": contract_evidence,
    }
    canonical_digest({"schema": "vfx-harness.acceptance-semantic-capture/v1", **semantic})
    return semantic
