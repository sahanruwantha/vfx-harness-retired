"""Crash/restart coverage for terminal-receipt projection reconciliation.

The terminal receipt is the durable decision boundary.  Every later write is a
reproducible projection of that receipt, so a restart must finish the projection
sequence without scheduling replay, Blender, rendering, or qualitative judgment.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import anyio
import pytest

from tests.integration.test_authority_receipt_lineage import (
    _published_finalized_root,
)
from tests.integration.test_judgment_debt_public_pipeline import (
    _build_prepassed_layer,
    _pass_layer_unit,
    _payload_for_bundle,
    _public_fixture_root,
    _publish_payload,
    _ReplaySession,
    _stash_fixture_capture,
)
from tests.unit.test_judgment_debt_materialization import (
    _camera_payload,
    _form_payload,
)
from vfx_harness.agents.builder import critic as critic_runtime
from vfx_harness.agents.builder import layer as builder_layer
from vfx_harness.agents.builder import layer_finalization_reconcile
from vfx_harness.agents.builder import prior as prior_runtime
from vfx_harness.agents.builder import verify as verify_runtime
from vfx_harness.agents.builder.models import BuildAuthorityDefect
from vfx_harness.domain.brief import load_shot
from vfx_harness.observability import run_artifacts
from vfx_harness.orchestration.judgment_debt_state import (
    current_judgment_debt_states,
)
from vfx_harness.orchestration.judgment_payment_attempts import (
    EVENTS as PAYMENT_ATTEMPT_EVENTS,
)
from vfx_harness.orchestration.layer_outcome_paths import layer_outcome_path
from vfx_harness.orchestration.plan_authority import publish_current
from vfx_harness.orchestration.unit_state import load as load_unit_state


def _remove_mutable_projections(root: Path, layer_id: str) -> None:
    outcome = layer_outcome_path(root, layer_id)
    if outcome.exists():
        outcome.unlink()
    ledger_path = root / "shot.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    ledger["milestones"].pop(layer_id, None)
    ledger_path.write_text(
        json.dumps(ledger, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _forbid_new_finalization(monkeypatch: pytest.MonkeyPatch) -> None:
    async def forbidden(*_args, **_kwargs):
        raise AssertionError("terminal projection recovery scheduled replay, Blender, render, or critic work")

    monkeypatch.setattr(builder_layer, "finalize_composed_layer", forbidden)


def _restart(root: Path, layer, selected) -> None:
    asyncio.run(
        builder_layer.build_layer(
            load_shot(root),
            layer,
            object(),
            selected_authority=selected,
            verbose=False,
        )
    )


def _exact_fixture_executable_evidence(
    _shot,
    layer,
    *_args,
    active_unit=None,
    **_kwargs,
) -> list[dict]:
    """Satisfy the exact evidence ids sealed into the active replay plan."""

    claims = tuple(getattr(getattr(active_unit, "evaluation", None), "claims", ()) or ())
    return [
        {
            "id": str(evidence.id),
            "metric": "object_property",
            "value": 1.0,
            "target": ">= 1",
            "pass": True,
            "source": "builder_state",
            "authoritative": True,
            "owner_layer": str(layer.id),
            "fault_owner": str(layer.id),
            "activates_at": str(layer.id),
            "lifecycle": "layer",
        }
        for claim in claims
        if getattr(claim, "required", False)
        for evidence in tuple(getattr(claim, "evidence", ()) or ())
    ]


def test_restart_reconciles_each_projection_boundary_from_exact_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layer, receipt, selected = _published_finalized_root(tmp_path)
    _remove_mutable_projections(tmp_path, layer.id)
    _forbid_new_finalization(monkeypatch)
    expected_terminal = receipt.as_dict()

    real_revalidation = layer_finalization_reconcile.reconcile_layer_revalidation_projection

    def crash_before_first_projection(*_args, **_kwargs):
        raise RuntimeError("injected death after terminal commit")

    monkeypatch.setattr(
        layer_finalization_reconcile,
        "reconcile_layer_revalidation_projection",
        crash_before_first_projection,
    )
    with pytest.raises(RuntimeError, match="after terminal commit"):
        _restart(tmp_path, layer, selected)
    assert load_unit_state(tmp_path, layer.id)["layer_finalization"]["terminal_receipt"] == expected_terminal

    def crash_after_revalidation(*args, **kwargs):
        real_revalidation(*args, **kwargs)
        raise RuntimeError("injected death after revalidation projection")

    monkeypatch.setattr(
        layer_finalization_reconcile,
        "reconcile_layer_revalidation_projection",
        crash_after_revalidation,
    )
    with pytest.raises(RuntimeError, match="after revalidation projection"):
        _restart(tmp_path, layer, selected)

    monkeypatch.setattr(
        layer_finalization_reconcile,
        "reconcile_layer_revalidation_projection",
        real_revalidation,
    )
    real_outcome = layer_finalization_reconcile.publish_finalized_layer_outcome

    def crash_after_outcome(*args, **kwargs):
        result = real_outcome(*args, **kwargs)
        assert result == layer_outcome_path(tmp_path, layer.id)
        raise RuntimeError("injected death after outcome projection")

    monkeypatch.setattr(
        layer_finalization_reconcile,
        "publish_finalized_layer_outcome",
        crash_after_outcome,
    )
    with pytest.raises(RuntimeError, match="after outcome projection"):
        _restart(tmp_path, layer, selected)
    expected_outcome = layer_outcome_path(tmp_path, layer.id).read_bytes()

    monkeypatch.setattr(
        layer_finalization_reconcile,
        "publish_finalized_layer_outcome",
        real_outcome,
    )
    real_ledger = layer_finalization_reconcile._reconcile_ledger

    def crash_after_ledger(*args, **kwargs):
        real_ledger(*args, **kwargs)
        raise RuntimeError("injected death after ledger projection")

    monkeypatch.setattr(
        layer_finalization_reconcile,
        "_reconcile_ledger",
        crash_after_ledger,
    )
    with pytest.raises(RuntimeError, match="after ledger projection"):
        _restart(tmp_path, layer, selected)
    expected_ledger = (tmp_path / "shot.json").read_bytes()

    monkeypatch.setattr(
        layer_finalization_reconcile,
        "_reconcile_ledger",
        real_ledger,
    )
    _restart(tmp_path, layer, selected)
    assert layer_outcome_path(tmp_path, layer.id).read_bytes() == expected_outcome
    assert (tmp_path / "shot.json").read_bytes() == expected_ledger
    assert load_unit_state(tmp_path, layer.id)["layer_finalization"]["terminal_receipt"] == expected_terminal

    # Repeating a fully reconciled restart is byte-idempotent.
    _restart(tmp_path, layer, selected)
    assert layer_outcome_path(tmp_path, layer.id).read_bytes() == expected_outcome
    assert (tmp_path / "shot.json").read_bytes() == expected_ledger


@pytest.mark.parametrize(
    "surface",
    ["revalidation", "outcome", "ledger"],
)
def test_restart_refuses_conflicting_preexisting_projection_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    surface: str,
) -> None:
    layer, receipt, selected = _published_finalized_root(tmp_path)
    _forbid_new_finalization(monkeypatch)

    if surface == "revalidation":
        (tmp_path / "runtime_checks.json").write_text(
            '{"conflicts":"with-terminal-source-absence"}\n',
            encoding="utf-8",
        )
        match = "runtime image checks conflict"
        error = ValueError
    elif surface == "outcome":
        outcome_path = layer_outcome_path(tmp_path, layer.id)
        outcome = json.loads(outcome_path.read_text(encoding="utf-8"))
        outcome["status"] = "failed"
        outcome_path.write_text(
            json.dumps(outcome, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        match = "existing layer outcome conflicts"
        error = ValueError
    else:
        # Normalize the fixture's compact outcome into the exact production
        # projection before isolating the later ledger boundary.
        layer_outcome_path(tmp_path, layer.id).unlink()
        _restart(tmp_path, layer, selected)
        ledger_path = tmp_path / "shot.json"
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        slot = ledger["milestones"][layer.id]
        assert slot["finalization_receipt_digest"] == receipt.receipt_digest
        slot["status"] = "failed"
        ledger_path.write_text(
            json.dumps(ledger, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        match = "ledger conflicts with its exact terminal receipt"
        error = ValueError

    with pytest.raises(error, match=match):
        _restart(tmp_path, layer, selected)


@pytest.mark.parametrize(
    "debt_result",
    ["no_signal", "satisfied", "falsified"],
)
def test_typed_debt_projection_crashes_resume_without_new_payment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    debt_result: str,
) -> None:
    """Every terminal debt projection is restartable exactly once."""

    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    renders: list[str] = []
    critic_calls: list[str] = []

    async def axes(*_args):
        return [
            ("camera_alignment", "camera framing"),
            ("form", "rendered form"),
        ]

    async def fail_if_critic_called(*_args, **_kwargs):
        critic_calls.append("called")
        raise AssertionError("a no-signal plate must not call the visual critic")

    async def deterministic_or_signal_judge(
        shot,
        milestone,
        render,
        owned_axes,
        session,
        verbose,
        scope=None,
        *,
        active_unit=None,
        **kwargs,
    ):
        if tuple(getattr(active_unit, "provisional_debt_ids", ()) or ()):
            if debt_result == "no_signal":
                return await critic_runtime._judge(
                    shot,
                    milestone,
                    render,
                    owned_axes,
                    session,
                    verbose,
                    scope,
                    active_unit=active_unit,
                    **kwargs,
                )
            critic_calls.append(debt_result)
            if debt_result == "satisfied":
                return {
                    "scores": {key: 5 for key, _description in owned_axes},
                    "mean": 5.0,
                    "pass": True,
                    "issues": [],
                    "scored_axes": [key for key, _description in owned_axes],
                    "na_axes": [],
                    "observations": [],
                    "observation_reconciliation": [],
                    "contract_gap": False,
                    "judge_conflict": False,
                    "decided_by": "typed-fixture-critic",
                }
            binding = "requirement:R-camera:camera_alignment"
            observation = {
                "id": "unbound-hall-framing",
                "kind": "qualitative",
                "axis": "camera_alignment",
                "property": "camera_framing",
                "observation": "The hall carrier exposes a framing mismatch.",
                "action": "Amend the producer graph before changing the scene.",
                "moment": 1,
                "roles": ["hall.mass"],
                "claim_id": binding,
                "check_ids": [binding],
                "panel_ids": [],
            }
            gap = {
                "state": "contract_gap",
                "observation": observation,
                "reason": "no authoritative evidence binds the property",
                "check_ids": [binding],
            }
            return {
                "scores": {key: 1 for key, _description in owned_axes},
                "mean": 1.0,
                "pass": False,
                "issues": [],
                "scored_axes": [key for key, _description in owned_axes],
                "na_axes": [],
                "observations": [observation],
                "observation_reconciliation": [gap],
                "contract_gaps": [gap],
                "contract_gap": True,
                "judge_conflict": False,
                "decided_by": "typed-fixture-contract-gap",
            }
        return {
            "scores": {key: 5 for key, _description in owned_axes},
            "mean": 5.0,
            "pass": True,
            "issues": [],
            "scored_axes": [key for key, _description in owned_axes],
            "na_axes": [],
            "observations": [],
            "observation_reconciliation": [],
            "contract_gap": False,
            "judge_conflict": False,
            "decided_by": "unit_executable_evidence",
            "evidence": list(kwargs.get("evidence") or []),
        }

    def stash_black(*args, **kwargs):
        render_rel, capture = _stash_fixture_capture(*args, **kwargs)
        renders.append(Path(render_rel).name)
        return render_rel, capture

    builder = verify_runtime.builder_package()
    monkeypatch.setattr(builder_layer, "ensure_axes", axes)
    monkeypatch.setattr(
        builder_layer,
        "_blender_version",
        lambda _session: "fixture",
    )
    monkeypatch.setattr(
        builder_layer.costlog,
        "bind",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(builder_layer.costlog, "unbind", lambda: None)
    monkeypatch.setattr(
        builder_layer.transcript,
        "bind",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(builder_layer.transcript, "unbind", lambda: None)
    monkeypatch.setattr(
        prior_runtime.generate_construction,
        "pin_for_script",
        lambda *_args: None,
    )
    monkeypatch.setattr(builder, "_scene_object_manifest", lambda _session: {})
    monkeypatch.setattr(
        builder,
        "_stash_render_with_receipt",
        stash_black,
    )
    monkeypatch.setattr(
        builder,
        "_render_evidence",
        _exact_fixture_executable_evidence,
    )
    if debt_result != "falsified":
        monkeypatch.setattr(
            verify_runtime,
            "_persist_contract_gaps",
            lambda *_args, **_kwargs: None,
        )
    monkeypatch.setattr(
        verify_runtime,
        "_judge_unit_or_layer",
        deterministic_or_signal_judge,
    )
    monkeypatch.setattr(critic_runtime, "_critique", fail_if_critic_called)

    root = _public_fixture_root(tmp_path)
    bundle = publish_current(
        root,
        run_artifacts.create(root, "projection-crash-typed-debt"),
        outcome="clean_with_deferred",
    )
    _publish_payload(
        root,
        _payload_for_bundle(
            root,
            "camera-jit.json",
            _camera_payload(),
            bundle.content_hash,
        ),
    )
    _pass_layer_unit(root, "1", "camera")
    session = _ReplaySession()
    anyio.run(_build_prepassed_layer, root, "1", session)
    _publish_payload(
        root,
        _payload_for_bundle(
            root,
            "form-jit.json",
            _form_payload(),
            bundle.content_hash,
        ),
    )
    if debt_result == "falsified":
        (root / "plans" / "02_hall_form.md").write_text(
            "# Exact fixture plan for the contract-gap source unit\n",
            encoding="utf-8",
        )
    _pass_layer_unit(root, "2", "hall_form")

    real_activate = layer_finalization_reconcile.mark_judgment_debt_due

    def crash_after_activation(*args, **kwargs):
        real_activate(*args, **kwargs)
        raise RuntimeError("injected death after debt activation projection")

    monkeypatch.setattr(
        layer_finalization_reconcile,
        "mark_judgment_debt_due",
        crash_after_activation,
    )
    with pytest.raises(
        RuntimeError,
        match="after debt activation projection",
    ):
        anyio.run(_build_prepassed_layer, root, "2", session)

    state = load_unit_state(root, "2")
    terminal = state["layer_finalization"]["terminal_receipt"]
    assert terminal is not None
    assert current_judgment_debt_states(root)[0][2].status == "due"
    paid_execution_count = len(session.executed)
    paid_render_count = len(renders)
    assert paid_render_count == 1
    expected_critic_calls = [] if debt_result == "no_signal" else [debt_result]
    assert critic_calls == expected_critic_calls

    _forbid_new_finalization(monkeypatch)

    async def forbid_judgment(*_args, **_kwargs):
        raise AssertionError("terminal restart invoked qualitative judgment")

    def forbid_render(*_args, **_kwargs):
        raise AssertionError("terminal restart invoked rendering")

    def forbid_blender_replay(*_args, **_kwargs):
        raise AssertionError("terminal restart invoked Blender replay")

    monkeypatch.setattr(
        verify_runtime,
        "_judge_unit_or_layer",
        forbid_judgment,
    )
    monkeypatch.setattr(builder, "_stash_render_with_receipt", forbid_render)
    monkeypatch.setattr(session, "run", forbid_blender_replay)
    monkeypatch.setattr(
        layer_finalization_reconcile,
        "mark_judgment_debt_due",
        real_activate,
    )
    payment_events = root / "state" / PAYMENT_ATTEMPT_EVENTS
    if debt_result == "no_signal":
        real_payment_failure = layer_finalization_reconcile.reconcile_judgment_payment_attempt_failures

        def crash_after_payment_failure(*args, **kwargs):
            real_payment_failure(*args, **kwargs)
            raise RuntimeError("injected death after payment-failure projection")

        monkeypatch.setattr(
            layer_finalization_reconcile,
            "reconcile_judgment_payment_attempt_failures",
            crash_after_payment_failure,
        )
        with pytest.raises(
            RuntimeError,
            match="after payment-failure projection",
        ):
            anyio.run(_build_prepassed_layer, root, "2", session)
        assert len(payment_events.read_text(encoding="utf-8").splitlines()) == 1
        monkeypatch.setattr(
            layer_finalization_reconcile,
            "reconcile_judgment_payment_attempt_failures",
            real_payment_failure,
        )
    else:
        if debt_result == "falsified":
            real_finding = layer_finalization_reconcile.record_prepared_accepted_hypothesis_falsification

            def crash_after_finding(*args, **kwargs):
                real_finding(*args, **kwargs)
                raise RuntimeError("injected death after typed finding projection")

            monkeypatch.setattr(
                layer_finalization_reconcile,
                "record_prepared_accepted_hypothesis_falsification",
                crash_after_finding,
            )
            with pytest.raises(
                RuntimeError,
                match="after typed finding projection",
            ):
                anyio.run(_build_prepassed_layer, root, "2", session)
            falsifications = load_unit_state(root, "2")["falsifications"]
            assert len(falsifications) == 1
            assert falsifications[0] == terminal["projection"]["finding"]
            monkeypatch.setattr(
                layer_finalization_reconcile,
                "record_prepared_accepted_hypothesis_falsification",
                real_finding,
            )

        real_resolution = layer_finalization_reconcile.resolve_current_judgment_debt

        def crash_after_resolution(*args, **kwargs):
            real_resolution(*args, **kwargs)
            raise RuntimeError("injected death after terminal debt resolution")

        monkeypatch.setattr(
            layer_finalization_reconcile,
            "resolve_current_judgment_debt",
            crash_after_resolution,
        )
        with pytest.raises(
            RuntimeError,
            match="after terminal debt resolution",
        ):
            anyio.run(_build_prepassed_layer, root, "2", session)
        expected_status = "satisfied" if debt_result == "satisfied" else "falsified"
        assert current_judgment_debt_states(root)[0][2].status == expected_status
        monkeypatch.setattr(
            layer_finalization_reconcile,
            "resolve_current_judgment_debt",
            real_resolution,
        )

    debt_events = root / "state" / "judgment-debts.jsonl"
    debt_event_count = len(debt_events.read_text(encoding="utf-8").splitlines())

    def finish_reconciliation() -> None:
        if debt_result == "falsified":
            with pytest.raises(BuildAuthorityDefect):
                anyio.run(_build_prepassed_layer, root, "2", session)
        else:
            anyio.run(_build_prepassed_layer, root, "2", session)

    assert len(session.executed) == paid_execution_count
    assert len(renders) == paid_render_count
    assert critic_calls == expected_critic_calls
    assert len(debt_events.read_text(encoding="utf-8").splitlines()) == debt_event_count
    finish_reconciliation()
    outcome_bytes = layer_outcome_path(root, "2").read_bytes()
    ledger_bytes = (root / "shot.json").read_bytes()
    finish_reconciliation()

    if debt_result == "no_signal":
        assert len(payment_events.read_text(encoding="utf-8").splitlines()) == 1
        assert current_judgment_debt_states(root)[0][2].status == "due"
    else:
        assert not payment_events.exists()
    assert layer_outcome_path(root, "2").read_bytes() == outcome_bytes
    assert (root / "shot.json").read_bytes() == ledger_bytes
    assert len(session.executed) == paid_execution_count
    assert len(renders) == paid_render_count
    assert critic_calls == expected_critic_calls
    assert len(debt_events.read_text(encoding="utf-8").splitlines()) == debt_event_count
    if debt_result == "falsified":
        falsifications = load_unit_state(root, "2")["falsifications"]
        assert len(falsifications) == 1
        assert falsifications[0] == terminal["projection"]["finding"]
    assert load_unit_state(root, "2")["layer_finalization"]["terminal_receipt"] == terminal
