"""The authoring diet's core proof: a valid compact mapping expands into a complete
authority surface that the FULL deterministic plan gate accepts by construction, across
heterogeneous shot families — and everything mechanical (ids, citations, exact text,
derived owned_requirements, empty evidence documents, global.md) is harness-generated,
never model-authored."""

from __future__ import annotations

from pathlib import Path

import pytest

from vfx_harness.evaluation import plan_gate
from vfx_harness.orchestration.plan_authoring import (
    clause_registry,
    derive_owned_requirements,
    expand_mapping,
    registry_prompt_block,
    validate_mapping,
)

PRODUCT_BRIEF = """---
id: product-fixture
title: Studio object
type: still
frames: 1
fps: 24
resolution: [1920, 1080]
engine: BLENDER_EEVEE_NEXT
---

# Studio object

## Intent

A single hero object photographed on a seamless studio sweep with soft key light.

## Look

- The object sits centred with a gentle reflection under it.
- The backdrop stays clean with no visible horizon seam.

## Deliverables

- One approval still at frame 1.
"""

MOTION_BRIEF = """---
id: motion-fixture
title: Two part move
type: motion
frames: 48
fps: 24
resolution: [1920, 1080]
engine: BLENDER_EEVEE_NEXT
---

# Two part move

## Intent

A dolly move reveals a plaza, then dressing elements settle into place.

## Beats

| Beat | Frames | Required action |
|---|---:|---|
| B1 | 1-24 | The camera dollies forward to reveal the plaza. |
| B2 | 25-48 | Dressing elements settle into their final positions. |

## Motion

- The camera must travel continuously with motivated easing throughout.

## Deliverables

- Approval stills at frames 24 and 48.
"""


def _shot(tmp_path: Path, brief: str, refs: list[str]) -> Path:
    (tmp_path / "brief.md").write_text(brief, encoding="utf-8")
    (tmp_path / "refs").mkdir()
    for name in refs:
        (tmp_path / "refs" / name).write_bytes(b"png")
    return tmp_path


def _product_mapping(registry) -> dict:
    rids = [row["id"] for row in registry]
    resolutions = {rid: {"kind": "deferred_owner", "owner_layer": "1"} for rid in rids}
    resolutions[rids[-1]] = {
        "kind": "decision",
        "statement": "One approval still at frame 1 is the delivery.",
        "decision_strength": "hard_constraint",
    }
    return {
        "schema": "vfx-harness.ownership-mapping/v1",
        "layers": [{
            "id": "1", "title": "Hero object", "script": "build/01_hero.py",
            "charter": "authored brief", "primary_judge": 1,
            "judge": [{"frame": 1, "ref": "refs/f001.png"}],
            "owns": ["object_presentation"], "evidence_domains": ["scene", "image"],
            "depends_on": [], "reserved_roles": ["hero.*"],
        }],
        "axes": [{"key": "object_presentation", "desc": "centred, reflected, clean"}],
        "resolutions": resolutions,
        "blockers": [],
    }


def _motion_mapping(registry) -> dict:
    rids = [row["id"] for row in registry]
    by_text = {row["id"]: row["statement"] for row in registry}
    resolutions = {}
    for rid in rids:
        text = by_text[rid].lower()
        owner = "2" if ("settle" in text or "48" in text) else "1"
        resolutions[rid] = {"kind": "deferred_owner", "owner_layer": owner}
    return {
        "schema": "vfx-harness.ownership-mapping/v1",
        "layers": [
            {"id": "1", "title": "Camera reveal", "script": "build/01_camera.py",
             "charter": "authored brief", "primary_judge": 24,
             "judge": [{"frame": 24, "ref": "refs/f024.png"}],
             "owns": ["camera_motion"], "evidence_domains": ["scene", "temporal"],
             "depends_on": [], "reserved_roles": ["camera.*"]},
            {"id": "2", "title": "Dressing settle", "script": "build/02_dressing.py",
             "charter": "camera reveal outcome", "primary_judge": 48,
             "judge": [{"frame": 48, "ref": "refs/f048.png"}],
             "owns": ["dressing_settle"], "evidence_domains": ["scene", "temporal"],
             "depends_on": ["1"], "reserved_roles": ["dressing.*"]},
        ],
        "axes": [
            {"key": "camera_motion", "desc": "continuous motivated dolly"},
            {"key": "dressing_settle", "desc": "elements reach final positions"},
        ],
        "resolutions": resolutions,
        "blockers": [],
    }


def test_product_family_expansion_is_gate_clean_by_construction(tmp_path: Path) -> None:
    shot = _shot(tmp_path, PRODUCT_BRIEF, ["f001.png"])
    registry = clause_registry(shot / "brief.md")
    mapping = _product_mapping(registry)

    expand_mapping(shot, mapping)
    result = plan_gate.run(shot, "plans/global.md", require_scene_checks=True)

    assert result.clean, [f.what for f in result.blocking]


