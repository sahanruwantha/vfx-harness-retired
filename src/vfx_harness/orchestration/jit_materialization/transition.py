"""Prepare the exact JIT pointer and authority-state transition a gate previews."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from vfx_harness.domain.authority_capsules import AuthorityCapsuleSet
from vfx_harness.domain.authority_head_records import (
    AuthoritySelectionTokenProjection,
    canonical_json_bytes,
)
from vfx_harness.domain.authority_state_records import AuthorityStateRecordRef
from vfx_harness.orchestration.authority_capsule_resolution import (
    capture_selected_authority_capsules,
    compile_proposed_authority_capsules,
)
from vfx_harness.orchestration.authority_selection_heads import (
    read_authority_selection_heads,
)
from vfx_harness.orchestration.authority_selection_transaction import (
    authority_selection_lock,
    require_matching_authority_selection_token,
)
from vfx_harness.orchestration.authority_state_context import (
    resolve_current_authority_state,
)
from vfx_harness.orchestration.authority_state_preparation import (
    PreparedAuthorityStateTransition,
    capture_authority_state_snapshot_locked,
    prepare_authority_state_transition_locked,
)
from vfx_harness.orchestration.jit_materialization.schema import (
    MATERIALIZATION_SCHEMA,
    OVERLAY_ARTIFACTS,
    STATE_DIR,
)
from vfx_harness.orchestration.jit_materialization.view_pointer import JitViewPointer

if TYPE_CHECKING:
    from vfx_harness.orchestration.authority_selection import (
        ResolvedSelectedAuthority,
    )
    from vfx_harness.orchestration.authority_selection_heads import (
        AuthoritySelectionHeads,
    )


@dataclass(frozen=True, slots=True)
class PreparedMaterializationPublication:
    """One exact no-op or successor publication prepared from the live base."""

    pointer: JitViewPointer
    pointer_payload: bytes
    capsule_set: AuthorityCapsuleSet
    transition: PreparedAuthorityStateTransition | None
    authority_state_head_ref: AuthorityStateRecordRef
    before_state_hashes: Mapping[str, str]
    after_state_hashes: Mapping[str, str]
    after_state_payloads: Mapping[str, bytes]

    @property
    def transition_kind(self) -> str:
        return "noop" if self.transition is None else "commit"

    @property
    def pointer_sha256(self) -> str:
        return hashlib.sha256(self.pointer_payload).hexdigest()

    @property
    def effects_digest(self) -> str | None:
        if self.transition is None:
            return None
        return self.transition.intent.proposal.effects_digest


def _materialized_layers(documents: Mapping[str, Any]) -> tuple[str, ...]:
    rows = documents["layers.json"].get("layers")
    if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
        raise ValueError("proposed layers.json.layers must be a list of objects")
    return tuple(
        sorted(
            str(row.get("id"))
            for row in rows
            if row.get("execution") != "jit_deferred"
        )
    )


def _pointer(
    shot: Path,
    *,
    heads: AuthoritySelectionHeads,
    bundle_hash: str,
    view_hash: str,
    artifact_hashes: Mapping[str, str],
    documents: Mapping[str, Any],
    revision: int,
) -> JitViewPointer:
    view = shot / STATE_DIR / "views" / view_hash
    return JitViewPointer(
        revision=revision,
        plan_revision=heads.token.plan_revision,
        bundle_hash=bundle_hash,
        view_hash=view_hash,
        materialized_layers=_materialized_layers(documents),
        artifacts={
            name: (view / name).relative_to(shot).as_posix()
            for name in OVERLAY_ARTIFACTS
        },
        hashes=dict(artifact_hashes),
    )


def _state_hashes(payloads: Mapping[str, bytes]) -> Mapping[str, str]:
    return MappingProxyType(
        {
            layer_id: hashlib.sha256(payload).hexdigest()
            for layer_id, payload in sorted(payloads.items())
        }
    )


def _selection_projection(heads: AuthoritySelectionHeads) -> AuthoritySelectionTokenProjection:
    return AuthoritySelectionTokenProjection(
        heads.token.plan_revision,
        heads.token.plan_pointer_sha256,
        heads.token.jit_revision,
        heads.token.jit_pointer_sha256,
    )


def _require_noop_coordinator(
    shot: Path,
    *,
    heads: AuthoritySelectionHeads,
    selected_before: ResolvedSelectedAuthority,
    proposed_capsules: AuthorityCapsuleSet,
) -> tuple[AuthorityStateRecordRef, Mapping[str, bytes]]:
    context = resolve_current_authority_state(shot)
    if context is None:  # pragma: no cover - strict resolver never returns this here
        raise ValueError("no-op JIT publication requires a coordinator head")
    if context.head.selection_token != _selection_projection(heads):
        raise ValueError(
            "no-op JIT publication coordinator does not authorize the live selection"
        )
    current = capture_selected_authority_capsules(shot, selected_before)
    current.require_sources_unchanged()
    if current.capsule_set.as_dict() != proposed_capsules.as_dict():
        raise ValueError(
            "no-op JIT publication proposed capsules differ from current authority"
        )
    states, payloads = capture_authority_state_snapshot_locked(
        shot,
        proposed_capsules,
    )
    installed = {row.layer_id: row for row in context.commit.installed_states}
    if set(installed) != set(states):
        raise ValueError(
            "no-op JIT publication state namespace differs from the coordinator binding"
        )
    layer_capsules = {row.layer_id: row for row in proposed_capsules.layers}
    for layer_id, image in installed.items():
        layer = layer_capsules.get(layer_id)
        if (
            layer is None
            or image.binding.transition_revision != context.head.revision
            or image.binding.selection_token != context.head.selection_token
            or image.binding.layer_generation_digest != layer.capsule_digest
            or {
                row.unit_id: row.unit_generation_digest
                for row in image.binding.units
            }
            != dict(layer.unit_capsule_digests)
        ):
            raise ValueError(
                f"no-op JIT publication has a stale coordinator binding for layer {layer_id!r}"
            )
    return context.head_ref, MappingProxyType(dict(payloads))


def prepare_materialization_publication_locked(
    shot_folder: str | Path,
    *,
    heads: AuthoritySelectionHeads,
    selected_before: ResolvedSelectedAuthority,
    documents: Mapping[str, Any],
    bundle_hash: str,
    view_hash: str,
    artifact_hashes: Mapping[str, str],
    candidate_payload: bytes,
    candidate_digest: str,
    prepared_at: str | None = None,
) -> PreparedMaterializationPublication:
    """Prepare under an already-held selection lock without changing live state."""

    shot = Path(shot_folder).expanduser().absolute()
    require_matching_authority_selection_token(
        selected_before.selection_token,
        heads.token,
    )
    captured = compile_proposed_authority_capsules(
        shot,
        selected_before,
        documents,
    )
    captured.require_sources_unchanged()
    current = None
    if heads.jit is not None:
        current = _pointer(
            shot,
            heads=heads,
            bundle_hash=bundle_hash,
            view_hash=view_hash,
            artifact_hashes=artifact_hashes,
            documents=documents,
            revision=heads.token.jit_revision,
        )
    if current is not None and heads.jit == current:
        assert heads.jit_pointer_bytes is not None
        head_ref, state_payloads = _require_noop_coordinator(
            shot,
            heads=heads,
            selected_before=selected_before,
            proposed_capsules=captured.capsule_set,
        )
        hashes = _state_hashes(state_payloads)
        return PreparedMaterializationPublication(
            pointer=current,
            pointer_payload=heads.jit_pointer_bytes,
            capsule_set=captured.capsule_set,
            transition=None,
            authority_state_head_ref=head_ref,
            before_state_hashes=hashes,
            after_state_hashes=hashes,
            after_state_payloads=state_payloads,
        )
    successor = _pointer(
        shot,
        heads=heads,
        bundle_hash=bundle_hash,
        view_hash=view_hash,
        artifact_hashes=artifact_hashes,
        documents=documents,
        revision=heads.token.jit_revision + 1,
    )
    pointer_payload = canonical_json_bytes(successor.as_dict())
    prepared = prepare_authority_state_transition_locked(
        shot,
        heads=heads,
        selected_before=selected_before,
        after_capsules=captured.capsule_set,
        after_plan_pointer_bytes=heads.plan_pointer_bytes,
        after_plan_revision=heads.token.plan_revision,
        after_jit_pointer_bytes=pointer_payload,
        after_jit_revision=successor.revision,
        producer_payload=candidate_payload,
        producer_schema=MATERIALIZATION_SCHEMA,
        producer_digest=candidate_digest,
        prepared_at=prepared_at,
    )
    before_hashes = MappingProxyType(
        {
            member.layer_id: member.before.sha256
            for member in prepared.intent.state_members
            if member.before is not None
        }
    )
    after_hashes = MappingProxyType(
        {
            member.layer_id: member.after.sha256
            for member in prepared.intent.state_members
            if member.after is not None
        }
    )
    predecessor_ref = prepared.intent.proposal.predecessor_head_ref
    if predecessor_ref is None:
        raise ValueError("JIT authority-state transition cannot be a genesis transition")
    return PreparedMaterializationPublication(
        pointer=successor,
        pointer_payload=pointer_payload,
        capsule_set=captured.capsule_set,
        transition=prepared,
        authority_state_head_ref=predecessor_ref,
        before_state_hashes=before_hashes,
        after_state_hashes=after_hashes,
        after_state_payloads=prepared.after_state_payloads,
    )


def prepare_selected_view_republication_locked(
    shot_folder: str | Path,
    *,
    heads: AuthoritySelectionHeads,
    selected_before: ResolvedSelectedAuthority,
    producer_payload: bytes,
    producer_schema: str,
    producer_digest: str,
    prepared_at: str | None = None,
) -> PreparedMaterializationPublication:
    """Prepare a transition that reselects the current JIT view under the next revision.

    The documents, view hash, and artifact hashes are the selected ones; only the
    pointer revision advances, so the transition's semantic effect is whatever the
    effects compiler derives from durable state alone (HIR-0182 digest migration).
    """

    shot = Path(shot_folder).expanduser().absolute()
    require_matching_authority_selection_token(selected_before.selection_token, heads.token)
    if heads.jit is None:
        raise ValueError("selected-view republication requires a selected JIT view")
    documents = {
        name: json.loads(Path(selected_before.artifact_paths[name]).read_text(encoding="utf-8"))
        for name in OVERLAY_ARTIFACTS
    }
    captured = capture_selected_authority_capsules(shot, selected_before)
    captured.require_sources_unchanged()
    successor = _pointer(
        shot,
        heads=heads,
        bundle_hash=heads.jit.bundle_hash,
        view_hash=heads.jit.view_hash,
        artifact_hashes=heads.jit.hashes,
        documents=documents,
        revision=heads.token.jit_revision + 1,
    )
    pointer_payload = canonical_json_bytes(successor.as_dict())
    prepared = prepare_authority_state_transition_locked(
        shot,
        heads=heads,
        selected_before=selected_before,
        after_capsules=captured.capsule_set,
        after_plan_pointer_bytes=heads.plan_pointer_bytes,
        after_plan_revision=heads.token.plan_revision,
        after_jit_pointer_bytes=pointer_payload,
        after_jit_revision=successor.revision,
        producer_payload=producer_payload,
        producer_schema=producer_schema,
        producer_digest=producer_digest,
        prepared_at=prepared_at,
    )
    predecessor_ref = prepared.intent.proposal.predecessor_head_ref
    if predecessor_ref is None:
        raise ValueError("selected-view republication cannot be a genesis transition")
    return PreparedMaterializationPublication(
        pointer=successor,
        pointer_payload=pointer_payload,
        capsule_set=captured.capsule_set,
        transition=prepared,
        authority_state_head_ref=predecessor_ref,
        before_state_hashes=MappingProxyType(
            {
                member.layer_id: member.before.sha256
                for member in prepared.intent.state_members
                if member.before is not None
            }
        ),
        after_state_hashes=MappingProxyType(
            {
                member.layer_id: member.after.sha256
                for member in prepared.intent.state_members
                if member.after is not None
            }
        ),
        after_state_payloads=prepared.after_state_payloads,
    )


def prepare_materialization_publication(
    shot_folder: str | Path,
    *,
    selected_before: ResolvedSelectedAuthority,
    documents: Mapping[str, Any],
    bundle_hash: str,
    view_hash: str,
    artifact_hashes: Mapping[str, str],
    candidate_payload: bytes,
    candidate_digest: str,
    prepared_at: str | None = None,
) -> PreparedMaterializationPublication:
    """Snapshot and prepare one exact publication while leaving authority untouched."""

    shot = Path(shot_folder).expanduser().absolute()
    with authority_selection_lock(shot, exclusive=False):
        heads = read_authority_selection_heads(shot)
        return prepare_materialization_publication_locked(
            shot,
            heads=heads,
            selected_before=selected_before,
            documents=documents,
            bundle_hash=bundle_hash,
            view_hash=view_hash,
            artifact_hashes=artifact_hashes,
            candidate_payload=candidate_payload,
            candidate_digest=candidate_digest,
            prepared_at=prepared_at,
        )


__all__ = [
    "PreparedMaterializationPublication",
    "prepare_materialization_publication",
    "prepare_materialization_publication_locked",
    "prepare_selected_view_republication_locked",
]
