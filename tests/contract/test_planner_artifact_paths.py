from __future__ import annotations

from vfx_harness.agents.prompts import PLANNER_SYSTEM
from vfx_harness.evaluation.plan_gate import _check_coverage, _check_hierarchical_plans
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


def test_plan_gate_reports_all_cross_layer_dependencies_in_one_pass(tmp_path) -> None:
    (tmp_path / "plans").mkdir()
    (tmp_path / "plans" / "global.md").write_text("# plan\n", encoding="utf-8")
    (tmp_path / "layers.json").write_text(
        """{
  "schema": 4,
  "layers": [
    {"id": "1", "stages": [
      {"id": "layout", "depends_on": []}
    ]},
    {"id": "2", "stages": [
      {"id": "lookdev", "depends_on": ["layout"]}
    ]},
    {"id": "3", "stages": [
      {"id": "lighting", "depends_on": ["lookdev"]}
    ]},
    {"id": "4", "stages": [
      {"id": "finish", "depends_on": ["lighting", "missing_peer"]}
    ]}
  ]
}
""",
        encoding="utf-8",
    )

    findings, stats = _check_hierarchical_plans(tmp_path)

    assert stats == {"unit_plans_required": 0, "layers_passed": 0}
    assert [finding.where for finding in findings] == [
        "layers.json.layers[1].stages.lookdev",
        "layers.json.layers[2].stages.lighting",
        "layers.json.layers[3].stages.finish",
    ]
    assert "layout" in findings[0].what
    assert "lookdev" in findings[1].what
    assert "lighting, missing_peer" in findings[2].what
