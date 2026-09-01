"""Prepared publication of one claim-bound composed layer artifact."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from vfx_harness.agents.builder.composition_helpers import compose_unit_artifact_source
from vfx_harness.agents.builder.layer_finalization_guard import (
    LayerFinalizationClaimGuard,
)
from vfx_harness.infrastructure.trusted_files import (
    TrustedFileBinding,
    TrustedFileError,
    read_trusted_file,
    require_trusted_file_unchanged,
)
from vfx_harness.observability.prepared_publication import (
    FilePublicationConflict,
    PreparedFilePublication,
    commit_prepared_file,
    discard_prepared_file,
    prepare_file_update,
)


class LayerArtifactPublicationConflict(ValueError):
    """The claimed constituent bytes cannot publish this layer artifact."""


@dataclass(frozen=True, slots=True)
class PreparedLayerArtifact:
    destination: Path
    relative: str
    sha256: str
    source_bindings: tuple[TrustedFileBinding, ...]
    publication: PreparedFilePublication | None
    authority_binding: str


def _composed_payload(
    shot: Path,
    inputs: Iterable[tuple[str, str, str | None]],
    *,
    evaluation_barrier: str,
) -> tuple[bytes, tuple[TrustedFileBinding, ...]]:
    parts: list[tuple[str, str, str]] = []
    bindings: list[TrustedFileBinding] = []
    for index, (unit_id, script_path, expected_sha256) in enumerate(inputs):
        try:
            source = read_trusted_file(
                shot,
                shot / script_path,
                f"layer finalization unit input {index}",
                require_nonempty=True,
            )
        except TrustedFileError as exc:
            raise LayerArtifactPublicationConflict(str(exc)) from exc
        observed_sha256 = hashlib.sha256(source.payload).hexdigest()
        if expected_sha256 is not None and observed_sha256 != expected_sha256:
            raise LayerArtifactPublicationConflict(
                f"layer finalization unit input changed: {script_path}"
            )
        try:
            text = source.payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise LayerArtifactPublicationConflict(
                f"layer finalization unit input is not UTF-8: {script_path}"
            ) from exc
        parts.append((unit_id, script_path, text))
        bindings.append(source.binding)
    payload = compose_unit_artifact_source(
        parts,
        evaluation_barrier=evaluation_barrier,
    ).encode("utf-8")
    return payload, tuple(bindings)


def proposed_layer_artifact_sha256(
    folder: str | Path,
    unit_inputs: Iterable[tuple[str, str]],
    *,
    evaluation_barrier: str,
) -> str:
    """Safely derive the composed bytes that a future claim will bind."""

    shot = Path(folder).expanduser().absolute()
    payload, bindings = _composed_payload(
        shot,
        (
            (str(unit_id), str(script_path), None)
            for unit_id, script_path in unit_inputs
        ),
        evaluation_barrier=evaluation_barrier,
    )
    try:
        for binding in bindings:
            require_trusted_file_unchanged(
                binding,
                "proposed composed layer unit input",
            )
    except TrustedFileError as exc:
        raise LayerArtifactPublicationConflict(str(exc)) from exc
    return hashlib.sha256(payload).hexdigest()


def _require_sources_current(prepared: PreparedLayerArtifact) -> None:
    try:
        for binding in prepared.source_bindings:
            require_trusted_file_unchanged(binding, "composed layer unit input")
    except TrustedFileError as exc:
        raise LayerArtifactPublicationConflict(str(exc)) from exc


def _require_prepared_claim(
    prepared: PreparedLayerArtifact,
    guard: LayerFinalizationClaimGuard,
) -> None:
    claim = guard.claim
    if prepared.relative != claim.layer_script_path:
        raise LayerArtifactPublicationConflict(
            "prepared layer artifact path does not match its finalization claim"
        )
    if prepared.sha256 != claim.layer_script_sha256:
        raise LayerArtifactPublicationConflict(
            "prepared layer artifact SHA-256 does not match the proposed composed "
            "bytes bound by its finalization claim"
        )
    expected_authority = (
        f"layer-artifact:{claim.claim_id}:{claim.layer_script_sha256}"
    )
    if prepared.authority_binding != expected_authority:
        raise LayerArtifactPublicationConflict(
            "prepared layer artifact authority does not match its finalization claim"
        )


def prepare_layer_artifact(
    folder: str | Path,
    guard: LayerFinalizationClaimGuard,
    *,
    evaluation_barrier: str,
) -> PreparedLayerArtifact:
    """Read exact unit bytes and fsync the composed artifact outside claim locks."""

    guard.check("start composed layer artifact preparation")
    shot = Path(folder).expanduser().absolute()
    payload, bindings = _composed_payload(
        shot,
        (
            (row.unit_id, row.script_path, row.script_sha256)
            for row in guard.claim.unit_inputs
        ),
        evaluation_barrier=evaluation_barrier,
    )
    sha256 = hashlib.sha256(payload).hexdigest()
    if sha256 != guard.claim.layer_script_sha256:
        raise LayerArtifactPublicationConflict(
            "composed layer artifact SHA-256 does not match the proposed bytes "
            "bound by its finalization claim"
        )
    authority = f"layer-artifact:{guard.claim.claim_id}:{sha256}"

    def replace_if_needed(current: bytes | None) -> tuple[bytes | None, None]:
        if current == payload:
            return None, None
        return payload, None

    try:
        update = prepare_file_update(
            shot,
            guard.claim.layer_script_path,
            replace_if_needed,
            authority_binding=authority,
        )
    except FilePublicationConflict as exc:
        raise LayerArtifactPublicationConflict(str(exc)) from exc
    prepared = PreparedLayerArtifact(
        destination=shot / guard.claim.layer_script_path,
        relative=guard.claim.layer_script_path,
        sha256=sha256,
        source_bindings=bindings,
        publication=update.publication,
        authority_binding=authority,
    )
    _require_prepared_claim(prepared, guard)
    _require_sources_current(prepared)
    return prepared


def commit_layer_artifact(
    prepared: PreparedLayerArtifact,
    guard: LayerFinalizationClaimGuard,
) -> Path:
    """CAS-publish prepared bytes while the exact finalization claim is live."""

    if not isinstance(prepared, PreparedLayerArtifact):
        raise LayerArtifactPublicationConflict("layer artifact commit requires a typed preparation")
    _require_prepared_claim(prepared, guard)

    def commit() -> None:
        _require_prepared_claim(prepared, guard)
        _require_sources_current(prepared)
        if prepared.publication is not None:
            commit_prepared_file(
                prepared.publication,
                authority_binding=prepared.authority_binding,
            )

    try:
        guard.publish("publish composed layer artifact", commit)
        _require_prepared_claim(prepared, guard)
        _require_sources_current(prepared)
    except FilePublicationConflict as exc:
        raise LayerArtifactPublicationConflict(str(exc)) from exc
    try:
        published = read_trusted_file(
            guard.folder,
            prepared.destination,
            "published composed layer artifact",
            require_nonempty=True,
        )
    except TrustedFileError as exc:
        raise LayerArtifactPublicationConflict(str(exc)) from exc
    if hashlib.sha256(published.payload).hexdigest() != prepared.sha256:
        raise LayerArtifactPublicationConflict(
            "composed layer artifact bytes changed after publication"
        )
    return prepared.destination


def discard_layer_artifact(prepared: PreparedLayerArtifact) -> None:
    if isinstance(prepared, PreparedLayerArtifact):
        discard_prepared_file(prepared.publication)


__all__ = [
    "LayerArtifactPublicationConflict",
    "PreparedLayerArtifact",
    "commit_layer_artifact",
    "discard_layer_artifact",
    "prepare_layer_artifact",
    "proposed_layer_artifact_sha256",
]
