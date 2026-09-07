"""The verify budget has exactly one ceiling, and a rejection names the dotenv it read.

Two sessions running fresh shots reported the same defect independently: hansa
(6 layers) and caesar (7 layers) both logged "verify budget: 12 turns", the draft's
``plan_max_turns``, where the rule mandates 18 and 20. The per-layer scaling HIR-0177
added was inert for every shot with four or more layers (HIR-0198).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest

from vfx_harness.agents import global_planner, planner
from vfx_harness.agents.planner import budget
from vfx_harness.infrastructure import config
from vfx_harness.observability import run_artifacts


def _two_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, layers: int, max_turns: int):
    draft = tmp_path / "plans" / "global.md"
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_text("# exact draft\n", encoding="utf-8")
    (tmp_path / "ownership_mapping.json").write_text(
        json.dumps({"layers": [{"id": index} for index in range(layers)]}), encoding="utf-8"
    )
    seen: list[int] = []

    async def capturing_generate(*args, **kwargs):
        if kwargs.get("role") == "verify":
            seen.append(kwargs["max_turns"])
        return draft

    monkeypatch.setattr(planner, "load_shot", lambda folder: SimpleNamespace(folder=tmp_path))
    monkeypatch.setattr(planner, "generate_plan", capturing_generate)
    monkeypatch.setattr(
        planner.Settings,
        "from_environment",
        lambda **kwargs: SimpleNamespace(global_planner_model="model", plan_verify_max_turns=6),
    )
    monkeypatch.setattr(
        run_artifacts, "ensure", lambda *args, **kwargs: SimpleNamespace(scratch=tmp_path / "scratch")
    )

    monkeypatch.setattr(global_planner, "snapshot_candidate", lambda _: tmp_path / "draft-snapshot.json")

    async def invoke():
        return await planner.generate_plan_two_pass(
            tmp_path, workspace=tmp_path, max_turns=max_turns
        )

    anyio.run(invoke)
    return seen


def test_the_drafts_cap_is_not_a_second_ceiling_on_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact shipped defect: a 6-layer shot ran verify on the draft's 12 turns."""
    assert _two_pass(tmp_path, monkeypatch, layers=6, max_turns=12) == [18]
    assert _two_pass(tmp_path, monkeypatch, layers=7, max_turns=12) == [20]


def test_every_layer_count_gets_the_budget_the_rule_states(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for layers, expected in ((1, 8), (3, 12), (4, 14), (8, 22), (9, 24), (40, 24)):
        # The draft's cap varies; the verify budget does not follow it in either direction.
        for max_turns in (12, 100):
            assert _two_pass(tmp_path, monkeypatch, layers=layers, max_turns=max_turns) == [expected]
            assert expected == budget.plan_verify_turn_budget(6, layers)


def test_the_budget_log_line_shows_its_derivation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """"12 turns for 6 drafted layers" read as a computed budget while it was a clamp."""
    _two_pass(tmp_path, monkeypatch, layers=6, max_turns=12)

    line = next(
        text for text in capsys.readouterr().out.splitlines() if "verify budget:" in text
    )
    assert "18 turns" in line
    assert "max(6 configured, 6 + 2×6 drafted layer(s))" in line
    assert f"ceiling {budget.PLAN_VERIFY_TURN_CEILING}" in line


def test_the_dotenv_note_names_the_file_this_process_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three sessions each rediscovered that .env follows the code, not the cwd."""
    monkeypatch.setenv(config.ENV_FILE_VARIABLE, "/somewhere/.env")
    assert config.dotenv_resolution_note() == (
        f"{config.ENV_FILE_VARIABLE}=/somewhere/.env is the dotenv file this process reads."
    )

    monkeypatch.delenv(config.ENV_FILE_VARIABLE, raising=False)
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    note = config.dotenv_resolution_note()
    assert str(tmp_path) in note and config.ENV_FILE_VARIABLE in note
    assert "no readable .env" in note

    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=x\n", encoding="utf-8")
    assert config.dotenv_resolution_note() == (
        f"This process reads {tmp_path / '.env'}; the variables must be set there."
    )


def test_the_credential_rejection_points_at_the_dotenv_not_at_the_variables() -> None:
    from vfx_harness.application import preflight

    raw = {
        "ok": False,
        "auth": {"ok": False, "using": None, "problems": ["no credential"], "notes": [], "present": [], "decoys": []},
        "configuration": {"ok": True, "problems": []},
        "blender": {"ok": True, "requested": "blender", "resolved": "/usr/bin/blender", "problems": []},
        "blender_confinement": {
            "ok": True,
            "bwrap": "/usr/bin/bwrap",
            "libseccomp": "libseccomp.so.2",
            "worker_blender": "5.2.1 LTS",
            "worker_gpu": {"renderer": "r", "backend": "OPENGL", "device_type": "NVIDIA"},
            "host_gpu_device_nodes": ["/dev/dri"],
            "problems": [],
        },
        "builder_execution_fence": {"ok": True, "mechanism": "sysv-sem-undo+descriptor-flock", "problems": []},
        "plan_consumer_directory": {"ok": True, "mechanism": "fanotify-target-fid+openat2", "problems": []},
    }

    check = next(
        row
        for row in preflight.environment_result(raw).checks
        if row.check_id == "credential_configuration"
    )

    assert "VFXH_ENV_FILE" in check.next_action
    assert "dotenv" in check.next_action
    assert "Correct the named credential variables" not in check.next_action
