"""A gate report's signature must satisfy the validator that recomputes it (HIR-0193).

Run 20260904T031???Z on `artifacts/room_1046_opening` produced one blocking finding and
routed the entire run to the engineering sink:

    plan-stop-evidence.json  issues: ['gate_report_signature_invalid']
    ✗ controller refused dispatch (not_dispatchable): route_engineering has no
      receipt-backed controller adapter

The signature is built from a fixed 60-character slice of each finding's text. That
finding's text has a space at index 59, so the slice ended in whitespace, and the
published-report validator requires `value == value.strip()`. The producer and the reader
each implemented the rule, and they disagreed.
"""

from __future__ import annotations

import pytest

from vfx_harness.agents.planner.planning_stop import _text
from vfx_harness.evaluation.plan_gate.types import Finding, GateResult, gate_report_signature


def _result(what: str) -> GateResult:
    return GateResult("shot", [Finding.in_layer("capability-origin", True, "1", "units a, b", what)], {})


# index 59 is a space, so a naive [:60] slice ends in whitespace
_CUTS_ON_A_SPACE = "units cam_lens, cam_path each declare provides 'camera' and none reaches another"


def test_the_observed_text_produced_an_invalid_signature_before_the_fix() -> None:
    """Pin the exact shape: the naive slice really does end in whitespace."""
    naive = _CUTS_ON_A_SPACE[:60]
    assert naive != naive.strip()


def test_the_signature_satisfies_the_validator_that_recomputes_it() -> None:
    signature = _result(_CUTS_ON_A_SPACE).signature()
    assert _text(signature)


def test_the_producer_and_the_reader_compute_the_same_value() -> None:
    result = _result(_CUTS_ON_A_SPACE)
    rows = [{"check": f.check, "where": f.where, "what": f.what} for f in result.findings]
    assert gate_report_signature(rows) == result.signature()


@pytest.mark.parametrize(
    "what",
    [
        _CUTS_ON_A_SPACE,
        "x" * 59 + " " + "tail",           # space exactly at the cut
        "   leading and trailing space   ",
        "short",
        "\ttabbed" + " " * 60,
    ],
)
def test_no_finding_text_can_produce_an_unusable_signature(what: str) -> None:
    signature = _result(what).signature()
    assert _text(signature)


def test_an_empty_report_has_an_empty_signature() -> None:
    """A clean report signs as empty; only non-empty reports reach the stop parser."""
    assert GateResult("shot", [], {}).signature() == ""


def test_the_signature_still_distinguishes_different_findings() -> None:
    """Stripping must not collapse distinct findings into one identity."""
    a = _result("first problem").signature()
    b = _result("second problem").signature()
    assert a != b
