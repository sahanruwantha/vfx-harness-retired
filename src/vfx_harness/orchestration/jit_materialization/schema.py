"""Pinned materialization of one globally deferred layer.

The global bundle reserves dependency and semantic authority without inventing future
units.  A JIT materialization may replace exactly one ``jit_deferred`` layer with a full
ready DAG and add its contracts, but only after every promised contract is concretely
bound.  The resulting consumer view is content-addressed and pinned to the selected
global bundle; it never mutates that bundle.
"""

from __future__ import annotations

import fcntl
import fnmatch
import hashlib
import json
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vfx_harness.domain.layer_outcomes import (
    LayerOutcomeContractError,
    parse_sealed_layer_outcome,
)
from vfx_harness.observability.provenance import atomic_write
from vfx_harness.orchestration.layer_outcome_paths import layer_outcome_path
from vfx_harness.orchestration.ledger import Layer

MATERIALIZATION_SCHEMA = "vfx-harness.jit-layer-materialization/v2"
VIEW_SCHEMA = "vfx-harness.jit-layer-view/v1"
STATE_DIR = Path("state/jit-layers")
CURRENT = STATE_DIR / "current.json"
OVERLAY_ARTIFACTS = (
    "layers.json",
    "scene_checks.json",
    "checks.json",
    "requirements.json",
    "acceptance.json",
)
ROLE_SELECTOR_CLOSURE_RULE = (
    "required evidence and repair authority must close together: bind the contract "
    "on the unit that mutates or dresses those roles, or use typed control_roles/"
    "compare_control_roles for bvfx_control ids. A mutation-empty observer cannot "
    "pay bbox or other role selectors it does not own"
)
TWO_SIDED_MEASUREMENT_KINDS = frozenset({
    "path_clearance_min",
    "parallax_displacement_profile",
    "onset_order",
})


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _document(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain an object")
    return value


class MaterializationRevisionConflict(ValueError):
    """The candidate changed after the caller observed it."""


@dataclass(frozen=True, slots=True)
class UnstagedMaterializationUnit:
    """One revision-checked removal from unpublished materialization scratch."""

    unit_id: str
    removed_contract_ids: tuple[str, ...]
    removed_requirement_ids: tuple[str, ...]


FINALIZATION_SCHEMA = "vfx-harness.materialization-finalization/v1"


def materialization_candidate_revision(path: str | Path) -> str:
    """Return the byte revision used by candidate compare-and-swap writes."""
    return _sha256(Path(path))


def materialization_finalization_path(path: str | Path) -> Path:
    candidate = Path(path)
    return candidate.with_name(candidate.name + ".finalization.json")


def attest_materialization_finalization(
    path: str | Path,
    *,
    bundle_hash: str,
) -> Path:
    """Bind an explicit successful finalize call to the exact candidate revision."""
    candidate = Path(path)
    attestation = materialization_finalization_path(candidate)
    atomic_write(
        attestation,
        json.dumps(
            {
                "schema": FINALIZATION_SCHEMA,
                "bundle_hash": str(bundle_hash),
                "candidate_revision": materialization_candidate_revision(candidate),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )
    return attestation


def materialization_finalization_attested(
    path: str | Path,
    *,
    bundle_hash: str,
) -> bool:
    """True only for the current bytes explicitly accepted by finalization."""
    candidate = Path(path)
    attestation = materialization_finalization_path(candidate)
    if not candidate.is_file() or not attestation.is_file():
        return False
    try:
        payload = json.loads(attestation.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return (
        payload.get("schema") == FINALIZATION_SCHEMA
        and payload.get("bundle_hash") == str(bundle_hash)
        and payload.get("candidate_revision")
        == materialization_candidate_revision(candidate)
    )


@contextmanager
def _materialization_candidate_lock(path: Path) -> Iterator[None]:
    """Serialize candidate read/validate/write across sessions and processes."""
    lock_path = path.with_name(f"{path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _mutate_materialization_candidate(
    path: Path,
    mutate: Callable[[dict[str, Any]], None],
    *,
    expected_revision: str | None,
) -> tuple[dict[str, Any], str]:
    """Run one locked compare-and-swap candidate mutation."""
    with _materialization_candidate_lock(path):
        raw = path.read_bytes()
        actual_revision = hashlib.sha256(raw).hexdigest()
        if expected_revision is not None and expected_revision != actual_revision:
            raise MaterializationRevisionConflict(
                "materialization candidate revision changed: expected "
                f"{expected_revision}, found {actual_revision}; inspect status and restart "
                "the materialization session from the current candidate"
            )
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError(f"{path} must contain an object")
        mutate(payload)
        encoded = (json.dumps(payload, indent=1) + "\n").encode("utf-8")
        # Tests patch the package attribute; look it up at call time.
        import vfx_harness.orchestration.jit_materialization as package  # noqa: PLC0415

        package.atomic_write(path, encoded.decode("utf-8"))
        return payload, hashlib.sha256(encoded).hexdigest()


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
    """Compatibility projection over the strict sealed-outcome evidence locations."""

    if not isinstance(value, dict):
        return set()
    layer_id = value.get("layer")
    if not isinstance(layer_id, str):
        return set()
    try:
        outcome = parse_sealed_layer_outcome(value, expected_layer_id=layer_id)
    except LayerOutcomeContractError:
        return set()
    return {identifier for _kind, identifier in outcome.passed_bindings}


def _require_upstream_outcomes(
    shot: Path,
    layer: Layer,
    available_layers: Mapping[str, Layer],
) -> None:
    if layer.jit is None:
        raise ValueError(f"layer {layer.id} has no deferred JIT authority")
    # Imported at the call boundary to avoid making the JIT schema module part of
    # the layer-plans/revalidation import cycle.
    from vfx_harness.orchestration.revalidation import (  # noqa: PLC0415
        current_outcome_eligibility,
    )

    passed: set[tuple[str, str]] = set()
    for dependency in layer.jit.depends_on_layers:
        dependency_layer = available_layers.get(dependency)
        if dependency_layer is None:
            raise ValueError(
                f"layer {layer.id} dependency {dependency} is absent from the "
                "selected executable consumer view"
            )
        path = layer_outcome_path(shot, dependency)
        try:
            outcome = _document(path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"layer {layer.id} cannot materialize before dependency {dependency} has a sealed outcome"
            ) from exc
        try:
            sealed = parse_sealed_layer_outcome(
                outcome,
                expected_layer_id=dependency,
            )
        except LayerOutcomeContractError as exc:
            raise ValueError(
                f"layer {layer.id} dependency {dependency} has an invalid sealed outcome: "
                f"{exc}"
            ) from exc
        if sealed.status != "passed":
            raise ValueError(
                f"layer {layer.id} dependency {dependency} does not have a passed sealed outcome"
            )
        eligible, reasons = current_outcome_eligibility(
            shot,
            dependency_layer,
            outcome,
        )
        if not eligible:
            raise ValueError(
                f"layer {layer.id} dependency {dependency} sealed outcome is stale: "
                + "; ".join(reasons)
            )
        passed.update(sealed.passed_bindings)
    missing = sorted(
        f"{kind}:{identifier}"
        for kind, identifier in layer.jit.required_outcomes
        if (kind, identifier) not in passed
    )
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
    requirement_bindings: dict[str, tuple[str, ...]]
    requirement_decisions: dict[str, dict[str, str]]
    requirement_evidence_domains: dict[str, tuple[str, ...]]
    requirement_domain_bindings: dict[str, tuple[dict[str, Any], ...]]
    judgment_debt_definitions: tuple[dict[str, Any], ...]
    judgment_debt_activations: tuple[dict[str, Any], ...]
    acceptance: tuple[dict[str, Any], ...]
