from __future__ import annotations

from types import SimpleNamespace

from vfx_harness.agents.builder import (
    _ARTIFACT_EVALUATION_BARRIER,
    _candidate_scope_errors,
    _compose_unit_artifact_source,
    _run_artifact_script,
    _run_prior_paths,
    _scope_added_object_errors,
    _scope_bound_evidence,
    _unit_completion_evidence_ids,
    _unit_evidence_ids,
    _unit_evidence_ids_by_frame,
)
from vfx_harness.blender.tools import _bound_static_frames


def test_unit_completion_requires_bindings_from_every_required_moment() -> None:
    unit = SimpleNamespace(
        evaluation=SimpleNamespace(
            claims=(
                SimpleNamespace(
                    required=True,
                    moments=(1,),
                    evidence=(SimpleNamespace(id="frame-1"),),
                ),
                SimpleNamespace(
                    required=True,
                    moments=(36,),
                    evidence=(SimpleNamespace(id="frame-36"),),
                ),
                SimpleNamespace(
                    required=False,
                    moments=(72,),
                    evidence=(SimpleNamespace(id="optional"),),
                ),
            )
        )
    )

    assert _unit_evidence_ids(unit, 1) == {"frame-1"}
    assert _unit_completion_evidence_ids(unit) == {"frame-1", "frame-36"}


def test_active_unit_static_evidence_is_produced_at_every_bound_frame() -> None:
    rows = [
        {"id": "frame-1", "frame": 1, "kind": "bbox_width"},
        {"id": "frame-36", "frame": 36, "kind": "bbox_width"},
        {"id": "functional", "frames": [1, 36], "kind": "frame_delta"},
        {"id": "unrelated", "frame": 72, "kind": "bbox_width"},
    ]

    assert _bound_static_frames(rows, {"frame-1", "frame-36", "functional"}, 1) == [1, 36]


def test_bound_static_frames_honors_temporal_contract_frame_list() -> None:
    rows = [
        {"id": "temporal-static", "frames": [3, 9], "kind": "visible_fraction"},
    ]

    assert _bound_static_frames(rows, {"temporal-static"}, 39) == [3, 9]


def test_candidate_probe_evidence_is_exactly_unit_and_frame_scoped() -> None:
    unit = SimpleNamespace(
        evaluation=SimpleNamespace(
            claims=(
                SimpleNamespace(
                    required=True,
                    moments=(1,),
                    evidence=(SimpleNamespace(id="target-height"),),
                ),
                SimpleNamespace(
                    required=True,
                    moments=(38,),
                    evidence=(SimpleNamespace(id="target-static"),),
                ),
            )
        )
    )
    ids = _unit_evidence_ids_by_frame(
        SimpleNamespace(), None, unit, [(1, "f1.png"), (38, "f38.png")]
    )

    assert ids == {"1": ["target-height"], "38": ["target-static"]}
    rows = [
        {"id": "target-height", "pass": True},
        {"id": "successor-camera-x", "pass": False},
        {"id": "builder-worklist", "source": "builder_state", "pass": True},
    ]
    assert _scope_bound_evidence(rows, set(ids["1"])) == [rows[0], rows[2]]
    assert _scope_bound_evidence(rows, set()) == [rows[2]]
    assert _scope_bound_evidence(rows, None) == rows


