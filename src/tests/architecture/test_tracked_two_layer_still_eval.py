"""Tracked-input contract for the small HIR-0170/HIR-0171 real-model seal."""

from __future__ import annotations

import ast
import importlib.util
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from vfx_harness.domain.brief import load_shot
from vfx_harness.domain.plan_records import brief_clause_spans

REPOSITORY = Path(__file__).resolve().parents[3]
FIXTURE = REPOSITORY / "evals" / "fixtures" / "hir-0170-0171-two-layer-still"
SUITE = REPOSITORY / "evals" / "suites" / "judgment-debt-seal-v1.json"
GRADER = REPOSITORY / "evals" / "graders" / "judgment_debt_seal_v1.py"


def _load_grader():
    spec = importlib.util.spec_from_file_location("judgment_debt_seal_v1", GRADER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_two_layer_still_eval_has_closed_hashed_manifests() -> None:
    grader = _load_grader()
    fixture = grader.load_fixture_manifest()
    suite = grader.load_suite_manifest()

    assert fixture["fixture_digest"] == suite["fixture"]["digest"]
    assert suite["grader"] == {
        "path": "evals/graders/judgment_debt_seal_v1.py",
        "sha256": grader._sha256(GRADER),
    }
    assert {row["path"] for row in fixture["files"]} == {
        "brief.md",
        "generate_reference.py",
    }
    assert not list(FIXTURE.rglob("*.png")), "reference PNG must remain generated"
    assert suite["execution"]["workspace_root"] == "artifacts/evaluations/workspaces"
    assert suite["invariants"]["dag"]["layer_count"] == 2
    assert suite["invariants"]["judgment_debt"]["settlement_count"] == 1


def test_two_layer_still_brief_is_visual_intent_not_eval_mechanics() -> None:
    shot = load_shot(FIXTURE)
    assert shot.id == "hir-0170-0171-two-layer-still"
    assert shot.frames == 1
    assert shot.fps == 24
    assert shot.resolution == (512, 512)
    assert shot.refs == []
    clauses = brief_clause_spans(FIXTURE / "brief.md")
    assert len(clauses) == 2
    assert "camera.primary" not in clauses[0][2]
    assert "camera.primary" in clauses[1][2]
    assert "only for this framing" in clauses[1][2]
    assert "non-binding" in clauses[1][2]
    assert "Evaluation architecture" not in (FIXTURE / "brief.md").read_text(
        encoding="utf-8"
    )


def test_two_layer_still_commands_are_public_and_workspace_confined() -> None:
    grader = _load_grader()
    commands = grader.compiled_commands(trial_id="trial-001")
    rendered = [" ".join(command) for command in commands]
    workspace = "artifacts/evaluations/workspaces/judgment-debt-seal-v1/trial-001"
    assert rendered == [
        ".venv/bin/vfx preflight --strict",
        f".venv/bin/vfx plan {workspace}/hir-0170-0171-two-layer-still --until-clean",
        f".venv/bin/vfx evals plan {workspace}/hir-0170-0171-two-layer-still",
        ".venv/bin/python evals/graders/judgment_debt_seal_v1.py "
        "capture-authority --trial-id trial-001",
        f".venv/bin/vfx run {workspace}/hir-0170-0171-two-layer-still --rounds 2",
        ".venv/bin/python evals/graders/judgment_debt_seal_v1.py "
        "validate --trial-id trial-001",
    ]
    assert not (
        {argument for command in commands for argument in command}
        & grader.FORBIDDEN_OPTIONS
    )
    with pytest.raises(ValueError, match="invalid trial id"):
        grader.compiled_commands(trial_id="../escape")
    assert "artifacts/" in (REPOSITORY / ".gitignore").read_text(
        encoding="utf-8"
    ).splitlines()


def test_two_layer_still_manifest_rejects_changed_fixture_bytes(tmp_path: Path) -> None:
    grader = _load_grader()
    copied = tmp_path / "fixture"
    shutil.copytree(FIXTURE, copied)
    (copied / "brief.md").write_text("changed\n", encoding="utf-8")
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        grader.load_fixture_manifest(copied / "fixture.json")


def test_two_layer_sparse_form_uses_local_geometry_capability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    grader = _load_grader()
    suite = grader.load_suite_manifest()
    rows = [
        {
            "id": "camera",
            "jit": {
                "depends_on_layers": [],
                "reserved_roles": ["camera.primary"],
                "provides": {"camera": ["camera.primary"]},
            },
        },
        {
            "id": "hero",
            "jit": {
                "depends_on_layers": ["camera"],
                "reserved_roles": ["hero.monolith"],
                "provides": {},
            },
        },
    ]
    monkeypatch.setattr(grader, "read_document", lambda _path: rows)
    selected = SimpleNamespace(
        plan=SimpleNamespace(bundle=SimpleNamespace(root=tmp_path))
    )

    identities, bindings, _by_id = grader._sparse_identity(selected, suite)

    assert bindings == {"camera_layer_id": "camera", "hero_layer_id": "hero"}
    assert identities[1]["slot"] == "hero_mesh"
    assert identities[1]["provides"] == {}


def test_two_layer_still_eval_scripts_parse_without_blender() -> None:
    for path in (FIXTURE / "generate_reference.py", GRADER):
        source = path.read_text(encoding="utf-8")
        ast.parse(source, filename=str(path))
        compile(source, str(path), "exec")
    generator = (FIXTURE / "generate_reference.py").read_text(encoding="utf-8")
    assert 'ENGINE = "BLENDER_EEVEE"' in generator
    assert "available engines" in generator
    assert "BLENDER_EEVEE_NEXT" not in generator
    grader = GRADER.read_text(encoding="utf-8")
    assert '"--python-exit-code"' in grader
