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
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vfx_harness.domain.authority_head_records import (
    JIT_CURRENT_PATH,
    JIT_STATE_DIR,
    JIT_VIEW_POINTER_SCHEMA,
    canonical_json_bytes,
    decode_canonical_json_object,
)
from vfx_harness.domain.layer_outcomes import (
    LayerOutcomeContractError,
    parse_sealed_layer_outcome,
)
from vfx_harness.orchestration.authority_selection_transaction import (
    AuthoritySelectionConflict,
    AuthoritySelectionToken,
    durable_replace_pointer_bytes,
)
from vfx_harness.orchestration.layer_outcome_paths import layer_outcome_path
from vfx_harness.orchestration.ledger import Layer
from vfx_harness.orchestration.plan_consumer_view import OVERLAY_ARTIFACTS

MATERIALIZATION_SCHEMA = "vfx-harness.jit-layer-materialization/v3"
VIEW_SCHEMA = JIT_VIEW_POINTER_SCHEMA
STATE_DIR = JIT_STATE_DIR
CURRENT = JIT_CURRENT_PATH
ROLE_SELECTOR_CLOSURE_RULE = (
    "required evidence and repair authority must close together: bind the contract "
    "on the unit that mutates or dresses those roles, or use typed control_roles/"
    "compare_control_roles for bvfx_control ids. A mutation-empty observer cannot "
    "pay bbox or other role selectors it does not own"
)
TWO_SIDED_MEASUREMENT_KINDS = frozenset(
    {
        "path_clearance_min",
        "parallax_displacement_profile",
        "onset_order",
    }
)
MATERIALIZATION_FIELDS = frozenset(
    {
        "schema",
        "bundle_hash",
        "base_selection",
        "layer",
        "scene_contracts",
        "image_contracts",
        "requirement_bindings",
        "acceptance",
    }
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _document(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain an object")
    return value


def materialization_base_selection(value: Mapping[str, Any]) -> AuthoritySelectionToken:
    """Parse the exact selection from which one v3 candidate was designed."""

    try:
        return AuthoritySelectionToken.from_dict(
            value.get("base_selection"),
            "materialization candidate.base_selection",
        )
    except AuthoritySelectionConflict as exc:
        raise ValueError(str(exc)) from exc


class MaterializationRevisionConflict(ValueError):
    """The candidate changed after the caller observed it."""


@dataclass(frozen=True, slots=True)
class UnstagedMaterializationUnit:
    """One revision-checked removal from unpublished materialization scratch."""

    unit_id: str
    removed_contract_ids: tuple[str, ...]
    removed_requirement_ids: tuple[str, ...]


FINALIZATION_SCHEMA = "vfx-harness.materialization-finalization/v3"
FINALIZATION_GATE_POLICY = {
    "gate_schema": "vfx-harness.plan-gate/v1",
    "policy": "structural-authority/runtime-falsification-v1",
    "validation_scope": "structural_authority",
    "require_scene_checks": False,
}
_FINALIZATION_FIELDS = frozenset(
    {
        "schema",
        "base_selection",
        "bundle_hash",
        "candidate_revision",
        "proposed_view_hash",
        "proposed_artifact_hashes",
        "planning_inputs_digest",
        "consumer_marker_sha256",
        "gate_policy",
    }
)


def _require_digest(value: Any, where: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{where} must be a lowercase SHA-256 digest")
    return value


@dataclass(frozen=True, slots=True)
class MaterializationFinalization:
    """Exact gated candidate/view identity authorized for one selection CAS."""

    base_selection: AuthoritySelectionToken
    bundle_hash: str
    candidate_revision: str
    proposed_view_hash: str
    proposed_artifact_hashes: dict[str, str]
    planning_inputs_digest: str
    consumer_marker_sha256: str

    @classmethod
    def from_dict(cls, value: Any) -> MaterializationFinalization:
        if not isinstance(value, Mapping) or set(value) != _FINALIZATION_FIELDS:
            raise ValueError("materialization finalization fields do not match the v3 schema")
        if value.get("schema") != FINALIZATION_SCHEMA:
            raise ValueError("materialization finalization schema is unsupported")
        try:
            base_selection = AuthoritySelectionToken.from_dict(
                value.get("base_selection"),
                "materialization finalization.base_selection",
            )
        except AuthoritySelectionConflict as exc:
            raise ValueError(str(exc)) from exc
        hashes = value.get("proposed_artifact_hashes")
        if not isinstance(hashes, Mapping) or set(hashes) != set(OVERLAY_ARTIFACTS):
            raise ValueError("materialization finalization must hash every proposed artifact exactly once")
        parsed_hashes = {
            name: _require_digest(
                hashes[name],
                f"materialization finalization.proposed_artifact_hashes[{name!r}]",
            )
            for name in OVERLAY_ARTIFACTS
        }
        if value.get("gate_policy") != FINALIZATION_GATE_POLICY:
            raise ValueError("materialization finalization gate policy is unsupported")
        return cls(
            base_selection=base_selection,
            bundle_hash=_require_digest(
                value.get("bundle_hash"),
                "materialization finalization.bundle_hash",
            ),
            candidate_revision=_require_digest(
                value.get("candidate_revision"),
                "materialization finalization.candidate_revision",
            ),
            proposed_view_hash=_require_digest(
                value.get("proposed_view_hash"),
                "materialization finalization.proposed_view_hash",
            ),
            proposed_artifact_hashes=parsed_hashes,
            planning_inputs_digest=_require_digest(
                value.get("planning_inputs_digest"),
                "materialization finalization.planning_inputs_digest",
            ),
            consumer_marker_sha256=_require_digest(
                value.get("consumer_marker_sha256"),
                "materialization finalization.consumer_marker_sha256",
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": FINALIZATION_SCHEMA,
            "base_selection": self.base_selection.to_dict(),
            "bundle_hash": self.bundle_hash,
            "candidate_revision": self.candidate_revision,
            "proposed_view_hash": self.proposed_view_hash,
            "proposed_artifact_hashes": dict(self.proposed_artifact_hashes),
            "planning_inputs_digest": self.planning_inputs_digest,
            "consumer_marker_sha256": self.consumer_marker_sha256,
            "gate_policy": dict(FINALIZATION_GATE_POLICY),
        }


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
    base_selection: AuthoritySelectionToken,
    proposed_view_hash: str,
    proposed_artifact_hashes: Mapping[str, str],
    planning_inputs_digest: str,
    consumer_marker_sha256: str,
) -> Path:
    """Bind one clean gate result to its exact candidate, base, and proposed view."""
    candidate = Path(path)
    attestation = materialization_finalization_path(candidate)
    record = MaterializationFinalization.from_dict(
        {
            "schema": FINALIZATION_SCHEMA,
            "base_selection": base_selection.to_dict(),
            "bundle_hash": str(bundle_hash),
            "candidate_revision": materialization_candidate_revision(candidate),
            "proposed_view_hash": proposed_view_hash,
            "proposed_artifact_hashes": dict(proposed_artifact_hashes),
            "planning_inputs_digest": planning_inputs_digest,
            "consumer_marker_sha256": consumer_marker_sha256,
            "gate_policy": dict(FINALIZATION_GATE_POLICY),
        }
    )
    durable_replace_pointer_bytes(
        candidate.parent,
        attestation,
        canonical_json_bytes(record.to_dict()),
    )
    return attestation


def read_materialization_finalization(path: str | Path) -> MaterializationFinalization:
    candidate = Path(path)
    attestation = materialization_finalization_path(candidate)
    if attestation.is_symlink() or not attestation.is_file():
        raise ValueError("materialization finalization is missing or not a regular file")
    raw = attestation.read_bytes()
    payload = decode_canonical_json_object(
        raw,
        "materialization finalization",
    )
    return MaterializationFinalization.from_dict(payload)


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
        finalization = read_materialization_finalization(candidate)
        payload = _document(candidate)
        base_selection = materialization_base_selection(payload)
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    return (
        finalization.bundle_hash == str(bundle_hash)
        and finalization.candidate_revision == materialization_candidate_revision(candidate)
        and finalization.base_selection == base_selection
    )


@contextmanager
def materialization_candidate_lock(path: Path) -> Iterator[None]:
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
    candidate_write_guard: Callable[[], AbstractContextManager[None]] | None = None,
) -> tuple[dict[str, Any], str]:
    """Run one locked compare-and-swap candidate mutation."""
    with materialization_candidate_lock(path):
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

        guard = nullcontext() if candidate_write_guard is None else candidate_write_guard()
        with guard:
            package.atomic_write(path, encoded.decode("utf-8"))
        return payload, hashlib.sha256(encoded).hexdigest()


def _rows(document: dict[str, Any], key: str, where: str) -> list[dict[str, Any]]:
    value = document.get(key)
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ValueError(f"{where}.{key} must be a list of objects")
    return value


def _matches_reserved(role: str, reserved: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatchcase(role, pattern) or fnmatch.fnmatchcase(pattern, role) for pattern in reserved)


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
                f"layer {layer.id} dependency {dependency} is absent from the selected executable consumer view"
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
            raise ValueError(f"layer {layer.id} dependency {dependency} has an invalid sealed outcome: {exc}") from exc
        if sealed.status != "passed":
            raise ValueError(f"layer {layer.id} dependency {dependency} does not have a passed sealed outcome")
        eligible, reasons = current_outcome_eligibility(
            shot,
            dependency_layer,
            outcome,
        )
        if not eligible:
            raise ValueError(f"layer {layer.id} dependency {dependency} sealed outcome is stale: " + "; ".join(reasons))
        passed.update(sealed.passed_bindings)
    missing = sorted(
        f"{kind}:{identifier}" for kind, identifier in layer.jit.required_outcomes if (kind, identifier) not in passed
    )
    if missing:
        raise ValueError(f"layer {layer.id} required upstream outcomes have not passed: " + ", ".join(missing))


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
