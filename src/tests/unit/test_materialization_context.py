from __future__ import annotations

import json
from types import SimpleNamespace

from vfx_harness.agents.planner import _materialization_kickoff
from vfx_harness.agents.prompts import layer_user_prompt
from vfx_harness.agents.unit_scope import compile_predecessor_interface


def _write(path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_materialization_kickoff_compiles_layer_bounded_authority(tmp_path) -> None:
    bundle_root = tmp_path / "bundle"
    layer_two = {
        "id": "2",
        "title": "Lookdev",
        "script": "build/02.py",
        "primary_judge": 72,
        "judge": [{"frame": 72, "ref": "refs/f72.png"}],
        "owns": ["material"],
        "reads": "lookdev",
        "execution": "jit_deferred",
        "stages": [],
        "jit": {
            "depends_on_layers": ["1"],
            "required_outcomes": [{"kind": "scene_contract", "id": "upstream-vis"}],
            "reserved_roles": ["lookdev.*"],
            "owned_requirements": ["R2"],
        },
    }
    _write(bundle_root / "layers.json", {
        "schema": 5,
        "layers": [{
            "id": "1",
            "title": "Camera",
            "dressable": ["cam.blockout_fg"],
            "execution": "ready",
            "stages": [{
                "id": "blockout",
                "provides": ["geometry"],
                "mutates": {"roles": ["cam.blockout_fg"]},
            }],
        }, layer_two],
    })
    _write(bundle_root / "requirements.json", {
        "schema": "vfx-harness.requirements/v1",
        "requirements": [
            {"id": "R1", "statement": "GLOBAL-JUNK-THAT-MUST-NOT-ENTER-CONTEXT"},
            {"id": "R2", "statement": "Create the material language"},
        ],
    })
    _write(tmp_path / "plans" / "outcomes" / "01.json", {
        "schema": 2,
        "layer": "1",
        "status": "passed",
        "script": "build/01.py",
        "large_unrelated_report": "OUTCOME-JUNK" * 1000,
        "interfaces": [{
            "id": "upstream-vis",
            "kind": "visible_fraction",
            "value": 1.0,
            "target": ">= 0.5",
            "pass": True,
        }],
    })
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "plan-resolutions.jsonl").write_text("", encoding="utf-8")
    layer = SimpleNamespace(
        id="2",
        title="Lookdev",
        jit=SimpleNamespace(
            reserved_roles=("lookdev.*",),
            depends_on_layers=("1",),
            required_outcomes=(("scene_contract", "upstream-vis"),),
        ),
        judges=((72, "refs/f72.png"),),
    )
    bundle = SimpleNamespace(root=bundle_root, content_hash="a" * 64)

    kickoff = _materialization_kickoff(
        tmp_path,
        layer,
        bundle,
        "runs/current/scratch/jit-layer-2.json",
        overlay_root=bundle_root,
    )

    assert '"id": "R2"' in kickoff
    assert '"id": "R1"' not in kickoff
    assert "GLOBAL-JUNK-THAT-MUST-NOT-ENTER-CONTEXT" not in kickoff
    assert "cam.blockout_fg" in kickoff
    assert "upstream-vis" in kickoff
    assert "OUTCOME-JUNK" not in kickoff
    assert "Selected bundle root (readable)" not in kickoff
    assert "state/plan-resolutions.jsonl" not in kickoff


