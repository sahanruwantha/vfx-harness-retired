from __future__ import annotations

from vfx_harness.agents.prompts import PLANNER_SYSTEM
from vfx_harness.evaluation.plan_gate import _check_coverage
from vfx_harness.observability import run_artifacts, transcript


def test_planner_prompt_makes_machine_contract_location_unambiguous() -> None:
    assert "SHOT ROOT" in PLANNER_SYSTEM
    assert "These files do NOT live under `plans/`" in PLANNER_SYSTEM
    for name in (
        "layers.json",
        "acceptance.json",
        "checks.json",
        "scene_checks.json",
        "critic_axes.json",
    ):
        assert f"SHOT-ROOT `{name}`" in PLANNER_SYSTEM


def test_transcript_label_cannot_create_nested_paths(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("VFXH_NO_TRANSCRIPT", raising=False)
    path = transcript.bind(
        tmp_path,
        "plan",
        label="VERIFY (auditing plans/global.draft.md)",
        run_id="fresh-run",
    )
    assert path is not None
    transcript.unbind()

    assert path.is_file()
    layout = run_artifacts.select(tmp_path, "fresh-run")
    assert layout is not None
    assert path.parent == layout.logs / "transcripts" / "plan"
    assert "/" not in path.name
    assert "plans-global.draft.md" in path.name


def test_scene_contract_satisfies_own_stage_coverage(tmp_path) -> None:
    (tmp_path / "layers.json").write_text(
        '{"schema": 4, "layers": [{"id": "1"}]}\n', encoding="utf-8"
    )
    (tmp_path / "checks.json").write_text(
        '{"schema": 2, "checks": []}\n', encoding="utf-8"
    )
    (tmp_path / "scene_checks.json").write_text(
        '{"schema": 2, "contracts": [{"id": "layout-count", "activates_at": "1"}]}\n',
        encoding="utf-8",
    )

    findings, stats = _check_coverage(tmp_path)

    assert findings == []
    assert stats["layers_uncovered"] == 0


def test_layer_without_any_executable_contract_remains_uncovered(tmp_path) -> None:
    (tmp_path / "layers.json").write_text(
        '{"schema": 4, "layers": [{"id": "1"}]}\n', encoding="utf-8"
    )
    (tmp_path / "checks.json").write_text(
        '{"schema": 2, "checks": []}\n', encoding="utf-8"
    )

    findings, stats = _check_coverage(tmp_path)

    assert len(findings) == 1
    assert findings[0].check == "coverage"
    assert stats["layers_uncovered"] == 1
