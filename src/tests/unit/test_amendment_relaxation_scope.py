"""A finding-driven amendment may enlarge a named row, never shrink it (HIR-0232).

room_1046_opening layer 1 carried two camera-owned bands the built geometry falsified
together. The controller dispatched an amendment; it resolved the conflict and, unasked,
raised f1's floor from 0.15 to 0.28 -- past a reference measuring 0.216 by horizontal-span
segmentation. Only f38 was ever wrong. The amendment fixed the broken row and broke the
correct one, at the establishing frame whose authored beat is a small pool of light.
"""

from __future__ import annotations

import json
from pathlib import Path

from vfx_harness.domain.amendment_scope import (
    admissible_interval,
    tightened_conflict_rows,
)
from vfx_harness.orchestration.hypothesis_falsification_projection import (
    PROJECTION_DIR,
    open_conflict_contract_ids,
)

# The exact rows and finding from room_1046_opening, run 20260905T154134Z-2feeec.
CONFLICT = ("cam-bbox-f1-building", "cam-bbox-f38-building")
BASE = [
    {"id": "cam-bbox-f1-building", "op": "band", "lo": 0.15, "hi": 0.35, "owner_layer": "1"},
    {"id": "cam-bbox-f38-building", "op": "band", "lo": 0.55, "hi": 0.80, "owner_layer": "1"},
]
AMENDED = [
    {"id": "cam-bbox-f1-building", "op": "band", "lo": 0.28, "hi": 0.46, "owner_layer": "1"},
    {"id": "cam-bbox-f38-building", "op": "band", "lo": 0.40, "hi": 0.58, "owner_layer": "1"},
]
REFERENCE_F1 = 0.216  # horizontal-span segmentation of refs/frame_0s.jpg


def test_the_real_amendment_is_refused_on_both_unasked_bounds() -> None:
    found = {t.contract_id: t for t in tightened_conflict_rows(CONFLICT, BASE, AMENDED)}
    assert set(found) == set(CONFLICT), "both rows shrank and both must be named"

    f1 = found["cam-bbox-f1-building"]
    assert f1.lost_below > 0 and f1.lost_above == 0, "f1 lost range from the floor"
    assert "raised its floor 0.15 -> 0.28" in f1.describe()

    f38 = found["cam-bbox-f38-building"]
    assert f38.lost_above > 0 and f38.lost_below == 0, "f38 lost range from the ceiling"
    assert "lowered its ceiling 0.8 -> 0.58" in f38.describe()


def test_the_refusal_names_the_permitted_move_as_well_as_the_refused_one() -> None:
    """A refusal that only says "you shrank this" leaves the materializer guessing.

    The finding records residuals in prose, so the binding bound is not readable from it.
    The amendment shows it: the bound that was enlarged is where the resolution took its
    room. Naming both makes the compliant amendment obvious rather than inferable.
    """
    found = {t.contract_id: t for t in tightened_conflict_rows(CONFLICT, BASE, AMENDED)}

    f1 = found["cam-bbox-f1-building"].describe()
    assert "took its room at the ceiling (0.35 -> 0.46)" in f1
    assert "keep that and restore the other bound" in f1

    f38 = found["cam-bbox-f38-building"].describe()
    assert "took its room at the floor (0.55 -> 0.4)" in f38


def test_the_refused_move_is_exactly_what_excluded_the_reference() -> None:
    """The floor raise is not a stylistic objection: it excludes the measured plate."""
    before = admissible_interval(BASE[0])
    after = admissible_interval(AMENDED[0])
    assert before is not None and after is not None
    assert before[0] <= REFERENCE_F1 <= before[1], "the original band contained the reference"
    assert not (after[0] <= REFERENCE_F1 <= after[1]), "the amended band excludes it"


def test_a_resolution_that_only_enlarges_is_accepted() -> None:
    """Enlarging resolves the contradiction and keeps the reference admissible."""
    enlarged = [
        {"id": "cam-bbox-f1-building", "op": "band", "lo": 0.15, "hi": 0.36},
        {"id": "cam-bbox-f38-building", "op": "band", "lo": 0.45, "hi": 0.80},
    ]
    assert tightened_conflict_rows(CONFLICT, BASE, enlarged) == ()
    interval = admissible_interval(enlarged[0])
    assert interval is not None and interval[0] <= REFERENCE_F1 <= interval[1]


def test_an_amendment_with_no_finding_is_not_bound_by_this_rule() -> None:
    """An operator-directed rematerialization is bounded by the operator, not a finding.

    room's corrected remat narrows f1's ceiling back down deliberately, with the
    satisfiability argument in the trigger. Nothing here may refuse that.
    """
    assert tightened_conflict_rows((), AMENDED, BASE) == ()


def test_rows_absent_from_either_side_are_not_compared() -> None:
    """Removal and introduction are different transactions with their own rules."""
    assert tightened_conflict_rows(CONFLICT, BASE, []) == ()
    assert tightened_conflict_rows(CONFLICT, [], AMENDED) == ()


def test_the_reader_does_not_filter_on_the_layer_the_finding_was_raised_at(
    tmp_path: Path,
) -> None:
    """A finding's `layer` is where it was raised, not the layer it indicts.

    room's record carries `layer: "2"` and `unit: "ground_island"` while naming two rows
    owned by layer 1 with a fault owner of `camera_move`. Filtering on that field would
    mean the check never fired on the layer actually being amended.
    """
    directory = tmp_path / PROJECTION_DIR
    directory.mkdir(parents=True)
    (directory / "hf-room.json").write_text(
        json.dumps(
            {
                "record_id": "hf-room",
                "layer": "2",
                "unit": "ground_island",
                "fault_owner_units": ["camera_move"],
                "conflict": {"kind": "contract"},
                "contract_ids": list(CONFLICT),
            }
        ),
        encoding="utf-8",
    )
    found = open_conflict_contract_ids(tmp_path)
    assert found == (("hf-room", CONFLICT),)


def test_a_non_contract_conflict_names_no_rows_and_the_check_is_inert(
    tmp_path: Path,
) -> None:
    directory = tmp_path / PROJECTION_DIR
    directory.mkdir(parents=True)
    (directory / "hf-cap.json").write_text(
        json.dumps(
            {
                "record_id": "hf-cap",
                "layer": "1",
                "conflict": {"kind": "capability"},
                "contract_ids": list(CONFLICT),
            }
        ),
        encoding="utf-8",
    )
    assert open_conflict_contract_ids(tmp_path) == ()
