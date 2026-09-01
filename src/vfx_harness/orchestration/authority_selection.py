"""Resolve one semantic selected-authority state from verified plan and JIT pointers.

The returned semantic assertion deliberately excludes run ids, paths, timestamps, and pointer
bytes.  Its companion selection token binds both monotone v2 head revisions to the exact bytes
read under the shared shot lock.  Consumers use the immutable paths from this snapshot; writers
compare the token under the same lock immediately before mutation.
"""

from __future__ import annotations

import hashlib
import json
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from vfx_harness.domain.authority_head_records import (
    OVERLAY_ARTIFACTS,
    JitViewPointer,
    JitViewPointerError,
    canonical_view_hash,
    materialized_layers_from_document,
    parse_jit_view_pointer,
    require_materialized_layers_match,
)
from vfx_harness.domain.stop_envelope_primitives import canonical_digest
from vfx_harness.domain.stop_transaction_state import (
    SelectedAuthorityAssertionV2,
    SelectedAuthorityBundle,
    SelectedAuthorityView,
)
from vfx_harness.orchestration import plan_authority
from vfx_harness.orchestration.authority_selection_heads import (
    AuthoritySelectionHeadError,
    AuthoritySelectionHeads,
    read_authority_selection_heads,
)
from vfx_harness.orchestration.authority_selection_transaction import (
    AuthoritySelectionConflict,
    AuthoritySelectionToken,
    authority_selection_lock,
)

_BUNDLE_MANIFEST_FIELDS = frozenset(
    {"schema", "run_id", "content_hash", "outcome", "artifacts"}
)
_PUBLISHABLE_OUTCOMES = frozenset(
    {"clean", "clean_with_assumptions", "clean_with_deferred"}
)


class SelectedAuthorityResolutionError(RuntimeError):
    """Selected plan/JIT authority cannot produce one verified semantic assertion."""


class _DuplicateJsonKey(ValueError):
    """An authority document contains an ambiguous repeated object member."""


@dataclass(frozen=True, slots=True)
class AuthorityPointerObservation:
    """Exact non-semantic pointer observations made while deriving an assertion."""

    plan_pointer_sha256: str | None
    jit_pointer_sha256: str | None

    @property
    def digest(self) -> str:
        return canonical_digest(
            {
                "schema": "vfx-harness.authority-pointer-observation/v1",
                "plan_pointer_sha256": self.plan_pointer_sha256,
                "jit_pointer_sha256": self.jit_pointer_sha256,
            }
        )


@dataclass(frozen=True, slots=True)
class ResolvedSelectedAuthority:
    assertion: SelectedAuthorityAssertionV2
    pointer_observation: AuthorityPointerObservation
    selection_token: AuthoritySelectionToken
    plan: plan_authority.SelectedPlanAuthority | None
    artifact_paths: Mapping[str, Path]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_file(
    shot: Path,
    path: Path,
    *,
    label: str,
    optional: bool,
) -> bytes | None:
    """Read one regular in-shot file without following a symlink component."""

    try:
        relative = path.relative_to(shot)
    except ValueError as exc:
        raise SelectedAuthorityResolutionError(f"{label} escapes the shot root") from exc
    current = shot
    for index, part in enumerate(relative.parts):
        current /= part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            if optional:
                return None
            raise SelectedAuthorityResolutionError(f"{label} is missing: {relative}") from None
        except OSError as exc:
            raise SelectedAuthorityResolutionError(
                f"{label} is unreadable: {relative}"
            ) from exc
        if stat.S_ISLNK(mode):
            raise SelectedAuthorityResolutionError(
                f"{label} contains a symlink component: {relative}"
            )
        if index < len(relative.parts) - 1:
            if not stat.S_ISDIR(mode):
                raise SelectedAuthorityResolutionError(
                    f"{label} has a non-directory parent: {relative}"
                )
        elif not stat.S_ISREG(mode):
            raise SelectedAuthorityResolutionError(
                f"{label} is not a regular file: {relative}"
            )
    try:
        return path.read_bytes()
    except OSError as exc:
        raise SelectedAuthorityResolutionError(f"{label} is unreadable: {relative}") from exc


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateJsonKey(key)
        value[key] = item
    return value


def _json_value(payload: bytes, *, label: str) -> Any:
    try:
        return json.loads(payload, object_pairs_hook=_reject_duplicate_keys)
    except _DuplicateJsonKey as exc:
        raise SelectedAuthorityResolutionError(
            f"{label} contains duplicate JSON key {exc.args[0]!r}"
        ) from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SelectedAuthorityResolutionError(f"{label} is not valid JSON") from exc


