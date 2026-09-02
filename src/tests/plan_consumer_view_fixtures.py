"""Shared construction of one installed, registered plan-consumer view for tests."""

from __future__ import annotations

import hashlib
from pathlib import Path

from vfx_harness.observability.run_artifacts import RunLayout
from vfx_harness.orchestration.authority_selection_transaction import (
    AuthoritySelectionToken,
)
from vfx_harness.orchestration.jit_materialization.schema import OVERLAY_ARTIFACTS
from vfx_harness.orchestration.plan_consumer_view import PlanConsumerViewMarker
from vfx_harness.orchestration.plan_consumer_view_mutation import (
    allocating_plan_consumer_view,
    constructing_plan_consumer_view,
    install_plan_consumer_view,
    prepare_plan_consumer_view_installation,
)


def registered_consumer_view(
    root: Path,
) -> tuple[Path, Path, PlanConsumerViewMarker]:
    """Install one empty consumer view under ``root/shot`` and return its identity."""

    shot = root / "shot"
    shot.mkdir()
    layout = RunLayout(
        shot=shot,
        run_id="test",
        root=shot / "runs" / "test",
    )
    layout.scratch.mkdir(parents=True)
    digest = hashlib.sha256(b"consumer-view-test").hexdigest()
    marker = PlanConsumerViewMarker(
        shot=shot,
        bundle=shot / "runs" / "publisher" / "checkpoints" / digest,
        content_hash=digest,
        base_selection=AuthoritySelectionToken(0, None, 0, None),
        view_source="bundle",
        view_digest=digest,
        artifact_hashes=dict.fromkeys(OVERLAY_ARTIFACTS, digest),
        authored_inputs={},
        decision_inputs={},
    )
    with allocating_plan_consumer_view(layout) as (_temporary, allocation), constructing_plan_consumer_view(
        layout,
        allocation,
        marker,
    ) as capability:
        installation = prepare_plan_consumer_view_installation(capability)
    view = layout.scratch / "plan-consumer-view"
    install_plan_consumer_view(installation, view)
    return shot, view, marker
