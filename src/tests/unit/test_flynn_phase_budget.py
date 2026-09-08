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


@pytest.mark.parametrize('written,observed,captures,payments,can_pay,unpaid,steps,external,expected', [
    (False, None, 1, 1, False, True, 6, 5, {'abstain', 'write_candidate'}),
    (False, None, 2, 1, False, True, 6, 5, {'abstain'}),
    (True, None, 1, 1, False, True, 5, 4, {'abstain', 'capture_unit_frame', 'probe_candidate'}),
    (True, None, 0, 1, True, True, 4, 3, {'abstain', 'propose_checks', 'probe_candidate'}),
    (True, 'measured', 0, 1, True, True, 2, 1, {'abstain'}),
    (True, 'measured', 1, 0, False, False, 2, 1, {'abstain'}),
    (True, 'measured', 0, 0, True, False, 2, 1, {'abstain', 'freeze_candidate'}),
])
def test_image_phases_reserve_capture_payment_probe_and_canonical(
    written, observed, captures, payments, can_pay, unpaid, steps, external, expected,
):
    assert set(engine._model_grants(
        inspected=False, written=written, observed=observed,
        remaining={'inference': steps, 'tool': steps, 'external': external},
        image_tools=True, captures=captures, payments=payments, can_pay=can_pay, unpaid=unpaid,
    )) == expected


def test_rewriting_a_paid_candidate_reserves_recapture():
    grants = engine._model_grants(
        inspected=True, written=True, observed='measured',
        remaining={'inference': 4, 'tool': 4, 'external': 3},
        image_tools=True, can_pay=True, write_captures=1,
    )
    assert 'write_candidate' not in grants
    assert 'freeze_candidate' in grants


@pytest.mark.parametrize('written,observed,captures,payments,steps,external,allowed', [
    (False, None, 0, 0, 5, 3, True),
    (False, None, 0, 0, 4, 3, False),
    (True, None, 0, 0, 8, 8, False),
    (True, 'measured', 1, 1, 7, 5, True),
    (True, 'measured', 1, 1, 6, 5, False),
    (True, 'measured', 1, 1, 7, 4, False),
])
def test_recipe_reads_leave_capacity_for_candidate_and_evidence(
    written, observed, captures, payments, steps, external, allowed,
):
    grants = engine._model_grants(
        inspected=True, written=written, observed=observed, recipes=True,
        captures=captures, payments=payments, write_captures=captures,
        remaining={'inference': steps, 'tool': steps, 'external': external},
    )
    assert ('find_recipe' in grants) is allowed
