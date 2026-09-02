"""Strict HIR-0172 manifest parsing for a root run-owner claim."""

from __future__ import annotations

import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from vfx_harness.domain.run_ids import require_run_id
from vfx_harness.domain.run_lifecycle_primitives import timestamp
from vfx_harness.domain.run_owner_claims import RUN_OWNER_CLAIM_LOCATOR, RUN_OWNER_FENCE_LOCATOR
from vfx_harness.domain.stop_envelope_primitives import canonical_digest, require_id, require_text

RUN_MANIFEST_SCHEMA = "vfx-harness.run/v2"
RUN_MANIFEST_LOCATOR = "manifest.json"
RUN_DISPATCH_SCHEMA = "vfx-harness.run-dispatch/v1"

_DIRECT_OWNER_COMMANDS = frozenset({"accept", "build", "plan", "reconcile", "render"})

_MANIFEST_FIELDS = frozenset(
    {
        "schema",
        "run_id",
        "shot_id",
        "started_at",
        "invocation",
        "layout",
        "authority",
        "reader_entrypoint",
    }
)
_MANIFEST_LAYOUT = {
    "status": "status.json",
    "artifact_index": "artifacts.json",
    "logs": "logs/",
    "reports": "reports/",
    "evidence": "evidence/",
    "checkpoints": "checkpoints/",
    "scratch": "scratch/",
    "deliverables": "deliverables/",
    "owner_claim": RUN_OWNER_CLAIM_LOCATOR,
    "owner_fence": RUN_OWNER_FENCE_LOCATOR,
}
RUN_MANIFEST_LAYOUT = MappingProxyType(dict(_MANIFEST_LAYOUT))
_AUTHORITY_FIELDS = frozenset(
    {
        "authored_inputs",
        "published_plan",
        "plan_authoring_workspace",
        "selected_plan_consumers",
        "accepted_build",
        "generated_output",
    }
)


