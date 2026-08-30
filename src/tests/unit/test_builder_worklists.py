from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from tests.unit.test_vis_repair_authority import _detail_unit
from vfx_harness.agents.builder import _composition_judge_unit, _worklist_evidence
from vfx_harness.observability.worklists import (
    load_unit_worklist,
    write_unit_worklist,
)
from vfx_harness.orchestration.unit_state import unit_digest


def test_worklist_is_bound_to_exact_unit_digest_and_ignores_layer_legacy(
    tmp_path: Path,
) -> None:
    legacy = tmp_path / "state" / "worklists" / "layer-2.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(
        json.dumps({"items": ["foreign old defect"], "done": [], "notes": []}),
        encoding="utf-8",
    )
    unit = _detail_unit(provides=["geometry"])

    assert _worklist_evidence(tmp_path, "2", unit) == []

    digest = unit_digest(unit)
    path, state = load_unit_worklist(
        tmp_path, layer_id="2", unit_id=unit.id, unit_hash=digest
    )
    state["items"] = ["current defect"]
    write_unit_worklist(path, state)

    evidence = _worklist_evidence(tmp_path, "2", unit)
    assert len(evidence) == 1
    assert evidence[0]["id"] == "L2@detail-builder-worklist-complete"
    assert evidence[0]["open_items"] == ["current defect"]

    changed = replace(unit, title="Changed generation")
    assert unit_digest(changed) != digest
    assert _worklist_evidence(tmp_path, "2", changed) == []


def test_worklist_identity_mismatch_fails_closed(tmp_path: Path) -> None:
    unit = _detail_unit(provides=["geometry"])
    digest = unit_digest(unit)
    path, state = load_unit_worklist(
        tmp_path, layer_id="2", unit_id=unit.id, unit_hash=digest
    )
    state["unit_id"] = "other"
    write_unit_worklist(path, state)

    evidence = _worklist_evidence(tmp_path, "2", unit)

    assert len(evidence) == 1
    assert evidence[0]["id"] == "L2@detail-builder-worklist-valid"
    assert not evidence[0]["pass"]
    assert "identity mismatch" in evidence[0]["error"]


def test_lookless_composition_fans_in_each_constituent_worklist(tmp_path: Path) -> None:
    first = _detail_unit(provides=["geometry"])
    second = replace(
        first,
        id="detail_successor",
        title="Detail successor",
        plan="plans/units/detail_successor.md",
    )
    layer = SimpleNamespace(id="2", stages=(first, second))
    composition = _composition_judge_unit(layer)
    assert composition is not None

    first_path, first_state = load_unit_worklist(
        tmp_path,
        layer_id="2",
        unit_id=first.id,
        unit_hash=unit_digest(first),
    )
    first_state["items"] = ["unfinished first-unit work"]
    write_unit_worklist(first_path, first_state)

    second_path, second_state = load_unit_worklist(
        tmp_path,
        layer_id="2",
        unit_id=second.id,
        unit_hash=unit_digest(second),
    )
    second_state["items"] = ["finished successor work"]
    second_state["done"] = ["finished successor work"]
    write_unit_worklist(second_path, second_state)

    evidence = _worklist_evidence(tmp_path, "2", composition)

    assert [row["id"] for row in evidence] == [
        "L2@detail-builder-worklist-complete",
        "L2@detail_successor-builder-worklist-complete",
    ]
    assert evidence[0]["open_items"] == ["unfinished first-unit work"]
    assert not evidence[0]["pass"]
    assert evidence[1]["open_items"] == []
    assert evidence[1]["pass"]
