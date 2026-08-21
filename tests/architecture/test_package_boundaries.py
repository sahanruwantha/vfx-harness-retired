from __future__ import annotations

import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[2] / "src" / "vfx_harness"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_runtime_modules_are_grouped_by_responsibility():
    assert sorted(path.name for path in PACKAGE.glob("*.py")) == ["__init__.py"]


def test_domain_does_not_import_runtime_adapters():
    forbidden = (
        "vfx_harness.agents",
        "vfx_harness.application",
        "vfx_harness.assets",
        "vfx_harness.blender",
        "vfx_harness.evaluation",
        "vfx_harness.infrastructure",
        "vfx_harness.observability",
    )
    violations = {
        str(path.relative_to(PACKAGE)): sorted(
            name for name in _imports(path) if name.startswith(forbidden)
        )
        for path in (PACKAGE / "domain").glob("*.py")
    }
    assert not {path: names for path, names in violations.items() if names}


def test_runtime_never_imports_repository_artifact_namespaces():
    forbidden = ("tests", "evals", "artifacts", "shots", "docs")
    violations = {
        str(path.relative_to(PACKAGE)): sorted(
            name
            for name in _imports(path)
            if name in forbidden or name.startswith(tuple(f"{item}." for item in forbidden))
        )
        for path in PACKAGE.rglob("*.py")
    }
    assert not {path: names for path, names in violations.items() if names}


def test_generated_output_paths_are_owned_by_run_artifacts():
    forbidden_parts = {"logs", "renders", ".artifacts", ".snapshots", ".versions"}
    owner = PACKAGE / "observability" / "run_artifacts.py"
    violations: list[str] = []
    for path in PACKAGE.rglob("*.py"):
        if path == owner:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.BinOp)
                and isinstance(node.op, ast.Div)
                and isinstance(node.right, ast.Constant)
                and node.right.value in forbidden_parts
            ):
                violations.append(f"{path.relative_to(PACKAGE)}:{node.lineno}:{node.right.value}")
    assert not violations, (
        "generated output paths must resolve through observability/run_artifacts.py: "
        + ", ".join(violations)
    )
