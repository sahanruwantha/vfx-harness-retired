from __future__ import annotations

from copy import deepcopy

import pytest

from vfx_harness.domain.authority_capsules import (
    AuthorityCapsuleError,
    compile_authority_capsules,
)
from vfx_harness.domain.judgment_debts import (
    JudgmentDebtActivation,
    JudgmentDebtSeed,
    JudgmentPoint,
    JudgmentProvider,
    compile_judgment_debt,
)


def _requirement(requirement_id: str, owner: str) -> dict:
    statement = {
        "R-camera": "The camera frames the future hall.",
        "R-form": "The hall has rendered form.",
        "R-sibling": "The independent prop exists.",
    }[requirement_id]
    return {
        "id": requirement_id,
        "statement": statement,
        "citation": {
            "source": "brief.md",
            "sha256": "0" * 64,
            "line_start": 1,
            "line_end": 1,
        },
        "resolution": {
            "kind": "deferred_owner",
            "ids": [],
            "owner_layer": owner,
            "due": {"kind": "before_layer", "layer": owner},
            "evidence_domains": ["image" if owner == "1" else "scene"],
        },
    }


def _sparse_layer(
    layer_id: str,
    *,
    axis: str,
    role: str,
    requirement_id: str,
    depends_on: list[str],
) -> dict:
    frame = 10 * int(layer_id)
    return {
        "id": layer_id,
        "script": f"build/{layer_id}.py",
        "title": f"Layer {layer_id}",
        "primary_judge": frame,
        "judge": [{"frame": frame, "ref": "refs/reference.png"}],
        "owns": [axis],
        "evidence_domains": ["image" if layer_id == "1" else "scene"],
        "reads": f"Layer {layer_id} reads",
        "execution": "jit_deferred",
        "stages": [],
        "jit": {
            "depends_on_layers": depends_on,
            "required_outcomes": [],
            "provides": {"camera": [role]} if layer_id == "1" else {"geometry": [role]},
            "reserved_roles": [role],
            "owned_requirements": [requirement_id],
        },
    }


def _unit(layer_id: str, unit_id: str, *, axis: str, role: str, contract_id: str) -> dict:
    frame = 10 * int(layer_id)
    return {
        "id": unit_id,
        "title": f"Build {unit_id}",
        "plan": f"plans/{layer_id}/{unit_id}.md",
        "depends_on": [],
        "mutates": {
            "mode": "scoped",
            "roles": [role],
            "controls": [],
            "control_roles": {},
            "script_spans": [f"build/units/{layer_id}/{unit_id}.py"],
        },
        "protects": {
            "selector": "all_active_upstream_interfaces",
            "resolve_to_explicit_ids_at": "freeze",
        },
        "evaluation": {
            "primary_judge": frame,
            "judge": [{"frame": frame, "ref": "refs/reference.png"}],
            "temporal_evidence": "none",
            "claims": [
                {
                    "id": f"{unit_id}-claim",
                    "proposition": f"{unit_id} exists",
                    "axis": axis,
                    "property": "object_count",
                    "subject_roles": [role],
                    "subject_controls": [],
                    "moments": [frame],
                    "kind": "atomic",
                    "required": True,
                    "authority": "executable_required",
                    "repair_owner": unit_id,
                    "asserts": "scene",
                    "evidence": [{"kind": "scene_contract", "id": contract_id}],
                }
            ],
        },
        "completion": "all_required_claims_and_protected_contracts_pass",
        "look_capabilities": [],
        "provides": ["camera"] if layer_id == "1" else ["geometry"],
    }


def _contract(layer_id: str, unit_id: str, contract_id: str, role: str) -> dict:
    return {
        "id": contract_id,
        "kind": "object_count",
        "owner_layer": layer_id,
        "fault_owner": unit_id,
        "activates_at": layer_id,
        "lifecycle": "layer",
        "axis": {"1": "camera", "2": "form", "3": "prop"}[layer_id],
        "roles": [role],
        "op": "min",
        "lo": 1,
    }


def _global_documents() -> dict[str, object]:
    layers = [
        _sparse_layer(
            "1", axis="camera", role="camera.rig", requirement_id="R-camera", depends_on=[]
        ),
        _sparse_layer(
            "2", axis="form", role="hall.mass", requirement_id="R-form", depends_on=["1"]
        ),
        _sparse_layer(
            "3", axis="prop", role="prop.mass", requirement_id="R-sibling", depends_on=[]
        ),
    ]
    return {
        "layers.json": {"schema": 5, "layers": layers},
        "scene_checks.json": {"schema": 2, "contracts": []},
        "checks.json": {"schema": 2, "checks": []},
        "requirements.json": {
            "schema": "vfx-harness.requirements/v2",
            "requirements": [
                _requirement("R-camera", "1"),
                _requirement("R-form", "2"),
                _requirement("R-sibling", "3"),
            ],
            "judgment_debt_definitions": [],
            "judgment_debt_activations": [],
        },
        "acceptance.json": [
            {"id": "M-final", "frame": 30, "ref": "refs/reference.png", "reads": "final"}
        ],
    }


