"""Closed critic response contract, independent of model transport."""

from __future__ import annotations


def critic_verdict_schema(
    axes: list[tuple[str, str]],
    *,
    allow_na: bool = True,
    focus_frames: list[int] | None = None,
) -> dict:
    """Force the verdict shape instead of regex-scraping the last {...} out of prose.
    Layer builds pass only their owned axes, so scope is no longer a model decision there.
    Full-rubric/acceptance calls may still need n/a for beat-specific axes."""
    numeric = {"type": "integer", "minimum": 0, "maximum": 5}
    score = {"anyOf": [numeric, {"type": "string", "enum": ["n/a"]}]} if allow_na else numeric
    return {
        "type": "object",
        "properties": {
            "scores": {
                "type": "object",
                "properties": {k: score for k, _ in axes},
                "required": [k for k, _ in axes],
                "additionalProperties": False,
            },
            "observations": {
                "type": "array",
                "maxItems": 4,
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "kind": {"type": "string", "enum": ["qualitative", "measurable"]},
                        "axis": {"type": "string", "enum": [k for k, _ in axes]},
                        "property": {"type": "string"},
                        "observation": {"type": "string"},
                        "action": {"type": "string"},
                        "moment": {
                            "type": "integer",
                            **({"enum": sorted(set(focus_frames))} if focus_frames else {"minimum": 1}),
                        },
                        "roles": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 1,
                        },
                        "claim_id": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                        "check_ids": {"type": "array", "items": {"type": "string"}},
                        "panel_ids": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": [
                        "id",
                        "kind",
                        "axis",
                        "property",
                        "observation",
                        "action",
                        "moment",
                        "roles",
                        "claim_id",
                        "check_ids",
                        "panel_ids",
                    ],
                    "additionalProperties": False,
                },
                "description": "One typed observation for each axis scored below 3. "
                "Bind planned defects to one exact claim and its evidence ids. Use a null "
                "claim_id only for a coverage defect absent from the supplied claim manifest.",
            },
            "focus_requests": {
                "type": "array",
                "maxItems": 2,
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "short stable id"},
                        "axis": {"type": "string", "enum": [k for k, _ in axes]},
                        "source": {
                            "type": "string",
                            "enum": ["candidate_frame", "motion_strip"],
                            "description": "coordinate space used by region",
                        },
                        "source_frame": {
                            "type": "integer",
                            **({"enum": sorted(set(focus_frames))} if focus_frames else {"minimum": 1}),
                            "description": "shot frame whose detail must be rerendered",
                        },
                        "region": {
                            "type": "array",
                            "items": {"type": "number", "minimum": 0, "maximum": 1},
                            "minItems": 4,
                            "maxItems": 4,
                        },
                        "reason": {"type": "string"},
                    },
                    "required": ["id", "axis", "source", "source_frame", "region", "reason"],
                    "additionalProperties": False,
                },
                "description": "At most two normalized TOP-LEFT regions needed to resolve "
                "a material below-3/uncertain visual decision. source=candidate_frame "
                "uses coordinates local to source_frame; source=motion_strip uses global "
                "strip coordinates and must remain inside that frame's one panel. Empty "
                "when the supplied images are sufficient.",
            },
            # Asked EXPLICITLY because the critic will otherwise mention a bad reference
            # in `issues` and score anyway: handed a render of a night city against a
            # green meadow, it wrote "cannot be the shot's look reference" and returned
            # camera_framing=4, PASS — silently grading the frame against the brief's
            # prose instead of an image. Ref-relative scoring is the whole premise, so
            # this has to be a first-class field, not a remark.
            "reference_usable": {
                "type": "boolean",
                "description": "false if the REFERENCE image is not a plausible target "
                "for this candidate at all (wrong shot, wrong beat, "
                "corrupt, blank). Absent-by-design content that a LATER "
                "layer adds does NOT make a reference unusable.",
            },
            "reference_note": {"type": "string", "description": "one line; required when unusable"},
        },
        "required": ["scores", "observations", "focus_requests", "reference_usable", "reference_note"],
        "additionalProperties": False,
    }

