"""Closed JSON schema for the materialization unit-ticket tool."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from vfx_harness.domain.construction import UNIT_CONSTRUCTION_ROUTES
from vfx_harness.domain.publish_interfaces import PUBLISH_INTERFACE_KINDS
from vfx_harness.domain.publish_interfaces import SCHEMA as PUB_SCHEMA
from vfx_harness.domain.work_units.capabilities import CAMERA_LAYER_DEFERS_SUBJECT_FORM_RULE, UNIT_PROVIDES
from vfx_harness.domain.work_units.evidence_domains import CLAIM_DOMAINS
from vfx_harness.domain.work_units.parsing import (
    CLAIM_AUTHORITIES,
    CLAIM_KINDS,
    EVIDENCE_KINDS,
    TEMPORAL_EVIDENCE,
    canonical_unit_script_path,
)
from vfx_harness.domain.work_units.unit import LOOK_CAPABILITIES


def compile_clustered_mutation_roles(value: Mapping[str, Any]) -> dict[str, Any]:
    """Compile the staging-only one-namespace role shape into a WorkUnit row."""
    unit = dict(value)
    raw_mutates = unit.get("mutates")
    if not isinstance(raw_mutates, Mapping):
        raise ValueError("unit.mutates must be an object")
    mutates = dict(raw_mutates)
    if "roles" in mutates:
        raise ValueError(
            "unit.mutates.roles is not accepted by clustered staging; choose one "
            "role_namespace and relative role_members"
        )
    namespace = str(mutates.pop("role_namespace", "") or "").strip()
    members = mutates.pop("role_members", None)
    if not isinstance(members, list):
        raise ValueError("unit.mutates.role_members must be a list")
    if members and not namespace:
        raise ValueError("non-empty role_members requires one role_namespace")
    if not members and namespace:
        raise ValueError("role_namespace must be omitted when role_members is empty")
    roles = [namespace if str(member) == "$self" else f"{namespace}.{member!s}" for member in members]
    if any(role != namespace and not role.startswith(f"{namespace}.") for role in roles):
        # Defense below the JSON schema for direct/non-SDK callers.
        raise ValueError(f"compiled mutation roles must stay in one namespace {namespace!r}: {roles}")
    mutates["roles"] = roles
    unit["mutates"] = mutates
    return unit


def work_unit_authoring_schema(
    *,
    image_property_kinds: Iterable[str] | None = None,
    axis_ids: Iterable[str] | None = None,
    layer_id: str | None = None,
    allowed_provides: Iterable[str] | None = None,
    clustered_mutation_roles: bool = False,
    stageable_authorities: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Closed JSON schema exposed by the materialization unit-ticket tool.

    This is an authoring instrument, not a second parser. ``WorkUnit.parse`` remains
    authoritative; the schema prevents the model from inventing field names before the
    typed parser and its cross-field validation run.
    """
    text = {"type": "string", "minLength": 1}
    positive_int = {"type": "integer", "minimum": 1}
    claim_axis = dict(text)
    if axis_ids is not None:
        claim_axis = {
            "type": "string",
            "enum": sorted({str(value) for value in axis_ids}),
            "description": "Exact axis owned by the active layer; do not invent prefixes.",
        }

    def strings(*, nonempty: bool = False) -> dict[str, Any]:
        row: dict[str, Any] = {
            "type": "array",
            "items": dict(text),
            "uniqueItems": True,
        }
        if nonempty:
            row["minItems"] = 1
        return row

    script_item = dict(text)
    if layer_id is not None:
        directory = canonical_unit_script_path(str(layer_id), "unit-id").rsplit("/", 1)[0]
        script_item.update(
            {
                "pattern": rf"^{re.escape(directory)}/[a-zA-Z0-9][a-zA-Z0-9_.-]*\.py$",
                "description": (
                    f"Exact unit artifact under {directory}/; the basename must equal "
                    "the ticket id plus .py. Fragment notation is invalid."
                ),
            }
        )
    else:
        script_item.update(
            {
                "pattern": r"^build/units/[a-zA-Z0-9][a-zA-Z0-9_.-]*/"
                r"[a-zA-Z0-9][a-zA-Z0-9_.-]*\.py$",
                "description": (
                    "One identity-derived build/units/<layer>/<unit-id>.py artifact; fragment notation is invalid."
                ),
            }
        )
    script_spans = {
        "type": "array",
        "items": script_item,
        "minItems": 1,
        "maxItems": 1,
        "uniqueItems": True,
    }

    judge = {
        "type": "object",
        "properties": {"frame": positive_int, "ref": text},
        "required": ["frame", "ref"],
        "additionalProperties": False,
    }
    evidence = {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": sorted(EVIDENCE_KINDS)},
            "id": text,
            "moments": {"type": "array", "items": positive_int, "minItems": 1, "uniqueItems": True},
        },
        "required": ["kind", "id"],
        "additionalProperties": False,
    }
    claim = {
        "type": "object",
        "properties": {
            "id": text,
            "proposition": text,
            "axis": claim_axis,
            "property": text,
            "subject_roles": strings(),
            "subject_controls": strings(),
            "moments": {"type": "array", "items": positive_int, "minItems": 1, "uniqueItems": True},
            "kind": {"type": "string", "enum": sorted(CLAIM_KINDS)},
            "required": {"type": "boolean"},
            "authority": {
                "type": "string",
                "enum": sorted(
                    CLAIM_AUTHORITIES
                    if stageable_authorities is None
                    else {str(value) for value in stageable_authorities} & CLAIM_AUTHORITIES
                ),
                "description": (
                    "qualified_qualitative_required is harness-minted judgment authority: the "
                    "composed judgment unit binds it for approved_start / planner_start judgment "
                    "debt, and no qualification suite registers a rubric artifact an authored "
                    "claim could cite. Appearance judged at build time is executable_required "
                    "with asserts image, or a judgment debt on the owning requirement."
                ),
            },
            "repair_owner": text,
            "asserts": {"type": "string", "enum": sorted(CLAIM_DOMAINS)},
            "evidence": {"type": "array", "items": evidence, "minItems": 1},
            "qualification": {"type": "object"},
            "coordination_owner": {
                **text,
                "description": (
                    "Required only for interaction claims: exact same-layer work-unit id that owns bounded balancing."
                ),
            },
            "participants": {
                **strings(),
                "description": (
                    "Required only for interaction claims: at least two exact same-layer "
                    "work-unit ids, never semantic roles or controls."
                ),
            },
            "controls": {
                **strings(),
                "description": (
                    "Required only for interaction claims: non-empty bounded control ids "
                    "available to the coordination owner."
                ),
            },
        },
        "required": [
            "id",
            "proposition",
            "axis",
            "property",
            "subject_roles",
            "subject_controls",
            "moments",
            "kind",
            "required",
            "authority",
            "repair_owner",
            "asserts",
            "evidence",
        ],
        "additionalProperties": False,
    }
    claim["allOf"] = [
        {
            "if": {
                "properties": {"kind": {"const": "interaction"}},
                "required": ["kind"],
            },
            "then": {
                "required": ["coordination_owner", "participants", "controls"],
                "properties": {
                    "participants": {"minItems": 2},
                    "controls": {"minItems": 1},
                },
            },
            "else": {
                "not": {
                    "anyOf": [
                        {"required": ["coordination_owner"]},
                        {"required": ["participants"]},
                        {"required": ["controls"]},
                    ]
                }
            },
        }
    ]
    if image_property_kinds is not None:
        payable = sorted({str(value) for value in image_property_kinds})
        claim["allOf"].append(
            {
                "if": {
                    "properties": {"asserts": {"const": "image"}},
                    "required": ["asserts"],
                },
                "then": {
                    "properties": {
                        "property": {
                            "type": "string",
                            "enum": payable,
                            "description": (
                                "Executable image property. Put free-form appearance language in proposition."
                            ),
                        }
                    }
                },
            }
        )
    composition = {
        "type": "object",
        "description": (
            "Optional. Omit when required claims directly bind all evidence. When present, "
            "frames is non-empty and exactly one of source_unit or non-empty contract_ids is set."
        ),
        "properties": {
            "frames": {"type": "array", "items": positive_int, "minItems": 1, "uniqueItems": True},
            "source_unit": text,
            "contract_ids": strings(nonempty=True),
        },
        "required": ["frames"],
        "oneOf": [
            {"required": ["source_unit"], "not": {"required": ["contract_ids"]}},
            {"required": ["contract_ids"], "not": {"required": ["source_unit"]}},
        ],
        "additionalProperties": False,
    }
    publish = {
        "type": "object",
        "properties": {
            "id": text,
            "kind": {"type": "string", "enum": sorted(PUBLISH_INTERFACE_KINDS)},
            "schema": {"type": "string", "enum": [PUB_SCHEMA]},
            "exports": {
                "type": "object",
                "additionalProperties": dict(text),
                "minProperties": 1,
            },
        },
        "required": ["id", "kind", "exports"],
        "additionalProperties": False,
    }
    consume = {
        "type": "object",
        "description": "Exact read-only predecessor interface; producer must be in depends_on.",
        "properties": {
            "producer": text,
            "interface_id": text,
            "kind": {"type": "string", "enum": sorted(PUBLISH_INTERFACE_KINDS)},
        },
        "required": ["producer", "interface_id", "kind"],
        "additionalProperties": False,
    }
    provides_vocabulary = sorted(
        UNIT_PROVIDES if allowed_provides is None else {str(value) for value in allowed_provides}
    )

    mutation_properties = {
        "mode": {"type": "string", "enum": ["scoped", "none"]},
        "roles": strings(),
        "controls": strings(),
        "control_roles": {"type": "object", "additionalProperties": strings(nonempty=True)},
        "dresses": strings(),
        "script_spans": script_spans,
    }
    mutation_required = ["mode", "roles", "controls", "control_roles", "script_spans"]
    mutation_all_of: list[dict[str, Any]] = []
    if clustered_mutation_roles:
        # The model chooses one derived two-token namespace, then only relative members.
        # It cannot put building.mass and building.roof into one list because no field
        # accepts a second absolute namespace (HIR-0150).
        mutation_properties.pop("roles")
        mutation_properties.update(
            {
                "role_namespace": {
                    "type": "string",
                    "pattern": r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$",
                    "description": (
                        "The unit's one derived two-token write namespace, e.g. "
                        "building.mass. Every role member is relative to this namespace."
                    ),
                },
                "role_members": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "pattern": r"^(?:\$self|[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*)$",
                    },
                    "uniqueItems": True,
                    "description": (
                        "Relative role suffixes inside role_namespace; use $self for the "
                        "namespace tag itself. Absolute roles are not accepted."
                    ),
                },
            }
        )
        mutation_required = ["mode", "role_members", "controls", "control_roles", "script_spans"]
        mutation_all_of = [
            {
                "if": {"properties": {"role_members": {"minItems": 1}}},
                "then": {"required": ["role_namespace"]},
                "else": {"not": {"required": ["role_namespace"]}},
            }
        ]

    return {
        "type": "object",
        "properties": {
            "id": text,
            "title": text,
            "plan": text,
            "depends_on": strings(),
            "publishes": {"type": "array", "items": publish, "minItems": 1},
            "consumes": {"type": "array", "items": consume, "minItems": 1},
            "mutates": {
                "type": "object",
                "properties": mutation_properties,
                "required": mutation_required,
                **({"allOf": mutation_all_of} if mutation_all_of else {}),
                "additionalProperties": False,
            },
            "protects": {
                "type": "object",
                "properties": {
                    "selector": text,
                    "ids": strings(nonempty=True),
                    "resolve_to_explicit_ids_at": {"type": "string", "enum": ["freeze"]},
                },
                "required": ["resolve_to_explicit_ids_at"],
                "oneOf": [
                    {"required": ["selector"], "not": {"required": ["ids"]}},
                    {"required": ["ids"], "not": {"required": ["selector"]}},
                ],
                "additionalProperties": False,
            },
            "look_capabilities": {
                "type": "array",
                "items": {"type": "string", "enum": sorted(LOOK_CAPABILITIES)},
                "uniqueItems": True,
            },
            "provides": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": provides_vocabulary,
                    "description": CAMERA_LAYER_DEFERS_SUBJECT_FORM_RULE,
                },
                "uniqueItems": True,
            },
            "evaluation": {
                "type": "object",
                "properties": {
                    "primary_judge": positive_int,
                    "judge": {"type": "array", "items": judge, "minItems": 1},
                    "temporal_evidence": {"type": "string", "enum": sorted(TEMPORAL_EVIDENCE)},
                    "claims": {"type": "array", "items": claim, "minItems": 1},
                    "composition_context": composition,
                },
                "required": ["primary_judge", "judge", "temporal_evidence", "claims"],
                "additionalProperties": False,
            },
            "completion": text,
            "construction": {
                "type": "object",
                "description": (
                    "Optional construction route. Omit for procedural mesh. generate "
                    "and retrieve require a mesh write family; generate requires "
                    "refobs-* witnesses. omit and abstain are not unit routes."
                ),
                "properties": {
                    "route": {
                        "type": "string",
                        "enum": sorted(UNIT_CONSTRUCTION_ROUTES),
                    },
                    "witnesses": {
                        "type": "array",
                        "items": dict(text),
                        "uniqueItems": True,
                    },
                    "reason": {"type": "string"},
                },
                "required": ["route"],
                "additionalProperties": False,
                "allOf": [
                    {
                        "if": {
                            "properties": {"route": {"const": "generate"}},
                            "required": ["route"],
                        },
                        "then": {
                            "required": ["witnesses"],
                            "properties": {
                                "witnesses": {
                                    "type": "array",
                                    "minItems": 1,
                                    "uniqueItems": True,
                                    "items": {
                                        "type": "string",
                                        "pattern": r"^refobs-[A-Za-z0-9]+$",
                                    },
                                }
                            },
                        },
                    },
                    {
                        "if": {
                            "properties": {"route": {"const": "retrieve"}},
                            "required": ["route"],
                        },
                        "then": {
                            "required": ["witnesses"],
                            "properties": {
                                "witnesses": {
                                    "type": "array",
                                    "minItems": 1,
                                    "uniqueItems": True,
                                }
                            },
                        },
                    },
                ],
            },
        },
        "required": [
            "id",
            "title",
            "plan",
            "depends_on",
            "mutates",
            "protects",
            "look_capabilities",
            "provides",
            "evaluation",
            "completion",
        ],
        "additionalProperties": False,
    }