def _debt(*, lifecycle: str = "persistent"):
    seed = JudgmentDebtSeed(
        requirement_id="R-camera",
        statement="The camera frames the future hall.",
        decision_strength="planner_start",
        claim_kind="atomic",
        property="camera_framing",
        owner_layer="1",
        fault_owner="camera_unit",
        subject_roles=("hall.mass",),
        axes=("camera",),
        judge_points=(JudgmentPoint(10, "refs/reference.png"),),
        observation_medium="workbench_solid",
        lifecycle=lifecycle,
        bundle_digest="a" * 64,
        carrier_families=("mesh",),
    )
    provider = JudgmentProvider("form-provider", "2", "mesh", ("hall.mass",))
    return compile_judgment_debt(
        seed,
        (provider,),
        layer_dependencies={"1": (), "2": ("1",), "3": ()},
        layer_order=("1", "2", "3"),
    )


def _materialize(
    documents: dict[str, object],
    *,
    layer_id: str,
    unit_id: str,
    axis: str,
    role: str,
    contract_id: str,
    requirement_id: str,
) -> None:
    layers = documents["layers.json"]["layers"]
    index = next(index for index, row in enumerate(layers) if row["id"] == layer_id)
    ready = deepcopy(layers[index])
    ready.pop("jit")
    ready["execution"] = "ready"
    ready["stages"] = [
        _unit(layer_id, unit_id, axis=axis, role=role, contract_id=contract_id)
    ]
    layers[index] = ready
    documents["scene_checks.json"]["contracts"].append(
        _contract(layer_id, unit_id, contract_id, role)
    )
    requirement = next(
        row
        for row in documents["requirements.json"]["requirements"]
        if row["id"] == requirement_id
    )
    requirement["resolution"] = {
        "kind": "contract",
        "ids": [contract_id],
        "evidence_domains": ["scene"],
        "domain_bindings": [
            {"domain": "scene", "kind": "contract", "ids": [contract_id]}
        ],
    }


def _materialize_camera(documents: dict[str, object], definition) -> None:
    _materialize(
        documents,
        layer_id="1",
        unit_id="camera_unit",
        axis="camera",
        role="camera.rig",
        contract_id="camera-count",
        requirement_id="R-camera",
    )
    requirement = documents["requirements.json"]["requirements"][0]
    requirement["resolution"] = {
        "kind": "decision",
        "ids": [],
        "decision": requirement["statement"],
        "decision_strength": "planner_start",
        "evidence_domains": ["image"],
        "domain_bindings": [
            {
                "domain": "image",
                "kind": "provisional_decision",
                "statement": requirement["statement"],
                "decision_strength": "planner_start",
                "debt_id": definition.debt_id,
                "definition_digest": definition.digest,
                "activates_at": "2",
            }
        ],
    }
    documents["requirements.json"]["judgment_debt_definitions"] = [definition.as_dict()]


def test_later_payer_activation_preserves_semantic_owner_capsule() -> None:
    global_documents = _global_documents()
    definition = _debt()
    camera_view = deepcopy(global_documents)
    _materialize_camera(camera_view, definition)
    before = compile_authority_capsules(global_documents, camera_view)

    payer_view = deepcopy(camera_view)
    _materialize(
        payer_view,
        layer_id="2",
        unit_id="form_unit",
        axis="form",
        role="hall.mass",
        contract_id="hall-count",
        requirement_id="R-form",
    )
    activation = JudgmentDebtActivation.for_definition(
        definition,
        payer_unit_digests=(("2:form_unit", "b" * 64),),
    )
    payer_view["requirements.json"]["judgment_debt_activations"] = [activation.as_dict()]
    after = compile_authority_capsules(global_documents, payer_view)

    assert before.layer("1").capsule_digest == after.layer("1").capsule_digest
    assert before.unit("1", "camera_unit").capsule_digest == after.unit(
        "1", "camera_unit"
    ).capsule_digest
    assert before.layer("2").capsule_digest != after.layer("2").capsule_digest
    assert after.layer("1").projection["judgment_debt_activations"] == []
    assert after.layer("2").projection["judgment_debt_activations"] == [
        activation.as_dict()
    ]


@pytest.mark.parametrize("payer_unit_id", ["form_unit", "1:form_unit", "2:missing"])
def test_activation_payer_requires_exact_qualified_unit_identity(
    payer_unit_id: str,
) -> None:
    global_documents = _global_documents()
    definition = _debt()
    payer_view = deepcopy(global_documents)
    _materialize_camera(payer_view, definition)
    _materialize(
        payer_view,
        layer_id="2",
        unit_id="form_unit",
        axis="form",
        role="hall.mass",
        contract_id="hall-count",
        requirement_id="R-form",
    )
    activation = JudgmentDebtActivation.for_definition(
        definition,
        payer_unit_digests=((payer_unit_id, "b" * 64),),
    )
    payer_view["requirements.json"]["judgment_debt_activations"] = [
        activation.as_dict()
    ]

    with pytest.raises(
        AuthorityCapsuleError,
        match="non-canonical payer unit identities",
    ):
        compile_authority_capsules(global_documents, payer_view)