def test_artifact_replay_publishes_fresh_state_before_each_successor(tmp_path) -> None:
    import inspect

    from vfx_harness.agents import builder

    class Session:
        def __init__(self):
            self.calls = []

        def run(self, code, *, journal=True, execution_policy=None):
            self.calls.append((code, journal, execution_policy))
            return {"result": "ok"}

    first = tmp_path / "first.py"
    second = tmp_path / "second.py"
    first.write_text("FIRST = True\n", encoding="utf-8")
    second.write_text("SECOND = True\n", encoding="utf-8")
    session = Session()

    assert _run_prior_paths(session, [first, second]) == ["first.py", "second.py"]
    assert session.calls == [
        ("FIRST = True\n", True, "artifact"),
        (_ARTIFACT_EVALUATION_BARRIER, False, None),
        ("SECOND = True\n", True, "artifact"),
        (_ARTIFACT_EVALUATION_BARRIER, False, None),
    ]

    candidate = Session()
    assert _run_artifact_script(candidate, first, journal=False) == {"result": "ok"}
    assert candidate.calls == [
        ("FIRST = True\n", False, "artifact"),
        (_ARTIFACT_EVALUATION_BARRIER, False, None),
    ]
    assert "_run_artifact_script(verify, Path(prior), journal=False)" in inspect.getsource(
        builder._build_probe_candidate_server
    )
    verify_source = inspect.getsource(builder._verify_script)
    assert "_prepare_artifact_replay_inputs" in verify_source
    assert "prepared_replay[-1]" in verify_source

    composed = _compose_unit_artifact_source(
        [
            ("producer", "build/producer.py", "PRODUCER = True\n"),
            ("consumer", "build/consumer.py", "CONSUMER = True\n"),
        ]
    )
    first_barrier = composed.index(_ARTIFACT_EVALUATION_BARRIER.rstrip())
    assert composed.index("PRODUCER = True") < first_barrier
    assert first_barrier < composed.index("CONSUMER = True")
    assert composed.count(_ARTIFACT_EVALUATION_BARRIER.rstrip()) == 2


def test_scoped_artifact_rejects_persisted_untagged_and_undeclared_objects() -> None:
    before = {"upstream": "chamber"}
    after = {
        **before,
        "housing": "iris_housing",
        "TEMP_camera": "",
        "foreign": "camera",
    }

    assert _scope_added_object_errors(before, after, ("iris_housing",)) == [
        "new object 'TEMP_camera' has no bvfx_role",
        "new object 'foreign' has undeclared role 'camera'; allowed ['iris_housing']",
    ]


def test_scoped_artifact_accepts_only_dot_delimited_role_namespace_descendants() -> None:
    before: dict[str, str] = {}
    after = {
        "camera": "camera.main",
        "blade": "iris_blade.segment.01",
        "sibling": "camera_rig.main",
    }

    assert _scope_added_object_errors(before, after, ("camera", "iris_blade")) == [
        "new object 'sibling' has undeclared role 'camera_rig.main'; "
        "allowed ['camera', 'iris_blade']"
    ]


def test_candidate_probe_applies_scope_before_reporting_evidence() -> None:
    before = {"upstream": "cam.blockout"}
    after = {**before, "fabricated_detail": "lookdev.detail.tier_architectural"}

    assert _candidate_scope_errors(
        "scoped", ("lookdev.material.*",), before, after
    ) == [
        "new object 'fabricated_detail' has undeclared role "
        "'lookdev.detail.tier_architectural'; allowed ['lookdev.material.*']"
    ]
    assert _candidate_scope_errors(
        "unrestricted", ("lookdev.material.*",), before, after
    ) == []


def test_frame_scoped_bindings_are_due_at_their_own_frame() -> None:
    """Run 20260825 (detail_instancing): one claim judging [72, 150] bound vis-f72 AND
    vis-f150; each canonical frame faulted the OTHER frame's row as 'not produced'
    although both were green at their own frame. A binding scoped to a sibling judged
    frame is due there; one scoped to a frame no claim moment covers stays loudly due."""
    from types import SimpleNamespace

    from vfx_harness.agents.builder import _executable_unit_verdict

    claim = SimpleNamespace(
        required=True, authority="executable_required", moments=(72, 150),
        evidence=[SimpleNamespace(kind="scene_contract", id="vis-f72"),
                  SimpleNamespace(kind="scene_contract", id="vis-f150")],
    )
    unit = SimpleNamespace(evaluation=SimpleNamespace(claims=[claim]))
    frames = {"vis-f72": 72, "vis-f150": 150}
    evidence_f72 = [{"id": "vis-f72", "pass": True, "value": 0.9}]

    verdict = _executable_unit_verdict(unit, 72, [("a", "axis")], evidence_f72, contract_frames=frames)
    assert verdict is not None and verdict["pass"], verdict

    # a binding scoped to a frame outside every claim moment must stay missing
    stray = {"vis-f72": 72, "vis-f150": 240}
    verdict = _executable_unit_verdict(unit, 72, [("a", "axis")], evidence_f72, contract_frames=stray)
    assert verdict is not None and not verdict["pass"]
    assert verdict["contract_gap"]


