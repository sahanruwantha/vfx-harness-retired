"""Active-unit scope is compiled, not guessed from siblings or helper source."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from vfx_harness.agents.build_prompts import builder_kickoff
from vfx_harness.agents.unit_scope import (
    compile_unit_scope,
    format_unit_scope_card,
    helper_inventory,
)
from vfx_harness.domain.work_units import WorkUnit
from vfx_harness.orchestration.ledger import Milestone

_WORKER = Path(__file__).resolve().parents[2] / "src" / "vfx_harness" / "blender" / "worker.py"


def _claim(uid: str, *, contract_id: str, frame: int = 1) -> dict:
    return {
        "id": f"claim.{uid}",
        "proposition": f"{uid} has the declared state",
        "axis": "form",
        "property": f"state.{uid}",
        "subject_roles": [f"{uid}.role"],
        "subject_controls": [],
        "moments": [frame],
        "kind": "atomic",
        "required": True,
        "authority": "executable_required",
        "repair_owner": uid,
        "asserts": "scene",
        "evidence": [{"kind": "scene_contract", "id": contract_id}],
    }


def _unit(uid: str, *, roles: list[str], contract_id: str, extra_contract: str | None = None) -> WorkUnit:
    evaluation: dict = {
        "primary_judge": 1,
        "judge": [{"frame": 1, "ref": "refs/a.png"}],
        "temporal_evidence": "none",
        "claims": [_claim(uid, contract_id=contract_id)],
    }
    if extra_contract:
        evaluation["composition_context"] = {
            "frames": [1],
            "contract_ids": [extra_contract],
        }
    row = {
        "id": uid,
        "title": uid,
        "plan": f"plans/{uid}.md",
        "depends_on": [],
        "mutates": {
            "mode": "scoped",
            "roles": roles,
            "controls": [f"{uid}.gain"],
            "script_spans": [f"build/units/01/{uid}.py"],
        },
        "protects": {
            "selector": "all_active_upstream_interfaces",
            "resolve_to_explicit_ids_at": "freeze",
        },
        "evaluation": evaluation,
        "completion": "all_required_claims_and_protected_contracts_pass",
    }
    return WorkUnit.parse(row, f"unit.{uid}")


def _contracts() -> list[dict]:
    return [
        {
            "id": "cam-spine",
            "kind": "keyframe_exists",
            "roles": ["cam_rig"],
            "frame": 1,
        },
        {
            "id": "cam-vis-f1",
            "kind": "visible_fraction",
            "roles": ["cam.proxy.interior"],
            "frame": 1,
        },
        {
            "id": "fg-exist",
            "kind": "object_count",
            "roles": ["cam.blockout_fg"],
            "frame": 1,
        },
    ]


def test_helper_inventory_is_worker_helpers_not_a_private_copy() -> None:
    inventory = helper_inventory()
    names = {row["name"] for row in inventory}
    declared = set(re.findall(r'"(bvfx_\w+)":', _WORKER.read_text(encoding="utf-8")))
    assert names == declared
    assert "bvfx_camera_rig" in names
    assert "bvfx_role" in names
    by_name = {row["name"]: row for row in inventory}
    assert "role" in by_name["bvfx_camera_rig"]["signature"]
    assert "role" in by_name["bvfx_role"]["signature"]


def test_unit_scope_card_is_the_active_unit_not_a_sibling() -> None:
    camera = _unit("camera_rig", roles=["cam_rig"], contract_id="cam-spine", extra_contract="cam-vis-f1")
    _blockout = _unit("blockout_proxies", roles=["cam.blockout_fg"], contract_id="fg-exist")
    card = compile_unit_scope(unit=camera, layer_id="1", contracts=_contracts())
    assert card["unit_id"] == "camera_rig"
    assert card["mutates"]["roles"] == ["cam_rig"]
    assert [row["id"] for row in card["contracts"]] == ["cam-spine", "cam-vis-f1"]
    dumped = format_unit_scope_card(card)
    assert "cam.blockout_fg" not in dumped
    assert "fg-exist" not in dumped
    assert "cam_rig" in dumped
    assert "bvfx_role" in dumped


def test_unknown_bound_contract_names_requested_and_present() -> None:
    unit = _unit("camera_rig", roles=["cam_rig"], contract_id="missing-spine")
    with pytest.raises(ValueError, match="missing-spine") as caught:
        compile_unit_scope(unit=unit, layer_id="1", contracts=_contracts())
    message = str(caught.value)
    assert "cam-spine" in message
    assert "present:" in message


def test_kickoff_carries_the_compiled_card(tmp_path: Path) -> None:
    (tmp_path / "brief.md").write_text(
        "---\nid: fixture\nframes: 24\nfps: 24\n---\nA fixture shot.\n",
        encoding="utf-8",
    )
    (tmp_path / "refs").mkdir()
    from vfx_harness.domain.brief import load_shot

    shot = load_shot(tmp_path)
    camera = _unit("camera_rig", roles=["cam_rig"], contract_id="cam-spine")
    card = compile_unit_scope(unit=camera, layer_id="1", contracts=_contracts())
    text = builder_kickoff(
        shot,
        Milestone("1@camera_rig", 1, "refs/a.png", "rig exists", ()),
        unit_scope=card,
    )
    assert "UNIT SCOPE CARD" in text
    assert "cam_rig" in text
    assert "bvfx_camera_rig" in text
    assert "cam.blockout_fg" not in text