def _digest_map(value: object, *, label: str) -> dict[str, str]:
    if not isinstance(value, Mapping) or not value:
        raise SelectedAuthorityResolutionError(f"{label} must be a non-empty digest map")
    result: dict[str, str] = {}
    for raw_name, raw_digest in value.items():
        if not isinstance(raw_name, str) or not raw_name:
            raise SelectedAuthorityResolutionError(f"{label} contains an invalid artifact name")
        if (
            not isinstance(raw_digest, str)
            or len(raw_digest) != 64
            or any(character not in "0123456789abcdef" for character in raw_digest)
        ):
            raise SelectedAuthorityResolutionError(
                f"{label} contains an invalid digest for {raw_name!r}"
            )
        result[raw_name] = raw_digest
    return result


def _bundle_components(
    shot: Path,
    bundle: plan_authority.PlanBundle,
) -> tuple[SelectedAuthorityBundle, SelectedAuthorityView]:
    manifest_payload = _read_file(
        shot,
        bundle.root / "bundle.json",
        label="selected plan bundle manifest",
        optional=False,
    )
    assert manifest_payload is not None
    manifest = _json_value(manifest_payload, label="selected plan bundle manifest")
    if not isinstance(manifest, Mapping) or set(manifest) != _BUNDLE_MANIFEST_FIELDS:
        raise SelectedAuthorityResolutionError(
            "selected plan bundle manifest fields do not match the v1 schema"
        )
    if manifest.get("schema") != plan_authority.BUNDLE_SCHEMA:
        raise SelectedAuthorityResolutionError(
            "selected plan bundle manifest has an unsupported schema"
        )
    if manifest.get("content_hash") != bundle.content_hash:
        raise SelectedAuthorityResolutionError(
            "selected plan bundle manifest disagrees with the resolved content digest"
        )
    outcome = manifest.get("outcome")
    if outcome not in _PUBLISHABLE_OUTCOMES or bundle.outcome != outcome:
        raise SelectedAuthorityResolutionError(
            "selected plan bundle carries an invalid or inconsistent gate outcome"
        )
    artifact_hashes = _digest_map(
        manifest.get("artifacts"),
        label="selected plan bundle artifact manifest",
    )
    if set(artifact_hashes) != set(bundle.artifacts):
        raise SelectedAuthorityResolutionError(
            "selected plan bundle artifact manifest disagrees with verified membership"
        )

    semantic_manifest_digest = canonical_digest(
        {
            "schema": "vfx-harness.plan-bundle-semantic/v1",
            "content_hash": bundle.content_hash,
            "outcome": outcome,
            "artifacts": artifact_hashes,
        }
    )
    bundle_assertion = SelectedAuthorityBundle(
        digest=bundle.content_hash,
        outcome=str(outcome),
        semantic_manifest_digest=semantic_manifest_digest,
    )

    bundle_view_hashes = {
        name: artifact_hashes[name]
        for name in OVERLAY_ARTIFACTS
        if name in artifact_hashes
    }
    if set(bundle_view_hashes) != set(OVERLAY_ARTIFACTS):
        raise SelectedAuthorityResolutionError(
            "selected plan bundle lacks the complete consumer-view artifact surface"
        )
    layers_payload = _read_file(
        shot,
        bundle.root / "layers.json",
        label="selected plan bundle layers",
        optional=False,
    )
    assert layers_payload is not None
    layers_document = _json_value(layers_payload, label="selected plan bundle layers")
    try:
        materialized_layers = materialized_layers_from_document(layers_document)
    except JitViewPointerError as exc:
        raise SelectedAuthorityResolutionError(str(exc)) from exc
    view_manifest_digest = canonical_digest(
        {
            "schema": "vfx-harness.selected-view-semantic/v1",
            "source": "bundle",
            "bundle_digest": bundle.content_hash,
            "view_digest": bundle.content_hash,
            "materialized_layers": list(materialized_layers),
            "artifact_hashes": bundle_view_hashes,
        }
    )
    return bundle_assertion, SelectedAuthorityView(
        source="bundle",
        digest=bundle.content_hash,
        semantic_manifest_digest=view_manifest_digest,
    )


def _verified_jit_view(
    shot: Path,
    payload: bytes,
) -> tuple[JitViewPointer, SelectedAuthorityView, dict[str, Path]]:
    value = _json_value(payload, label="selected JIT pointer")
    try:
        pointer = parse_jit_view_pointer(value)
    except JitViewPointerError as exc:
        raise SelectedAuthorityResolutionError(
            f"selected JIT pointer is invalid: {exc}"
        ) from exc
    documents: dict[str, Any] = {}
    paths: dict[str, Path] = {}
    for name in OVERLAY_ARTIFACTS:
        path = shot / pointer.artifacts[name]
        artifact_payload = _read_file(
            shot,
            path,
            label=f"selected JIT artifact {name!r}",
            optional=False,
        )
        assert artifact_payload is not None
        if _sha256(artifact_payload) != pointer.hashes[name]:
            raise SelectedAuthorityResolutionError(
                f"selected JIT artifact {name!r} hash does not match its pointer"
            )
        documents[name] = _json_value(
            artifact_payload,
            label=f"selected JIT artifact {name!r}",
        )
        paths[name] = path
    try:
        require_materialized_layers_match(pointer, documents["layers.json"])
    except JitViewPointerError as exc:
        raise SelectedAuthorityResolutionError(str(exc)) from exc
    if canonical_view_hash(documents) != pointer.view_hash:
        raise SelectedAuthorityResolutionError(
            "selected JIT view digest does not match its pinned documents"
        )
    semantic_manifest_digest = canonical_digest(
        {
            "schema": "vfx-harness.selected-view-semantic/v1",
            "source": "jit",
            "bundle_digest": pointer.bundle_hash,
            "view_digest": pointer.view_hash,
            "materialized_layers": list(pointer.materialized_layers),
            "artifact_hashes": pointer.hashes,
        }
    )
    return (
        pointer,
        SelectedAuthorityView(
            source="jit",
            digest=pointer.view_hash,
            semantic_manifest_digest=semantic_manifest_digest,
        ),
        paths,
    )


