from __future__ import annotations

import hashlib
import importlib
import json
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image

from tests.layer_outcome_fixtures import write_test_layer_outcome
from tests.unit_attempt_fixtures import pass_unit, synthetic_completion_receipt
from vfx_harness.agents.builder.composition_helpers import (
    compose_unit_artifact_source,
)
from vfx_harness.agents.builder.layer_artifact import (
    commit_layer_artifact,
    prepare_layer_artifact,
)
from vfx_harness.agents.builder.layer_finalization_guard import (
    LayerFinalizationClaimGuard,
)
from vfx_harness.domain.judgment_debt_replay_receipts import (
    ReplayPrefixLayerReceipt,
    ReplayPrefixReceipt,
    ReplayPrefixUnitReceipt,
)
from vfx_harness.domain.judgment_debts import (
    JudgmentDebtActivation,
    JudgmentDebtSeed,
    JudgmentPoint,
    JudgmentProvider,
    compile_judgment_debt,
)
from vfx_harness.domain.layer_finalizations import (
    LAYER_FINALIZATION_PROJECTION_SCHEMA,
    LayerEvaluationReceipt,
    LayerFinalizationReceipt,
    LayerReplayClaimRequirement,
    LayerReplayEvaluationGroupPlan,
    LayerReplayObservation,
    LayerReplayPointObservation,
    LayerReplayReceipt,
    LayerReplayReceiptBinding,
)
from vfx_harness.domain.plan_records import (
    load_active_structured_decisions,
    load_judgment_debt_activations,
    load_judgment_debt_definitions,
    load_requirements,
    read_selected_bundle_hash,
)
from vfx_harness.domain.unit_evaluation_receipts import ReplayInputBinding
from vfx_harness.evaluation.plan_gate import _check_meta_records
from vfx_harness.evidence.checks import acceptance_evidence
from vfx_harness.observability import run_artifacts
from vfx_harness.orchestration import plan_bundle_integrity, unit_state
from vfx_harness.orchestration.authority_capsule_resolution import (
    selected_layer_capsule_digest,
)
from vfx_harness.orchestration.authority_selection import resolve_selected_authority
from vfx_harness.orchestration.jit_materialization import (
    MATERIALIZATION_SCHEMA,
    apply_materialization_patch,
    apply_materialization_patches,
    inspect_materialization,
    publish_materialization,
    validate_materialization,
)
from vfx_harness.orchestration.jit_materialization.errors import (
    MaterializationSelectionConflict,
)
from vfx_harness.orchestration.layer_evaluation_receipts import (
    commit_layer_evaluation_receipt,
    prepare_layer_evaluation_receipt,
)
from vfx_harness.orchestration.layer_finalization_state import (
    claim_layer_finalization,
    complete_layer_finalization,
)
from vfx_harness.orchestration.layer_outcome_paths import layer_outcome_path
from vfx_harness.orchestration.layer_plans import (
    build_layer_outcome_projection,
    layer_finalization_canonical_rows,
)
from vfx_harness.orchestration.layer_replay_receipts import (
    commit_layer_replay_receipt,
    prepare_layer_replay_receipt,
)
from vfx_harness.orchestration.ledger import load_layers_from_path
from vfx_harness.orchestration.plan_authority import publish_current as _publish_current
from vfx_harness.orchestration.plan_authority import selected_artifact_path
from vfx_harness.orchestration.plan_due import (
    PlanDueError,
    require_due_clear,
    resolve_acceptance_completion,
    resolve_unit_completion,
)
from vfx_harness.orchestration.revalidation import eligibility, input_manifest


def _deferred_owner(layer: str, *domains: str) -> dict:
    return {
        "kind": "deferred_owner",
        "ids": [],
        "owner_layer": layer,
        "due": {"kind": "before_layer", "layer": layer},
        "evidence_domains": list(domains) or ["scene"],
    }


def _judgment(
    *,
    fault_owner: str = "polish",
    property_kind: str = "subject_appearance",
    subject_roles: list[str] | None = None,
    moments: list[int] | None = None,
) -> dict:
    return {
        "claim_kind": "atomic",
        "property": property_kind,
        "fault_owner": fault_owner,
        "subject_roles": subject_roles or ["polish.comp"],
        "axes": ["final_lock"],
        "moments": moments or [239, 240],
        "carrier_families": ["mesh"],
        "observation_medium": "workbench_solid",
        "lifecycle": "persistent",
    }


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) + "\n", encoding="utf-8")


