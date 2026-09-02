from __future__ import annotations

import pytest

from vfx_harness.agents.builder.layer_artifact import (
    LayerArtifactPublicationConflict,
)
from vfx_harness.agents.builder.layer_composition_finalization import (
    _discard_preserving_primary_error,
)
from vfx_harness.orchestration.layer_evaluation_receipts import (
    LayerEvaluationReceiptConflict,
)
from vfx_harness.orchestration.layer_replay_receipts import (
    LayerReplayReceiptConflict,
)


@pytest.mark.parametrize(
    ("conflict_type", "label"),
    [
        (LayerArtifactPublicationConflict, "layer-artifact"),
        (LayerReplayReceiptConflict, "layer-replay-receipt"),
        (LayerEvaluationReceiptConflict, "layer-evaluation-receipt"),
    ],
)
def test_publication_cleanup_diagnostic_never_replaces_primary_error(
    conflict_type: type[BaseException],
    label: str,
) -> None:
    primary = RuntimeError("primary publication failure")
    prepared = object()

    def fail_cleanup(observed: object) -> None:
        assert observed is prepared
        raise conflict_type("secondary cleanup failure")

    def operation() -> None:
        try:
            raise primary
        except BaseException as exc:
            _discard_preserving_primary_error(
                exc,
                fail_cleanup,
                prepared,
                conflict_type=conflict_type,
                label=label,
            )
            raise

    with pytest.raises(RuntimeError, match="primary publication failure") as observed:
        operation()

    assert observed.value is primary
    assert getattr(primary, "__notes__", ()) == [
        f"{label} cleanup diagnostic: secondary cleanup failure"
    ]
