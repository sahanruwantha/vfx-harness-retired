"""Builder scheduling preserves typed HIR-0163 debt boundaries."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

import anyio
import pytest

from tests.unit_attempt_fixtures import pass_unit
from vfx_harness.agents import acceptance
from vfx_harness.agents.builder import layer as layer_runtime
from vfx_harness.agents.builder.verdicts import (
    _composition_judge_unit,
    _load_provisional_decisions,
)
from vfx_harness.domain.judgment_debts import (
    JudgmentDebtActivation,
    JudgmentDebtSeed,
    JudgmentDebtState,
    JudgmentPoint,
    JudgmentProvider,
    compile_judgment_debt,
)
from vfx_harness.domain.work_units import WorkUnit
from vfx_harness.orchestration.ledger import Layer
from vfx_harness.orchestration.unit_state import initialize

_PLAN_HASH = "1" * 64


def _due_debt(
    *,
    requirement_id: str,
    debt_subject: str,
    frame: int,
) -> tuple:
    definition = compile_judgment_debt(
        JudgmentDebtSeed(
            requirement_id=requirement_id,
            statement=f"{debt_subject} matches its authored reference.",
            decision_strength="approved_start",
            claim_kind="atomic",
            property="reference_identity",
            owner_layer="1",
            fault_owner="camera",
            subject_roles=(debt_subject,),
            axes=("reference_match",),
            judge_points=(JudgmentPoint(frame=frame, ref=f"refs/{debt_subject}.png"),),
            observation_medium="workbench_solid",
            lifecycle="persistent",
            bundle_digest=hashlib.sha256(b"builder-debt-schedule").hexdigest(),
            carrier_families=("mesh",),
        ),
        (
            JudgmentProvider(
                id=f"unit:2:{debt_subject}:mesh",
                layer_id="2",
                carrier_family="mesh",
                subject_roles=(f"{debt_subject}.mass",),
            ),
        ),
        layer_dependencies={"1": (), "2": ("1",)},
        layer_order=("1", "2"),
    )
    activation = JudgmentDebtActivation.for_definition(
        definition,
        payer_unit_digests=((f"2:{debt_subject}", hashlib.sha256(debt_subject.encode()).hexdigest()),),
    )
    return definition, activation, JudgmentDebtState.pending(definition)


def _lookless_layer() -> SimpleNamespace:
    executable_claim = SimpleNamespace(
        id="form-exists",
        required=True,
        moments=(40,),
        authority="executable_required",
        axis="form",
    )
    unit = SimpleNamespace(
        id="hall_form",
        look_capabilities=(),
        mutates=SimpleNamespace(roles=("unrelated.scaffold",), controls=()),
        evaluation=SimpleNamespace(claims=(executable_claim,)),
    )
    return SimpleNamespace(
        id="2",
        stages=(unit,),
        judges=(),
        owns=("form",),
    )


def test_due_builder_debt_exposes_exact_subject_roles_and_owner_points(
    monkeypatch,
    tmp_path,
) -> None:
    definition, activation, state = _due_debt(
        requirement_id="R-hall-read",
        debt_subject="hall",
        frame=40,
    )
    monkeypatch.setattr(
        "vfx_harness.agents.builder.verdicts.current_judgment_debt_states",
        lambda _folder: ((definition, activation, state),),
    )

    decisions = _load_provisional_decisions(SimpleNamespace(folder=tmp_path), "2")

    assert decisions == (
        {
            "id": "R-hall-read",
            "debt_id": definition.debt_id,
            "definition_digest": definition.digest,
            "activation_digest": activation.digest,
            "statement": "hall matches its authored reference.",
            "decision_strength": "approved_start",
            "evidence_domains": ("image",),
            "claim_kind": "atomic",
            "property": "reference_identity",
            "fault_owner": "camera",
            "subject_roles": ("hall",),
            "axes": ("reference_match",),
            "judge_points": ((40, "refs/hall.png"),),
            "carrier_families": ("mesh",),
            "observation_medium": "workbench_solid",
            "lifecycle": "persistent",
            "state": "pending_not_due",
        },
    )


def test_each_due_debt_gets_an_independent_composition_judgment_unit() -> None:
    layer = _lookless_layer()
    first = _load_one_due_decision("R-hall-read", "hall", 40)
    second = _load_one_due_decision("R-tower-read", "tower", 72)

    scheduled = tuple(_composition_judge_unit(layer, (decision,)) for decision in (first, second))

    assert len(scheduled) == 2
    assert [unit.provisional_debt_ids for unit in scheduled] == [
        (first["debt_id"],),
        (second["debt_id"],),
    ]
    assert [
        tuple(
            claim.subject_roles
            for claim in unit.evaluation.claims
            if claim.authority == "qualified_qualitative_required"
        )
        for unit in scheduled
    ] == [(("hall",),), (("tower",),)]
    assert [tuple((point.frame, point.ref) for point in unit.evaluation.judges) for unit in scheduled] == [
        ((40, "refs/hall.png"),),
        ((72, "refs/tower.png"),),
    ]


def _load_one_due_decision(requirement_id: str, subject: str, frame: int) -> dict:
    definition, activation, state = _due_debt(
        requirement_id=requirement_id,
        debt_subject=subject,
        frame=frame,
    )
    return {
        "id": requirement_id,
        "debt_id": definition.debt_id,
        "definition_digest": definition.digest,
        "activation_digest": activation.digest,
        "statement": definition.seed.statement,
        "decision_strength": definition.seed.decision_strength,
        "evidence_domains": ("image",),
        "claim_kind": definition.seed.claim_kind,
        "property": definition.seed.property,
        "fault_owner": definition.seed.fault_owner,
        "subject_roles": definition.seed.subject_roles,
        "axes": definition.seed.axes,
        "judge_points": tuple((point.frame, point.ref) for point in definition.seed.judge_points),
        "carrier_families": definition.seed.carrier_families,
        "observation_medium": definition.seed.observation_medium,
        "lifecycle": definition.seed.lifecycle,
        "state": state.status,
    }


def test_no_signal_composition_marks_due_but_does_not_resolve_debt(
    tmp_path,
    monkeypatch,
) -> None:
    layer = _composition_layer(tmp_path)
    initialize(tmp_path, "2", layer.stages, plan_hash=_PLAN_HASH)
    for unit in layer.stages:
        pass_unit(
            tmp_path,
            "2",
            unit,
            layer.stages,
            plan_hash=_PLAN_HASH,
        )
    decision = _load_one_due_decision("R-hall-read", "hall", 40)
    marked: list[tuple[str, str]] = []
    debt_state = "pending_not_due"

    class FakeLedger:
        def __init__(self, _shot) -> None:
            self.slots: dict[str, dict] = {}

        def _slot(self, milestone) -> dict:
            return self.slots.setdefault(milestone.id, {})

        def begin(self, milestone) -> None:
            self._slot(milestone)["attempt"] = 1

        def mark(self, _milestone, _status, *, best) -> None:
            return None

    async def axes(*_args):
        return [("reference_match", "reference identity")]

    async def no_signal_verify(
        *_args,
        active_unit,
        out_verdicts,
        on_replay_ready,
        **_kwargs,
    ) -> str:
        assert active_unit.provisional_debt_ids == (decision["debt_id"],)
        assert on_replay_ready is not None
        on_replay_ready(())
        out_verdicts.append(
            (
                (40, "refs/hall.png"),
                {
                    "mean": 0.0,
                    "pass": False,
                    "decided_by": "no_optical_signal",
                    "contract_gap": False,
                },
            )
        )
        return "failed"

    def mark_due(
        _folder,
        digest,
        *,
        layer_id,
        replayed_unit_digests,
        **_kwargs,
    ) -> None:
        nonlocal debt_state
        marked.append((digest, layer_id))
        assert replayed_unit_digests == (("2:hall_form", "1" * 64),)
        debt_state = "due"

    monkeypatch.setattr(layer_runtime, "Ledger", FakeLedger)
    monkeypatch.setattr(layer_runtime, "active_plan_hash", lambda _folder: _PLAN_HASH)
    monkeypatch.setattr(layer_runtime, "_prior_layer_paths", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(layer_runtime, "load_layers", lambda *_args, **_kwargs: {"2": layer})
    monkeypatch.setattr(layer_runtime, "plan_strips", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(layer_runtime, "ensure_axes", axes)
    monkeypatch.setattr(
        layer_runtime,
        "_load_provisional_decisions",
        lambda *_args, **_kwargs: (decision,),
    )
    monkeypatch.setattr(layer_runtime, "_unit_raster_mode", lambda _unit: "solid")
    monkeypatch.setattr(
        layer_runtime,
        "replay_prefix_receipt",
        lambda *_args, **_kwargs: SimpleNamespace(
            unit_digests=(("2:hall_form", "1" * 64),)
        ),
    )
    monkeypatch.setattr(
        layer_runtime,
        "mark_judgment_debt_due",
        mark_due,
    )
    monkeypatch.setattr(
        layer_runtime,
        "require_replay_inputs_unchanged",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        layer_runtime,
        "resolve_current_judgment_debt",
        lambda *_args, **_kwargs: pytest.fail("no-signal debt must stay due"),
    )
    monkeypatch.setattr(layer_runtime, "_verify_script", no_signal_verify)
    monkeypatch.setattr(
        layer_runtime,
        "publish_composed_layer_outcome",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(layer_runtime, "_blender_version", lambda _session: "test")
    monkeypatch.setattr(layer_runtime.costlog, "bind", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(layer_runtime.costlog, "unbind", lambda: None)
    monkeypatch.setattr(layer_runtime.transcript, "bind", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(layer_runtime.transcript, "unbind", lambda: None)

    async def run() -> None:
        await layer_runtime.build_layer(
            SimpleNamespace(folder=tmp_path),
            layer,
            session=None,
            verbose=False,
        )

    anyio.run(run)

    assert marked == [(decision["definition_digest"], "2")]
    assert debt_state == "due"


def test_acceptance_refuses_unresolved_judgment_debt_before_chain_work(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        acceptance,
        "require_judgment_debts_satisfied",
        lambda _folder, _selected=None: (_ for _ in ()).throw(
            ValueError("R-hall-read=pending_not_due")
        ),
    )

    async def run() -> None:
        with pytest.raises(acceptance.IncompleteChain, match="R-hall-read=pending_not_due"):
            await acceptance.accept(
                SimpleNamespace(folder=tmp_path),
                session=None,
                verbose=False,
            )

    anyio.run(run)


def _composition_layer(root) -> Layer:
    stages = []
    for unit_id, role in (("hall_form", "hall.mass"), ("hall_detail", "hall.detail")):
        artifact = root / "build" / "units" / "02" / f"{unit_id}.py"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text("pass\n", encoding="utf-8")
        stages.append(
            WorkUnit.parse(
                {
                    "id": unit_id,
                    "title": unit_id,
                    "plan": f"plans/02_form/{unit_id}.md",
                    "depends_on": [],
                    "mutates": {
                        "mode": "scoped",
                        "roles": [role],
                        "controls": [],
                        "script_spans": [artifact.relative_to(root).as_posix()],
                    },
                    "protects": {
                        "selector": "all_active_upstream_interfaces",
                        "resolve_to_explicit_ids_at": "freeze",
                    },
                    "look_capabilities": [],
                    "evaluation": {
                        "primary_judge": 40,
                        "judge": [{"frame": 40, "ref": "refs/hall.png"}],
                        "temporal_evidence": "none",
                        "claims": [
                            {
                                "id": f"{unit_id}-exists",
                                "proposition": f"{unit_id} exists",
                                "axis": "form",
                                "property": f"state.{unit_id}",
                                "subject_roles": [role],
                                "subject_controls": [],
                                "moments": [40],
                                "kind": "atomic",
                                "required": True,
                                "authority": "executable_required",
                                "repair_owner": unit_id,
                                "asserts": "scene",
                                "evidence": [
                                    {
                                        "kind": "scene_contract",
                                        "id": f"contract.{unit_id}",
                                    }
                                ],
                            }
                        ],
                    },
                    "completion": "all required claims pass",
                },
                f"composition.{unit_id}",
            )
        )
    return Layer(
        id="2",
        script="build/02_form.py",
        title="Form",
        judges=((40, "refs/hall.png"),),
        reads="form",
        owns=("form",),
        primary_judge=40,
        stages=tuple(stages),
    )
