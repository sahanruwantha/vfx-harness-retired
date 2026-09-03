"""Every key the lifecycle domain requires must be a key a contract row may carry.

Run 20260903T211552Z-8ad44d's layer-2 rematerialization looped on one contract:

    unit staging refused: scene contract sc-facade-parallax: valid_through must be a
                          positive integer
    unit staging refused: unknown contract key(s) valid_through — the harness would
                          ignore them silently; accepted keys are ...

``lifecycle: "window"`` is offered by the authoring vocabulary and requires
``valid_through``; the scene-contract row vocabulary listed ``expires_at`` — a name no
validator, reader or writer used — and refused ``valid_through``.  No contract could
satisfy both validators, so the materializer burned turns oscillating between them
(HIR-0188).
"""

from __future__ import annotations

import pytest

from vfx_harness.domain.contracts import LIFECYCLE_ROW_KEYS, LIFECYCLES, validate_lifecycle
from vfx_harness.evidence.scene_checks import KNOWN_ROW_KEYS


def _row(lifecycle: str, **extra: object) -> dict:
    row = {
        "id": f"sc-{lifecycle}-probe",
        "kind": "mesh_vertex_count",
        "axis": "structure",
        "op": "min",
        "lo": 1,
        "owner_layer": "2",
        "fault_owner": "2",
        "activates_at": "2",
        "lifecycle": lifecycle,
        "roles": ["exterior.facade"],
        "frame": 75,
    }
    row.update(extra)
    return row


def test_the_row_vocabulary_admits_every_lifecycle_key() -> None:
    assert LIFECYCLE_ROW_KEYS <= KNOWN_ROW_KEYS


@pytest.mark.parametrize(
    ("lifecycle", "extra"),
    [
        ("layer", {}),
        ("persistent", {}),
        ("window", {"valid_through": "3"}),
    ],
)
def test_every_declared_lifecycle_can_be_expressed(lifecycle: str, extra: dict) -> None:
    """A lifecycle both validators accept, for each value the vocabulary offers."""
    row = _row(lifecycle, **extra)
    assert validate_lifecycle(row) is None
    assert not sorted(set(row) - KNOWN_ROW_KEYS)


def test_every_offered_lifecycle_is_covered_by_this_test() -> None:
    """A new lifecycle value must come with proof that it can be expressed."""
    assert {"layer", "window", "persistent"} == LIFECYCLES


def test_a_window_row_without_its_end_is_still_refused() -> None:
    """The fix widens the vocabulary; it does not weaken the lifecycle rule."""
    assert validate_lifecycle(_row("window")) == "valid_through must be a positive integer"


def test_a_persistent_row_may_not_carry_an_end() -> None:
    assert validate_lifecycle(_row("persistent", valid_through="3")) == (
        "persistent lifecycle must omit valid_through"
    )


def test_the_vocabulary_names_no_key_no_validator_reads() -> None:
    """`expires_at` was accepted by the row vocabulary and read by nothing."""
    assert "expires_at" not in KNOWN_ROW_KEYS
