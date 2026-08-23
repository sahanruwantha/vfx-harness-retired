"""Enforce typed plan obligations and assumptions at dependency boundaries."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from vfx_harness.domain.plan_records import (
    load_assumptions,
    load_obligations,
    load_resolutions,
)
from vfx_harness.observability.provenance import atomic_write
from vfx_harness.observability.run_artifacts import shot_state_dir
from vfx_harness.orchestration.plan_authority import resolve_current

RESOLUTIONS = "plan-resolutions.jsonl"


@dataclass(frozen=True, slots=True)
class DueRecord:
    kind: str
    id: str
    statement: str
    owner: str


class PlanDueError(RuntimeError):
    def __init__(self, records: tuple[DueRecord, ...], boundary: str):
        self.records = records
        self.boundary = boundary
        details = "; ".join(f"{row.kind} {row.id}: {row.statement}" for row in records)
        super().__init__(f"{boundary} is blocked by unresolved plan authority — {details}")


def unresolved_due(
    shot_folder: str | Path,
    *,
    layer: str | None = None,
    unit: str | None = None,
    acceptance: bool = False,
    completion: bool = False,
    record_kinds: frozenset[str] | None = None,
) -> tuple[DueRecord, ...]:
    bundle = resolve_current(shot_folder)
    resolved = load_resolutions(
        shot_state_dir(shot_folder) / RESOLUTIONS,
        bundle_hash=bundle.content_hash,
    )
    out: list[DueRecord] = []
    for kind, records in (
        ("obligation", load_obligations(bundle.root)),
        ("assumption", load_assumptions(bundle.root)),
    ):
        if record_kinds is not None and kind not in record_kinds:
            continue
        for record in records:
            evidence = resolved.get((kind, record.id))
            if evidence is not None and (
                (
                    kind == "assumption"
                    and (
                        any(item[0] == "human_decision" for item in evidence)
                        or {
                            ("scene_contract", contract_id)
                            for contract_id in record.falsification_contract_ids
                        }
                        <= set(evidence)
                    )
                )
                or (kind == "obligation" and set(record.evidence) <= set(evidence))
            ):
                continue
            if record.due.due_for(
                layer=layer,
                unit=unit,
                acceptance=acceptance,
                completion=completion,
            ):
                out.append(DueRecord(kind, record.id, record.statement, record.owner))
    return tuple(out)


def require_due_clear(
    shot_folder: str | Path,
    *,
    layer: str | None = None,
    unit: str | None = None,
    acceptance: bool = False,
    completion: bool = False,
    record_kinds: frozenset[str] | None = None,
) -> None:
    records = unresolved_due(
        shot_folder,
        layer=layer,
        unit=unit,
        acceptance=acceptance,
        completion=completion,
        record_kinds=record_kinds,
    )
    if records:
        boundary = "shot acceptance" if acceptance else (
            f"layer {layer} unit {unit} completion" if completion else
            f"layer {layer} unit {unit}" if unit else f"layer {layer}"
        )
        raise PlanDueError(records, boundary)


def resolve_unit_completion(
    shot_folder: str | Path,
    *,
    layer: str,
    unit: str,
    passed_evidence: Iterable[tuple[str, str]],
    checkpoint_hash: str | None = None,
) -> tuple[str, ...]:
    """Discharge machine-verifiable obligations at an accepted unit boundary.

    Obligations resolve from their declared evidence. An approved/planner start due at
    this unit may advance to ``confirmed_outcome`` only when every declared falsification
    contract passed and the frozen candidate checkpoint is hash-pinned.
    """
    bundle = resolve_current(shot_folder)
    evidence = frozenset((str(kind), str(identifier)) for kind, identifier in passed_evidence)
    resolutions_path = shot_state_dir(shot_folder) / RESOLUTIONS
    resolved = load_resolutions(resolutions_path, bundle_hash=bundle.content_hash)
    rows: list[dict] = []
    for record in load_obligations(bundle.root):
        owned_here = record.owner == f"{layer}.{unit}"
        due_here = record.due.due_for(layer=layer, unit=unit, completion=True)
        if not due_here and not (owned_here and record.due.kind == "before_acceptance"):
            continue
        if ("obligation", record.id) in resolved or not set(record.evidence) <= evidence:
            continue
        rows.append({
            "schema": "vfx-harness.plan-resolutions/v1",
            "bundle_hash": bundle.content_hash,
            "kind": "obligation",
            "id": record.id,
            "status": "satisfied",
            "evidence": [
                {"kind": kind, "id": identifier}
                for kind, identifier in record.evidence
            ],
            "resolved_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "resolved_by": f"unit_completion:{layer}.{unit}",
        })
    for record in load_assumptions(bundle.root):
        if record.decision_strength not in {"approved_start", "planner_start"}:
            continue
        if record.due.kind != "unit_completion" or not record.due.due_for(
            layer=layer, unit=unit, completion=True
        ):
            continue
        if record.falsification_owner != f"{layer}.{unit}":
            continue
        expected = {
            ("scene_contract", contract_id)
            for contract_id in record.falsification_contract_ids
        }
        if ("assumption", record.id) in resolved or not expected <= evidence:
            continue
        if (
            not isinstance(checkpoint_hash, str)
            or len(checkpoint_hash) != 64
            or any(char not in "0123456789abcdef" for char in checkpoint_hash)
        ):
            raise ValueError(
                f"assumption {record.id} confirmation requires a lowercase SHA-256 checkpoint hash"
            )
        rows.append({
            "schema": "vfx-harness.plan-resolutions/v1",
            "bundle_hash": bundle.content_hash,
            "kind": "assumption",
            "id": record.id,
            "status": "satisfied",
            "decision_strength": "confirmed_outcome",
            "checkpoint_hash": checkpoint_hash,
            "evidence": [
                {"kind": kind, "id": identifier}
                for kind, identifier in sorted(expected)
            ],
            "resolved_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "resolved_by": f"unit_completion:{layer}.{unit}",
        })
    if not rows:
        return ()
    resolutions_path.parent.mkdir(parents=True, exist_ok=True)
    existing = resolutions_path.read_text(encoding="utf-8") if resolutions_path.is_file() else ""
    suffix = "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows)
    atomic_write(resolutions_path, existing + suffix)
    return tuple(str(row["id"]) for row in rows)


def resolve_acceptance_completion(
    shot_folder: str | Path,
    *,
    passed_evidence: Iterable[tuple[str, str]],
) -> tuple[str, ...]:
    """Discharge acceptance-due obligations from finished-chain evidence."""
    bundle = resolve_current(shot_folder)
    evidence = frozenset((str(kind), str(identifier)) for kind, identifier in passed_evidence)
    resolutions_path = shot_state_dir(shot_folder) / RESOLUTIONS
    resolved = load_resolutions(resolutions_path, bundle_hash=bundle.content_hash)
    rows: list[dict] = []
    for record in load_obligations(bundle.root):
        if record.due.kind != "before_acceptance":
            continue
        if ("obligation", record.id) in resolved or not set(record.evidence) <= evidence:
            continue
        rows.append({
            "schema": "vfx-harness.plan-resolutions/v1",
            "bundle_hash": bundle.content_hash,
            "kind": "obligation",
            "id": record.id,
            "status": "satisfied",
            "evidence": [
                {"kind": kind, "id": identifier}
                for kind, identifier in record.evidence
            ],
            "resolved_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "resolved_by": "acceptance_completion",
        })
    if not rows:
        return ()
    resolutions_path.parent.mkdir(parents=True, exist_ok=True)
    existing = resolutions_path.read_text(encoding="utf-8") if resolutions_path.is_file() else ""
    suffix = "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows)
    atomic_write(resolutions_path, existing + suffix)
    return tuple(str(row["id"]) for row in rows)
