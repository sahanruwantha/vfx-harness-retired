"""Enforce typed plan obligations and assumptions at dependency boundaries."""

from __future__ import annotations

import fcntl
import json
from collections.abc import Iterable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from vfx_harness.domain.plan_records import (
    load_assumptions,
    load_obligations,
    load_resolutions,
)
from vfx_harness.domain.stop_envelope_primitives import require_digest
from vfx_harness.domain.unit_completion_receipts import UnitCompletionReceipt
from vfx_harness.observability.provenance import atomic_write
from vfx_harness.observability.run_artifacts import shot_state_dir
from vfx_harness.orchestration.plan_authority import resolve_current
from vfx_harness.orchestration.unit_completion_state import (
    current_completion_receipt_digests,
)

if TYPE_CHECKING:
    from vfx_harness.orchestration.authority_selection import ResolvedSelectedAuthority
    from vfx_harness.orchestration.plan_authority import PlanBundle

RESOLUTIONS = "plan-resolutions.jsonl"


@contextmanager
def _locked_resolutions(path: Path):
    """Serialize the resolution ledger's read/merge/publish transaction."""

    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


@contextmanager
def _provided_completion_receipts(
    receipts: Mapping[tuple[str, str], str],
):
    """Adapt an outer state-guarded receipt map without reacquiring state locks."""

    current: dict[tuple[str, str], str] = {}
    for key, digest in receipts.items():
        if not isinstance(key, tuple) or len(key) != 2 or not all(
            isinstance(part, str) and part.strip() == part and part for part in key
        ):
            raise ValueError(
                "current completion receipt keys must be exact (layer, unit) strings"
            )
        current[(key[0], key[1])] = require_digest(
            digest,
            f"current completion receipt {key[0]}.{key[1]}",
        )
    yield current


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


