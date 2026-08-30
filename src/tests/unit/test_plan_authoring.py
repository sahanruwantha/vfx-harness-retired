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


def _owned(layer: str, *domains: str) -> dict:
    return {
        "kind": "deferred_owner",
        "owner_layer": layer,
        "evidence_domains": list(domains) or ["scene"],
    }


def _shot(tmp_path: Path, brief: str, refs: list[str]) -> Path:
    (tmp_path / "brief.md").write_text(brief, encoding="utf-8")
    (tmp_path / "refs").mkdir()
    for name in refs:
        (tmp_path / "refs" / name).write_bytes(b"png")
    return tmp_path


def _product_mapping(registry) -> dict:
    rids = [row["id"] for row in registry]
    resolutions = {rid: _owned("1") for rid in rids}
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
            "depends_on": [], "provides": {"camera": ["camera.*"]},
            "reserved_roles": ["camera.*", "hero.*"],
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
        resolutions[rid] = _owned(owner)
    return {
        "schema": "vfx-harness.ownership-mapping/v1",
        "layers": [
            {"id": "1", "title": "Camera reveal", "script": "build/01_camera.py",
             "charter": "authored brief", "primary_judge": 24,
             "judge": [{"frame": 24, "ref": "refs/f024.png"}],
             "owns": ["camera_motion"], "evidence_domains": ["scene", "temporal"],
             "depends_on": [], "provides": {"camera": ["camera.*"]},
             "reserved_roles": ["camera.*"]},
            {"id": "2", "title": "Dressing settle", "script": "build/02_dressing.py",
             "charter": "camera reveal outcome", "primary_judge": 48,
             "judge": [{"frame": 48, "ref": "refs/f048.png"}],
             "owns": ["dressing_settle"], "evidence_domains": ["scene", "temporal"],
             "depends_on": ["1"], "provides": {},
             "reserved_roles": ["dressing.*"]},
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
    assert layers["layers"][0]["jit"]["provides"] == {"camera": ["camera.*"]}
    assert layers["layers"][1]["jit"]["provides"] == {}


def test_global_camera_capability_must_precede_every_judged_layer(tmp_path: Path) -> None:
    shot = _shot(tmp_path, MOTION_BRIEF, ["f024.png", "f048.png"])
    registry = clause_registry(shot / "brief.md")
    mapping = _motion_mapping(registry)
    mapping["layers"][0]["provides"] = {}
    mapping["layers"][1]["provides"] = {"camera": ["dressing.*"]}

    errors = validate_mapping(mapping, registry, shot / "refs")

    assert any("layers[0] is judged before a camera capability" in error for error in errors)
    assert not any("layers[1] is judged before a camera capability" in error for error in errors)


def test_global_camera_capability_flows_through_dependency_closure(tmp_path: Path) -> None:
    shot = _shot(tmp_path, MOTION_BRIEF, ["f024.png", "f048.png"])
    registry = clause_registry(shot / "brief.md")
    mapping = _motion_mapping(registry)

    errors = validate_mapping(mapping, registry, shot / "refs")

    assert not any("camera capability" in error for error in errors)


def test_plan_gate_rejects_a_selected_global_view_without_camera_closure(
    tmp_path: Path,
) -> None:
    import json

    shot = _shot(tmp_path, MOTION_BRIEF, ["f024.png", "f048.png"])
    registry = clause_registry(shot / "brief.md")
    expand_mapping(shot, _motion_mapping(registry))
    layers = json.loads((shot / "layers.json").read_text(encoding="utf-8"))
    layers["layers"][0]["jit"]["provides"] = {}
    (shot / "layers.json").write_text(json.dumps(layers), encoding="utf-8")

    result = plan_gate.run(shot, "plans/global.md", require_scene_checks=True)

    failures = [finding for finding in result.blocking if finding.check == "global-capability"]
    assert {finding.where for finding in failures} == {"layer 1", "layer 2"}


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


READABLE_BRIEF = """---
id: readable-fixture
title: Readable subject
type: motion
frames: 24
fps: 24
resolution: [1920, 1080]
engine: BLENDER_EEVEE_NEXT
---

# Readable subject

## Intent

A camera push frames the subject, then lighting keeps the subject readable.

## Look

- The key light is the illumination owner.

## Beats

| Beat | Frames | Required action |
|---|---:|---|
| B1 | 1 | The camera frames the subject. |
| B2 | 24 | The subject stays readable under the key light. |

## Deliverables

- Approval stills at frames 1 and 24.
"""


def _readable_mapping(registry) -> dict:
    rids = [row["id"] for row in registry]
    by_text = {row["id"]: row["statement"] for row in registry}
    resolutions = {}
    for rid in rids:
        text = by_text[rid].lower()
        if "readable" in text:
            resolutions[rid] = _owned("1", "scene")
        elif "light" in text:
            resolutions[rid] = _owned("2", "image")
        else:
            resolutions[rid] = _owned("1", "scene")
    return {
        "schema": "vfx-harness.ownership-mapping/v1",
        "layers": [
            {
                "id": "1", "title": "Camera path", "script": "build/01_camera.py",
                "charter": "authored brief", "primary_judge": 1,
                "judge": [{"frame": 1, "ref": "refs/f001.png"},
                          {"frame": 24, "ref": "refs/f024.png"}],
                "owns": ["camera_path"],
                "evidence_domains": ["scene", "projected_composition", "temporal"],
                "depends_on": [], "provides": {"camera": ["camera.*"]},
                "reserved_roles": ["camera.*"],
            },
            {
                "id": "2", "title": "Key light", "script": "build/02_light.py",
                "charter": "camera path outcome", "primary_judge": 24,
                "judge": [{"frame": 24, "ref": "refs/f024.png"}],
                "owns": ["subject_readability"],
                "evidence_domains": ["image", "scene"],
                "depends_on": ["1"], "provides": {},
                "reserved_roles": ["light.*"],
            },
        ],
        "axes": [
            {"key": "camera_path", "desc": "push-in framing"},
            {"key": "subject_readability", "desc": "subject stays readable"},
        ],
        "resolutions": resolutions,
        "blockers": [],
    }


def _readable_clause(registry) -> str:
    return next(
        row["id"] for row in registry if "readable" in row["statement"].lower()
    )


def test_claim_domains_are_the_layer_evidence_domain_vocabulary() -> None:
    from vfx_harness.domain.work_units import CLAIM_DOMAINS, EVIDENCE_DOMAINS

    assert CLAIM_DOMAINS is EVIDENCE_DOMAINS
    assert "human" in EVIDENCE_DOMAINS


def test_ownership_mapping_schema_enumerates_deferred_owner_domains() -> None:
    from jsonschema import Draft202012Validator

    from vfx_harness.domain.work_units import EVIDENCE_DOMAINS
    from vfx_harness.orchestration.plan_authoring import (
        ownership_mapping_authoring_schema,
    )

    schema = ownership_mapping_authoring_schema()
    deferred = schema["properties"]["resolutions"]["additionalProperties"]["oneOf"][0]
    assert deferred["properties"]["evidence_domains"]["items"]["enum"] == sorted(
        EVIDENCE_DOMAINS
    )
    assert "evidence_domains" in deferred["required"]

    ticket = {
        "kind": "deferred_owner",
        "owner_layer": "1",
        "evidence_domains": ["scene"],
    }
    assert list(Draft202012Validator(deferred).iter_errors(ticket)) == []

    errors = list(
        Draft202012Validator(deferred).iter_errors(
            {"kind": "deferred_owner", "owner_layer": "1"}
        )
    )
    assert errors
    assert any("evidence_domains" in error.message for error in errors)


def test_deferred_owner_missing_domains_enumerates_accepted_set(tmp_path: Path) -> None:
    shot = _shot(tmp_path, PRODUCT_BRIEF, ["f001.png"])
    registry = clause_registry(shot / "brief.md")
    mapping = _product_mapping(registry)
    first_rid = registry[0]["id"]
    mapping["resolutions"][first_rid] = {"kind": "deferred_owner", "owner_layer": "1"}

    errors = validate_mapping(mapping, registry, shot / "refs")
    joined = "\n".join(errors)

    assert first_rid in joined
    assert "projected_composition" in joined
    assert "human" in joined


def test_image_domain_on_non_image_owner_names_covering_layer(tmp_path: Path) -> None:
    shot = _shot(tmp_path, READABLE_BRIEF, ["f001.png", "f024.png"])
    registry = clause_registry(shot / "brief.md")
    mapping = _readable_mapping(registry)
    rid = _readable_clause(registry)
    mapping["resolutions"][rid] = _owned("1", "image")

    errors = validate_mapping(mapping, registry, shot / "refs")
    joined = "\n".join(errors)

    assert rid in joined
    assert "missing image" in joined
    assert "Layers whose domains cover every declared domain: 2" in joined
    assert "do not infer domains from brief keywords" in joined
    with pytest.raises(ValueError, match="mapping is invalid"):
        expand_mapping(shot, mapping)

    mapping["resolutions"][rid] = _owned("2", "image")
    assert validate_mapping(mapping, registry, shot / "refs") == []
    expand_mapping(shot, mapping)
    result = plan_gate.run(shot, "plans/global.md", require_scene_checks=True)
    assert result.clean, [f.what for f in result.blocking]


def test_and_coverage_forces_split_when_no_layer_declares_every_domain(
    tmp_path: Path,
) -> None:
    shot = _shot(tmp_path, READABLE_BRIEF, ["f001.png", "f024.png"])
    registry = clause_registry(shot / "brief.md")
    mapping = _readable_mapping(registry)
    rid = _readable_clause(registry)

    mapping["resolutions"][rid] = _owned(
        "1", "projected_composition", "temporal", "image"
    )
    errors = validate_mapping(mapping, registry, shot / "refs")
    joined = "\n".join(errors)
    assert rid in joined
    assert "missing image" in joined
    assert "Layers whose domains cover every declared domain: none" in joined

    mapping["resolutions"][rid] = _owned("1", "projected_composition", "temporal")
    assert validate_mapping(mapping, registry, shot / "refs") == []
    expand_mapping(shot, mapping)
    import json

    register = json.loads((shot / "requirements.json").read_text(encoding="utf-8"))
    row = next(item for item in register["requirements"] if item["id"] == rid)
    assert row["resolution"]["evidence_domains"] == [
        "projected_composition", "temporal"
    ]
    result = plan_gate.run(shot, "plans/global.md", require_scene_checks=True)
    assert result.clean, [f.what for f in result.blocking]


def test_plan_gate_teaches_requirement_domain_coverage_on_selected_authority(
    tmp_path: Path,
) -> None:
    shot = _shot(tmp_path, READABLE_BRIEF, ["f001.png", "f024.png"])
    registry = clause_registry(shot / "brief.md")
    mapping = _readable_mapping(registry)
    rid = _readable_clause(registry)
    mapping["resolutions"][rid] = _owned("1", "projected_composition", "temporal")
    expand_mapping(shot, mapping)
    import json

    register = json.loads((shot / "requirements.json").read_text(encoding="utf-8"))
    for row in register["requirements"]:
        if row["id"] == rid:
            row["resolution"]["evidence_domains"] = [
                "image", "projected_composition", "temporal"
            ]
    (shot / "requirements.json").write_text(
        json.dumps(register, indent=1) + "\n", encoding="utf-8"
    )

    result = plan_gate.run(shot, "plans/global.md", require_scene_checks=True)
    coverage = [
        finding for finding in result.blocking
        if finding.check == "requirement-domain-coverage" and finding.where == rid
    ]
    assert coverage, [f.what for f in result.blocking]
    assert "missing image" in coverage[0].what
    assert "Layers whose domains cover every declared domain: none" in coverage[0].what
    assert coverage[0].layer == "1"
