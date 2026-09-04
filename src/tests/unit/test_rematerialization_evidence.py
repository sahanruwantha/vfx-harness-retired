"""A rematerialization's kickoff carries the findings it was dispatched against (HIR-0191)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vfx_harness.agents.planner import rematerialization_evidence as evidence


def _finding(shot: Path, name: str, *, layer: str = "2", reason: str = "no box fits") -> Path:
    directory = shot / "state" / "hypothesis-falsifications"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.json"
    digest = "a" * 64
    path.write_text(
        json.dumps(
            {
                "schema": "vfx-harness.hypothesis-falsification/v1",
                "record_id": name,
                "recorded_at": "2026-09-04T00:00:00+00:00",
                "layer": layer,
                "unit": "building_shell",
                "identities": dict.fromkeys(
                    (
                        "bundle_hash",
                        "plan_hash",
                        "unit_hash",
                        "unit_plan_hash",
                        "candidate_hash",
                        "settings_hash",
                    ),
                    digest,
                ),
                "contract_ids": ["corner-f113", "far-f1"],
                "observations": [
                    {
                        "classification": "unsatisfiable_in_scope",
                        "contract_ids": ["corner-f113"],
                        "reason": reason,
                    }
                ],
                "decisions": [],
                "conflict": {
                    "kind": "contract",
                    "required_authority": "replace the camera path so the corner fits at f113",
                    "roles": ["building"],
                    "controls": [],
                },
                "evidence": ["build/units/02/building_shell.py"],
                "affected": ["building_shell", "facade_modules"],
                "fault_owner_units": ["camera_rig"],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_findings_render_compactly_and_other_locators_are_named(tmp_path: Path) -> None:
    _finding(tmp_path, "hf-shell")
    (tmp_path / "reports").mkdir()
    other = tmp_path / "reports" / "plan-stop-evidence.json"
    other.write_text("{}", encoding="utf-8")

    shell = "state/hypothesis-falsifications/hf-shell.json"
    block = evidence.replacement_evidence_block(
        tmp_path,
        (shell, "reports/plan-stop-evidence.json", shell),
    )

    assert block.startswith("EVIDENCE THIS REPLACEMENT MUST ANSWER")
    assert block.count("finding hf-shell") == 1, "duplicate locators render once"
    assert "layer 2 unit building_shell" in block
    assert "contracts: corner-f113, far-f1" in block
    assert "unsatisfiable_in_scope [corner-f113]: no box fits" in block
    assert "required authority: replace the camera path" in block
    assert "fault owners: camera_rig" in block
    assert "affected units: building_shell, facade_modules" in block
    assert "- evidence reports/plan-stop-evidence.json" in block


def test_block_is_bounded_and_empty_without_locators(tmp_path: Path) -> None:
    assert evidence.replacement_evidence_block(tmp_path, ()) == ""
    long_reason = "x" * 5000
    locators = []
    for index in range(6):
        _finding(tmp_path, f"hf-{index}", reason=long_reason)
        locators.append(f"state/hypothesis-falsifications/hf-{index}.json")

    block = evidence.replacement_evidence_block(tmp_path, tuple(locators))

    assert len(block) <= evidence.BLOCK_LIMIT
    assert "x" * (evidence.REASON_LIMIT + 1) not in block


def test_locators_cannot_escape_the_shot_root(tmp_path: Path) -> None:
    shot = tmp_path / "shot"
    shot.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError):
        evidence.replacement_evidence_block(shot, ("../outside.json",))