def test_motion_family_expansion_is_gate_clean_by_construction(tmp_path: Path) -> None:
    shot = _shot(tmp_path, MOTION_BRIEF, ["f024.png", "f048.png"])
    registry = clause_registry(shot / "brief.md")
    mapping = _motion_mapping(registry)

    expand_mapping(shot, mapping)
    result = plan_gate.run(shot, "plans/global.md", require_scene_checks=True)

    assert result.clean, [f.what for f in result.blocking]


def test_owned_requirements_are_derived_not_authored(tmp_path: Path) -> None:
    shot = _shot(tmp_path, MOTION_BRIEF, ["f024.png", "f048.png"])
    registry = clause_registry(shot / "brief.md")
    mapping = _motion_mapping(registry)

    owned = derive_owned_requirements(mapping)
    expected = {
        "1": sorted(
            (rid for rid, res in mapping["resolutions"].items()
             if res.get("owner_layer") == "1"),
            key=lambda rid: int(rid[1:]),
        ),
        "2": sorted(
            (rid for rid, res in mapping["resolutions"].items()
             if res.get("owner_layer") == "2"),
            key=lambda rid: int(rid[1:]),
        ),
    }
    assert owned == expected

    import json
    expand_mapping(shot, mapping)
    layers = json.loads((shot / "layers.json").read_text(encoding="utf-8"))
    for row in layers["layers"]:
        assert row["jit"]["owned_requirements"] == expected[row["id"]]


def test_expansion_is_deterministic(tmp_path: Path) -> None:
    shot = _shot(tmp_path, PRODUCT_BRIEF, ["f001.png"])
    registry = clause_registry(shot / "brief.md")
    mapping = _product_mapping(registry)

    first = {
        name: path.read_bytes()
        for name, path in expand_mapping(shot, mapping).items()
    }
    second = {
        name: path.read_bytes()
        for name, path in expand_mapping(shot, mapping).items()
    }
    assert first == second


def test_mapping_validation_enumerates_accepted_values(tmp_path: Path) -> None:
    shot = _shot(tmp_path, PRODUCT_BRIEF, ["f001.png"])
    registry = clause_registry(shot / "brief.md")
    mapping = _product_mapping(registry)
    first_rid = registry[0]["id"]
    mapping["resolutions"][first_rid] = {"kind": "contract", "ids": ["x"]}
    mapping["layers"][0]["evidence_domains"] = ["vibes"]

    errors = validate_mapping(mapping, registry, shot / "refs")

    joined = "\n".join(errors)
    assert "'decision' or 'deferred_owner'" in joined
    assert "projected_composition" in joined  # domains enumerated
    with pytest.raises(ValueError, match="mapping is invalid"):
        expand_mapping(shot, mapping)


def test_missing_clause_resolution_fails_closed(tmp_path: Path) -> None:
    shot = _shot(tmp_path, PRODUCT_BRIEF, ["f001.png"])
    registry = clause_registry(shot / "brief.md")
    mapping = _product_mapping(registry)
    dropped = registry[1]["id"]
    del mapping["resolutions"][dropped]

    errors = validate_mapping(mapping, registry, shot / "refs")

    assert any(dropped in error and "resolved exactly once" in error for error in errors)


def test_mapping_expander_closes_the_session_authoring_loop(tmp_path: Path) -> None:
    """The write-hook loop the sessions run on: invalid mapping -> enumerated errors,
    valid mapping -> full authority surface expanded, including plans/global.md, which
    is the flow's success condition."""
    import json

    from vfx_harness.agents.planner import mapping_expander

    shot = _shot(tmp_path, PRODUCT_BRIEF, ["f001.png"])
    registry = clause_registry(shot / "brief.md")
    mapping_path = shot / "ownership_mapping.json"
    expander = mapping_expander(shot, registry, mapping_path)

    mapping_path.write_text("{not json", encoding="utf-8")
    assert any("not readable JSON" in error for error in expander())

    bad = _product_mapping(registry)
    bad["layers"][0]["evidence_domains"] = ["vibes"]
    mapping_path.write_text(json.dumps(bad), encoding="utf-8")
    errors = expander()
    assert any("projected_composition" in error for error in errors)
    assert not (shot / "plans" / "global.md").is_file()

    mapping_path.write_text(json.dumps(_product_mapping(registry)), encoding="utf-8")
    assert expander() == []
    assert (shot / "plans" / "global.md").is_file()
    assert (shot / "plans" / "ownership_mapping.json").is_file()  # bundle provenance
    result = plan_gate.run(shot, "plans/global.md", require_scene_checks=True)
    assert result.clean


def test_registry_prompt_block_carries_ids_lines_and_text(tmp_path: Path) -> None:
    shot = _shot(tmp_path, PRODUCT_BRIEF, ["f001.png"])
    registry = clause_registry(shot / "brief.md")

    block = registry_prompt_block(registry)

    assert "R1 [lines" in block
    assert "seamless studio sweep" in block