class RunOwnerManifestError(ValueError):
    """The manifest cannot safely authorize a root owner claim."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    record: dict[str, Any] = {}
    for key, value in pairs:
        if key in record:
            raise RunOwnerManifestError(f"JSON object contains duplicate field {key!r}")
        record[key] = value
    return record


def strict_json_object(payload: bytes, *, where: str) -> dict[str, Any]:
    """Parse one UTF-8 JSON object while rejecting ambiguous duplicate keys."""

    try:
        text = payload.decode("utf-8", errors="strict")
        value = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunOwnerManifestError(f"{where} must be one strict JSON object") from exc
    if not isinstance(value, dict):
        raise RunOwnerManifestError(f"{where} must be one JSON object")
    return value


def _exact_fields(value: object, expected: frozenset[str], *, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RunOwnerManifestError(f"{where} must be one JSON object")
    found = set(value)
    if found != expected:
        raise RunOwnerManifestError(
            f"{where} fields mismatch; missing={sorted(expected - found)}; unexpected={sorted(found - expected)}"
        )
    return value


def _invocation_owner_identity(invocation: object) -> tuple[str, str]:
    row = _exact_fields(
        invocation,
        frozenset({"dispatch", "argv", "parameters"}),
        where="run manifest invocation",
    )
    dispatch = _exact_fields(
        row["dispatch"],
        frozenset({"schema", "kind", "command"}),
        where="run manifest invocation.dispatch",
    )
    if dispatch["schema"] != RUN_DISPATCH_SCHEMA:
        raise RunOwnerManifestError(f"run manifest invocation.dispatch.schema must be {RUN_DISPATCH_SCHEMA!r}")
    try:
        command = require_id(dispatch["command"], "run manifest invocation.dispatch.command")
    except ValueError as exc:
        raise RunOwnerManifestError(str(exc)) from exc
    kind = dispatch["kind"]
    if kind == "direct":
        if command not in _DIRECT_OWNER_COMMANDS:
            raise RunOwnerManifestError(
                f"direct run-owner dispatch command must be one of {sorted(_DIRECT_OWNER_COMMANDS)}; found {command!r}"
            )
        owner_kind = "direct"
    elif kind == "driver":
        if command != "run":
            raise RunOwnerManifestError("driver run-owner dispatch command must be 'run'")
        owner_kind = "driver"
    else:
        raise RunOwnerManifestError("run manifest invocation.dispatch.kind must be 'direct' or 'driver'")

    argv = row["argv"]
    parameters = row["parameters"]
    if (
        not isinstance(argv, list)
        or len(argv) < 2
        or any(not isinstance(item, str) or not item or item != item.strip() for item in argv)
    ):
        raise RunOwnerManifestError(
            "run manifest invocation.argv must be a list of at least two non-empty trimmed strings"
        )
    if not isinstance(parameters, dict):
        raise RunOwnerManifestError("run manifest invocation.parameters must be one JSON object")
    legacy_discriminants = sorted({"command", "direct"} & parameters.keys())
    if legacy_discriminants:
        raise RunOwnerManifestError(
            f"run manifest invocation.parameters cannot duplicate dispatch authority; found {legacy_discriminants}"
        )
    expected_program = f"vfx {command}"
    if argv[0] != expected_program:
        raise RunOwnerManifestError(
            f"run manifest public dispatch disagrees with argv[0]; expected {expected_program!r}, found {argv[0]!r}"
        )
    return command, owner_kind


@dataclass(frozen=True, slots=True)
class VerifiedRunOwnerManifest:
    """Exact manifest bytes and the root identity derived from their invocation."""

    payload: bytes
    invocation_digest: str
    run_id: str
    shot_id: str
    started_at: str
    command: str
    owner_kind: str


def parse_run_owner_manifest(payload: bytes, *, run_id: str) -> VerifiedRunOwnerManifest:
    """Parse the one closed v2 manifest generation accepted by HIR-0172."""

    try:
        require_run_id(run_id, "run owner manifest run_id")
    except ValueError as exc:
        raise RunOwnerManifestError(str(exc)) from exc
    record = _exact_fields(strict_json_object(payload, where="run manifest"), _MANIFEST_FIELDS, where="run manifest")
    if record["schema"] != RUN_MANIFEST_SCHEMA:
        raise RunOwnerManifestError(f"run manifest schema must be {RUN_MANIFEST_SCHEMA!r}, found {record['schema']!r}")
    if record["run_id"] != run_id:
        raise RunOwnerManifestError(f"run manifest names {record['run_id']!r}, expected {run_id!r}")
    try:
        shot_id = require_id(record["shot_id"], "run manifest shot_id")
        started_at = timestamp(record["started_at"], "run manifest started_at")
    except ValueError as exc:
        raise RunOwnerManifestError(str(exc)) from exc

    layout = _exact_fields(record["layout"], frozenset(_MANIFEST_LAYOUT), where="run manifest layout")
    if layout != _MANIFEST_LAYOUT:
        raise RunOwnerManifestError(f"run manifest layout must equal the closed {RUN_MANIFEST_SCHEMA} layout")
    authority = _exact_fields(record["authority"], _AUTHORITY_FIELDS, where="run manifest authority")
    try:
        for field, value in authority.items():
            require_text(value, f"run manifest authority.{field}")
    except ValueError as exc:
        raise RunOwnerManifestError(str(exc)) from exc
    if record["reader_entrypoint"] != RUN_MANIFEST_LOCATOR:
        raise RunOwnerManifestError("run manifest reader_entrypoint must be 'manifest.json'")

    invocation = record["invocation"]
    command, owner_kind = _invocation_owner_identity(invocation)
    try:
        invocation_digest = canonical_digest(invocation)
    except ValueError as exc:
        raise RunOwnerManifestError("run manifest invocation is not canonical JSON") from exc
    return VerifiedRunOwnerManifest(
        payload,
        invocation_digest,
        run_id,
        shot_id,
        started_at,
        command,
        owner_kind,
    )