def unresolved_due_for_bundle(
    shot_folder: str | Path,
    bundle: PlanBundle | None,
    *,
    layer: str | None = None,
    unit: str | None = None,
    acceptance: bool = False,
    completion: bool = False,
    record_kinds: frozenset[str] | None = None,
    expected_bundle_digest: str | None = None,
    current_completion_receipts: Mapping[tuple[str, str], str] | None = None,
) -> tuple[DueRecord, ...]:
    if bundle is None:
        return ()
    if (
        expected_bundle_digest is not None
        and bundle.content_hash != expected_bundle_digest
    ):
        raise ValueError(
            "plan authority changed before the due-state boundary could be verified"
        )
    if current_completion_receipts is None:
        receipt_context = current_completion_receipt_digests(shot_folder)
    else:
        receipt_context = _provided_completion_receipts(current_completion_receipts)
    with receipt_context as current_receipts:
        resolved = load_resolutions(
            shot_state_dir(shot_folder) / RESOLUTIONS,
            bundle_hash=bundle.content_hash,
            current_completion_receipts=current_receipts,
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


def unresolved_due(
    shot_folder: str | Path,
    *,
    layer: str | None = None,
    unit: str | None = None,
    acceptance: bool = False,
    completion: bool = False,
    record_kinds: frozenset[str] | None = None,
    expected_bundle_digest: str | None = None,
    selected_authority: ResolvedSelectedAuthority | None = None,
    current_completion_receipts: Mapping[tuple[str, str], str] | None = None,
) -> tuple[DueRecord, ...]:
    bundle = (
        resolve_current(shot_folder)
        if selected_authority is None
        else (
            None
            if selected_authority.plan is None
            else selected_authority.plan.bundle
        )
    )
    return unresolved_due_for_bundle(
        shot_folder,
        bundle,
        layer=layer,
        unit=unit,
        acceptance=acceptance,
        completion=completion,
        record_kinds=record_kinds,
        expected_bundle_digest=expected_bundle_digest,
        current_completion_receipts=current_completion_receipts,
    )


def _require_records_clear(
    records: tuple[DueRecord, ...],
    *,
    layer: str | None,
    unit: str | None,
    acceptance: bool,
    completion: bool,
) -> None:
    if records:
        boundary = "shot acceptance" if acceptance else (
            f"layer {layer} unit {unit} completion" if completion else
            f"layer {layer} unit {unit}" if unit else f"layer {layer}"
        )
        raise PlanDueError(records, boundary)


def require_due_clear_for_bundle(
    shot_folder: str | Path,
    bundle: PlanBundle | None,
    *,
    layer: str | None = None,
    unit: str | None = None,
    acceptance: bool = False,
    completion: bool = False,
    record_kinds: frozenset[str] | None = None,
    expected_bundle_digest: str | None = None,
    current_completion_receipts: Mapping[tuple[str, str], str] | None = None,
) -> None:
    """Verify due state against an already selected bundle without relocking selection."""

    records = unresolved_due_for_bundle(
        shot_folder,
        bundle,
        layer=layer,
        unit=unit,
        acceptance=acceptance,
        completion=completion,
        record_kinds=record_kinds,
        expected_bundle_digest=expected_bundle_digest,
        current_completion_receipts=current_completion_receipts,
    )
    _require_records_clear(
        records,
        layer=layer,
        unit=unit,
        acceptance=acceptance,
        completion=completion,
    )


def require_due_clear(
    shot_folder: str | Path,
    *,
    layer: str | None = None,
    unit: str | None = None,
    acceptance: bool = False,
    completion: bool = False,
    record_kinds: frozenset[str] | None = None,
    expected_bundle_digest: str | None = None,
    selected_authority: ResolvedSelectedAuthority | None = None,
    current_completion_receipts: Mapping[tuple[str, str], str] | None = None,
) -> None:
    records = unresolved_due(
        shot_folder,
        layer=layer,
        unit=unit,
        acceptance=acceptance,
        completion=completion,
        record_kinds=record_kinds,
        expected_bundle_digest=expected_bundle_digest,
        selected_authority=selected_authority,
        current_completion_receipts=current_completion_receipts,
    )
    _require_records_clear(
        records,
        layer=layer,
        unit=unit,
        acceptance=acceptance,
        completion=completion,
    )


def resolve_unit_completion(
    shot_folder: str | Path,
    *,
    layer: str,
    unit: str,
    completion_receipt: UnitCompletionReceipt,
    checkpoint_hash: str | None = None,
    selected_authority: ResolvedSelectedAuthority | None = None,
) -> tuple[str, ...]:
    """Discharge machine-verifiable obligations at an accepted unit boundary.

    Obligations resolve from their declared evidence. An approved/planner start due at
    this unit may advance to ``confirmed_outcome`` only when every declared falsification
    contract passed and the frozen candidate checkpoint is hash-pinned.
    """
    bundle = (
        resolve_current(shot_folder)
        if selected_authority is None
        else (
            None
            if selected_authority.plan is None
            else selected_authority.plan.bundle
        )
    )
    if bundle is None:
        return ()
    selected_digest = bundle.content_hash
    if not isinstance(completion_receipt, UnitCompletionReceipt):
        raise ValueError("unit completion resolution requires a typed completion receipt")
    if (
        completion_receipt.claim.layer_id != str(layer)
        or completion_receipt.claim.unit_id != str(unit)
    ):
        raise ValueError("unit completion receipt belongs to another layer or unit")
    completion_receipt_digest = completion_receipt.receipt_digest
    evidence = frozenset(completion_receipt.passed_evidence)
    resolutions_path = shot_state_dir(shot_folder) / RESOLUTIONS
    with _locked_resolutions(resolutions_path):
        if (
            selected_authority is None
            and resolve_current(shot_folder).content_hash != selected_digest
        ):
            raise ValueError(
                "plan authority changed before unit completion evidence could be resolved"
            )
        resolved = load_resolutions(
            resolutions_path,
            bundle_hash=selected_digest,
            current_completion_receipts={
                (str(layer), str(unit)): completion_receipt_digest
            },
        )
        rows: list[dict] = []
        for record in load_obligations(bundle.root):
            owned_here = record.owner == f"{layer}.{unit}"
            due_here = record.due.due_for(layer=layer, unit=unit, completion=True)
            if not due_here and not (
                owned_here and record.due.kind == "before_acceptance"
            ):
                continue
            if (
                ("obligation", record.id) in resolved
                or not set(record.evidence) <= evidence
            ):
                continue
            rows.append({
                "schema": "vfx-harness.plan-resolutions/v1",
                "bundle_hash": selected_digest,
                "kind": "obligation",
                "id": record.id,
                "status": "satisfied",
                "evidence": [
                    {"kind": kind, "id": identifier}
                    for kind, identifier in record.evidence
                ],
                "resolved_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "resolved_by": f"unit_completion:{layer}.{unit}",
                "completion_receipt_digest": completion_receipt_digest,
                "completion_layer": str(layer),
                "completion_unit": str(unit),
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
                    f"assumption {record.id} confirmation requires a lowercase "
                    "SHA-256 checkpoint hash"
                )
            rows.append({
                "schema": "vfx-harness.plan-resolutions/v1",
                "bundle_hash": selected_digest,
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
                "completion_receipt_digest": completion_receipt_digest,
                "completion_layer": str(layer),
                "completion_unit": str(unit),
            })
        if not rows:
            return ()
        if (
            selected_authority is None
            and resolve_current(shot_folder).content_hash != selected_digest
        ):
            raise ValueError(
                "plan authority changed before unit completion resolutions could be published"
            )
        existing = (
            resolutions_path.read_text(encoding="utf-8")
            if resolutions_path.is_file()
            else ""
        )
        suffix = "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows)
        atomic_write(resolutions_path, existing + suffix)
        return tuple(str(row["id"]) for row in rows)


def resolve_acceptance_completion(
    shot_folder: str | Path,
    *,
    passed_evidence: Iterable[tuple[str, str]],
    expected_bundle_digest: str,
    selected_authority: ResolvedSelectedAuthority | None = None,
) -> tuple[str, ...]:
    """Discharge acceptance-due obligations from finished-chain evidence."""
    bundle = (
        resolve_current(shot_folder)
        if selected_authority is None
        else (
            None
            if selected_authority.plan is None
            else selected_authority.plan.bundle
        )
    )
    if bundle is None:
        return ()
    if bundle.content_hash != expected_bundle_digest:
        raise ValueError(
            "plan authority changed before acceptance evidence could be resolved"
        )
    evidence = frozenset((str(kind), str(identifier)) for kind, identifier in passed_evidence)
    resolutions_path = shot_state_dir(shot_folder) / RESOLUTIONS
    with _locked_resolutions(resolutions_path):
        if (
            selected_authority is None
            and resolve_current(shot_folder).content_hash != expected_bundle_digest
        ):
            raise ValueError(
                "plan authority changed before acceptance evidence could be resolved"
            )
        resolved = load_resolutions(
            resolutions_path,
            bundle_hash=expected_bundle_digest,
        )
        rows: list[dict] = []
        for record in load_obligations(bundle.root):
            if record.due.kind != "before_acceptance":
                continue
            if (
                ("obligation", record.id) in resolved
                or not set(record.evidence) <= evidence
            ):
                continue
            rows.append({
                "schema": "vfx-harness.plan-resolutions/v1",
                "bundle_hash": expected_bundle_digest,
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
        if (
            selected_authority is None
            and resolve_current(shot_folder).content_hash != expected_bundle_digest
        ):
            raise ValueError(
                "plan authority changed before acceptance resolutions could be published"
            )
        existing = (
            resolutions_path.read_text(encoding="utf-8")
            if resolutions_path.is_file()
            else ""
        )
        suffix = "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows)
        atomic_write(resolutions_path, existing + suffix)
        return tuple(str(row["id"]) for row in rows)
