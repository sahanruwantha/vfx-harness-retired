"""Standalone preflight is typed; run-scoped failure permits environment recovery only."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from vfx_harness.application import preflight
from vfx_harness.blender import resolution as blender_resolution
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
        "blender_confinement": {
            "ok": True,
            "bwrap": "/usr/bin/bwrap",
            "libseccomp": "libseccomp.so.2",
            "worker_blender": "5.2.1 LTS",
            "problems": [],
        },
        "builder_execution_fence": {
            "ok": True,
            "mechanism": "sysv-sem-undo+descriptor-flock",
            "problems": [],
        },
    }


def test_environment_result_round_trips_and_rejects_stale_summary() -> None:
    result = preflight.environment_result(_failed_raw())

    assert result.ok is False
    assert result.as_dict()["probe_spec"]["probe_revision"] == 4
    assert EnvironmentResult.from_dict(result.as_dict(), "result") == result

    stale = deepcopy(result.as_dict())
    stale["ok"] = True
    with pytest.raises(ValueError, match="ok is inconsistent"):
        EnvironmentResult.from_dict(stale, "result")

    legacy = deepcopy(result.as_dict())
    legacy["schema"] = "vfx-harness.environment-result/v1"
    legacy.pop("probe_spec")
    with pytest.raises(ValueError, match=r"fields mismatch|schema must be"):
        EnvironmentResult.from_dict(legacy, "result")

    changed_probe = deepcopy(result.as_dict())
    changed_probe["probe_spec"]["probe_revision"] = 5
    with pytest.raises(ValueError, match="probe_spec_digest is stale"):
        EnvironmentResult.from_dict(changed_probe, "result")


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
    assert stdout["schema"] == "vfx-harness.environment-result/v2"
    assert stdout["ok"] is False
    assert {check["check_id"] for check in stdout["checks"]} == {
        "builder_execution_fence",
        "blender_confinement",
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
            "blender_confinement": {
                "ok": True,
                "bwrap": "/usr/bin/bwrap",
                "libseccomp": "libseccomp.so.2",
                "worker_blender": "5.2.1 LTS",
                "problems": [],
            },
            "builder_execution_fence": {
                "ok": True,
                "mechanism": "sysv-sem-undo+descriptor-flock",
                "problems": [],
            },
        }
    )

    assert secret not in json.dumps(result.as_dict())


def test_confinement_failure_is_a_typed_preflight_failure() -> None:
    raw = _failed_raw()
    raw["auth"]["ok"] = True
    raw["auth"]["using"] = "ANTHROPIC_API_KEY"
    raw["auth"]["problems"] = []
    raw["blender_confinement"] = {
        "ok": False,
        "bwrap": None,
        "libseccomp": "libseccomp.so.2",
        "worker_blender": None,
        "problems": ["bubblewrap (`bwrap`) is unavailable."],
    }

    result = preflight.environment_result(raw)

    assert result.ok is False
    failed = [check for check in result.checks if not check.passed]
    assert [check.check_id for check in failed] == ["blender_confinement"]
    assert result.probe_spec is not None
    assert result.probe_spec.probe_revision == 4


def test_builder_fence_failure_is_a_typed_preflight_failure() -> None:
    raw = _failed_raw()
    raw["auth"]["ok"] = True
    raw["auth"]["using"] = "ANTHROPIC_API_KEY"
    raw["auth"]["problems"] = []
    raw["builder_execution_fence"] = {
        "ok": False,
        "mechanism": "sysv-sem-undo+descriptor-flock",
        "problems": ["System V semaphores are unavailable."],
    }

    result = preflight.environment_result(raw)

    assert result.ok is False
    failed = [check for check in result.checks if not check.passed]
    assert [check.check_id for check in failed] == ["builder_execution_fence"]
    assert "System V semaphore" in failed[0].next_action


def test_builder_fence_probe_exercises_live_exclusion() -> None:
    preflight._probe_builder_execution_fence.cache_clear()

    result = preflight._probe_builder_execution_fence()

    assert result == {
        "ok": True,
        "mechanism": "sysv-sem-undo+descriptor-flock",
        "problems": [],
    }


def test_check_reports_the_confined_resolution_diagnostic_as_the_blender_problem(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(requested: str, *, shot_bound: bool = True) -> str:
        raise blender_resolution.BlenderResolutionError(
            requested,
            (
                blender_resolution.BlenderCandidateRejection(
                    "/snap/bin/blender",
                    "timeout waiting for snap system profiles to get updated",
                ),
            ),
            shot_bound=shot_bound,
        )

    monkeypatch.setattr(preflight.blender_resolution, "resolve_blender", refuse)
    preflight._probe_blender_confinement.cache_clear()

    result = preflight.check(blender="blender")

    assert result["ok"] is False
    assert result["blender"]["requested"] == "blender"
    assert result["blender"]["resolved"] is None
    assert result["blender"]["ok"] is False
    (problem,) = result["blender"]["problems"]
    assert "mandatory filesystem confinement" in problem
    assert "/snap/bin/blender: timeout waiting for snap system profiles" in problem
    assert "BLENDER_BIN" in problem
    assert result["blender_confinement"]["problems"] == [
        "Blender confinement cannot run until Blender resolves."
    ]
    typed = preflight.environment_result(result)
    executable = next(
        check for check in typed.checks if check.check_id == "blender_executable"
    )
    assert executable.passed is False
    assert "inside the mandatory filesystem confinement" in executable.expected
    assert "BLENDER_BIN" in executable.next_action
