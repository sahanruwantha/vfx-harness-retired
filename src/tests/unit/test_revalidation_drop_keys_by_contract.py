"""A dropped image debt is recorded once per contract id, not once per frame (HIR-0237).

`hansa_silk_road` layer 2, attempt 6: all four units sealed, then

    ValueError: layer finalization receipt projection.revalidation.result.dropped
                contains duplicate ids

`hero-facade-appearance-debt` read 1.826 against `>= 2` at two frames. A multi-frame image
debt has one runtime row per frame, each bound to its own candidate handle, so the
producer appended the same id twice -- and the drop record's whole shape is
`{"id", "reason"}`, with no frame that could distinguish them. The projection validator
refused, and a layer whose build was good could not seal.
"""

from __future__ import annotations

import json
from pathlib import Path

from vfx_harness.domain.layer_finalization_projection import _validate_revalidation_projection
from vfx_harness.evidence.checks import prepare_layer_revalidation


def _row(identifier: str, frame: int) -> dict:
    return {
        "id": identifier, "origin": "builder", "layer": "2", "frame": frame,
        "metric": "frame_detail", "op": "min", "lo": 2.0,
    }


def _prepared(tmp_path: Path, rows: list[dict]):
    shot = tmp_path / "shot"
    (shot / "artifacts").mkdir(parents=True)
    checks = shot / "runtime_checks.json"
    checks.write_text(json.dumps(rows), encoding="utf-8")
    # No shipped render for any frame: every row drops, which is the failing path.
    return prepare_layer_revalidation(shot, "2", lambda _c: None)


def test_one_contract_dropped_at_two_frames_yields_one_entry(tmp_path: Path) -> None:
    prepared = _prepared(
        tmp_path,
        [_row("hero-facade-appearance-debt", 51), _row("hero-facade-appearance-debt", 151)],
    )
    dropped = prepared.result["dropped"]
    ids = [identifier for identifier, _reason in dropped]
    assert ids == ["hero-facade-appearance-debt"], dropped
    assert len(ids) == len(set(ids)), "the projection validator refuses duplicate ids"


def test_the_single_entry_names_both_frames_so_nothing_is_lost(tmp_path: Path) -> None:
    prepared = _prepared(
        tmp_path,
        [_row("hero-facade-appearance-debt", 51), _row("hero-facade-appearance-debt", 151)],
    )
    (_identifier, reason), = prepared.result["dropped"]
    assert "f51" in reason and "f151" in reason, reason


def test_distinct_contracts_stay_distinct(tmp_path: Path) -> None:
    """Aggregation must key on the id, not collapse the list."""
    prepared = _prepared(tmp_path, [_row("debt-a", 51), _row("debt-b", 51)])
    ids = sorted(identifier for identifier, _reason in prepared.result["dropped"])
    assert ids == ["debt-a", "debt-b"], prepared.result["dropped"]


def test_the_projection_validator_accepts_the_result(tmp_path: Path) -> None:
    """The discriminator: the exact validator that refused hansa's receipt.

    Only `dropped` comes from the producer; every other field is the minimum the
    validator requires, so a failure here is about duplicate ids and nothing else.
    """
    import hashlib

    from vfx_harness.evidence.layer_revalidation_projection import (
        _LAYER_REVALIDATION_PROJECTION_SCHEMA as SCHEMA,
    )

    prepared = _prepared(
        tmp_path,
        [_row("hero-facade-appearance-debt", 51), _row("hero-facade-appearance-debt", 151)],
    )
    replacement_text = "[]\n"
    _validate_revalidation_projection(
        {
            "schema": SCHEMA,
            "layer_id": "2",
            "source_sha256": prepared.source_sha256,
            "replacement_sha256": hashlib.sha256(
                replacement_text.encode("utf-8")
            ).hexdigest(),
            "replacement_text": replacement_text,
            "result": {
                "kept": prepared.result["kept"],
                "dropped": [
                    {"id": identifier, "reason": reason}
                    for identifier, reason in prepared.result["dropped"]
                ],
            },
        },
        "projection.revalidation",
        layer_id="2",
    )


def test_every_drop_path_goes_through_the_single_recorder() -> None:
    """Four branches can drop a row; the duplicate arises in all of them.

    The observed crash came from the threshold branch (1.826 against `>= 2` at two
    frames); the test above exercises the provenance branch, because a threshold drop
    needs a real render and adversary. Rather than leave three branches unpinned, assert
    the funnel: no branch appends to the list directly.

    The bug is invisible on the happy path -- a duplicate only arises when a multi-frame
    debt is DROPPED, which only happens when it FAILS. That is why it survived until a
    build good enough to seal every unit finally failed one debt at two frames.
    """
    import inspect

    from vfx_harness.evidence import checks

    source = inspect.getsource(checks)
    assert "dropped.append" not in source, "a drop must go through the per-contract recorder"
    assert source.count("_drop(") >= 5, "four drop branches plus the definition"
