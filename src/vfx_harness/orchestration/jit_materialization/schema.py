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
from typing import TYPE_CHECKING, Any

from vfx_harness.domain.authority_head_records import (
    JIT_CURRENT_PATH,
    JIT_STATE_DIR,
    JIT_VIEW_POINTER_SCHEMA,
    canonical_json_bytes,
    decode_canonical_json_object,
)
from vfx_harness.domain.authority_state_records import AuthorityStateRecordRef
from vfx_harness.orchestration import layer_publication
from vfx_harness.orchestration.authority_selection_transaction import (
    AuthoritySelectionConflict,
    AuthoritySelectionToken,
    durable_replace_file_bytes,
)
from vfx_harness.orchestration.ledger import Layer
from vfx_harness.orchestration.plan_consumer_view import OVERLAY_ARTIFACTS

if TYPE_CHECKING:
    from vfx_harness.orchestration.authority_selection import (
        ResolvedSelectedAuthority,
    )

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


FINALIZATION_SCHEMA = "vfx-harness.materialization-finalization/v4"
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
        "publication_jit_pointer_sha256",
        "authority_transition_kind",
        "authority_transition_intent_ref",
        "authority_capsule_set_digest",
        "authority_effects_digest",
        "authority_state_head_ref",
        "before_state_hashes",
        "after_state_hashes",
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