def test_changing_debt_definition_changes_owner_capsule() -> None:
    global_documents = _global_documents()
    first, second = _debt(lifecycle="persistent"), _debt(lifecycle="layer")
    first_view, second_view = deepcopy(global_documents), deepcopy(global_documents)
    _materialize_camera(first_view, first)
    _materialize_camera(second_view, second)

    first_capsules = compile_authority_capsules(global_documents, first_view)
    second_capsules = compile_authority_capsules(global_documents, second_view)

    assert first.debt_id == second.debt_id
    assert first.digest != second.digest
    assert first_capsules.layer("1").capsule_digest != second_capsules.layer(
        "1"
    ).capsule_digest


def test_independent_sibling_materialization_preserves_unaffected_capsules() -> None:
    global_documents = _global_documents()
    definition = _debt()
    before_view = deepcopy(global_documents)
    _materialize_camera(before_view, definition)
    _materialize(
        before_view,
        layer_id="2",
        unit_id="form_unit",
        axis="form",
        role="hall.mass",
        contract_id="hall-count",
        requirement_id="R-form",
    )
    before = compile_authority_capsules(global_documents, before_view)

    after_view = deepcopy(before_view)
    _materialize(
        after_view,
        layer_id="3",
        unit_id="prop_unit",
        axis="prop",
        role="prop.mass",
        contract_id="prop-count",
        requirement_id="R-sibling",
    )
    after = compile_authority_capsules(global_documents, after_view)

    assert before.effective_authority_digest != after.effective_authority_digest
    assert before.layer("1").capsule_digest == after.layer("1").capsule_digest
    assert before.layer("2").capsule_digest == after.layer("2").capsule_digest
    assert before.layer("3").capsule_digest != after.layer("3").capsule_digest
    assert after.layer("1").projection["acceptance"] == global_documents["acceptance.json"]


def test_unit_row_change_changes_unit_and_layer_capsules() -> None:
    global_documents = _global_documents()
    first_view = deepcopy(global_documents)
    _materialize(
        first_view,
        layer_id="3",
        unit_id="prop_unit",
        axis="prop",
        role="prop.mass",
        contract_id="prop-count",
        requirement_id="R-sibling",
    )
    second_view = deepcopy(first_view)
    second_view["layers.json"]["layers"][2]["stages"][0]["title"] = "Changed prop unit"

    first = compile_authority_capsules(global_documents, first_view)
    second = compile_authority_capsules(global_documents, second_view)

    assert first.unit("3", "prop_unit").work_unit_digest != second.unit(
        "3", "prop_unit"
    ).work_unit_digest
    assert first.unit("3", "prop_unit").capsule_digest != second.unit(
        "3", "prop_unit"
    ).capsule_digest
    assert first.layer("3").capsule_digest != second.layer("3").capsule_digest
    assert first.layer("1").capsule_digest == second.layer("1").capsule_digest


def test_requirement_can_bind_a_typed_semantic_diff_without_contract_row() -> None:
    global_documents = _global_documents()
    effective = deepcopy(global_documents)
    layer = deepcopy(effective["layers.json"]["layers"][2])
    layer.pop("jit")
    layer["execution"] = "ready"
    unit = _unit(
        "3",
        "prop_unit",
        axis="prop",
        role="prop.mass",
        contract_id="prop-semantic-diff",
    )
    unit["evaluation"]["claims"][0]["evidence"] = [
        {"kind": "semantic_diff", "id": "prop-semantic-diff"}
    ]
    layer["stages"] = [unit]
    effective["layers.json"]["layers"][2] = layer
    effective["requirements.json"]["requirements"][2]["resolution"] = {
        "kind": "contract",
        "ids": ["prop-semantic-diff"],
    }

    capsules = compile_authority_capsules(global_documents, effective)

    unit_capsule = capsules.unit("3", "prop_unit")
    assert unit_capsule.projection["claim_bindings"][0]["kind"] == "semantic_diff"
    assert unit_capsule.projection["requirements"][0]["id"] == "R-sibling"


def test_unsupported_schema_and_ambiguous_ownership_fail_closed() -> None:
    global_documents = _global_documents()
    bad_schema = deepcopy(global_documents)
    bad_schema["checks.json"]["schema"] = 3
    with pytest.raises(AuthorityCapsuleError, match="schema unsupported"):
        compile_authority_capsules(global_documents, bad_schema)

    ambiguous = deepcopy(global_documents)
    _materialize(
        ambiguous,
        layer_id="3",
        unit_id="prop_unit",
        axis="prop",
        role="prop.mass",
        contract_id="prop-count",
        requirement_id="R-sibling",
    )
    ambiguous["checks.json"]["checks"] = [
        {"id": "prop-count", "owner_layer": "1"}
    ]
    with pytest.raises(AuthorityCapsuleError, match="ambiguous across contract catalogs"):
        compile_authority_capsules(global_documents, ambiguous)