def test_camera_materialization_kickoff_compiles_earliest_geometry_layer(tmp_path) -> None:
    bundle_root = tmp_path / "bundle"
    camera = {
        "id": "1",
        "title": "Camera",
        "script": "build/01.py",
        "primary_judge": 1,
        "judge": [{"frame": 1, "ref": "refs/a.png"}],
        "owns": ["camera_path"],
        "reads": "camera",
        "execution": "jit_deferred",
        "stages": [],
        "jit": {
            "depends_on_layers": [],
            "provides": {"camera": ["camera.rig"]},
            "reserved_roles": ["camera.rig"],
            "owned_requirements": [],
        },
    }
    form = {
        "id": "2",
        "title": "Form",
        "script": "build/02.py",
        "primary_judge": 1,
        "judge": [{"frame": 1, "ref": "refs/a.png"}],
        "owns": ["form"],
        "reads": "form",
        "execution": "jit_deferred",
        "stages": [],
        "jit": {
            "depends_on_layers": ["1"],
            "provides": {},
            "reserved_roles": ["subject.mass"],
            "owned_requirements": [],
        },
    }
    _write(bundle_root / "layers.json", {"schema": 5, "layers": [camera, form]})
    _write(bundle_root / "requirements.json", {
        "schema": "vfx-harness.requirements/v1",
        "requirements": [],
    })
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "plan-resolutions.jsonl").write_text("", encoding="utf-8")
    layer = SimpleNamespace(id="1", title="Camera", jit=SimpleNamespace(), judges=((1, "refs/a.png"),))
    bundle = SimpleNamespace(root=bundle_root, content_hash="b" * 64)

    kickoff = _materialization_kickoff(
        tmp_path,
        layer,
        bundle,
        "runs/current/scratch/jit-layer-1.json",
        overlay_root=bundle_root,
    )

    assert '"earliest_geometry_layer": "2"' in kickoff
    assert "do not ask_supervisor for layer occupancy" in kickoff
    assert "Do not ask_supervisor which selected layer" in kickoff


def test_unit_plan_kickoff_uses_compiled_cards_not_raw_catalogs(tmp_path) -> None:
    shot = SimpleNamespace(
        id="bounded-shot",
        folder=tmp_path,
        refs=(tmp_path / "f72.png",),
    )
    layer = SimpleNamespace(
        id="2",
        title="Lookdev",
        judges=((72, "refs/f72.png"),),
        owns=("material",),
        script="build/02.py",
    )
    unit = SimpleNamespace(id="material", title="Material")
    card = {
        "schema": "vfx-harness.unit-scope/v1",
        "unit_id": "material",
        "contracts": [{"id": "mat-count", "kind": "material_count"}],
    }

    kickoff = layer_user_prompt(
        shot,
        layer,
        unit,
        "plans/units/material.md",
        "sealed predecessor summary",
        unit_card=card,
        predecessor_cards=[{"unit_id": "camera", "provides": ["camera"]}],
    )

    assert "complete compiled unit authority" in kickoff
    assert '"id": "mat-count"' in kickoff
    assert '"unit_id": "camera"' in kickoff
    assert "sealed predecessor summary" in kickoff
    assert "Read `brief.md`" not in kickoff
    assert "layers.json" not in kickoff
    assert "scene_checks.json" not in kickoff


def test_predecessor_interface_excludes_dependency_implementation_closure() -> None:
    interface = compile_predecessor_interface(
        {
            "schema": "vfx-harness.unit-scope/v1",
            "unit_id": "material",
            "title": "Material Language",
            "mutates": {
                "roles": ["lookdev.primary_material"],
                "controls": ["finish"],
                "dresses": ["cam.blockout_fg"],
            },
            "provides": [],
            "look_capabilities": ["material"],
            "judge": {"frames": [{"frame": 72, "ref": "refs/f72.png"}]},
            "claims": [{
                "id": "material-exists",
                "required": True,
                "proposition": "LARGE PREDECESSOR PROPOSITION MUST NOT SCALE CONTEXT",
            }],
            "contracts": [{
                "id": "material-count",
                "kind": "material_count",
                "lo": 1,
                "large_internal_field": "CONTRACT IMPLEMENTATION MUST NOT ENTER",
            }],
            "image_debts": [{
                "id": "matlook-f72",
                "frame": 72,
                "property": "render_region_stat",
                "axis": "material_language",
            }],
            "helpers": [{"name": "bvfx_role", "signature": "LARGE HELPER INVENTORY"}],
        }
    )

    encoded = json.dumps(interface)
    assert interface["dependency_status"] == "passed"
    assert interface["semantic_roles"] == ["lookdev.primary_material"]
    assert interface["sealed_claim_ids"] == ["material-exists"]
    assert interface["scene_contract_ids"] == ["material-count"]
    assert interface["image_contract_ids"] == ["matlook-f72"]
    assert "LARGE PREDECESSOR PROPOSITION" not in encoded
    assert "CONTRACT IMPLEMENTATION" not in encoded
    assert "LARGE HELPER INVENTORY" not in encoded
    assert "refs/f72.png" not in encoded
