"""Standalone preflight is typed; run-scoped failure permits environment recovery only."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from vfx_harness.application import preflight
from vfx_harness.domain.environment_results import EnvironmentResult
from vfx_harness.observability import run_artifacts


def _failed_raw() -> dict:
    return {
        "ok": False,
        "auth": {
            "ok": False,
            "using": None,
            "problems": ["ANTHROPIC_API_KEY is not selected"],
            "notes": [],
            "present": [],
            "decoys": ["CLAUDE_API_KEY"],
        },
        "configuration": {"ok": True, "problems": []},
        "blender": {
            "ok": True,
            "requested": "blender",
            "resolved": "/usr/bin/blender",
            "problems": [],
        },
    }


def test_environment_result_round_trips_and_rejects_stale_summary() -> None:
    result = preflight.environment_result(_failed_raw())

    assert result.ok is False
    assert EnvironmentResult.from_dict(result.as_dict(), "result") == result

    stale = deepcopy(result.as_dict())
    stale["ok"] = True
    with pytest.raises(ValueError, match="ok is inconsistent"):
        EnvironmentResult.from_dict(stale, "result")


def test_failed_preflight_stop_is_recovery_only_and_stable_across_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    result = preflight.environment_result(_failed_raw())
    first = preflight.environment_stop(run_artifacts.create(tmp_path, "run-1"), result)
    second = preflight.environment_stop(run_artifacts.create(tmp_path, "run-2"), result)

    assert first.stop_class == "infrastructure_failure"
    assert first.retryable is False
    assert [action.transaction_id for action in first.actions] == ["recover_environment"]
    assert first.actions[0].dispatch_mode == "external_recovery"
    assert first.evidence_refs[0].record_digest == result.digest
    assert first.cause_fingerprint == second.cause_fingerprint
    assert first.digest != second.digest


def test_strict_preflight_emits_typed_result_and_optional_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(preflight, "probe", _failed_raw)
    output = tmp_path / "preflight.json"

    assert preflight.main(["--strict", "--output", str(output)]) == 1

    stdout = json.loads(capsys.readouterr().out)
    written = json.loads(output.read_text(encoding="utf-8"))
    assert stdout == written
    assert stdout["schema"] == "vfx-harness.environment-result/v1"
    assert stdout["ok"] is False
    assert {check["check_id"] for check in stdout["checks"]} == {
        "blender_executable",
        "credential_configuration",
        "runtime_configuration",
    }
    assert "ANTHROPIC_API_KEY is not selected" in json.dumps(stdout)


def test_preflight_result_never_serializes_credential_values() -> None:
    secret = "sk-ant-api-secret-value"
    result = preflight.environment_result(
        {
            "ok": True,
            "auth": {
                "ok": True,
                "using": "ANTHROPIC_API_KEY",
                "problems": [],
                "notes": [],
                "present": ["ANTHROPIC_API_KEY"],
                "decoys": [],
                "raw_value_for_test": secret,
            },
            "configuration": {"ok": True, "problems": []},
            "blender": {
                "ok": True,
                "requested": "blender",
                "resolved": "/usr/bin/blender",
                "problems": [],
            },
        }
    )

    assert secret not in json.dumps(result.as_dict())
