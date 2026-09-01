"""Prepared publication for builder-authored runtime image-check rows."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from vfx_harness.observability import prepared_publication


class RuntimeCheckPublicationError(ValueError):
    """The runtime-check ledger cannot accept a typed replacement."""


def prepare_runtime_check_update(
    shot_folder: str | Path,
    rows: Sequence[Mapping[str, Any]],
    *,
    authority_binding: str,
) -> prepared_publication.PreparedFileUpdate[int]:
    """Merge and fsync runtime rows before an attempt guard is retained."""

    root = Path(shot_folder)
    destination = root / "runtime_checks.json"
    additions = [dict(row) for row in rows]
    if not additions:
        raise RuntimeCheckPublicationError(
            "runtime-check publication requires at least one row"
        )
    for index, row in enumerate(additions):
        if not str(row.get("layer") or "").strip() or not str(row.get("id") or "").strip():
            raise RuntimeCheckPublicationError(
                f"runtime-check row {index} requires non-empty layer and id"
            )

    def build(raw: bytes | None) -> tuple[bytes, int]:
        current = json.loads(raw) if raw is not None else []
        if not isinstance(current, list) or not all(
            isinstance(row, dict) for row in current
        ):
            raise RuntimeCheckPublicationError(
                "runtime_checks.json must contain a JSON array of objects"
            )
        replacement_keys = {
            (str(row["layer"]), str(row["id"])) for row in additions
        }
        merged = [
            row
            for row in current
            if (str(row.get("layer")), str(row.get("id")))
            not in replacement_keys
        ]
        merged.extend(additions)
        payload = (json.dumps(merged, indent=1) + "\n").encode()
        return payload, len(additions)

    return prepared_publication.prepare_file_update(
        root,
        destination,
        build,
        authority_binding=authority_binding,
    )