def _write_authority_record(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _select_generation(root: Path, bundle_hash: str) -> None:
    """Pin the folder to one generation hash so ledger adoption can key to it."""
    from vfx_harness.orchestration.authority_selection_transaction import (
        AuthoritySelectionToken,
    )
    from vfx_harness.orchestration.plan_consumer_view import (
        OVERLAY_ARTIFACTS,
        PlanConsumerViewMarker,
    )

    marker = PlanConsumerViewMarker(
        shot=root,
        bundle=(
            root
            / "runs"
            / "fixture-plan"
            / "checkpoints"
            / "plans"
            / "bundles"
            / bundle_hash
        ),
        content_hash=bundle_hash,
        base_selection=AuthoritySelectionToken(
            plan_revision=1,
            plan_pointer_sha256=hashlib.sha256(b"fixture plan pointer").hexdigest(),
            jit_revision=0,
            jit_pointer_sha256=None,
        ),
        view_source="bundle",
        view_digest=bundle_hash,
        artifact_hashes={
            name: hashlib.sha256(f"fixture:{name}".encode()).hexdigest()
            for name in OVERLAY_ARTIFACTS
        },
        authored_inputs={"brief.md": hashlib.sha256(b"fixture brief").hexdigest()},
        decision_inputs={},
    )
    _write_authority_record(root / ".plan-consumer-view.json", marker.to_dict())


def _plan_pointer_payload(bundle_hash: str) -> dict:
    from vfx_harness.orchestration.plan_pointer import PlanPointer

    return PlanPointer(
        revision=1,
        run_id="fixture-plan",
        bundle=(
            Path("runs")
            / "fixture-plan"
            / "checkpoints"
            / "plans"
            / "bundles"
            / bundle_hash
        ),
        content_hash=bundle_hash,
        outcome="clean",
        published_at="2026-09-01T00:00:00+00:00",
    ).as_dict()


def test_selected_bundle_hash_reads_only_strict_v2_heads(tmp_path: Path) -> None:
    selected = hashlib.sha256(b"selected generation").hexdigest()
    assert read_selected_bundle_hash(tmp_path) is None

    _select_generation(tmp_path, selected)
    assert read_selected_bundle_hash(tmp_path) == selected

    (tmp_path / ".plan-consumer-view.json").unlink()
    _write_authority_record(
        tmp_path / "plans" / "current.json",
        _plan_pointer_payload(selected),
    )
    assert read_selected_bundle_hash(tmp_path) == selected


@pytest.mark.parametrize(
    "damage",
    ("legacy_schema", "extra_field", "zero_plan_revision", "bad_digest", "bad_view"),
)
def test_selected_bundle_hash_rejects_invalid_consumer_marker(
    tmp_path: Path,
    damage: str,
) -> None:
    selected = hashlib.sha256(b"selected generation").hexdigest()
    _select_generation(tmp_path, selected)
    marker_path = tmp_path / ".plan-consumer-view.json"
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    if damage == "legacy_schema":
        marker["schema"] = "vfx-harness.plan-consumer-view/v1"
    elif damage == "extra_field":
        marker["unexpected"] = True
    elif damage == "zero_plan_revision":
        marker["base_selection"]["plan_revision"] = 0
        marker["base_selection"]["plan_pointer_sha256"] = None
    elif damage == "bad_digest":
        marker["content_hash"] = "not-a-digest"
    else:
        marker["effective_view"]["artifact_hashes"].pop("checks.json")
    _write_authority_record(marker_path, marker)

    with pytest.raises(ValueError):
        read_selected_bundle_hash(tmp_path)


def test_selected_bundle_hash_rejects_duplicate_marker_keys_without_pointer_fallback(
    tmp_path: Path,
) -> None:
    selected = hashlib.sha256(b"selected generation").hexdigest()
    _write_authority_record(
        tmp_path / "plans" / "current.json",
        _plan_pointer_payload(selected),
    )
    (tmp_path / ".plan-consumer-view.json").write_text(
        '{"schema":"vfx-harness.plan-consumer-view/v3",'
        '"schema":"vfx-harness.plan-consumer-view/v3"}\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate JSON key"):
        read_selected_bundle_hash(tmp_path)


def test_selected_bundle_hash_rejects_duplicate_plan_pointer_keys(
    tmp_path: Path,
) -> None:
    pointer = tmp_path / "plans" / "current.json"
    pointer.parent.mkdir(parents=True)
    pointer.write_text(
        '{"schema":"vfx-harness.plan-pointer/v2",'
        '"schema":"vfx-harness.plan-pointer/v2"}\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate JSON key"):
        read_selected_bundle_hash(tmp_path)


@pytest.mark.parametrize("head", ["consumer", "plan"])
def test_selected_bundle_hash_rejects_noncanonical_head_bytes(
    tmp_path: Path,
    head: str,
) -> None:
    selected = hashlib.sha256(b"selected generation").hexdigest()
    if head == "consumer":
        _select_generation(tmp_path, selected)
        path = tmp_path / ".plan-consumer-view.json"
    else:
        path = tmp_path / "plans" / "current.json"
        _write_authority_record(path, _plan_pointer_payload(selected))
    value = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps(value) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="bytes are not canonical"):
        read_selected_bundle_hash(tmp_path)


@pytest.mark.parametrize(
    "damage",
    ("legacy_schema", "extra_field", "zero_revision", "bad_digest", "wrong_bundle"),
)
def test_selected_bundle_hash_rejects_invalid_plan_pointer(
    tmp_path: Path,
    damage: str,
) -> None:
    selected = hashlib.sha256(b"selected generation").hexdigest()
    pointer = _plan_pointer_payload(selected)
    if damage == "legacy_schema":
        pointer["schema"] = "vfx-harness.plan-pointer/v1"
    elif damage == "extra_field":
        pointer["unexpected"] = True
    elif damage == "zero_revision":
        pointer["revision"] = False
    elif damage == "bad_digest":
        pointer["content_hash"] = "not-a-digest"
    else:
        pointer["bundle"] = "runs/fixture-plan/checkpoints/plans/bundles/wrong"
    _write_authority_record(tmp_path / "plans" / "current.json", pointer)

    with pytest.raises(ValueError):
        read_selected_bundle_hash(tmp_path)


def _candidate(root: Path) -> None:
    (root / "brief.md").write_text("Final image must hold unchanged from frame 239 to 240.\n", encoding="utf-8")
    (root / "refs").mkdir()
    (root / "refs" / "a.png").write_bytes(b"sealed layer-one reference")
    (root / "plans").mkdir()
    (root / "plans" / "global.md").write_text("# executable fixture plan\n", encoding="utf-8")
    _write(root / "layers.json", {
        "schema": 4,
        "layers": [{
            "id": "1", "script": "build/01_finish.py", "title": "Finish",
            "primary_judge": 240,
            "judge": [{"frame": 239, "ref": "refs/a.png"}, {"frame": 240, "ref": "refs/a.png"}],
            "owns": ["final_lock"], "reads": "locked ending",
            "evidence_domains": ["scene", "temporal"],
            "stages": [{
                "id": "lock", "title": "Lock", "plan": "plans/01_finish/lock.md",
                "depends_on": [],
                "mutates": {"mode": "scoped", "roles": ["comp"], "controls": ["hold"],
                            "control_roles": {"hold": ["comp"]},
                            "script_spans": ["build/units/01/lock.py"]},
                "protects": {"selector": "all_active_upstream_interfaces",
                             "resolve_to_explicit_ids_at": "freeze"},
                "evaluation": {"primary_judge": 240,
                               "judge": [{"frame": 239, "ref": "refs/a.png"},
                                         {"frame": 240, "ref": "refs/a.png"}],
                               "temporal_evidence": "keyframes",
                               "claims": [{
                                   "id": "lock-claim", "proposition": "ending locks",
                                   "axis": "final_lock", "property": "frame_delta",
                                   "subject_roles": ["comp"], "subject_controls": ["hold"],
                                   "moments": [239, 240], "kind": "atomic", "required": True,
                                   "authority": "executable_required", "repair_owner": "lock",
                                   "asserts": "image",
                                   "evidence": [{"kind": "scene_contract", "id": "final-lock"}],
                               }],
                               "composition_context": {
                                   "frames": [239, 240],
                                   "contract_ids": ["vis-f239", "vis-f240"],
                               }},
                "completion": "all_required_claims_and_protected_contracts_pass",
            }],
        }],
    })
    _write(root / "acceptance.json", [])
    _write(root / "critic_axes.json", [{"key": "final_lock", "desc": "ending is still"}])
    _write(root / "checks.json", {"schema": 2, "checks": []})
    _write(root / "scene_checks.json", {"schema": 2, "contracts": [{
        "id": "final-lock", "kind": "frame_delta", "owner_layer": "1",
        "fault_owner": "1", "activates_at": "1", "lifecycle": "layer",
        "axis": "final_lock", "frames": [239, 240], "op": "max", "hi": 0.01,
    }]})
    digest = hashlib.sha256((root / "brief.md").read_bytes()).hexdigest()
    _write(root / "requirements.json", {
        "schema": "vfx-harness.requirements/v2",
        "judgment_debt_definitions": [],
        "judgment_debt_activations": [],
        "requirements": [{
            "id": "R-final-lock", "statement": "frames 239 and 240 are unchanged",
            "citation": {"source": "brief.md", "sha256": digest, "line_start": 1, "line_end": 1},
            "resolution": {"kind": "obligation", "ids": ["O-final-lock"]},
        }],
    })
    _write(root / "obligations.json", {
        "schema": "vfx-harness.obligations/v1",
        "obligations": [{
            "id": "O-final-lock", "statement": "prove the 239 to 240 rendered lock",
            "requirement_ids": ["R-final-lock"], "owner": "1.lock",
            "due": {"kind": "before_acceptance"},
            "evidence": [{"kind": "scene_contract", "id": "final-lock"}],
        }],
    })
    _write(root / "assumptions.json", {
        "schema": "vfx-harness.assumptions/v1", "assumptions": [],
    })


def _add_judgment_debt_catalog(root: Path, bundle_digest: str) -> None:
    """Attach one exact qualitative-debt definition and payer to the fixture."""
    definition = compile_judgment_debt(
        JudgmentDebtSeed(
            requirement_id="R-final-lock",
            statement="frames 239 and 240 are unchanged",
            decision_strength="approved_start",
            claim_kind="atomic",
            property="reference_identity",
            owner_layer="1",
            fault_owner="lock",
            subject_roles=("comp",),
            axes=("final_lock",),
            judge_points=(JudgmentPoint(frame=240, ref="refs/a.png"),),
            observation_medium="workbench_solid",
            lifecycle="layer",
            bundle_digest=bundle_digest,
            carrier_families=("mesh",),
        ),
        (JudgmentProvider("comp-mesh", "1", "mesh", ("comp.surface",)),),
        layer_dependencies={"1": ()},
        layer_order=("1",),
    )
    activation = JudgmentDebtActivation.for_definition(
        definition,
        payer_unit_digests=(("lock", hashlib.sha256(b"lock").hexdigest()),),
    )
    requirements = json.loads((root / "requirements.json").read_text(encoding="utf-8"))
    requirements["judgment_debt_definitions"] = [definition.as_dict()]
    requirements["judgment_debt_activations"] = [activation.as_dict()]
    requirements["requirements"][0]["resolution"] = {
        "kind": "decision",
        "ids": [],
        "decision": definition.seed.statement,
        "decision_strength": definition.seed.decision_strength,
        "evidence_domains": ["image"],
        "domain_bindings": [
            {
                "domain": "image",
                "kind": "provisional_decision",
                "statement": definition.seed.statement,
                "decision_strength": definition.seed.decision_strength,
                "debt_id": definition.debt_id,
                "definition_digest": definition.digest,
                "activates_at": definition.binding.activates_at,
            }
        ],
    }
    _write(root / "requirements.json", requirements)


def test_judgment_debt_catalog_loads_strict_typed_round_trip(tmp_path: Path) -> None:
    _candidate(tmp_path)
    bundle_digest = hashlib.sha256(b"selected-plan").hexdigest()
    _add_judgment_debt_catalog(tmp_path, bundle_digest)

    requirements = load_requirements(tmp_path)
    definitions = load_judgment_debt_definitions(
        tmp_path,
        requirements=requirements,
        selected_bundle_digest=bundle_digest,
    )
    activations = load_judgment_debt_activations(
        tmp_path,
        definitions=definitions,
    )

    assert len(definitions) == len(activations) == 1
    assert activations[0].definition_digest == definitions[0].digest


def test_judgment_debt_catalog_rejects_stale_and_orphan_rows(tmp_path: Path) -> None:
    _candidate(tmp_path)
    bundle_digest = hashlib.sha256(b"selected-plan").hexdigest()
    _add_judgment_debt_catalog(tmp_path, bundle_digest)
    requirements = json.loads((tmp_path / "requirements.json").read_text(encoding="utf-8"))
    requirements["judgment_debt_definitions"][0]["definition_digest"] = "0" * 64
    _write(tmp_path / "requirements.json", requirements)

    with pytest.raises(ValueError, match="definition_digest is stale"):
        load_judgment_debt_definitions(tmp_path)

    _add_judgment_debt_catalog(tmp_path, bundle_digest)
    definitions = load_judgment_debt_definitions(tmp_path)
    orphan = replace(
        load_judgment_debt_activations(tmp_path, definitions=definitions)[0],
        definition_digest=hashlib.sha256(b"orphan-definition").hexdigest(),
    )
    requirements = json.loads((tmp_path / "requirements.json").read_text(encoding="utf-8"))
    requirements["judgment_debt_activations"] = [orphan.as_dict()]
    _write(tmp_path / "requirements.json", requirements)

    with pytest.raises(ValueError, match="unknown judgment debt definition"):
        load_judgment_debt_activations(tmp_path, definitions=definitions)


def test_judgment_debt_catalog_rejects_duplicate_definition_and_activation(
    tmp_path: Path,
) -> None:
    _candidate(tmp_path)
    bundle_digest = hashlib.sha256(b"selected-plan").hexdigest()
    _add_judgment_debt_catalog(tmp_path, bundle_digest)
    requirements = json.loads((tmp_path / "requirements.json").read_text(encoding="utf-8"))
    requirements["judgment_debt_definitions"].append(
        requirements["judgment_debt_definitions"][0]
    )
    _write(tmp_path / "requirements.json", requirements)

    with pytest.raises(ValueError, match="definition_digest duplicates exact definition"):
        load_judgment_debt_definitions(tmp_path)

    _add_judgment_debt_catalog(tmp_path, bundle_digest)
    definition = load_judgment_debt_definitions(tmp_path)[0]
    conflicting = compile_judgment_debt(
        replace(
            definition.seed,
            bundle_digest=hashlib.sha256(b"conflicting-plan").hexdigest(),
        ),
        definition.providers,
        layer_dependencies={"1": ()},
        layer_order=("1",),
    )
    requirements = json.loads((tmp_path / "requirements.json").read_text(encoding="utf-8"))
    requirements["judgment_debt_definitions"].append(conflicting.as_dict())
    _write(tmp_path / "requirements.json", requirements)

    with pytest.raises(ValueError, match="debt_id duplicates"):
        load_judgment_debt_definitions(tmp_path)

    _add_judgment_debt_catalog(tmp_path, bundle_digest)
    requirements = json.loads((tmp_path / "requirements.json").read_text(encoding="utf-8"))
    requirements["judgment_debt_activations"].append(
        requirements["judgment_debt_activations"][0]
    )
    _write(tmp_path / "requirements.json", requirements)

    with pytest.raises(ValueError, match="duplicates activation for exact definition"):
        load_judgment_debt_activations(tmp_path)


def test_judgment_debt_catalog_requires_exact_image_domain_binding(
    tmp_path: Path,
) -> None:
    _candidate(tmp_path)
    bundle_digest = hashlib.sha256(b"selected-plan").hexdigest()
    _add_judgment_debt_catalog(tmp_path, bundle_digest)
    requirements = json.loads((tmp_path / "requirements.json").read_text(encoding="utf-8"))
    binding = requirements["requirements"][0]["resolution"]["domain_bindings"][0]
    binding["definition_digest"] = hashlib.sha256(b"unknown-definition").hexdigest()
    _write(tmp_path / "requirements.json", requirements)

    with pytest.raises(ValueError, match="names no selected judgment debt definition"):
        load_judgment_debt_definitions(tmp_path)


def test_plan_gate_reports_judgment_debt_requirement_and_bundle_conflicts(
    tmp_path: Path,
) -> None:
    _candidate(tmp_path)
    bundle_digest = hashlib.sha256(b"selected-plan").hexdigest()
    _add_judgment_debt_catalog(tmp_path, bundle_digest)
    requirements = json.loads((tmp_path / "requirements.json").read_text(encoding="utf-8"))
    requirements["requirements"][0]["statement"] = "rewritten requirement"
    _write(tmp_path / "requirements.json", requirements)

    with pytest.raises(ValueError, match=r"must exactly equal.*requirement"):
        load_judgment_debt_definitions(tmp_path)

    requirements["requirements"][0]["statement"] = "frames 239 and 240 are unchanged"
    _write(tmp_path / "requirements.json", requirements)
    _select_generation(tmp_path, hashlib.sha256(b"different-plan").hexdigest())

    findings, _ = _check_meta_records(tmp_path)

    assert len(findings) == 1
    assert findings[0].check == "requirement-closure"
    assert "seed.bundle_digest is stale for selected bundle" in findings[0].what


def _add_deferred_layer(root: Path) -> None:
    data = json.loads((root / "layers.json").read_text(encoding="utf-8"))
    data["layers"][0]["execution"] = "ready"
    data["layers"].append({
        "id": "2", "script": "build/02_polish.py", "title": "Polish",
        "primary_judge": 240,
        "judge": [{"frame": 239, "ref": "refs/a.png"}, {"frame": 240, "ref": "refs/a.png"}],
        "owns": ["final_lock"], "reads": "polished ending",
        "evidence_domains": ["scene", "temporal", "image", "projected_composition"],
        "execution": "jit_deferred", "stages": [],
        "jit": {
            "depends_on_layers": ["1"],
            "required_outcomes": [{"kind": "scene_contract", "id": "final-lock"}],
            "provides": {},
            "reserved_roles": ["polish.*"],
            "owned_requirements": ["R-final-lock"],
        },
    })
    _write(root / "layers.json", data)
    requirements = json.loads((root / "requirements.json").read_text(encoding="utf-8"))
    requirements["requirements"][0]["resolution"] = _deferred_owner("2", "image")
    _write(root / "requirements.json", requirements)
    _write(root / "obligations.json", {
        "schema": "vfx-harness.obligations/v1", "obligations": [],
    })


def _declaring(layer: dict) -> dict:
    """Materialized stages must declare look_capabilities; [] owns no appearance."""
    row = dict(layer)
    row["stages"] = [
        {**stage, "look_capabilities": stage.get("look_capabilities", [])}
        for stage in row.get("stages") or []
    ]
    return row


def _vis_rows(layer_id: str, frames: tuple[int, ...], axis: str = "final_lock") -> list[dict]:
    # validate_materialization requires occlusion-true visibility evidence at every
    # judge frame (run 20260825: a lookdev layer was judged at frames where every
    # subject sat behind a solid proxy disc and no contract could say so)
    return [{
        "id": f"vis-f{frame}", "kind": "visible_fraction", "owner_layer": layer_id,
        "fault_owner": layer_id, "activates_at": layer_id, "lifecycle": "layer",
        "axis": axis, "roles": ["comp"], "frame": frame, "op": "min", "lo": 0.25,
    } for frame in frames]


def _base_selection(root: Path):
    return resolve_selected_authority(root).selection_token


def _attest_materialization(
    root: Path,
    candidate: Path,
    *,
    overlay_root: Path | None = None,
) -> None:
    """Attest through the same prepared transition used by production finalization."""

    from tests.materialization_support import (
        attest_exact_materialization_view,
    )

    attest_exact_materialization_view(
        root,
        candidate,
        overlay_root=overlay_root,
    )


def _publish_materialization(
    root: Path,
    candidate: Path,
    *,
    overlay_root: Path | None = None,
) -> Path:
    _attest_materialization(root, candidate, overlay_root=overlay_root)
    return publish_materialization(root, candidate, overlay_root=overlay_root)


def _unit_reserved_roles(layer: dict) -> list[str]:
    roles: set[str] = set()
    for unit in layer.get("stages") or []:
        mutates = unit.get("mutates") or {}
        roles.update(str(role) for role in mutates.get("roles") or [])
        roles.update(str(role) for role in unit.get("dresses") or [])
    return sorted(roles)


def _unit_provides(layer: dict) -> dict[str, list[str]]:
    provided: dict[str, set[str]] = {}
    for unit in layer.get("stages") or []:
        roles = [str(role) for role in (unit.get("mutates") or {}).get("roles") or []]
        for capability in unit.get("provides") or []:
            provided.setdefault(str(capability), set()).update(roles)
    return {key: sorted(values) for key, values in sorted(provided.items())}


def _sparse_publication_rows(
    root: Path,
) -> list[tuple[dict, list[dict], list[dict]]]:
    """Convert legacy ready fixture rows into strict sparse global authority.

    The returned rows are the exact effective layers the fixture had already treated as
    materialized.  Publication selects only deferred stubs; the test helper then installs
    these rows through the real JIT transaction.
    """

    document = json.loads((root / "layers.json").read_text(encoding="utf-8"))
    requirements = json.loads((root / "requirements.json").read_text(encoding="utf-8"))
    owned_by_layer: dict[str, list[str]] = {}
    for requirement in requirements["requirements"]:
        resolution = requirement.get("resolution") or {}
        if resolution.get("kind") == "deferred_owner":
            owned_by_layer.setdefault(str(resolution["owner_layer"]), []).append(
                str(requirement["id"])
            )
    ready_rows: list[tuple[dict, list[dict], list[dict]]] = []
    sparse_rows: list[dict] = []
    for row in document["layers"]:
        if row.get("execution") == "jit_deferred":
            deferred = json.loads(json.dumps(row))
            deferred["jit"].setdefault("provides", {})
            deferred["jit"]["required_outcomes"] = []
            sparse_rows.append(deferred)
            continue
        ready = json.loads(json.dumps(row))
        ready["execution"] = "ready"
        ready.pop("jit", None)
        ready_rows.append((ready, [], []))
        layer_id = str(ready["id"])
        owned_requirements = owned_by_layer.get(layer_id, [])
        if not owned_requirements:
            requirement_id = f"R-fixture-prerequisite-{layer_id}"
            authored = requirements["requirements"][0]
            requirements["requirements"].append(
                {
                    "id": requirement_id,
                    "statement": authored["statement"],
                    "citation": json.loads(json.dumps(authored["citation"])),
                    "resolution": _deferred_owner(layer_id, "image"),
                }
            )
            owned_requirements = [requirement_id]
        sparse = {
            key: json.loads(json.dumps(value))
            for key, value in row.items()
            if key
            in {
                "id",
                "script",
                "title",
                "primary_judge",
                "judge",
                "owns",
                "evidence_domains",
                "reads",
                "dressable",
            }
        }
        sparse.update(
            {
                "execution": "jit_deferred",
                "stages": [],
                "jit": {
                    "depends_on_layers": [],
                    "required_outcomes": [],
                    "provides": _unit_provides(ready),
                    "reserved_roles": _unit_reserved_roles(ready),
                    "owned_requirements": owned_requirements,
                },
            }
        )
        sparse_rows.append(sparse)
    document["schema"] = 5
    document["layers"] = sparse_rows
    ready_ids = {str(row["id"]) for row, _scene, _image in ready_rows}
    scene_document = json.loads((root / "scene_checks.json").read_text(encoding="utf-8"))
    image_document = json.loads((root / "checks.json").read_text(encoding="utf-8"))
    captured: list[tuple[dict, list[dict], list[dict]]] = []
    for row, _scene, _image in ready_rows:
        layer_id = str(row["id"])
        captured.append(
            (
                row,
                [
                    json.loads(json.dumps(contract))
                    for contract in scene_document["contracts"]
                    if str(contract.get("owner_layer")) == layer_id
                ],
                [
                    json.loads(json.dumps(contract))
                    for contract in image_document["checks"]
                    if str(contract.get("owner_layer")) == layer_id
                ],
            )
        )
    scene_document["contracts"] = [
        row
        for row in scene_document["contracts"]
        if str(row.get("owner_layer")) not in ready_ids
    ]
    image_document["checks"] = [
        row
        for row in image_document["checks"]
        if str(row.get("owner_layer")) not in ready_ids
    ]
    _write(root / "layers.json", document)
    _write(root / "requirements.json", requirements)
    _write(root / "scene_checks.json", scene_document)
    _write(root / "checks.json", image_document)
    return captured


def _materialize_fixture_ready_layer(
    root: Path,
    bundle_hash: str,
    layer: dict,
    global_scene_contracts: list[dict],
    global_image_contracts: list[dict],
    *,
    overlay_root: Path | None = None,
) -> None:
    layer_id = str(layer["id"])
    existing_ids = {str(row["id"]) for row in global_scene_contracts}
    scene_contracts = json.loads(json.dumps(global_scene_contracts))
    required_context_ids = {
        str(contract_id)
        for unit in layer.get("stages") or []
        for contract_id in (unit.get("evaluation") or {})
        .get("composition_context", {})
        .get("contract_ids", [])
    }
    missing_visibility = sorted(required_context_ids - existing_ids)
    visibility_id_map: dict[str, str] = {}
    if missing_visibility:
        frames = tuple(
            int(point["frame"])
            for point in layer.get("judge") or []
            if f"vis-f{int(point['frame'])}" in missing_visibility
        )
        for row in _vis_rows(layer_id, frames):
            old_id = str(row["id"])
            new_id = f"fixture-{layer_id}-{old_id}"
            visibility_id_map[old_id] = new_id
            row["id"] = new_id
            scene_contracts.append(row)
    image_contracts = json.loads(json.dumps(global_image_contracts))
    materialized = _declaring(json.loads(json.dumps(layer)))
    materialized["execution"] = "ready"
    materialized.pop("jit", None)
    for unit in materialized.get("stages") or []:
        context = (unit.get("evaluation") or {}).get("composition_context") or {}
        context["contract_ids"] = [
            visibility_id_map.get(str(contract_id), str(contract_id))
            for contract_id in context.get("contract_ids") or []
        ]
    sparse_layers = json.loads((root / "layers.json").read_text(encoding="utf-8"))[
        "layers"
    ]
    sparse = next(row for row in sparse_layers if str(row["id"]) == layer_id)
    binding_contract_ids = [
        str(row["id"])
        for row in [*scene_contracts, *image_contracts]
    ]
    if not binding_contract_ids:
        raise AssertionError(
            f"fixture ready layer {layer_id!r} has no scene contract for its owned requirement"
        )
    payload = root / f"fixture-materialize-{layer_id}.json"
    _write(
        payload,
        {
            "schema": MATERIALIZATION_SCHEMA,
            "bundle_hash": bundle_hash,
            "base_selection": _base_selection(root).to_dict(),
            "layer": materialized,
            "scene_contracts": scene_contracts,
            "image_contracts": image_contracts,
            "requirement_bindings": [
                {
                    "requirement_id": requirement_id,
                    "contract_ids": [binding_contract_ids[0]],
                }
                for requirement_id in sparse["jit"]["owned_requirements"]
            ],
            "acceptance": [],
        },
    )
    _publish_materialization(root, payload, overlay_root=overlay_root)


def publish_current(
    shot_folder: str | Path,
    layout,
    *,
    outcome: str,
    plan_path: str | Path | None = None,
    source_root: str | Path | None = None,
):
    """Publish schema-5 fixture authority and restore pre-materialized test layers.

    HIR-0171 forbids a ready row in global authority.  Older tests used such rows as
    setup, so this adapter preserves their intended effective view through the public
    sparse-publication plus JIT-materialization lifecycle.
    """

    root = Path(shot_folder).resolve()
    source = Path(source_root).resolve() if source_root is not None else root
    ready_rows = _sparse_publication_rows(source)
    bundle = _publish_current(
        root,
        layout,
        outcome=outcome,
        plan_path=plan_path,
        source_root=source_root,
    )
    if source == root:
        for layer, scene_contracts, image_contracts in ready_rows:
            _materialize_fixture_ready_layer(
                root,
                bundle.content_hash,
                layer,
                scene_contracts,
                image_contracts,
            )
    return bundle


def _publish_due_fixture(root: Path, layout):
    """Select sparse authority while retaining the legacy due-record catalog under test."""

    requirements = json.loads((root / "requirements.json").read_text(encoding="utf-8"))
    requirements["requirements"][0]["resolution"] = _deferred_owner("1", "image")
    _write(root / "requirements.json", requirements)
    _sparse_publication_rows(root)
    return _publish_current(root, layout, outcome="clean_with_deferred")


def _jit_payload(root: Path, bundle_hash: str) -> Path:
    layers = json.loads((root / "layers.json").read_text(encoding="utf-8"))["layers"]
    layer = dict(layers[1])
    layer["execution"] = "ready"
    layer.pop("jit")
    layer["stages"] = [{
        "id": "polish", "title": "Polish lock", "plan": "plans/02_polish/polish.md",
        "depends_on": [],
        "mutates": {"mode": "scoped", "roles": ["polish.comp"], "controls": ["hold"],
                    "control_roles": {"hold": ["polish.comp"]},
                    "script_spans": ["build/units/02/polish.py"]},
        "protects": {"selector": "all_active_upstream_interfaces",
                     "resolve_to_explicit_ids_at": "freeze"},
        "evaluation": {"primary_judge": 240, "judge": layer["judge"],
                       "temporal_evidence": "keyframes", "claims": [{
                           "id": "polish-lock", "proposition": "polish preserves the lock",
                           "axis": "final_lock", "property": "frame_delta",
                           "subject_roles": ["polish.comp"], "subject_controls": ["hold"],
                           "moments": [239, 240], "kind": "atomic", "required": True,
                           "authority": "executable_required", "repair_owner": "polish",
                           "asserts": "image",
                           "evidence": [{"kind": "scene_contract", "id": "polish-lock"}],
                       }],
                       "composition_context": {
                           "frames": [239, 240],
                           "contract_ids": ["vis-f239", "vis-f240"],
                       }},
        "completion": "all_required_claims_and_protected_contracts_pass",
        "look_capabilities": [],
    }]
    path = root / "jit.json"
    _write(path, {
        "schema": MATERIALIZATION_SCHEMA,
        "bundle_hash": bundle_hash,
        "base_selection": resolve_selected_authority(root).selection_token.to_dict(),
        "layer": layer,
        "scene_contracts": [{
            "id": "polish-lock", "kind": "frame_delta", "owner_layer": "2",
            "fault_owner": "2", "activates_at": "2", "lifecycle": "layer",
            "axis": "final_lock", "frames": [239, 240], "op": "max", "hi": 0.01,
        }, *_vis_rows("2", (239, 240))],
        "image_contracts": [],
        "requirement_bindings": [{
            "requirement_id": "R-final-lock", "contract_ids": ["polish-lock"],
        }],
        "acceptance": [],
    })
    return path


def _passed_layer_one_outcome(root: Path) -> None:
    layer = load_layers_from_path(selected_artifact_path(root, "layers.json"))["1"]
    for unit in layer.stages:
        plan = root / unit.plan
        plan.parent.mkdir(parents=True, exist_ok=True)
        plan.write_text(f"# sealed plan for {unit.id}\n", encoding="utf-8")
    for _frame, ref in layer.judges:
        reference = root / ref
        if not reference.is_file():
            raise AssertionError(f"fixture reference was not published before plan selection: {ref}")
    selected_authority = resolve_selected_authority(root)
    plan_hash = selected_layer_capsule_digest(
        root,
        layer.id,
        selected_authority,
    )
    unit_state.initialize(
        root,
        layer.id,
        layer.stages,
        plan_hash=plan_hash,
    )
    state = unit_state.load(root, layer.id)
    eligible = {
        unit_id
        for unit_id, row in state["units"].items()
        if row.get("status") == "passed"
    }
    for unit in layer.stages:
        if unit.id not in eligible:
            pass_unit(
                root,
                layer.id,
                unit,
                layer.stages,
                plan_hash=plan_hash,
                eligible_passed=eligible,
                selection_token=selected_authority.selection_token,
            )
            eligible.add(unit.id)

    state = unit_state.load(root, layer.id)
    parts: list[tuple[str, str, str]] = []
    for unit in layer.stages:
        completion = state["units"][unit.id]["completion_receipt"]
        unit_script = str(completion["script_path"])
        parts.append(
            (
                unit.id,
                unit_script,
                (root / unit_script).read_text(encoding="utf-8"),
            )
        )
    evaluation_barrier = "# fixture evaluated-state barrier\npass"
    proposed_source = compose_unit_artifact_source(
        parts,
        evaluation_barrier=evaluation_barrier,
    ).encode("utf-8")
    proposed_sha256 = hashlib.sha256(proposed_source).hexdigest()
    claim = claim_layer_finalization(
        root,
        layer,
        expected_plan_hash=plan_hash,
        run_id="sealed-layer-one-fixture",
        layer_script_sha256=proposed_sha256,
        predecessor_inputs=(),
        selection_token=selected_authority.selection_token,
    )
    claim_guard = LayerFinalizationClaimGuard.bind(
        root,
        claim,
        layer.stages,
        selected_authority,
    )
    prepared_artifact = prepare_layer_artifact(
        root,
        claim_guard,
        evaluation_barrier=evaluation_barrier,
    )
    commit_layer_artifact(prepared_artifact, claim_guard)
    replay_prefix = ReplayPrefixReceipt(
        (
            ReplayPrefixLayerReceipt(
                layer_id=claim.layer_id,
                layer_generation_digest=claim.plan_hash,
                predecessor_layer_digests=(),
                script_path=claim.layer_script_path,
                script_sha256=proposed_sha256,
                dependencies=(),
                units=tuple(
                    ReplayPrefixUnitReceipt(
                        layer_id=claim.layer_id,
                        unit_id=row.unit_id,
                        unit_digest=row.unit_digest,
                        checkpoint_unit_digest=row.unit_digest,
                        script_path=row.script_path,
                        script_sha256=row.script_sha256,
                        checkpoint_script_sha256=row.script_sha256,
                        completion_receipt_digest=row.completion_receipt_digest,
                    )
                    for row in claim.unit_inputs
                ),
                payer_claim_id=claim.claim_id,
            ),
        )
    )
    group_plan = LayerReplayEvaluationGroupPlan(
        group_index=0,
        planned_group_count=1,
        requirement_ids=(),
        debt_id=None,
        definition_digest=None,
        activation_digest=None,
        payment_generation_digest=None,
        judge_points=tuple((frame, ref) for frame, ref in layer.judges),
        axes=("final_lock",),
        claims=(
            LayerReplayClaimRequirement(
                claim_id="sealed-layer-one-final-lock",
                authority="executable_required",
                judge_frames=tuple(frame for frame, _ref in layer.judges),
                evidence_ids=("final-lock",),
            ),
        ),
        evidence_kind="executable_only",
        render_mode=None,
        render_scale=None,
    )
    point_evidence = (
        {
            "id": "final-lock",
            "metric": "frame_delta",
            "value": 0.0,
            "target": "<= 0.01",
            "pass": True,
            "source": "interface_contract",
            "authoritative": True,
            "owner_layer": "1",
            "fault_owner": "1",
            "activates_at": "1",
            "lifecycle": "layer",
        },
    )
    points = tuple(
        LayerReplayPointObservation.mint(
            plan=group_plan,
            frame=frame,
            ref=ref,
            ref_sha256=hashlib.sha256((root / ref).read_bytes()).hexdigest(),
            evidence=point_evidence,
        )
        for frame, ref in layer.judges
    )
    replay = LayerReplayReceipt.mint(
        claim=claim,
        layer_script_sha256=proposed_sha256,
        replay_inputs=(
            ReplayInputBinding.mint(
                script_path=layer.script,
                script_sha256=proposed_sha256,
            ),
        ),
        observation=LayerReplayObservation(
            replay_prefix=replay_prefix,
            plan=group_plan,
            points=points,
        ),
        created_at="2026-09-01T00:01:00+00:00",
    )
    prepared_replay = prepare_layer_replay_receipt(root, replay)
    stored_replay = commit_layer_replay_receipt(prepared_replay, claim_guard)
    canonical = [
        (
            (frame, ref),
            {
                "evidence_kind": "executable_only",
                "pass": True,
                "mean": 5.0,
                "decided_by": "unit_executable_evidence",
                "issues": [],
                "evidence": list(point_evidence),
                "evidence_failures": [],
                "missing_evidence": [],
                "layer_replay_receipt_digest": replay.receipt_digest,
            },
        )
        for frame, ref in layer.judges
    ]
    best = {"round": 1, "mean": 5.0, "render": None}
    revalidation_projection = {
        "schema": "vfx-harness.layer-image-check-revalidation/v1",
        "layer_id": layer.id,
        "source_sha256": None,
        "replacement_sha256": None,
        "replacement_text": None,
        "result": {"kept": 0, "dropped": []},
    }
    outcome_projection = build_layer_outcome_projection(
        root,
        layer,
        best=best,
        canonical=canonical,
        finalization_claim=claim,
        final_status="passed",
        blender_version="fixture",
        revalidation_projection=revalidation_projection,
        selected_authority=selected_authority,
    )
    receipt_canonical = layer_finalization_canonical_rows(canonical)
    evaluation = LayerEvaluationReceipt.mint(
        replay_receipts=(
            LayerReplayReceiptBinding.mint(
                locator=stored_replay.locator,
                sha256=stored_replay.sha256,
                receipt=replay,
            ),
        ),
        evaluation_groups=[
            {
                "group_index": 0,
                "result": "passed",
                "requirement_ids": [],
                "debt_id": None,
                "definition_digest": None,
                "activation_digest": None,
                "canonical_start": 0,
                "canonical_end": len(canonical),
                "payment_failures": [],
            }
        ],
        canonical=receipt_canonical,
        created_at="2026-09-01T00:01:30+00:00",
    )
    prepared_evaluation = prepare_layer_evaluation_receipt(root, evaluation)
    stored_evaluation = commit_layer_evaluation_receipt(
        prepared_evaluation,
        claim_guard,
    )
    receipt = LayerFinalizationReceipt.mint(
        evaluation_receipt=evaluation,
        evaluation_receipt_locator=stored_evaluation.locator,
        evaluation_receipt_sha256=stored_evaluation.sha256,
        projection={
            "schema": LAYER_FINALIZATION_PROJECTION_SCHEMA,
            "best": best,
            "blender_version": "fixture",
            "ablation": {"ok": True, "note": "fixture ablation"},
            "revalidation": revalidation_projection,
            "judgment_debts": [],
            "finding": None,
            "outcome": outcome_projection.as_dict(),
            "ledger": {
                "status": "passed",
                "script": layer.script,
                "script_sha256": proposed_sha256,
            },
        },
        completed_at="2026-09-01T00:02:00+00:00",
    )
    complete_layer_finalization(
        root,
        receipt,
        evaluation,
        layer.stages,
        selection_token=selected_authority.selection_token,
    )
    write_test_layer_outcome(
        root,
        layer,
        status="passed",
        best={"round": 1, "mean": 5.0, "render": None},
        canonical=canonical,
        run_id="sealed-layer-one-fixture",
        attempt=1,
        blender_version="fixture",
        selected_authority=selected_authority,
        finalization_receipt=receipt,
    )
    ledger = json.loads((root / "shot.json").read_text(encoding="utf-8"))
    ledger.setdefault("milestones", {}).setdefault(layer.id, {}).update(
        {
            "status": "passed",
            "script": layer.script,
            "script_sha256": proposed_sha256,
            "finalization_receipt_digest": receipt.receipt_digest,
        }
    )
    _write(root / "shot.json", ledger)


def test_producer_sealed_outcome_rejects_a_changed_layer_script(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    layout = run_artifacts.create(tmp_path, "sealed-script-integrity")
    publish_current(tmp_path, layout, outcome="clean_with_deferred")
    _passed_layer_one_outcome(tmp_path)

    layer = load_layers_from_path(selected_artifact_path(tmp_path, "layers.json"))["1"]
    outcome = json.loads(
        layer_outcome_path(tmp_path, "1").read_text(encoding="utf-8")
    )
    sealed_manifest = input_manifest(tmp_path, layer, blender_version="fixture")
    assert eligibility(outcome, sealed_manifest, tmp_path)[0]

    (tmp_path / layer.script).write_text(
        "# changed after the producer sealed this layer\n",
        encoding="utf-8",
    )
    changed_manifest = input_manifest(tmp_path, layer, blender_version="fixture")
    eligible, reasons = eligibility(outcome, changed_manifest, tmp_path)
    assert not eligible
    assert "input manifest changed" in reasons


def test_deferred_root_needs_no_fictional_upstream_outcome(tmp_path: Path) -> None:
    _candidate(tmp_path)
    document = json.loads((tmp_path / "layers.json").read_text(encoding="utf-8"))
    document["schema"] = 5
    layer = document["layers"][0]
    layer["execution"] = "jit_deferred"
    layer["stages"] = []
    layer["jit"] = {
        "depends_on_layers": [],
        "required_outcomes": [],
        "reserved_roles": ["product.*"],
        "owned_requirements": ["R-final-lock"],
    }
    _write(tmp_path / "layers.json", document)

    parsed = load_layers_from_path(tmp_path / "layers.json")

    assert parsed["1"].execution == "jit_deferred"
    assert parsed["1"].jit.depends_on_layers == ()
    assert parsed["1"].jit.required_outcomes == ()


def test_deferred_root_materializes_without_fabricated_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    _candidate(tmp_path)
    document = json.loads((tmp_path / "layers.json").read_text(encoding="utf-8"))
    ready_layer = document["layers"][0]
    deferred_layer = {**ready_layer, "execution": "jit_deferred", "stages": []}
    deferred_layer["jit"] = {
        "depends_on_layers": [],
        "required_outcomes": [],
        "reserved_roles": ["comp"],
        "owned_requirements": ["R-final-lock"],
    }
    document["schema"] = 5
    document["layers"] = [deferred_layer]
    _write(tmp_path / "layers.json", document)
    _write(tmp_path / "scene_checks.json", {"schema": 2, "contracts": []})
    requirements = json.loads((tmp_path / "requirements.json").read_text(encoding="utf-8"))
    requirements["requirements"][0]["resolution"] = _deferred_owner("1", "image")
    _write(tmp_path / "requirements.json", requirements)
    _write(tmp_path / "obligations.json", {
        "schema": "vfx-harness.obligations/v1", "obligations": [],
    })
    layout = run_artifacts.create(tmp_path, "root-materialization")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    payload = tmp_path / "root-jit.json"
    _write(payload, {
        "schema": MATERIALIZATION_SCHEMA,
        "bundle_hash": bundle.content_hash,
        "base_selection": _base_selection(tmp_path).to_dict(),
        "layer": _declaring({**ready_layer, "execution": "ready"}),
        "scene_contracts": [{
            "id": "final-lock", "kind": "frame_delta", "owner_layer": "1",
            "fault_owner": "1", "activates_at": "1", "lifecycle": "layer",
            "axis": "final_lock", "frames": [239, 240], "op": "max", "hi": 0.01,
        }, *_vis_rows("1", (239, 240))],
        "image_contracts": [],
        "requirement_bindings": [{
            "requirement_id": "R-final-lock", "contract_ids": ["final-lock"],
        }],
        "acceptance": [],
    })

    pointer = _publish_materialization(tmp_path, payload)

    assert pointer.is_file()
    selected = json.loads(pointer.read_text(encoding="utf-8"))
    assert selected["materialized_layers"] == ["1"]


@pytest.mark.parametrize("camera_role", ["product.camera", "motion.rig"])
def test_materialized_consumer_keeps_global_camera_capability_from_sparse_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, camera_role: str
) -> None:
    """A ready overlay has no jit block, but the gate must retain global interfaces.

    Product and motion namespaces prove the mechanism is typed authority rather than a
    camera-name heuristic. Before HIR-0087 both fixtures emitted global-capability
    blockers immediately after a valid materialization.
    """
    from vfx_harness.evaluation.plan_gate import _check_contracts
    from vfx_harness.orchestration.plan_authority import prepare_consumer_view

    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    _candidate(tmp_path)
    document = json.loads((tmp_path / "layers.json").read_text(encoding="utf-8"))
    ready_layer = document["layers"][0]
    stage = ready_layer["stages"][0]
    stage["provides"] = ["camera"]
    stage["mutates"]["roles"] = [camera_role]
    stage["mutates"]["control_roles"] = {"hold": [camera_role]}
    for claim in stage["evaluation"]["claims"]:
        claim["subject_roles"] = [camera_role]
    deferred_layer = {**ready_layer, "execution": "jit_deferred", "stages": []}
    deferred_layer["jit"] = {
        "depends_on_layers": [],
        "required_outcomes": [],
        "provides": {"camera": [camera_role]},
        "reserved_roles": [camera_role],
        "owned_requirements": ["R-final-lock"],
    }
    document["schema"] = 5
    document["layers"] = [deferred_layer]
    _write(tmp_path / "layers.json", document)
    _write(tmp_path / "scene_checks.json", {"schema": 2, "contracts": []})
    requirements = json.loads((tmp_path / "requirements.json").read_text(encoding="utf-8"))
    requirements["requirements"][0]["resolution"] = _deferred_owner("1", "image")
    _write(tmp_path / "requirements.json", requirements)
    _write(tmp_path / "obligations.json", {
        "schema": "vfx-harness.obligations/v1", "obligations": [],
    })
    layout = run_artifacts.create(tmp_path, f"global-capability-{camera_role.replace('.', '-')}")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    payload = tmp_path / "root-jit.json"
    visibility = _vis_rows("1", (239, 240))
    for row in visibility:
        row["roles"] = [camera_role]
    _write(payload, {
        "schema": MATERIALIZATION_SCHEMA,
        "bundle_hash": bundle.content_hash,
        "base_selection": _base_selection(tmp_path).to_dict(),
        "layer": _declaring({**ready_layer, "execution": "ready"}),
        "scene_contracts": [{
            "id": "final-lock", "kind": "frame_delta", "owner_layer": "1",
            "fault_owner": "1", "activates_at": "1", "lifecycle": "layer",
            "axis": "final_lock", "frames": [239, 240], "op": "max", "hi": 0.01,
        }, *visibility],
        "image_contracts": [],
        "requirement_bindings": [{
            "requirement_id": "R-final-lock", "contract_ids": ["final-lock"],
        }],
        "acceptance": [],
    })

    _publish_materialization(tmp_path, payload)
    consumer = run_artifacts.create(tmp_path, f"consumer-{camera_role.replace('.', '-')}")
    view = prepare_consumer_view(consumer)
    overlaid = json.loads((view / "layers.json").read_text(encoding="utf-8"))
    assert "jit" not in overlaid["layers"][0]

    findings, _ = _check_contracts(view)
    assert not [finding for finding in findings if finding.check == "global-capability"]


@pytest.mark.parametrize(
    ("payload", "patches", "expected"),
    [
        (
            {"product": {"material": "clay", "parts": ["body"]}},
            (("/product/material", "metal"), ("/product/parts/-", "trim")),
            {"product": {"material": "metal", "parts": ["body", "trim"]}},
        ),
        (
            {"motion": {"curves": ["linear"], "timing": {"end": 24}}},
            (("/motion/curves/0", "bezier"), ("/motion/timing/end", 48)),
            {"motion": {"curves": ["bezier"], "timing": {"end": 48}}},
        ),
    ],
)
def test_materialization_patch_batch_is_atomic_and_supports_append(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: dict,
    patches: tuple[tuple[str, object], ...],
    expected: dict,
) -> None:
    """Independent product and motion repairs cost one write and one validation."""
    import vfx_harness.orchestration.jit_materialization as materialization

    candidate = tmp_path / "candidate.json"
    _write(candidate, payload)
    validations: list[Path] = []

    def inspect(*args, **kwargs):
        validations.append(Path(args[1]))
        return [], None

    monkeypatch.setattr(materialization, "inspect_materialization", inspect)
    assert apply_materialization_patches(
        tmp_path,
        candidate,
        patches,
        expected_bundle_hash="0" * 64,
    ) == []
    assert json.loads(candidate.read_text(encoding="utf-8")) == expected
    assert len(validations) == 1
    assert validations[0] != candidate
    assert not validations[0].exists()

    before = candidate.read_bytes()
    with pytest.raises(ValueError, match=r"absent|does not exist|out of range"):
        apply_materialization_patches(
            tmp_path,
            candidate,
            (("/motion/missing", 1), ("/absent/child", 2)),
            expected_bundle_hash="0" * 64,
        )
    assert candidate.read_bytes() == before


def test_materialization_candidate_is_seeded_and_staged_one_unit_at_a_time(
    tmp_path: Path,
) -> None:
    from vfx_harness.orchestration.jit_materialization import (
        seed_materialization_candidate,
        stage_materialization_unit,
    )

    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    layout = run_artifacts.create(tmp_path, "incremental-materialization")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    full = json.loads(_jit_payload(tmp_path, bundle.content_hash).read_text(encoding="utf-8"))
    target = tmp_path / "incremental.json"

    seed_materialization_candidate(
        bundle.root,
        target,
        layer_id="2",
        bundle_hash=bundle.content_hash,
        base_selection=_base_selection(tmp_path),
    )
    seeded = json.loads(target.read_text(encoding="utf-8"))
    assert seeded["layer"]["execution"] == "ready"
    assert "jit" not in seeded["layer"]
    assert seeded["layer"]["stages"] == []
    assert seeded["scene_contracts"] == []

    unit = full["layer"]["stages"][0]
    stage_materialization_unit(
        target,
        unit=unit,
        scene_contracts=full["scene_contracts"],
        requirement_bindings=full["requirement_bindings"],
    )
    staged = json.loads(target.read_text(encoding="utf-8"))
    assert [row["id"] for row in staged["layer"]["stages"]] == ["polish"]
    assert len(staged["scene_contracts"]) == len(full["scene_contracts"])

    validate_materialization(
        bundle.root,
        target,
        expected_bundle_hash=bundle.content_hash,
    )
    before = target.read_bytes()
    with pytest.raises(ValueError, match="already staged"):
        stage_materialization_unit(
            target,
            unit=unit,
            scene_contracts=[],
            requirement_bindings=[],
        )
    assert target.read_bytes() == before


@pytest.mark.parametrize("camera_role", ["product.view_rig", "motion.capture_host"])
def test_camera_global_layer_refuses_geometry_proxy_before_candidate_write(
    tmp_path: Path, camera_role: str
) -> None:
    """HIR-0128: typed camera authority cannot manufacture subject form."""
    from vfx_harness.domain.work_units import (
        CAMERA_LAYER_DEFERS_SUBJECT_FORM_RULE,
        allowed_unit_provides,
        extra_reserved_roles_on_camera_layer,
        work_unit_authoring_schema,
    )
    from vfx_harness.orchestration.jit_materialization import (
        seed_materialization_candidate,
        stage_materialization_unit,
    )

    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    document = json.loads((tmp_path / "layers.json").read_text(encoding="utf-8"))
    global_row = document["layers"][1]
    global_row["jit"]["provides"] = {"camera": [camera_role]}
    global_row["jit"]["reserved_roles"] = [camera_role, "polish.*"]
    _write(tmp_path / "layers.json", document)
    layout = run_artifacts.create(tmp_path, "camera-layer-geometry-proxy")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    full = json.loads(_jit_payload(tmp_path, bundle.content_hash).read_text(encoding="utf-8"))
    unit = full["layer"]["stages"][0]
    unit["provides"] = ["geometry"]
    target = tmp_path / "camera-layer-staging.json"
    seed_materialization_candidate(
        bundle.root,
        target,
        layer_id="2",
        bundle_hash=bundle.content_hash,
        base_selection=_base_selection(tmp_path),
    )
    allowed = allowed_unit_provides(global_row)
    assert allowed == frozenset({"camera"})
    assert allowed_unit_provides({"jit": {"provides": {}}}) == frozenset({"geometry"})
    schema = work_unit_authoring_schema(allowed_provides=allowed)
    assert schema["properties"]["provides"]["items"]["enum"] == ["camera"]
    assert "persistent bbox_*" in schema["properties"]["provides"]["items"]["description"]
    before = target.read_bytes()

    with pytest.raises(
        ValueError,
        match=r"global capability boundary refused.*provides:\[\"geometry\"\].*composition_context",
    ):
        stage_materialization_unit(
            target,
            unit=unit,
            scene_contracts=full["scene_contracts"],
            requirement_bindings=full["requirement_bindings"],
            allowed_provides=allowed,
        )

    assert target.read_bytes() == before
    assert "earliest downstream form layer" in CAMERA_LAYER_DEFERS_SUBJECT_FORM_RULE
    assert extra_reserved_roles_on_camera_layer(
        provided_capabilities={"camera"},
        camera_selectors=["product.view_rig"],
        reserved_roles=["product.view_rig", "polish.*"],
    ) == ("polish.*",)
    assert extra_reserved_roles_on_camera_layer(
        provided_capabilities={"camera"},
        camera_selectors=["motion.capture_host"],
        reserved_roles=["motion.capture_host"],
    ) == ()


def test_materialization_finalization_rejects_camera_layer_geometry_proxy(
    tmp_path: Path,
) -> None:
    """HIR-0128: final publication independently enforces sparse authority."""
    from vfx_harness.domain.work_units import CAMERA_LAYER_DEFERS_SUBJECT_FORM_RULE

    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    document = json.loads((tmp_path / "layers.json").read_text(encoding="utf-8"))
    global_row = document["layers"][1]
    global_row["jit"]["provides"] = {"camera": ["view.rig"]}
    global_row["jit"]["reserved_roles"] = ["view.rig", "polish.*"]
    _write(tmp_path / "layers.json", document)
    layout = run_artifacts.create(tmp_path, "camera-layer-geometry-finalization")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    payload = _jit_payload(tmp_path, bundle.content_hash)
    materialized = json.loads(payload.read_text(encoding="utf-8"))
    materialized["layer"]["stages"][0]["provides"] = ["geometry"]
    _write(payload, materialized)

    findings, accepted = inspect_materialization(
        bundle.root, payload, expected_bundle_hash=bundle.content_hash
    )

    assert accepted is None
    text = "\n".join(findings)
    assert "/layer/stages/0/provides:" in text
    assert "outside sparse global authority: geometry" in text
    assert CAMERA_LAYER_DEFERS_SUBJECT_FORM_RULE in text


def test_future_active_contract_is_context_not_claim_evidence(tmp_path: Path) -> None:
    """HIR-0129: deferred evidence cannot seal the unit that merely authors it."""
    from vfx_harness.domain.work_units import DEFERRED_CONTRACT_CONTEXT_RULE
    from vfx_harness.orchestration.jit_materialization import (
        seed_materialization_candidate,
        stage_materialization_unit,
    )

    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    layout = run_artifacts.create(tmp_path, "deferred-contract-context")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    full = json.loads(_jit_payload(tmp_path, bundle.content_hash).read_text(encoding="utf-8"))
    deferred_bbox = {
        "id": "future-subject-bbox",
        "kind": "bbox_height",
        "owner_layer": "2",
        "fault_owner": "2",
        "activates_at": "3",
        "lifecycle": "persistent",
        "axis": "final_lock",
        "roles": ["product.subject"],
        "frame": 240,
        "op": "band",
        "lo": 0.45,
        "hi": 0.55,
    }
    direct_unit = json.loads(json.dumps(full["layer"]["stages"][0]))
    direct_unit["evaluation"]["claims"][0]["evidence"].append(
        {"kind": "scene_contract", "id": deferred_bbox["id"], "moments": [240]}
    )
    direct_target = tmp_path / "deferred-direct.json"
    seed_materialization_candidate(
        bundle.root,
        direct_target,
        layer_id="2",
        bundle_hash=bundle.content_hash,
        base_selection=_base_selection(tmp_path),
    )
    before = direct_target.read_bytes()

    with pytest.raises(
        ValueError,
        match=r"deferred contract claim binding refused.*future-subject-bbox.*composition_context",
    ):
        stage_materialization_unit(
            direct_target,
            unit=direct_unit,
            scene_contracts=[*full["scene_contracts"], deferred_bbox],
            requirement_bindings=full["requirement_bindings"],
        )

    assert direct_target.read_bytes() == before

    context_unit = json.loads(json.dumps(full["layer"]["stages"][0]))
    context_unit["evaluation"]["composition_context"]["contract_ids"].append(
        deferred_bbox["id"]
    )
    context_target = tmp_path / "deferred-context.json"
    seed_materialization_candidate(
        bundle.root,
        context_target,
        layer_id="2",
        bundle_hash=bundle.content_hash,
        base_selection=_base_selection(tmp_path),
    )
    stage_materialization_unit(
        context_target,
        unit=context_unit,
        scene_contracts=[*full["scene_contracts"], deferred_bbox],
        requirement_bindings=full["requirement_bindings"],
    )
    staged = json.loads(context_target.read_text(encoding="utf-8"))
    assert deferred_bbox["id"] in (
        staged["layer"]["stages"][0]["evaluation"]["composition_context"]["contract_ids"]
    )

    direct_payload = tmp_path / "deferred-direct-finalization.json"
    direct_document = json.loads(json.dumps(full))
    direct_document["layer"]["stages"][0] = direct_unit
    direct_document["scene_contracts"].append(deferred_bbox)
    _write(direct_payload, direct_document)
    findings, accepted = inspect_materialization(
        bundle.root, direct_payload, expected_bundle_hash=bundle.content_hash
    )
    assert accepted is None
    assert DEFERRED_CONTRACT_CONTEXT_RULE in "\n".join(findings)

    from vfx_harness.evaluation.plan_gate import _check_evidence_coherence

    gate_root = tmp_path / "deferred-gate"
    _write(gate_root / "layers.json", {"schema": 4, "layers": [direct_document["layer"]]})
    _write(
        gate_root / "scene_checks.json",
        {"schema": 2, "contracts": direct_document["scene_contracts"]},
    )
    _write(gate_root / "checks.json", {"schema": 2, "checks": []})
    gate_findings, _counts = _check_evidence_coherence(gate_root)
    due = [
        finding
        for finding in gate_findings
        if finding.check == "unit-evidence-due"
        and "future-subject-bbox" in finding.where
    ]
    assert due and DEFERRED_CONTRACT_CONTEXT_RULE in due[0].fix
    assert not any(
        finding.check == "role-selector-closure"
        and "future-subject-bbox" in finding.where
        for finding in gate_findings
    )


def test_deferred_contract_keeps_owner_judge_frame_authority(tmp_path: Path) -> None:
    """HIR-0130: activation layers pay owner moments as extra-frame evidence."""
    from vfx_harness.domain.work_units import DEFERRED_CONTRACT_FRAME_AUTHORITY_RULE
    from vfx_harness.evaluation.plan_gate import _check_contracts

    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    layers = json.loads((tmp_path / "layers.json").read_text(encoding="utf-8"))
    layers["layers"][1]["primary_judge"] = 40
    layers["layers"][1]["judge"] = [{"frame": 40, "ref": "refs/a.png"}]
    _write(tmp_path / "layers.json", layers)
    scene = json.loads((tmp_path / "scene_checks.json").read_text(encoding="utf-8"))
    deferred = {
        "id": "owner-frame-deferred-bbox",
        "kind": "bbox_height",
        "owner_layer": "1",
        "fault_owner": "1",
        "activates_at": "2",
        "lifecycle": "persistent",
        "axis": "final_lock",
        "roles": ["comp"],
        "frame": 239,
        "op": "band",
        "lo": 0.45,
        "hi": 0.55,
    }
    scene["contracts"].append(deferred)
    _write(tmp_path / "scene_checks.json", scene)

    findings, _counts = _check_contracts(tmp_path)
    assert not any(
        finding.check == "contracts"
        and finding.where == deferred["id"]
        and "not judged" in finding.what
        for finding in findings
    )

    scene["contracts"][-1]["frame"] = 238
    _write(tmp_path / "scene_checks.json", scene)
    findings, _counts = _check_contracts(tmp_path)
    frame_findings = [
        finding
        for finding in findings
        if finding.check == "contracts"
        and finding.where == deferred["id"]
        and "not judged" in finding.what
    ]
    assert frame_findings
    assert "owner layer 1" in frame_findings[0].what
    assert "activation layer 2" not in frame_findings[0].what
    assert DEFERRED_CONTRACT_FRAME_AUTHORITY_RULE in frame_findings[0].fix

    scene["contracts"][-1].update(
        {"owner_layer": "2", "fault_owner": "2", "frame": 239}
    )
    _write(tmp_path / "scene_checks.json", scene)
    findings, _counts = _check_contracts(tmp_path)
    ordinary = [
        finding
        for finding in findings
        if finding.check == "contracts"
        and finding.where == deferred["id"]
        and "not judged" in finding.what
    ]
    assert ordinary and "activation layer 2" in ordinary[0].what


def test_unit_staging_refuses_unpayable_image_property_before_write(
    tmp_path: Path,
) -> None:
    from vfx_harness.orchestration.jit_materialization import (
        seed_materialization_candidate,
        stage_materialization_unit,
    )

    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    layout = run_artifacts.create(tmp_path, "image-property-staging")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    full = json.loads(_jit_payload(tmp_path, bundle.content_hash).read_text(encoding="utf-8"))
    target = tmp_path / "incremental.json"
    seed_materialization_candidate(
        bundle.root,
        target,
        layer_id="2",
        bundle_hash=bundle.content_hash,
        base_selection=_base_selection(tmp_path),
    )
    unit = full["layer"]["stages"][0]
    unit["evaluation"]["claims"][0].update(
        {
            "property": "rim_light_emission_glow",
            "asserts": "image",
            "evidence": [{"kind": "image_contract", "id": "rim-glow-f240"}],
        }
    )
    before = target.read_bytes()

    with pytest.raises(ValueError, match="image property vocabulary refused"):
        stage_materialization_unit(
            target,
            unit=unit,
            scene_contracts=full["scene_contracts"],
            requirement_bindings=full["requirement_bindings"],
        )

    assert target.read_bytes() == before


def test_unit_staging_refuses_same_layer_dressing_before_write(tmp_path: Path) -> None:
    """HIR-0161: sibling geometry cannot be dressed; grant is later-layer only."""
    from vfx_harness.domain.dressing import SAME_LAYER_DRESS_RULE
    from vfx_harness.orchestration.jit_materialization import (
        seed_materialization_candidate,
        stage_materialization_unit,
    )

    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    layout = run_artifacts.create(tmp_path, "same-layer-dress-staging")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    full = json.loads(_jit_payload(tmp_path, bundle.content_hash).read_text(encoding="utf-8"))
    target = tmp_path / "same-layer-dress.json"
    seed_materialization_candidate(
        bundle.root,
        target,
        layer_id="2",
        bundle_hash=bundle.content_hash,
        base_selection=_base_selection(tmp_path),
    )
    unit = full["layer"]["stages"][0]
    stage_materialization_unit(
        target,
        unit=unit,
        scene_contracts=full["scene_contracts"],
        requirement_bindings=full["requirement_bindings"],
    )
    shade = json.loads(json.dumps(unit))
    shade["id"] = "shade"
    shade["plan"] = "plans/02_polish/shade.md"
    shade["depends_on"] = ["polish"]
    shade["mutates"]["roles"] = ["polish.shade"]
    shade["mutates"]["controls"] = []
    shade["mutates"]["control_roles"] = {}
    shade["mutates"]["dresses"] = ["polish.comp"]
    shade["mutates"]["script_spans"] = ["build/units/02/shade.py"]
    claim = shade["evaluation"]["claims"][0]
    claim["id"] = "shade-claim"
    claim["repair_owner"] = "shade"
    claim["subject_roles"] = ["polish.shade", "polish.comp"]
    claim["subject_controls"] = []
    claim["property"] = "material_assignment_fraction"
    claim["asserts"] = "scene"
    claim["evidence"] = [{"kind": "scene_contract", "id": "shade-assigned"}]
    shade["evaluation"].pop("composition_context", None)
    shade_contract = {
        "id": "shade-assigned",
        "kind": "material_assignment_fraction",
        "owner_layer": "2",
        "fault_owner": "2",
        "activates_at": "2",
        "lifecycle": "layer",
        "axis": "final_lock",
        "roles": ["polish.comp"],
        "material_roles": ["polish.shade"],
        "op": "min",
        "lo": 1,
    }
    before = target.read_bytes()

    with pytest.raises(ValueError, match="same-layer dressing refused"):
        stage_materialization_unit(
            target,
            unit=shade,
            scene_contracts=[shade_contract],
            requirement_bindings=[],
        )

    assert target.read_bytes() == before
    with pytest.raises(ValueError, match=SAME_LAYER_DRESS_RULE[:48]):
        stage_materialization_unit(
            target,
            unit=shade,
            scene_contracts=[shade_contract],
            requirement_bindings=[],
        )


def test_unit_staging_refuses_generate_on_non_mesh_family_before_write(tmp_path: Path) -> None:
    """HIR-0162: generate is illegal on a look/control unit; candidate bytes stay put."""
    from vfx_harness.domain.construction import CONSTRUCTION_ROUTE_RULE
    from vfx_harness.orchestration.jit_materialization import (
        seed_materialization_candidate,
        stage_materialization_unit,
    )

    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    layout = run_artifacts.create(tmp_path, "generate-route-staging")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    full = json.loads(_jit_payload(tmp_path, bundle.content_hash).read_text(encoding="utf-8"))
    target = tmp_path / "generate-route.json"
    seed_materialization_candidate(
        bundle.root,
        target,
        layer_id="2",
        bundle_hash=bundle.content_hash,
        base_selection=_base_selection(tmp_path),
    )
    unit = json.loads(json.dumps(full["layer"]["stages"][0]))
    unit["construction"] = {"route": "generate", "witnesses": ["refobs-abc123"]}
    before = target.read_bytes()

    with pytest.raises(ValueError, match="construction route refused"):
        stage_materialization_unit(
            target,
            unit=unit,
            scene_contracts=full["scene_contracts"],
            requirement_bindings=[],
        )

    assert target.read_bytes() == before
    with pytest.raises(ValueError, match=CONSTRUCTION_ROUTE_RULE[:40]):
        stage_materialization_unit(
            target,
            unit=unit,
            scene_contracts=full["scene_contracts"],
            requirement_bindings=[],
            shot_folder=tmp_path,
        )


def _generate_mesh_unit(witness: str) -> tuple[dict, list[dict]]:
    unit = {
        "id": "prop_source",
        "title": "Prop source",
        "plan": "plans/02_polish/prop_source.md",
        "depends_on": [],
        "provides": ["geometry"],
        "look_capabilities": [],
        "construction": {"route": "generate", "witnesses": [witness]},
        "mutates": {
            "mode": "scoped",
            "roles": ["prop.shell"],
            "controls": [],
            "control_roles": {},
            "script_spans": ["build/units/02/prop_source.py"],
        },
        "protects": {
            "selector": "all_active_upstream_interfaces",
            "resolve_to_explicit_ids_at": "freeze",
        },
        "evaluation": {
            "primary_judge": 240,
            "judge": [
                {"frame": 239, "ref": "refs/a.png"},
                {"frame": 240, "ref": "refs/a.png"},
            ],
            "temporal_evidence": "none",
            "claims": [{
                "id": "prop-count",
                "proposition": "one source mesh exists",
                "axis": "final_lock",
                "property": "object_count",
                "subject_roles": ["prop.shell"],
                "subject_controls": [],
                "moments": [239, 240],
                "kind": "atomic",
                "required": True,
                "authority": "executable_required",
                "repair_owner": "prop_source",
                "asserts": "scene",
                "evidence": [
                    {"kind": "scene_contract", "id": "prop-count-239"},
                    {"kind": "scene_contract", "id": "prop-count-240"},
                ],
            }],
        },
        "completion": "all_required_claims_and_protected_contracts_pass",
    }
    contracts = [
        {
            "id": f"prop-count-{frame}",
            "kind": "object_count",
            "owner_layer": "2",
            "fault_owner": "2",
            "activates_at": "2",
            "lifecycle": "layer",
            "axis": "final_lock",
            "roles": ["prop.shell"],
            "frame": frame,
            "op": "eq",
            "value": 1,
        }
        for frame in (239, 240)
    ]
    return unit, contracts


def test_unit_staging_refuses_unregistered_generate_witness(tmp_path: Path) -> None:
    """HIR-0162: mesh generate still fails closed until mint_refobs persists the crop."""
    from vfx_harness.domain.refobs import UNREGISTERED_WITNESS_RULE
    from vfx_harness.orchestration.jit_materialization import (
        seed_materialization_candidate,
        stage_materialization_unit,
    )

    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    layout = run_artifacts.create(tmp_path, "unregistered-witness-staging")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    target = tmp_path / "generate-witness.json"
    seed_materialization_candidate(
        bundle.root,
        target,
        layer_id="2",
        bundle_hash=bundle.content_hash,
        base_selection=_base_selection(tmp_path),
    )
    unit, contracts = _generate_mesh_unit("refobs-notminted")
    before = target.read_bytes()

    with pytest.raises(ValueError, match="unregistered witnesses"):
        stage_materialization_unit(
            target,
            unit=unit,
            scene_contracts=contracts,
            requirement_bindings=[],
            shot_folder=tmp_path,
        )
    assert target.read_bytes() == before
    with pytest.raises(ValueError, match=UNREGISTERED_WITNESS_RULE[:24]):
        stage_materialization_unit(
            target,
            unit=unit,
            scene_contracts=contracts,
            requirement_bindings=[],
        )


def test_unit_staging_accepts_minted_generate_witness(tmp_path: Path) -> None:
    """HIR-0162: a minted refobs-* crop is the generate-construction witness."""
    from vfx_harness.orchestration.jit_materialization import (
        seed_materialization_candidate,
        stage_materialization_unit,
    )
    from vfx_harness.orchestration.refobs import mint_refobs

    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    still = tmp_path / "refs" / "a.png"
    Image.new("RGB", (64, 64), (40, 80, 120)).save(still)
    token = mint_refobs(tmp_path, still, [0.2, 0.2, 0.6, 0.55], source_rel="refs/a.png")
    layout = run_artifacts.create(tmp_path, "minted-witness-staging")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    target = tmp_path / "generate-minted.json"
    seed_materialization_candidate(
        bundle.root,
        target,
        layer_id="2",
        bundle_hash=bundle.content_hash,
        base_selection=_base_selection(tmp_path),
    )
    unit, contracts = _generate_mesh_unit(token)

    stage_materialization_unit(
        target,
        unit=unit,
        scene_contracts=contracts,
        requirement_bindings=[],
        shot_folder=tmp_path,
    )
    staged = json.loads(target.read_text(encoding="utf-8"))
    assert [row["id"] for row in staged["layer"]["stages"]] == ["prop_source"]
    assert staged["layer"]["stages"][0]["construction"]["witnesses"] == [token]


@pytest.mark.parametrize(
    "invalid_path",
    [
        "build/02_polish.py#polish",
        "build/02_polish.py",
        "build/units/02/not-polish.py",
    ],
)
def test_unit_staging_refuses_noncanonical_replay_path_before_write(
    tmp_path: Path, invalid_path: str
) -> None:
    """HIR-0126: a fragment string must never become a literal build filename."""
    from vfx_harness.orchestration.jit_materialization import (
        seed_materialization_candidate,
        stage_materialization_unit,
    )

    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    layout = run_artifacts.create(tmp_path, "script-path-staging")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    full = json.loads(_jit_payload(tmp_path, bundle.content_hash).read_text(encoding="utf-8"))
    target = tmp_path / "incremental.json"
    seed_materialization_candidate(
        bundle.root,
        target,
        layer_id="2",
        bundle_hash=bundle.content_hash,
        base_selection=_base_selection(tmp_path),
    )
    unit = full["layer"]["stages"][0]
    unit["mutates"]["script_spans"] = [invalid_path]
    before = target.read_bytes()

    with pytest.raises(ValueError, match=r"build/units/02/polish\.py"):
        stage_materialization_unit(
            target,
            unit=unit,
            scene_contracts=full["scene_contracts"],
            requirement_bindings=full["requirement_bindings"],
        )

    assert target.read_bytes() == before


def test_unstage_materialization_unit_prunes_only_unbound_candidate_rows(
    tmp_path: Path,
) -> None:
    from vfx_harness.orchestration.jit_materialization import (
        materialization_candidate_revision,
        seed_materialization_candidate,
        stage_materialization_unit,
        unstage_materialization_unit,
    )

    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    layout = run_artifacts.create(tmp_path, "unstage-materialization")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    full = json.loads(_jit_payload(tmp_path, bundle.content_hash).read_text(encoding="utf-8"))
    target = tmp_path / "unstage-materialization.json"
    seed_materialization_candidate(
        bundle.root,
        target,
        layer_id="2",
        bundle_hash=bundle.content_hash,
        base_selection=_base_selection(tmp_path),
    )
    stage_materialization_unit(
        target,
        unit=full["layer"]["stages"][0],
        scene_contracts=full["scene_contracts"],
        requirement_bindings=full["requirement_bindings"],
    )
    revision = materialization_candidate_revision(target)

    result = unstage_materialization_unit(
        target,
        unit_id="polish",
        expected_revision=revision,
    )

    payload = json.loads(target.read_text(encoding="utf-8"))
    assert result.unit_id == "polish"
    assert set(result.removed_contract_ids) == {"polish-lock", "vis-f239", "vis-f240"}
    assert result.removed_requirement_ids == ("R-final-lock",)
    assert payload["layer"]["stages"] == []
    assert payload["scene_contracts"] == []
    assert payload["requirement_bindings"] == []
    assert materialization_candidate_revision(target) != revision


def test_unstage_materialization_unit_refuses_surviving_dependant(
    tmp_path: Path,
) -> None:
    from vfx_harness.orchestration.jit_materialization import (
        seed_materialization_candidate,
        stage_materialization_unit,
        unstage_materialization_unit,
    )

    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    layout = run_artifacts.create(tmp_path, "unstage-dependant")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    full = json.loads(_jit_payload(tmp_path, bundle.content_hash).read_text(encoding="utf-8"))
    target = tmp_path / "unstage-dependant.json"
    seed_materialization_candidate(
        bundle.root,
        target,
        layer_id="2",
        bundle_hash=bundle.content_hash,
        base_selection=_base_selection(tmp_path),
    )
    unit = full["layer"]["stages"][0]
    stage_materialization_unit(
        target,
        unit=unit,
        scene_contracts=full["scene_contracts"],
        requirement_bindings=full["requirement_bindings"],
    )
    successor = json.loads(json.dumps(unit))
    successor["id"] = "successor"
    successor["plan"] = "plans/02_polish/successor.md"
    successor["depends_on"] = ["polish"]
    successor["mutates"]["roles"] = ["polish.successor"]
    successor["mutates"]["controls"] = ["next_hold"]
    successor["mutates"]["control_roles"] = {"next_hold": ["polish.successor"]}
    successor["mutates"]["script_spans"] = ["build/units/02/successor.py"]
    claim = successor["evaluation"]["claims"][0]
    claim["id"] = "successor-claim"
    claim["repair_owner"] = "successor"
    claim["subject_roles"] = ["polish.successor"]
    claim["subject_controls"] = ["next_hold"]
    claim["evidence"] = [{"kind": "scene_contract", "id": "successor-lock"}]
    successor["evaluation"].pop("composition_context", None)
    successor_contract = {
        **full["scene_contracts"][0],
        "id": "successor-lock",
    }
    stage_materialization_unit(
        target,
        unit=successor,
        scene_contracts=[successor_contract],
        requirement_bindings=[],
    )
    before = target.read_bytes()

    with pytest.raises(
        ValueError,
        match=r"remaining unit\(s\) successor depend on or consume it",
    ):
        unstage_materialization_unit(target, unit_id="polish")

    assert target.read_bytes() == before


def test_materialization_candidate_compare_and_swap_serializes_overlapping_writers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One observed revision can authorize only one overlapping candidate write."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event, Lock

    import vfx_harness.orchestration.jit_materialization as materialization
    from vfx_harness.orchestration.jit_materialization import (
        MaterializationRevisionConflict,
        apply_materialization_patches,
        materialization_candidate_revision,
    )

    candidate = tmp_path / "candidate.json"
    _write(candidate, {"product": "clay", "motion": "linear"})
    revision = materialization_candidate_revision(candidate)
    entered_write = Event()
    release_write = Event()
    calls_lock = Lock()
    write_calls = 0
    real_atomic_write = materialization.atomic_write

    def slow_first_write(path, content):
        nonlocal write_calls
        with calls_lock:
            write_calls += 1
            is_first = write_calls == 1
        if is_first:
            entered_write.set()
            assert release_write.wait(timeout=5)
        real_atomic_write(path, content)

    monkeypatch.setattr(materialization, "atomic_write", slow_first_write)
    monkeypatch.setattr(materialization, "inspect_materialization", lambda *a, **k: ([], None))

    def patch(pointer: str, value: str) -> list[str]:
        return apply_materialization_patches(
            tmp_path,
            candidate,
            ((pointer, value),),
            expected_bundle_hash="0" * 64,
            expected_revision=revision,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(patch, "/product", "metal")
        assert entered_write.wait(timeout=5)
        second = pool.submit(patch, "/motion", "bezier")
        assert not second.done()
        release_write.set()
        assert first.result(timeout=5) == []
        with pytest.raises(MaterializationRevisionConflict, match="revision changed"):
            second.result(timeout=5)

    assert json.loads(candidate.read_text(encoding="utf-8")) == {
        "product": "metal",
        "motion": "linear",
    }


def test_materialization_finalization_attestation_is_revision_bound(tmp_path: Path) -> None:
    from vfx_harness.domain.authority_state_records import (
        AuthorityStateRecordRef,
    )
    from vfx_harness.orchestration.authority_selection_transaction import (
        AuthoritySelectionToken,
    )
    from vfx_harness.orchestration.jit_materialization import (
        OVERLAY_ARTIFACTS,
        attest_materialization_finalization,
        materialization_finalization_attested,
    )

    candidate = tmp_path / "candidate.json"
    base_selection = AuthoritySelectionToken(
        plan_revision=1,
        plan_pointer_sha256="c" * 64,
        jit_revision=0,
        jit_pointer_sha256=None,
    )
    _write(candidate, {
        "schema": MATERIALIZATION_SCHEMA,
        "base_selection": base_selection.to_dict(),
        "value": 1,
    })
    bundle_hash = "a" * 64
    head_ref = AuthorityStateRecordRef.mint(
        locator=f"state/authority-state/objects/{'1' * 64}/record.json",
        sha256="1" * 64,
        record_schema="vfx-harness.authority-state-head/v1",
        record_digest="2" * 64,
    )

    attest_materialization_finalization(
        candidate,
        bundle_hash=bundle_hash,
        base_selection=base_selection,
        proposed_view_hash="d" * 64,
        proposed_artifact_hashes=dict.fromkeys(OVERLAY_ARTIFACTS, "e" * 64),
        planning_inputs_digest="f" * 64,
        consumer_marker_sha256="0" * 64,
        publication_jit_pointer_sha256="1" * 64,
        authority_transition_kind="noop",
        authority_transition_intent_ref=None,
        authority_capsule_set_digest="2" * 64,
        authority_effects_digest=None,
        authority_state_head_ref=head_ref,
        before_state_hashes={},
        after_state_hashes={},
    )

    assert materialization_finalization_attested(
        candidate, bundle_hash=bundle_hash
    )
    finalization = candidate.with_name(candidate.name + ".finalization.json")
    finalization.write_text(
        json.dumps(json.loads(finalization.read_text(encoding="utf-8"))) + "\n",
        encoding="utf-8",
    )
    assert not materialization_finalization_attested(
        candidate, bundle_hash=bundle_hash
    )
    attest_materialization_finalization(
        candidate,
        bundle_hash=bundle_hash,
        base_selection=base_selection,
        proposed_view_hash="d" * 64,
        proposed_artifact_hashes=dict.fromkeys(OVERLAY_ARTIFACTS, "e" * 64),
        planning_inputs_digest="f" * 64,
        consumer_marker_sha256="0" * 64,
        publication_jit_pointer_sha256="1" * 64,
        authority_transition_kind="noop",
        authority_transition_intent_ref=None,
        authority_capsule_set_digest="2" * 64,
        authority_effects_digest=None,
        authority_state_head_ref=head_ref,
        before_state_hashes={},
        after_state_hashes={},
    )
    assert not materialization_finalization_attested(
        candidate, bundle_hash="b" * 64
    )
    _write(candidate, {
        "schema": MATERIALIZATION_SCHEMA,
        "base_selection": base_selection.to_dict(),
        "value": 2,
    })
    assert not materialization_finalization_attested(
        candidate, bundle_hash=bundle_hash
    )


def test_patch_cannot_insert_or_pad_a_staged_unit(
    tmp_path: Path,
) -> None:
    from vfx_harness.orchestration.jit_materialization import (
        apply_materialization_patches,
        seed_materialization_candidate,
        stage_materialization_unit,
    )

    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    layout = run_artifacts.create(tmp_path, "patch-stage-boundary")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    full = json.loads(_jit_payload(tmp_path, bundle.content_hash).read_text(encoding="utf-8"))
    target = tmp_path / "patch-stage-boundary.json"
    seed_materialization_candidate(
        bundle.root,
        target,
        layer_id="2",
        bundle_hash=bundle.content_hash,
        base_selection=_base_selection(tmp_path),
    )
    before = target.read_bytes()
    with pytest.raises(ValueError, match="add each unit through stage_materialization_unit"):
        apply_materialization_patches(
            bundle.root,
            target,
            (("/layer/stages/-", full["layer"]["stages"][0]),),
            expected_bundle_hash=bundle.content_hash,
        )
    assert target.read_bytes() == before

    stage_materialization_unit(
        target,
        unit=full["layer"]["stages"][0],
        scene_contracts=full["scene_contracts"],
        requirement_bindings=full["requirement_bindings"],
    )
    before = target.read_bytes()
    with pytest.raises(ValueError, match=r"atomicity refused before candidate write.*padding"):
        apply_materialization_patches(
            bundle.root,
            target,
            (("/layer/stages/0/family", "mesh"),),
            expected_bundle_hash=bundle.content_hash,
        )
    assert target.read_bytes() == before


def test_materialization_refuses_mixed_unit_before_it_enters_staged_scratch(
    tmp_path: Path,
) -> None:
    from vfx_harness.orchestration.jit_materialization import (
        seed_materialization_candidate,
        stage_materialization_unit,
    )

    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    layout = run_artifacts.create(tmp_path, "staging-atomicity")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    full = json.loads(_jit_payload(tmp_path, bundle.content_hash).read_text(encoding="utf-8"))
    target = tmp_path / "mixed-incremental.json"
    seed_materialization_candidate(
        bundle.root,
        target,
        layer_id="2",
        bundle_hash=bundle.content_hash,
        base_selection=_base_selection(tmp_path),
    )
    unit = full["layer"]["stages"][0]
    unit["provides"] = ["camera"]
    unit["evaluation"]["claims"][0]["evidence"].append({
        "kind": "scene_contract",
        "id": "camera-motion",
    })
    unit["evaluation"]["claims"][0]["evidence"].append({
        "kind": "scene_contract",
        "id": "fill-energy",
    })
    contracts = [
        *full["scene_contracts"],
        {
            "id": "camera-motion",
            "kind": "keyframe_schedule",
            "owner_layer": "2",
            "fault_owner": "2",
            "activates_at": "2",
            "lifecycle": "layer",
            "axis": "final_lock",
            "roles": ["polish.comp"],
            "samples": [
                {"frame": 239, "values": {"location": [0, 0, 0]}},
                {"frame": 240, "values": {"location": [0, 0, 1]}},
            ],
            "op": "max",
            "hi": 0.01,
        },
        {
            "id": "fill-energy",
            "kind": "object_property",
            "owner_layer": "2",
            "fault_owner": "2",
            "activates_at": "2",
            "lifecycle": "layer",
            "axis": "final_lock",
            "roles": ["polish.comp"],
            "frame": 239,
            "property": "data.energy",
            "op": "eq",
            "value": 50,
            "tol": 0.1,
        },
    ]
    before = target.read_bytes()

    with pytest.raises(ValueError, match=r"atomicity refused before candidate write.*mixed_clusters"):
        stage_materialization_unit(
            target,
            unit=unit,
            scene_contracts=contracts,
            requirement_bindings=full["requirement_bindings"],
        )

    assert target.read_bytes() == before


def test_schema_five_global_publication_rejects_ready_preproduction(tmp_path: Path) -> None:
    from vfx_harness.evaluation.plan_gate import _check_contracts

    _candidate(tmp_path)
    document = json.loads((tmp_path / "layers.json").read_text(encoding="utf-8"))
    document["schema"] = 5
    _write(tmp_path / "layers.json", document)

    findings, _ = _check_contracts(tmp_path)

    assert any(
        finding.check == "global-preproduction" and "ready layers: 1" in finding.what
        for finding in findings
    )


def _pin_materialized_view(root: Path, layer_ids: list[str]) -> None:
    """Fabricate the verified JIT view pointer declaring `layer_ids` materialized,
    pinning the CURRENT bytes of the staged overlay files (as publish_materialization
    would have)."""
    import hashlib as _hashlib

    from vfx_harness.orchestration.authority_selection_transaction import (
        AuthoritySelectionToken,
    )
    from vfx_harness.orchestration.jit_materialization.view_pointer import (
        JitViewPointer,
    )
    from vfx_harness.orchestration.plan_consumer_view import PlanConsumerViewMarker

    pointer_dir = root / "state" / "jit-layers"
    pointer_dir.mkdir(parents=True, exist_ok=True)
    documents = {}
    hashes = {}
    names = (
        "layers.json",
        "scene_checks.json",
        "checks.json",
        "requirements.json",
        "acceptance.json",
    )
    for name in names:
        path = root / name
        documents[name] = json.loads(path.read_text(encoding="utf-8"))
        hashes[name] = _hashlib.sha256(path.read_bytes()).hexdigest()
    view_hash = _hashlib.sha256(
        json.dumps(documents, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    pointer_path = pointer_dir / "current.json"
    revision = 1
    plan_revision = 1
    if pointer_path.is_file():
        prior = json.loads(pointer_path.read_text(encoding="utf-8"))
        revision = int(prior["revision"]) + 1
        plan_revision = int(prior["plan_revision"])
    pointer = JitViewPointer(
        revision=revision,
        plan_revision=plan_revision,
        bundle_hash="a" * 64,
        view_hash=view_hash,
        materialized_layers=tuple(str(layer_id) for layer_id in layer_ids),
        artifacts={name: name for name in names},
        hashes=hashes,
    )
    _write_authority_record(pointer_path, pointer.as_dict())
    marker_path = root / ".plan-consumer-view.json"
    if marker_path.is_file():
        claimed = json.loads(marker_path.read_text(encoding="utf-8"))
        bundle_hash = str(claimed.get("content_hash") or "")
        marker = PlanConsumerViewMarker(
            shot=root,
            bundle=(
                root
                / "runs"
                / "fixture-plan"
                / "checkpoints"
                / "plans"
                / "bundles"
                / bundle_hash
            ),
            content_hash=bundle_hash,
            base_selection=AuthoritySelectionToken(
                plan_revision=plan_revision,
                plan_pointer_sha256=_hashlib.sha256(b"fixture plan pointer").hexdigest(),
                jit_revision=revision - 1,
                jit_pointer_sha256=(
                    None
                    if revision == 1
                    else _hashlib.sha256(b"fixture prior JIT pointer").hexdigest()
                ),
            ),
            view_source="jit",
            view_digest=view_hash,
            artifact_hashes=hashes,
            authored_inputs={
                "brief.md": _hashlib.sha256(b"fixture brief").hexdigest()
            },
            decision_inputs={},
        )
        _write_authority_record(marker_path, marker.to_dict())


def test_materialized_ready_layer_is_not_preproduction_debt(tmp_path: Path) -> None:
    """Run 20260824T153427Z-91b7c1: layer 1 materialized legitimately and the
    pre-materialization rule then blocked every unit plan of the generation — a ready
    layer declared by the hash-pinned view IS the designed post-materialization shape.
    Any integrity break falls back to the strict reading."""
    from vfx_harness.evaluation.plan_gate import _check_contracts

    _candidate(tmp_path)
    document = json.loads((tmp_path / "layers.json").read_text(encoding="utf-8"))
    document["schema"] = 5
    _write(tmp_path / "layers.json", document)
    _pin_materialized_view(tmp_path, ["1"])

    findings, _ = _check_contracts(tmp_path)
    assert not any(
        finding.check == "global-preproduction" and "ready layers" in finding.what
        for finding in findings
    )

    # staged bytes no longer match the pinned view → no exemption
    document["tampered"] = True
    _write(tmp_path / "layers.json", document)
    findings, _ = _check_contracts(tmp_path)
    assert any(
        finding.check == "global-preproduction" and "ready layers: 1" in finding.what
        for finding in findings
    )


def test_materialized_decision_adoption_satisfies_reservation(tmp_path: Path) -> None:
    """After the reserving layer materializes it is no longer deferred; the obligation
    transfers to the adoption itself — a pinned materialized contract carrying the
    decision_id with the exact approved values."""
    _candidate(tmp_path)
    expected = _structured_camera_decision(tmp_path)
    _all_deferred_schema5(tmp_path, reserved=["iris.*"])  # nobody reserves cam_rig
    layers = json.loads((tmp_path / "layers.json").read_text(encoding="utf-8"))
    layers["layers"][0]["execution"] = "ready"
    _write(tmp_path / "layers.json", layers)
    _write(tmp_path / "scene_checks.json", {
        "schema": 2,
        "contracts": [{
            **expected,
            "id": "cam-a2-adopted",
            "decision_id": "A-camera",
            "owner_layer": "1",
            "fault_owner": "1",
            "activates_at": "1",
            "lifecycle": "layer",
            "axis": "camera_framing",
        }],
    })
    _pin_materialized_view(tmp_path, ["1"])

    findings, _ = _check_meta_records(tmp_path)
    assert not any(finding.check == "decision-adoption" for finding in findings)

    # altered adoption values are still a violation
    contracts = json.loads((tmp_path / "scene_checks.json").read_text(encoding="utf-8"))
    contracts["contracts"][0]["hi"] = 999
    _write(tmp_path / "scene_checks.json", contracts)
    _pin_materialized_view(tmp_path, ["1"])
    findings, _ = _check_meta_records(tmp_path)
    assert any(finding.blocking and finding.check == "decision-adoption" for finding in findings)


@pytest.mark.parametrize(
    ("title", "reserved_role", "axis"),
    [
        ("Product foundation", "product.view_rig", "product_shape"),
        ("Camera foundation", "camera.*", "camera_framing"),
    ],
)
def test_unit_first_global_bundle_is_clean_for_heterogeneous_roots(
    tmp_path: Path, title: str, reserved_role: str, axis: str
) -> None:
    from vfx_harness.evaluation.plan_gate import run

    brief = (
        "---\n"
        "id: heterogeneous-root\n"
        "frames: 1\n"
        "fps: 24\n"
        "---\n"
        "The delivered image must preserve the approved visual target.\n"
    )
    (tmp_path / "brief.md").write_text(brief, encoding="utf-8")
    (tmp_path / "refs").mkdir()
    (tmp_path / "refs" / "target.png").write_bytes(b"fixture")
    (tmp_path / "plans").mkdir()
    (tmp_path / "plans" / "global.md").write_text(
        f"# Unit-first publication\n\n1. {title}: owns `{axis}`.\n", encoding="utf-8"
    )
    _write(tmp_path / "layers.json", {
        "schema": 5,
        "layers": [{
            "id": "1", "script": "build/01_foundation.py", "title": title,
            "primary_judge": 1,
            "judge": [{"frame": 1, "ref": "refs/target.png"}],
            "owns": [axis], "reads": "approved target", "evidence_domains": ["image"],
            "execution": "jit_deferred", "stages": [],
            "jit": {
                "depends_on_layers": [], "required_outcomes": [],
                "provides": {"camera": [reserved_role]},
                "reserved_roles": [reserved_role],
                "owned_requirements": ["R1"],
            },
        }],
    })
    _write(tmp_path / "acceptance.json", [])
    _write(tmp_path / "critic_axes.json", [{"key": axis, "desc": "approved visual target"}])
    _write(tmp_path / "checks.json", {"schema": 2, "checks": []})
    _write(tmp_path / "scene_checks.json", {"schema": 2, "contracts": []})
    digest = hashlib.sha256((tmp_path / "brief.md").read_bytes()).hexdigest()
    _write(tmp_path / "requirements.json", {
        "schema": "vfx-harness.requirements/v2",
        "judgment_debt_definitions": [],
        "judgment_debt_activations": [],
        "requirements": [{
            "id": "R1", "statement": "The delivered image must preserve the approved visual target.",
            "citation": {
                "source": "brief.md", "sha256": digest, "line_start": 6, "line_end": 6,
            },
            "resolution": _deferred_owner("1", "image"),
        }],
    })
    _write(tmp_path / "obligations.json", {
        "schema": "vfx-harness.obligations/v1", "obligations": [],
    })
    _write(tmp_path / "assumptions.json", {
        "schema": "vfx-harness.assumptions/v1", "assumptions": [],
    })

    result = run(tmp_path)

    assert result.blocking == []


def test_jit_materialization_rejects_candidate_sensitive_image_contracts(tmp_path: Path) -> None:
    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    layout = run_artifacts.create(tmp_path, "plan-run")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    payload = _jit_payload(tmp_path, bundle.content_hash)
    document = json.loads(payload.read_text(encoding="utf-8"))
    document["image_contracts"] = [{"id": "premature-image-check", "owner_layer": "2"}]
    _write(payload, document)

    with pytest.raises(ValueError, match="candidate-sensitive image checks"):
        validate_materialization(
            bundle.root, payload, expected_bundle_hash=bundle.content_hash
        )


def test_materialized_unit_cannot_invent_global_camera_capability(tmp_path: Path) -> None:
    """A geometry layer cannot satisfy camera bootstrap with an authored label."""
    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    layout = run_artifacts.create(tmp_path, "camera-capability-scope")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    payload = _jit_payload(tmp_path, bundle.content_hash)
    document = json.loads(payload.read_text(encoding="utf-8"))
    document["layer"]["stages"][0]["provides"] = ["camera"]
    _write(payload, document)

    with pytest.raises(ValueError, match=r"did not reserve it in jit\.provides"):
        validate_materialization(
            bundle.root, payload, expected_bundle_hash=bundle.content_hash
        )


def test_materialization_must_fulfill_global_camera_capability(tmp_path: Path) -> None:
    """The reverse edge is also closed: a global promise needs a producing unit."""
    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    layers = json.loads((tmp_path / "layers.json").read_text(encoding="utf-8"))
    layers["layers"][1]["jit"]["provides"] = {"camera": ["polish.*"]}
    _write(tmp_path / "layers.json", layers)
    layout = run_artifacts.create(tmp_path, "camera-capability-fulfillment")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    payload = _jit_payload(tmp_path, bundle.content_hash)

    with pytest.raises(ValueError, match=r"does not fulfill globally declared.*camera"):
        validate_materialization(
            bundle.root, payload, expected_bundle_hash=bundle.content_hash
        )


@pytest.mark.parametrize("kind", ["bbox_center_x", "visible_fraction"])
def test_control_host_unit_cannot_bind_surface_metric_on_its_mutated_role(
    tmp_path: Path, kind: str
) -> None:
    """Transform-only units cannot turn a control point into proxy mesh to pass."""
    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    layout = run_artifacts.create(tmp_path, f"surface-control-{kind}")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    payload = _jit_payload(tmp_path, bundle.content_hash)
    document = json.loads(payload.read_text(encoding="utf-8"))
    unit = document["layer"]["stages"][0]
    contract = {
        "id": "control-surface", "kind": kind, "owner_layer": "2",
        "fault_owner": "2", "activates_at": "2", "lifecycle": "layer",
        "axis": "final_lock", "roles": ["polish.comp"], "frame": 239,
        "op": "min", "lo": 0.25,
    }
    unit["evaluation"]["claims"] = [{
        "id": "control-placement", "proposition": "the control point is placed",
        "axis": "final_lock", "property": kind,
        "subject_roles": ["polish.comp"], "subject_controls": ["hold"],
        "moments": [239, 240], "kind": "atomic", "required": True,
        "authority": "executable_required", "repair_owner": "polish",
        "asserts": "projected_composition",
        "evidence": [{"kind": "scene_contract", "id": "control-surface"}],
    }]
    unit["evaluation"]["composition_context"] = {
        "frames": [239, 240], "contract_ids": ["control-surface"],
    }
    document["scene_contracts"] = [contract]
    document["requirement_bindings"] = [{
        "requirement_id": "R-final-lock", "contract_ids": ["control-surface"],
    }]
    _write(payload, document)

    with pytest.raises(ValueError, match=r"surface metric.*does not provide geometry"):
        validate_materialization(
            bundle.root, payload, expected_bundle_hash=bundle.content_hash
        )


def test_control_producer_cannot_own_camera_projection_repair(tmp_path: Path) -> None:
    from vfx_harness.orchestration.jit_materialization import (
        seed_materialization_candidate,
        stage_materialization_unit,
    )

    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    layout = run_artifacts.create(tmp_path, "point-projection-owner")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    payload = _jit_payload(tmp_path, bundle.content_hash)
    document = json.loads(payload.read_text(encoding="utf-8"))
    unit = document["layer"]["stages"][0]
    unit["evaluation"]["temporal_evidence"] = "none"
    unit["evaluation"]["claims"] = [{
        "id": "self-fitted-point",
        "proposition": "the control point is centered",
        "axis": "final_lock",
        "property": "projected_origin_x",
        "subject_roles": ["polish.comp"],
        "subject_controls": ["hold"],
        "moments": [239, 240],
        "kind": "atomic",
        "required": True,
        "authority": "executable_required",
        "repair_owner": "polish",
        "asserts": "projected_composition",
        "evidence": [
            {"kind": "scene_contract", "id": "point-x-f239", "moments": [239]},
            {"kind": "scene_contract", "id": "point-x-f240", "moments": [240]},
        ],
    }]
    unit["evaluation"]["composition_context"] = {
        "frames": [239, 240],
        "contract_ids": ["point-x-f239", "point-x-f240"],
    }
    document["scene_contracts"] = [{
        "id": f"point-x-f{frame}",
        "kind": "projected_origin_x",
        "owner_layer": "2",
        "fault_owner": "2",
        "activates_at": "2",
        "lifecycle": "layer",
        "axis": "final_lock",
        "roles": ["polish.comp"],
        "frame": frame,
        "op": "band",
        "lo": 0.45,
        "hi": 0.55,
    } for frame in (239, 240)]
    document["requirement_bindings"] = [{
        "requirement_id": "R-final-lock",
        "contract_ids": ["point-x-f239", "point-x-f240"],
    }]
    _write(payload, document)

    staged = tmp_path / "point-owner-staged.json"
    seed_materialization_candidate(
        bundle.root,
        staged,
        layer_id="2",
        bundle_hash=bundle.content_hash,
        base_selection=_base_selection(tmp_path),
    )
    before = staged.read_bytes()
    with pytest.raises(ValueError, match="point-projection ownership refused before candidate write"):
        stage_materialization_unit(
            staged,
            unit=unit,
            scene_contracts=document["scene_contracts"],
            requirement_bindings=document["requirement_bindings"],
        )
    assert staged.read_bytes() == before

    with pytest.raises(ValueError, match=r"repair_owner polish does not provide camera"):
        validate_materialization(
            bundle.root, payload, expected_bundle_hash=bundle.content_hash
        )


@pytest.mark.parametrize("role", ["product.camera_target", "motion.aim_control"])
def test_control_host_unit_publishes_with_point_projection_and_no_visibility_proxy(
    tmp_path: Path, role: str
) -> None:
    """A fixed control publishes first; its camera successor owns projection."""
    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    requirements = json.loads((tmp_path / "requirements.json").read_text(encoding="utf-8"))
    requirements["requirements"][0]["resolution"] = _deferred_owner(
        "2", "scene", "projected_composition"
    )
    _write(tmp_path / "requirements.json", requirements)
    layers = json.loads((tmp_path / "layers.json").read_text(encoding="utf-8"))
    layers["layers"][1]["jit"]["reserved_roles"] = [role, "camera.rig"]
    layers["layers"][1]["jit"]["provides"] = {"camera": ["camera.rig"]}
    _write(tmp_path / "layers.json", layers)
    layout = run_artifacts.create(tmp_path, f"point-control-{role.replace('.', '-')}")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    payload = _jit_payload(tmp_path, bundle.content_hash)
    document = json.loads(payload.read_text(encoding="utf-8"))
    target = document["layer"]["stages"][0]
    target["id"] = "target"
    target["plan"] = "plans/units/target.md"
    target["mutates"]["roles"] = [role]
    target["mutates"]["control_roles"] = {"hold": [role]}
    target["mutates"]["script_spans"] = ["build/units/02/target.py"]
    target["evaluation"]["temporal_evidence"] = "none"
    target["evaluation"]["claims"] = [{
        "id": "control-fixed",
        "proposition": "the fixed control point exists without rendered geometry",
        "axis": "final_lock",
        "property": "object_count",
        "subject_roles": [role],
        "subject_controls": ["hold"],
        "moments": [239, 240],
        "kind": "atomic",
        "required": True,
        "authority": "executable_required",
        "repair_owner": "target",
        "asserts": "scene",
        "evidence": [{"kind": "scene_contract", "id": "target-count"}],
    }]
    target["evaluation"]["composition_context"] = {
        "frames": [239, 240],
        "contract_ids": ["target-count"],
    }
    camera = {
        "id": "camera",
        "title": "Camera alignment",
        "plan": "plans/units/camera.md",
        "depends_on": ["target"],
        "consumes": [{
            "producer": "target",
            "interface_id": "target.publish",
            "kind": "placement_control",
        }],
        "mutates": {
            "mode": "scoped",
            "roles": ["camera.rig"],
            "controls": [],
            "control_roles": {},
            "script_spans": ["build/units/02/camera.py"],
        },
        "protects": {
            "selector": "all_active_upstream_interfaces",
            "resolve_to_explicit_ids_at": "freeze",
        },
        "look_capabilities": [],
        "provides": ["camera"],
        "evaluation": {
            "primary_judge": 240,
            "judge": document["layer"]["judge"],
            "temporal_evidence": "none",
            "claims": [{
                "id": "camera-aligns-control",
                "proposition": "the camera keeps the fixed control on the declared target",
                "axis": "final_lock",
                "property": "projected_origin",
                "subject_roles": [role, "camera.rig"],
                "subject_controls": [],
                "moments": [239, 240],
                "kind": "atomic",
                "required": True,
                "authority": "executable_required",
                "repair_owner": "camera",
                "asserts": "projected_composition",
                "evidence": [
                    {"kind": "scene_contract", "id": "point-x-f239", "moments": [239]},
                    {"kind": "scene_contract", "id": "point-x-f240", "moments": [240]},
                ],
            }],
            "composition_context": {
                "frames": [239, 240],
                "contract_ids": ["point-x-f239", "point-x-f240"],
            },
        },
        "completion": "all_required_claims_and_protected_contracts_pass",
    }
    document["layer"]["stages"] = [target, camera]
    document["scene_contracts"] = [{
        "id": "target-count",
        "kind": "object_count",
        "owner_layer": "2",
        "fault_owner": "2",
        "activates_at": "2",
        "lifecycle": "layer",
        "axis": "final_lock",
        "roles": [role],
        "op": "eq",
        "value": 1,
    }, *[
        {
            "id": f"point-x-f{frame}",
            "kind": "projected_origin_x",
            "owner_layer": "2",
            "fault_owner": "2",
            "activates_at": "2",
            "lifecycle": "layer",
            "axis": "final_lock",
            "roles": [role],
            "frame": frame,
            "op": "band",
            "lo": 0.45,
            "hi": 0.55,
        }
        for frame in (239, 240)
    ]]
    document["requirement_bindings"] = [{
        "requirement_id": "R-final-lock",
        "contract_ids": ["target-count", "point-x-f239", "point-x-f240"],
    }]
    _write(payload, document)

    materialized = validate_materialization(
        bundle.root, payload, expected_bundle_hash=bundle.content_hash
    )

    assert [unit.id for unit in materialized.layer.stages] == ["target", "camera"]
    assert all(row["kind"] != "visible_fraction" for row in materialized.scene_contracts)

    from vfx_harness.orchestration.jit_materialization import (
        seed_materialization_candidate,
        stage_materialization_unit,
    )

    staged = tmp_path / f"point-interface-{role.replace('.', '-')}.json"
    seed_materialization_candidate(
        bundle.root,
        staged,
        layer_id="2",
        bundle_hash=bundle.content_hash,
        base_selection=_base_selection(tmp_path),
    )
    stage_materialization_unit(
        staged,
        unit=target,
        scene_contracts=[document["scene_contracts"][0]],
        requirement_bindings=[],
    )
    camera_without_interface = json.loads(json.dumps(camera))
    camera_without_interface.pop("consumes")
    before = staged.read_bytes()
    with pytest.raises(ValueError, match="point-projection interface refused before candidate write"):
        stage_materialization_unit(
            staged,
            unit=camera_without_interface,
            scene_contracts=document["scene_contracts"][1:],
            requirement_bindings=document["requirement_bindings"],
        )
    assert staged.read_bytes() == before

    document["layer"]["stages"][1].pop("consumes")
    _write(payload, document)
    with pytest.raises(ValueError, match="consumes no compatible typed interface"):
        validate_materialization(
            bundle.root, payload, expected_bundle_hash=bundle.content_hash
        )


def test_camera_unit_must_mutate_the_globally_bound_interface_role(tmp_path: Path) -> None:
    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    layers = json.loads((tmp_path / "layers.json").read_text(encoding="utf-8"))
    layers["layers"][1]["jit"]["reserved_roles"].append("camera.*")
    layers["layers"][1]["jit"]["provides"] = {"camera": ["camera.*"]}
    _write(tmp_path / "layers.json", layers)
    layout = run_artifacts.create(tmp_path, "camera-interface-role")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    payload = _jit_payload(tmp_path, bundle.content_hash)
    document = json.loads(payload.read_text(encoding="utf-8"))
    document["layer"]["stages"][0]["provides"] = ["camera"]
    _write(payload, document)

    with pytest.raises(ValueError, match="without mutating any globally reserved camera"):
        validate_materialization(
            bundle.root, payload, expected_bundle_hash=bundle.content_hash
        )


def test_materialization_rejects_uncovered_unit_judge_frame(tmp_path: Path) -> None:
    """HIR-0045: atmosphere judged f150 with claims only at f72."""
    from vfx_harness.domain.work_units import UNIT_JUDGE_CLAIM_COVERAGE_RULE

    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    layout = run_artifacts.create(tmp_path, "uncovered-judge")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    payload = _jit_payload(tmp_path, bundle.content_hash)
    document = json.loads(payload.read_text(encoding="utf-8"))
    document["layer"]["stages"][0]["evaluation"]["claims"][0]["moments"] = [239]
    _write(payload, document)

    findings, materialized = inspect_materialization(
        bundle.root, payload, expected_bundle_hash=bundle.content_hash
    )
    assert materialized is None
    text = "\n".join(findings)
    assert "/layer/stages/0/evaluation/judge:" in text
    assert "no required claim moment" in text
    assert "240" in text
    assert UNIT_JUDGE_CLAIM_COVERAGE_RULE in text


def test_materialization_rejects_look_without_image_domain(tmp_path: Path) -> None:
    """HIR-0046: look-owning polish with only a scene binding cannot publish."""
    from vfx_harness.domain.work_units import LOOK_REQUIRES_IMAGE_DOMAIN_RULE

    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    layout = run_artifacts.create(tmp_path, "look-without-image")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    payload = _jit_payload(tmp_path, bundle.content_hash)
    document = json.loads(payload.read_text(encoding="utf-8"))
    document["layer"]["stages"][0]["look_capabilities"] = ["color"]
    _write(payload, document)

    findings, materialized = inspect_materialization(
        bundle.root, payload, expected_bundle_hash=bundle.content_hash
    )
    assert materialized is None
    text = "\n".join(findings)
    assert "/layer/stages/0/look_capabilities:" in text
    assert "no required image-domain claim" in text
    assert LOOK_REQUIRES_IMAGE_DOMAIN_RULE in text


def test_materialization_rejects_image_debt_before_optical_signal(tmp_path: Path) -> None:
    """HIR-0110: a mesh-only beauty owner cannot publish ahead of illumination."""
    from vfx_harness.domain.image_signal import IMAGE_SIGNAL_DEPENDENCY_RULE

    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    layout = run_artifacts.create(tmp_path, "image-signal-bootstrap")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    payload = _jit_payload(tmp_path, bundle.content_hash)
    document = json.loads(payload.read_text(encoding="utf-8"))
    unit = document["layer"]["stages"][0]
    unit["provides"] = ["geometry"]
    unit["look_capabilities"] = ["material"]
    unit["evaluation"]["claims"][0].update(
        {
            "property": "frame_delta",
            "asserts": "image",
            "evidence": [
                {
                    "kind": "image_contract",
                    "id": "polish-beauty",
                    "moments": [239, 240],
                }
            ],
        }
    )
    _write(payload, document)

    findings, materialized = inspect_materialization(
        bundle.root, payload, expected_bundle_hash=bundle.content_hash
    )

    assert materialized is None
    text = "\n".join(findings)
    assert "/layer/stages/0/depends_on:" in text
    assert "polish-beauty" in text
    assert "No same-layer unit currently derives a signal family" in text
    assert "object_property(property=data.energy)" in text
    assert "declare look_capabilities []" in text
    assert "defaults to Workbench solid" in text
    assert IMAGE_SIGNAL_DEPENDENCY_RULE in text


def test_materialization_rejects_image_debt_before_rendered_carrier(tmp_path: Path) -> None:
    """HIR-0160: a shading-only beauty owner cannot publish ahead of mesh."""
    from vfx_harness.domain.image_signal import IMAGE_SUBJECT_DEPENDENCY_RULE

    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    layout = run_artifacts.create(tmp_path, "image-subject-bootstrap")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    payload = _jit_payload(tmp_path, bundle.content_hash)
    document = json.loads(payload.read_text(encoding="utf-8"))
    unit = document["layer"]["stages"][0]
    unit["look_capabilities"] = ["material"]
    unit["evaluation"]["claims"][0].update(
        {
            "property": "frame_delta",
            "asserts": "image",
            "evidence": [
                {
                    "kind": "image_contract",
                    "id": "polish-beauty",
                    "moments": [239, 240],
                }
            ],
        }
    )
    document["scene_contracts"] = [
        {
            "id": "polish-lock",
            "kind": "material_assignment_fraction",
            "owner_layer": "2",
            "fault_owner": "2",
            "activates_at": "2",
            "lifecycle": "layer",
            "axis": "final_lock",
            "roles": ["polish.comp"],
            "material_roles": ["polish.comp"],
            "op": "min",
            "lo": 1,
        },
        *_vis_rows("2", (239, 240)),
    ]
    _write(payload, document)

    findings, materialized = inspect_materialization(
        bundle.root, payload, expected_bundle_hash=bundle.content_hash
    )

    assert materialized is None
    text = "\n".join(findings)
    assert "/layer/stages/0/depends_on:" in text
    assert "polish-beauty" in text
    assert "before a rendered carrier is available" in text
    assert IMAGE_SUBJECT_DEPENDENCY_RULE in text


def test_materialization_rejects_same_layer_dressing(tmp_path: Path) -> None:
    """HIR-0161: this layer's dressable grant cannot authorize a sibling dresser."""
    from vfx_harness.domain.dressing import SAME_LAYER_DRESS_RULE

    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    layout = run_artifacts.create(tmp_path, "same-layer-dress-validate")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    payload = _jit_payload(tmp_path, bundle.content_hash)
    document = json.loads(payload.read_text(encoding="utf-8"))
    polish = document["layer"]["stages"][0]
    mass = json.loads(json.dumps(polish))
    mass.update(
        {
            "id": "polish_mass",
            "title": "Polish mass",
            "plan": "plans/02_polish/polish_mass.md",
            "depends_on": [],
            "provides": ["geometry"],
            "look_capabilities": [],
        }
    )
    mass["mutates"] = {
        "mode": "scoped",
        "roles": ["polish.mass"],
        "controls": [],
        "control_roles": {},
        "script_spans": ["build/units/02/polish_mass.py"],
    }
    mass["evaluation"]["temporal_evidence"] = "none"
    mass["evaluation"]["claims"] = [
        {
            "id": "polish-mass-mesh",
            "proposition": "The polished mass has authored polygons.",
            "axis": "final_lock",
            "property": "mesh_vertex_count",
            "subject_roles": ["polish.mass"],
            "subject_controls": [],
            "moments": [239, 240],
            "kind": "atomic",
            "required": True,
            "authority": "executable_required",
            "repair_owner": "polish_mass",
            "asserts": "scene",
            "evidence": [
                {"kind": "scene_contract", "id": "polish-mass-mesh-239"},
                {"kind": "scene_contract", "id": "polish-mass-mesh-240"},
            ],
        }
    ]
    polish["depends_on"] = ["polish_mass"]
    polish["mutates"]["dresses"] = ["polish.mass"]
    polish["mutates"]["controls"] = []
    polish["mutates"]["control_roles"] = {}
    document["layer"]["stages"] = [mass, polish]
    document["layer"]["dressable"] = ["polish.mass"]
    document["scene_contracts"] = [
        {
            "id": "polish-lock",
            "kind": "material_assignment_fraction",
            "owner_layer": "2",
            "fault_owner": "2",
            "activates_at": "2",
            "lifecycle": "layer",
            "axis": "final_lock",
            "roles": ["polish.mass"],
            "material_roles": ["polish.comp"],
            "op": "min",
            "lo": 1,
        },
        {
            "id": "polish-mass-mesh-239",
            "kind": "mesh_vertex_count",
            "owner_layer": "2",
            "fault_owner": "2",
            "activates_at": "2",
            "lifecycle": "layer",
            "axis": "final_lock",
            "roles": ["polish.mass"],
            "frame": 239,
            "op": "min",
            "lo": 1,
        },
        {
            "id": "polish-mass-mesh-240",
            "kind": "mesh_vertex_count",
            "owner_layer": "2",
            "fault_owner": "2",
            "activates_at": "2",
            "lifecycle": "layer",
            "axis": "final_lock",
            "roles": ["polish.mass"],
            "frame": 240,
            "op": "min",
            "lo": 1,
        },
        *_vis_rows("2", (239, 240)),
    ]
    polish["evaluation"]["claims"][0].update(
        {
            "property": "material_assignment_fraction",
            "asserts": "scene",
            "subject_roles": ["polish.comp", "polish.mass"],
            "subject_controls": [],
            "evidence": [{"kind": "scene_contract", "id": "polish-lock"}],
        }
    )
    _write(payload, document)

    findings, materialized = inspect_materialization(
        bundle.root, payload, expected_bundle_hash=bundle.content_hash
    )

    assert materialized is None
    text = "\n".join(findings)
    assert "/layer/stages/1/mutates/dresses:" in text
    assert "same-layer mutation roles" in text
    assert "polish.mass" in text
    assert SAME_LAYER_DRESS_RULE in text


def test_materialization_rejects_generate_on_non_mesh_family(tmp_path: Path) -> None:
    """HIR-0162: collectable validation names construction on a non-mesh generate unit."""
    from vfx_harness.domain.construction import CONSTRUCTION_ROUTE_RULE

    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    layout = run_artifacts.create(tmp_path, "generate-route-validate")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    payload = _jit_payload(tmp_path, bundle.content_hash)
    document = json.loads(payload.read_text(encoding="utf-8"))
    document["layer"]["stages"][0]["construction"] = {
        "route": "generate",
        "witnesses": ["refobs-abc123"],
    }
    _write(payload, document)

    findings, materialized = inspect_materialization(
        bundle.root, payload, expected_bundle_hash=bundle.content_hash
    )

    assert materialized is None
    text = "\n".join(findings)
    assert "/layer/stages/0/construction:" in text
    assert "generate" in text
    assert "mesh" in text
    assert CONSTRUCTION_ROUTE_RULE in text


def test_materialization_requirement_binding_accepts_required_image_debt(
    tmp_path: Path,
) -> None:
    """HIR-0122: empty image_contracts is intentional, not missing requirement evidence."""
    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    layout = run_artifacts.create(tmp_path, "image-debt-requirement")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    payload = _jit_payload(tmp_path, bundle.content_hash)
    document = json.loads(payload.read_text(encoding="utf-8"))
    unit = document["layer"]["stages"][0]
    mass = json.loads(json.dumps(unit))
    mass.update({
        "id": "polish_mass",
        "title": "Polish mass",
        "plan": "plans/02_polish/polish_mass.md",
        "depends_on": [],
        "provides": ["geometry"],
        "look_capabilities": [],
    })
    mass["mutates"] = {
        "mode": "scoped",
        "roles": ["polish.mass"],
        "controls": [],
        "control_roles": {},
        "script_spans": ["build/units/02/polish_mass.py"],
    }
    mass["evaluation"]["temporal_evidence"] = "none"
    mass["evaluation"]["claims"] = [{
        "id": "polish-mass-mesh",
        "proposition": "The polished mass has authored polygons.",
        "axis": "final_lock",
        "property": "mesh_vertex_count",
        "subject_roles": ["polish.mass"],
        "subject_controls": [],
        "moments": [239, 240],
        "kind": "atomic",
        "required": True,
        "authority": "executable_required",
        "repair_owner": "polish_mass",
        "asserts": "scene",
        "evidence": [
            {"kind": "scene_contract", "id": "polish-mass-mesh-239"},
            {"kind": "scene_contract", "id": "polish-mass-mesh-240"},
        ],
    }]
    unit["depends_on"] = ["polish_mass"]
    unit["mutates"]["controls"] = []
    unit["mutates"]["control_roles"] = {}
    unit["look_capabilities"] = ["material"]
    unit["evaluation"]["temporal_evidence"] = "none"
    unit["evaluation"]["claims"] = [
        {
            "id": "polish-material",
            "proposition": "The polished surface has its authored material.",
            "axis": "final_lock",
            "property": "material_assignment_fraction",
            "subject_roles": ["polish.comp"],
            "subject_controls": [],
            "moments": [239],
            "kind": "atomic",
            "required": True,
            "authority": "executable_required",
            "repair_owner": "polish",
            "asserts": "scene",
            "evidence": [{"kind": "scene_contract", "id": "polish-material"}],
        },
        {
            "id": "polish-beauty",
            "proposition": "The final polish remains stable across the ending hold.",
            "axis": "final_lock",
            "property": "frame_delta",
            "subject_roles": ["polish.comp"],
            "subject_controls": [],
            "moments": [239, 240],
            "kind": "atomic",
            "required": True,
            "authority": "executable_required",
            "repair_owner": "polish",
            "asserts": "image",
            "evidence": [
                {
                    "kind": "image_contract",
                    "id": "polish-beauty-f239-f240",
                    "moments": [239, 240],
                }
            ],
        },
    ]
    document["scene_contracts"] = [
            {
                "id": "polish-mass-mesh-239",
                "kind": "mesh_vertex_count",
                "owner_layer": "2",
                "fault_owner": "2",
                "activates_at": "2",
                "lifecycle": "layer",
                "axis": "final_lock",
                "roles": ["polish.mass"],
                "frame": 239,
                "op": "min",
                "lo": 8,
            },
            {
                "id": "polish-mass-mesh-240",
                "kind": "mesh_vertex_count",
                "owner_layer": "2",
                "fault_owner": "2",
                "activates_at": "2",
                "lifecycle": "layer",
                "axis": "final_lock",
                "roles": ["polish.mass"],
                "frame": 240,
                "op": "min",
                "lo": 8,
            },
        {
            "id": "polish-material",
            "kind": "material_assignment_fraction",
            "owner_layer": "2",
            "fault_owner": "2",
            "activates_at": "2",
            "lifecycle": "layer",
            "axis": "final_lock",
            "roles": ["polish.comp"],
            "material_roles": ["polish.material"],
            "op": "min",
            "lo": 1,
        },
        *_vis_rows("2", (239, 240)),
    ]
    document["layer"]["stages"] = [mass, unit]
    document["image_contracts"] = []
    document["requirement_bindings"] = [
        {
            "requirement_id": "R-final-lock",
            "contract_ids": ["polish-beauty-f239-f240"],
        }
    ]
    _write(payload, document)

    findings, materialized = inspect_materialization(
        bundle.root, payload, expected_bundle_hash=bundle.content_hash
    )

    assert not findings
    assert materialized is not None
    assert materialized.image_contracts == ()


def test_materialization_requirement_binding_rejects_optional_image_reference(
    tmp_path: Path,
) -> None:
    """Only required image claims compile debts that can close owned requirements."""
    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    layout = run_artifacts.create(tmp_path, "optional-image-reference")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    payload = _jit_payload(tmp_path, bundle.content_hash)
    document = json.loads(payload.read_text(encoding="utf-8"))
    claim = document["layer"]["stages"][0]["evaluation"]["claims"][0]
    claim.update(
        {
            "required": False,
            "asserts": "image",
            "evidence": [
                {"kind": "image_contract", "id": "optional-look", "moments": [239, 240]}
            ],
        }
    )
    document["requirement_bindings"] = [
        {"requirement_id": "R-final-lock", "contract_ids": ["optional-look"]}
    ]
    _write(payload, document)

    findings, materialized = inspect_materialization(
        bundle.root, payload, expected_bundle_hash=bundle.content_hash
    )

    assert materialized is None
    assert any(
        "requirement R-final-lock names absent contracts: optional-look" in finding
        for finding in findings
    )


def test_materialization_interaction_feedback_enumerates_unit_ids(tmp_path: Path) -> None:
    """HIR-0123: role-like participants get the exact legal unit-id vocabulary."""
    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    layout = run_artifacts.create(tmp_path, "interaction-participants")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    payload = _jit_payload(tmp_path, bundle.content_hash)
    document = json.loads(payload.read_text(encoding="utf-8"))
    claim = document["layer"]["stages"][0]["evaluation"]["claims"][0]
    claim.update(
        {
            "kind": "interaction",
            "coordination_owner": "polish",
            "participants": ["polish", "facade.window_bay"],
            "controls": ["hold"],
        }
    )
    _write(payload, document)

    findings, materialized = inspect_materialization(
        bundle.root, payload, expected_bundle_hash=bundle.content_hash
    )

    assert materialized is None
    text = "\n".join(findings)
    assert "unknown participants: facade.window_bay" in text
    assert "not semantic roles or controls" in text
    assert "valid unit ids: polish" in text


def test_materialization_rejects_unpayable_image_property(tmp_path: Path) -> None:
    """HIR-0111: free-form image labels cannot become build-time debts."""
    from vfx_harness.domain.image_debts import IMAGE_PROPERTY_VOCABULARY_RULE

    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    layout = run_artifacts.create(tmp_path, "image-property-vocabulary")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    payload = _jit_payload(tmp_path, bundle.content_hash)
    document = json.loads(payload.read_text(encoding="utf-8"))
    claim = document["layer"]["stages"][0]["evaluation"]["claims"][0]
    claim.update(
        {
            "property": "rim_light_emission_glow",
            "asserts": "image",
            "evidence": [{"kind": "image_contract", "id": "rim-glow-f240"}],
        }
    )
    _write(payload, document)

    findings, materialized = inspect_materialization(
        bundle.root, payload, expected_bundle_hash=bundle.content_hash
    )

    assert materialized is None
    text = "\n".join(findings)
    assert "/layer/stages/0/evaluation/claims:" in text
    assert "rim_light_emission_glow" in text
    assert "frame_halation" in text
    assert IMAGE_PROPERTY_VOCABULARY_RULE in text


def test_plan_gate_mirrors_unpayable_image_property(tmp_path: Path) -> None:
    """A selected legacy view cannot bypass the materialization write boundary."""
    from vfx_harness.evaluation.plan_gate import _check_evidence_coherence

    _candidate(tmp_path)
    document = json.loads((tmp_path / "layers.json").read_text(encoding="utf-8"))
    claim = document["layers"][0]["stages"][0]["evaluation"]["claims"][0]
    claim.update(
        {
            "property": "rim_light_emission_glow",
            "asserts": "image",
            "evidence": [{"kind": "image_contract", "id": "rim-glow-f240"}],
        }
    )
    _write(tmp_path / "layers.json", document)

    findings, _ = _check_evidence_coherence(tmp_path)

    vocabulary = [row for row in findings if row.check == "image-property-vocabulary"]
    assert len(vocabulary) == 1
    assert vocabulary[0].blocking is True
    assert "rim_light_emission_glow" in vocabulary[0].what
    assert "frame_halation" in vocabulary[0].fix


def test_materialization_reports_independent_findings_with_pointers(tmp_path: Path) -> None:
    """l1-remat3 walked one field-precise error per full-document rewrite. Two independent
    defects must land in one write, each addressed by a JSON pointer, and a pointer patch
    must leave the remaining finding then clear the document."""
    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    layout = run_artifacts.create(tmp_path, "pointer-findings")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    payload = _jit_payload(tmp_path, bundle.content_hash)
    document = json.loads(payload.read_text(encoding="utf-8"))
    document["scene_contracts"][1]["owner_layer"] = "9"
    document["layer"]["stages"][0].pop("look_capabilities")
    _write(payload, document)

    findings, materialized = inspect_materialization(
        bundle.root, payload, expected_bundle_hash=bundle.content_hash
    )
    assert materialized is None
    text = "\n".join(findings)
    assert "/scene_contracts/1/owner_layer:" in text
    assert "must be owned by layer 2" in text
    assert "/layer/stages/0/look_capabilities:" in text
    assert "must declare look_capabilities" in text

    remaining = apply_materialization_patch(
        bundle.root,
        payload,
        "/scene_contracts/1/owner_layer",
        "2",
        expected_bundle_hash=bundle.content_hash,
    )
    remaining_text = "\n".join(remaining)
    assert "/scene_contracts/1/owner_layer:" not in remaining_text
    assert "/layer/stages/0/look_capabilities:" in remaining_text

    cleared = apply_materialization_patch(
        bundle.root,
        payload,
        "/layer/stages/0/look_capabilities",
        [],
        expected_bundle_hash=bundle.content_hash,
    )
    assert cleared == []
    validate_materialization(
        bundle.root, payload, expected_bundle_hash=bundle.content_hash
    )


def test_deferred_layer_has_no_fake_units_and_materializes_through_bound_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    parsed = load_layers_from_path(tmp_path / "layers.json")
    assert parsed["2"].execution == "jit_deferred"
    assert parsed["2"].stages == ()
    assert not (tmp_path / "state" / "units" / "2.json").exists()

    layout = run_artifacts.create(tmp_path, "plan-run")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    payload = _jit_payload(tmp_path, bundle.content_hash)
    _passed_layer_one_outcome(tmp_path)
    _publish_materialization(tmp_path, payload)

    from vfx_harness.orchestration.plan_authority import selected_artifact_path

    materialized = load_layers_from_path(selected_artifact_path(tmp_path, "layers.json"))
    assert materialized["2"].execution == "ready"
    assert [unit.id for unit in materialized["2"].stages] == ["polish"]
    requirements = json.loads(
        selected_artifact_path(tmp_path, "requirements.json").read_text(encoding="utf-8")
    )
    assert requirements["requirements"][0]["resolution"] == {
        "kind": "contract",
        "ids": ["polish-lock"],
        "evidence_domains": ["image"],
        "domain_bindings": [
            {"domain": "image", "kind": "contract", "ids": ["polish-lock"]}
        ],
    }
    assert not (tmp_path / "state" / "units" / "2.json").exists()


def test_jit_materialization_fails_closed_on_unbound_requirement(tmp_path: Path) -> None:
    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    layout = run_artifacts.create(tmp_path, "plan-run")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    payload = _jit_payload(tmp_path, bundle.content_hash)
    data = json.loads(payload.read_text(encoding="utf-8"))
    data["requirement_bindings"] = []
    _write(payload, data)

    with pytest.raises(ValueError, match=r"missing R-final-lock"):
        validate_materialization(
            bundle.root, payload, expected_bundle_hash=bundle.content_hash
        )


def test_jit_materialization_waits_for_receipt_backed_upstream_publication(
    tmp_path: Path,
) -> None:
    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    layout = run_artifacts.create(tmp_path, "plan-run")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    payload = _jit_payload(tmp_path, bundle.content_hash)

    with pytest.raises(
        ValueError,
        match="dependency 1 has a current receipt-backed publication",
    ):
        _publish_materialization(tmp_path, payload)


def test_final_lock_is_typed_and_blocks_acceptance_until_matching_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    _candidate(tmp_path)
    findings, stats = _check_meta_records(tmp_path)
    assert findings == []
    assert stats == {"requirements": 1, "open_obligations": 1, "open_assumptions": 0}
    layout = run_artifacts.create(tmp_path, "plan-run")
    bundle = _publish_due_fixture(tmp_path, layout)

    with pytest.raises(PlanDueError, match="O-final-lock"):
        require_due_clear(tmp_path, acceptance=True)

    resolutions = tmp_path / "state" / "plan-resolutions.jsonl"
    resolutions.parent.mkdir(exist_ok=True)
    resolutions.write_text(json.dumps({
        "schema": "vfx-harness.plan-resolutions/v1",
        "bundle_hash": bundle.content_hash,
        "kind": "obligation", "id": "O-final-lock", "status": "satisfied",
        "evidence": [{"kind": "scene_contract", "id": "wrong-contract"}],
    }) + "\n", encoding="utf-8")
    with pytest.raises(PlanDueError, match="O-final-lock"):
        require_due_clear(tmp_path, acceptance=True)

    with resolutions.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({
            "schema": "vfx-harness.plan-resolutions/v1",
            "bundle_hash": bundle.content_hash,
            "kind": "obligation", "id": "O-final-lock", "status": "satisfied",
            "evidence": [{"kind": "scene_contract", "id": "final-lock"}],
        }) + "\n")
    require_due_clear(tmp_path, acceptance=True)


def test_requirement_resolution_cannot_name_an_unknown_obligation(tmp_path: Path) -> None:
    _candidate(tmp_path)
    data = json.loads((tmp_path / "requirements.json").read_text(encoding="utf-8"))
    data["requirements"][0]["resolution"]["ids"] = ["missing"]
    _write(tmp_path / "requirements.json", data)

    findings, _ = _check_meta_records(tmp_path)

    assert any(f.blocking and "unknown obligation" in f.what for f in findings)


def test_uncited_brief_clause_blocks_register_completeness(tmp_path: Path) -> None:
    _candidate(tmp_path)
    (tmp_path / "brief.md").write_text(
        "Final image must hold unchanged from frame 239 to 240.\n\n"
        "| Beat | Required action |\n"
        "|---|---|\n"
        "| Final | End with a clean two-frame lock. |\n",
        encoding="utf-8",
    )
    digest = hashlib.sha256((tmp_path / "brief.md").read_bytes()).hexdigest()
    requirements = json.loads((tmp_path / "requirements.json").read_text(encoding="utf-8"))
    requirements["requirements"][0]["citation"]["sha256"] = digest
    _write(tmp_path / "requirements.json", requirements)

    findings, _ = _check_meta_records(tmp_path)

    assert any(
        finding.blocking
        and finding.check == "requirement-completeness"
        and "lines 5" in finding.what
        for finding in findings
    )


def test_terminal_hold_cannot_resolve_to_a_single_frame_proxy(tmp_path: Path) -> None:
    _candidate(tmp_path)
    requirements = json.loads((tmp_path / "requirements.json").read_text(encoding="utf-8"))
    requirements["requirements"][0]["resolution"] = {
        "kind": "contract",
        "ids": ["final-brightness"],
    }
    _write(tmp_path / "requirements.json", requirements)
    checks = {
        "schema": 2,
        "checks": [{
            "id": "final-brightness",
            "owner_layer": "1",
            "fault_owner": "1",
            "activates_at": "1",
            "lifecycle": "layer",
            "axis": "final_lock",
            "frame": 240,
            "ref": "refs/a.png",
            "metric": "frame_mean",
            "op": "band",
            "lo": 1,
            "hi": 255,
            "stage": "pre_grade",
            "rejects": ["artifacts/bad.png"],
            "proof": {"ref": 100, "adversary": [0]},
        }],
    }
    _write(tmp_path / "checks.json", checks)

    findings, _ = _check_meta_records(tmp_path)

    assert any(
        finding.blocking
        and finding.check == "temporal-requirement"
        and "239→240" in finding.what
        for finding in findings
    )


def test_explicit_motion_law_cannot_be_downgraded_to_a_prose_decision(tmp_path: Path) -> None:
    _candidate(tmp_path)
    (tmp_path / "brief.md").write_text(
        "Final image must hold unchanged from frame 239 to 240.\n\n"
        "Light travels through conduits in expanding waves rather than all at once.\n",
        encoding="utf-8",
    )
    digest = hashlib.sha256((tmp_path / "brief.md").read_bytes()).hexdigest()
    requirements = json.loads((tmp_path / "requirements.json").read_text(encoding="utf-8"))
    requirements["requirements"][0]["citation"]["sha256"] = digest
    requirements["requirements"].append({
        "id": "R-pulse",
        "statement": "light travels through conduits in expanding waves",
        "citation": {"source": "brief.md", "sha256": digest, "line_start": 3, "line_end": 3},
        "resolution": {
            "kind": "decision",
            "ids": [],
            "decision": "animate the conduit light in the ignition unit",
        },
    })
    _write(tmp_path / "requirements.json", requirements)

    findings, _ = _check_meta_records(tmp_path)

    assert any(
        finding.blocking
        and finding.check == "temporal-requirement"
        and "explicit motion law" in finding.what
        for finding in findings
    )


def test_structured_human_decision_must_be_adopted_exactly_by_required_contract(
    tmp_path: Path,
) -> None:
    _candidate(tmp_path)
    selected_bundle = hashlib.sha256(b"view-bundle").hexdigest()
    state = tmp_path / "state"
    state.mkdir()
    expected = {
        "kind": "keyframe_schedule",
        "roles": ["cam_rig"],
        "samples": [
            {"frame": 1, "values": {"location": [0, -6, 0]}},
            {"frame": 240, "values": {"location": [0, 225, 0]}},
        ],
        "op": "max",
        "hi": 0.001,
    }
    (state / "plan-resolutions.jsonl").write_text(json.dumps({
        "schema": "vfx-harness.plan-resolutions/v1",
        "bundle_hash": selected_bundle,
        "kind": "assumption",
        "id": "A-camera",
        "status": "satisfied",
        "evidence": [{"kind": "human_decision", "id": "user-approved-camera"}],
        "decision": "approved exact camera spine",
        "values": {"contract": expected},
    }) + "\n", encoding="utf-8")
    _select_generation(tmp_path, selected_bundle)

    findings, _ = _check_meta_records(tmp_path)
    assert any(
        finding.blocking
        and finding.check == "decision-adoption"
        and "A-camera" in finding.where
        for finding in findings
    )

    scene = json.loads((tmp_path / "scene_checks.json").read_text(encoding="utf-8"))
    scene["contracts"].append({
        "id": "camera-spine",
        "decision_id": "A-camera",
        "owner_layer": "1",
        "fault_owner": "1",
        "activates_at": "1",
        "lifecycle": "persistent",
        "axis": "final_lock",
        **expected,
    })
    _write(tmp_path / "scene_checks.json", scene)
    layers = json.loads((tmp_path / "layers.json").read_text(encoding="utf-8"))
    layers["layers"][0]["stages"][0]["mutates"]["roles"].append("cam_rig")
    layers["layers"][0]["stages"][0]["evaluation"]["claims"].append({
        "id": "camera-spine-claim",
        "proposition": "camera follows the approved exact schedule",
        "axis": "final_lock",
        "property": "keyframe_schedule",
        "subject_roles": ["cam_rig"],
        "subject_controls": [],
        "moments": [1, 240],
        "kind": "atomic",
        "required": True,
        "authority": "executable_required",
        "repair_owner": "lock",
        "evidence": [{"kind": "scene_contract", "id": "camera-spine"}],
    })
    _write(tmp_path / "layers.json", layers)

    findings, _ = _check_meta_records(tmp_path)
    assert not any(finding.check == "decision-adoption" for finding in findings)


def test_exact_reassembly_uses_the_prefracture_frame_as_its_baseline(tmp_path: Path) -> None:
    _candidate(tmp_path)
    (tmp_path / "brief.md").write_text(
        "---\n"
        "id: fixture\n"
        "title: Fixture\n"
        "type: motion\n"
        "frames: 240\n"
        "fps: 24\n"
        "resolution: [1920, 1080]\n"
        "engine: BLENDER_EEVEE_NEXT\n"
        "palette: [black]\n"
        "---\n\n"
        "Final image must hold unchanged from frame 239 to 240.\n\n"
        "| Beat | Frames | Required action |\n"
        "|---|---:|---|\n"
        "| B5 — Fracture | 169–204 | The core fractures the architecture. |\n"
        "| B6 — Reassembly | 205–232 | Pieces return to their exact structural positions. |\n",
        encoding="utf-8",
    )
    digest = hashlib.sha256((tmp_path / "brief.md").read_bytes()).hexdigest()
    requirements = json.loads((tmp_path / "requirements.json").read_text(encoding="utf-8"))
    requirements["requirements"][0]["citation"]["sha256"] = digest
    requirements["requirements"][0]["citation"]["line_start"] = 12
    requirements["requirements"][0]["citation"]["line_end"] = 12
    requirements["requirements"].extend([
        {
            "id": "R-fracture",
            "statement": "fracture spans 169 through 204",
            "citation": {"source": "brief.md", "sha256": digest, "line_start": 16, "line_end": 16},
            "resolution": {"kind": "decision", "ids": [], "decision": "fracture timing"},
        },
        {
            "id": "R-return",
            "statement": "pieces return exactly",
            "citation": {"source": "brief.md", "sha256": digest, "line_start": 17, "line_end": 17},
            "resolution": {"kind": "contract", "ids": ["return-delta"]},
        },
    ])
    _write(tmp_path / "requirements.json", requirements)
    scene = json.loads((tmp_path / "scene_checks.json").read_text(encoding="utf-8"))
    scene["contracts"].append({
        "id": "return-delta",
        "owner_layer": "1",
        "fault_owner": "1",
        "activates_at": "1",
        "lifecycle": "layer",
        "axis": "final_lock",
        "kind": "transform_return_delta",
        "roles": ["comp"],
        "frames": [224, 240],
        "component": "location",
        "op": "max",
        "hi": 0.01,
    })
    _write(tmp_path / "scene_checks.json", scene)
    layers = json.loads((tmp_path / "layers.json").read_text(encoding="utf-8"))
    layers["layers"][0]["stages"][0]["evaluation"]["claims"].append({
        "id": "return-claim",
        "proposition": "pieces return exactly",
        "axis": "final_lock",
        "property": "transform_return_delta",
        "subject_roles": ["comp"],
        "subject_controls": [],
        "moments": [224, 240],
        "kind": "atomic",
        "required": True,
        "authority": "executable_required",
        "repair_owner": "lock",
        "evidence": [{"kind": "scene_contract", "id": "return-delta"}],
    })
    _write(tmp_path / "layers.json", layers)

    findings, _ = _check_meta_records(tmp_path)

    assert any(
        finding.blocking
        and finding.check == "temporal-requirement"
        and "baseline 168" in finding.what
        for finding in findings
    )


def test_entry_gate_cannot_depend_on_contract_produced_by_same_unit(tmp_path: Path) -> None:
    _candidate(tmp_path)
    data = json.loads((tmp_path / "obligations.json").read_text(encoding="utf-8"))
    data["obligations"][0]["due"] = {
        "kind": "before_unit",
        "layer": "1",
        "unit": "lock",
    }
    _write(tmp_path / "obligations.json", data)

    findings, _ = _check_meta_records(tmp_path)

    assert any(
        finding.blocking
        and finding.where == "O-final-lock"
        and "evidence produced by the gated layer" in finding.what
        for finding in findings
    )


def test_unit_completion_obligation_is_discharged_only_by_declared_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    _candidate(tmp_path)
    data = json.loads((tmp_path / "obligations.json").read_text(encoding="utf-8"))
    data["obligations"][0]["due"] = {
        "kind": "unit_completion",
        "layer": "1",
        "unit": "lock",
    }
    _write(tmp_path / "obligations.json", data)
    findings, _ = _check_meta_records(tmp_path)
    assert findings == []
    layout = run_artifacts.create(tmp_path, "plan-run")
    _publish_due_fixture(tmp_path, layout)
    wrong_receipt = synthetic_completion_receipt(
        "1", "lock", {("scene_contract", "wrong-contract")}
    )
    receipt = synthetic_completion_receipt(
        "1", "lock", {("scene_contract", "final-lock")}
    )
    receipt_digest = receipt.receipt_digest
    monkeypatch.setattr(
        "vfx_harness.orchestration.plan_due._current_completion_receipt_projection_for_bundle",
        lambda _shot, _bundle: nullcontext({("1", "lock"): receipt_digest}),
    )

    require_due_clear(tmp_path, layer="1", unit="lock")
    with pytest.raises(PlanDueError, match=r"completion.*O-final-lock"):
        require_due_clear(tmp_path, layer="1", unit="lock", completion=True)

    assert resolve_unit_completion(
        tmp_path,
        layer="1",
        unit="lock",
        completion_receipt=wrong_receipt,
    ) == ()
    assert resolve_unit_completion(
        tmp_path,
        layer="1",
        unit="lock",
        completion_receipt=receipt,
    ) == ("O-final-lock",)
    require_due_clear(tmp_path, layer="1", unit="lock", completion=True)

    monkeypatch.setattr(
        "vfx_harness.orchestration.plan_due._current_completion_receipt_projection_for_bundle",
        lambda _shot, _bundle: nullcontext({}),
    )
    with pytest.raises(PlanDueError, match=r"completion.*O-final-lock"):
        require_due_clear(tmp_path, layer="1", unit="lock", completion=True)


def test_assumption_cannot_use_machine_completion_gate(tmp_path: Path) -> None:
    _candidate(tmp_path)
    _write(tmp_path / "assumptions.json", {
        "schema": "vfx-harness.assumptions/v1",
        "assumptions": [{
            "id": "A-calibration",
            "statement": "use a provisional calibration",
            "requirement_ids": [],
            "owner": "PLAN",
            "due": {"kind": "unit_completion", "layer": "1", "unit": "lock"},
            "impact": {"layers": ["1"], "axes": ["final_lock"], "global_decision": False},
        }],
    })

    findings, _ = _check_meta_records(tmp_path)

    assert any(
        finding.blocking
        and finding.where == "A-calibration"
        and "cannot be machine-resolved" in finding.what
        for finding in findings
    )


def test_provisional_start_requires_a_named_runtime_falsification_path(tmp_path: Path) -> None:
    _candidate(tmp_path)
    _write(tmp_path / "assumptions.json", {
        "schema": "vfx-harness.assumptions/v1",
        "assumptions": [{
            "id": "A-camera",
            "statement": "start from the approved camera spine",
            "requirement_ids": [],
            "owner": "PLAN",
            "decision_strength": "approved_start",
            "due": {"kind": "before_layer", "layer": "1"},
            "impact": {"layers": ["1"], "axes": ["final_lock"], "global_decision": False},
        }],
    })

    findings, _ = _check_meta_records(tmp_path)

    assert any(
        finding.blocking
        and "requires falsification.owner and contract_ids" in finding.what
        for finding in findings
    )


def test_legacy_assumption_defaults_to_hard_constraint_not_tunable_start(tmp_path: Path) -> None:
    from vfx_harness.domain.plan_records import load_assumptions

    _candidate(tmp_path)
    _write(tmp_path / "assumptions.json", {
        "schema": "vfx-harness.assumptions/v1",
        "assumptions": [{
            "id": "A-aspect",
            "statement": "use 16:9",
            "requirement_ids": [],
            "owner": "PLAN",
            "due": {"kind": "before_layer", "layer": "1"},
            "impact": {"layers": ["1"], "axes": [], "global_decision": True},
        }],
    })

    assert load_assumptions(tmp_path)[0].decision_strength == "hard_constraint"


def test_acceptance_obligation_blocks_verdict_not_acceptance_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    _candidate(tmp_path)
    layout = run_artifacts.create(tmp_path, "plan-run")
    bundle = _publish_due_fixture(tmp_path, layout)

    # The finished-chain renderer must be allowed to run and produce this evidence.
    require_due_clear(
        tmp_path,
        acceptance=True,
        record_kinds=frozenset({"assumption"}),
    )
    with pytest.raises(PlanDueError, match="O-final-lock"):
        require_due_clear(tmp_path, acceptance=True)

    assert resolve_acceptance_completion(
        tmp_path,
        passed_evidence={("scene_contract", "wrong-contract")},
        expected_bundle_digest=bundle.content_hash,
    ) == ()
    selected_authority = resolve_selected_authority(tmp_path)
    monkeypatch.setattr(
        "vfx_harness.orchestration.plan_due.resolve_current",
        lambda _folder: pytest.fail(
            "snapshot-bound acceptance completion re-resolved the live plan"
        ),
    )
    assert resolve_acceptance_completion(
        tmp_path,
        passed_evidence={("scene_contract", "final-lock")},
        expected_bundle_digest=bundle.content_hash,
        selected_authority=selected_authority,
    ) == ("O-final-lock",)
    require_due_clear(
        tmp_path,
        acceptance=True,
        selected_authority=selected_authority,
    )


def test_unit_completion_requires_a_required_claim_in_the_due_unit(tmp_path: Path) -> None:
    _candidate(tmp_path)
    obligations = json.loads((tmp_path / "obligations.json").read_text(encoding="utf-8"))
    obligations["obligations"][0]["due"] = {
        "kind": "unit_completion",
        "layer": "1",
        "unit": "lock",
    }
    _write(tmp_path / "obligations.json", obligations)
    layers = json.loads((tmp_path / "layers.json").read_text(encoding="utf-8"))
    layers["layers"][0]["stages"][0]["evaluation"]["claims"][0]["required"] = False
    layers["layers"][0]["stages"][0]["evaluation"]["claims"][0]["authority"] = "advisory"
    _write(tmp_path / "layers.json", layers)

    findings, _ = _check_meta_records(tmp_path)

    assert any(
        finding.blocking
        and finding.where == "O-final-lock"
        and "not bound to required claims" in finding.what
        for finding in findings
    )


def test_acceptance_evidence_evaluates_post_grade_contracts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    render = tmp_path / "finished.png"
    Image.new("RGB", (16, 16), (100, 100, 100)).save(render)
    checks = tmp_path / "checks.json"
    _write(checks, {
        "schema": 2,
        "checks": [{
            "id": "finished-exposure",
            "metric": "frame_mean",
            "op": "band",
            "lo": 90,
            "hi": 110,
            "frame": 240,
            "owner_layer": "1",
            "fault_owner": "1",
            "activates_at": "1",
            "lifecycle": "acceptance",
            "axis": "exposure",
            "stage": "post_grade",
            "rejects": ["artifacts/bad.png"],
            "proof": {"adversary": [20.0]},
        }],
    })
    monkeypatch.setattr(
        "vfx_harness.orchestration.plan_authority.selected_artifact_path",
        lambda _root, name: checks if name == "checks.json" else tmp_path / name,
    )

    rows = acceptance_evidence(
        tmp_path,
        frame=240,
        ref="refs/a.png",
        render=render,
    )

    assert len(rows) == 1
    assert rows[0]["id"] == "finished-exposure"
    assert rows[0]["pass"] is True
    assert rows[0]["authoritative"] is True
    assert rows[0]["source"] == "image_contract"


def test_acceptance_evidence_scopes_frameless_checks_to_the_selected_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    render = tmp_path / "finished.png"
    Image.new("RGB", (16, 16), (100, 100, 100)).save(render)
    checks = tmp_path / "checks.json"
    _write(checks, {
        "schema": 2,
        "checks": [
            {
                "id": "m1-only-exposure",
                "metric": "frame_mean",
                "op": "band",
                "lo": 90,
                "hi": 110,
                "ref": "refs/m1.png",
                "owner_layer": "1",
                "fault_owner": "1",
                "activates_at": "1",
                "lifecycle": "acceptance",
                "axis": "exposure",
                "stage": "post_grade",
                "rejects": ["artifacts/m1-bad.png"],
                "proof": {"adversary": [20.0]},
            },
            {
                "id": "m2-only-exposure",
                "metric": "frame_mean",
                "op": "band",
                "lo": 90,
                "hi": 110,
                "ref": "refs/m2.png",
                "owner_layer": "2",
                "fault_owner": "2",
                "activates_at": "2",
                "lifecycle": "acceptance",
                "axis": "exposure",
                "stage": "post_grade",
                "rejects": ["artifacts/m2-bad.png"],
                "proof": {"adversary": [20.0]},
            },
        ],
    })
    monkeypatch.setattr(
        "vfx_harness.orchestration.plan_authority.selected_artifact_path",
        lambda _root, name: checks if name == "checks.json" else tmp_path / name,
    )

    rows = acceptance_evidence(
        tmp_path,
        frame=200,
        ref="refs/m2.png",
        render=render,
    )

    assert [row["id"] for row in rows] == ["m2-only-exposure"]


def test_typed_promises_are_rejected_by_the_loader(tmp_path: Path) -> None:
    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    data = json.loads((tmp_path / "layers.json").read_text(encoding="utf-8"))
    data["layers"][1]["jit"]["promises"] = [{
        "id": "L2.JIT-P1", "contract_kind": "frame_delta", "moments": [239, 240],
        "requirement_ids": ["R-final-lock"],
    }]
    _write(tmp_path / "layers.json", data)

    with pytest.raises(ValueError, match="superseded"):
        load_layers_from_path(tmp_path / "layers.json")


def test_claim_moments_outside_judge_name_extra_frame_binding(tmp_path: Path) -> None:
    from vfx_harness.domain.work_units import EXTRA_FRAME_BINDING_RULE

    _candidate(tmp_path)
    data = json.loads((tmp_path / "layers.json").read_text(encoding="utf-8"))
    data["layers"][0]["stages"][0]["evaluation"]["claims"][0]["moments"] = [239, 240, 12]
    _write(tmp_path / "layers.json", data)

    with pytest.raises(ValueError) as raised:
        load_layers_from_path(tmp_path / "layers.json")
    message = str(raised.value)
    assert "moments outside its judge set: [12]" in message
    assert EXTRA_FRAME_BINDING_RULE in message


def test_unit_judge_outside_layer_names_extra_frame_binding(tmp_path: Path) -> None:
    from vfx_harness.domain.work_units import EXTRA_FRAME_BINDING_RULE

    _candidate(tmp_path)
    data = json.loads((tmp_path / "layers.json").read_text(encoding="utf-8"))
    data["layers"][0]["stages"][0]["evaluation"]["judge"].append(
        {"frame": 12, "ref": "refs/a.png"}
    )
    _write(tmp_path / "layers.json", data)

    with pytest.raises(ValueError) as raised:
        load_layers_from_path(tmp_path / "layers.json")
    message = str(raised.value)
    assert "judges frames outside the layer contract: [12]" in message
    assert EXTRA_FRAME_BINDING_RULE in message


def _structured_camera_decision(root: Path, bundle_hash: str = "a" * 64) -> dict:
    state = root / "state"
    state.mkdir(exist_ok=True)
    expected = {
        "kind": "keyframe_schedule",
        "roles": ["cam_rig"],
        "samples": [
            {"frame": 1, "values": {"location": [0, -6, 0]}},
            {"frame": 240, "values": {"location": [0, 225, 0]}},
        ],
        "op": "max",
        "hi": 0.001,
    }
    (state / "plan-resolutions.jsonl").write_text(json.dumps({
        "schema": "vfx-harness.plan-resolutions/v1",
        "bundle_hash": bundle_hash,
        "kind": "assumption",
        "id": "A-camera",
        "status": "satisfied",
        "evidence": [{"kind": "human_decision", "id": "user-approved-camera"}],
        "decision": "approved exact camera spine",
        "values": {"contract": expected},
    }) + "\n", encoding="utf-8")
    _select_generation(root, bundle_hash)
    return expected


def _all_deferred_schema5(root: Path, reserved: list[str]) -> None:
    data = json.loads((root / "layers.json").read_text(encoding="utf-8"))
    data["schema"] = 5
    for row in data["layers"]:
        row["execution"] = "jit_deferred"
        row["stages"] = []
        row.setdefault("jit", {
            "depends_on_layers": [], "required_outcomes": [],
            "reserved_roles": ["placeholder.*"], "owned_requirements": ["R-final-lock"],
        })
    data["layers"][0]["jit"]["reserved_roles"] = reserved
    _write(root / "layers.json", data)


def test_unit_first_decision_adoption_defers_to_owning_layer(tmp_path: Path) -> None:
    """A schema-5 bundle publishes no contracts, so demanding the exact adopted copy at
    global time deadlocks against the global-preproduction rule (run
    20260823T095834Z-7040c2). The global obligation narrows to ownership; the verbatim
    copy is enforced by validate_materialization at the owning layer."""
    _candidate(tmp_path)
    _structured_camera_decision(tmp_path)
    _all_deferred_schema5(tmp_path, reserved=["cam_rig", "camera.*"])

    findings, _ = _check_meta_records(tmp_path)
    assert not any(finding.check == "decision-adoption" for finding in findings)


def test_unit_first_unowned_decision_roles_still_block(tmp_path: Path) -> None:
    _candidate(tmp_path)
    _structured_camera_decision(tmp_path)
    _all_deferred_schema5(tmp_path, reserved=["iris.*"])

    findings, _ = _check_meta_records(tmp_path)
    assert any(
        finding.blocking
        and finding.check == "decision-adoption"
        and "A-camera" in finding.where
        and "no deferred layer reserves" in finding.what
        for finding in findings
    )


def test_materialization_must_adopt_owned_structured_decision(tmp_path: Path) -> None:
    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    layout = run_artifacts.create(tmp_path, "plan-run")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    expected = {
        "kind": "keyframe_schedule",
        "roles": ["polish.comp"],
        "samples": [
            {"frame": 239, "values": {"location": [0, 0, 0]}},
            {"frame": 240, "values": {"location": [0, 0, 0]}},
        ],
        "op": "max",
        "hi": 0.001,
    }
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    (state / "plan-resolutions.jsonl").write_text(json.dumps({
        "schema": "vfx-harness.plan-resolutions/v1",
        "bundle_hash": bundle.content_hash,
        "kind": "assumption",
        "id": "A-polish",
        "status": "satisfied",
        "evidence": [{"kind": "human_decision", "id": "user-approved-polish"}],
        "decision": "approved polish hold",
        "values": {"contract": expected},
    }) + "\n", encoding="utf-8")
    payload = _jit_payload(tmp_path, bundle.content_hash)

    with pytest.raises(ValueError, match="must adopt structured decision A-polish"):
        validate_materialization(
            bundle.root, payload,
            expected_bundle_hash=bundle.content_hash,
            resolutions_path=state / "plan-resolutions.jsonl",
        )

    data = json.loads(payload.read_text(encoding="utf-8"))
    data["scene_contracts"].append({
        "id": "polish-spine", "decision_id": "A-polish", "owner_layer": "2",
        "fault_owner": "2", "activates_at": "2", "lifecycle": "persistent",
        "axis": "final_lock", **expected,
    })
    data["layer"]["stages"][0]["evaluation"]["claims"].append({
        "id": "polish-spine-claim",
        "proposition": "polish keys the approved hold exactly",
        "axis": "final_lock", "property": "keyframe_schedule",
        "subject_roles": ["polish.comp"], "subject_controls": [],
        "moments": [240], "kind": "atomic", "required": True,
        "authority": "executable_required", "repair_owner": "polish",
        "asserts": "temporal",
        "evidence": [{"kind": "scene_contract", "id": "polish-spine"}],
    })
    _write(payload, data)

    materialized = validate_materialization(
        bundle.root, payload,
        expected_bundle_hash=bundle.content_hash,
        resolutions_path=state / "plan-resolutions.jsonl",
    )
    assert any(
        row.get("decision_id") == "A-polish" for row in materialized.scene_contracts
    )


def test_load_active_structured_decisions_keys_to_selected_bundle_and_retires(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "plan-resolutions.jsonl"
    selected = "a" * 64
    other = "b" * 64
    contract = {"kind": "keyframe_schedule", "roles": ["cam_rig"], "op": "max", "hi": 0.001}
    replacement = {**contract, "hi": 0.002}
    rows = [
        {
            "schema": "vfx-harness.plan-resolutions/v1",
            "bundle_hash": other, "kind": "assumption", "id": "A2",
            "status": "satisfied", "values": {"contract": contract},
        },
        {
            "schema": "vfx-harness.plan-resolutions/v1",
            "bundle_hash": selected, "kind": "assumption", "id": "A2",
            "status": "satisfied", "values": {"contract": contract},
        },
        {
            "schema": "vfx-harness.plan-resolutions/v1",
            "bundle_hash": selected, "kind": "assumption", "id": "A2",
            "status": "falsified", "decision": "calibration occludes the subject",
        },
        {
            "schema": "vfx-harness.plan-resolutions/v1",
            "bundle_hash": selected, "kind": "assumption", "id": "A3",
            "status": "satisfied", "values": {"contract": replacement},
        },
        {
            "schema": "vfx-harness.plan-resolutions/v1",
            "bundle_hash": selected, "kind": "assumption", "id": "A3",
            "status": "superseded",
        },
        {
            "schema": "vfx-harness.plan-resolutions/v1",
            "bundle_hash": selected, "kind": "assumption", "id": "A4",
            "status": "satisfied", "values": {"contract": replacement},
        },
    ]
    ledger.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    active = load_active_structured_decisions(ledger, bundle_hash=selected)
    assert "A2" not in active
    assert "A3" not in active
    assert active["A4"].contract["hi"] == 0.002
    assert load_active_structured_decisions(ledger, bundle_hash=other)["A2"].id == "A2"


def test_other_generation_structured_decision_is_inert_at_the_gate(tmp_path: Path) -> None:
    _candidate(tmp_path)
    other_bundle = hashlib.sha256(b"other-generation").hexdigest()
    selected_bundle = hashlib.sha256(b"view-bundle").hexdigest()
    _structured_camera_decision(tmp_path, bundle_hash=other_bundle)
    _select_generation(tmp_path, selected_bundle)

    findings, _ = _check_meta_records(tmp_path)
    assert not any(finding.check == "decision-adoption" for finding in findings)


def test_other_generation_structured_decision_does_not_force_materialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    layout = run_artifacts.create(tmp_path, "plan-run")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    expected = {
        "kind": "keyframe_schedule",
        "roles": ["polish.comp"],
        "samples": [
            {"frame": 239, "values": {"location": [0, 0, 0]}},
            {"frame": 240, "values": {"location": [0, 0, 0]}},
        ],
        "op": "max",
        "hi": 0.001,
    }
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    other_bundle = hashlib.sha256(b"other-generation").hexdigest()
    (state / "plan-resolutions.jsonl").write_text(json.dumps({
        "schema": "vfx-harness.plan-resolutions/v1",
        "bundle_hash": other_bundle,
        "kind": "assumption",
        "id": "A-polish",
        "status": "satisfied",
        "evidence": [{"kind": "human_decision", "id": "user-approved-polish"}],
        "decision": "approved polish hold",
        "values": {"contract": expected},
    }) + "\n", encoding="utf-8")
    payload = _jit_payload(tmp_path, bundle.content_hash)

    validate_materialization(
        bundle.root, payload,
        expected_bundle_hash=bundle.content_hash,
        resolutions_path=state / "plan-resolutions.jsonl",
    )

    data = json.loads(payload.read_text(encoding="utf-8"))
    data["scene_contracts"][0]["decision_id"] = "A-polish"
    _write(payload, data)
    with pytest.raises(ValueError, match="not active on the selected bundle"):
        validate_materialization(
            bundle.root, payload,
            expected_bundle_hash=bundle.content_hash,
            resolutions_path=state / "plan-resolutions.jsonl",
        )


def test_later_falsified_row_retires_structured_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    layout = run_artifacts.create(tmp_path, "plan-run")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    expected = {
        "kind": "keyframe_schedule",
        "roles": ["polish.comp"],
        "samples": [
            {"frame": 239, "values": {"location": [0, 0, 0]}},
            {"frame": 240, "values": {"location": [0, 0, 0]}},
        ],
        "op": "max",
        "hi": 0.001,
    }
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    (state / "plan-resolutions.jsonl").write_text(
        json.dumps({
            "schema": "vfx-harness.plan-resolutions/v1",
            "bundle_hash": bundle.content_hash,
            "kind": "assumption",
            "id": "A-polish",
            "status": "satisfied",
            "evidence": [{"kind": "human_decision", "id": "user-approved-polish"}],
            "decision": "approved polish hold",
            "values": {"contract": expected},
        })
        + "\n"
        + json.dumps({
            "schema": "vfx-harness.plan-resolutions/v1",
            "bundle_hash": bundle.content_hash,
            "kind": "assumption",
            "id": "A-polish",
            "status": "falsified",
            "evidence": [{"kind": "human_decision", "id": "operator-falsified-polish"}],
            "decision": "calibration occludes the subject; explicit replan",
        })
        + "\n",
        encoding="utf-8",
    )
    payload = _jit_payload(tmp_path, bundle.content_hash)

    validate_materialization(
        bundle.root, payload,
        expected_bundle_hash=bundle.content_hash,
        resolutions_path=state / "plan-resolutions.jsonl",
    )


def test_deferred_dependent_may_leave_required_outcomes_empty(tmp_path: Path) -> None:
    """All-deferred global documents have no sealed evidence to name: evidence_owners is
    empty because nothing has stages, so demanding non-empty required_outcomes forced
    fabricated ids (run 20260823T093656Z-35a68c). The edge is global; the binding waits
    for materialization."""
    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    data = json.loads((tmp_path / "layers.json").read_text(encoding="utf-8"))
    data["schema"] = 5
    data["layers"][0]["execution"] = "jit_deferred"
    data["layers"][0]["stages"] = []
    data["layers"][0]["jit"] = {
        "depends_on_layers": [], "required_outcomes": [],
        "reserved_roles": ["product.*"], "owned_requirements": ["R-root"],
    }
    data["layers"][1]["jit"]["required_outcomes"] = []
    _write(tmp_path / "layers.json", data)

    parsed = load_layers_from_path(tmp_path / "layers.json")

    assert parsed["2"].jit.required_outcomes == ()


def test_outcome_against_deferred_dependency_says_leave_empty(tmp_path: Path) -> None:
    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    data = json.loads((tmp_path / "layers.json").read_text(encoding="utf-8"))
    data["schema"] = 5
    data["layers"][0]["execution"] = "jit_deferred"
    data["layers"][0]["stages"] = []
    data["layers"][0]["jit"] = {
        "depends_on_layers": [], "required_outcomes": [],
        "reserved_roles": ["product.*"], "owned_requirements": ["R-root"],
    }
    # layer 2 keeps its scene_contract:final-lock reference, but layer 1 is now deferred
    # and owns no sealed evidence — the error must teach the legal shape.
    _write(tmp_path / "layers.json", data)

    with pytest.raises(ValueError, match="leave required_outcomes empty"):
        load_layers_from_path(tmp_path / "layers.json")


def test_ready_dependency_still_requires_named_outcomes(tmp_path: Path) -> None:
    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    data = json.loads((tmp_path / "layers.json").read_text(encoding="utf-8"))
    data["layers"][1]["jit"]["required_outcomes"] = []
    _write(tmp_path / "layers.json", data)

    with pytest.raises(ValueError, match="must name the sealed evidence"):
        load_layers_from_path(tmp_path / "layers.json")


def test_unit_first_ready_dependency_leaves_deferred_consumer_unbound(tmp_path: Path) -> None:
    """A dependency turning ready is the normal state after every upstream
    materialization in a unit-first document; the still-deferred consumer's row is
    immutable global authority with deliberately empty outcomes. Run 20260823T133128Z
    burned a full session against the legacy rule firing during the ROOT layer's
    overlay validation — an error with no legal fix."""
    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    data = json.loads((tmp_path / "layers.json").read_text(encoding="utf-8"))
    data["schema"] = 5
    data["layers"][1]["jit"]["required_outcomes"] = []
    _write(tmp_path / "layers.json", data)

    parsed = load_layers_from_path(tmp_path / "layers.json")

    assert parsed["1"].execution == "ready"
    assert parsed["2"].jit.required_outcomes == ()


def test_root_materialization_validates_with_deferred_dependents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The combined validation view must keep the base document's schema so unit-first
    loader semantics apply — the exact shape of materializing layer 1 while layer 2
    stays deferred with empty outcomes."""
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    _candidate(tmp_path)
    document = json.loads((tmp_path / "layers.json").read_text(encoding="utf-8"))
    ready_layer = document["layers"][0]
    root = {**ready_layer, "execution": "jit_deferred", "stages": []}
    root["jit"] = {
        "depends_on_layers": [], "required_outcomes": [],
        "reserved_roles": ["comp"], "owned_requirements": ["R-final-lock"],
    }
    dependent = {
        **ready_layer, "id": "2", "script": "build/02_later.py", "title": "Later",
        "execution": "jit_deferred", "stages": [],
        "jit": {
            "depends_on_layers": ["1"], "required_outcomes": [],
            "reserved_roles": ["later.*"], "owned_requirements": ["R-later"],
        },
    }
    document["schema"] = 5
    document["layers"] = [root, dependent]
    _write(tmp_path / "layers.json", document)
    _write(tmp_path / "scene_checks.json", {"schema": 2, "contracts": []})
    requirements = json.loads((tmp_path / "requirements.json").read_text(encoding="utf-8"))
    requirements["requirements"][0]["resolution"] = _deferred_owner("1", "image")
    _write(tmp_path / "requirements.json", requirements)
    _write(tmp_path / "obligations.json", {
        "schema": "vfx-harness.obligations/v1", "obligations": [],
    })
    layout = run_artifacts.create(tmp_path, "root-with-dependents")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    payload = tmp_path / "root-jit.json"
    _write(payload, {
        "schema": MATERIALIZATION_SCHEMA,
        "bundle_hash": bundle.content_hash,
        "base_selection": _base_selection(tmp_path).to_dict(),
        "layer": _declaring({**ready_layer, "execution": "ready"}),
        "scene_contracts": [{
            "id": "final-lock", "kind": "frame_delta", "owner_layer": "1",
            "fault_owner": "1", "activates_at": "1", "lifecycle": "layer",
            "axis": "final_lock", "frames": [239, 240], "op": "max", "hi": 0.01,
        }, *_vis_rows("1", (239, 240))],
        "image_contracts": [],
        "requirement_bindings": [{
            "requirement_id": "R-final-lock", "contract_ids": ["final-lock"],
        }],
        "acceptance": [],
    })

    materialized = validate_materialization(
        bundle.root, payload, expected_bundle_hash=bundle.content_hash
    )

    assert materialized.layer.id == "1"


def test_concretely_resolved_owned_requirement_fails_closed_at_materialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run 20260823T135432Z deadlocked on a bundle whose owned_requirements carried
    decision-resolved rows: closure demanded a binding the publish check forbade. Owned
    means owed — such a bundle is inconsistent authority and the remedy is
    republication, never a consumption-side accommodation that would generalize one
    shot's accident into the contract."""
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    requirements = json.loads((tmp_path / "requirements.json").read_text(encoding="utf-8"))
    requirements["requirements"].append({
        "id": "R-format",
        "statement": "Build target: 240 frames at 24fps.",
        "citation": requirements["requirements"][0]["citation"],
        "resolution": {"kind": "decision", "ids": [],
                       "decision": "authored front matter",
                       "decision_strength": "hard_constraint"},
    })
    _write(tmp_path / "requirements.json", requirements)
    layers = json.loads((tmp_path / "layers.json").read_text(encoding="utf-8"))
    layers["layers"][1]["jit"]["owned_requirements"] = ["R-final-lock", "R-format"]
    _write(tmp_path / "layers.json", layers)
    layout = run_artifacts.create(tmp_path, "closed-rows")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    payload = _jit_payload(tmp_path, bundle.content_hash)

    with pytest.raises(ValueError, match="inconsistent authority; republish"):
        validate_materialization(
            bundle.root, payload, expected_bundle_hash=bundle.content_hash
        )


def test_gate_rejects_owned_requirements_that_carry_no_debt(tmp_path: Path) -> None:
    """The publication gate owns this consistency: bundle a5692e9f shipped the deadlock
    because only the deferred_owner→owned direction was verified."""
    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    requirements = json.loads((tmp_path / "requirements.json").read_text(encoding="utf-8"))
    requirements["requirements"].append({
        "id": "R-format",
        "statement": "Build target: 240 frames at 24fps.",
        "citation": requirements["requirements"][0]["citation"],
        "resolution": {"kind": "decision", "ids": [],
                       "decision": "authored front matter",
                       "decision_strength": "hard_constraint"},
    })
    _write(tmp_path / "requirements.json", requirements)
    layers = json.loads((tmp_path / "layers.json").read_text(encoding="utf-8"))
    layers["layers"][1]["jit"]["owned_requirements"] = ["R-final-lock", "R-format"]
    _write(tmp_path / "layers.json", layers)

    findings, _ = _check_meta_records(tmp_path)

    assert any(
        finding.blocking
        and finding.check == "requirement-closure"
        and "carries no debt" in finding.what
        and finding.where == "R-format"
        for finding in findings
    )


def test_owned_requirement_deferred_to_another_layer_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    requirements = json.loads((tmp_path / "requirements.json").read_text(encoding="utf-8"))
    requirements["requirements"][0]["resolution"] = _deferred_owner("1", "image")
    _write(tmp_path / "requirements.json", requirements)
    layout = run_artifacts.create(tmp_path, "foreign-owner")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    payload = _jit_payload(tmp_path, bundle.content_hash)

    with pytest.raises(ValueError, match="deferred to another layer"):
        validate_materialization(
            bundle.root, payload, expected_bundle_hash=bundle.content_hash
        )


def test_required_outcome_kind_error_enumerates_valid_kinds(tmp_path: Path) -> None:
    """The planner is workspace-confined, so the validation message is its only route to
    the enum. Run 20260823T085630Z-1c18c2 spent draft turns guessing spellings for a
    constraint whose accepted values it had no way to look up."""
    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    data = json.loads((tmp_path / "layers.json").read_text(encoding="utf-8"))
    data["layers"][1]["jit"]["required_outcomes"] = [
        {"kind": "property", "id": "camera_spine_locked"}
    ]
    _write(tmp_path / "layers.json", data)

    with pytest.raises(
        ValueError, match="'scene_contract', 'image_contract', or 'semantic_diff'"
    ):
        load_layers_from_path(tmp_path / "layers.json")


def test_owned_requirements_must_be_unique(tmp_path: Path) -> None:
    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    data = json.loads((tmp_path / "layers.json").read_text(encoding="utf-8"))
    data["layers"][1]["jit"]["owned_requirements"] = ["R-final-lock", "R-final-lock"]
    _write(tmp_path / "layers.json", data)

    with pytest.raises(ValueError, match="unique non-empty"):
        load_layers_from_path(tmp_path / "layers.json")


def test_deferred_layer_cannot_hide_future_units_in_its_stub(tmp_path: Path) -> None:
    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    data = json.loads((tmp_path / "layers.json").read_text(encoding="utf-8"))
    data["layers"][1]["stages"] = [data["layers"][0]["stages"][0]]
    _write(tmp_path / "layers.json", data)

    with pytest.raises(ValueError, match="must be empty for jit_deferred"):
        load_layers_from_path(tmp_path / "layers.json")


def test_deferred_layer_is_exempt_from_ready_coverage_rules(tmp_path: Path) -> None:
    from vfx_harness.evaluation.plan_gate import _check_coverage

    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)

    findings, _ = _check_coverage(tmp_path)

    assert not [f for f in findings if f.where == "layer 2"], (
        "a jit_deferred layer deliberately has no executable checks before materialization"
    )


def test_full_global_gate_emits_no_executable_findings_for_sparse_deferred_layer(
    tmp_path: Path,
) -> None:
    from vfx_harness.evaluation.plan_gate import run

    _candidate(tmp_path)
    Image.new("RGB", (32, 32), "black").save(tmp_path / "refs" / "a.png")
    _add_deferred_layer(tmp_path)
    requirements = json.loads((tmp_path / "requirements.json").read_text(encoding="utf-8"))
    requirements["requirements"][0]["resolution"] = _deferred_owner("2")
    _write(tmp_path / "requirements.json", requirements)
    _write(tmp_path / "obligations.json", {
        "schema": "vfx-harness.obligations/v1", "obligations": [],
    })

    result = run(tmp_path)

    deferred_findings = [
        finding for finding in result.findings
        if "layer 2" in finding.where.lower()
        or "L2.JIT" in finding.where
        or "L2.JIT" in finding.what
    ]
    assert deferred_findings == []


def test_deferred_owner_and_requirement_link_directly_without_obligation(
    tmp_path: Path,
) -> None:
    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    findings, _ = _check_meta_records(tmp_path)

    assert not [
        finding for finding in findings
        if finding.check in {"requirement-closure", "temporal-requirement"}
    ]


def test_deferred_owner_link_must_be_symmetric(tmp_path: Path) -> None:
    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    layers = json.loads((tmp_path / "layers.json").read_text(encoding="utf-8"))
    layers["layers"][1]["jit"]["owned_requirements"] = ["R-other"]
    _write(tmp_path / "layers.json", layers)

    findings, _ = _check_meta_records(tmp_path)

    assert any("ownership register names layer 2" in finding.what for finding in findings)


def test_deferred_layer_cannot_publish_concrete_contract_before_materialization(
    tmp_path: Path,
) -> None:
    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    checks = json.loads((tmp_path / "scene_checks.json").read_text(encoding="utf-8"))
    checks["contracts"].append({
        "id": "early-polish-lock", "kind": "frame_delta", "owner_layer": "2",
        "fault_owner": "2", "activates_at": "2", "lifecycle": "layer",
        "axis": "final_lock", "frames": [239, 240], "op": "max", "hi": 0.01,
    })
    _write(tmp_path / "scene_checks.json", checks)

    findings, _ = _check_meta_records(tmp_path)

    assert any(finding.check == "deferred-overplanning" for finding in findings)


def test_global_acceptance_rejects_unmaterialized_layer_fingerprint(tmp_path: Path) -> None:
    from vfx_harness.evaluation.plan_gate import _check_contracts

    _candidate(tmp_path)
    Image.new("RGB", (32, 32), "black").save(tmp_path / "refs" / "a.png")
    _add_deferred_layer(tmp_path)
    layers = json.loads((tmp_path / "layers.json").read_text(encoding="utf-8"))
    layers["layers"][1]["judge"] = [{"frame": 300, "ref": "refs/a.png"}]
    layers["layers"][1]["primary_judge"] = 300
    _write(tmp_path / "layers.json", layers)
    _write(tmp_path / "acceptance.json", [{
        "id": "M-later", "frame": 300, "ref": "refs/a.png", "reads": "later finish",
        "fingerprint": "exposure_mean=0",
    }])

    findings, _ = _check_contracts(tmp_path)

    assert any(
        finding.check == "deferred-overplanning" and "fingerprints frame 300" in finding.what
        for finding in findings
    )


def test_materialization_can_close_owned_requirement_with_typed_decision(tmp_path: Path) -> None:
    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    layout = run_artifacts.create(tmp_path, "plan-run")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    payload = _jit_payload(tmp_path, bundle.content_hash)
    data = json.loads(payload.read_text(encoding="utf-8"))
    data["layer"]["stages"][0]["provides"] = ["geometry"]
    data["requirement_bindings"] = [{
        "requirement_id": "R-final-lock",
        "decision": {
            "statement": "frames 239 and 240 are unchanged",
            "decision_strength": "approved_start",
            "judgment": _judgment(),
        },
    }]
    _write(payload, data)

    materialized = validate_materialization(
        bundle.root, payload, expected_bundle_hash=bundle.content_hash
    )

    assert materialized.requirement_decisions["R-final-lock"]["decision_strength"] == "approved_start"


def test_materialization_refuses_required_bbox_outside_mutation_roles(tmp_path: Path) -> None:
    """HIR-0159: validator-clean then gate role-selector-closure burned the last turn."""
    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    layout = run_artifacts.create(tmp_path, "observer-bbox")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    payload = _jit_payload(tmp_path, bundle.content_hash)
    data = json.loads(payload.read_text(encoding="utf-8"))
    layer = data["layer"]
    layer["stages"].append({
        "id": "observer",
        "title": "Composition observer",
        "plan": "plans/02_polish/observer.md",
        "depends_on": ["polish"],
        "mutates": {
            "mode": "scoped",
            "roles": [],
            "controls": [],
            "script_spans": ["build/units/02/observer.py"],
        },
        "protects": {
            "selector": "all_active_upstream_interfaces",
            "resolve_to_explicit_ids_at": "freeze",
        },
        "evaluation": {
            "primary_judge": 240,
            "judge": layer["judge"],
            "temporal_evidence": "none",
            "claims": [{
                "id": "observer-bbox",
                "proposition": "subject stays framed",
                "axis": "final_lock",
                "property": "bbox_height",
                "subject_roles": ["polish.comp"],
                "subject_controls": [],
                "moments": [239, 240],
                "kind": "atomic",
                "required": True,
                "authority": "executable_required",
                "repair_owner": "observer",
                "asserts": "projected_composition",
                "evidence": [
                    {"kind": "scene_contract", "id": "observer-bbox-f239"},
                    {"kind": "scene_contract", "id": "observer-bbox-f240"},
                ],
            }],
        },
        "completion": "all_required_claims_and_protected_contracts_pass",
        "look_capabilities": [],
    })
    for frame in (239, 240):
        data["scene_contracts"].append({
            "id": f"observer-bbox-f{frame}",
            "kind": "bbox_height",
            "owner_layer": "2",
            "fault_owner": "2",
            "activates_at": "2",
            "lifecycle": "layer",
            "axis": "final_lock",
            "roles": ["polish.comp"],
            "frame": frame,
            "op": "band",
            "lo": 0.3,
            "hi": 0.5,
        })
    _write(payload, data)

    with pytest.raises(ValueError, match="outside mutation authority"):
        validate_materialization(
            bundle.root, payload, expected_bundle_hash=bundle.content_hash
        )


def test_materialization_cannot_replace_requirement_with_meta_debt_statement(
    tmp_path: Path,
) -> None:
    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    layout = run_artifacts.create(tmp_path, "proposition-laundering")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    payload = _jit_payload(tmp_path, bundle.content_hash)
    data = json.loads(payload.read_text(encoding="utf-8"))
    data["requirement_bindings"] = [{
        "requirement_id": "R-final-lock",
        "decision": {
            "statement": "This image debt is deferred to downstream look development.",
            "decision_strength": "approved_start",
        },
    }]
    _write(payload, data)

    with pytest.raises(ValueError, match="preserve the authored requirement statement exactly"):
        validate_materialization(
            bundle.root, payload, expected_bundle_hash=bundle.content_hash
        )


def test_materialization_requires_and_retains_every_requirement_domain(
    tmp_path: Path,
) -> None:
    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    requirements = json.loads((tmp_path / "requirements.json").read_text(encoding="utf-8"))
    requirements["requirements"][0]["resolution"] = _deferred_owner(
        "2", "image", "scene"
    )
    _write(tmp_path / "requirements.json", requirements)
    layout = run_artifacts.create(tmp_path, "domain-and")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    payload = _jit_payload(tmp_path, bundle.content_hash)
    data = json.loads(payload.read_text(encoding="utf-8"))
    data["scene_contracts"].append({
        "id": "polish-count",
        "kind": "object_count",
        "owner_layer": "2",
        "fault_owner": "2",
        "activates_at": "2",
        "lifecycle": "layer",
        "axis": "final_lock",
        "roles": ["polish.comp"],
        "op": "min",
        "lo": 1,
    })
    data["layer"]["stages"][0]["evaluation"]["claims"].append({
        "id": "polish-count-claim",
        "proposition": "the polish subject exists",
        "axis": "final_lock",
        "property": "object_count",
        "subject_roles": ["polish.comp"],
        "subject_controls": [],
        "moments": [239, 240],
        "kind": "atomic",
        "required": True,
        "authority": "executable_required",
        "repair_owner": "polish",
        "asserts": "scene",
        "evidence": [{"kind": "scene_contract", "id": "polish-count"}],
    })
    data["requirement_bindings"] = [{
        "requirement_id": "R-final-lock",
        "contract_ids": ["polish-count"],
    }]
    _write(payload, data)

    with pytest.raises(ValueError, match=r"does not pay \['image'\]"):
        validate_materialization(
            bundle.root, payload, expected_bundle_hash=bundle.content_hash
        )

    data["requirement_bindings"][0]["decision"] = {
        "statement": "frames 239 and 240 are unchanged",
        "decision_strength": "approved_start",
        "judgment": _judgment(),
    }
    data["layer"]["stages"][0]["provides"] = ["geometry"]
    _write(payload, data)
    materialized = validate_materialization(
        bundle.root, payload, expected_bundle_hash=bundle.content_hash
    )

    assert materialized.requirement_evidence_domains["R-final-lock"] == (
        "image",
        "scene",
    )
    definition = materialized.judgment_debt_definitions[0]
    assert materialized.requirement_domain_bindings["R-final-lock"] == (
        {
            "domain": "image",
            "kind": "provisional_decision",
            "statement": "frames 239 and 240 are unchanged",
            "decision_strength": "approved_start",
            "debt_id": definition["debt_id"],
            "definition_digest": definition["definition_digest"],
            "activates_at": "2",
        },
        {"domain": "scene", "kind": "contract", "ids": ["polish-count"]},
    )
    assert len(materialized.judgment_debt_activations) == 1

    _passed_layer_one_outcome(tmp_path)
    pointer = _publish_materialization(tmp_path, payload)
    selected = json.loads(
        (tmp_path / json.loads(pointer.read_text(encoding="utf-8"))["artifacts"]["requirements.json"])
        .read_text(encoding="utf-8")
    )
    resolution = selected["requirements"][0]["resolution"]
    assert resolution["evidence_domains"] == ["image", "scene"]
    assert resolution["domain_bindings"] == list(
        materialized.requirement_domain_bindings["R-final-lock"]
    )


def test_gate_rejects_relabelled_requirement_domain_binding(tmp_path: Path) -> None:
    _candidate(tmp_path)
    requirements = json.loads((tmp_path / "requirements.json").read_text(encoding="utf-8"))
    requirements["requirements"][0]["resolution"] = {
        "kind": "contract",
        "ids": ["final-lock"],
        "evidence_domains": ["projected_composition"],
        "domain_bindings": [{
            "domain": "projected_composition",
            "kind": "contract",
            "ids": ["final-lock"],
        }],
    }
    _write(tmp_path / "requirements.json", requirements)

    findings, _ = _check_meta_records(tmp_path)

    assert any(
        finding.check == "requirement-domain-binding"
        and "final-lock=image" in finding.what
        for finding in findings
    )


def test_requirement_binding_rejects_contract_padding_outside_declared_domains(
    tmp_path: Path,
) -> None:
    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    requirements = json.loads((tmp_path / "requirements.json").read_text(encoding="utf-8"))
    requirements["requirements"][0]["resolution"] = _deferred_owner("2", "image")
    _write(tmp_path / "requirements.json", requirements)
    layout = run_artifacts.create(tmp_path, "requirement-padding")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    payload = _jit_payload(tmp_path, bundle.content_hash)
    data = json.loads(payload.read_text(encoding="utf-8"))
    data["scene_contracts"].append({
        "id": "polish-count-padding",
        "kind": "object_count",
        "owner_layer": "2",
        "fault_owner": "2",
        "activates_at": "2",
        "lifecycle": "layer",
        "axis": "final_lock",
        "roles": ["polish.comp"],
        "op": "min",
        "lo": 1,
    })
    data["layer"]["stages"][0]["evaluation"]["claims"].append({
        "id": "polish-count-claim",
        "proposition": "the polish subject exists",
        "axis": "final_lock",
        "property": "object_count",
        "subject_roles": ["polish.comp"],
        "subject_controls": [],
        "moments": [239, 240],
        "kind": "atomic",
        "required": True,
        "authority": "executable_required",
        "repair_owner": "polish",
        "asserts": "scene",
        "evidence": [{"kind": "scene_contract", "id": "polish-count-padding"}],
    })
    data["requirement_bindings"][0]["contract_ids"].append("polish-count-padding")
    data["requirement_bindings"][0]["decision"] = {
        "statement": "frames 239 and 240 are unchanged",
        "decision_strength": "approved_start",
    }
    _write(payload, data)

    with pytest.raises(ValueError, match="padding contract bindings"):
        validate_materialization(
            bundle.root, payload, expected_bundle_hash=bundle.content_hash
        )


def test_selected_requirement_map_refuses_unassigned_resolution_ids(
    tmp_path: Path,
) -> None:
    _candidate(tmp_path)
    requirements = json.loads((tmp_path / "requirements.json").read_text(encoding="utf-8"))
    requirements["requirements"][0]["resolution"] = {
        "kind": "contract",
        "ids": ["final-lock", "unassigned-padding"],
        "evidence_domains": ["image"],
        "domain_bindings": [{
            "domain": "image",
            "kind": "contract",
            "ids": ["final-lock"],
        }],
    }
    _write(tmp_path / "requirements.json", requirements)

    with pytest.raises(ValueError, match=r"unassigned \['unassigned-padding'\]"):
        load_requirements(tmp_path)


def test_selected_provisional_domain_binding_preserves_authored_proposition(
    tmp_path: Path,
) -> None:
    _candidate(tmp_path)
    requirements = json.loads((tmp_path / "requirements.json").read_text(encoding="utf-8"))
    requirements["requirements"][0]["resolution"] = {
        "kind": "decision",
        "ids": [],
        "decision": "This debt is deferred to a downstream layer.",
        "decision_strength": "approved_start",
        "evidence_domains": ["image"],
        "domain_bindings": [{
            "domain": "image",
            "kind": "provisional_decision",
            "statement": "This debt is deferred to a downstream layer.",
            "decision_strength": "approved_start",
        }],
    }
    _write(tmp_path / "requirements.json", requirements)

    with pytest.raises(ValueError, match="provisional debt cannot rewrite the proposition"):
        load_requirements(tmp_path)


def test_direct_required_bbox_claims_are_projected_composition_context(
    tmp_path: Path,
) -> None:
    from vfx_harness.evaluation.plan_gate import _check_evidence_coherence

    _candidate(tmp_path)
    data = json.loads((tmp_path / "layers.json").read_text(encoding="utf-8"))
    layer = data["layers"][0]
    layer["evidence_domains"] = ["scene", "temporal", "projected_composition"]
    unit = layer["stages"][0]
    unit["mutates"]["roles"] = ["comp", "camera"]
    unit["evaluation"]["claims"].append({
        "id": "framing-claim", "proposition": "subject stays framed",
        "axis": "final_lock", "property": "bbox_height",
        "subject_roles": ["camera"], "subject_controls": ["hold"],
        "moments": [239, 240], "kind": "atomic", "required": True,
        "authority": "executable_required", "repair_owner": "lock",
        "evidence": [
            {"kind": "scene_contract", "id": "subject-bbox-f239"},
            {"kind": "scene_contract", "id": "subject-bbox-f240"},
        ],
    })
    _write(tmp_path / "layers.json", data)
    checks = json.loads((tmp_path / "scene_checks.json").read_text(encoding="utf-8"))
    for frame in (239, 240):
        checks["contracts"].append({
            "id": f"subject-bbox-f{frame}", "kind": "bbox_height", "owner_layer": "1",
            "fault_owner": "1", "activates_at": "1", "lifecycle": "layer",
            "axis": "final_lock", "roles": ["comp"], "frame": frame,
            "op": "band", "lo": 0.4, "hi": 0.9,
        })
    _write(tmp_path / "scene_checks.json", checks)

    findings, _ = _check_evidence_coherence(tmp_path)
    assert not [f for f in findings if f.check == "composition-coverage"], (
        "a required claim bound straight to bbox contracts at the judge frames is "
        "executable projected context; composition_context is one valid spelling, not the only one"
    )

    stripped = json.loads((tmp_path / "layers.json").read_text(encoding="utf-8"))
    stripped["layers"][0]["stages"][0]["evaluation"]["claims"] = [
        claim
        for claim in stripped["layers"][0]["stages"][0]["evaluation"]["claims"]
        if claim["id"] != "framing-claim"
    ]
    _write(tmp_path / "layers.json", stripped)
    findings, _ = _check_evidence_coherence(tmp_path)
    assert [f for f in findings if f.check == "composition-coverage"]


def test_selected_revert_is_retired_and_preserves_live_authority(
    tmp_path,
    monkeypatch,
) -> None:
    """A reverted design base cannot become live without a gated replacement."""
    from vfx_harness.orchestration.jit_materialization import revert_materialization

    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    pointer = tmp_path / "state" / "jit-layers" / "current.json"
    pointer.parent.mkdir(parents=True)
    pointer.write_bytes(b'{"sentinel":"live-authority"}\n')
    before = pointer.read_bytes()

    with pytest.raises(
        MaterializationSelectionConflict,
        match="selected materialization revert is retired",
    ):
        revert_materialization(tmp_path, "2")

    assert pointer.read_bytes() == before


def test_selected_revert_fails_before_view_or_pointer_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vfx_harness.orchestration.jit_materialization import revert_materialization

    jit_publish = importlib.import_module(
        "vfx_harness.orchestration.jit_materialization.publish"
    )
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    pointer = tmp_path / "state" / "jit-layers" / "current.json"
    pointer.parent.mkdir(parents=True)
    pointer.write_bytes(b'{"sentinel":"live-authority"}\n')
    before = pointer.read_bytes()
    view_parent = tmp_path / "state" / "jit-layers" / "views"
    before_views: tuple[Path, ...] = ()

    def forbidden_write(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("selected revert reached a retired write boundary")

    monkeypatch.setattr(
        plan_bundle_integrity,
        "_fsync_directory",
        forbidden_write,
    )
    monkeypatch.setattr(
        jit_publish,
        "durably_install_or_flush_view_directory",
        forbidden_write,
    )

    with pytest.raises(
        MaterializationSelectionConflict,
        match="selected materialization revert is retired",
    ):
        revert_materialization(tmp_path, "2")

    assert pointer.read_bytes() == before
    assert (
        tuple(sorted(view_parent.iterdir())) if view_parent.is_dir() else ()
    ) == before_views


def test_materialization_publication_reports_live_input_race_after_atomic_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A committed authority transition is never erased after its visibility boundary."""

    jit_publish = importlib.import_module(
        "vfx_harness.orchestration.jit_materialization.publish"
    )
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    layout = run_artifacts.create(tmp_path, "jit-publication-input-race")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    candidate = _jit_payload(tmp_path, bundle.content_hash)
    _passed_layer_one_outcome(tmp_path)
    _attest_materialization(tmp_path, candidate)
    plan_pointer = tmp_path / "plans" / "current.json"
    plan_predecessor = plan_pointer.read_bytes()
    jit_pointer = tmp_path / "state" / "jit-layers" / "current.json"
    real_commit = jit_publish.commit_prepared_authority_state_transition_locked

    def commit_then_mutate_authored_input(*args, **kwargs):
        result = real_commit(*args, **kwargs)
        (tmp_path / "brief.md").write_text(
            "Changed authored intent after the atomic JIT commit.\n",
            encoding="utf-8",
        )
        return result

    monkeypatch.setattr(
        jit_publish,
        "commit_prepared_authority_state_transition_locked",
        commit_then_mutate_authored_input,
    )

    with pytest.raises(
        MaterializationSelectionConflict,
        match="materialization publication selection conflict",
    ):
        publish_materialization(tmp_path, candidate)

    assert plan_pointer.read_bytes() == plan_predecessor
    assert jit_pointer.is_file()
    assert not (tmp_path / "state" / "authority-state" / "pending.json").exists()


def test_materialization_publication_reports_decision_race_after_atomic_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The append-only plan verifier cannot hide a newer gate-input generation."""

    jit_publish = importlib.import_module(
        "vfx_harness.orchestration.jit_materialization.publish"
    )
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    layout = run_artifacts.create(tmp_path, "jit-publication-decision-race")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    candidate = _jit_payload(tmp_path, bundle.content_hash)
    _passed_layer_one_outcome(tmp_path)
    _attest_materialization(tmp_path, candidate)
    plan_pointer = tmp_path / "plans" / "current.json"
    plan_predecessor = plan_pointer.read_bytes()
    jit_pointer = tmp_path / "state" / "jit-layers" / "current.json"
    real_commit = jit_publish.commit_prepared_authority_state_transition_locked

    def commit_then_append_decision(*args, **kwargs):
        result = real_commit(*args, **kwargs)
        resolutions = tmp_path / "state" / "plan-resolutions.jsonl"
        resolutions.parent.mkdir(parents=True, exist_ok=True)
        with resolutions.open("ab") as handle:
            handle.write(b"{}\n")
        return result

    monkeypatch.setattr(
        jit_publish,
        "commit_prepared_authority_state_transition_locked",
        commit_then_append_decision,
    )

    with pytest.raises(
        MaterializationSelectionConflict,
        match="materialization publication selection conflict",
    ):
        publish_materialization(tmp_path, candidate)

    assert plan_pointer.read_bytes() == plan_predecessor
    assert jit_pointer.is_file()
    assert not (tmp_path / "state" / "authority-state" / "pending.json").exists()


def test_selected_revert_preserves_exact_authority_heads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refusal happens before either live authority pointer can be replaced."""

    from vfx_harness.orchestration.jit_materialization import revert_materialization

    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    plan_pointer = tmp_path / "plans" / "current.json"
    plan_pointer.parent.mkdir(parents=True)
    plan_pointer.write_bytes(b'{"sentinel":"plan-head"}\n')
    plan_predecessor = plan_pointer.read_bytes()
    jit_pointer = tmp_path / "state" / "jit-layers" / "current.json"
    jit_pointer.parent.mkdir(parents=True)
    jit_pointer.write_bytes(b'{"sentinel":"jit-head"}\n')
    jit_predecessor = jit_pointer.read_bytes()

    with pytest.raises(
        MaterializationSelectionConflict,
        match="selected materialization revert is retired",
    ):
        revert_materialization(tmp_path, "2", select=True)

    assert plan_pointer.read_bytes() == plan_predecessor
    assert jit_pointer.read_bytes() == jit_predecessor


def test_unselected_revert_leaves_live_pointer(tmp_path, monkeypatch) -> None:
    """Remat must not select the reverted overlay. Run 3af3b7 selected a hole, then
    died on a broken pipe before the replacement published — live authority lost the
    layer, unit state still said passed (HIR-0026)."""
    from vfx_harness.orchestration.jit_materialization import revert_materialization
    from vfx_harness.orchestration.plan_authority import selected_artifact_path

    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    layout = run_artifacts.create(tmp_path, "unselected-revert-run")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    payload = _jit_payload(tmp_path, bundle.content_hash)
    _passed_layer_one_outcome(tmp_path)
    preserved_receipt = unit_state.load(tmp_path, "1")["layer_finalization"][
        "terminal_receipt"
    ]["receipt_digest"]
    _publish_materialization(tmp_path, payload)
    assert (
        unit_state.load(tmp_path, "1")["layer_finalization"]["terminal_receipt"]
        ["receipt_digest"]
        == preserved_receipt
    )

    jit_publish = importlib.import_module(
        "vfx_harness.orchestration.jit_materialization.publish"
    )
    stored_member_sets: list[set[str]] = []
    original_store = jit_publish.durably_install_or_flush_view_directory

    def recording_store(
        shot: Path,
        root: Path,
        members: dict[str, bytes],
    ) -> Path:
        stored_member_sets.append(set(members))
        return original_store(shot, root, members)

    monkeypatch.setattr(
        jit_publish,
        "durably_install_or_flush_view_directory",
        recording_store,
    )
    pointer = tmp_path / "state" / "jit-layers" / "current.json"
    before = pointer.read_bytes()
    overlay = revert_materialization(tmp_path, "2", select=False)

    assert overlay is not None
    assert stored_member_sets == [{
        "layers.json",
        "scene_checks.json",
        "checks.json",
        "requirements.json",
        "acceptance.json",
        ".authority-base.json",
    }]
    assert pointer.read_bytes() == before
    overlay_layers = load_layers_from_path(overlay / "layers.json")
    assert overlay_layers["2"].execution == "jit_deferred"
    live = load_layers_from_path(selected_artifact_path(tmp_path, "layers.json"))
    assert live["2"].execution == "ready"

    overlay_register = json.loads((overlay / "requirements.json").read_text(encoding="utf-8"))
    overlay_kind = {
        row["id"]: row["resolution"]["kind"]
        for row in overlay_register["requirements"]
        if row["id"] == "R-final-lock"
    }
    assert overlay_kind == {"R-final-lock": "deferred_owner"}

    # A replacement candidate is authored against the still-selected full view. Its
    # base token therefore includes the first JIT publication, while the unpublished
    # revert overlay carries that same token without selecting a hole.
    candidate = json.loads(payload.read_text(encoding="utf-8"))
    candidate["base_selection"] = _base_selection(tmp_path).to_dict()
    _write(payload, candidate)

    with pytest.raises(ValueError, match="already resolved concretely"):
        _publish_materialization(tmp_path, payload)
    assert pointer.read_bytes() == before

    published = _publish_materialization(tmp_path, payload, overlay_root=overlay)
    assert published == pointer
    selected = json.loads(pointer.read_text(encoding="utf-8"))
    assert "2" in selected["materialized_layers"]
    after = load_layers_from_path(selected_artifact_path(tmp_path, "layers.json"))
    assert after["2"].execution == "ready"


def test_selected_revert_names_the_only_legal_replacement_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rejection teaches the gated overlay-to-publication transaction."""
    from vfx_harness.orchestration.jit_materialization import revert_materialization

    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    with pytest.raises(MaterializationSelectionConflict) as exc_info:
        revert_materialization(tmp_path, "2", select=True)

    assert str(exc_info.value) == (
        "selected materialization revert is retired; prepare and gate a replacement "
        "with select=False, then publish that candidate atomically"
    )


def test_unselected_revert_ignores_stale_view_after_republished_bundle(
    tmp_path, monkeypatch
) -> None:
    """A live JIT view belongs to one immutable global generation.

    Global republication atomically retires the JIT head, so the newly selected sparse
    bundle is already the effective authority and there is nothing to revert. The prior
    content-addressed view remains immutable audit state without remaining selected.
    """
    from vfx_harness.orchestration.jit_materialization import revert_materialization

    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    first_layout = run_artifacts.create(tmp_path, "first-generation")
    first_bundle = publish_current(tmp_path, first_layout, outcome="clean_with_deferred")
    _passed_layer_one_outcome(tmp_path)
    _publish_materialization(tmp_path, _jit_payload(tmp_path, first_bundle.content_hash))

    pointer = tmp_path / "state" / "jit-layers" / "current.json"
    before = pointer.read_bytes()
    old_view = json.loads(before)
    assert old_view["bundle_hash"] == first_bundle.content_hash
    assert old_view["materialized_layers"] == ["1", "2"]
    old_view_root = (tmp_path / next(iter(old_view["artifacts"].values()))).parent

    (tmp_path / "plans" / "global.md").write_text(
        "# republished sparse authority\n", encoding="utf-8"
    )
    second_layout = run_artifacts.create(tmp_path, "second-generation")
    second_bundle = publish_current(tmp_path, second_layout, outcome="clean_with_deferred")
    assert second_bundle.content_hash != first_bundle.content_hash

    overlay = revert_materialization(tmp_path, "2", select=False)

    assert overlay is None
    assert not pointer.exists()
    assert old_view_root.is_dir()
    selected = resolve_selected_authority(tmp_path)
    assert selected.assertion.effective_view is not None
    assert selected.assertion.effective_view.source == "bundle"
    selected_layers = load_layers_from_path(selected.artifact_paths["layers.json"])
    assert selected_layers["2"].execution == "jit_deferred"
    assert selected_layers["2"].stages == ()
    selected_register = json.loads(
        selected.artifact_paths["requirements.json"].read_text(encoding="utf-8")
    )
    assert {
        row["id"]: row["resolution"]["kind"]
        for row in selected_register["requirements"]
        if row["id"] == "R-final-lock"
    } == {"R-final-lock": "deferred_owner"}


def test_unselected_revert_of_last_layer_does_not_unlink_pointer(
    tmp_path, monkeypatch
) -> None:
    """select=False must still return the overlay and must not unlink current.json
    even when the reverted view has no remaining materialized layers."""
    from vfx_harness.orchestration.jit_materialization import revert_materialization

    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    _candidate(tmp_path)
    document = json.loads((tmp_path / "layers.json").read_text(encoding="utf-8"))
    ready_layer = document["layers"][0]
    deferred_layer = {**ready_layer, "execution": "jit_deferred", "stages": []}
    deferred_layer["jit"] = {
        "depends_on_layers": [],
        "required_outcomes": [],
        "reserved_roles": ["comp"],
        "owned_requirements": ["R-final-lock"],
    }
    document["schema"] = 5
    document["layers"] = [deferred_layer]
    _write(tmp_path / "layers.json", document)
    _write(tmp_path / "scene_checks.json", {"schema": 2, "contracts": []})
    requirements = json.loads((tmp_path / "requirements.json").read_text(encoding="utf-8"))
    requirements["requirements"][0]["resolution"] = _deferred_owner("1", "image")
    _write(tmp_path / "requirements.json", requirements)
    _write(tmp_path / "obligations.json", {
        "schema": "vfx-harness.obligations/v1", "obligations": [],
    })
    layout = run_artifacts.create(tmp_path, "unselected-last-layer")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    payload = tmp_path / "root-jit.json"
    _write(payload, {
        "schema": MATERIALIZATION_SCHEMA,
        "bundle_hash": bundle.content_hash,
        "base_selection": _base_selection(tmp_path).to_dict(),
        "layer": _declaring({**ready_layer, "execution": "ready"}),
        "scene_contracts": [{
            "id": "final-lock", "kind": "frame_delta", "owner_layer": "1",
            "fault_owner": "1", "activates_at": "1", "lifecycle": "layer",
            "axis": "final_lock", "frames": [239, 240], "op": "max", "hi": 0.01,
        }, *_vis_rows("1", (239, 240))],
        "image_contracts": [],
        "requirement_bindings": [{
            "requirement_id": "R-final-lock", "contract_ids": ["final-lock"],
        }],
        "acceptance": [],
    })
    _publish_materialization(tmp_path, payload)

    pointer = tmp_path / "state" / "jit-layers" / "current.json"
    assert pointer.is_file()
    before = pointer.read_bytes()
    overlay = revert_materialization(tmp_path, "1", select=False)

    assert overlay is not None
    assert pointer.is_file()
    assert pointer.read_bytes() == before
    overlay_layers = load_layers_from_path(overlay / "layers.json")
    assert overlay_layers["1"].execution == "jit_deferred"
