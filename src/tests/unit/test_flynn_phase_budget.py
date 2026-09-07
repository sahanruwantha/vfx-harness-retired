"""Do not spend on a candidate that cannot reach measured canonical completion."""
import pytest

engine = pytest.importorskip("vfx_harness.agents.builder.flynn_unit")


@pytest.mark.parametrize("written,observed,inference,tool,external,expected", [
    (False, None, 4, 4, 3, {"write_candidate", "abstain"}),
    (False, None, 5, 5, 4, {"inspect_unit", "write_candidate", "abstain"}),
    (True, None, 3, 3, 2, {"probe_candidate", "abstain"}),
    (True, "measured", 4, 4, 3, {"write_candidate", "freeze_candidate", "abstain"}),
    (True, "measured", 4, 4, 2, {"freeze_candidate", "abstain"}),
    (True, "measured", 4, 3, 3, {"freeze_candidate", "abstain"}),
    (True, "measured", 3, 4, 3, {"freeze_candidate", "abstain"}),
    (True, "measured", 2, 2, 1, {"freeze_candidate", "abstain"}),
    (True, None, 2, 2, 1, {"abstain"}),
    (False, None, 3, 3, 3, {"abstain"}),
])
def test_grants_preserve_the_whole_remaining_path(written, observed, inference, tool, external, expected):
    assert set(engine._model_grants(
        inspected=False, written=written, observed=observed,
        remaining={"inference": inference, "tool": tool, "external": external},
    )) == expected
