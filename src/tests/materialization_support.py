"""Test-only helpers for strict JIT publication fixtures.

Production publication accepts only the exact candidate/base/view identity that passed
the terminal gate.  Focused tests that are not exercising the gate itself still need a
fully bound finalization; these helpers derive that record from the real composition
functions instead of weakening or bypassing the publisher contract.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from vfx_harness.orchestration.jit_materialization import publish as jit_publish
from vfx_harness.orchestration.jit_materialization.proposal import (
    serialized_documents,
    serialized_hashes,
)
from vfx_harness.orchestration.jit_materialization.schema import (
    attest_materialization_finalization,
)
from vfx_harness.orchestration.jit_materialization.view_pointer import (
    canonical_view_hash,
)
from vfx_harness.orchestration.plan_inputs import (
    exact_planning_input_identity_digest,
)


def attest_exact_materialization_view(
    shot_folder: str | Path,
    candidate: str | Path,
    *,
    overlay_root: str | Path | None = None,
) -> None:
    """Attest the exact composed bytes a focused fixture is about to publish."""

    shot = Path(shot_folder).resolve()
    candidate_path = Path(candidate)
    bundle, materialized, bases, base_selection = jit_publish._composed_documents(
        shot,
        candidate_path,
        overlay_root=overlay_root,
    )
    documents = jit_publish._overlay_documents(materialized, bases)
    artifact_hashes = serialized_hashes(
        serialized_documents(documents)
    )
    attest_materialization_finalization(
        candidate_path,
        bundle_hash=bundle.content_hash,
        base_selection=base_selection,
        proposed_view_hash=canonical_view_hash(documents),
        proposed_artifact_hashes=artifact_hashes,
        planning_inputs_digest=exact_planning_input_identity_digest(shot),
        consumer_marker_sha256=hashlib.sha256(
            b"test-only-terminal-gate-marker"
        ).hexdigest(),
    )