def test_declared_binding_moments_outrank_frame_inference() -> None:
    """The binding contract can now SAY which moments it settles; inference from the
    contract's frame is only the fallback for undeclared bindings. Declared moments
    also cannot stray outside the claim's judged set, and a materialization refuses a
    binding declared due where its contract cannot produce."""
    from types import SimpleNamespace

    import pytest as _pytest

    from vfx_harness.agents.builder import _executable_unit_verdict
    from vfx_harness.domain.work_units import Claim

    claim = SimpleNamespace(
        required=True, authority="executable_required", moments=(72, 150),
        evidence=[
            SimpleNamespace(kind="scene_contract", id="vis-f72", moments=(72,)),
            SimpleNamespace(kind="scene_contract", id="vis-f150", moments=(150,)),
        ],
    )
    unit = SimpleNamespace(evaluation=SimpleNamespace(claims=[claim]))
    evidence_f72 = [{"id": "vis-f72", "pass": True, "value": 0.9}]
    verdict = _executable_unit_verdict(unit, 72, [("a", "axis")], evidence_f72, contract_frames={})
    assert verdict is not None and verdict["pass"], verdict

    row = {
        "id": "c", "proposition": "p", "axis": "a", "property": "visible_fraction",
        "subject_roles": ["r"], "subject_controls": [], "moments": [72, 150],
        "kind": "atomic", "required": True, "authority": "executable_required",
        "repair_owner": "u",
        "evidence": [{"kind": "scene_contract", "id": "vis-f72", "moments": [72, 240]}],
    }
    with _pytest.raises(ValueError, match="outside this claim's judged set"):
        Claim.parse(row, "claim")


def test_workunit_shape_changes_demand_a_digest_schema_bump() -> None:
    """unit_digest hashes asdict(WorkUnit), so a field that is always present in the
    payload silently changes every stored digest unless DIGEST_SCHEMA is bumped with
    it (8ab8f5d shipped optional binding moments without the bump; every layer's
    retry refused until the replan). Empty publishes/consumes are omitted so schema-4
    identity stays comparable; non-empty interface rows participate (HIR-0084).
    If this test fails because a new always-present field landed in the payload,
    bump DIGEST_SCHEMA in orchestration/unit_state.py and update BOTH constants
    here together. Default procedural construction is omitted like empty
    publishes/consumes (ADR-0009); a generate/retrieve/simplify route participates."""
    from vfx_harness.domain.work_units import WorkUnit
    from vfx_harness.orchestration.unit_state import DIGEST_SCHEMA, unit_digest

    row = {
        "id": "golden", "title": "Golden", "plan": "plans/units/golden.md",
        "depends_on": [],
        "mutates": {"mode": "scoped", "roles": ["g.role"], "controls": ["g_ctl"],
                    "control_roles": {"g_ctl": ["g.role"]},
                    "script_spans": ["build/units/01/golden.py"]},
        "protects": {"selector": "all_active_upstream_interfaces",
                     "resolve_to_explicit_ids_at": "freeze"},
        "look_capabilities": [],
        "evaluation": {"primary_judge": 1,
                       "judge": [{"frame": 1, "ref": "refs/g.png"}],
                       "temporal_evidence": "none",
                       "claims": [{
                           "id": "g-claim", "proposition": "golden holds",
                           "axis": "g_axis", "property": "object_count",
                           "subject_roles": ["g.role"], "subject_controls": [],
                           "moments": [1], "kind": "atomic", "required": True,
                           "authority": "executable_required", "repair_owner": "golden",
                           "asserts": "scene",
                           "evidence": [{"kind": "scene_contract", "id": "g-count"}],
                       }]},
        "completion": "all_required_claims_and_protected_contracts_pass",
    }
    assert DIGEST_SCHEMA == 5
    assert unit_digest(WorkUnit.parse(row, "golden")) == (
        "d5a8d319ba4482ea592ffd365588f212ba2132bfb0a9d799d0b3b99fdff71678"
    )
