"""Strict hierarchical planning artifacts.

The global plan is a dependency map, not an execution prompt. Builders consume exactly
one schema-declared work-unit plan plus machine-readable contracts and prior outcomes. There
is deliberately no fallback to ``plan.md``: silently accepting a giant legacy plan would
keep the stale-context failure mode alive behind a compatibility branch.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

from .provenance import atomic_write

PLAN_DIR = "plans"
GLOBAL_PLAN = "global.md"
AMENDMENTS = "plan_amendments.jsonl"
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
    return Path(folder) / PLAN_DIR / GLOBAL_PLAN


def layer_plan_path(folder: str | Path, layer) -> Path:
    return Path(folder) / PLAN_DIR / f"{_slug(layer.script)}.md"


def work_unit_plan_path(folder: str | Path, unit) -> Path:
    """Resolve the schema-declared unit plan inside the shot root."""
    root = Path(folder).resolve()
    path = (root / unit.plan).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"work-unit plan escapes the shot root: {unit.plan!r}") from exc
    if path.relative_to(root).parts[:1] != (PLAN_DIR,):
        raise ValueError(f"work-unit plan must live under {PLAN_DIR}/: {unit.plan!r}")
    return path


def read_work_unit_plan(folder: str | Path, layer, unit) -> str:
    """Read exactly the execution plan named by a schema-4 work unit."""
    path = work_unit_plan_path(folder, unit)
    if not path.is_file():
        raise FileNotFoundError(
            f"{path} missing — monolithic plan fallback has been removed. Generate and "
            f"gate the just-in-time plan for layer {layer.id} unit {unit.id} before building it"
        )
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
    path = Path(folder) / "logs" / "contract_gaps.jsonl"
    if not path.is_file():
        return ""
    records = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"logs/contract_gaps.jsonl:{line_no}: {exc}") from exc
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
    out_dir = Path(folder) / PLAN_DIR / "outcomes"
    if not out_dir.is_dir():
        return ""
    rows = []
    try:
        current = int(layer_id)
    except ValueError:
        current = 10**9
    for path in sorted(out_dir.glob("*.json")):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
            if int(row.get("layer", current)) < current and row.get("status") == "passed":
                rows.append(row)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
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
    from .revalidation import OUTCOME_SCHEMA, canonical_records, input_manifest

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
        "revalidation_manifest": input_manifest(folder, layer, blender_version=blender_version),
        "canonical": canonical_records(folder, layer, canonical),
    }
    path = Path(folder) / PLAN_DIR / "outcomes" / f"{int(layer.id):02d}.json"
    atomic_write(path, json.dumps(record, indent=2) + "\n")
    return path


def load_layer_outcome(folder: str | Path, layer_id: str) -> dict:
    path = Path(folder) / PLAN_DIR / "outcomes" / f"{int(layer_id):02d}.json"
    try:
        row = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return row if isinstance(row, dict) else {}


def record_revalidation(folder: str | Path, layer_id: str, *, run_id: str, attempt: int, evidence: list[dict]) -> Path:
    """Append the latest replay result without changing the sealed pass boundary."""
    path = Path(folder) / PLAN_DIR / "outcomes" / f"{int(layer_id):02d}.json"
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
