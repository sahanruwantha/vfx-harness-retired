"""Deterministic eligibility and sealing for passed-layer revalidation.

A fast path is safe only when every input that could change the pixels or the verdict is
identical to the sealed pass.  This module owns that cache key; the builder owns replaying
Blender and evaluating the resulting evidence.
"""

from __future__ import annotations

import hashlib
import json
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from vfx_harness.domain.work_units import read_document
from vfx_harness.infrastructure.config import Settings
from vfx_harness.orchestration.layer_plans import global_plan_path, work_unit_plan_path

OUTCOME_SCHEMA = 2
try:
    HARNESS_VERSION = version("vfx-harness")
except PackageNotFoundError:
    HARNESS_VERSION = "source-tree"


def digest(path: str | Path) -> str | None:
    path = Path(path)
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _layers(folder: Path) -> list[dict]:
    try:
        rows = read_document(folder / "layers.json")
    except (OSError, ValueError):
        return []
    return rows


def _prior_outcome_digests(folder: Path, current: int) -> dict[str, str | None]:
    out = {}
    for layer_id in range(1, current):
        path = folder / "plans" / "outcomes" / f"{layer_id:02d}.json"
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            out[str(layer_id)] = None
            continue
        # A later REVALIDATE updates only this audit field.  It must not invalidate every
        # downstream layer when the sealed inputs and pixels remained identical.
        stable = {
            key: row.get(key)
            for key in (
                "schema",
                "layer",
                "script",
                "status",
                "authoritative_total",
                "authoritative_passed",
                "failed_contracts",
                "revalidation_manifest",
                "canonical",
            )
        }
        payload = json.dumps(stable, sort_keys=True, separators=(",", ":")).encode()
        out[str(layer_id)] = hashlib.sha256(payload).hexdigest()
    return out


def _runtime_checks_digest(folder: Path, current: int) -> str | None:
    path = folder / "runtime_checks.json"
    if not path.is_file():
        return None
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return "invalid"
    selected = []
    for row in rows if isinstance(rows, list) else []:
        try:
            if int(row.get("layer")) <= current:
                selected.append(row)
        except (TypeError, ValueError):
            selected.append(row)  # malformed evidence must invalidate, not disappear
    payload = json.dumps(selected, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def input_manifest(
    folder: str | Path, layer, *, blender_version: str, comparison_mode: str = "eevee", comparison_scale: float = 0.5
) -> dict:
    """Hash the complete deterministic boundary for one layer."""
    root = Path(folder)
    current = int(layer.id)
    paths = [
        root / "brief.md",
        global_plan_path(root),
        root / "layers.json",
        root / "acceptance.json",
        root / "critic_axes.json",
        root / "checks.json",
        root / "scene_checks.json",
        root / "plan_amendments.jsonl",
    ]
    paths.extend(work_unit_plan_path(root, unit) for unit in layer.stages)
    for row in _layers(root):
        try:
            if int(row.get("id")) <= current:
                paths.append(root / str(row.get("script")))
        except (TypeError, ValueError):
            continue
    for _frame, ref in getattr(layer, "judges", ()):
        paths.append(root / ref)
    files = {}
    for path in paths:
        try:
            rel = str(path.relative_to(root))
        except ValueError:
            rel = str(path)
        files[rel] = digest(path)
    package = Path(__file__).resolve().parents[1]
    settings = Settings.from_environment(load_dotenv_file=False)
    harness_files = {
        str(path.relative_to(package)): digest(path)
        for path in (
            package / "orchestration" / "revalidation.py",
            package / "evidence" / "checks.py",
            package / "evidence" / "scene_checks.py",
            package / "agents" / "builder.py",
            package / "blender" / "session.py",
            package / "blender" / "worker.py",
            package / "blender" / "render_ext.py",
        )
    }
    return {
        "harness_version": HARNESS_VERSION,
        "harness_files": dict(sorted(harness_files.items())),
        "blender_version": str(blender_version),
        "comparison": {"mode": comparison_mode, "scale": comparison_scale},
        "models": {
            "builder": settings.builder_model,
            "script": settings.script_model,
            "critic": settings.critic_model,
            "reviewer": settings.reviewer_model,
        },
        "files": dict(sorted(files.items())),
        "runtime_checks": _runtime_checks_digest(root, current),
        "prior_outcomes": _prior_outcome_digests(root, current),
    }


def canonical_records(folder: str | Path, layer, canonical: list) -> list[dict]:
    root = Path(folder)
    records = []
    for (frame, ref), verdict in canonical:
        rel = (
            f"renders/{layer.id}_canonical.png"
            if len(canonical) == 1
            else f"renders/{layer.id}_canonical_f{int(frame)}.png"
        )
        evidence = verdict.get("evidence") or []
        records.append(
            {
                "frame": int(frame),
                "ref": str(ref),
                "ref_sha256": digest(root / ref),
                "render": rel,
                "render_sha256": digest(root / rel),
                "authoritative": [
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
                    if row.get("authoritative")
                ],
                "qualitative_defects": list(verdict.get("issues") or []),
            }
        )
    return records


def eligibility(outcome: dict, current_manifest: dict, folder: str | Path) -> tuple[bool, list[str]]:
    """Explain whether a sealed outcome is eligible for deterministic replay."""
    reasons = []
    if outcome.get("schema") != OUTCOME_SCHEMA:
        reasons.append(f"outcome schema must be {OUTCOME_SCHEMA}")
    if outcome.get("status") != "passed":
        reasons.append("prior outcome is not passed")
    if outcome.get("revalidation_manifest") != current_manifest:
        reasons.append("input manifest changed")
    canonical = outcome.get("canonical") or []
    if not canonical:
        reasons.append("sealed canonical records are missing")
    root = Path(folder)
    for row in canonical:
        render = root / str(row.get("render", ""))
        if digest(render) != row.get("render_sha256"):
            reasons.append(f"sealed canonical changed or is missing for f{row.get('frame')}")
        ref = root / str(row.get("ref", ""))
        if digest(ref) != row.get("ref_sha256"):
            reasons.append(f"reference changed or is missing for f{row.get('frame')}")
        if row.get("qualitative_defects"):
            reasons.append(f"sealed outcome retains a qualitative defect at f{row.get('frame')}")
    return not reasons, reasons
