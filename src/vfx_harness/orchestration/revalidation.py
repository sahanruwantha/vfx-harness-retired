"""Deterministic eligibility and sealing for passed-layer revalidation.

A fast path is safe only when every input that could change the pixels or the verdict is
identical to the sealed pass.  This module owns that cache key; the builder owns replaying
Blender and evaluating the resulting evidence.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path, PurePosixPath

from vfx_harness.domain import evidence_authority
from vfx_harness.domain.layer_outcome_projections import CANONICAL_EVIDENCE_KINDS
from vfx_harness.domain.layer_outcomes import OUTCOME_SCHEMA
from vfx_harness.domain.stop_envelope_primitives import canonical_digest
from vfx_harness.domain.work_units import (
    read_document,
    strict_topological_sparse_layer_ids,
)
from vfx_harness.infrastructure.config import Settings
from vfx_harness.orchestration import layer_publication
from vfx_harness.orchestration.authority_selection import (
    ResolvedSelectedAuthority,
    resolve_selected_authority,
)
from vfx_harness.orchestration.layer_plans import work_unit_plan_path
from vfx_harness.orchestration.ledger import Layer, load_layers_from_path
from vfx_harness.orchestration.plan_authority import (
    selected_artifact_path,
)

try:
    HARNESS_VERSION = version("vfx-harness")
except PackageNotFoundError:
    HARNESS_VERSION = "source-tree"


RENDER_CAPTURE_SCHEMA = "vfx-harness.canonical-render-capture/v1"
DEFAULT_COMPARISON_MODE = "eevee"
DEFAULT_COMPARISON_SCALE = 0.5
_CANONICAL_COMMON_FIELDS = frozenset(
    {
        "evidence_kind",
        "frame",
        "ref",
        "ref_sha256",
        "input_manifest_sha256",
        "authoritative",
        "authoritative_sha256",
        "qualitative_defects",
    }
)
_CANONICAL_RENDER_FIELDS = frozenset(
    {"render", "render_sha256", "render_capture"}
)
_RENDER_CAPTURE_FIELDS = frozenset(
    {
        "schema",
        "frame",
        "mode",
        "scale",
        "resolution",
        "render_state",
        "warnings",
        "png_sha256",
        "capture_digest",
    }
)


def digest(path: str | Path) -> str | None:
    path = Path(path)
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


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
    path = (root / Path(*locator.parts)).resolve()
    return path if path.is_relative_to(root.resolve()) else None


def render_capture_reasons(
    value: object,
    *,
    frame: int,
    render_sha256: str,
) -> list[str]:
    reasons: list[str] = []
    if not isinstance(value, Mapping) or set(value) != _RENDER_CAPTURE_FIELDS:
        return ["render capture fields do not match the producer schema"]
    if value.get("schema") != RENDER_CAPTURE_SCHEMA:
        reasons.append(f"render capture schema must be {RENDER_CAPTURE_SCHEMA}")
    if value.get("frame") != frame:
        reasons.append("render capture frame is stale")
    mode = value.get("mode")
    if not isinstance(mode, str) or not mode or mode != mode.strip():
        reasons.append("render capture mode is missing")
    scale = value.get("scale")
    if (
        not isinstance(scale, (int, float))
        or isinstance(scale, bool)
        or not math.isfinite(float(scale))
        or float(scale) <= 0
    ):
        reasons.append("render capture scale must be a positive finite number")
    resolution = value.get("resolution")
    if (
        not isinstance(resolution, list)
        or len(resolution) != 3
        or not all(
            isinstance(item, int) and not isinstance(item, bool) and item > 0
            for item in resolution
        )
    ):
        reasons.append("render capture resolution must contain three positive integers")
    render_state = value.get("render_state")
    if not isinstance(render_state, Mapping) or not render_state:
        reasons.append("render capture has no settings state")
    warnings = value.get("warnings")
    if not isinstance(warnings, list) or any(not isinstance(item, str) for item in warnings):
        reasons.append("render capture warnings must be strings")
    png_sha256 = value.get("png_sha256")
    if not _is_digest(png_sha256) or png_sha256 != render_sha256:
        reasons.append("render capture PNG digest is missing or stale")
    capture_digest = value.get("capture_digest")
    payload = {key: item for key, item in value.items() if key != "capture_digest"}
    try:
        expected_capture_digest = canonical_digest(payload)
    except ValueError:
        expected_capture_digest = None
    if not _is_digest(capture_digest) or capture_digest != expected_capture_digest:
        reasons.append("render capture receipt digest is missing or stale")
    return reasons


def _layers(folder: Path, selected_authority=None) -> list[dict]:
    if selected_authority is None:
        path = selected_artifact_path(folder, "layers.json")
    elif selected_authority.plan is None:
        path = folder / "layers.json"
    else:
        path = selected_authority.artifact_paths["layers.json"]
    try:
        rows = read_document(path)
    except (OSError, ValueError):
        return []
    return rows


def _global_layers(folder: Path, selected_authority=None) -> list[dict]:
    if selected_authority is None:
        path = selected_artifact_path(folder, "layers.json")
    elif selected_authority.plan is None:
        path = folder / "layers.json"
    else:
        path = selected_authority.plan.bundle.root / "layers.json"
    rows = read_document(path)
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError("selected global layer DAG must contain layer objects")
    return rows


def _prior_outcome_digests(
    folder: Path,
    prior_layers: tuple[Layer, ...],
    selected_authority: ResolvedSelectedAuthority,
) -> dict[str, str]:
    """Bind revalidation only to complete current predecessor publications."""

    out: dict[str, str] = {}
    for layer in prior_layers:
        layer_id = str(layer.id)
        try:
            publication = layer_publication.require_current_layer_publication(
                folder,
                layer,
                selected_authority,
            )
        except layer_publication.LayerPublicationConflict as exc:
            raise ValueError(
                f"revalidation cannot consume prior layer {layer_id} without a current "
                f"receipt-backed publication: {exc}. Reconcile that layer's exact "
                "terminal receipt projections before revalidation"
            ) from exc
        out[layer_id] = publication.outcome_sha256
    return out


def runtime_checks_digest_from_text(
    runtime_checks_text: str,
    included_layer_ids: frozenset[str],
    known_layer_ids: frozenset[str],
) -> str:
    """Project raw runtime-check bytes with the manifest's prefix semantics."""

    try:
        rows = json.loads(runtime_checks_text)
    except json.JSONDecodeError:
        return "invalid"
    selected = []
    for row in rows if isinstance(rows, list) else []:
        layer_id = row.get("layer") if isinstance(row, Mapping) else None
        if (
            not isinstance(layer_id, str)
            or not layer_id
            or layer_id != layer_id.strip()
            or layer_id in included_layer_ids
            or layer_id not in known_layer_ids
        ):
            # Malformed and unknown evidence must invalidate, not disappear.  A known
            # successor is the only row outside the current replay prefix.
            selected.append(row)
    payload = json.dumps(selected, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _runtime_checks_digest(
    folder: Path,
    included_layer_ids: frozenset[str],
    known_layer_ids: frozenset[str],
    *,
    runtime_checks_text: str | None = None,
) -> str | None:
    path = folder / "runtime_checks.json"
    if runtime_checks_text is None and not path.is_file():
        return None
    return runtime_checks_digest_from_text(
        path.read_text(encoding="utf-8")
        if runtime_checks_text is None
        else runtime_checks_text,
        included_layer_ids,
        known_layer_ids,
    )


def harness_identity_paths(package: Path) -> tuple[Path, ...]:
    """Hash files, expanding packages so a split cannot drop identity members."""
    roots = (
        package / "orchestration" / "revalidation.py",
        package / "orchestration" / "layer_outcome_source_verification.py",
        package / "evidence" / "checks.py",
        package / "evidence" / "scene_checks",
        package / "agents" / "builder",
        package / "blender" / "session.py",
        package / "blender" / "worker.py",
        package / "blender" / "render_ext.py",
    )
    paths: list[Path] = []
    for root in roots:
        if root.is_dir():
            paths.extend(sorted(path for path in root.rglob("*.py") if "__pycache__" not in path.parts))
        else:
            paths.append(root)
    return tuple(paths)


def current_model_identity() -> dict[str, str]:
    """Return the exact live model settings that participate in revalidation."""

    settings = Settings.from_environment(load_dotenv_file=False)
    return {
        "builder": settings.builder_model,
        "script": settings.script_model,
        "critic": settings.critic_model,
        "reviewer": settings.reviewer_model,
    }


def input_manifest(
    folder: str | Path,
    layer,
    *,
    blender_version: str,
    comparison_mode: str = DEFAULT_COMPARISON_MODE,
    comparison_scale: float = DEFAULT_COMPARISON_SCALE,
    selected_authority: ResolvedSelectedAuthority | None = None,
    runtime_checks_text: str | None = None,
) -> dict:
    """Hash the complete deterministic boundary for one layer.

    ``runtime_checks_text`` projects an already prepared, receipt-bound
    replacement without publishing it.  This lets terminal authority seal the
    exact post-reconciliation manifest while mutable runtime-check bytes remain
    untouched until after terminal commit.
    """

    if runtime_checks_text is not None and not isinstance(runtime_checks_text, str):
        raise ValueError("runtime_checks_text must be a string or null")
    root = Path(folder).expanduser().resolve()
    selected_authority = (
        resolve_selected_authority(root)
        if selected_authority is None
        else selected_authority
    )
    current_id = str(layer.id)
    global_layers = _global_layers(root, selected_authority)
    selected_layers = _layers(root, selected_authority)
    order = strict_topological_sparse_layer_ids(global_layers)
    selected_by_id = {
        str(row.get("id") or "").strip(): row
        for row in selected_layers
        if isinstance(row, Mapping) and str(row.get("id") or "").strip()
    }
    if (
        len(selected_by_id) != len(selected_layers)
        or set(selected_by_id) != set(order)
        or current_id not in selected_by_id
    ):
        raise ValueError(
            "selected executable layer view does not match the exact global DAG while "
            f"deriving the replay prefix for layer {current_id!r}"
        )
    current_index = order.index(current_id)
    prefix_ids = tuple(order[: current_index + 1])
    prefix_rows = [selected_by_id[layer_id] for layer_id in prefix_ids]
    prior_ids = prefix_ids[:-1]
    prior_layers: tuple[Layer, ...] = ()
    if prior_ids:
        selected_layers_path = (
            root / "layers.json"
            if selected_authority.plan is None
            else selected_authority.artifact_paths["layers.json"]
        )
        typed_layers = load_layers_from_path(selected_layers_path)
        if set(typed_layers) != set(order):
            raise ValueError(
                "selected typed layer authority does not match the exact global DAG "
                "while deriving the replay prefix"
            )
        prior_layers = tuple(typed_layers[layer_id] for layer_id in prior_ids)
    paths = [
        root / "brief.md",
        (
            selected_authority.plan.bundle.root / "global.md"
            if selected_authority.plan is not None
            else root / "plans" / "global.md"
        ),
        root / "plan_amendments.jsonl",
    ]
    selected_names = (
        "layers.json",
        "acceptance.json",
        "critic_axes.json",
        "checks.json",
        "scene_checks.json",
    )
    paths.extend(
        selected_authority.artifact_paths[name]
        if selected_authority.plan is not None
        else root / name
        for name in selected_names
    )
    if selected_authority.plan is not None:
        paths.extend(
            selected_authority.artifact_paths[name]
            for name in (
                "requirements.json",
                "obligations.json",
                "assumptions.json",
                "plan.provenance.json",
            )
        )
    paths.extend(
        work_unit_plan_path(
            root,
            unit,
            selected_authority=selected_authority,
        )
        for unit in layer.stages
    )
    for row in prefix_rows:
        script = row.get("script")
        if not isinstance(script, str) or not script.strip():
            raise ValueError(
                f"selected replay prefix layer {row.get('id')!r} has no script locator"
            )
        paths.append(root / script)
    for _frame, ref in getattr(layer, "judges", ()):
        paths.append(root / ref)
    files = {}
    for path in paths:
        try:
            rel = str(path.relative_to(root))
        except ValueError as exc:
            raise ValueError(
                f"input manifest source escapes the shot root: {path}"
            ) from exc
        files[rel] = digest(path)
    package = Path(__file__).resolve().parents[1]
    harness_files = {
        str(path.relative_to(package)): digest(path)
        for path in harness_identity_paths(package)
    }
    return {
        "harness_version": HARNESS_VERSION,
        "harness_files": dict(sorted(harness_files.items())),
        "blender_version": str(blender_version),
        "comparison": {"mode": comparison_mode, "scale": comparison_scale},
        "models": current_model_identity(),
        "files": dict(sorted(files.items())),
        "runtime_checks": _runtime_checks_digest(
            root,
            frozenset(prefix_ids),
            frozenset(order),
            runtime_checks_text=runtime_checks_text,
        ),
        "prior_outcomes": _prior_outcome_digests(
            root,
            prior_layers,
            selected_authority,
        ),
    }


def _authoritative_projection(verdict: Mapping) -> list[dict]:
    evidence = verdict.get("evidence") or []
    if not isinstance(evidence, list) or any(not isinstance(row, Mapping) for row in evidence):
        raise ValueError("canonical verdict evidence must be a list of objects")
    return [
        {
            key: row.get(key)
            for key in (
                "id",
                "metric",
                "value",
                "target",
                "pass",
                "source",
                "owner_layer",
                "fault_owner",
                "activates_at",
                "lifecycle",
            )
        }
        for row in evidence
        # The sealed record keeps every typed measurement a claim can bind, pass or
        # fail. Filtering on autonomy dropped the bound image rows a look unit is
        # judged on, so the record omitted the evidence it was sealed for (HIR-0205).
        if evidence_authority.is_recorded_evidence(row)
    ]


def _canonical_record_reasons(
    row: object,
    *,
    root: Path,
    manifest_sha256: str,
    allow_qualitative_defects: bool = False,
) -> list[str]:
    if not isinstance(row, Mapping):
        return ["sealed canonical row is not an object"]
    kind = row.get("evidence_kind")
    expected_fields = (
        _CANONICAL_COMMON_FIELDS | _CANONICAL_RENDER_FIELDS
        if kind == "render"
        else _CANONICAL_COMMON_FIELDS
    )
    if kind not in CANONICAL_EVIDENCE_KINDS:
        return ["sealed canonical evidence_kind is missing or unsupported"]
    if set(row) != expected_fields:
        return [f"sealed {kind} canonical fields do not match the producer schema"]

    reasons: list[str] = []
    frame = row.get("frame")
    if not isinstance(frame, int) or isinstance(frame, bool):
        reasons.append("sealed canonical frame is not an integer")
        frame = -1
    if not _is_digest(row.get("input_manifest_sha256")):
        reasons.append(f"sealed canonical input-manifest digest is missing for f{frame}")
    elif row["input_manifest_sha256"] != manifest_sha256:
        reasons.append(f"sealed canonical input-manifest digest is stale for f{frame}")

    ref = _safe_locator(root, row.get("ref"))
    ref_sha256 = row.get("ref_sha256")
    if ref is None:
        reasons.append(f"sealed reference locator is missing or unsafe for f{frame}")
    if not _is_digest(ref_sha256):
        reasons.append(f"sealed reference digest is missing for f{frame}")
    elif ref is None or digest(ref) != ref_sha256:
        reasons.append(f"reference changed or is missing for f{frame}")

    authoritative = row.get("authoritative")
    if not isinstance(authoritative, list) or any(
        not isinstance(item, Mapping) for item in authoritative
    ):
        reasons.append(f"sealed authoritative evidence is malformed for f{frame}")
    else:
        try:
            evidence_sha256 = canonical_digest({"authoritative": authoritative})
        except ValueError:
            evidence_sha256 = None
        if (
            not _is_digest(row.get("authoritative_sha256"))
            or row.get("authoritative_sha256") != evidence_sha256
        ):
            reasons.append(f"sealed authoritative evidence digest is missing or stale for f{frame}")
        if kind == "executable_only" and not authoritative:
            reasons.append(f"executable-only canonical has no typed evidence for f{frame}")

    defects = row.get("qualitative_defects")
    if not isinstance(defects, list) or any(not isinstance(item, str) for item in defects):
        reasons.append(f"sealed qualitative defects are malformed for f{frame}")
    elif defects and not allow_qualitative_defects:
        reasons.append(f"sealed outcome retains a qualitative defect at f{frame}")

    if kind == "render":
        render = _safe_locator(root, row.get("render"))
        render_sha256 = row.get("render_sha256")
        if render is None:
            reasons.append(f"sealed canonical render locator is missing or unsafe for f{frame}")
        if not _is_digest(render_sha256):
            reasons.append(f"sealed canonical render digest is missing for f{frame}")
        elif render is None or digest(render) != render_sha256:
            reasons.append(f"sealed canonical changed or is missing for f{frame}")
        if _is_digest(render_sha256):
            reasons.extend(
                f"{reason} for f{frame}"
                for reason in render_capture_reasons(
                    row.get("render_capture"),
                    frame=frame,
                    render_sha256=render_sha256,
                )
            )
    return reasons


def canonical_records(
    folder: str | Path,
    layer,
    canonical: list,
    *,
    input_manifest_sha256: str,
    allow_qualitative_defects: bool = False,
) -> list[dict]:
    """Seal terminal canonical evidence without inventing raster locators.

    A failed terminal finalization must retain its observed qualitative defects so
    reconciliation can publish one immutable failure source.  Passed-outcome sealing
    and later eligibility keep the stricter no-defect rule.
    """

    if not _is_digest(input_manifest_sha256):
        raise ValueError("canonical input_manifest_sha256 must be a lowercase SHA-256 digest")
    root = Path(folder).resolve()
    records = []
    for (frame, ref), verdict in canonical:
        if not isinstance(verdict, Mapping):
            raise ValueError(f"layer {layer.id} canonical f{frame} verdict must be an object")
        kind = verdict.get("evidence_kind")
        if kind is None and verdict.get("pass") is not True:
            # A script/scope failure can occur before any canonical observation exists.
            # Its outcome remains failed and ineligible; do not fabricate an observation.
            continue
        if kind not in CANONICAL_EVIDENCE_KINDS:
            raise ValueError(
                f"layer {layer.id} canonical f{frame} has no producer-authored evidence_kind"
            )
        ref_path = _safe_locator(root, ref)
        ref_sha256 = digest(ref_path) if ref_path is not None else None
        if ref_path is None or not _is_digest(ref_sha256):
            raise ValueError(
                f"layer {layer.id} canonical f{frame} reference locator/bytes are missing or unsafe"
            )
        authoritative = _authoritative_projection(verdict)
        record = {
            "evidence_kind": kind,
            "frame": int(frame),
            "ref": str(ref),
            "ref_sha256": ref_sha256,
            "input_manifest_sha256": input_manifest_sha256,
            "authoritative": authoritative,
            "authoritative_sha256": canonical_digest(
                {"authoritative": authoritative}
            ),
            "qualitative_defects": list(verdict.get("issues") or []),
        }
        if kind == "render":
            render_rel = verdict.get("render")
            render_path = _safe_locator(root, render_rel)
            render_sha256 = digest(render_path) if render_path is not None else None
            if render_path is None or not _is_digest(render_sha256):
                raise ValueError(
                    f"layer {layer.id} canonical f{frame} render locator/bytes are missing or unsafe"
                )
            record.update(
                render=str(render_rel),
                render_sha256=render_sha256,
                render_capture=verdict.get("render_capture"),
            )
        elif any(key in verdict for key in _CANONICAL_RENDER_FIELDS):
            raise ValueError(
                f"layer {layer.id} executable-only canonical f{frame} cannot carry raster fields"
            )
        reasons = _canonical_record_reasons(
            record,
            root=root,
            manifest_sha256=input_manifest_sha256,
            allow_qualitative_defects=allow_qualitative_defects,
        )
        if reasons:
            raise ValueError("; ".join(reasons))
        records.append(record)
    return records


def eligibility(outcome: dict, current_manifest: dict, folder: str | Path) -> tuple[bool, list[str]]:
    """Explain whether a sealed outcome is eligible for deterministic replay."""
    reasons: list[str] = []
    if outcome.get("schema") != OUTCOME_SCHEMA:
        reasons.append(f"outcome schema must be {OUTCOME_SCHEMA}")
    if outcome.get("status") != "passed":
        reasons.append("prior outcome is not passed")
    if outcome.get("revalidation_manifest") != current_manifest:
        reasons.append("input manifest changed")
    try:
        manifest_sha256 = canonical_digest(current_manifest)
    except (TypeError, ValueError):
        manifest_sha256 = ""
        reasons.append("current input manifest is not canonical JSON")
    canonical = outcome.get("canonical")
    if not isinstance(canonical, list) or not canonical:
        reasons.append("sealed canonical records are missing")
        canonical = []
    root = Path(folder).resolve()
    frames: set[int] = set()
    for row in canonical:
        reasons.extend(
            _canonical_record_reasons(
                row,
                root=root,
                manifest_sha256=manifest_sha256,
            )
        )
        if isinstance(row, Mapping):
            frame = row.get("frame")
            if isinstance(frame, int) and not isinstance(frame, bool):
                if frame in frames:
                    reasons.append(f"sealed canonical frame {frame} is duplicated")
                frames.add(frame)
    return not reasons, reasons
