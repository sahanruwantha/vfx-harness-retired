"""Strict hierarchical planning artifacts.

The global plan is a dependency map, not an execution prompt. Builders consume exactly
one schema-declared work-unit plan plus machine-readable contracts and prior outcomes. There
is deliberately no fallback to ``plan.md``: silently accepting a giant legacy plan would
keep the stale-context failure mode alive behind a compatibility branch.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path

from vfx_harness.domain.layer_outcomes import OUTCOME_SCHEMA
from vfx_harness.domain.work_units import strict_topological_sparse_layer_ids
from vfx_harness.observability import run_artifacts
from vfx_harness.observability.provenance import atomic_write
from vfx_harness.orchestration.layer_outcome_paths import layer_outcome_path

PLAN_DIR = "plans"
GLOBAL_PLAN = "global.md"
AMENDMENTS = "plan_amendments.jsonl"
# v2 adds gate attestation: a shot-root unit plan is authority only when a clean
# deterministic gate published it. Run 20260824T103842Z-afec73 wrote its generated plan
# to the shot, failed the gate, and the next build trusted the file's existence — v1
# sidecars could not distinguish "published" from "left behind by a failed transaction".
UNIT_PLAN_AUTHORITY_SCHEMA = "vfx-harness.unit-plan-authority/v2"
_AMENDMENT_STATUSES = {"proposed", "approved", "rejected", "superseded"}
_AMENDMENT_CLASSES = {
    "build_defect",
    "plan_defect",
    "reference_ambiguity",
    "scope_mismatch",
    "tooling_defect",
}


def _slug(script: str) -> str:
    stem = Path(script).stem
    return re.sub(r"[^a-z0-9_]+", "_", stem.lower()).strip("_")


def global_plan_path(folder: str | Path) -> Path:
    # plan_authority imports this module only for unit-plan consumer-view validation.
    from vfx_harness.orchestration.plan_authority import selected_artifact_path  # noqa: PLC0415

    return selected_artifact_path(folder, "global.md")


def layer_plan_path(folder: str | Path, layer) -> Path:
    return Path(folder) / PLAN_DIR / f"{_slug(layer.script)}.md"


def work_unit_plan_path(folder: str | Path, unit) -> Path:
    """Resolve the schema-declared unit plan inside the shot root."""
    root = Path(folder).resolve()
    path = Path(os.path.abspath(root / unit.plan))
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"work-unit plan escapes the shot root: {unit.plan!r}") from exc
    if path.relative_to(root).parts[:1] != (PLAN_DIR,):
        raise ValueError(f"work-unit plan must live under {PLAN_DIR}/: {unit.plan!r}")
    from vfx_harness.orchestration.plan_authority import POINTER, resolve_current  # noqa: PLC0415

    if (root / POINTER).exists():
        bundle = resolve_current(root)
        name = path.relative_to(root).as_posix()
        if name in bundle.artifacts:
            return bundle.root / name
    return path


def is_selected_bundle_member(folder: str | Path, path: str | Path) -> bool:
    """Return whether a plan path is frozen in the currently selected bundle.

    Resolving current authority verifies every member hash, so this predicate also refuses a
    bundle that has been modified after publication.
    """
    from vfx_harness.orchestration.plan_authority import POINTER, resolve_current  # noqa: PLC0415

    root = Path(folder).resolve()
    if not (root / POINTER).exists():
        return False
    bundle = resolve_current(root)
    candidate = Path(path).resolve()
    try:
        name = candidate.relative_to(bundle.root).as_posix()
    except ValueError:
        return False
    return name in bundle.artifacts


def _unit_authority_path(path: Path) -> Path:
    return path.with_name(path.name + ".authority.json")


def work_unit_plan_authority_path(path: str | Path) -> Path:
    """Return the sidecar path that binds a JIT plan to selected global authority."""
    return _unit_authority_path(Path(path))


def stamp_work_unit_plan(folder: str | Path, path: str | Path, *, gate: dict | None = None) -> Path | None:
    """Pin one JIT plan to the selected global bundle and its exact bytes.

    Without `gate`, the stamp asserts integrity only — enough for the deterministic gate
    to admit the plan into a consumer view, never enough to build on. Publication is the
    two-phase form: the materialization transaction re-stamps with
    ``gate={"clean": True, "blocking": 0, "run_id": …}`` after (and only after) the gate
    passes, and consumers refuse anything less (see validate_work_unit_plan_authority)."""
    from vfx_harness.orchestration.plan_authority import POINTER, resolve_current  # noqa: PLC0415

    root = Path(folder).resolve()
    plan = Path(path).resolve()
    if not (root / POINTER).exists():
        return None
    bundle = resolve_current(root)
    record = {
        "schema": UNIT_PLAN_AUTHORITY_SCHEMA,
        "bundle_hash": bundle.content_hash,
        "plan_sha256": hashlib.sha256(plan.read_bytes()).hexdigest(),
        "path": plan.relative_to(root).as_posix(),
    }
    if gate is not None:
        if gate.get("clean") is not True or int(gate.get("blocking", 1)) != 0:
            raise ValueError("gate attestation may only be stamped for a clean gate result")
        record["gate_clean"] = True
        record["gate_blocking"] = 0
        record["gate_run_id"] = str(gate.get("run_id") or "")
        if not record["gate_run_id"]:
            raise ValueError("gate attestation requires the gating run id")
    authority = _unit_authority_path(plan)
    atomic_write(authority, json.dumps(record, indent=2, sort_keys=True) + "\n")
    return authority


def _require_gate_attestation(record: dict, plan: Path) -> None:
    if (
        record.get("gate_clean") is True
        and record.get("gate_blocking") == 0
        and str(record.get("gate_run_id") or "")
    ):
        return
    raise ValueError(
        f"{plan} has no clean-gate attestation — it was never published through a "
        "passing deterministic gate (a failed materialization may have left it behind). "
        "Regenerate the JIT unit plan; generation republishes only through a clean gate"
    )


def _base_record(record: dict) -> dict:
    return {key: record.get(key) for key in ("schema", "bundle_hash", "plan_sha256", "path")}


def validate_work_unit_plan_authority(
    folder: str | Path, path: str | Path, *, require_gate: bool = True
) -> None:
    """Fail closed when a JIT plan belongs to another global generation or was never
    published through a clean deterministic gate.

    ``require_gate=False`` is for the gate pipeline itself (view staging and the gate's
    own hierarchy check): those run BEFORE attestation exists and check integrity only.
    Every build-time consumer takes the default and refuses unattested plans."""
    from vfx_harness.orchestration.plan_authority import (  # noqa: PLC0415
        BUNDLE_SCHEMA,
        CONSUMER_VIEW_SCHEMA,
        POINTER,
        resolve_current,
    )

    root = Path(folder).resolve()
    lexical_plan = Path(os.path.abspath(path))
    plan = lexical_plan.resolve()
    marker = root / ".plan-consumer-view.json"
    if marker.is_file():
        try:
            view = json.loads(marker.read_text(encoding="utf-8"))
            if view.get("schema") != CONSUMER_VIEW_SCHEMA:
                raise ValueError("unsupported plan consumer view")
            bundle = resolve_current(view["shot"])
            if bundle.root != Path(view["bundle"]).resolve():
                raise ValueError("plan consumer view points at a non-selected bundle")
            if bundle.content_hash != view.get("content_hash"):
                raise ValueError("plan consumer view bundle hash is stale")
            name = lexical_plan.relative_to(root).as_posix()
            manifest = json.loads((bundle.root / "bundle.json").read_text(encoding="utf-8"))
            if manifest.get("schema") != BUNDLE_SCHEMA:
                raise ValueError("selected plan bundle manifest is unsupported")
            try:
                target_name = plan.relative_to(bundle.root).as_posix()
            except ValueError:
                target_name = ""
            expected = (manifest.get("artifacts") or {}).get(name)
            if target_name == name and expected:
                if hashlib.sha256(plan.read_bytes()).hexdigest() != expected:
                    raise ValueError("work-unit plan bundle member hash does not match authority")
            else:
                shot = Path(str(view["shot"])).resolve()
                source = (shot / name).resolve()
                if plan != source:
                    raise ValueError("JIT unit plan view points outside its shot-root authority")
                lexical_authority = _unit_authority_path(lexical_plan)
                source_authority = _unit_authority_path(source)
                if lexical_authority.resolve() != source_authority.resolve():
                    raise ValueError("JIT unit plan authority sidecar is absent or redirected")
                record = json.loads(source_authority.read_text(encoding="utf-8"))
                expected_record = {
                    "schema": UNIT_PLAN_AUTHORITY_SCHEMA,
                    "bundle_hash": bundle.content_hash,
                    "plan_sha256": hashlib.sha256(plan.read_bytes()).hexdigest(),
                    "path": name,
                }
                if _base_record(record) != expected_record:
                    raise ValueError("JIT unit plan is stale or edited in the consumer view")
                if require_gate:
                    _require_gate_attestation(record, plan)
        except (KeyError, OSError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"{lexical_plan} has invalid selected-bundle authority") from exc
        return
    if not (root / POINTER).exists():
        return
    bundle = resolve_current(root)
    try:
        bundled_name = plan.relative_to(bundle.root).as_posix()
    except ValueError:
        bundled_name = ""
    if bundled_name in bundle.artifacts:
        return
    authority = _unit_authority_path(plan)
    try:
        record = json.loads(authority.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{plan} has no readable selected-bundle authority record") from exc
    expected = {
        "schema": UNIT_PLAN_AUTHORITY_SCHEMA,
        "bundle_hash": bundle.content_hash,
        "plan_sha256": hashlib.sha256(plan.read_bytes()).hexdigest(),
        "path": plan.relative_to(root).as_posix(),
    }
    if _base_record(record) != expected:
        raise ValueError(f"{plan} is stale or edited relative to the selected global plan")
    if require_gate:
        _require_gate_attestation(record, plan)


def read_work_unit_plan(folder: str | Path, layer, unit) -> str:
    """Read exactly the execution plan named by a schema-4 work unit."""
    path = work_unit_plan_path(folder, unit)
    if not path.is_file():
        raise FileNotFoundError(
            f"{path} missing — monolithic plan fallback has been removed. Generate and "
            f"gate the just-in-time plan for layer {layer.id} unit {unit.id} before building it"
        )
    validate_work_unit_plan_authority(folder, path)
    text = path.read_text(encoding="utf-8").strip()
    if len(text) < 200:
        raise ValueError(f"{path} is too small to be an executable layer plan")
    lines = text.count("\n") + 1
    if lines > 160:
        raise ValueError(
            f"{path} has {lines} lines; strict unit plans are capped at 160. Machine "
            "contracts and sealed outcomes carry evidence; the plan is only an execution index"
        )
    return text


def read_layer_plan(folder: str | Path, layer) -> str:
    """Single-unit vertical-slice adapter; never collapse a unit DAG implicitly."""
    if len(layer.stages) != 1:
        raise ValueError(
            f"layer {layer.id} declares {len(layer.stages)} work units; layer-level plan "
            "retrieval cannot choose or combine them. Use staged work-unit execution"
        )
    return read_work_unit_plan(folder, layer, layer.stages[0])


def load_amendments(folder: str | Path) -> list[dict]:
    path = Path(folder) / AMENDMENTS
    if not path.is_file():
        return []
    rows = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{AMENDMENTS}:{line_no}: {exc}") from exc
        error = validate_amendment(row)
        if error:
            raise ValueError(f"{AMENDMENTS}:{line_no}: {error}")
        rows.append(row)
    return rows


def validate_amendment(row: dict) -> str | None:
    if not isinstance(row, dict):
        return "record must be an object"
    for key in ("id", "layer", "status", "classification", "original", "replacement", "evidence", "impact"):
        if not row.get(key):
            return f"missing {key}"
    if row["status"] not in _AMENDMENT_STATUSES:
        return f"invalid status {row['status']!r}"
    if row["classification"] not in _AMENDMENT_CLASSES:
        return f"invalid classification {row['classification']!r}"
    if not isinstance(row["evidence"], list) or not row["evidence"]:
        return "evidence must be a non-empty list"
    if row["status"] == "approved" and not row.get("approved_by"):
        return "approved amendment requires approved_by"
    return None


def amendment_block(folder: str | Path, layer_id: str) -> str:
    rows = [
        row
        for row in load_amendments(folder)
        if str(row.get("layer")) == str(layer_id) and row.get("status") == "approved"
    ]
    if not rows:
        return ""
    lines = ["## Approved plan amendments"]
    for row in rows:
        lines.append(
            f"- **{row['id']}**: ~~{row['original']}~~ → {row['replacement']} "
            f"(evidence: {', '.join(map(str, row['evidence']))}; impact: {row['impact']})"
        )
    return "\n".join(lines)


def contract_gaps_block(folder: str | Path, layer_id: str, unit_id: str | None = None) -> str:
    """Latest hash-pinned coverage defects for transactional replanning context."""
    path = run_artifacts.shot_state_dir(folder) / "contract-gaps.jsonl"
    if not path.is_file():
        return ""
    records = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path.relative_to(Path(folder))}:{line_no}: {exc}") from exc
        if str(row.get("layer")) != str(layer_id):
            continue
        if unit_id is not None and str(row.get("unit")) not in {str(unit_id), "coverage_audit"}:
            continue
        records.append(row)
    if not records:
        return ""
    latest = records[-1]
    lines = [
        "## Verified contract gaps — plan defects, not builder instructions",
        f"Candidate `{latest.get('candidate_hash')}` under settings "
        f"`{latest.get('settings_hash')}` exposed:",
    ]
    seen = set()
    for gap in latest.get("gaps") or []:
        observation = gap.get("observation") or {}
        key = (observation.get("axis"), observation.get("property"), observation.get("moment"))
        if key in seen:
            continue
        seen.add(key)
        lines.append(
            f"- axis `{observation.get('axis')}`, property `{observation.get('property')}`, "
            f"f{observation.get('moment')}, roles {observation.get('roles') or []}: "
            f"{observation.get('observation')}"
        )
    lines.append(
        "Do not turn these directly into scene edits. Amend the claim/contract graph with "
        "an independent measurement or qualified qualitative authority, validate its "
        "adversary, then transactionally invalidate only affected downstream units."
    )
    return "\n".join(lines)


def prior_outcomes_block(folder: str | Path, layer_id: str) -> str:
    root = Path(folder)
    try:
        # A layer prefix is authority from the selected document, never integer order or
        # whatever filenames happen to exist in the outcomes directory.
        from vfx_harness.orchestration.plan_authority import (  # noqa: PLC0415
            POINTER,
            resolve_current,
            selected_artifact_path,
        )

        selected_document = json.loads(
            selected_artifact_path(root, "layers.json").read_text(encoding="utf-8")
        )
        global_path = (
            resolve_current(root).root / "layers.json"
            if (root / POINTER).exists()
            else selected_artifact_path(root, "layers.json")
        )
        global_document = json.loads(global_path.read_text(encoding="utf-8"))
        selected_rows = (
            selected_document.get("layers")
            if isinstance(selected_document, dict)
            else None
        )
        global_rows = (
            global_document.get("layers")
            if isinstance(global_document, dict)
            else None
        )
        if (
            not isinstance(selected_rows, list)
            or any(not isinstance(row, dict) for row in selected_rows)
            or not isinstance(global_rows, list)
            or any(not isinstance(row, dict) for row in global_rows)
        ):
            raise ValueError("selected/global layers.json must contain layer objects")
        ordered_ids = list(strict_topological_sparse_layer_ids(global_rows))
        selected_ids = [str(row.get("id") or "").strip() for row in selected_rows]
        if len(selected_ids) != len(set(selected_ids)) or set(selected_ids) != set(ordered_ids):
            raise ValueError("selected executable layer view does not match the exact global DAG")
        current = ordered_ids.index(str(layer_id))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("cannot read selected layer DAG for prior outcomes") from exc
    rows = []
    for prior_id in ordered_ids[:current]:
        path = layer_outcome_path(root, prior_id)
        if not path.is_file():
            continue
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
            if str(row.get("layer") or "") != prior_id:
                raise ValueError(
                    f"sealed outcome at {path} names layer {row.get('layer')!r}, "
                    f"expected {prior_id!r}"
                )
            if row.get("status") == "passed":
                rows.append(row)
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"sealed prior-layer outcome is unreadable: {path}") from exc
    if not rows:
        return ""
    lines = ["## Sealed prior-layer outcomes"]
    for row in rows:
        lines.append(
            f"- Layer {row['layer']} passed; script `{row.get('script', '?')}`; "
            f"canonical decision `{row.get('decided_by', '?')}`; "
            f"authoritative checks {row.get('authoritative_passed', 0)}/"
            f"{row.get('authoritative_total', 0)}. Do not reopen it without an approved "
            f"amendment."
        )
    return "\n".join(lines)


def write_layer_outcome(
    folder: str | Path,
    layer,
    *,
    status: str,
    best: dict,
    canonical: list,
    run_id: str,
    attempt: int | None = None,
    blender_version: str,
) -> Path:
    """Seal measured state for the next layer's just-in-time planning input."""
    # revalidation imports the path helpers above; outcome sealing is the reverse edge.
    from vfx_harness.orchestration.revalidation import (  # noqa: PLC0415
        canonical_records,
        input_manifest,
    )

    evidence = [
        item
        for _frame_ref, verdict in canonical
        for item in (verdict.get("evidence") or [])
        if item.get("authoritative")
    ]
    interfaces = [
        {
            key: item.get(key)
            for key in (
                "id",
                "metric",
                "value",
                "target",
                "pass",
                "owner_layer",
                "fault_owner",
                "activates_at",
                "lifecycle",
            )
        }
        for item in evidence
        if item.get("source") == "interface_contract" and str(item.get("owner_layer")) == str(layer.id)
    ]
    decisions = [verdict.get("decided_by", "critic") for _fr, verdict in canonical]
    revalidation_manifest = input_manifest(
        folder,
        layer,
        blender_version=blender_version,
    )
    manifest_sha256 = hashlib.sha256(
        json.dumps(
            revalidation_manifest,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    record = {
        "schema": OUTCOME_SCHEMA,
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "layer": str(layer.id),
        "title": layer.title,
        "script": layer.script,
        "status": status,
        "run_id": run_id,
        "attempt": attempt,
        "best": {key: best.get(key) for key in ("round", "mean", "render")},
        "decided_by": decisions[0] if len(set(decisions)) == 1 and decisions else decisions,
        "authoritative_total": len(evidence),
        "authoritative_passed": sum(bool(item.get("pass")) for item in evidence),
        "failed_contracts": [item.get("id") for item in evidence if not item.get("pass")],
        "interfaces": interfaces,
        "revalidation_manifest": revalidation_manifest,
        "canonical": canonical_records(
            folder,
            layer,
            canonical,
            input_manifest_sha256=manifest_sha256,
        ),
    }
    path = layer_outcome_path(folder, str(layer.id))
    atomic_write(path, json.dumps(record, indent=2) + "\n")
    return path


def load_layer_outcome(folder: str | Path, layer_id: str) -> dict:
    path = layer_outcome_path(folder, str(layer_id))
    try:
        row = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return row if isinstance(row, dict) else {}


def record_revalidation(folder: str | Path, layer_id: str, *, run_id: str, attempt: int, evidence: list[dict]) -> Path:
    """Append the latest replay result without changing the sealed pass boundary."""
    path = layer_outcome_path(folder, str(layer_id))
    row = load_layer_outcome(folder, layer_id)
    if not row:
        raise FileNotFoundError(path)
    row["last_revalidation"] = {
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "run_id": run_id,
        "attempt": attempt,
        "status": "passed",
        "frames": evidence,
    }
    atomic_write(path, json.dumps(row, indent=2) + "\n")
    return path
