"""The amendment-scope check, exercised through the public validator (HIR-0232).

Deliberately imports no symbol this change introduced: on the pre-fix tree it must fail
because the validator produces no such finding, not because a module is missing. That is
the difference between proving a mechanism works and proving a name exists.
"""

from __future__ import annotations

import json
from pathlib import Path

CONFLICT_ROW_BASE = {"op": "band", "lo": 0.15, "hi": 0.35}
CONFLICT_ROW_AMENDED = {"op": "band", "lo": 0.28, "hi": 0.46}
FALSIFICATIONS = Path("state") / "hypothesis-falsifications"


def test_the_validator_reports_the_tightening_end_to_end(tmp_path: Path) -> None:
    """The discriminator: reverting the mechanism loses a FINDING, not a symbol.

    This imports only `validate_materialization`, so on the pre-fix tree it fails because
    the note is absent -- not because a new module is missing.
    """
    import pytest as _pytest

    from tests.unit.test_plan_records import (
        _add_deferred_layer,
        _candidate,
        _jit_payload,
        _write,
        publish_current,
    )
    from vfx_harness.observability import run_artifacts
    from vfx_harness.orchestration.jit_materialization import (
        validate_materialization,
    )

    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    layout = run_artifacts.create(tmp_path, "amendment-scope")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    payload = _jit_payload(tmp_path, bundle.content_hash)
    data = json.loads(payload.read_text(encoding="utf-8"))

    # A row this candidate carries, and a base view where it was wider.
    rows = data.get("scene_contracts") or []
    if not rows:
        _pytest.skip("fixture carries no scene contracts to narrow")
    target = str(rows[0]["id"])
    rows[0].update({"op": "band", "lo": 0.28, "hi": 0.46})
    _write(payload, data)

    base = Path(bundle.root) / "scene_checks.json"
    base.write_text(
        json.dumps(
            {
                "schema": "vfx-harness.scene-checks/v1",
                "contracts": [
                    {"id": target, "kind": "bbox_height", "frame": 1,
                     "op": "band", "lo": 0.15, "hi": 0.35, "owner_layer": "1"}
                ],
            }
        ),
        encoding="utf-8",
    )
    directory = Path(tmp_path) / FALSIFICATIONS
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "hf-e2e.json").write_text(
        json.dumps(
            {
                "record_id": "hf-e2e",
                "layer": "2",
                "conflict": {"kind": "contract"},
                "contract_ids": [target],
            }
        ),
        encoding="utf-8",
    )

    with _pytest.raises(ValueError) as excinfo:
        validate_materialization(
            bundle.root,
            payload,
            expected_bundle_hash=bundle.content_hash,
            shot_folder=tmp_path,
            base_scene_checks_path=base,
        )
    message = str(excinfo.value)
    assert "hf-e2e" in message, message
    assert "raised its floor 0.15 -> 0.28" in message, message
    assert "did not put in question" in message, message
