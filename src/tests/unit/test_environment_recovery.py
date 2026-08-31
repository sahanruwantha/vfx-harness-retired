"""Receipt-backed external environment recovery and crash reconciliation."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from vfx_harness.application import environment_recovery, preflight
from vfx_harness.application.inspect_run import collect
from vfx_harness.cli import _COMMANDS
from vfx_harness.domain.environment_results import EnvironmentProbeSpec, EnvironmentResult
from vfx_harness.domain.stop_transactions import action_idempotency_key
from vfx_harness.observability import run_artifacts
from vfx_harness.orchestration import environment_recovery_state, transaction_receipts


def _failed_raw() -> dict:
    return {
        "ok": False,
        "auth": {
            "ok": False,
            "using": None,
            "problems": ["ANTHROPIC_API_KEY is not selected"],
            "notes": [],
            "present": [],
            "decoys": [],
        },
        "configuration": {"ok": True, "problems": []},
        "blender": {
            "ok": True,
            "requested": "blender",
            "resolved": "/usr/bin/blender",
            "problems": [],
        },
    }


def _passing_raw(*, secret: str | None = None) -> dict:
    auth = {
        "ok": True,
        "using": "ANTHROPIC_API_KEY",
        "problems": [],
        "notes": [],
        "present": ["ANTHROPIC_API_KEY"],
        "decoys": [],
    }
    if secret is not None:
        auth["raw_value_for_test"] = secret
    return {
        "ok": True,
        "auth": auth,
        "configuration": {"ok": True, "problems": []},
        "blender": {
            "ok": True,
            "requested": "blender",
            "resolved": "/usr/bin/blender",
            "problems": [],
        },
    }


def _source_stop(
    shot: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    run_id: str = "failed-preflight",
) -> tuple[str, str]:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    layout = run_artifacts.create(shot, run_id)
    result = preflight.environment_result(_failed_raw())
    envelope = preflight.environment_stop(layout, result)
    layout.write_stop_envelope(envelope)
    layout.set_status(
        "failed",
        exit_code=1,
        metadata={
            "stop_envelope": "reports/stop-envelope.json",
            "stop_envelope_digest": envelope.digest,
        },
    )
    action = envelope.actions[0]
    key = action_idempotency_key(
        action,
        authoritative_before_digest=envelope.authoritative_before_digest,
        attempt_evidence_digest=envelope.attempt_evidence_digest,
    )
    return run_id, key


def test_failed_reverification_waits_then_commits_once_after_external_fix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, key = _source_stop(tmp_path, monkeypatch)
    stop = collect(tmp_path, run_id=run_id)["run"]["stop"]
    assert stop["legal_actions"][0]["idempotency_key"] == key
    assert (
        _COMMANDS["recover-environment"]
        == "vfx_harness.application.environment_recovery:main"
    )
    calls = 0

    def probe(_blender: str | None = None) -> dict:
        nonlocal calls
        calls += 1
        return _failed_raw() if calls < 3 else _passing_raw()

    monkeypatch.setattr(preflight, "probe", probe)
    first = environment_recovery.execute_environment_recovery(
        tmp_path, run_id=run_id, idempotency_key=key
    )
    second = environment_recovery.execute_environment_recovery(
        tmp_path, run_id=run_id, idempotency_key=key
    )
    terminal = environment_recovery.execute_environment_recovery(
        tmp_path, run_id=run_id, idempotency_key=key
    )

    assert first.phase == second.phase == "running"
    assert first == second
    assert terminal.phase == "terminal"
    assert terminal.terminal_outcome == "committed"
    assert calls == 3
    evaluation = environment_recovery.evaluate_environment_recovery(
        tmp_path, run_id=run_id, idempotency_key=key
    )
    assert evaluation is not None and evaluation.satisfied

    replay = environment_recovery.execute_environment_recovery(
        tmp_path, run_id=run_id, idempotency_key=key
    )
    assert replay == terminal
    assert calls == 3
    assert {"recover_environment"} == environment_recovery.RECEIPT_CAPABLE_TRANSACTION_IDS


def test_crash_after_running_resumes_safe_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, key = _source_stop(tmp_path, monkeypatch)
    monkeypatch.setattr(
        preflight,
        "probe",
        lambda _blender=None: (_ for _ in ()).throw(RuntimeError("injected probe crash")),
    )

    with pytest.raises(RuntimeError, match="injected probe crash"):
        environment_recovery.execute_environment_recovery(
            tmp_path, run_id=run_id, idempotency_key=key
        )
    running = transaction_receipts.current_transaction_receipt(tmp_path, key)
    assert running is not None and running.phase == "running" and running.revision == 2

    monkeypatch.setattr(preflight, "probe", lambda _blender=None: _passing_raw())
    terminal = environment_recovery.execute_environment_recovery(
        tmp_path, run_id=run_id, idempotency_key=key
    )
    assert terminal.phase == "terminal"


def test_crash_after_commit_reconciles_without_second_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, key = _source_stop(tmp_path, monkeypatch)
    calls = 0

    def passing_probe(_blender: str | None = None) -> dict:
        nonlocal calls
        calls += 1
        return _passing_raw()

    monkeypatch.setattr(preflight, "probe", passing_probe)
    publish = transaction_receipts.publish_transaction_receipt

    def crash_before_terminal(shot: Path, receipt):
        if receipt.phase == "terminal":
            raise RuntimeError("injected terminal publication crash")
        return publish(shot, receipt)

    monkeypatch.setattr(
        transaction_receipts,
        "publish_transaction_receipt",
        crash_before_terminal,
    )
    with pytest.raises(RuntimeError, match="terminal publication crash"):
        environment_recovery.execute_environment_recovery(
            tmp_path, run_id=run_id, idempotency_key=key
        )
    assert environment_recovery_state.read_environment_commit(tmp_path, key) is not None
    assert transaction_receipts.current_transaction_receipt(tmp_path, key).phase == "running"

    monkeypatch.setattr(transaction_receipts, "publish_transaction_receipt", publish)
    terminal = environment_recovery.execute_environment_recovery(
        tmp_path, run_id=run_id, idempotency_key=key
    )
    assert terminal.phase == "terminal"
    assert calls == 1


def test_crash_orphaned_observation_is_selected_before_a_changed_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, key = _source_stop(tmp_path, monkeypatch)
    calls = 0

    def changing_probe(_blender: str | None = None) -> dict:
        nonlocal calls
        calls += 1
        return _failed_raw() if calls == 1 else _passing_raw()

    monkeypatch.setattr(preflight, "probe", changing_probe)
    publish_pointer = transaction_receipts._publish_pointer

    def crash_on_observation(path: Path, pointer) -> None:
        if pointer.revision == 3:
            raise RuntimeError("injected observation pointer crash")
        publish_pointer(path, pointer)

    monkeypatch.setattr(transaction_receipts, "_publish_pointer", crash_on_observation)
    with pytest.raises(RuntimeError, match="observation pointer crash"):
        environment_recovery.execute_environment_recovery(
            tmp_path, run_id=run_id, idempotency_key=key
        )
    selected = transaction_receipts.current_transaction_receipt(tmp_path, key)
    assert selected is not None and selected.revision == 2

    monkeypatch.setattr(transaction_receipts, "_publish_pointer", publish_pointer)
    terminal = environment_recovery.execute_environment_recovery(
        tmp_path, run_id=run_id, idempotency_key=key
    )
    assert terminal.phase == "terminal" and terminal.revision == 5
    assert calls == 2
    assert tuple(
        receipt.revision
        for receipt in transaction_receipts.transaction_receipt_chain(tmp_path, key)
    ) == (1, 2, 3, 4, 5)


def test_concurrent_same_key_calls_publish_one_chain_and_probe_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, key = _source_stop(tmp_path, monkeypatch)
    calls = 0
    calls_lock = threading.Lock()

    def passing_probe(_blender: str | None = None) -> dict:
        nonlocal calls
        with calls_lock:
            calls += 1
        return _passing_raw()

    monkeypatch.setattr(preflight, "probe", passing_probe)

    def execute() -> str:
        return environment_recovery.execute_environment_recovery(
            tmp_path, run_id=run_id, idempotency_key=key
        ).digest

    with ThreadPoolExecutor(max_workers=2) as pool:
        digests = tuple(pool.map(lambda _index: execute(), range(2)))

    assert len(set(digests)) == 1
    assert calls == 1
    chain = transaction_receipts.transaction_receipt_chain(tmp_path, key)
    assert tuple(receipt.phase for receipt in chain) == (
        "prepared",
        "running",
        "running",
        "terminal",
    )


def test_identical_failed_runs_converge_on_one_semantic_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_a, key_a = _source_stop(tmp_path, monkeypatch, run_id="failed-preflight-a")
    run_b, key_b = _source_stop(tmp_path, monkeypatch, run_id="failed-preflight-b")
    assert key_a == key_b
    calls = 0

    def passing_probe(_blender: str | None = None) -> dict:
        nonlocal calls
        calls += 1
        return _passing_raw()

    monkeypatch.setattr(preflight, "probe", passing_probe)
    terminal_a = environment_recovery.execute_environment_recovery(
        tmp_path, run_id=run_a, idempotency_key=key_a
    )
    terminal_b = environment_recovery.execute_environment_recovery(
        tmp_path, run_id=run_b, idempotency_key=key_b
    )

    assert terminal_b == terminal_a
    assert calls == 1
    evaluation = environment_recovery.evaluate_environment_recovery(
        tmp_path, run_id=run_b, idempotency_key=key_b
    )
    assert evaluation is not None and evaluation.satisfied


def test_probe_spec_change_halts_and_secret_never_enters_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, key = _source_stop(tmp_path, monkeypatch)
    secret = "sk-ant-api-secret-never-persist"
    passing = preflight.environment_result(_passing_raw(secret=secret))
    assert passing.probe_spec is not None
    changed = EnvironmentResult(
        probe_id=passing.probe_id,
        checks=passing.checks,
        probe_spec=EnvironmentProbeSpec(
            probe_id=passing.probe_id,
            probe_revision=passing.probe_spec.probe_revision + 1,
            check_ids=passing.probe_spec.check_ids,
        ),
    )
    monkeypatch.setattr(preflight, "probe", lambda _blender=None: _passing_raw(secret=secret))
    monkeypatch.setattr(preflight, "environment_result", lambda _raw: changed)

    with pytest.raises(ValueError, match="probe identity changed"):
        environment_recovery.execute_environment_recovery(
            tmp_path, run_id=run_id, idempotency_key=key
        )
    current = transaction_receipts.current_transaction_receipt(tmp_path, key)
    assert current is not None and current.recovery_disposition == "halt_on_uncertainty"
    state_text = "".join(
        path.read_text(encoding="utf-8")
        for path in (tmp_path / "state").rglob("*.json")
    )
    assert secret not in state_text


def test_wrong_key_and_tampered_commit_fail_before_reuse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, key = _source_stop(tmp_path, monkeypatch)
    monkeypatch.setattr(preflight, "probe", lambda _blender=None: _passing_raw())
    with pytest.raises(ValueError, match="idempotency key does not match"):
        environment_recovery.execute_environment_recovery(
            tmp_path,
            run_id=run_id,
            idempotency_key="0" * 64,
        )
    assert not (tmp_path / "state" / "transactions" / ("0" * 64)).exists()

    environment_recovery.execute_environment_recovery(
        tmp_path, run_id=run_id, idempotency_key=key
    )
    commit_path = environment_recovery_state.environment_commit_path(tmp_path, key)
    value = json.loads(commit_path.read_text(encoding="utf-8"))
    value["after_environment_digest"] = "f" * 64
    commit_path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="commit_digest is stale"):
        environment_recovery.evaluate_environment_recovery(
            tmp_path, run_id=run_id, idempotency_key=key
        )


def test_source_environment_evidence_cannot_be_substituted_by_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, key = _source_stop(tmp_path, monkeypatch)
    layout = run_artifacts.select(tmp_path, run_id)
    assert layout is not None
    envelope = layout.read_stop_envelope()
    evidence = next(
        item for item in envelope.evidence_refs if item.kind == "environment_result"
    )
    evidence_path = tmp_path / evidence.locator
    outside = tmp_path / "relocated-environment-result.json"
    outside.write_bytes(evidence_path.read_bytes())
    evidence_path.unlink()
    evidence_path.symlink_to(outside)

    with pytest.raises(ValueError, match="must not use symlink path components"):
        environment_recovery.execute_environment_recovery(
            tmp_path, run_id=run_id, idempotency_key=key
        )
