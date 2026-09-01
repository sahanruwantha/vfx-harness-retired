"""Resolve pure HIR-0171 capsules from one verified selected-authority snapshot."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vfx_harness.domain.authority_capsules import (
    AuthorityCapsuleError,
    AuthorityCapsuleSet,
    compile_authority_capsules,
)
from vfx_harness.domain.authority_head_records import OVERLAY_ARTIFACTS
from vfx_harness.infrastructure.trusted_files import (
    TrustedFileBinding,
    TrustedFileError,
    require_trusted_file_unchanged,
)
from vfx_harness.orchestration import plan_bundle_integrity

if TYPE_CHECKING:
    from vfx_harness.orchestration.authority_selection import (
        ResolvedSelectedAuthority,
    )


class AuthorityCapsuleResolutionError(ValueError):
    """Selected or proposed authority cannot produce one stable capsule set."""


@dataclass(frozen=True, slots=True)
class CapturedAuthorityCapsules:
    """Capsules plus every exact authority byte binding from which they were read."""

    capsule_set: AuthorityCapsuleSet
    source_bindings: tuple[TrustedFileBinding, ...]

    def require_sources_unchanged(self) -> None:
        try:
            for binding in self.source_bindings:
                require_trusted_file_unchanged(
                    binding,
                    "authority capsule source",
                )
        except TrustedFileError as exc:
            raise AuthorityCapsuleResolutionError(str(exc)) from exc


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise AuthorityCapsuleResolutionError(
                f"authority capsule source contains duplicate key {key!r}"
            )
        value[key] = item
    return value


def _reject_non_finite(value: str) -> None:
    raise AuthorityCapsuleResolutionError(
        f"authority capsule source contains non-finite number {value!r}"
    )


def _decode(payload: bytes, where: str) -> Any:
    try:
        return json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_non_finite,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AuthorityCapsuleResolutionError(
            f"{where} is not strict UTF-8 JSON: {exc}"
        ) from exc


def _snapshot_documents(
    shot: Path,
    paths: Mapping[str, Path],
    *,
    where: str,
) -> tuple[dict[str, Any], tuple[TrustedFileBinding, ...]]:
    if set(paths) != set(OVERLAY_ARTIFACTS):
        raise AuthorityCapsuleResolutionError(
            f"{where} must name every overlay artifact exactly once"
        )
    documents: dict[str, Any] = {}
    bindings: list[TrustedFileBinding] = []
    for name in OVERLAY_ARTIFACTS:
        try:
            snapshot = plan_bundle_integrity.read_real_file_snapshot(
                shot,
                paths[name],
                f"{where} {name}",
            )
        except (OSError, plan_bundle_integrity.PlanPublicationError) as exc:
            raise AuthorityCapsuleResolutionError(str(exc)) from exc
        documents[name] = _decode(snapshot.payload, f"{where} {name}")
        bindings.append(snapshot.binding)
    return documents, tuple(bindings)


def capture_selected_authority_capsules(
    shot_folder: str | Path,
    selected_authority: ResolvedSelectedAuthority,
) -> CapturedAuthorityCapsules:
    """Compile capsules from one exact global bundle and effective selected view."""

    shot = Path(shot_folder).expanduser().absolute()
    plan = selected_authority.plan
    if plan is None:
        raise AuthorityCapsuleResolutionError(
            "authority capsules require selected global plan authority"
        )
    global_paths = {
        name: plan.bundle.root / name for name in OVERLAY_ARTIFACTS
    }
    try:
        effective_paths = {
            name: selected_authority.artifact_paths[name]
            for name in OVERLAY_ARTIFACTS
        }
    except KeyError as exc:
        raise AuthorityCapsuleResolutionError(
            f"selected authority omits capsule source {exc.args[0]}"
        ) from exc
    global_documents, global_bindings = _snapshot_documents(
        shot,
        global_paths,
        where="global authority capsule source",
    )
    effective_documents, effective_bindings = _snapshot_documents(
        shot,
        effective_paths,
        where="effective authority capsule source",
    )
    try:
        capsule_set = compile_authority_capsules(
            global_documents,
            effective_documents,
        )
    except AuthorityCapsuleError as exc:
        raise AuthorityCapsuleResolutionError(str(exc)) from exc
    captured = CapturedAuthorityCapsules(
        capsule_set=capsule_set,
        source_bindings=global_bindings + effective_bindings,
    )
    captured.require_sources_unchanged()
    return captured


def compile_proposed_authority_capsules(
    shot_folder: str | Path,
    selected_authority: ResolvedSelectedAuthority,
    proposed_documents: Mapping[str, Any],
) -> CapturedAuthorityCapsules:
    """Compile a proposed effective view against the exact selected sparse bundle."""

    shot = Path(shot_folder).expanduser().absolute()
    plan = selected_authority.plan
    if plan is None:
        raise AuthorityCapsuleResolutionError(
            "proposed authority capsules require selected global plan authority"
        )
    global_documents, bindings = _snapshot_documents(
        shot,
        {name: plan.bundle.root / name for name in OVERLAY_ARTIFACTS},
        where="proposed authority global capsule source",
    )
    if set(proposed_documents) != set(OVERLAY_ARTIFACTS):
        raise AuthorityCapsuleResolutionError(
            "proposed authority must contain every overlay document exactly once"
        )
    try:
        capsule_set = compile_authority_capsules(
            global_documents,
            proposed_documents,
        )
    except AuthorityCapsuleError as exc:
        raise AuthorityCapsuleResolutionError(str(exc)) from exc
    captured = CapturedAuthorityCapsules(
        capsule_set=capsule_set,
        source_bindings=bindings,
    )
    captured.require_sources_unchanged()
    return captured


def selected_layer_capsule_digest(
    shot_folder: str | Path,
    layer_id: str,
    selected_authority: ResolvedSelectedAuthority,
) -> str:
    """Return one selected layer's semantic execution identity."""

    captured = capture_selected_authority_capsules(
        shot_folder,
        selected_authority,
    )
    try:
        layer = captured.capsule_set.layer(str(layer_id))
    except StopIteration as exc:
        raise AuthorityCapsuleResolutionError(
            f"selected authority has no capsule for layer {layer_id!r}"
        ) from exc
    captured.require_sources_unchanged()
    return layer.capsule_digest


__all__ = [
    "AuthorityCapsuleResolutionError",
    "CapturedAuthorityCapsules",
    "capture_selected_authority_capsules",
    "compile_proposed_authority_capsules",
    "selected_layer_capsule_digest",
]
