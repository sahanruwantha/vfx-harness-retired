from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest
from PIL import Image

from tests.unit_attempt_fixtures import (
    ABSENT_SELECTION_TOKEN,
    claim_for_build,
    legacy_apply_replan,
    pass_unit,
)
from vfx_harness.agents.builder import _executable_unit_verdict, _scope_unit_evidence
from vfx_harness.blender.tools import _image_evidence_ids_at_frame, _pixel_contract_gate
from vfx_harness.domain.work_units import Claim, ProtectionSpec, WorkUnit, read_document
from vfx_harness.evidence.checks import IMAGE_PAYMENT_SCHEMA
from vfx_harness.evidence.claim_evidence import validate_claim_closure
from vfx_harness.observability.provenance import check as provenance_check
from vfx_harness.observability.provenance import stamp as provenance_stamp
from vfx_harness.orchestration.layer_plans import read_layer_plan
from vfx_harness.orchestration.ledger import load_layers
from vfx_harness.orchestration.unit_state import (
    block_dependents,
    initialize,
    invalidate_checkpoint,
    load,
)
from vfx_harness.orchestration.unit_state_claims import claim_ready_unit_for_planning

_PLAN = "0" * 64
_PLAN_V1 = "1" * 64
_PLAN_V2 = "2" * 64


def _claim(uid: str, *, cid: str | None = None, frame: int = 40) -> dict:
    return {
        "id": cid or f"claim.{uid}",
        "proposition": f"{uid} has the declared state",
        "axis": "form",
        "property": f"state.{uid}",
        "subject_roles": [],
        "subject_controls": [f"control.{uid}"],
        "moments": [frame],
        "kind": "atomic",
        "required": True,
        "authority": "executable_required",
        "repair_owner": uid,
        "asserts": "scene",
        "evidence": [{"kind": "scene_contract", "id": f"contract.{uid}"}],
    }


def _provenance_payment(root, row: dict) -> dict:
    """Attach the strict runtime image-payment envelope used by claim-closure tests."""
    run_id = "20260827T000000Z-fixture"
    renders = root / "runs" / run_id / "evidence" / "renders"
    renders.mkdir(parents=True, exist_ok=True)
    manifest = root / "runs" / run_id / "manifest.json"
    manifest.write_text(json.dumps({"run_id": run_id}), encoding="utf-8")
    frame = int(row["frame"])
    candidate = renders / f"candidate-f{frame}.png"
    adversary = renders / f"adversary-f{frame}.png"
    candidate.write_bytes(b"candidate")
    adversary.write_bytes(b"adversary")
    parent_hash = hashlib.sha256(b"parent").hexdigest()
    settings = {"frame": frame, "mode": "eevee", "scale": 0.5, "resolution": [64, 36, 100]}
    return {
        **row,
        "payment": {
            "schema": IMAGE_PAYMENT_SCHEMA,
            "run_id": run_id,
            "unit_id": "form",
            "unit_hash": hashlib.sha256(b"unit").hexdigest(),
            "parent_chain_hash": parent_hash,
            "candidate": {
                **settings,
                "path": candidate.relative_to(root).as_posix(),
                "sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
            },
            "adversary": {
                **settings,
                "path": adversary.relative_to(root).as_posix(),
                "sha256": hashlib.sha256(adversary.read_bytes()).hexdigest(),
                "parent_chain_hash": parent_hash,
            },
        },
    }


def _unit(
    uid: str,
    *,
    depends_on: list[str] | None = None,
    frame: int = 40,
    proposition_suffix: str = "",
    script_span: str | None = None,
) -> WorkUnit:
    script_span = script_span or f"build/units/04/{uid}.py"
    row = {
        "id": uid,
        "title": uid,
        "plan": f"plans/04_lighting/{uid}.md",
        "depends_on": depends_on or [],
        "mutates": {
            "mode": "scoped",
            "roles": [],
            "controls": [],
            "script_spans": [script_span],
        },
        "protects": {
            "selector": "all_active_upstream_interfaces",
            "resolve_to_explicit_ids_at": "freeze",
        },
        "evaluation": {
            "primary_judge": frame,
            "judge": [{"frame": frame, "ref": f"refs/f{frame:03d}.png"}],
            "temporal_evidence": "none",
            "claims": [_claim(uid, frame=frame)],
        },
        "completion": "all_required_claims_and_protected_contracts_pass" + proposition_suffix,
    }
    return WorkUnit.parse(row, f"unit.{uid}")


def _pass_unit(
    folder,
    layer_id: str,
    unit: WorkUnit,
    units: tuple[WorkUnit, ...],
    *,
    plan_hash: str,
) -> None:
    pass_unit(
        folder,
        layer_id,
        unit,
        units,
        plan_hash=plan_hash,
        active_contract_ids=("upstream.z", "upstream.a"),
    )


def test_unit_evidence_scope_excludes_sibling_contracts():
    unit = _unit("shell")
    evidence = [
        {"id": "contract.shell", "pass": True, "authoritative": True},
        {"id": "contract.pedestal", "pass": False, "authoritative": True},
    ]

    assert _scope_unit_evidence(evidence, unit, 40) == [evidence[0]]