def _require_state_hashes(value: Any, where: str) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{where} must be an object")
    parsed: dict[str, str] = {}
    for layer_id, digest in value.items():
        if (
            not isinstance(layer_id, str)
            or not layer_id
            or layer_id != layer_id.strip()
        ):
            raise ValueError(f"{where} contains an invalid layer id")
        parsed[layer_id] = _require_digest(digest, f"{where}[{layer_id!r}]")
    return dict(sorted(parsed.items()))


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
    publication_jit_pointer_sha256: str
    authority_transition_kind: str
    authority_transition_intent_ref: AuthorityStateRecordRef | None
    authority_capsule_set_digest: str
    authority_effects_digest: str | None
    authority_state_head_ref: AuthorityStateRecordRef
    before_state_hashes: dict[str, str]
    after_state_hashes: dict[str, str]

    @classmethod
    def from_dict(cls, value: Any) -> MaterializationFinalization:
        if not isinstance(value, Mapping) or set(value) != _FINALIZATION_FIELDS:
            raise ValueError("materialization finalization fields do not match the v4 schema")
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
        transition_kind = value.get("authority_transition_kind")
        if transition_kind not in {"commit", "noop"}:
            raise ValueError(
                "materialization finalization authority_transition_kind must be "
                "'commit' or 'noop'"
            )
        raw_intent = value.get("authority_transition_intent_ref")
        intent_ref = (
            None
            if raw_intent is None
            else AuthorityStateRecordRef.parse(
                raw_intent,
                "materialization finalization.authority_transition_intent_ref",
            )
        )
        if (
            intent_ref is not None
            and intent_ref.record_schema
            != "vfx-harness.authority-state-transition-intent/v1"
        ):
            raise ValueError(
                "materialization finalization intent reference must name an "
                "authority-state transition intent"
            )
        raw_effects = value.get("authority_effects_digest")
        effects_digest = (
            None
            if raw_effects is None
            else _require_digest(
                raw_effects,
                "materialization finalization.authority_effects_digest",
            )
        )
        if transition_kind == "commit" and (intent_ref is None or effects_digest is None):
            raise ValueError(
                "committing materialization finalization requires an intent and "
                "effects digest"
            )
        if transition_kind == "noop" and (intent_ref is not None or effects_digest is not None):
            raise ValueError(
                "no-op materialization finalization cannot name an intent or effects"
            )
        head_ref = AuthorityStateRecordRef.parse(
            value.get("authority_state_head_ref"),
            "materialization finalization.authority_state_head_ref",
        )
        if head_ref.record_schema != "vfx-harness.authority-state-head/v1":
            raise ValueError(
                "materialization finalization authority_state_head_ref must name a "
                "coordinator head"
            )
        before_state_hashes = _require_state_hashes(
            value.get("before_state_hashes"),
            "materialization finalization.before_state_hashes",
        )
        after_state_hashes = _require_state_hashes(
            value.get("after_state_hashes"),
            "materialization finalization.after_state_hashes",
        )
        if transition_kind == "noop" and before_state_hashes != after_state_hashes:
            raise ValueError(
                "no-op materialization finalization must preserve every state hash"
            )
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
            publication_jit_pointer_sha256=_require_digest(
                value.get("publication_jit_pointer_sha256"),
                "materialization finalization.publication_jit_pointer_sha256",
            ),
            authority_transition_kind=transition_kind,
            authority_transition_intent_ref=intent_ref,
            authority_capsule_set_digest=_require_digest(
                value.get("authority_capsule_set_digest"),
                "materialization finalization.authority_capsule_set_digest",
            ),
            authority_effects_digest=effects_digest,
            authority_state_head_ref=head_ref,
            before_state_hashes=before_state_hashes,
            after_state_hashes=after_state_hashes,
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
            "publication_jit_pointer_sha256": self.publication_jit_pointer_sha256,
            "authority_transition_kind": self.authority_transition_kind,
            "authority_transition_intent_ref": (
                None
                if self.authority_transition_intent_ref is None
                else self.authority_transition_intent_ref.as_dict()
            ),
            "authority_capsule_set_digest": self.authority_capsule_set_digest,
            "authority_effects_digest": self.authority_effects_digest,
            "authority_state_head_ref": self.authority_state_head_ref.as_dict(),
            "before_state_hashes": dict(self.before_state_hashes),
            "after_state_hashes": dict(self.after_state_hashes),
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
    publication_jit_pointer_sha256: str,
    authority_transition_kind: str,
    authority_transition_intent_ref: AuthorityStateRecordRef | None,
    authority_capsule_set_digest: str,
    authority_effects_digest: str | None,
    authority_state_head_ref: AuthorityStateRecordRef,
    before_state_hashes: Mapping[str, str],
    after_state_hashes: Mapping[str, str],
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
            "publication_jit_pointer_sha256": publication_jit_pointer_sha256,
            "authority_transition_kind": authority_transition_kind,
            "authority_transition_intent_ref": (
                None
                if authority_transition_intent_ref is None
                else authority_transition_intent_ref.as_dict()
            ),
            "authority_capsule_set_digest": authority_capsule_set_digest,
            "authority_effects_digest": authority_effects_digest,
            "authority_state_head_ref": authority_state_head_ref.as_dict(),
            "before_state_hashes": dict(before_state_hashes),
            "after_state_hashes": dict(after_state_hashes),
            "gate_policy": dict(FINALIZATION_GATE_POLICY),
        }
    )
    durable_replace_file_bytes(
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


def _require_upstream_outcomes(
    shot: Path,
    layer: Layer,
    available_layers: Mapping[str, Layer],
    *,
    selected_authority: ResolvedSelectedAuthority,
) -> None:
    if layer.jit is None:
        raise ValueError(f"layer {layer.id} has no deferred JIT authority")
    passed: set[tuple[str, str]] = set()
    for dependency in layer.jit.depends_on_layers:
        dependency_layer = available_layers.get(dependency)
        if dependency_layer is None:
            raise ValueError(
                f"layer {layer.id} dependency {dependency} is absent from the selected executable consumer view"
            )
        try:
            publication = layer_publication.require_current_layer_publication(
                shot,
                dependency_layer,
                selected_authority,
            )
        except layer_publication.LayerPublicationConflict as exc:
            raise ValueError(
                f"layer {layer.id} cannot materialize before dependency {dependency} "
                f"has a current receipt-backed publication: {exc}"
            ) from exc
        passed.update(publication.outcome.passed_bindings)
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
