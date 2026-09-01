"""Selection-neutral source verification for terminal layer outcomes.

The terminal receipt owns an immutable outcome projection.  Historical
reconciliation may publish that projection under a preserved successor, but it
must never rebuild the projection against the successor's selected plan.  This
module instead reopens every source named by the sealed projection and validates
the live-derived manifest fields immediately before publication preparation.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path, PurePosixPath

from vfx_harness.domain.layer_finalizations import LayerFinalizationReceipt
from vfx_harness.domain.work_units import strict_topological_sparse_layer_ids
from vfx_harness.infrastructure.trusted_files import (
    TrustedFileError,
    TrustedFileNotFound,
    bind_trusted_file_absence,
    read_trusted_file,
)
from vfx_harness.orchestration import revalidation
from vfx_harness.orchestration.layer_outcome_paths import layer_outcome_path
from vfx_harness.orchestration.layer_terminal_sources import (
    capture_terminal_layer_sources,
)

_MANIFEST_FIELDS = frozenset(
    {
        "harness_version",
        "harness_files",
        "blender_version",
        "comparison",
        "models",
        "files",
        "runtime_checks",
        "prior_outcomes",
    }
)
_COMPARISON_FIELDS = frozenset({"mode", "scale"})


def _is_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _safe_locator(root: Path, value: object) -> Path | None:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "\\" in value
    ):
        return None
    locator = PurePosixPath(value)
    if (
        locator.is_absolute()
        or locator.as_posix() != value
        or value == "."
        or ".." in locator.parts
    ):
        return None
    path = root / Path(*locator.parts)
    return path if path.is_relative_to(root) else None


def _manifest_source_path(root: Path, locator: object, where: str) -> Path:
    if (
        not isinstance(locator, str)
        or not locator
        or locator != locator.strip()
        or "\\" in locator
    ):
        raise ValueError(f"{where} locator must be a non-empty canonical path")
    pure = PurePosixPath(locator)
    if pure.is_absolute() or pure.as_posix() != locator or ".." in pure.parts:
        raise ValueError(f"{where} locator is not canonical: {locator!r}")
    path = _safe_locator(root, locator)
    if path is None:
        raise ValueError(f"{where} locator escapes its source root: {locator!r}")
    return path


def _source_map(value: object, where: str) -> Mapping:
    if not isinstance(value, Mapping):
        raise ValueError(f"{where} must be an object of locator-to-digest bindings")
    return value


def _require_source_payload(
    trusted_root: Path,
    path: Path,
    expected: object,
    *,
    where: str,
    allow_absence: bool,
) -> bytes | None:
    if expected is None:
        if not allow_absence:
            raise ValueError(f"{where} requires an immutable SHA-256 digest")
    elif not _is_digest(expected):
        raise ValueError(f"{where} has an invalid immutable SHA-256 digest")
    try:
        snapshot = read_trusted_file(trusted_root, path, where)
    except TrustedFileNotFound:
        snapshot = None
        if expected is None:
            try:
                bind_trusted_file_absence(
                    trusted_root,
                    path,
                    where,
                )
            except TrustedFileError as exc:
                raise ValueError(
                    f"{where} absence is not trusted: {path}"
                ) from exc
    except TrustedFileError as exc:
        raise ValueError(f"{where} is not a trusted regular file: {path}") from exc
    payload = snapshot.payload if snapshot is not None else None
    observed = snapshot.sha256 if snapshot is not None else None
    if observed != expected:
        raise ValueError(
            f"{where} bytes changed or are missing: "
            f"expected={expected!r}, observed={observed!r}, path={path}"
        )
    return payload


def _runtime_text(payload: bytes | None) -> str | None:
    if payload is None:
        return None
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("sealed runtime-check source is not UTF-8 JSON") from exc


def _selected_layer_order(
    files: Mapping,
    payloads: Mapping[str, bytes | None],
    receipt: LayerFinalizationReceipt,
) -> tuple[str, ...]:
    locators = [
        locator
        for locator in files
        if isinstance(locator, str) and PurePosixPath(locator).name == "layers.json"
    ]
    if len(locators) != 1:
        raise ValueError(
            "sealed layer-outcome manifest must bind exactly one selected layers.json"
        )
    payload = payloads[locators[0]]
    if payload is None:
        raise ValueError("sealed selected layers.json cannot be absent")
    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("sealed selected layers.json is not valid UTF-8 JSON") from exc
    rows = document.get("layers") if isinstance(document, Mapping) else None
    if not isinstance(rows, list) or not rows or any(
        not isinstance(row, Mapping) for row in rows
    ):
        raise ValueError("sealed selected layers.json has no typed layer rows")
    order = strict_topological_sparse_layer_ids(rows)
    prefix = (
        *(row.layer_id for row in receipt.claim.predecessor_inputs),
        receipt.claim.layer_id,
    )
    if order[: len(prefix)] != prefix:
        raise ValueError(
            "terminal receipt predecessor prefix does not match its sealed layers.json"
        )
    return order


def _verify_manifest_values(
    manifest: Mapping,
    *,
    receipt: LayerFinalizationReceipt,
    runtime_text: str | None,
    layer_order: tuple[str, ...],
) -> None:
    if set(manifest) != _MANIFEST_FIELDS:
        raise ValueError(
            "sealed layer-outcome manifest fields mismatch; "
            f"missing={sorted(_MANIFEST_FIELDS - set(manifest))}; "
            f"unexpected={sorted(set(manifest) - _MANIFEST_FIELDS)}"
        )
    if manifest.get("harness_version") != revalidation.HARNESS_VERSION:
        raise ValueError("sealed layer-outcome harness version is stale")
    if manifest.get("models") != revalidation.current_model_identity():
        raise ValueError("sealed layer-outcome model settings are stale")
    comparison = manifest.get("comparison")
    if not isinstance(comparison, Mapping) or set(comparison) != _COMPARISON_FIELDS:
        raise ValueError("sealed layer-outcome comparison settings are malformed")
    if comparison != {
        "mode": revalidation.DEFAULT_COMPARISON_MODE,
        "scale": revalidation.DEFAULT_COMPARISON_SCALE,
    }:
        raise ValueError("sealed layer-outcome comparison settings are stale")
    expected_blender = str(receipt.projection["blender_version"])
    if manifest.get("blender_version") != expected_blender:
        raise ValueError(
            "sealed layer-outcome Blender version does not match terminal authority"
        )

    prefix = (
        *(row.layer_id for row in receipt.claim.predecessor_inputs),
        receipt.claim.layer_id,
    )
    expected_runtime_digest = (
        None
        if runtime_text is None
        else revalidation.runtime_checks_digest_from_text(
            runtime_text,
            frozenset(prefix),
            frozenset(layer_order),
        )
    )
    if manifest.get("runtime_checks") != expected_runtime_digest:
        raise ValueError(
            "sealed layer-outcome runtime_checks projection does not match raw source"
        )


def _verify_harness_files(
    manifest: Mapping,
    *,
    paths: set[Path],
) -> None:
    package = Path(revalidation.__file__).absolute().parents[1]
    harness_files = _source_map(
        manifest.get("harness_files"),
        "sealed layer-outcome harness files",
    )
    expected_paths = {
        path.relative_to(package).as_posix(): path
        for path in revalidation.harness_identity_paths(package)
    }
    if set(harness_files) != set(expected_paths):
        raise ValueError(
            "sealed layer-outcome harness source closure is incomplete or stale; "
            f"missing={sorted(set(expected_paths) - set(harness_files))}; "
            f"unexpected={sorted(set(harness_files) - set(expected_paths))}"
        )
    for locator, path in expected_paths.items():
        expected = harness_files[locator]
        _require_source_payload(
            package,
            path,
            expected,
            where=f"sealed layer-outcome harness file {locator!r}",
            allow_absence=True,
        )
        paths.add(path)


def verify_sealed_layer_outcome_sources(
    folder: str | Path,
    outcome: Mapping,
    *,
    finalization_receipt: LayerFinalizationReceipt,
) -> tuple[Path, ...]:
    """Reopen the exact terminal projection's complete causal source closure."""

    if not isinstance(finalization_receipt, LayerFinalizationReceipt):
        raise ValueError(
            "sealed layer-outcome source verification requires a typed terminal receipt"
        )
    if not isinstance(outcome, Mapping):
        raise ValueError("sealed layer-outcome source verification requires an object")
    root = Path(folder).expanduser().absolute()
    manifest = outcome.get("revalidation_manifest")
    if not isinstance(manifest, Mapping):
        raise ValueError("sealed layer-outcome revalidation_manifest must be an object")
    if set(manifest) != _MANIFEST_FIELDS:
        _verify_manifest_values(
            manifest,
            receipt=finalization_receipt,
            runtime_text=None,
            layer_order=(),
        )

    paths: set[Path] = set()
    runtime_path = root / "runtime_checks.json"
    revalidation_projection = finalization_receipt.projection["revalidation"]
    replacement_text = revalidation_projection.get("replacement_text")
    expected_runtime_sha256 = (
        revalidation_projection.get("replacement_sha256")
        if replacement_text is not None
        else revalidation_projection.get("source_sha256")
    )
    runtime_payload = _require_source_payload(
        root,
        runtime_path,
        expected_runtime_sha256,
        where="sealed layer-outcome runtime-check source",
        allow_absence=True,
    )
    paths.add(runtime_path)

    files = _source_map(
        manifest.get("files"),
        "sealed layer-outcome manifest files",
    )
    file_payloads: dict[str, bytes | None] = {}
    for locator, expected in files.items():
        path = _manifest_source_path(
            root,
            locator,
            "sealed layer-outcome manifest file",
        )
        payload = _require_source_payload(
            root,
            path,
            expected,
            where=f"sealed layer-outcome manifest file {locator!r}",
            allow_absence=True,
        )
        assert isinstance(locator, str)
        file_payloads[locator] = payload
        paths.add(path)

    layer_order = _selected_layer_order(
        files,
        file_payloads,
        finalization_receipt,
    )
    _verify_manifest_values(
        manifest,
        receipt=finalization_receipt,
        runtime_text=_runtime_text(runtime_payload),
        layer_order=layer_order,
    )
    _verify_harness_files(manifest, paths=paths)

    prior_outcomes = _source_map(
        manifest.get("prior_outcomes"),
        "sealed layer-outcome prior outcomes",
    )
    expected_prior_ids = tuple(
        row.layer_id for row in finalization_receipt.claim.predecessor_inputs
    )
    if set(prior_outcomes) != set(expected_prior_ids):
        raise ValueError(
            "sealed layer-outcome prior-outcome closure does not match the terminal "
            "receipt predecessor prefix"
        )
    for layer_id, expected in prior_outcomes.items():
        if not isinstance(layer_id, str) or not layer_id or layer_id != layer_id.strip():
            raise ValueError("sealed layer-outcome prior outcome id must be trimmed text")
        path = layer_outcome_path(root, layer_id)
        _require_source_payload(
            root,
            path,
            expected,
            where=f"sealed prior-layer outcome {layer_id!r}",
            allow_absence=False,
        )
        paths.add(path)

    canonical = outcome.get("canonical")
    if not isinstance(canonical, list):
        raise ValueError("sealed layer-outcome canonical projection must be a list")
    canonical_render_digests: dict[str, str] = {}
    for index, row in enumerate(canonical):
        if not isinstance(row, Mapping):
            raise ValueError(f"sealed layer-outcome canonical[{index}] must be an object")
        frame = row.get("frame")
        reference = _safe_locator(root, row.get("ref"))
        if reference is None:
            raise ValueError(
                f"sealed layer-outcome canonical[{index}] reference locator is unsafe"
            )
        _require_source_payload(
            root,
            reference,
            row.get("ref_sha256"),
            where=f"sealed layer-outcome canonical[{index}] reference at frame {frame}",
            allow_absence=False,
        )
        paths.add(reference)
        if row.get("evidence_kind") != "render":
            continue
        render_locator = row.get("render")
        render = _safe_locator(root, render_locator)
        if render is None:
            raise ValueError(
                f"sealed layer-outcome canonical[{index}] render locator is unsafe"
            )
        render_sha256 = row.get("render_sha256")
        _require_source_payload(
            root,
            render,
            render_sha256,
            where=f"sealed layer-outcome canonical[{index}] render at frame {frame}",
            allow_absence=False,
        )
        capture = row.get("render_capture")
        capture_reasons = revalidation.render_capture_reasons(
            capture,
            frame=frame,
            render_sha256=str(render_sha256),
        )
        if capture_reasons:
            raise ValueError(
                f"sealed layer-outcome canonical[{index}] render capture is stale: "
                + "; ".join(capture_reasons)
            )
        assert isinstance(render_locator, str)
        assert isinstance(render_sha256, str)
        previous = canonical_render_digests.setdefault(render_locator, render_sha256)
        if previous != render_sha256:
            raise ValueError(
                f"sealed layer outcome binds conflicting digests for {render_locator!r}"
            )
        paths.add(render)

    best = outcome.get("best")
    best_render = best.get("render") if isinstance(best, Mapping) else None
    if best_render is not None:
        if not isinstance(best_render, str) or best_render not in canonical_render_digests:
            raise ValueError(
                "sealed layer-outcome best render has no canonical immutable digest"
            )
        best_path = _safe_locator(root, best_render)
        assert best_path is not None
        paths.add(best_path)

    terminal_sources = capture_terminal_layer_sources(root, finalization_receipt)
    paths.add(terminal_sources.stored_evaluation.source_binding.path)
    paths.add(terminal_sources.layer_script.binding.path)
    paths.update(
        replay.source_binding.path for replay in terminal_sources.stored_replays
    )
    paths.update(
        binding.path
        for binding in (
            *terminal_sources.replay_source_bindings,
            *terminal_sources.observation_source_bindings,
        )
    )
    return tuple(sorted(paths, key=lambda path: str(path)))


__all__ = ["verify_sealed_layer_outcome_sources"]