def test_executable_only_unit_is_decided_by_bound_checks():
    unit = _unit("shell")
    axes = [("form", "declared form")]
    passed = _executable_unit_verdict(
        unit,
        40,
        axes,
        [{"id": "contract.shell", "pass": True, "authoritative": True}],
    )
    missing = _executable_unit_verdict(unit, 40, axes, [])

    assert passed is not None and passed["pass"] is True
    assert passed["decided_by"] == "unit_executable_evidence"
    assert missing is not None and missing["pass"] is False
    assert missing["contract_gap"] is True
    assert missing["missing_evidence"] == ["contract.shell"]


def test_executable_unit_without_image_bindings_has_no_generic_brightness_gate(tmp_path):
    passed, rows = _pixel_contract_gate(
        tmp_path,
        "1",
        frame=1,
        ref="missing-ref.png",
        render="missing-render.png",
        evidence_ids=set(),
    )

    assert passed is True
    assert rows == []


def test_live_image_gate_consumes_exact_builder_payments_without_false_reopen(
    tmp_path, monkeypatch
):
    render = tmp_path / "candidate.png"
    Image.new("RGB", (8, 8), (20, 20, 20)).save(render)

    monkeypatch.setattr(
        "vfx_harness.evidence.checks.layer_evidence",
        lambda *args, **kwargs: [
            {
                "id": "look-f72",
                "metric": "region_mean",
                "value": 6.0,
                "target": ">= 2",
                "pass": True,
                "origin": "builder",
                "authoritative": False,
            }
        ],
    )

    passed, rows = _pixel_contract_gate(
        tmp_path,
        "2",
        frame=72,
        ref="refs/f72.png",
        render=render,
        evidence_ids={"look-f72"},
    )
    missing, missing_rows = _pixel_contract_gate(
        tmp_path,
        "2",
        frame=72,
        ref="refs/f72.png",
        render=render,
        evidence_ids={"look-f72", "other-f72"},
    )

    assert passed is True
    assert [row["id"] for row in rows] == ["look-f72"]
    assert missing is False
    assert any(row["id"] == "other-f72" and not row["pass"] for row in missing_rows)


def test_live_image_gate_scopes_cross_frame_debts_to_current_plate():
    state = {
        "image_debts": [
            {"id": "look-f72", "frame": 72},
            {"id": "look-f150", "frame": 150},
        ],
        "active_image_evidence_ids": {"look-f72", "look-f150"},
    }

    assert _image_evidence_ids_at_frame(state, 72) == {"look-f72"}
    assert _image_evidence_ids_at_frame(state, 150) == {"look-f150"}


def test_schema4_rejects_legacy_arrays(tmp_path):
    path = tmp_path / "layers.json"
    path.write_text("[]\n", encoding="utf-8")
    with pytest.raises(ValueError, match="legacy layer arrays are not supported"):
        read_document(path)


