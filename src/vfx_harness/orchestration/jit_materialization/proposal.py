"""Exact serialized identity of one proposed materialized consumer view."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from vfx_harness.orchestration.authority_selection_transaction import (
    AuthoritySelectionToken,
)
from vfx_harness.orchestration.jit_materialization.schema import (
    OVERLAY_ARTIFACTS,
    MaterializedLayer,
)
from vfx_harness.orchestration.jit_materialization.transition import (
    PreparedMaterializationPublication,
)


@dataclass(frozen=True, slots=True)
class ProposedMaterializationView:
    """Exact candidate overlay staged for the terminal deterministic gate."""

    bundle: Any
    materialized: MaterializedLayer
    documents: dict[str, Any]
    base_selection: AuthoritySelectionToken
    view_hash: str
    artifact_hashes: dict[str, str]
    authored_inputs: dict[str, str]
    decision_inputs: dict[str, dict[str, str | int]]
    planning_inputs_digest: str
    consumer_marker_sha256: str
    consumer_pointer_sha256: str
    publication: PreparedMaterializationPublication


def serialized_documents(documents: dict[str, Any]) -> dict[str, bytes]:
    """Encode proposed overlay documents in their sole publication representation."""

    return {
        name: (json.dumps(documents[name], indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        )
        for name in OVERLAY_ARTIFACTS
    }


def serialized_hashes(payloads: dict[str, bytes]) -> dict[str, str]:
    """Hash every exact proposed overlay byte stream."""

    return {
        name: hashlib.sha256(payloads[name]).hexdigest()
        for name in OVERLAY_ARTIFACTS
    }
