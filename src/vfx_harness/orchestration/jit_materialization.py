"""Pinned materialization of one globally deferred layer.

The global bundle reserves dependency and semantic authority without inventing future
units.  A JIT materialization may replace exactly one ``jit_deferred`` layer with a full
ready DAG and add its contracts, but only after every promised contract is concretely
bound.  The resulting consumer view is content-addressed and pinned to the selected
global bundle; it never mutates that bundle.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vfx_harness.evidence.scene_checks import validate_row
from vfx_harness.observability.provenance import atomic_write
from vfx_harness.orchestration.ledger import Layer, load_layers_from_path

MATERIALIZATION_SCHEMA = "vfx-harness.jit-layer-materialization/v1"
VIEW_SCHEMA = "vfx-harness.jit-layer-view/v1"
STATE_DIR = Path("state/jit-layers")
CURRENT = STATE_DIR / "current.json"
OVERLAY_ARTIFACTS = ("layers.json", "scene_checks.json", "checks.json")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _document(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain an object")
    return value


def _rows(document: dict[str, Any], key: str, where: str) -> list[dict[str, Any]]:
    value = document.get(key)
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ValueError(f"{where}.{key} must be a list of objects")
    return value


def _matches_reserved(role: str, reserved: tuple[str, ...]) -> bool:
    return any(
        fnmatch.fnmatchcase(role, pattern) or fnmatch.fnmatchcase(pattern, role)
        for pattern in reserved
    )


def _passed_evidence_ids(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        identifier = value.get("id")
        if identifier and value.get("pass") is True:
            found.add(str(identifier))
        for child in value.values():
            found.update(_passed_evidence_ids(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_passed_evidence_ids(child))
    return found


def _require_upstream_outcomes(shot: Path, layer: Layer) -> None:
    if layer.jit is None:
        raise ValueError(f"layer {layer.id} has no deferred JIT authority")
    passed: set[str] = set()
    for dependency in layer.jit.depends_on_layers:
        path = shot / "plans" / "outcomes" / f"{int(dependency):02d}.json"
        try:
            outcome = _document(path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"layer {layer.id} cannot materialize before dependency {dependency} has a sealed outcome"
            ) from exc
        if outcome.get("status") != "passed" or str(outcome.get("layer")) != dependency:
            raise ValueError(
                f"layer {layer.id} dependency {dependency} does not have a passed sealed outcome"
            )
        passed.update(_passed_evidence_ids(outcome))
    missing = sorted(identifier for _kind, identifier in layer.jit.required_outcomes if identifier not in passed)
    if missing:
        raise ValueError(
            f"layer {layer.id} required upstream outcomes have not passed: " + ", ".join(missing)
        )


@dataclass(frozen=True, slots=True)
class MaterializedLayer:
    layer: Layer
    layer_row: dict[str, Any]
    scene_contracts: tuple[dict[str, Any], ...]
    image_contracts: tuple[dict[str, Any], ...]
    promise_bindings: dict[str, tuple[str, str]]


def validate_materialization(
    global_root: str | Path,
    materialization_path: str | Path,
    *,
    expected_bundle_hash: str,
    base_layers_path: str | Path | None = None,
) -> MaterializedLayer:
    """Validate one overlay without publishing or creating durable unit state."""
    root = Path(global_root)
    source = Path(materialization_path)
    payload = _document(source)
    if payload.get("schema") != MATERIALIZATION_SCHEMA:
        raise ValueError(f"{source} has unsupported JIT materialization schema")
    if payload.get("bundle_hash") != expected_bundle_hash:
        raise ValueError("JIT materialization is pinned to another global bundle")
    layer_row = payload.get("layer")
    if not isinstance(layer_row, dict):
        raise ValueError("JIT materialization.layer must be an object")
    layer_id = str(layer_row.get("id") or "")

    global_layers = _rows(_document(root / "layers.json"), "layers", "layers.json")
    by_id = {str(row.get("id")): row for row in global_layers}
    global_row = by_id.get(layer_id)
    if global_row is None or global_row.get("execution") != "jit_deferred":
        raise ValueError(f"layer {layer_id!r} is not selected jit_deferred authority")
    if layer_row.get("execution") not in {None, "ready"}:
        raise ValueError("materialized layer execution must be ready")
    layer_row = dict(layer_row)
    layer_row["execution"] = "ready"
    layer_row.pop("jit", None)
    structural = ("id", "script", "title", "primary_judge", "judge", "owns", "evidence_domains", "reads")
    changed = [key for key in structural if layer_row.get(key) != global_row.get(key)]
    if changed:
        raise ValueError(
            "JIT materialization changes global structural authority: " + ", ".join(changed)
        )

    base_layers = (
        _rows(_document(Path(base_layers_path)), "layers", "layers.json")
        if base_layers_path
        else global_layers
    )
    combined_layers = [layer_row if str(row.get("id")) == layer_id else row for row in base_layers]
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".layers.json", prefix=".jit-validate-", dir=root, delete=False
    ) as handle:
        temp_path = Path(handle.name)
        json.dump({"schema": 4, "layers": combined_layers}, handle)
    try:
        parsed = load_layers_from_path(temp_path)
    finally:
        temp_path.unlink(missing_ok=True)
    layer = parsed[layer_id]

    jit = global_row.get("jit") or {}
    reserved = tuple(map(str, jit.get("reserved_roles") or []))
    escaped = sorted({
        role
        for unit in layer.stages
        for role in unit.mutates.roles
        if not _matches_reserved(role, reserved)
    })
    if escaped:
        raise ValueError(
            "materialized roles escape global namespace reservations: " + ", ".join(escaped)
        )

    scene_rows = _rows(payload, "scene_contracts", "materialization")
    image_rows = _rows(payload, "image_contracts", "materialization")
    all_contracts = {
        str(row.get("id")): ("scene_contract", row) for row in scene_rows if row.get("id")
    }
    all_contracts.update({
        str(row.get("id")): ("image_contract", row) for row in image_rows if row.get("id")
    })
    if len(all_contracts) != len(scene_rows) + len(image_rows):
        raise ValueError("materialized contract ids must be present and unique")
    for row in scene_rows:
        error = validate_row(row)
        if error:
            raise ValueError(f"scene contract {row.get('id', '<missing>')}: {error}")
        if str(row.get("owner_layer") or row.get("activates_at") or "") != layer_id:
            raise ValueError(f"scene contract {row.get('id')} must be owned by layer {layer_id}")
    for row in image_rows:
        if str(row.get("owner_layer") or "") != layer_id:
            raise ValueError(f"image contract {row.get('id')} must be owned by layer {layer_id}")

    required_bindings = {
        (binding.kind, binding.id)
        for unit in layer.stages
        for claim in unit.evaluation.claims
        if claim.required
        for binding in claim.evidence
    }
    missing_claims = sorted(
        contract_id
        for contract_id, (kind, _row) in all_contracts.items()
        if (kind, contract_id) not in required_bindings
    )
    if missing_claims:
        raise ValueError(
            "materialized contracts lack required producing claims: " + ", ".join(missing_claims)
        )

    raw_bindings = payload.get("promise_bindings")
    if not isinstance(raw_bindings, list):
        raise ValueError("materialization.promise_bindings must be a list")
    promise_bindings: dict[str, tuple[str, str]] = {}
    for index, binding in enumerate(raw_bindings):
        if not isinstance(binding, dict):
            raise ValueError(f"promise_bindings[{index}] must be an object")
        promise_id = str(binding.get("promise_id") or "")
        contract_id = str(binding.get("contract_id") or "")
        kind = str(binding.get("kind") or "")
        if not promise_id or promise_id in promise_bindings:
            raise ValueError(f"promise_bindings[{index}].promise_id must be unique")
        if all_contracts.get(contract_id, (None,))[0] != kind:
            raise ValueError(f"promise {promise_id} names an absent or wrong-kind contract")
        promise_bindings[promise_id] = (kind, contract_id)

    promises = {str(row.get("id")): row for row in jit.get("promises") or []}
    missing_promises = sorted(set(promises) - set(promise_bindings))
    extra_promises = sorted(set(promise_bindings) - set(promises))
    if missing_promises or extra_promises:
        raise ValueError(
            "JIT promise bindings are incomplete"
            + (f"; missing {', '.join(missing_promises)}" if missing_promises else "")
            + (f"; unknown {', '.join(extra_promises)}" if extra_promises else "")
        )
    for promise_id, promise in promises.items():
        _kind, contract_id = promise_bindings[promise_id]
        _contract_kind, row = all_contracts[contract_id]
        if row.get("kind") != promise.get("contract_kind"):
            raise ValueError(f"promise {promise_id} materialized with wrong contract kind")
        row_moments = tuple(row.get("frames") or ([row.get("frame")] if row.get("frame") else []))
        if row_moments != tuple(promise.get("moments") or []):
            raise ValueError(f"promise {promise_id} materialized at wrong moments")

    return MaterializedLayer(
        layer,
        layer_row,
        tuple(scene_rows),
        tuple(image_rows),
        promise_bindings,
    )


def selected_view_artifact(shot_folder: str | Path, name: str, bundle_hash: str) -> Path | None:
    """Return a verified materialized consumer artifact when one is selected."""
    if name not in OVERLAY_ARTIFACTS:
        return None
    shot = Path(shot_folder)
    pointer = shot / CURRENT
    if not pointer.is_file():
        return None
    value = _document(pointer)
    if value.get("schema") != VIEW_SCHEMA or value.get("bundle_hash") != bundle_hash:
        raise ValueError("selected JIT layer view is stale or malformed")
    relative = (value.get("artifacts") or {}).get(name)
    expected = (value.get("hashes") or {}).get(name)
    if not isinstance(relative, str) or not isinstance(expected, str):
        raise ValueError(f"selected JIT layer view is missing {name}")
    path = shot / relative
    if not path.is_file() or _sha256(path) != expected:
        raise ValueError(f"selected JIT layer view artifact {name} is stale")
    return path


def publish_materialization(
    shot_folder: str | Path,
    materialization_path: str | Path,
) -> Path:
    """Validate and atomically select one cumulative materialized consumer view."""
    from vfx_harness.orchestration.plan_authority import artifact_path, resolve_current

    shot = Path(shot_folder).resolve()
    bundle = resolve_current(shot)
    global_layers = load_layers_from_path(bundle.root / "layers.json")
    payload = _document(Path(materialization_path))
    layer_id = str((payload.get("layer") or {}).get("id") or "")
    if layer_id not in global_layers:
        raise ValueError(f"JIT materialization names unknown layer {layer_id!r}")
    _require_upstream_outcomes(shot, global_layers[layer_id])
    base_layers = selected_view_artifact(shot, "layers.json", bundle.content_hash) or artifact_path(
        shot, "layers.json"
    )
    base_scene = selected_view_artifact(
        shot, "scene_checks.json", bundle.content_hash
    ) or artifact_path(shot, "scene_checks.json")
    base_checks = selected_view_artifact(shot, "checks.json", bundle.content_hash) or artifact_path(
        shot, "checks.json"
    )
    materialized = validate_materialization(
        bundle.root,
        materialization_path,
        expected_bundle_hash=bundle.content_hash,
        base_layers_path=base_layers,
    )

    layers_doc = _document(base_layers)
    layers_doc["layers"] = [
        materialized.layer_row if str(row.get("id")) == materialized.layer.id else row
        for row in _rows(layers_doc, "layers", "layers.json")
    ]
    scene_doc = _document(base_scene)
    scene_doc["contracts"] = [
        *_rows(scene_doc, "contracts", "scene_checks.json"),
        *materialized.scene_contracts,
    ]
    checks_doc = _document(base_checks)
    checks_doc["checks"] = [
        *_rows(checks_doc, "checks", "checks.json"),
        *materialized.image_contracts,
    ]
    digest_payload = json.dumps(
        {
            "bundle": bundle.content_hash,
            "layer": materialized.layer_row,
            "scene": materialized.scene_contracts,
            "image": materialized.image_contracts,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    view_hash = hashlib.sha256(digest_payload).hexdigest()
    view = shot / STATE_DIR / "views" / view_hash
    view.mkdir(parents=True, exist_ok=True)
    for name, document in (
        ("layers.json", layers_doc),
        ("scene_checks.json", scene_doc),
        ("checks.json", checks_doc),
    ):
        atomic_write(view / name, json.dumps(document, indent=2, sort_keys=True) + "\n")
    pointer = {
        "schema": VIEW_SCHEMA,
        "bundle_hash": bundle.content_hash,
        "view_hash": view_hash,
        "materialized_layers": sorted(
            str(row.get("id"))
            for row in layers_doc["layers"]
            if row.get("execution") != "jit_deferred"
        ),
        "artifacts": {
            name: (view / name).relative_to(shot).as_posix() for name in OVERLAY_ARTIFACTS
        },
        "hashes": {name: _sha256(view / name) for name in OVERLAY_ARTIFACTS},
    }
    atomic_write(shot / CURRENT, json.dumps(pointer, indent=2, sort_keys=True) + "\n")
    return shot / CURRENT
