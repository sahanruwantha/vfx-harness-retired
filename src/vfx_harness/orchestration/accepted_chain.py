"""One accepted-chain row set shared by acceptance and the shot-ledger index.

The ``vfx-harness.acceptance-chain/v2`` digest identifies the composed build chain that
acceptance judges.  Every consumer derives its rows and digest through this module, so
the ledger's ``accepted_chain_digest`` is the same authority acceptance binds and never
a second projection of reduced rows.  A layer with a verified publication contributes
its exact receipt-bound identities; a layer without one contributes the legacy ledger
status and the bytes currently on disk, exactly as acceptance's unverified capture does.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from vfx_harness.domain.stop_envelope_primitives import canonical_digest
from vfx_harness.orchestration import generate_construction
from vfx_harness.orchestration.layer_publication import VerifiedLayerPublication
from vfx_harness.orchestration.ledger import Layer
from vfx_harness.orchestration.unit_state import unit_digest

ACCEPTANCE_CHAIN_SCHEMA = "vfx-harness.acceptance-chain/v2"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inside_shot(root: Path, relative: str, where: str) -> Path:
    """Resolve one shot-relative locator and refuse escapes from the shot root."""

    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{where} escapes the shot root: {relative!r}") from exc
    return candidate


def legacy_ledger_statuses(shot_root: Path) -> dict[str, str]:
    """Read the legacy milestone statuses that unverified chain rows report."""

    path = shot_root / "shot.json"
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"acceptance cannot pin unreadable ledger authority: {path}") from exc
    if not isinstance(value, dict) or not isinstance(value.get("milestones", {}), dict):
        raise ValueError("acceptance cannot pin malformed shot.json milestone authority")
    return {
        str(layer_id): str(row.get("status") or "pending")
        for layer_id, row in value.get("milestones", {}).items()
        if isinstance(row, dict)
    }


def accepted_chain_rows(
    shot_root: Path,
    layers: Sequence[Layer],
    publications: Mapping[str, VerifiedLayerPublication],
    *,
    ledger_statuses: Mapping[str, str],
) -> tuple[dict[str, Any], ...]:
    """Build the ordered chain rows for every selected layer.

    ``publications`` maps layer ids to their verified current publications; a layer
    absent from it is described from the legacy ledger status and on-disk bytes.
    """

    chain: list[dict[str, Any]] = []
    for layer in layers:
        layer_id = str(layer.id)
        layer_script = inside_shot(shot_root, layer.script, f"layer {layer.id} script")
        try:
            construction = generate_construction.prepare_construction_replay_input(
                shot_root,
                layer_script,
            )
        except generate_construction.GenerateConstructionError as exc:
            raise ValueError(
                f"acceptance cannot capture construction replay authority for "
                f"layer {layer.id}: {exc}"
            ) from exc
        publication = publications.get(layer_id)
        if publication is None:
            units = []
            for unit in layer.stages:
                script_path = inside_shot(
                    shot_root,
                    unit.mutates.script_spans[0],
                    f"layer {layer.id} unit {unit.id} script",
                )
                units.append(
                    {
                        "unit_id": unit.id,
                        "unit_digest": unit_digest(unit),
                        "completion_receipt_digest": None,
                        "script": unit.mutates.script_spans[0],
                        "script_sha256": (
                            sha256_of(script_path) if script_path.is_file() else None
                        ),
                    }
                )
        else:
            units = [
                {
                    "unit_id": unit.unit_id,
                    "unit_digest": unit.unit_digest,
                    "completion_receipt_digest": unit.completion_receipt_digest,
                    "script": unit.script_path,
                    "script_sha256": unit.script_sha256,
                }
                for unit in publication.receipt.claim.unit_inputs
            ]
        chain.append(
            {
                "layer_id": layer.id,
                "status": (
                    ledger_statuses.get(layer_id, "pending")
                    if publication is None
                    else publication.ledger_status
                ),
                "script": (
                    layer.script
                    if publication is None
                    else publication.ledger_script_path
                ),
                "script_sha256": (
                    sha256_of(layer_script) if publication is None and layer_script.is_file()
                    else (
                        None
                        if publication is None
                        else publication.ledger_script_sha256
                    )
                ),
                "replay_dependencies": (
                    []
                    if construction is None
                    else [
                        dependency.as_dict()
                        for dependency in construction.dependencies
                    ]
                ),
                "units": units,
                "finalization_receipt_digest": (
                    None if publication is None else publication.receipt.receipt_digest
                ),
                "layer_outcome": (
                    None if publication is None else publication.outcome_locator
                ),
                "layer_outcome_sha256": (
                    None if publication is None else publication.outcome_sha256
                ),
            }
        )
    return tuple(chain)


def accepted_chain_digest(rows: Sequence[Mapping[str, Any]]) -> str:
    """Digest the exact ordered chain rows under the acceptance-chain schema."""

    return canonical_digest({"schema": ACCEPTANCE_CHAIN_SCHEMA, "chain": list(rows)})


__all__ = [
    "ACCEPTANCE_CHAIN_SCHEMA",
    "accepted_chain_digest",
    "accepted_chain_rows",
    "inside_shot",
    "legacy_ledger_statuses",
    "sha256_of",
]
