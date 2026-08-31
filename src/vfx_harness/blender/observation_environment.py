"""Canonical, hashable inputs for a Blender qualitative observation.

This module deliberately has no Blender dependency.  The worker builds the typed scene
snapshot; callers share the request validation and canonical JSON/digest boundary.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from typing import Any

SCHEMA = "vfx-harness.canonical-observation-environment/v1"
OBSERVATION_MEDIA = frozenset({"eevee", "workbench_solid"})
CARRIER_FAMILIES = frozenset({"mesh", "volume", "compositor"})


def validate_observation_request(
    frame: int,
    subject_roles: Sequence[str],
    observation_medium: str,
    carrier_families: Sequence[str],
) -> tuple[int, tuple[str, ...], str, tuple[str, ...]]:
    """Validate the typed observation selector before contacting Blender."""
    if isinstance(frame, bool) or not isinstance(frame, int):
        raise ValueError("canonical observation frame must be an integer")
    if isinstance(subject_roles, (str, bytes)) or not isinstance(subject_roles, Sequence):
        raise ValueError("canonical observation subject_roles must be a non-empty role list")
    roles = tuple(str(role).strip() for role in subject_roles)
    if not roles or any(not role for role in roles):
        raise ValueError("canonical observation subject_roles must be a non-empty role list")
    if len(set(roles)) != len(roles):
        raise ValueError("canonical observation subject_roles must not repeat a selector")
    if any("," in role or any(character.isspace() for character in role) for role in roles):
        raise ValueError(
            "canonical observation subject_roles must contain one semantic selector per item"
        )
    medium = str(observation_medium or "").strip()
    if medium not in OBSERVATION_MEDIA:
        raise ValueError("canonical observation medium must be 'eevee' or 'workbench_solid'")
    if isinstance(carrier_families, (str, bytes)) or not isinstance(carrier_families, Sequence):
        raise ValueError("canonical observation carrier_families must be a non-empty list")
    families = tuple(str(family).strip() for family in carrier_families)
    if not families or any(not family for family in families):
        raise ValueError("canonical observation carrier_families must be a non-empty list")
    if len(set(families)) != len(families):
        raise ValueError("canonical observation carrier_families must not repeat a family")
    unsupported = sorted(set(families) - CARRIER_FAMILIES)
    if unsupported:
        raise ValueError(
            "canonical observation carrier_families must use only mesh, volume, compositor; "
            f"got {', '.join(unsupported)}"
        )
    return frame, roles, medium, tuple(sorted(families))


def canonical_observation_environment(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Return immutable JSON and its digest for one already-typed worker snapshot."""
    if not isinstance(snapshot, Mapping):
        raise ValueError("canonical observation snapshot must be an object")
    payload = dict(snapshot)
    if payload.get("schema") != SCHEMA:
        raise ValueError(f"canonical observation snapshot must declare schema {SCHEMA!r}")
    try:
        encoded = json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("canonical observation snapshot contains non-JSON or non-finite data") from exc
    if not math.isfinite(float(payload.get("frame", float("nan")))):
        raise ValueError("canonical observation snapshot frame must be finite")
    return {
        "schema": SCHEMA,
        "snapshot": json.loads(encoded),
        "canonical_json": encoded.decode("utf-8"),
        "digest": hashlib.sha256(encoded).hexdigest(),
    }