def _observation(plan_pointer: bytes | None, jit_pointer: bytes | None) -> AuthorityPointerObservation:
    return AuthorityPointerObservation(
        plan_pointer_sha256=None if plan_pointer is None else _sha256(plan_pointer),
        jit_pointer_sha256=None if jit_pointer is None else _sha256(jit_pointer),
    )


def resolve_selected_authority_from_heads(
    shot_folder: str | Path,
    heads: AuthoritySelectionHeads,
) -> ResolvedSelectedAuthority:
    """Verify semantic authority from heads read under a caller-owned lock.

    Writers use this after tentatively replacing one head while retaining the
    exclusive selection lock.  Calling :func:`resolve_selected_authority` there would
    recursively acquire the lock and could deadlock; accepting the exact already-read
    heads also makes the postcondition explicit.  The caller must keep the shared or
    exclusive authority-selection lock for this entire call.
    """

    if not isinstance(heads, AuthoritySelectionHeads):
        raise SelectedAuthorityResolutionError(
            "selected authority verification requires typed heads read under the selection lock"
        )
    shot = Path(shot_folder).expanduser().resolve()
    plan_pointer = heads.plan_pointer_bytes
    jit_pointer = heads.jit_pointer_bytes
    verified_jit = (
        None if jit_pointer is None else _verified_jit_view(shot, jit_pointer)
    )
    plan_selection = None
    artifact_paths: dict[str, Path] = {}
    if plan_pointer is None:
        assertion = SelectedAuthorityAssertionV2(
            selection="absent",
            bundle=None,
            effective_view=None,
        )
    else:
        assert heads.plan is not None
        try:
            plan_selection = plan_authority._resolve_pointer(
                shot,
                heads.plan.as_dict(),
            )
        except plan_authority.PlanPublicationError as exc:
            raise SelectedAuthorityResolutionError(
                f"selected plan authority is invalid: {exc}"
            ) from exc
        bundle = plan_selection.bundle
        artifact_paths = {
            name: bundle.root / name for name in bundle.artifacts
        }
        bundle_assertion, bundle_view = _bundle_components(shot, bundle)
        effective_view = bundle_view
        if verified_jit is not None:
            selected_jit, jit_view, jit_paths = verified_jit
            if (
                selected_jit.bundle_hash == bundle.content_hash
                and selected_jit.plan_revision == plan_selection.revision
            ):
                effective_view = jit_view
                artifact_paths.update(jit_paths)
        assertion = SelectedAuthorityAssertionV2(
            selection="selected",
            bundle=bundle_assertion,
            effective_view=effective_view,
        )
    return ResolvedSelectedAuthority(
        assertion=assertion,
        pointer_observation=_observation(plan_pointer, jit_pointer),
        selection_token=heads.token,
        plan=plan_selection,
        artifact_paths=MappingProxyType(artifact_paths),
    )


def resolve_selected_authority(
    shot_folder: str | Path,
) -> ResolvedSelectedAuthority:
    """Resolve one verified semantic selection and exact versioned CAS token.

    An absent global pointer is the only absent authority state.  Existing malformed,
    unreadable, stale, or symlinked plan/JIT authority fails closed.  A valid JIT pointer for a
    different global bundle is verified but inert, so the effective view is bundle-backed.
    """

    shot = Path(shot_folder).expanduser().resolve()
    try:
        lock = authority_selection_lock(shot, exclusive=False)
        with lock:
            heads = read_authority_selection_heads(shot)
            return resolve_selected_authority_from_heads(shot, heads)
    except AuthoritySelectionHeadError as exc:
        label = (
            "selected plan authority is invalid"
            if exc.head == "plan"
            else "selected JIT pointer is invalid"
        )
        raise SelectedAuthorityResolutionError(f"{label}: {exc}") from exc
    except AuthoritySelectionConflict as exc:
        raise SelectedAuthorityResolutionError(
            f"selected authority lock/storage is invalid: {exc}"
        ) from exc
