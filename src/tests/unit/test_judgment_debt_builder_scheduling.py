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
    definition, activation, state = _due_debt(
        requirement_id="R-hall-read",
        debt_subject="hall",
        frame=40,
    )
    decision = _load_one_due_decision("R-hall-read", "hall", 40)
    marked: list[tuple[str, str]] = []
    events: list[str] = []
    terminal_statuses: list[str] = []
    debt_state = "pending_not_due"
    claim = SimpleNamespace(
        mode="composed",
        claim_id="lfc-form",
        layer_script_path=layer.script,
        layer_script_sha256="a" * 64,
        predecessor_inputs=(),
    )
    replay_receipt = SimpleNamespace(
        receipt_digest="b" * 64,
        layer_script_sha256="a" * 64,
        created_at="2026-09-01T00:00:00+00:00",
        claim=claim,
        observation=None,
    )
    stored_replay = SimpleNamespace(
        receipt=replay_receipt,
        locator="runs/test/checkpoints/layer-finalizations/lfc-form.json",
        sha256="c" * 64,
    )
    terminal_receipt = SimpleNamespace(
        receipt_digest="d" * 64,
        completed_at="2026-09-01T00:00:00+00:00",
        layer_script_path=layer.script,
        layer_script_sha256="a" * 64,
        claim=claim,
        evaluation_receipt=None,
        projection={"revalidation": {"schema": "fixture-revalidation"}},
    )

    class FakeGuard:
        def __init__(self, authority) -> None:
            self.claim = claim
            self.authority = authority

        @property
        def label(self) -> str:
            return "fixture finalization guard"

        @property
        def authority_binding(self) -> dict:
            return {"fixture": True}

        def check(self, _operation):
            return self.authority

        def publish(self, _operation, mutation):
            return mutation()

    class FakeLedger:
        def __init__(self, _shot, *_args, **_kwargs) -> None:
            self.slots: dict[str, dict] = {}

        def _slot(self, milestone) -> dict:
            return self.slots.setdefault(milestone.id, {})

        def begin(self, milestone) -> None:
            self._slot(milestone)["attempt"] = 1

        def save(self) -> None:
            events.append("ledger_projection")

    async def axes(*_args):
        return [("reference_match", "reference identity")]

    async def no_signal_verify(
        *_args,
        active_unit,
        out_verdicts,
        on_replay_ready,
        on_observation_ready,
        **_kwargs,
    ) -> str:
        assert active_unit.provisional_debt_ids == (decision["debt_id"],)
        assert on_replay_ready is not None
        assert on_observation_ready is not None
        replay_context = on_replay_ready(())
        sealed, payment = on_observation_ready(
            (),
            (
                {
                    "frame": 40,
                    "ref": "refs/hall.png",
                    "render": "runs/test/evidence/renders/no-signal.png",
                    "render_capture": {"png_sha256": "f" * 64},
                    "evidence": (),
                    "motion_evidence": None,
                },
            ),
            replay_context,
        )
        assert sealed is replay_receipt
        assert payment is not None
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
        replay_receipt,
        **_kwargs,
    ) -> None:
        nonlocal debt_state
        assert "terminal_receipt" in events
        marked.append((digest, layer_id))
        assert tuple(map(tuple, replay_receipt.unit_digests)) == (
            ("2:hall_form", "1" * 64),
        )
        events.append("debt_projection")
        debt_state = "due"

    monkeypatch.setattr(layer_runtime, "AuthorityBoundLedger", FakeLedger)
    monkeypatch.setattr(
        layer_runtime,
        "selected_layer_capsule_digest",
        lambda *_args: _PLAN_HASH,
    )
    monkeypatch.setattr(
        layer_runtime,
        "authorize_completed_units_for_layer",
        lambda *_args, **_kwargs: SimpleNamespace(
            unit_ids=frozenset({"hall_form", "hall_detail"}),
        ),
    )
    monkeypatch.setattr(layer_runtime, "_prior_layer_paths", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(layer_runtime, "load_layers", lambda *_args, **_kwargs: {"2": layer})
    monkeypatch.setattr(layer_runtime, "plan_strips", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(layer_runtime, "ensure_axes", axes)
    monkeypatch.setattr(
        layer_runtime,
        "_load_provisional_decisions",
        lambda *_args, **_kwargs: (decision,),
    )
    monkeypatch.setattr(
        layer_runtime,
        "current_judgment_debt_states_for_authority",
        lambda *_args, **_kwargs: ((definition, activation, state),),
    )
    monkeypatch.setattr(
        layer_runtime,
        "payment_generation_for_replay",
        lambda selected_definition, selected_activation, _prefix: SimpleNamespace(
            digest="9" * 64
        )
        if (selected_definition, selected_activation) == (definition, activation)
        else pytest.fail("payment generation used another debt authority"),
    )
    monkeypatch.setattr(layer_runtime, "_unit_raster_mode", lambda _unit: "solid")
    monkeypatch.setattr(
        layer_runtime,
        "replay_prefix_receipt",
        lambda *_args, **_kwargs: SimpleNamespace(
            unit_digests=(("2:hall_form", "1" * 64),),
            as_dict=lambda: {"schema": "fixture-replay-prefix"},
        ),
    )
    monkeypatch.setattr(
        layer_runtime,
        "require_replay_inputs_unchanged",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        layer_runtime,
        "prepare_replay_inputs",
        lambda *_args, **_kwargs: ((), ()),
    )
    monkeypatch.setattr(layer_runtime, "_verify_script", no_signal_verify)
    monkeypatch.setattr(
        layer_runtime,
        "current_layer_finalization_receipt",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        layer_runtime,
        "claim_layer_finalization",
        lambda *_args, layer_script_sha256, **_kwargs: (
            setattr(claim, "layer_script_sha256", layer_script_sha256) or claim
        ),
    )
    claim_guard = FakeGuard(claim)
    monkeypatch.setattr(
        layer_runtime.LayerFinalizationClaimGuard,
        "bind",
        lambda *_args, **_kwargs: claim_guard,
    )
    monkeypatch.setattr(
        layer_runtime,
        "prepare_layer_artifact",
        lambda *_args, **_kwargs: SimpleNamespace(sha256=claim.layer_script_sha256),
    )
    monkeypatch.setattr(layer_runtime, "commit_layer_artifact", lambda *_args: None)
    monkeypatch.setattr(layer_runtime, "discard_layer_artifact", lambda *_args: None)

    def mint_replay(**kwargs):
        replay_receipt.observation = kwargs["observation"]
        return replay_receipt

    monkeypatch.setattr(
        layer_runtime,
        "LayerReplayPointObservation",
        SimpleNamespace(mint=lambda **kwargs: SimpleNamespace(**kwargs)),
    )
    monkeypatch.setattr(
        layer_runtime,
        "LayerReplayObservation",
        lambda **kwargs: SimpleNamespace(execution_status="passed", **kwargs),
    )
    monkeypatch.setattr(
        layer_runtime,
        "LayerReplayReceipt",
        SimpleNamespace(mint=mint_replay),
    )
    monkeypatch.setattr(
        layer_runtime,
        "prepare_layer_replay_receipt",
        lambda *_args, **_kwargs: SimpleNamespace(receipt=replay_receipt),
    )
    monkeypatch.setattr(
        layer_runtime,
        "commit_layer_replay_receipt",
        lambda *_args, **_kwargs: events.append("replay_receipt") or stored_replay,
    )
    monkeypatch.setattr(
        layer_runtime,
        "discard_layer_replay_receipt",
        lambda *_args, **_kwargs: None,
    )

    monkeypatch.setattr(
        layer_runtime,
        "JudgmentDebtPayment",
        lambda **_kwargs: SimpleNamespace(deferred_payment_attempt_failures=()),
    )
    evaluation_receipt = SimpleNamespace(
        receipt_digest="1" * 64,
        final_status="failed",
        claim=claim,
    )

    def mint_evaluation(**kwargs):
        groups = kwargs["evaluation_groups"]
        assert len(kwargs["replay_receipts"]) == 1
        assert [row["result"] for row in groups] == ["failed"]
        evaluation_receipt.replay_receipts = kwargs["replay_receipts"]
        evaluation_receipt.evaluation_groups = groups
        evaluation_receipt.canonical = kwargs["canonical"]
        return evaluation_receipt

    monkeypatch.setattr(
        layer_runtime,
        "LayerReplayReceiptBinding",
        SimpleNamespace(mint=lambda **kwargs: SimpleNamespace(**kwargs)),
    )
    monkeypatch.setattr(
        layer_runtime,
        "LayerEvaluationReceipt",
        SimpleNamespace(mint=mint_evaluation),
    )
    monkeypatch.setattr(
        layer_runtime,
        "prepare_layer_evaluation_receipt",
        lambda *_args, **_kwargs: SimpleNamespace(receipt=evaluation_receipt),
    )
    monkeypatch.setattr(
        layer_runtime,
        "commit_layer_evaluation_receipt",
        lambda *_args, **_kwargs: events.append("evaluation_receipt")
        or SimpleNamespace(
            receipt=evaluation_receipt,
            locator=(
                "runs/test/checkpoints/layer-finalizations/"
                "lfc-form/evaluation.json"
            ),
            sha256="2" * 64,
        ),
    )
    monkeypatch.setattr(
        layer_runtime,
        "discard_layer_evaluation_receipt",
        lambda *_args, **_kwargs: None,
    )

    def mint_terminal(**kwargs):
        evaluation = kwargs["evaluation_receipt"]
        terminal_statuses.append(evaluation.final_status)
        terminal_receipt.evaluation_receipt = evaluation
        terminal_receipt.projection = kwargs["projection"]
        return terminal_receipt

    monkeypatch.setattr(
        layer_runtime,
        "LayerFinalizationReceipt",
        SimpleNamespace(mint=mint_terminal),
    )
    monkeypatch.setattr(
        layer_runtime,
        "complete_layer_finalization",
        lambda *_args, **_kwargs: events.append("terminal_receipt"),
    )
    prepared_revalidation = SimpleNamespace(
        source_sha256="e" * 64,
        result={"kept": 0, "dropped": []},
    )
    monkeypatch.setattr(
        layer_runtime,
        "prepare_layer_revalidation",
        lambda *_args, **_kwargs: prepared_revalidation,
    )
    monkeypatch.setattr(
        layer_runtime,
        "layer_revalidation_projection",
        lambda _prepared: terminal_receipt.projection["revalidation"],
    )
    monkeypatch.setattr(
        layer_runtime,
        "build_layer_outcome_projection",
        lambda *_args, **_kwargs: SimpleNamespace(
            as_dict=lambda: {"schema": "fixture-outcome-projection"}
        ),
    )
    monkeypatch.setattr(
        layer_runtime,
        "discard_layer_revalidation",
        lambda *_args, **_kwargs: None,
    )
    def reconcile(_shot, _layer, receipt, **_kwargs):
        assert "terminal_receipt" in events
        events.append("revalidation_projection")
        assert len(receipt.projection["judgment_debts"]) == 1
        debt = receipt.projection["judgment_debts"][0]
        assert debt["resolution"] is None
        assert "replayed_unit_digests" not in debt
        mark_due(
            tmp_path,
            debt["decision"]["definition_digest"],
            layer_id="2",
            replay_receipt=SimpleNamespace(
                unit_digests=(("2:hall_form", "1" * 64),)
            ),
        )
        events.extend(["outcome_projection", "ledger_projection"])
        return SimpleNamespace(
            revalidation={"kept": 0, "dropped": []},
            finding=None,
            outcome=tmp_path / "runs/test/reports/layers/2.json",
            ledger=SimpleNamespace(),
        )

    monkeypatch.setattr(layer_runtime, "reconcile_layer_finalization", reconcile)
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
    assert terminal_statuses == ["failed"]
    assert events == [
        "replay_receipt",
        "evaluation_receipt",
        "terminal_receipt",
        "revalidation_projection",
        "debt_projection",
        "outcome_projection",
        "ledger_projection",
    ]


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