def test_schema4_rejects_schema3_without_adapter(tmp_path):
    path = tmp_path / "layers.json"
    path.write_text(json.dumps({"schema": 3, "layers": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="schema 3"):
        read_document(path)


def test_claim_rejects_collection_label_instead_of_explicit_binding():
    row = _claim("form")
    row["evidence"] = ["scene_contracts"]
    with pytest.raises(ValueError, match="must be an object"):
        Claim.parse(row, "claim")


def test_claim_closure_reports_unbound_owned_contract(tmp_path):
    (tmp_path / "scene_checks.json").write_text(
        json.dumps(
            {
                "schema": 2,
                "contracts": [
                    {"id": "contract.form", "axis": "form", "owner_layer": "1", "frame": 40},
                    {"id": "contract.unbound", "axis": "form", "owner_layer": "1", "frame": 40},
                ],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "checks.json").write_text(
        json.dumps({"schema": 2, "checks": []}), encoding="utf-8"
    )
    layer = SimpleNamespace(id="1", owns=("form",), stages=(_unit("form"),))
    closure = validate_claim_closure(tmp_path, [layer])
    assert not closure.clean
    assert any(finding.where == "contract.unbound" for finding in closure.findings)


def test_claim_closure_counts_composition_context_ids_as_bound(tmp_path):
    """Remat7 published extra-frame vis via composition_context.contract_ids; the
    validator counted them as producers, then claim-closure unbound them and
    retracted the unit plan (HIR-0029)."""
    (tmp_path / "scene_checks.json").write_text(
        json.dumps(
            {
                "schema": 2,
                "contracts": [
                    {"id": "contract.form", "axis": "form", "owner_layer": "1", "frame": 40},
                    {"id": "contract.extra", "axis": "form", "owner_layer": "1", "frame": 12},
                ],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "checks.json").write_text(
        json.dumps({"schema": 2, "checks": []}), encoding="utf-8"
    )
    row = {
        "id": "form",
        "title": "form",
        "plan": "plans/04_lighting/form.md",
        "depends_on": [],
        "mutates": {
            "mode": "scoped",
            "roles": [],
            "controls": [],
            "script_spans": ["build/04_lighting.py"],
        },
        "protects": {
            "selector": "all_active_upstream_interfaces",
            "resolve_to_explicit_ids_at": "freeze",
        },
        "evaluation": {
            "primary_judge": 40,
            "judge": [{"frame": 40, "ref": "refs/f040.png"}],
            "temporal_evidence": "none",
            "claims": [_claim("form")],
            "composition_context": {
                "frames": [40],
                "contract_ids": ["contract.extra"],
            },
        },
        "completion": "all_required_claims_and_protected_contracts_pass",
    }
    unit = WorkUnit.parse(row, "unit.form")
    closure = validate_claim_closure(
        tmp_path, [SimpleNamespace(id="1", owns=("form",), stages=(unit,))]
    )
    assert closure.clean
    assert "contract.extra" in closure.bound_contract_ids


def test_extra_frame_evidence_binding_names_id_path(tmp_path):
    (tmp_path / "scene_checks.json").write_text(
        json.dumps(
            {
                "schema": 2,
                "contracts": [
                    {"id": "contract.form", "axis": "form", "owner_layer": "1", "frame": 40},
                    {"id": "contract.extra", "axis": "form", "owner_layer": "1", "frame": 12},
                ],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "checks.json").write_text(
        json.dumps({"schema": 2, "checks": []}), encoding="utf-8"
    )
    claim = _claim("form")
    claim["evidence"] = [
        {"kind": "scene_contract", "id": "contract.form"},
        {"kind": "scene_contract", "id": "contract.extra"},
    ]
    row = {
        "id": "form",
        "title": "form",
        "plan": "plans/04_lighting/form.md",
        "depends_on": [],
        "mutates": {
            "mode": "scoped",
            "roles": [],
            "controls": [],
            "script_spans": ["build/04_lighting.py"],
        },
        "protects": {
            "selector": "all_active_upstream_interfaces",
            "resolve_to_explicit_ids_at": "freeze",
        },
        "evaluation": {
            "primary_judge": 40,
            "judge": [{"frame": 40, "ref": "refs/f040.png"}],
            "temporal_evidence": "none",
            "claims": [claim],
        },
        "completion": "all_required_claims_and_protected_contracts_pass",
    }
    unit = WorkUnit.parse(row, "unit.form")
    closure = validate_claim_closure(
        tmp_path, [SimpleNamespace(id="1", owns=("form",), stages=(unit,))]
    )
    assert not closure.clean
    assert any("outside claim moments" in finding.what for finding in closure.findings)
    assert any("composition_context.contract_ids" in finding.what for finding in closure.findings)


def test_claim_closure_counts_image_contract_debts_as_bound(tmp_path):
    """HIR-0046 published look image_contract ids while checks.json stayed empty;
    claim-closure then retracted the unit plan (run 20260827T080852Z-a0351a)."""
    (tmp_path / "scene_checks.json").write_text(
        json.dumps(
            {
                "schema": 2,
                "contracts": [
                    {"id": "contract.form", "axis": "form", "owner_layer": "1", "frame": 40},
                ],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "checks.json").write_text(
        json.dumps({"schema": 2, "checks": []}), encoding="utf-8"
    )
    look = _claim("form", cid="claim.look", frame=40)
    look["asserts"] = "image"
    look["evidence"] = [{"kind": "image_contract", "id": "form-look-f40"}]
    row = {
        "id": "form",
        "title": "form",
        "plan": "plans/04_lighting/form.md",
        "depends_on": [],
        "mutates": {
            "mode": "scoped",
            "roles": [],
            "controls": [],
            "script_spans": ["build/04_lighting.py"],
        },
        "protects": {
            "selector": "all_active_upstream_interfaces",
            "resolve_to_explicit_ids_at": "freeze",
        },
        "evaluation": {
            "primary_judge": 40,
            "judge": [{"frame": 40, "ref": "refs/f040.png"}],
            "temporal_evidence": "none",
            "claims": [_claim("form"), look],
        },
        "completion": "all_required_claims_and_protected_contracts_pass",
        "look_capabilities": ["material"],
    }
    unit = WorkUnit.parse(row, "unit.form")
    closure = validate_claim_closure(
        tmp_path, [SimpleNamespace(id="1", owns=("form",), stages=(unit,))]
    )
    assert closure.clean, tuple(finding.what for finding in closure.findings)
    assert "form-look-f40" in closure.bound_contract_ids
    assert not any("does not exist" in finding.what for finding in closure.findings)


def test_claim_closure_still_checks_existing_image_contract_axis(tmp_path):
    (tmp_path / "scene_checks.json").write_text(
        json.dumps({"schema": 2, "contracts": []}), encoding="utf-8"
    )
    (tmp_path / "checks.json").write_text(
        json.dumps(
            {
                "schema": 2,
                "checks": [
                    {"id": "form-look-f40", "axis": "other", "owner_layer": "1", "frame": 40},
                ],
            }
        ),
        encoding="utf-8",
    )
    look = _claim("form", cid="claim.look", frame=40)
    look["asserts"] = "image"
    look["evidence"] = [{"kind": "image_contract", "id": "form-look-f40"}]
    unit = WorkUnit.parse(
        {
            "id": "form",
            "title": "form",
            "plan": "plans/04_lighting/form.md",
            "depends_on": [],
            "mutates": {
                "mode": "scoped",
                "roles": [],
                "controls": [],
                "script_spans": ["build/04_lighting.py"],
            },
            "protects": {
                "selector": "all_active_upstream_interfaces",
                "resolve_to_explicit_ids_at": "freeze",
            },
            "evaluation": {
                "primary_judge": 40,
                "judge": [{"frame": 40, "ref": "refs/f040.png"}],
                "temporal_evidence": "none",
                "claims": [look],
            },
            "completion": "all_required_claims_and_protected_contracts_pass",
            "look_capabilities": ["material"],
        },
        "unit.form",
    )
    closure = validate_claim_closure(
        tmp_path, [SimpleNamespace(id="1", owns=("form",), stages=(unit,))]
    )
    assert not closure.clean
    assert any("belongs to axis" in finding.what for finding in closure.findings)


def _empty_catalogs(tmp_path, *, scene_rows: list | None = None):
    (tmp_path / "scene_checks.json").write_text(
        json.dumps(
            {
                "schema": 2,
                "contracts": scene_rows
                if scene_rows is not None
                else [
                    {"id": "contract.form", "axis": "form", "owner_layer": "1", "frame": 40},
                ],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "checks.json").write_text(
        json.dumps({"schema": 2, "checks": []}), encoding="utf-8"
    )


def _look_claim(*, cid: str = "form-look-f40", frame: int = 40) -> dict:
    look = _claim("form", cid="claim.look", frame=frame)
    look["asserts"] = "image"
    look["property"] = "render_region_stat"
    look["evidence"] = [{"kind": "image_contract", "id": cid}]
    return look


def test_claim_closure_consumes_matching_runtime_image_row(tmp_path):
    """HIR-0048: a coherent runtime_checks payment is consumed, not left as a debt."""
    from vfx_harness.domain.image_debts import image_contract_debt_cards, unpaid_image_contract_debts
    from vfx_harness.evidence.checks import load_image_contract_payment_rows

    _empty_catalogs(tmp_path)
    look = _look_claim()
    row = {
        "id": "form",
        "title": "form",
        "plan": "plans/04_lighting/form.md",
        "depends_on": [],
        "mutates": {
            "mode": "scoped",
            "roles": [],
            "controls": [],
            "script_spans": ["build/04_lighting.py"],
        },
        "protects": {
            "selector": "all_active_upstream_interfaces",
            "resolve_to_explicit_ids_at": "freeze",
        },
        "evaluation": {
            "primary_judge": 40,
            "judge": [{"frame": 40, "ref": "refs/f040.png"}],
            "temporal_evidence": "none",
            "claims": [_claim("form"), look],
        },
        "completion": "all_required_claims_and_protected_contracts_pass",
        "look_capabilities": ["material"],
    }
    unit = WorkUnit.parse(row, "unit.form")
    (tmp_path / "runtime_checks.json").write_text(
        json.dumps(
            [
                    _provenance_payment(tmp_path, {
                        "id": "form-look-f40",
                        "origin": "builder",
                        "axis": "form",
                        "frame": 40,
                        "metric": "region_mean",
                        "layer": "1",
                    })
            ]
        ),
        encoding="utf-8",
    )
    closure = validate_claim_closure(
        tmp_path, [SimpleNamespace(id="1", owns=("form",), stages=(unit,))]
    )
    assert closure.clean, tuple(finding.what for finding in closure.findings)
    assert "form-look-f40" in closure.bound_contract_ids
    assert "form-look-f40" in closure.claim_bindings["claim.look"]
    unpaid = unpaid_image_contract_debts(
        image_contract_debt_cards(unit),
        load_image_contract_payment_rows(tmp_path),
    )
    assert unpaid == ()


def test_claim_closure_rejects_runtime_image_row_wrong_frame(tmp_path):
    _empty_catalogs(tmp_path, scene_rows=[])
    look = _look_claim()
    unit = WorkUnit.parse(
        {
            "id": "form",
            "title": "form",
            "plan": "plans/04_lighting/form.md",
            "depends_on": [],
            "mutates": {
                "mode": "scoped",
                "roles": [],
                "controls": [],
                "script_spans": ["build/04_lighting.py"],
            },
            "protects": {
                "selector": "all_active_upstream_interfaces",
                "resolve_to_explicit_ids_at": "freeze",
            },
            "evaluation": {
                "primary_judge": 40,
                "judge": [{"frame": 40, "ref": "refs/f040.png"}],
                "temporal_evidence": "none",
                "claims": [look],
            },
            "completion": "all_required_claims_and_protected_contracts_pass",
            "look_capabilities": ["material"],
        },
        "unit.form",
    )
    (tmp_path / "runtime_checks.json").write_text(
        json.dumps(
            [
                    _provenance_payment(tmp_path, {
                        "id": "form-look-f40",
                        "origin": "builder",
                        "axis": "form",
                        "frame": 150,
                        "metric": "region_mean",
                        "layer": "1",
                    })
            ]
        ),
        encoding="utf-8",
    )
    closure = validate_claim_closure(
        tmp_path, [SimpleNamespace(id="1", owns=("form",), stages=(unit,))]
    )
    assert not closure.clean
    assert any("f150" in finding.what and "outside claim moments" in finding.what for finding in closure.findings)


def test_claim_closure_rejects_runtime_image_row_wrong_axis(tmp_path):
    _empty_catalogs(tmp_path, scene_rows=[])
    look = _look_claim()
    unit = WorkUnit.parse(
        {
            "id": "form",
            "title": "form",
            "plan": "plans/04_lighting/form.md",
            "depends_on": [],
            "mutates": {
                "mode": "scoped",
                "roles": [],
                "controls": [],
                "script_spans": ["build/04_lighting.py"],
            },
            "protects": {
                "selector": "all_active_upstream_interfaces",
                "resolve_to_explicit_ids_at": "freeze",
            },
            "evaluation": {
                "primary_judge": 40,
                "judge": [{"frame": 40, "ref": "refs/f040.png"}],
                "temporal_evidence": "none",
                "claims": [look],
            },
            "completion": "all_required_claims_and_protected_contracts_pass",
            "look_capabilities": ["material"],
        },
        "unit.form",
    )
    (tmp_path / "runtime_checks.json").write_text(
        json.dumps(
            [
                    _provenance_payment(tmp_path, {
                        "id": "form-look-f40",
                        "origin": "builder",
                        "axis": "other",
                        "frame": 40,
                        "metric": "region_mean",
                        "layer": "1",
                    })
            ]
        ),
        encoding="utf-8",
    )
    closure = validate_claim_closure(
        tmp_path, [SimpleNamespace(id="1", owns=("form",), stages=(unit,))]
    )
    assert not closure.clean
    assert any("belongs to axis" in finding.what for finding in closure.findings)


def test_explicit_primary_is_not_reordered(tmp_path):
    unit = _unit("complete")
    doc = {
        "schema": 4,
        "layers": [
            {
                "id": "4",
                "script": "build/04_lighting.py",
                "title": "Lighting",
                "primary_judge": 40,
                "judge": [
                    {"frame": 120, "ref": "refs/f120.png"},
                    {"frame": 1, "ref": "refs/f001.png"},
                    {"frame": 40, "ref": "refs/f040.png"},
                ],
                "owns": ["form"],
                "reads": "Lighting reads correctly.",
                "stages": [json.loads(json.dumps(unit, default=lambda value: value.__dict__))],
            }
        ],
    }
    # Dataclass serialization above uses tuples and nested dataclasses correctly but its
    # field names already match the schema except JudgePoint, which is an object here.
    stage = doc["layers"][0]["stages"][0]
    stage["evaluation"]["judge"] = [{"frame": 40, "ref": "refs/f040.png"}]
    stage["protects"] = {
        "selector": "all_active_upstream_interfaces",
        "resolve_to_explicit_ids_at": "freeze",
    }
    (tmp_path / "layers.json").write_text(json.dumps(doc), encoding="utf-8")
    layer = load_layers(SimpleNamespace(folder=tmp_path))["4"]
    assert layer.judge_frame == 40
    assert layer.judge_ref == "refs/f040.png"
    assert [frame for frame, _ref in layer.judges] == [120, 1, 40]


def test_layer_loader_rejects_fragment_script_authority(tmp_path):
    first = _unit("form")
    second = _unit(
        "finish",
        depends_on=["form"],
        script_span="build/04_lighting.py#finish",
    )
    stages = [json.loads(json.dumps(unit, default=lambda value: value.__dict__)) for unit in (first, second)]
    for stage in stages:
        stage["evaluation"]["judge"] = [{"frame": 40, "ref": "refs/f040.png"}]
        stage["protects"] = {
            "selector": "all_active_upstream_interfaces",
            "resolve_to_explicit_ids_at": "freeze",
        }
    doc = {
        "schema": 4,
        "layers": [
            {
                "id": "4",
                "script": "build/04_lighting.py",
                "title": "Lighting",
                "primary_judge": 40,
                "judge": [{"frame": 40, "ref": "refs/f040.png"}],
                "owns": ["form"],
                "reads": "Lighting reads correctly.",
                "stages": stages,
            }
        ],
    }
    (tmp_path / "layers.json").write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(ValueError, match="#fragment notation"):
        load_layers(SimpleNamespace(folder=tmp_path))


def test_layer_loader_rejects_composed_layer_script_as_unit_authority(tmp_path):
    units = (
        _unit("form", script_span="build/units/04/form.py"),
        _unit("finish", depends_on=["form"], script_span="build/04_lighting.py"),
    )
    stages = [json.loads(json.dumps(unit, default=lambda value: value.__dict__)) for unit in units]
    for stage in stages:
        stage["evaluation"]["judge"] = [{"frame": 40, "ref": "refs/f040.png"}]
        stage["protects"] = {
            "selector": "all_active_upstream_interfaces",
            "resolve_to_explicit_ids_at": "freeze",
        }
    (tmp_path / "layers.json").write_text(
        json.dumps(
            {
                "schema": 4,
                "layers": [
                    {
                        "id": "4",
                        "script": "build/04_lighting.py",
                        "title": "Lighting",
                        "primary_judge": 40,
                        "judge": [{"frame": 40, "ref": "refs/f040.png"}],
                        "owns": ["form"],
                        "reads": "Lighting reads correctly.",
                        "stages": stages,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="composed layer paths"):
        load_layers(SimpleNamespace(folder=tmp_path))


def test_qualitative_blocking_authority_requires_qualification():
    row = _claim("form") | {"authority": "qualified_qualitative_required"}
    with pytest.raises(ValueError, match="qualification must be an object"):
        Claim.parse(row, "claim")
    row["qualification"] = {
        "suite": "form-v1",
        "judge_model": "judge-v1",
        "prompt": "abc123",
        "evidence_shape": "beauty-plus-focus-v1",
        "artifact": "qualifications/form-v1.json",
        "artifact_sha256": "0" * 64,
    }
    row["evidence"] = [{"kind": "qualification", "id": "form-v1"}]
    assert Claim.parse(row, "claim").authority == "qualified_qualitative_required"


def test_layer_loader_requires_hash_pinned_passed_qualitative_qualification(tmp_path):
    artifact = {
        "schema": 1,
        "suite": "form-v1",
        "judge_model": "judge-v1",
        "prompt": "abc123",
        "evidence_shape": "beauty-plus-focus-v1",
        "passed": True,
        "budgets": {
            "false_pass_rate": 0.05,
            "false_failure_rate": 0.05,
            "repeatability_failure_rate": 0.05,
            "scope_leakage_rate": 0.05,
            "irrelevant_change_sensitivity_rate": 0.05,
        },
        "metrics": {
            "false_pass_rate": 0.0,
            "false_failure_rate": 0.02,
            "repeatability_failure_rate": 0.01,
            "scope_leakage_rate": 0.0,
            "irrelevant_change_sensitivity_rate": 0.0,
        },
    }
    raw = (json.dumps(artifact, sort_keys=True) + "\n").encode()
    (tmp_path / "qualifications").mkdir()
    (tmp_path / "qualifications" / "form-v1.json").write_bytes(raw)
    unit = _unit("form", script_span="build/units/01/form.py")
    stage = json.loads(json.dumps(unit, default=lambda value: value.__dict__))
    stage["evaluation"]["judge"] = [{"frame": 40, "ref": "refs/f040.png"}]
    stage["protects"] = {
        "selector": "all_active_upstream_interfaces",
        "resolve_to_explicit_ids_at": "freeze",
    }
    stage["evaluation"]["claims"][0].update(
        authority="qualified_qualitative_required",
        evidence=[{"kind": "qualification", "id": "form-v1"}],
        qualification={
            "suite": "form-v1",
            "judge_model": "judge-v1",
            "prompt": "abc123",
            "evidence_shape": "beauty-plus-focus-v1",
            "artifact": "qualifications/form-v1.json",
            "artifact_sha256": hashlib.sha256(raw).hexdigest(),
        },
    )
    doc = {
        "schema": 4,
        "layers": [
            {
                "id": "1",
                "script": "build/01_form.py",
                "title": "Form",
                "primary_judge": 40,
                "judge": [{"frame": 40, "ref": "refs/f040.png"}],
                "owns": ["form"],
                "reads": "Form reads.",
                "stages": [stage],
            }
        ],
    }
    (tmp_path / "layers.json").write_text(json.dumps(doc), encoding="utf-8")
    assert load_layers(SimpleNamespace(folder=tmp_path))["1"].stages[0].evaluation.claims[0].required

    artifact["passed"] = False
    bad_raw = (json.dumps(artifact, sort_keys=True) + "\n").encode()
    (tmp_path / "qualifications" / "form-v1.json").write_bytes(bad_raw)
    doc["layers"][0]["stages"][0]["evaluation"]["claims"][0]["qualification"][
        "artifact_sha256"
    ] = hashlib.sha256(bad_raw).hexdigest()
    (tmp_path / "layers.json").write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(ValueError, match="did not pass"):
        load_layers(SimpleNamespace(folder=tmp_path))


def test_interaction_claim_needs_bounded_coordination():
    row = _claim("form") | {
        "kind": "interaction",
        "coordination_owner": "balance",
        "participants": ["form", "exposure"],
    }
    with pytest.raises(ValueError, match="controls must bound"):
        Claim.parse(row, "claim")
    row["controls"] = ["control.key.level"]
    claim = Claim.parse(row, "claim")
    assert claim.coordination_owner == "balance"


def test_protection_selector_freezes_to_explicit_sorted_ids():
    spec = ProtectionSpec.parse(
        {
            "selector": "all_active_upstream_interfaces",
            "resolve_to_explicit_ids_at": "freeze",
        },
        "protects",
    )
    assert spec.resolve(["contract.z", "contract.a", "contract.z"]) == ("contract.a", "contract.z")


def test_checkpoint_and_replan_preserve_identity_but_reopen_plan_bound_receipts(tmp_path):
    old = (_unit("rig"), _unit("form", depends_on=["rig"]), _unit("independent"))
    initialize(tmp_path, "4", old, plan_hash=_PLAN_V1)
    for unit in old:
        _pass_unit(tmp_path, "4", unit, old, plan_hash=_PLAN_V1)

    frozen = load(tmp_path, "4")["units"]["rig"]["checkpoint"]
    assert frozen["protected_contract_ids"] == ["upstream.a", "upstream.z"]

    new = (
        old[0],
        _unit("form", depends_on=["rig"], proposition_suffix="-changed"),
        _unit("finish", depends_on=["form"]),
        old[2],
    )
    record = legacy_apply_replan(
        tmp_path,
        "4",
        old,
        new,
        old_plan_hash=_PLAN_V1,
        new_plan_hash=_PLAN_V2,
        owner="planner",
        trigger="form unit needs a distinct finish dependency",
        evidence=["verdict:form-flat"],
    )
    state = load(tmp_path, "4")
    assert record["invalidated"] == ["finish", "form"]
    assert record["preserved"] == ["independent", "rig"]
    assert state["units"]["rig"]["status"] == "retryable"
    assert state["units"]["independent"]["status"] == "retryable"
    assert "completion_receipt" not in state["units"]["rig"]
    assert "completion_receipt" not in state["units"]["independent"]
    assert state["units"]["form"]["status"] == "pending"
    assert state["units"]["finish"]["status"] == "pending"
    assert state["superseded"][-1]["id"] == "form"
    assert state["replans"][-1] == record


def test_digest_bound_state_replan_preserves_matches_without_old_view(tmp_path):
    """Global republication can make the old materialized view inert before remat.

    Current-schema durable unit hashes are still a closed old identity: matching units
    retain their checkpoint lineage, but plan-bound completion receipts reopen until a
    typed reconciliation exists. Changed/new closure reopens, and removed accepted units
    are retired by the amendment rather than misclassified as unauthorised orphans.
    """
    rig = _unit("rig")
    form = _unit("form", depends_on=["rig"])
    obsolete = _unit("obsolete")
    old = (rig, form, obsolete)
    initialize(tmp_path, "4", old, plan_hash=_PLAN_V1)
    for unit in old:
        _pass_unit(tmp_path, "4", unit, old, plan_hash=_PLAN_V1)

    changed_form = _unit("form", depends_on=["rig"], proposition_suffix="-changed")
    finish = _unit("finish", depends_on=["form"])
    new = (rig, changed_form, finish)
    record = legacy_apply_replan(
        tmp_path,
        "4",
        (),
        new,
        old_plan_hash=_PLAN_V1,
        new_plan_hash=_PLAN_V2,
        owner="planner",
        trigger="selected global generation superseded the old JIT view",
        evidence=["run:republished-plan"],
        state_backed_base=True,
    )

    state = load(tmp_path, "4")
    assert record["added"] == ["finish"]
    assert record["removed"] == ["obsolete"]
    assert record["changed"] == ["form"]
    assert record["invalidated"] == ["finish", "form"]
    assert record["preserved"] == ["rig"]
    assert record.get("orphaned") is None
    assert state["units"]["rig"]["status"] == "retryable"
    assert "completion_receipt" not in state["units"]["rig"]
    assert state["units"]["form"]["status"] == "pending"
    assert state["units"]["finish"]["status"] == "pending"
    assert {row["id"] for row in state["superseded"][-2:]} == {"form", "obsolete"}


def test_failed_dependency_blocks_transitive_units_without_failing_them(tmp_path):
    units = (_unit("a"), _unit("b", depends_on=["a"]), _unit("c", depends_on=["b"]))
    initialize(tmp_path, "1", units, plan_hash="plan")
    state = block_dependents(
        tmp_path,
        "1",
        "a",
        units,
        reason="a needs human-required adjudication",
    )
    assert state["units"]["a"]["status"] == "pending"
    assert state["units"]["b"]["status"] == "blocked"
    assert state["units"]["c"]["status"] == "blocked"


def test_checkpoint_invalidation_revokes_authority_and_blocks_consumers_atomically(tmp_path):
    units = (_unit("a"), _unit("b", depends_on=["a"]), _unit("c", depends_on=["b"]))
    initialize(tmp_path, "1", units, plan_hash=_PLAN)
    _pass_unit(tmp_path, "1", units[0], units, plan_hash=_PLAN)
    claim_for_build(
        tmp_path,
        "1",
        units,
        "b",
        plan_hash=_PLAN,
        eligible_passed={"a"},
    )

    record = invalidate_checkpoint(
        tmp_path,
        "1",
        "a",
        units,
        reason="post-acceptance scope audit failed",
        evidence=["run:test", "violation:undeclared-role"],
    )

    state = load(tmp_path, "1")
    assert record["affected"] == ["a", "b", "c"]
    assert record["archived_checkpoints"]["a"]["candidate_hash"] == "missing"
    assert state["units"]["a"]["status"] == "retryable"
    assert "checkpoint" not in state["units"]["a"]
    assert state["units"]["a"]["invalidated_checkpoints"][-1]["checkpoint"] == record[
        "archived_checkpoints"
    ]["a"]
    assert state["units"]["b"]["status"] == "blocked"
    assert state["units"]["c"]["status"] == "blocked"
    assert state["invalidations"][-1] == record


def test_checkpoint_invalidation_requires_auditable_evidence(tmp_path):
    units = (_unit("a"),)
    initialize(tmp_path, "1", units, plan_hash="plan")
    with pytest.raises(ValueError, match="reason and non-empty evidence"):
        invalidate_checkpoint(tmp_path, "1", "a", units, reason="", evidence=[])


def test_work_unit_state_is_isolated_per_layer(tmp_path):
    layout_units = (_unit("layout"),)
    form_units = (_unit("form"),)
    initialize(tmp_path, "1", layout_units, plan_hash="a" * 64)
    initialize(tmp_path, "2", form_units, plan_hash="b" * 64)
    claim_ready_unit_for_planning(
        tmp_path,
        "1",
        "layout",
        layout_units,
        expected_plan_hash="a" * 64,
        eligible_passed=set(),
        completion_authorization=None,
        run_id="fixture-layer-isolation",
        selection_token=ABSENT_SELECTION_TOKEN,
        reason="fixture layer is dependency-ready",
    )

    assert load(tmp_path, "1")["units"]["layout"]["status"] == "planning"
    assert load(tmp_path, "2")["units"]["form"]["status"] == "pending"


def test_stale_unit_state_cannot_authorize_a_changed_dag(tmp_path):
    initialize(tmp_path, "1", (_unit("layout"),), plan_hash="plan")
    with pytest.raises(
        ValueError,
        match="validated authority replacement or amendment",
    ):
        initialize(
            tmp_path,
            "1",
            (_unit("layout", proposition_suffix="-changed"),),
            plan_hash="plan",
        )


def test_layer_plan_reader_never_flattens_a_multi_unit_dag(tmp_path):
    layer = SimpleNamespace(id="4", stages=(_unit("form"), _unit("finish", depends_on=["form"])))
    with pytest.raises(ValueError, match="cannot choose or combine"):
        read_layer_plan(tmp_path, layer)


def test_provenance_tracks_schema_declared_unit_plan_paths(tmp_path):
    unit = _unit("form")
    stage = json.loads(json.dumps(unit, default=lambda value: value.__dict__))
    stage["evaluation"]["judge"] = [{"frame": 40, "ref": "refs/f040.png"}]
    stage["protects"] = {
        "selector": "all_active_upstream_interfaces",
        "resolve_to_explicit_ids_at": "freeze",
    }
    doc = {
        "schema": 4,
        "layers": [
            {
                "id": "1",
                "script": "build/01_form.py",
                "title": "Form",
                "primary_judge": 40,
                "judge": [{"frame": 40, "ref": "refs/f040.png"}],
                "owns": ["form"],
                "reads": "Form reads.",
                "stages": [stage],
            }
        ],
    }
    (tmp_path / "brief.md").write_text("brief\n", encoding="utf-8")
    (tmp_path / "layers.json").write_text(json.dumps(doc), encoding="utf-8")
    plan = tmp_path / unit.plan
    plan.parent.mkdir(parents=True)
    plan.write_text("bounded unit plan\n", encoding="utf-8")
    provenance_stamp(tmp_path)
    plan.write_text("changed bounded unit plan\n", encoding="utf-8")

    assert any(unit.plan in problem and "edited" in problem.lower() for problem in provenance_check(tmp_path))


def test_render_region_stat_family_tracks_the_metrics_registry():
    """HIR-0048 residual / ADR-0003: payment must not hand-copy METRICS region_* keys."""
    from vfx_harness.domain import image_debts
    from vfx_harness.domain.image_debts import (
        IMAGE_PROPERTY_PREFIXES,
        metric_matches_property,
        metrics_certifying_property,
    )
    from vfx_harness.evidence.checks import METRICS

    assert not hasattr(image_debts, "IMAGE_PROPERTY_METRICS")
    prefix = IMAGE_PROPERTY_PREFIXES["render_region_stat"]
    family = metrics_certifying_property("render_region_stat", METRICS)
    assert family == frozenset(name for name in METRICS if name.startswith(prefix))
    assert family, "METRICS lost its region_* family"
    for name in METRICS:
        assert metric_matches_property(name, "render_region_stat") is (name in family)
