from __future__ import annotations

import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[3] / "src" / "vfx_harness"
REPOSITORY = PACKAGE.parents[1]
MAX_SOURCE_LINES = 900
# Permanent: Blender launches and AST-parses worker.py by path.
# Follow-on (out of this layout pass): prompts, guardrails, image checks.
# Temporary names are removed as each giant is converted to a subpackage.
OVERSIZE_ALLOWLIST = {
    "blender/worker.py",
    "agents/build_prompts.py",
    "agents/guardrails.py",
    "evidence/checks.py",
}


def _python_sources(root: Path) -> list[Path]:
    return sorted(
        path
        for path in root.rglob("*.py")
        if "__pycache__" not in path.parts and "_spikes" not in path.parts
    )


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


def test_source_modules_stay_under_the_line_budget():
    """New host modules must not grow into another 7k-line file. Split instead."""
    violations: list[str] = []
    for path in _python_sources(PACKAGE):
        rel = str(path.relative_to(PACKAGE))
        if rel in OVERSIZE_ALLOWLIST:
            continue
        count = len(path.read_text(encoding="utf-8").splitlines())
        if count > MAX_SOURCE_LINES:
            violations.append(f"{rel}:{count}")
    assert not violations, "split oversized modules into subpackages: " + ", ".join(violations)


def test_oversize_allowlist_names_existing_files():
    missing = sorted(name for name in OVERSIZE_ALLOWLIST if not (PACKAGE / name).is_file())
    assert not missing, "oversize allowlist names missing files: " + ", ".join(missing)


def test_agent_instructions_have_one_authority():
    assert (REPOSITORY / "AGENTS.md").is_file()
    assert not (REPOSITORY / ".cursor" / "rules").exists()


def test_domain_does_not_import_runtime_adapters():
    forbidden = (
        "vfx_harness.agents",
        "vfx_harness.application",
        "vfx_harness.assets",
        "vfx_harness.blender",
        "vfx_harness.evidence",
        "vfx_harness.evaluation",
        "vfx_harness.infrastructure",
        "vfx_harness.observability",
    )
    violations = {
        str(path.relative_to(PACKAGE)): sorted(name for name in _imports(path) if name.startswith(forbidden))
        for path in _python_sources(PACKAGE / "domain")
    }
    assert not {path: names for path, names in violations.items() if names}


def test_cycle_safe_stdlib_and_pillow_live_at_module_scope():
    """re, datetime, and Pillow are not cycle breakers. bpy/mathutils stay lazy in worker.py."""
    forbidden_modules = {"re", "datetime", "PIL"}
    violations: list[str] = []
    for path in _python_sources(PACKAGE):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for function in (node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))):
            for node in ast.walk(function):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        root = alias.name.split(".", 1)[0]
                        if root in forbidden_modules:
                            violations.append(f"{path.relative_to(PACKAGE)}:{node.lineno}:{alias.name}")
                elif isinstance(node, ast.ImportFrom) and node.module:
                    root = node.module.split(".", 1)[0]
                    if root in forbidden_modules:
                        violations.append(f"{path.relative_to(PACKAGE)}:{node.lineno}:{node.module}")
    assert not violations, "move cycle-safe imports to module scope: " + ", ".join(violations)


def test_domain_function_imports_are_only_documented_cycle_breakers():
    """Domain dependencies belong at module scope unless they break one known cycle.

    Cross-package loops (planner ↔ jit_materialization ↔ plan_gate ↔ plan_tools ↔
    builder) stay function-level in those packages. bpy/mathutils stay lazy in
    blender/worker.py.
    """
    allowed = {"vfx_harness.domain.publish_interfaces"}
    violations: list[str] = []
    for path in _python_sources(PACKAGE / "domain"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for function in (node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))):
            for node in ast.walk(function):
                if isinstance(node, ast.Import):
                    violations.append(
                        f"{path.relative_to(PACKAGE)}:{node.lineno}:" + ",".join(alias.name for alias in node.names)
                    )
                elif isinstance(node, ast.ImportFrom) and node.module not in allowed:
                    violations.append(f"{path.relative_to(PACKAGE)}:{node.lineno}:{node.module}")
    assert not violations, "move safe imports to module scope: " + ", ".join(violations)


def test_runtime_imports_never_execute_inside_loops():
    """Imports in a loop repeat lookup work and hide dependencies in control flow."""
    violations: list[str] = []
    for path in _python_sources(PACKAGE):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for loop in (node for node in ast.walk(tree) if isinstance(node, (ast.For, ast.AsyncFor, ast.While))):
            for node in ast.walk(loop):
                if isinstance(node, ast.Import):
                    names = ",".join(alias.name for alias in node.names)
                    violations.append(f"{path.relative_to(PACKAGE)}:{node.lineno}:{names}")
                elif isinstance(node, ast.ImportFrom):
                    violations.append(f"{path.relative_to(PACKAGE)}:{node.lineno}:{node.module}")
    assert not violations, "move imports outside loops: " + ", ".join(violations)


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
    assert not violations, "generated output paths must resolve through observability/run_artifacts.py: " + ", ".join(
        violations
    )
