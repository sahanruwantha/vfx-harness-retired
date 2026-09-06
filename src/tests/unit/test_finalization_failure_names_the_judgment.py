"""A finalization failure names the judgment, not just the status.

hansa_silk_road run 20260906T095020Z-3ba2cc reached the operator as:

    harness_defect: The 'build' boundary returned without typed stop authority; it raised
    RequestedExit: layer 2 has no complete receipt-bound publication:
    layer 2 terminal finalization is 'failed', not 'passed'

The receipt one attribute away carried three canonical rows decided `no_optical_signal`
at frames 1, 51 and 151, and an evaluation group naming the debt and the requirement they
were paying. The frame held all of it and reported one field.

The distinction the tests below pin is the one a reader acts on: **a judgment that scored
badly and a judgment that could not be made are different failures with different owners.**
"""

from __future__ import annotations

from vfx_harness.domain.layer_finalization_diagnosis import (
    EVIDENCE_UNAVAILABLE_DECIDERS,
    describe_failed_finalization,
)

# The shape read from hansa's own receipt.
HANSA_CANONICAL = [
    {"frame": 1, "verdict": {"pass": True, "decided_by": "unit_executable_evidence"}},
    {"frame": 51, "verdict": {"pass": True, "decided_by": "unit_executable_evidence"}},
    {"frame": 151, "verdict": {"pass": True, "decided_by": "unit_executable_evidence"}},
    {
        "frame": 1,
        "verdict": {
            "pass": False,
            "decided_by": "no_optical_signal",
            "issues": ["candidate plate has no optical signal (black/empty)."],
        },
    },
    {"frame": 51, "verdict": {"pass": False, "decided_by": "no_optical_signal", "issues": []}},
    {"frame": 151, "verdict": {"pass": False, "decided_by": "no_optical_signal", "issues": []}},
]
HANSA_GROUPS = [
    {"group_index": 0, "canonical_start": 0, "canonical_end": 3, "requirement_ids": ["R15"]},
    {
        "group_index": 2,
        "canonical_start": 3,
        "canonical_end": 6,
        "debt_id": "jd-06fa28eac2ac05f7946072debde348383d17aef95a74ac75e6f6f1b3bf17bff6",
        "requirement_ids": ["R50"],
    },
]


def test_it_names_the_frames_the_decider_and_the_owner() -> None:
    described = describe_failed_finalization(HANSA_CANONICAL, HANSA_GROUPS)

    assert "f1, f51, f151" in described
    assert "no_optical_signal" in described
    assert "requirement R50" in described
    assert "debt jd-06fa28eac2ac05f79" in described
    assert "group 2" in described


def test_unavailable_evidence_and_a_negative_verdict_read_differently() -> None:
    """The distinction the operator acts on, and the one this exists for."""
    unavailable = describe_failed_finalization(HANSA_CANONICAL, HANSA_GROUPS)
    negative = describe_failed_finalization(
        [
            {
                "frame": 51,
                "verdict": {
                    "pass": False,
                    "decided_by": "unit_executable_evidence",
                    "issues": ["[check:hero-visible-f51] visible_fraction reads 0.19 against >= 0.30"],
                },
            }
        ],
        [{"group_index": 0, "canonical_start": 0, "canonical_end": 1, "requirement_ids": ["R8"]}],
    )

    assert "the evidence could not be produced" in unavailable
    assert "the work was judged and did not pass" in negative
    assert "the evidence could not be produced" not in negative
    # And the negative one still carries what failed and who owns it.
    assert "visible_fraction reads 0.19" in negative
    assert "requirement R8" in negative


def test_a_contract_gap_counts_as_unavailable_evidence() -> None:
    """An incomplete authority did not weigh the work either."""
    for decider in ("uncovered_judge_frame", "lookless_requires_executable_claims"):
        assert decider in EVIDENCE_UNAVAILABLE_DECIDERS
        described = describe_failed_finalization(
            [{"frame": 7, "verdict": {"pass": False, "decided_by": decider}}], []
        )
        assert "the evidence could not be produced" in described


def test_an_ordinary_measured_failure_is_not_called_unavailable() -> None:
    assert "unit_executable_evidence" not in EVIDENCE_UNAVAILABLE_DECIDERS


def test_a_row_with_no_group_says_so_rather_than_inventing_one() -> None:
    described = describe_failed_finalization(
        [{"frame": 7, "verdict": {"pass": False, "decided_by": "no_optical_signal"}}], []
    )

    assert "no group" in described


def test_nothing_failing_returns_nothing() -> None:
    """A caller reporting a non-passed status with no failing row has a different problem
    and must say so in its own words rather than borrow these."""
    assert describe_failed_finalization(HANSA_CANONICAL[:3], HANSA_GROUPS) == ""
    assert describe_failed_finalization([], []) == ""


def test_several_deciders_are_reported_separately_not_merged() -> None:
    described = describe_failed_finalization(
        [
            {"frame": 1, "verdict": {"pass": False, "decided_by": "no_optical_signal"}},
            {"frame": 2, "verdict": {"pass": False, "decided_by": "unit_executable_evidence"}},
        ],
        [],
    )

    assert "no_optical_signal" in described
    assert "unit_executable_evidence" in described
    assert " | " in described
