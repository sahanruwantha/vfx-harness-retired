"""Immutable critic rubric and separately recorded observation data."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from jsonschema import Draft202012Validator

OBSERVATION_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "candidate": {"type": "string"}, "reference": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "object"}},
        "focus_panels": {"type": "array", "maxItems": 2, "items": {"type": "object"}},
        "motion_frames": {"type": "array", "items": {"type": "integer", "minimum": 1}},
        "focus_frames": {"type": "array", "items": {"type": "integer", "minimum": 1}},
        "prior_mean": {"type": ["number", "null"]},
    },
}


AUTHORITY_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "target": {"type": "object", "additionalProperties": False, "properties": {
            "shot": {"type": "string"}, "stage": {"type": "string"},
            "frame": {"type": "integer", "minimum": 1}, "reads": {"type": "string"},
            "scope": {"type": ["string", "null"]},
        }, "required": ["shot", "stage", "frame", "reads", "scope"]},
        "claims": {"type": "array", "items": {"type": "object"}},
    },
}


def protocol_schema_digest() -> str:
    encoded = json.dumps({"authority": AUTHORITY_SCHEMA, "observation": OBSERVATION_SCHEMA}, sort_keys=True)
    return hashlib.sha256(encoded.encode()).hexdigest()


@dataclass(frozen=True)
class CriticPrompt:
    """Rubric is calibrated; observation bytes are verified independently per call.

    Empty observations are valid for transport/calibration instruments whose complete
    scope and images are supplied separately. Production supplies the exact target,
    measured evidence and auxiliary-image data. Neither surface grants acceptance.
    """

    rubric: str
    observation_json: str = "{}"
    authority_json: str = "{}"

    def __post_init__(self):
        if not isinstance(self.rubric, str) or not self.rubric.strip():
            raise ValueError("critic rubric must be nonempty text")
        for field, schema in (("observation_json", OBSERVATION_SCHEMA), ("authority_json", AUTHORITY_SCHEMA)):
            payload = getattr(self, field)
            if not isinstance(payload, str):
                raise ValueError(f"critic {field} requires JSON text")
            value = json.loads(payload)
            Draft202012Validator(schema).validate(value)
            encoded = json.dumps(value, sort_keys=True, allow_nan=False)
            object.__setattr__(self, field, encoded)

    def validate_images(self, images: tuple[tuple[str, str], ...]) -> None:
        """Observation metadata cannot label a different file as the scored source."""
        data = json.loads(self.observation_json)
        for role in ("reference", "candidate"):
            if role in data and [path for kind, path in images if kind == role] != [data[role]]:
                raise ValueError(f"critic observation {role} differs from its attached image")
        if "focus_panels" in data:
            paths = [row.get("image_rel") for row in data["focus_panels"]]
            if paths != [path for role, path in images if role == "focus"]:
                raise ValueError("critic observation focus panels differ from attached image order")
