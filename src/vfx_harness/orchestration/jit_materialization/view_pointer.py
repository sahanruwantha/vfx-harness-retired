"""Closed contract for the selected content-addressed JIT consumer view."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from vfx_harness.domain.authority_head_records import (
    JIT_VIEW_POINTER_SCHEMA,
    OVERLAY_ARTIFACTS,
)

VIEW_SCHEMA = JIT_VIEW_POINTER_SCHEMA

_POINTER_FIELDS = frozenset(
    {
        "schema",
        "revision",
        "plan_revision",
        "bundle_hash",
        "view_hash",
        "materialized_layers",
        "artifacts",
        "hashes",
    }
)


class JitViewPointerError(ValueError):
    """The selected-view pointer is not a producer-valid v2 record."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class JitViewPointer:
    """Validated v2 projection of ``state/jit-layers/current.json``."""

    revision: int
    plan_revision: int
    bundle_hash: str
    view_hash: str
    materialized_layers: tuple[str, ...]
    artifacts: dict[str, str]
    hashes: dict[str, str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": VIEW_SCHEMA,
            "revision": self.revision,
            "plan_revision": self.plan_revision,
            "bundle_hash": self.bundle_hash,
            "view_hash": self.view_hash,
            "materialized_layers": list(self.materialized_layers),
            "artifacts": dict(self.artifacts),
            "hashes": dict(self.hashes),
        }


def is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def canonical_view_hash(documents: Mapping[str, Any]) -> str:
    """Address a view by the exact final consumer documents."""

    payload = json.dumps(documents, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def parse_jit_view_pointer(value: object) -> JitViewPointer:
    """Validate the exact producer-owned v2 pointer shape and revisions."""

    if not isinstance(value, Mapping) or set(value) != _POINTER_FIELDS:
        raise JitViewPointerError(
            "shape",
            "selected JIT consumer view fields do not match the v2 producer schema",
        )
    if value.get("schema") != VIEW_SCHEMA:
        raise JitViewPointerError(
            "schema",
            "selected JIT consumer view has an unsupported schema",
        )
    revision = value.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision <= 0:
        raise JitViewPointerError(
            "revision",
            "selected JIT consumer view revision must be a positive integer",
        )
    plan_revision = value.get("plan_revision")
    if (
        not isinstance(plan_revision, int)
        or isinstance(plan_revision, bool)
        or plan_revision <= 0
    ):
        raise JitViewPointerError(
            "plan_revision",
            "selected JIT consumer view plan_revision must be a positive integer",
        )
    bundle_hash = value.get("bundle_hash")
    view_hash = value.get("view_hash")
    if not is_sha256(bundle_hash):
        raise JitViewPointerError(
            "bundle_digest",
            "selected JIT consumer view has no valid bundle_hash",
        )
    if not is_sha256(view_hash):
        raise JitViewPointerError(
            "view_digest",
            "selected JIT consumer view has no valid view_hash",
        )

    raw_layers = value.get("materialized_layers")
    if (
        not isinstance(raw_layers, list)
        or any(
            not isinstance(item, str) or not item or item != item.strip()
            for item in raw_layers
        )
        or raw_layers != sorted(set(raw_layers))
    ):
        raise JitViewPointerError(
            "materialized_layers",
            "selected JIT consumer view materialized_layers must be sorted unique ids",
        )

    raw_artifacts = value.get("artifacts")
    raw_hashes = value.get("hashes")
    expected_names = set(OVERLAY_ARTIFACTS)
    if not isinstance(raw_artifacts, Mapping) or set(raw_artifacts) != expected_names:
        raise JitViewPointerError(
            "artifacts",
            "selected JIT consumer view must pin every overlay artifact exactly once",
        )
    if not isinstance(raw_hashes, Mapping) or set(raw_hashes) != expected_names:
        raise JitViewPointerError(
            "hashes",
            "selected JIT consumer view must hash every overlay artifact exactly once",
        )

    artifacts: dict[str, str] = {}
    hashes: dict[str, str] = {}
    for name in OVERLAY_ARTIFACTS:
        relative = raw_artifacts[name]
        expected = raw_hashes[name]
        if not isinstance(relative, str) or not relative or relative != relative.strip():
            raise JitViewPointerError(
                "artifact_locator",
                f"selected JIT consumer view artifact {name!r} has no relative locator",
            )
        locator = PurePosixPath(relative)
        if locator.is_absolute() or ".." in locator.parts or locator.as_posix() != relative:
            raise JitViewPointerError(
                "artifact_locator",
                f"selected JIT consumer view artifact {name!r} has an unsafe locator",
            )
        if not is_sha256(expected):
            raise JitViewPointerError(
                "artifact_digest",
                f"selected JIT consumer view artifact {name!r} has no valid digest",
            )
        artifacts[name] = relative
        hashes[name] = expected

    return JitViewPointer(
        revision=revision,
        plan_revision=plan_revision,
        bundle_hash=str(bundle_hash),
        view_hash=str(view_hash),
        materialized_layers=tuple(raw_layers),
        artifacts=artifacts,
        hashes=hashes,
    )


def require_live_jit_artifact_locators(pointer: JitViewPointer) -> None:
    """Require the exact content-addressed producer paths of a selected live head.

    Candidate consumer views intentionally use local artifact names in their synthetic pointer.
    That pointer is parsed only inside the isolated evaluation boundary.  A live shot head has
    exactly one producer-owned store, so accepting another normalized in-shot path would let the
    pointer redirect verified hashes to an unowned alias.
    """

    root = PurePosixPath("state/jit-layers/views") / pointer.view_hash
    for name in OVERLAY_ARTIFACTS:
        expected = (root / name).as_posix()
        if pointer.artifacts[name] != expected:
            raise JitViewPointerError(
                "artifact_locator",
                f"selected live JIT artifact {name!r} must use exact producer locator "
                f"{expected!r}",
            )


def materialized_layers_from_document(value: object) -> tuple[str, ...]:
    """Derive the redundant pointer field from its pinned ``layers.json`` bytes."""

    if not isinstance(value, Mapping) or value.get("schema") not in {4, 5}:
        raise JitViewPointerError(
            "layers_document",
            "selected JIT consumer view layers.json must use a producer-supported schema",
        )
    rows = value.get("layers")
    if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
        raise JitViewPointerError(
            "layers_document",
            "selected JIT consumer view layers.json must contain layer objects",
        )
    layer_ids: list[str] = []
    for row in rows:
        layer_id = row.get("id")
        if not isinstance(layer_id, str) or not layer_id or layer_id != layer_id.strip():
            raise JitViewPointerError(
                "layers_document",
                "selected JIT consumer view layers.json contains an invalid layer id",
            )
        if row.get("execution") != "jit_deferred":
            layer_ids.append(layer_id)
    if len(layer_ids) != len(set(layer_ids)):
        raise JitViewPointerError(
            "layers_document",
            "selected JIT consumer view layers.json contains duplicate layer ids",
        )
    return tuple(sorted(layer_ids))


def require_materialized_layers_match(
    pointer: JitViewPointer,
    layers_document: object,
) -> None:
    derived = materialized_layers_from_document(layers_document)
    if pointer.materialized_layers != derived:
        raise JitViewPointerError(
            "materialized_layers_mismatch",
            "selected JIT consumer view materialized_layers disagree with layers.json",
        )
