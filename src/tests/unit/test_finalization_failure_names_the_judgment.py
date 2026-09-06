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

from types import SimpleNamespace

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


def test_a_falsified_provisional_requirement_is_a_judgment_made_not_evidence_missing():
    """Driven through the real producer, because the defect was in reading its output.

    `_provisional_composition_contract_gap` runs *after* a qualified critic identified a
    concrete defect. It returns early for `no_optical_signal` precisely because that case
    is different, then clears `issues` and moves the criticism into `contract_gaps`.

    Classifying its decider as unavailable evidence therefore made two errors at once: it
    told the reader the plate could not be judged when a critic had judged it, and it
    dropped the observation, because the only place the criticism still lived was the field
    the producer had moved it to.
    """
    from vfx_harness.agents.builder.verdicts import _provisional_composition_contract_gap

    unit = SimpleNamespace(
        provisional_requirement_ids=("R41",),
        provisional_debt_ids=("jd-abc123",),
        mutates=SimpleNamespace(roles=("building.mass.crown",)),
    )
    judged = _provisional_composition_contract_gap(
        {
            "pass": False,
            "decided_by": "critic",
            "issues": ["Tower crown is absent"],
            "observation_reconciliation": [
                {
                    "state": "actionable",
                    "observation": {
                        "claim_id": "judgment-debt:jd-abc123:crown",
                        "observation": "Tower crown is absent",
                        "action": "model the crown",
                    },
                }
            ],
        },
        unit,
        121,
    )
    # The producer's own postconditions, asserted so this test fails loudly rather than
    # vacuously if it ever stops moving the criticism.
    assert judged["decided_by"] == "provisional_requirement_contract_gap"
    assert judged["issues"] == []
    assert judged["contract_gaps"][0]["observation"]["observation"] == "Tower crown is absent"

    diagnosis = describe_failed_finalization([{"frame": 121, "verdict": judged}])

    assert "the work was judged and did not pass" in diagnosis
    assert "the evidence could not be produced" not in diagnosis
    assert "Tower crown is absent" in diagnosis


def test_a_no_signal_verdict_is_still_unavailable_evidence():
    """The other half: the producer returns this one untouched, and it must stay so."""
    diagnosis = describe_failed_finalization(
        [{"frame": 1, "verdict": {"pass": False, "decided_by": "no_optical_signal"}}]
    )

    assert "the evidence could not be produced" in diagnosis
